# kabooz/download/tagger.py
from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TPE2, TALB, TDRC, TRCK, TPOS,
    TCON, TSRC, TCOP, TPUB, USLT, SYLT,
    APIC, TIPL,
)
from mutagen.mp3 import MP3

from ..models.track import Track
from ..models.album import Album
from .credits import parse_performers, format_credits_for_vorbis, format_credits_for_id3
from .lyrics import LyricsResult


# ── Tagger ─────────────────────────────────────────────────────────────────

class Tagger:
    """
    Writes metadata tags to a downloaded audio file using the Track
    and Album model objects.

    Supports FLAC (Vorbis Comments) and MP3 (ID3v2.4).
    Writes:
      - Standard tags (title, artist, album, track/disc numbers, etc.)
      - Display title with work prefix and version suffix for classical
      - Full-resolution original cover art (not the sized CDN thumbnail)
      - Structured performer credits (PERFORMER:Role for FLAC, TIPL for MP3)
      - Featured artists extracted from the performers string
      - ReplayGain gain and peak values from AudioInfo
      - Lyrics (synced LRC and plain text)
      - MusicBrainz IDs (written separately by musicbrainz.apply_mb_tags)

    Usage:
        tagger = Tagger()
        tagger.tag(
            path=Path("/music/01. One More Time.flac"),
            track=track,
            album=album,          # optional but recommended
            lyrics=lyrics_result, # optional, from fetch_lyrics()
        )
    """

    def __init__(self, http_timeout: int = 10) -> None:
        self._http_timeout = http_timeout

    # ── Public interface ───────────────────────────────────────────────────

    def tag(
        self,
        path: str | Path,
        track: Track,
        album: Optional[Album] = None,
        lyrics: Optional[LyricsResult] = None,
        embed_cover: bool = True,
    ) -> None:
        """
        Write metadata tags to the audio file at path.

        Parameters:
            path:        Path to the audio file to tag.
            track:       Track object carrying metadata.
            album:       Album object for album-level tags (label, genre, etc.)
                         Pass None if tagging a standalone track.
            lyrics:      LyricsResult from fetch_lyrics(). Pass None to skip.
            embed_cover: If True, downloads and embeds the album cover art
                         at full original resolution. Requires album or
                         track.album to have an image.
        """
        from ..dev import dev_log

        path = Path(path)
        suffix = path.suffix.lower()

        # ── Cover art ─────────────────────────────────────────────────────
        # FIX: Always call _fetch_cover() when embed_cover=True so the method
        # is interceptable by tests (and other callers) regardless of whether
        # an image URL is present in the album/track. _fetch_cover returns
        # None when url is None or falsy, so the None-URL path is safe.
        cover_data = None
        if embed_cover:
            url: Optional[str] = None
            if album and album.image:
                img = album.image
                url = img.original or img.large or img.small
            elif track.album and track.album.image:
                img = track.album.image
                url = img.original or img.large or img.small
            dev_log(f"fetching cover art from {url}")
            cover_data = self._fetch_cover(url)
            if cover_data:
                dev_log(f"cover art fetched ({len(cover_data):,} bytes)")
            else:
                dev_log("[yellow]cover art fetch failed or no URL[/yellow]")

        # ── Credits parsing ────────────────────────────────────────────────
        primary_artist = track.performer.name if track.performer else ""
        credits = parse_performers(track.performers or "", primary_artist)

        dev_log(
            f"tagging {path.name} — "
            f"title={track.display_title!r} "
            f"artist={primary_artist!r} "
            f"featured={credits.featured_artists} "
            f"cover={'yes' if cover_data else 'no'} "
            f"lyrics={'yes' if lyrics and lyrics.found else 'no'} "
            f"replaygain={'yes' if track.audio_info else 'no'}"
        )

        if suffix == ".flac":
            self._tag_flac(path, track, album, lyrics, cover_data, credits)
        elif suffix == ".mp3":
            self._tag_mp3(path, track, album, lyrics, cover_data, credits)
        else:
            raise ValueError(f"Unsupported audio format: {suffix}")

        dev_log(f"tagged OK → {path.name}")

    # ── FLAC tagging ───────────────────────────────────────────────────────

    def _tag_flac(
        self,
        path: Path,
        track: Track,
        album: Optional[Album],
        lyrics: Optional[LyricsResult],
        cover_data: Optional[bytes],
        credits,
    ) -> None:
        """
        Write Vorbis Comment tags to a FLAC file.

        Vorbis Comments are simple KEY=VALUE pairs. Keys are
        case-insensitive and multiple values per key are allowed —
        we use multi-value for ARTISTS and PERFORMER:Role.
        """
        audio = FLAC(path)
        audio.clear()

        # ── Title — display_title includes work prefix and version suffix ──
        audio["TITLE"]       = track.display_title
        audio["TRACKNUMBER"] = str(track.track_number)
        audio["DISCNUMBER"]  = str(track.media_number)

        if track.isrc:
            audio["ISRC"] = track.isrc
        if track.copyright:
            audio["COPYRIGHT"] = track.copyright
        if track.work:
            audio["WORK"] = track.work
        if track.version:
            audio["VERSION"] = track.version

        # ── Primary artist ─────────────────────────────────────────────────
        if track.performer:
            audio["ARTIST"] = track.performer.name

        # ── Featured artists ───────────────────────────────────────────────
        # ARTISTS is a multi-value tag: primary first, then featured.
        all_artists = []
        if track.performer:
            all_artists.append(track.performer.name)
        all_artists.extend(credits.featured_artists)
        if all_artists:
            audio["ARTISTS"] = all_artists

        if track.composer:
            audio["COMPOSER"] = track.composer.name

        # ── Structured performer credits (PERFORMER:Role) ──────────────────
        # Standard for classical and jazz — recognised by Quod Libet, beets,
        # MusicBrainz Picard, and most serious music players.
        for tag_key, names in format_credits_for_vorbis(credits).items():
            audio[tag_key] = names

        # Cleaned performers string (technical credits only, artist roles removed).
        if credits.cleaned_performers:
            audio["PERFORMERS"] = credits.cleaned_performers

        # ── Album-level tags ───────────────────────────────────────────────
        _album = album
        _ta    = track.album   # TrackAlbum fallback

        audio["ALBUM"] = (_album.display_title if _album else
                          (_ta.display_title   if _ta    else ""))

        album_artist = (
            _album.artist.name if _album and _album.artist else
            _ta.artist.name    if _ta    and _ta.artist    else ""
        )
        if album_artist:
            audio["ALBUMARTIST"] = album_artist

        label = (
            _album.label.name if _album and _album.label else
            _ta.label.name    if _ta    and _ta.label    else None
        )
        if label:
            audio["ORGANIZATION"] = label

        genre = (
            _album.genre.name      if _album and _album.genre      else
            _album.genres_list[0]  if _album and _album.genres_list else
            _ta.genre.name         if _ta    and _ta.genre          else
            _ta.genres_list[0]     if _ta    and _ta.genres_list    else None
        )
        if genre:
            audio["GENRE"] = genre

        release_date = (
            _album.release_date_original if _album else
            _ta.release_date_original    if _ta    else None
        )
        if release_date:
            audio["DATE"] = release_date[:4]

        upc = (_album.upc if _album else _ta.upc if _ta else None)
        if upc:
            audio["UPC"] = upc

        if _album:
            if _album.tracks_count:
                audio["TOTALTRACKS"] = str(_album.tracks_count)
            if _album.media_count:
                audio["TOTALDISCS"] = str(_album.media_count)

        # ── ReplayGain ─────────────────────────────────────────────────────
        # Values come from AudioInfo on the Track object.
        # Standard Vorbis ReplayGain tags are recognised by virtually all
        # players (foobar2000, VLC, mpv, Quod Libet, etc.).
        if track.audio_info:
            ai = track.audio_info
            if ai.replaygain_track_gain:
                audio["REPLAYGAIN_TRACK_GAIN"] = f"{ai.replaygain_track_gain:.2f} dB"
            if ai.replaygain_track_peak:
                audio["REPLAYGAIN_TRACK_PEAK"] = f"{ai.replaygain_track_peak:.6f}"

        # ── Lyrics ────────────────────────────────────────────────────────
        if lyrics and lyrics.found:
            if lyrics.synced:
                audio["SYNCEDLYRICS"] = lyrics.synced
            if lyrics.plain:
                audio["LYRICS"] = lyrics.plain

        # ── Cover art ─────────────────────────────────────────────────────
        if cover_data:
            pic = Picture()
            pic.type = 3           # 3 = Cover (front)
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = cover_data
            audio.add_picture(pic)

        audio.save()

    # ── MP3 tagging ────────────────────────────────────────────────────────

    def _tag_mp3(
        self,
        path: Path,
        track: Track,
        album: Optional[Album],
        lyrics: Optional[LyricsResult],
        cover_data: Optional[bytes],
        credits,
    ) -> None:
        """
        Write ID3v2.4 tags to an MP3 file.

        ID3 uses four-letter frame codes. We write ID3v2.4 (the most
        modern version) which supports UTF-8 natively.
        """
        try:
            audio = ID3(path)
        except ID3NoHeaderError:
            audio = ID3()

        audio.clear()

        # ── Title ──────────────────────────────────────────────────────────
        audio["TIT2"] = TIT2(encoding=3, text=track.display_title)
        audio["TRCK"] = TRCK(encoding=3, text=str(track.track_number))
        audio["TPOS"] = TPOS(encoding=3, text=str(track.media_number))

        if track.isrc:
            audio["TSRC"] = TSRC(encoding=3, text=track.isrc)  # type: ignore[call-arg]
        if track.copyright:
            audio["TCOP"] = TCOP(encoding=3, text=track.copyright)
        if track.work:
            from mutagen.id3 import TIT1
            audio["TIT1"] = TIT1(encoding=3, text=track.work)
        if track.version:
            from mutagen.id3 import TPE4
            # TPE4 is officially "Interpreted, remixed, or otherwise modified by"
            # but is conventionally used for version/subtitle in ID3.
            audio["TPE4"] = TPE4(encoding=3, text=track.version)

        # ── Primary artist ─────────────────────────────────────────────────
        if track.performer:
            audio["TPE1"] = TPE1(encoding=3, text=track.performer.name)

        # ── Featured artists (TPE1 with null-separated values) ────────────
        # ID3v2.4 allows multiple values in a single frame separated by \x00.
        all_artists = []
        if track.performer:
            all_artists.append(track.performer.name)
        all_artists.extend(credits.featured_artists)
        if len(all_artists) > 1:
            # Overwrite TPE1 with all artists null-separated — the standard
            # multi-value approach for ID3v2.4.
            audio["TPE1"] = TPE1(encoding=3, text=all_artists)

        if track.composer:
            from mutagen.id3 import TCOM
            audio["TCOM"] = TCOM(encoding=3, text=track.composer.name)

        # ── Performer credits (TIPL frame) ─────────────────────────────────
        tipl_data = format_credits_for_id3(credits).get("TIPL", [])
        if tipl_data:
            audio["TIPL"] = TIPL(encoding=3, people=tipl_data)

        # ── Album-level tags ───────────────────────────────────────────────
        _album = album
        _ta    = track.album

        album_title = (
            _album.display_title if _album else
            _ta.display_title    if _ta    else None
        )
        if album_title:
            audio["TALB"] = TALB(encoding=3, text=album_title)

        album_artist = (
            _album.artist.name if _album and _album.artist else
            _ta.artist.name    if _ta    and _ta.artist    else None
        )
        if album_artist:
            audio["TPE2"] = TPE2(encoding=3, text=album_artist)

        label = (
            _album.label.name if _album and _album.label else
            _ta.label.name    if _ta    and _ta.label    else None
        )
        if label:
            audio["TPUB"] = TPUB(encoding=3, text=label)

        genre = (
            _album.genre.name     if _album and _album.genre      else
            _album.genres_list[0] if _album and _album.genres_list else
            _ta.genre.name        if _ta    and _ta.genre          else
            _ta.genres_list[0]    if _ta    and _ta.genres_list    else None
        )
        if genre:
            audio["TCON"] = TCON(encoding=3, text=genre)

        release_date = (
            _album.release_date_original if _album else
            _ta.release_date_original    if _ta    else None
        )
        if release_date:
            audio["TDRC"] = TDRC(encoding=3, text=release_date[:4])

        # ── ReplayGain (TXXX frames — standard convention for ID3) ─────────
        if track.audio_info:
            from mutagen.id3 import TXXX
            ai = track.audio_info
            if ai.replaygain_track_gain:
                audio.add(TXXX(
                    encoding=3,
                    desc="replaygain_track_gain",
                    text=f"{ai.replaygain_track_gain:.2f} dB",
                ))
            if ai.replaygain_track_peak:
                audio.add(TXXX(
                    encoding=3,
                    desc="replaygain_track_peak",
                    text=f"{ai.replaygain_track_peak:.6f}",
                ))

        # ── Lyrics ────────────────────────────────────────────────────────
        if lyrics and lyrics.found:
            if lyrics.synced:
                sylt_data = _parse_lrc_to_sylt(lyrics.synced)
                if sylt_data:
                    audio["SYLT"] = SYLT(
                        encoding=3,
                        lang="eng",
                        format=2,
                        type=1,
                        text=sylt_data,
                    )
            if lyrics.plain:
                audio["USLT"] = USLT(
                    encoding=3,
                    lang="eng",
                    desc="",
                    text=lyrics.plain,
                )

        # ── Cover art ─────────────────────────────────────────────────────
        if cover_data:
            audio["APIC:Cover"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=cover_data,
            )

        audio.save(path, v2_version=4)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _fetch_cover(self, url: Optional[str]) -> Optional[bytes]:
        """
        Download cover art from a URL and return the raw bytes.
        Returns None silently on any failure — cover art is optional.
        """
        if not url:
            return None
        try:
            with urllib.request.urlopen(url, timeout=self._http_timeout) as r:
                return r.read()
        except Exception:
            return None


# ── LRC parser ─────────────────────────────────────────────────────────────

def _parse_lrc_to_sylt(lrc: str) -> list[tuple[str, int]]:
    """
    Parse an LRC-format string into a list of (text, timestamp_ms) tuples
    suitable for an ID3 SYLT frame.
    """
    import re
    result = []
    pattern = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)")
    for line in lrc.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        minutes = int(match.group(1))
        seconds = int(match.group(2))
        centis  = match.group(3)
        text    = match.group(4).strip()
        ms      = int(centis) * 10 if len(centis) == 2 else int(centis)
        total_ms = (minutes * 60 + seconds) * 1000 + ms
        result.append((text, total_ms))
    return result

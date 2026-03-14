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
    APIC, Encoding,
)
from mutagen.mp3 import MP3

from ..models.track import Track
from ..models.album import Album
from .lyrics import LyricsResult


# ── Tagger ─────────────────────────────────────────────────────────────────

class Tagger:
    """
    Writes metadata tags to a downloaded audio file using the Track
    and Album model objects.

    Supports FLAC (Vorbis Comments) and MP3 (ID3v2.4).
    Optionally embeds cover art and lyrics.

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
            embed_cover: If True, downloads and embeds the album cover art.
                         Requires album to be provided and have an image URL.
        """
        path = Path(path)
        suffix = path.suffix.lower()

        cover_data = None
        if embed_cover:
            img = None
        if album and album.image:
            img = album.image
        elif track.album and track.album.image:
            img = track.album.image
        if img:
            url = img.large or img.small
            cover_data = self._fetch_cover(url)

        if suffix == ".flac":
            self._tag_flac(path, track, album, lyrics, cover_data)
        elif suffix == ".mp3":
            self._tag_mp3(path, track, album, lyrics, cover_data)
        else:
            raise ValueError(f"Unsupported audio format: {suffix}")

    # ── FLAC tagging ───────────────────────────────────────────────────────

    def _tag_flac(
        self,
        path: Path,
        track: Track,
        album: Optional[Album],
        lyrics: Optional[LyricsResult],
        cover_data: Optional[bytes],
    ) -> None:
        """
        Write Vorbis Comment tags to a FLAC file.

        Vorbis Comments are simple KEY=VALUE pairs. Keys are
        case-insensitive and multiple values per key are allowed,
        but we write one value per key for maximum compatibility.
        """
        audio = FLAC(path)
        # Clear existing tags to avoid stale values from a previous run.
        audio.clear()

        # ── Track-level tags ───────────────────────────────────────────────
        audio["TITLE"]       = track.title
        audio["TRACKNUMBER"] = str(track.track_number)
        audio["DISCNUMBER"]  = str(track.media_number)

        if track.isrc:
            audio["ISRC"] = track.isrc
        if track.copyright:
            audio["COPYRIGHT"] = track.copyright

        # performers (str) is a comma-separated list of all contributors.
        # performer (Performer) is the primary credited artist.
        if track.performer:
            audio["ARTIST"] = track.performer.name
        if track.performers:
            audio["PERFORMERS"] = track.performers
        if track.composer:
            audio["COMPOSER"] = track.composer.name

        # ── Album-level tags ───────────────────────────────────────────────
        if album:
            audio["ALBUM"] = album.title
            if album.artist:
                audio["ALBUMARTIST"] = album.artist.name
            if album.label:
                audio["ORGANIZATION"] = album.label.name
            if album.genre:
                audio["GENRE"] = album.genre.name
            elif album.genres_list:
                audio["GENRE"] = album.genres_list[0]
            if album.release_date_original:
                audio["DATE"] = album.release_date_original[:4]
            if album.upc:
                audio["UPC"] = album.upc
            if album.tracks_count:
                audio["TOTALTRACKS"] = str(album.tracks_count)
            if album.media_count:
                audio["TOTALDISCS"] = str(album.media_count)
        elif track.album:
            # Fall back to the nested TrackAlbum if no full Album was passed.
            audio["ALBUM"] = track.album.title
            if track.album.artist:
                audio["ALBUMARTIST"] = track.album.artist.name
            if track.album.label:
                audio["ORGANIZATION"] = track.album.label.name
            if track.album.genre:
                audio["GENRE"] = track.album.genre.name
            if track.album.release_date_original:
                audio["DATE"] = track.album.release_date_original[:4]
            if track.album.upc:
                audio["UPC"] = track.album.upc

        # ── Lyrics ────────────────────────────────────────────────────────
        # FLAC stores lyrics in a plain LYRICS Vorbis Comment.
        # There's no standard synced lyrics tag in Vorbis Comments,
        # so we store synced as SYNCEDLYRICS and plain as LYRICS.
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
    ) -> None:
        """
        Write ID3v2.4 tags to an MP3 file.

        ID3 uses four-letter frame codes. We write ID3v2.4 (the most
        modern version) which supports UTF-8 natively.
        """
        try:
            audio = ID3(path)
        except ID3NoHeaderError:
            # File has no ID3 header yet — create one.
            audio = ID3()

        # Clear existing tags.
        audio.clear()

        # ── Track-level tags ───────────────────────────────────────────────
        audio["TIT2"] = TIT2(encoding=3, text=track.title)
        audio["TRCK"] = TRCK(encoding=3, text=str(track.track_number))
        audio["TPOS"] = TPOS(encoding=3, text=str(track.media_number))

        if track.isrc:
            audio["TSRC"] = TSRC(encoding=3, text=track.isrc)  # type: ignore[call-arg]
        if track.copyright:
            audio["TCOP"] = TCOP(encoding=3, text=track.copyright)
        if track.performer:
            audio["TPE1"] = TPE1(encoding=3, text=track.performer.name)
        if track.composer:
            from mutagen.id3 import TCOM
            audio["TCOM"] = TCOM(encoding=3, text=track.composer.name)

        # ── Album-level tags ───────────────────────────────────────────────
        if album:
            audio["TALB"] = TALB(encoding=3, text=album.title)
            if album.artist:
                audio["TPE2"] = TPE2(encoding=3, text=album.artist.name)
            if album.label:
                audio["TPUB"] = TPUB(encoding=3, text=album.label.name)
            if album.genre:
                audio["TCON"] = TCON(encoding=3, text=album.genre.name)
            elif album.genres_list:
                audio["TCON"] = TCON(encoding=3, text=album.genres_list[0])
            if album.release_date_original:
                audio["TDRC"] = TDRC(encoding=3, text=album.release_date_original[:4])
        elif track.album:
            audio["TALB"] = TALB(encoding=3, text=track.album.title)
            if track.album.artist:
                audio["TPE2"] = TPE2(encoding=3, text=track.album.artist.name)
            if track.album.label:
                audio["TPUB"] = TPUB(encoding=3, text=track.album.label.name)
            if track.album.release_date_original:
                audio["TDRC"] = TDRC(encoding=3, text=track.album.release_date_original[:4])

        # ── Lyrics ────────────────────────────────────────────────────────
        if lyrics and lyrics.found:
            if lyrics.synced:
                # SYLT = Synchronised lyrics. Each entry is (text, timestamp_ms).
                # We parse the LRC format into the list of tuples ID3 expects.
                sylt_data = _parse_lrc_to_sylt(lyrics.synced)
                if sylt_data:
                    audio["SYLT"] = SYLT(
                        encoding=3,
                        lang="eng",
                        format=2,      # 2 = milliseconds
                        type=1,        # 1 = lyrics
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
        Returns None silently on any failure — cover art is optional,
        we should never let a failed image download break tagging.
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

    LRC format:
        [mm:ss.xx] Line of lyrics
        [00:13.45] One more time we're gonna celebrate
        [00:17.90] Oh yeah, all right, don't stop the dancing
    """
    import re
    result = []
    pattern = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)")
    for line in lrc.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        minutes  = int(match.group(1))
        seconds  = int(match.group(2))
        centis   = match.group(3)
        text     = match.group(4).strip()

        # Normalise centiseconds to milliseconds regardless of whether
        # the LRC file uses 2 or 3 decimal digits.
        if len(centis) == 2:
            ms = int(centis) * 10
        else:
            ms = int(centis)

        total_ms = (minutes * 60 + seconds) * 1000 + ms
        result.append((text, total_ms))

    return result


# kabooz/download/musicbrainz.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import httpx


# ── Result type ────────────────────────────────────────────────────────────

@dataclass
class MBResult:
    """
    MusicBrainz metadata for a single track.
    All fields are None if the lookup failed or found no match.
    """
    recording_mbid:  Optional[str] = None   # MusicBrainz recording ID
    artist_mbid:     Optional[str] = None   # MusicBrainz artist ID (first credited)
    release_mbid:    Optional[str] = None   # MusicBrainz release (album) ID
    found:           bool = False


# ── Rate limiter ───────────────────────────────────────────────────────────

class _RateLimiter:
    """
    Simple token-bucket rate limiter for the MusicBrainz 1 req/sec rule.
    Thread-safe enough for our use case (GIL protects the float update).
    """
    def __init__(self, min_interval: float = 1.05) -> None:
        self._min_interval = min_interval
        self._last: float = 0.0

    def wait(self) -> None:
        now     = time.monotonic()
        elapsed = now - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()


_limiter = _RateLimiter()

_MB_BASE    = "https://musicbrainz.org/ws/2"
_USER_AGENT = "qobuz-py/0.1 ( https://github.com/you/qobuz-py )"


# ── Public interface ───────────────────────────────────────────────────────

def lookup_isrc(
    isrc: str,
    http_client: Optional[httpx.Client] = None,
) -> MBResult:
    """
    Look up a track by ISRC on MusicBrainz.

    Returns a MBResult with recording_mbid, artist_mbid, and release_mbid
    populated on success. Returns an empty MBResult on any error so the
    caller never has to handle exceptions from this function.

    Rate-limited to 1 request per second per the MusicBrainz API terms.
    Always set a descriptive User-Agent — MB will throttle anonymous bots.

    Parameters:
        isrc:        The ISRC code from the Qobuz track metadata.
        http_client: Optional injected httpx.Client for testing.
    """
    if not isrc:
        return MBResult()

    _limiter.wait()

    own_client = http_client is None
    client = http_client or httpx.Client(
        headers={"User-Agent": _USER_AGENT},
        timeout=10.0,
    )

    try:
        resp = client.get(
            f"{_MB_BASE}/recording",
            params={
                "isrc":  isrc,
                "inc":   "artists releases",
                "fmt":   "json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return MBResult()
    finally:
        if own_client:
            client.close()

    recordings = data.get("recordings", [])
    if not recordings:
        return MBResult()

    # Take the first recording. When multiple recordings share an ISRC
    # (re-releases, compilations) the first is typically the canonical one.
    rec = recordings[0]
    recording_mbid = rec.get("id")

    artist_mbid: Optional[str] = None
    artist_credits = rec.get("artist-credit", [])
    for credit in artist_credits:
        if isinstance(credit, dict) and "artist" in credit:
            artist_mbid = credit["artist"].get("id")
            break

    release_mbid: Optional[str] = None
    releases = rec.get("releases", [])
    if releases:
        release_mbid = releases[0].get("id")

    return MBResult(
        recording_mbid = recording_mbid,
        artist_mbid    = artist_mbid,
        release_mbid   = release_mbid,
        found          = bool(recording_mbid),
    )


def apply_mb_tags(path, mb: MBResult) -> None:
    """
    Write MusicBrainz IDs into an already-tagged audio file.

    FLAC: uses standard Vorbis comment fields (MUSICBRAINZ_TRACKID etc.)
    MP3:  uses TXXX ID3 frames with the conventional description strings.

    Called after the main tagger has already written all other tags, so
    we open, update, and save rather than replacing everything.
    """
    if not mb.found:
        return

    from pathlib import Path as _Path
    path = _Path(path)
    suffix = path.suffix.lower()

    try:
        if suffix == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(path)
            if mb.recording_mbid:
                audio["MUSICBRAINZ_TRACKID"]  = [mb.recording_mbid]
            if mb.artist_mbid:
                audio["MUSICBRAINZ_ARTISTID"] = [mb.artist_mbid]
            if mb.release_mbid:
                audio["MUSICBRAINZ_ALBUMID"]  = [mb.release_mbid]
            audio.save()

        elif suffix == ".mp3":
            from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return
            if mb.recording_mbid:
                tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id",  text=mb.recording_mbid))
            if mb.artist_mbid:
                tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=mb.artist_mbid))
            if mb.release_mbid:
                tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id",  text=mb.release_mbid))
            tags.save()
    except Exception:
        # Non-fatal — MB enrichment is best-effort.
        pass


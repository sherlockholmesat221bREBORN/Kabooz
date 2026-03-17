# kabooz/download/musicbrainz.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class MBResult:
    recording_mbid:     Optional[str] = None
    artist_mbid:        Optional[str] = None
    release_mbid:       Optional[str] = None
    release_group_mbid: Optional[str] = None
    work_mbid:          Optional[str] = None
    found:              bool = False


class _RateLimiter:
    def __init__(self, min_interval: float = 1.1) -> None:
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
_USER_AGENT = "kabooz/0.1 ( https://gitlab.com/kabooz )"


def lookup_isrc(isrc: str, http_client: Optional[httpx.Client] = None) -> MBResult:
    from ..dev import dev_log
    if not isrc:
        return MBResult()

    own_client = http_client is None
    client = http_client or httpx.Client(headers={"User-Agent": _USER_AGENT}, timeout=10.0)

    try:
        # STEP 1: Basic lookup to get Recording ID (ISRC endpoint is restricted)
        dev_log(f"MusicBrainz ISRC lookup — {isrc}")
        _limiter.wait()
        resp = client.get(f"{_MB_BASE}/isrc/{isrc}?fmt=json")
        resp.raise_for_status()
        data = resp.json()
        
        recordings = data.get("recordings", [])
        if not recordings:
            return MBResult()
        
        recording_mbid = recordings[0].get("id")
        
        # STEP 2: Use Recording ID to get rich metadata (This allows inc=release-groups)
        dev_log(f"MusicBrainz fetching rich metadata for RecID: {recording_mbid[:8]}")
        _limiter.wait()
        full_resp = client.get(
            f"{_MB_BASE}/recording/{recording_mbid}?inc=artists+releases+release-groups+work-rels&fmt=json"
        )
        full_resp.raise_for_status()
        rec = full_resp.json()

        # Extract IDs
        artist_mbid = None
        for credit in rec.get("artist-credit", []):
            if isinstance(credit, dict) and "artist" in credit:
                artist_mbid = credit["artist"].get("id")
                break

        release_mbid = None
        release_group_mbid = None
        releases = rec.get("releases", [])
        if releases:
            rel = releases[0]
            release_mbid = rel.get("id")
            rg = rel.get("release-group")
            if rg:
                release_group_mbid = rg.get("id")

        work_mbid = None
        for rel_item in rec.get("relations", []):
            if rel_item.get("target-type") == "work":
                work_mbid = rel_item.get("work", {}).get("id")
                break

        return MBResult(
            recording_mbid=recording_mbid,
            artist_mbid=artist_mbid,
            release_mbid=release_mbid,
            release_group_mbid=release_group_mbid,
            work_mbid=work_mbid,
            found=True
        )

    except Exception as exc:
        dev_log(f"MusicBrainz error: {exc}")
        return MBResult()
    finally:
        if own_client:
            client.close()


def apply_mb_tags(path, mb: MBResult) -> None:
    from ..dev import dev_log
    if not mb.found:
        return

    from pathlib import Path as _Path
    path = _Path(path)
    suffix = path.suffix.lower()

    try:
        if suffix == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(path)
            if mb.recording_mbid: audio["MUSICBRAINZ_TRACKID"] = [mb.recording_mbid]
            if mb.artist_mbid:    audio["MUSICBRAINZ_ARTISTID"] = [mb.artist_mbid]
            if mb.release_mbid:   audio["MUSICBRAINZ_ALBUMID"] = [mb.release_mbid]
            if mb.release_group_mbid: audio["MUSICBRAINZ_RELEASEGROUPID"] = [mb.release_group_mbid]
            if mb.work_mbid:      audio["MUSICBRAINZ_WORKID"] = [mb.work_mbid]
            audio.save()

        elif suffix == ".mp3":
            from mutagen.id3 import ID3, TXXX, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return

            mb_map = {
                "MusicBrainz Track Id": mb.recording_mbid,
                "MusicBrainz Artist Id": mb.artist_mbid,
                "MusicBrainz Album Id": mb.release_mbid,
                "MusicBrainz Release Group Id": mb.release_group_mbid,
                "MusicBrainz Work Id": mb.work_mbid,
            }

            for desc, val in mb_map.items():
                if val:
                    tags.add(TXXX(encoding=3, desc=desc, text=val))
            tags.save()
            
        dev_log(f"MB tags written → {path.name}")
    except Exception as exc:
        dev_log(f"MB tag write failed: {exc}")

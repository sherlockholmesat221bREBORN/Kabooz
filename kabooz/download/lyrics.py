# kabooz/download/lyrics.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import urllib.request
import urllib.parse
import json


# ── Result object ──────────────────────────────────────────────────────────

@dataclass
class LyricsResult:
    """
    Result from a lyrics fetch attempt.

    Attributes:
        synced:    LRC-format synced lyrics string if available, else None.
                   LRC format looks like: [mm:ss.xx] Line of lyrics
        plain:     Plain unsynced lyrics string if available, else None.
        source:    Name of the provider that returned the result.
        found:     False if no lyrics were found at all.
    """
    synced: Optional[str] = None
    plain: Optional[str] = None
    source: str = "lrclib"
    found: bool = False


# ── LRCLIB provider ────────────────────────────────────────────────────────

class LRCLibProvider:
    """
    Fetches lyrics from LRCLIB (https://lrclib.net).

    LRCLIB is a free, open, no-auth lyrics database that provides both
    synced (LRC) and plain text lyrics. It's searched by track title,
    artist name, album title, and track duration — all of which we have
    from the Track object.

    The duration is important: LRCLIB uses it to disambiguate between
    multiple versions of the same song (e.g. album version vs. live).
    We pass it when available but the search still works without it.
    """

    _BASE = "https://lrclib.net/api"

    def __init__(
        self,
        timeout: int = 10,
        fallback_to_plain: bool = True,
    ) -> None:
        self.timeout = timeout
        self.fallback_to_plain = fallback_to_plain

    def fetch(
        self,
        title: str,
        artist: str,
        album: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> LyricsResult:
        """
        Fetch lyrics for a track. Returns a LyricsResult regardless of
        whether lyrics were found — check result.found before using.

        Parameters:
            title:    Track title.
            artist:   Primary artist name.
            album:    Album title (improves matching accuracy).
            duration: Track duration in seconds (improves matching accuracy).
        """
        params: dict[str, str] = {
            "track_name":  title,
            "artist_name": artist,
        }
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = str(duration)

        query = urllib.parse.urlencode(params)
        url = f"{self._BASE}/get?{query}"

        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                if response.status == 404:
                    return LyricsResult(found=False)
                if response.status != 200:
                    return LyricsResult(found=False)
                data = json.loads(response.read().decode("utf-8"))
        except Exception:
            return LyricsResult(found=False)

        synced = data.get("syncedLyrics") or None
        plain  = data.get("plainLyrics") or None

        if not synced and not plain:
            return LyricsResult(found=False)

        # If synced is available, use it as the primary result.
        # If not and fallback is enabled, use plain.
        # If not and fallback is disabled, return only synced (None).
        if not synced and not self.fallback_to_plain:
            return LyricsResult(found=False)

        return LyricsResult(
            synced=synced,
            plain=plain,
            source="lrclib",
            found=True,
        )


# ── Convenience function ───────────────────────────────────────────────────

def fetch_lyrics(
    title: str,
    artist: str,
    album: Optional[str] = None,
    duration: Optional[int] = None,
    fallback_to_plain: bool = True,
    timeout: int = 10,
) -> LyricsResult:
    """
    Module-level convenience wrapper around LRCLibProvider.
    Use this when you don't need to configure or reuse the provider.

        result = fetch_lyrics("One More Time", "Daft Punk", "Discovery", 320)
        if result.found:
            print(result.synced or result.plain)
    """
    provider = LRCLibProvider(
        timeout=timeout,
        fallback_to_plain=fallback_to_plain,
    )
    return provider.fetch(title, artist, album, duration)


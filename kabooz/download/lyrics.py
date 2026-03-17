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
        from ..dev import dev_log

        dev_log(
            f"fetching lyrics — title={title!r} artist={artist!r}"
            + (f" album={album!r}" if album else "")
            + (f" duration={duration}s" if duration else "")
        )

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
                    dev_log("lyrics: not found (404)")
                    return LyricsResult(found=False)
                if response.status != 200:
                    dev_log(f"lyrics: unexpected status {response.status}")
                    return LyricsResult(found=False)
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            dev_log(f"lyrics: fetch error — {exc}")
            return LyricsResult(found=False)

        synced = data.get("syncedLyrics") or None
        plain  = data.get("plainLyrics") or None

        if not synced and not plain:
            dev_log("lyrics: response empty — no lyrics in database")
            return LyricsResult(found=False)

        if not synced and not self.fallback_to_plain:
            dev_log("lyrics: no synced lyrics and fallback disabled")
            return LyricsResult(found=False)

        # ── Priority Logic ─────────────────────────────────────────────────
        # If we have synced lyrics, we ditch the plain text to avoid redundancy.
        if synced and plain:
            dev_log("lyrics: both synced and plain found; discarding plain")
            plain = None
        # ───────────────────────────────────────────────────────────────────

        dev_log(
            f"lyrics: found via lrclib — "
            f"synced={'yes' if synced else 'no'} "
            f"plain={'yes' if plain else 'no'}"
        )

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

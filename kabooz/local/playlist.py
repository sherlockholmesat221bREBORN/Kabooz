# kabooz/local/playlist.py
"""
Shareable TOML playlist format for kabooz.

A playlist file is a self-contained, human-readable TOML document that
can be shared between users. Importing it creates a local playlist in
the user's store without needing a Qobuz account.

File format (version 1):
────────────────────────
    # kabooz-playlist v1
    [playlist]
    name        = "Evening Classical"
    description = "Quiet pieces for the evening"
    created     = "2026-03-15T10:30:00Z"
    version     = "1"
    author      = "maxxx"          # optional

    [[tracks]]
    id       = "12345678"
    title    = "Goldberg Variations - Aria"
    artist   = "Glenn Gould"
    album    = "Goldberg Variations"
    duration = 213
    isrc     = "GBAYE6300001"         # optional

    [[tracks]]
    id       = "87654321"
    title    = "Cello Suite No. 1 - Prélude"
    artist   = "Pablo Casals"
    album    = "Six Cello Suites"
    duration = 177

The `id` field is the Qobuz track ID. When importing, the ID is used
to look up the track; the other fields are used as a fallback display
name if the track is unavailable or the user is offline.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tomli_w

PLAYLIST_VERSION = "1"
_MAGIC_COMMENT   = "# kabooz-playlist v1\n"


@dataclass
class LocalPlaylistTrack:
    id: str
    title: str
    # FIX: artist was a required positional arg; give it a default so callers
    # can construct a minimal track with just id + title (e.g. for duplicate
    # checks or placeholder entries).
    artist: str = ""
    album: str = ""
    duration: int = 0
    isrc: str = ""

    def to_dict(self) -> dict:
        d: dict = {"id": self.id, "title": self.title, "artist": self.artist}
        if self.album:
            d["album"] = self.album
        if self.duration:
            d["duration"] = self.duration
        if self.isrc:
            d["isrc"] = self.isrc
        return d

    @classmethod
    def from_dict(cls, data: dict) -> LocalPlaylistTrack:
        return cls(
            id=str(data["id"]),
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            album=data.get("album", ""),
            duration=int(data.get("duration", 0)),
            isrc=data.get("isrc", ""),
        )


@dataclass
class LocalPlaylist:
    name: str
    description: str = ""
    author: str = ""
    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    version: str = PLAYLIST_VERSION
    tracks: list[LocalPlaylistTrack] = field(default_factory=list)

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    @property
    def total_duration(self) -> int:
        """Total duration in seconds."""
        return sum(t.duration for t in self.tracks)

    def add_track(self, track: LocalPlaylistTrack) -> None:
        # Avoid duplicates by ID.
        if any(t.id == track.id for t in self.tracks):
            return
        self.tracks.append(track)

    def remove_track(self, track_id: str) -> bool:
        before = len(self.tracks)
        self.tracks = [t for t in self.tracks if t.id != track_id]
        return len(self.tracks) < before

    def to_dict(self) -> dict:
        return {
            "playlist": {
                "name":        self.name,
                "description": self.description,
                "author":      self.author,
                "created":     self.created,
                "version":     self.version,
            },
            "tracks": [t.to_dict() for t in self.tracks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> LocalPlaylist:
        meta = data.get("playlist", {})
        tracks = [
            LocalPlaylistTrack.from_dict(t)
            for t in data.get("tracks", [])
            if isinstance(t, dict)
        ]
        return cls(
            name=meta.get("name", "Untitled"),
            description=meta.get("description", ""),
            author=meta.get("author", ""),
            created=meta.get("created", ""),
            version=meta.get("version", PLAYLIST_VERSION),
            tracks=tracks,
        )


def save_playlist(playlist: LocalPlaylist, path: str | Path) -> None:
    """
    Write a LocalPlaylist to a TOML file.

    The magic comment header is prepended so recipients know the format
    at a glance, even before parsing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = tomli_w.dumps(playlist.to_dict())
    path.write_text(_MAGIC_COMMENT + body, encoding="utf-8")


def load_playlist(path: str | Path) -> LocalPlaylist:
    """
    Load a LocalPlaylist from a TOML file.

    Strips the magic comment before parsing, so files saved by
    save_playlist() are loaded correctly.

    Raises ValueError if the file is not a valid kabooz-playlist.
    Raises FileNotFoundError if the path doesn't exist.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    # Strip the magic comment if present.
    if text.startswith(_MAGIC_COMMENT):
        text = text[len(_MAGIC_COMMENT):]

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Not a valid kabooz-playlist file: {exc}") from exc

    if "playlist" not in data:
        raise ValueError(
            "Not a valid kabooz-playlist file: missing [playlist] section."
        )

    return LocalPlaylist.from_dict(data)


def playlist_from_store_tracks(
    name: str,
    tracks: list[dict],
    description: str = "",
    author: str = "",
) -> LocalPlaylist:
    """
    Build a LocalPlaylist from a list of store track dicts
    (as returned by LocalStore.get_playlist_tracks).
    """
    pl = LocalPlaylist(name=name, description=description, author=author)
    for t in tracks:
        pl.add_track(LocalPlaylistTrack(
            id=str(t.get("track_id", "")),
            # FIX: use `or ""` so SQLite NULL columns don't pass None through
            title=t.get("title") or "",
            artist=t.get("artist") or "",
            album=t.get("album") or "",
            duration=int(t.get("duration") or 0),
            isrc=t.get("isrc") or "",
        ))
    return pl

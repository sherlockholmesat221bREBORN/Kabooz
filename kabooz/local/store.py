# kabooz/local/store.py
"""
Local SQLite store for user data that works in all modes, including
token pool mode where write operations against the Qobuz API are disabled.

Database location: {local_data.data_dir}/library.db
All tables use TEXT primary keys (Qobuz IDs) so there's no conflict
between int and string representations across API versions.

Schema
──────
favorites        — locally saved tracks, albums, artists
playlists        — local playlist headers
playlist_tracks  — track entries within a playlist (ordered by position)
history          — track playback/download log
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS favorites (
    id          TEXT NOT NULL,
    type        TEXT NOT NULL CHECK(type IN ('track','album','artist')),
    title       TEXT,
    artist      TEXT,
    extra       TEXT,          -- e.g. album title for tracks, genre for albums
    added_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    metadata    TEXT,          -- full JSON blob of the original API object
    PRIMARY KEY (id, type)
);

CREATE TABLE IF NOT EXISTS playlists (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id    TEXT NOT NULL,
    position    INTEGER NOT NULL,
    title       TEXT,
    artist      TEXT,
    album       TEXT,
    duration    INTEGER,       -- seconds
    isrc        TEXT,
    added_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (playlist_id, track_id)
);

CREATE INDEX IF NOT EXISTS idx_playlist_tracks_pos
    ON playlist_tracks(playlist_id, position);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    TEXT NOT NULL,
    title       TEXT,
    artist      TEXT,
    album       TEXT,
    played_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_history_played
    ON history(played_at DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LocalStore:
    """
    Thread-safe SQLite wrapper for local user data.

    All methods are synchronous. The WAL journal mode allows concurrent
    reads while a write is in progress, which matters on Android/Termux
    where background processes may hold read locks.

    Usage:
        store = LocalStore(Path("~/.local/share/qobuz/library.db"))
        store.add_favorite("12345", "track", title="Billie Jean", artist="Michael Jackson")
        for fav in store.get_favorites("track"):
            print(fav["title"])
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Internals ──────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── Favorites ──────────────────────────────────────────────────────────

    def add_favorite(
        self,
        id: str,
        type: str,                       # 'track', 'album', 'artist'
        title: Optional[str] = None,
        artist: Optional[str] = None,
        extra: Optional[str] = None,     # album title, genre, etc.
        metadata: Optional[dict] = None, # full API object
    ) -> None:
        """Add or update a local favourite. Idempotent on (id, type)."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO favorites (id, type, title, artist, extra, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, type) DO UPDATE SET
                    title    = excluded.title,
                    artist   = excluded.artist,
                    extra    = excluded.extra,
                    metadata = excluded.metadata
                """,
                (
                    str(id), type, title, artist, extra,
                    json.dumps(metadata) if metadata else None,
                ),
            )

    def remove_favorite(self, id: str, type: str) -> bool:
        """Remove a favourite. Returns True if a row was deleted."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM favorites WHERE id=? AND type=?", (str(id), type)
            )
            return cur.rowcount > 0

    def is_favorite(self, id: str, type: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM favorites WHERE id=? AND type=?", (str(id), type)
            ).fetchone()
            return row is not None

    def get_favorites(
        self,
        type: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return favorites, optionally filtered by type, as plain dicts."""
        with self._conn() as conn:
            if type:
                rows = conn.execute(
                    "SELECT * FROM favorites WHERE type=? ORDER BY added_at DESC LIMIT ? OFFSET ?",
                    (type, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM favorites ORDER BY added_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [dict(r) for r in rows]

    def count_favorites(self, type: Optional[str] = None) -> int:
        with self._conn() as conn:
            if type:
                return conn.execute(
                    "SELECT COUNT(*) FROM favorites WHERE type=?", (type,)
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]

    def clear_favorites(self, type: Optional[str] = None) -> int:
        """Delete favorites. Returns number of rows deleted."""
        with self._conn() as conn:
            if type:
                cur = conn.execute("DELETE FROM favorites WHERE type=?", (type,))
            else:
                cur = conn.execute("DELETE FROM favorites")
            return cur.rowcount

    # ── Playlists ──────────────────────────────────────────────────────────

    def create_playlist(self, name: str, description: str = "") -> str:
        """Create a new local playlist and return its UUID."""
        playlist_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO playlists (id, name, description) VALUES (?, ?, ?)",
                (playlist_id, name, description),
            )
        return playlist_id

    def get_playlist(self, playlist_id: str) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM playlists WHERE id=?", (playlist_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_playlist_by_name(self, name: str) -> Optional[dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM playlists WHERE name=? LIMIT 1", (name,)
            ).fetchone()
            return dict(row) if row else None

    def list_playlists(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT p.*, COUNT(pt.track_id) AS track_count "
                "FROM playlists p "
                "LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
                "GROUP BY p.id ORDER BY p.updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE playlists SET name=?, updated_at=? WHERE id=?",
                (name, _now(), playlist_id),
            )

    def delete_playlist(self, playlist_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
            return cur.rowcount > 0

    # ── Playlist tracks ────────────────────────────────────────────────────

    def add_track_to_playlist(
        self,
        playlist_id: str,
        track_id: str,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        duration: Optional[int] = None,
        isrc: Optional[str] = None,
        position: Optional[int] = None,
    ) -> None:
        """
        Append a track to a playlist.
        If position is None, appends after the last current track.
        If the track is already in the playlist, updates its metadata.
        """
        with self._conn() as conn:
            if position is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_tracks WHERE playlist_id=?",
                    (playlist_id,),
                ).fetchone()
                position = row[0]

            conn.execute(
                """
                INSERT INTO playlist_tracks
                    (playlist_id, track_id, position, title, artist, album, duration, isrc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(playlist_id, track_id) DO UPDATE SET
                    title    = excluded.title,
                    artist   = excluded.artist,
                    album    = excluded.album,
                    duration = excluded.duration,
                    isrc     = excluded.isrc
                """,
                (playlist_id, str(track_id), position, title, artist, album, duration, isrc),
            )
            conn.execute(
                "UPDATE playlists SET updated_at=? WHERE id=?",
                (_now(), playlist_id),
            )

    def remove_track_from_playlist(self, playlist_id: str, track_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?",
                (playlist_id, str(track_id)),
            )
            if cur.rowcount:
                conn.execute(
                    "UPDATE playlists SET updated_at=? WHERE id=?",
                    (_now(), playlist_id),
                )
            return cur.rowcount > 0

    def get_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
                (playlist_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def reorder_track(self, playlist_id: str, track_id: str, new_position: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE playlist_tracks SET position=? WHERE playlist_id=? AND track_id=?",
                (new_position, playlist_id, str(track_id)),
            )
            conn.execute(
                "UPDATE playlists SET updated_at=? WHERE id=?",
                (_now(), playlist_id),
            )

    # ── History ────────────────────────────────────────────────────────────

    def log_play(
        self,
        track_id: str,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
    ) -> None:
        """Log a track play/download event."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO history (track_id, title, artist, album) VALUES (?, ?, ?, ?)",
                (str(track_id), title, artist, album),
            )

    def get_history(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM history ORDER BY played_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_history(self) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM history")
            return cur.rowcount

    # ── Bulk sync ──────────────────────────────────────────────────────────

    def sync_favorites_from_api(
        self,
        items: list[dict[str, Any]],
        type: str,
        clear_first: bool = False,
    ) -> int:
        """
        Bulk-load favorites from API response items.

        Parameters:
            items:       List of raw dicts from the Qobuz API (tracks, albums,
                         or artists from get_user_favorites).
            type:        'track', 'album', or 'artist'.
            clear_first: If True, wipes existing favorites of this type before
                         inserting. Use when doing a full sync.

        Returns the number of items inserted/updated.
        """
        if clear_first:
            self.clear_favorites(type)

        count = 0
        for item in items:
            try:
                item_id = str(item.get("id", ""))
                if not item_id:
                    continue

                title  = item.get("title") or item.get("name", "")
                artist = ""
                extra  = ""

                if type == "track":
                    p = item.get("performer") or {}
                    artist = p.get("name", "")
                    extra  = (item.get("album") or {}).get("title", "")
                elif type == "album":
                    a = item.get("artist") or {}
                    artist = a.get("name", "")
                    extra  = (item.get("genre") or {}).get("name", "")
                elif type == "artist":
                    title  = item.get("name", "")

                self.add_favorite(
                    id=item_id,
                    type=type,
                    title=title,
                    artist=artist,
                    extra=extra,
                    metadata=item,
                )
                count += 1
            except Exception:
                continue
        return count

# kabooz/local/export.py
"""
Export utilities: full backup to .tar.gz and favorites to TOML/JSON.

All operations use only the Python standard library (tarfile, json,
tomllib, pathlib). No external compression tools required.

Backup archive layout
─────────────────────
qobuz-backup-{date}/
├── library.db              — full SQLite database
├── config.toml             — config with secrets stripped
├── playlists/
│   ├── my-playlist.toml    — one TOML file per local playlist
│   └── ...
├── favorites/
│   ├── tracks.json         — all favorited tracks
│   ├── albums.json         — all favorited albums
│   └── artists.json        — all favorited artists
└── manifest.json           — archive metadata (version, date, counts)
"""
from __future__ import annotations

import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tomli_w


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _bytes_entry(tar: tarfile.TarFile, arc_path: str, content: bytes) -> None:
    """Add raw bytes as a file entry inside an open TarFile."""
    buf = io.BytesIO(content)
    info = tarfile.TarInfo(name=arc_path)
    info.size = len(content)
    tar.addfile(info, buf)


def _json_entry(tar: tarfile.TarFile, arc_path: str, data: object) -> None:
    content = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    _bytes_entry(tar, arc_path, content)


def backup_to_tar(
    store,                              # LocalStore instance
    config_path: Path,
    playlists_dir: Path,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Create a full backup archive at output_path (.tar.gz).

    If output_path is None, writes to the store's parent directory as
    qobuz-backup-{YYYY-MM-DD}.tar.gz.

    Parameters:
        store:         An open LocalStore instance.
        config_path:   Path to config.toml (secrets are stripped).
        playlists_dir: Directory containing .toml playlist files.
        output_path:   Destination path for the archive.

    Returns the path of the written archive.
    """
    db_path = store._path
    prefix  = f"qobuz-backup-{_now_date()}"

    if output_path is None:
        output_path = db_path.parent / f"{prefix}.tar.gz"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Collect data ───────────────────────────────────────────────────────
    favorites = {
        "tracks":  store.get_favorites("track",  limit=999999),
        "albums":  store.get_favorites("album",  limit=999999),
        "artists": store.get_favorites("artist", limit=999999),
    }

    playlists_meta = store.list_playlists()

    manifest = {
        "version":    "1",
        "created_at": _now_iso(),
        "counts": {
            "favorite_tracks":  len(favorites["tracks"]),
            "favorite_albums":  len(favorites["albums"]),
            "favorite_artists": len(favorites["artists"]),
            "playlists":        len(playlists_meta),
        },
    }

    # ── Strip secrets from config ──────────────────────────────────────────
    config_content = b""
    if config_path.exists():
        import tomllib
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        # Zero out credentials
        if "credentials" in raw:
            raw["credentials"]["app_id"]     = ""
            raw["credentials"]["app_secret"] = ""
            raw["credentials"]["pool"]       = ""
        config_content = tomli_w.dumps(raw).encode("utf-8")

    # ── Write archive ──────────────────────────────────────────────────────
    with tarfile.open(output_path, "w:gz") as tar:

        # manifest
        _json_entry(tar, f"{prefix}/manifest.json", manifest)

        # stripped config
        if config_content:
            _bytes_entry(tar, f"{prefix}/config.toml", config_content)

        # SQLite database (copy as-is — it's a self-contained file)
        if db_path.exists():
            tar.add(db_path, arcname=f"{prefix}/library.db")

        # Favorites as JSON (separate files for readability)
        for ftype, items in favorites.items():
            # Strip raw metadata blobs — they can be large and are
            # reconstructable from the track IDs.
            clean = []
            for item in items:
                d = {k: v for k, v in item.items() if k != "metadata"}
                clean.append(d)
            _json_entry(tar, f"{prefix}/favorites/{ftype}.json", clean)

        # Playlist TOML files
        if playlists_dir.exists():
            for toml_file in sorted(playlists_dir.glob("*.toml")):
                tar.add(
                    toml_file,
                    arcname=f"{prefix}/playlists/{toml_file.name}",
                )
        else:
            # Also export playlists inline from the store
            for pl_meta in playlists_meta:
                pl_id   = pl_meta["id"]
                pl_name = pl_meta["name"]
                tracks  = store.get_playlist_tracks(pl_id)

                from .playlist import LocalPlaylist, LocalPlaylistTrack, save_playlist
                import tempfile, os
                pl = LocalPlaylist(
                    name=pl_name,
                    description=pl_meta.get("description", ""),
                    created=pl_meta.get("created_at", ""),
                )
                for t in tracks:
                    pl.tracks.append(LocalPlaylistTrack(
                        id=str(t.get("track_id", "")),
                        title=t.get("title", ""),
                        artist=t.get("artist", ""),
                        album=t.get("album", ""),
                        duration=int(t.get("duration") or 0),
                        isrc=t.get("isrc", ""),
                    ))

                import tomli_w as _tw
                body = "# qobuz-playlist v1\n" + _tw.dumps(pl.to_dict())
                safe_name = "".join(
                    c if c.isalnum() or c in "._- " else "_"
                    for c in pl_name
                ).strip() or pl_id
                _bytes_entry(
                    tar,
                    f"{prefix}/playlists/{safe_name}.toml",
                    body.encode("utf-8"),
                )

    return output_path


def export_favorites_toml(
    store,
    output_path: Path,
    type: Optional[str] = None,
) -> Path:
    """
    Export local favorites to a human-readable TOML file.

    Parameters:
        store:       An open LocalStore instance.
        output_path: Where to write the TOML file.
        type:        'track', 'album', 'artist', or None for all.

    The output is a valid TOML file that can be inspected, edited,
    and re-imported into another qobuz-py installation.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {
        "meta": {
            "exported_at": _now_iso(),
            "version":     "1",
        }
    }

    types = [type] if type else ["track", "album", "artist"]
    for t in types:
        items = store.get_favorites(t, limit=999999)
        clean = []
        for item in items:
            entry: dict = {"id": item["id"]}
            if item.get("title"):
                entry["title"] = item["title"]
            if item.get("artist"):
                entry["artist"] = item["artist"]
            if item.get("extra"):
                entry["extra"] = item["extra"]
            if item.get("added_at"):
                entry["added_at"] = item["added_at"]
            clean.append(entry)
        if clean:
            data[f"{t}s"] = clean

    output_path.write_text(
        "# qobuz-py favorites export\n" + tomli_w.dumps(data),
        encoding="utf-8",
    )
    return output_path

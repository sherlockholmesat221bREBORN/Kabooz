# kabooz/local/export.py
"""
Export and import utilities: full backup/restore and per-format helpers.

All operations use only the Python standard library (tarfile, json,
tomllib, pathlib). No external compression tools required.

Backup archive layout
─────────────────────
kabooz-backup-{date}/
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

Export functions
────────────────
    backup_to_tar()          — full archive of the library
    export_favorites_toml()  — favorites to a single human-editable TOML

Import functions
────────────────
    import_favorites_toml()  — load favorites from an exported TOML file
    import_playlist_toml()   — load a single kabooz-playlist TOML into the store
    restore_from_tar()       — selective or full restore from a backup archive
"""
from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass, field
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


# ── Export ─────────────────────────────────────────────────────────────────

def backup_to_tar(
    store,                              # LocalStore instance
    config_path: Path,
    playlists_dir: Path,
    output_path: Optional[Path] = None,
) -> Path:
    """
    Create a full backup archive at output_path (.tar.gz).

    If output_path is None, writes to the store's parent directory as
    kabooz-backup-{YYYY-MM-DD}.tar.gz.

    Parameters:
        store:         An open LocalStore instance.
        config_path:   Path to config.toml (secrets are stripped).
        playlists_dir: Directory containing .toml playlist files.
        output_path:   Destination path for the archive.

    Returns the path of the written archive.
    """
    db_path = store._path
    prefix  = f"kabooz-backup-{_now_date()}"

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
            # Export playlists inline from the store
            from .playlist import LocalPlaylist, LocalPlaylistTrack

            for pl_meta in playlists_meta:
                pl_id   = pl_meta["id"]
                pl_name = pl_meta["name"]
                tracks  = store.get_playlist_tracks(pl_id)

                pl = LocalPlaylist(
                    name=pl_name,
                    description=pl_meta.get("description") or "",
                    created=pl_meta.get("created_at") or "",
                )
                for t in tracks:
                    pl.tracks.append(LocalPlaylistTrack(
                        id=str(t.get("track_id") or ""),
                        title=t.get("title") or "",
                        artist=t.get("artist") or "",
                        album=t.get("album") or "",
                        duration=int(t.get("duration") or 0),
                        isrc=t.get("isrc") or "",
                    ))

                body = "# kabooz-playlist v1\n" + tomli_w.dumps(pl.to_dict())
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
    and re-imported via import_favorites_toml().
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
        "# kabooz favorites export\n" + tomli_w.dumps(data),
        encoding="utf-8",
    )
    return output_path


# ── Import ─────────────────────────────────────────────────────────────────

def import_favorites_toml(
    store,
    path: Path,
    merge: bool = True,
) -> int:
    """
    Import favorites from a TOML file produced by export_favorites_toml().

    Parameters:
        store: An open LocalStore instance.
        path:  Path to the .toml favorites export file.
        merge: If True (default), existing favorites are preserved and new
               ones are added or updated (upsert semantics).
               If False, existing favorites for each type present in the
               file are cleared before importing — a full replacement for
               those types only.

    Returns the total number of favorites imported.
    Raises FileNotFoundError if path does not exist.
    Raises ValueError if the file is not a valid favorites export.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")

    # Strip leading comment lines before parsing TOML.
    cleaned_lines = [
        line for line in text.splitlines()
        if not line.startswith("#")
    ]
    text = "\n".join(cleaned_lines)

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Not a valid favorites export file: {exc}") from exc

    # Map plural TOML section names back to the singular store type names.
    _type_map = {"tracks": "track", "albums": "album", "artists": "artist"}

    count = 0
    for plural, type_name in _type_map.items():
        items = data.get(plural)
        if not items:
            continue

        if not merge:
            store.clear_favorites(type_name)

        for entry in items:
            fav_id = entry.get("id")
            if not fav_id:
                continue
            store.add_favorite(
                id=str(fav_id),
                type=type_name,
                title=entry.get("title"),
                artist=entry.get("artist"),
                extra=entry.get("extra"),
            )
            count += 1

    return count


def import_playlist_toml(
    store,
    path: Path,
    overwrite: bool = False,
) -> Optional[str]:
    """
    Import a single kabooz-playlist TOML file into the local store.

    Parameters:
        store:     An open LocalStore instance.
        path:      Path to a kabooz-playlist TOML file (saved by save_playlist).
        overwrite: If True, delete any existing playlist with the same name
                   before importing so the new tracks replace the old ones.
                   If False (default), a playlist with the same name is left
                   untouched and None is returned.

    Returns the playlist_id string of the newly created playlist,
    or None if the playlist was skipped because it already exists.
    Raises ValueError if the file is not a valid kabooz-playlist TOML.
    Raises FileNotFoundError if path does not exist.
    """
    from .playlist import load_playlist

    lpl = load_playlist(path)   # raises ValueError / FileNotFoundError

    existing = store.get_playlist_by_name(lpl.name)
    if existing:
        if not overwrite:
            return None
        store.delete_playlist(existing["id"])

    pl_id = store.create_playlist(lpl.name, lpl.description)
    for track in lpl.tracks:
        store.add_track_to_playlist(
            playlist_id=pl_id,
            track_id=track.id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            duration=track.duration or None,
            isrc=track.isrc or None,
        )
    return pl_id


# ── Restore result ─────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    """
    Summary returned by restore_from_tar().

    Attributes:
        favorites_imported: Total number of favorite entries loaded.
        playlists_imported: Number of playlists successfully imported.
        playlists_skipped:  Number of playlists skipped (already existed and
                            overwrite / merge=False was in effect).
        db_restored:        True when the raw library.db was replaced.
        errors:             List of non-fatal error messages. A non-empty list
                            means some items were skipped but the rest succeeded.
    """
    favorites_imported: int = 0
    playlists_imported: int = 0
    playlists_skipped:  int = 0
    db_restored:        bool = False
    errors:             list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no errors were recorded."""
        return not self.errors


def restore_from_tar(
    store,
    archive_path: Path,
    *,
    restore_favorites: bool = True,
    restore_playlists: bool = True,
    restore_db: bool = False,
    merge: bool = True,
    playlists_dir: Optional[Path] = None,
) -> ImportResult:
    """
    Selectively or fully restore from a backup archive made by backup_to_tar().

    By default only favorites and playlists are restored into the live store
    (merge mode), so existing data is not destroyed.  Pass restore_db=True
    to do a complete database replacement instead.

    Parameters:
        store:             An open LocalStore instance.
        archive_path:      Path to the .tar.gz backup archive.
        restore_favorites: Import favorites from the archive's favorites/*.json
                           files into the live store.
        restore_playlists: Import playlists from the archive's playlists/*.toml
                           files into the live store, and when playlists_dir is
                           provided, also extract the TOML files there.
        restore_db:        Replace the live library.db with the archived copy.
                           This is a destructive, all-or-nothing operation —
                           every current store record is overwritten.  The
                           store should be re-opened after this call; no
                           further store operations are performed when this
                           flag is True.
        merge:             For favorites: upsert (True) or clear-then-insert
                           (False).  For playlists: skip existing names (True)
                           or overwrite them (False / replace).
        playlists_dir:     When set and restore_playlists is True, extracted
                           playlist TOML files are also written to this
                           directory so they can be used by other tools.

    Returns an ImportResult with counts and any non-fatal error messages.
    Raises FileNotFoundError if archive_path does not exist.
    Raises ValueError if the archive is missing a manifest or does not look
    like a valid qobuz backup.
    """
    archive_path = Path(archive_path)
    if not archive_path.exists():
        raise FileNotFoundError(f"Backup archive not found: {archive_path}")

    result = ImportResult()

    try:
        tf = tarfile.open(archive_path, "r:gz")
    except tarfile.TarError as exc:
        raise ValueError(f"Cannot open archive: {exc}") from exc

    with tf as tar:
        names = tar.getnames()

        # ── Validate: must have a manifest ─────────────────────────────────
        manifest_names = [n for n in names if n.endswith("manifest.json")]
        if not manifest_names:
            raise ValueError(
                "Archive does not contain a manifest.json — "
                "this does not look like a qobuz backup."
            )

        # Derive the prefix from the manifest path so we are not
        # hard-coding the date-stamped top-level directory name.
        prefix = manifest_names[0].rsplit("/manifest.json", 1)[0]

        # ── restore_db: atomic file swap ───────────────────────────────────
        if restore_db:
            db_member_name = f"{prefix}/library.db"
            if db_member_name not in names:
                result.errors.append(
                    "restore_db=True but archive contains no library.db"
                )
            else:
                db_member = tar.getmember(db_member_name)
                db_fobj   = tar.extractfile(db_member)
                if db_fobj:
                    # Write to a temp file first then rename atomically so
                    # the live database is never left in a partial state.
                    dest = store._path
                    fd, tmp_path_str = tempfile.mkstemp(
                        dir=dest.parent, suffix=".db.tmp"
                    )
                    tmp_path = Path(tmp_path_str)
                    try:
                        with open(fd, "wb") as out:
                            shutil.copyfileobj(db_fobj, out)
                        shutil.move(str(tmp_path), str(dest))
                        result.db_restored = True
                    except Exception as exc:
                        tmp_path.unlink(missing_ok=True)
                        result.errors.append(f"DB restore failed: {exc}")
            # DB restore replaces everything; skip per-record paths.
            return result

        # ── restore_favorites ──────────────────────────────────────────────
        if restore_favorites:
            _fav_map = {
                f"{prefix}/favorites/tracks.json":  "track",
                f"{prefix}/favorites/albums.json":  "album",
                f"{prefix}/favorites/artists.json": "artist",
            }
            for arc_name, type_name in _fav_map.items():
                if arc_name not in names:
                    continue
                fobj = tar.extractfile(tar.getmember(arc_name))
                if not fobj:
                    continue
                try:
                    items = json.loads(fobj.read().decode("utf-8"))
                except json.JSONDecodeError as exc:
                    result.errors.append(
                        f"Skipping {arc_name}: invalid JSON ({exc})"
                    )
                    continue

                if not merge:
                    store.clear_favorites(type_name)

                for entry in items:
                    fav_id = entry.get("id")
                    if not fav_id:
                        continue
                    try:
                        store.add_favorite(
                            id=str(fav_id),
                            type=type_name,
                            title=entry.get("title"),
                            artist=entry.get("artist"),
                            extra=entry.get("extra"),
                        )
                        result.favorites_imported += 1
                    except Exception as exc:
                        result.errors.append(
                            f"Favorite {fav_id}/{type_name}: {exc}"
                        )

        # ── restore_playlists ──────────────────────────────────────────────
        if restore_playlists:
            pl_members = [
                m for m in tar.getmembers()
                if m.name.startswith(f"{prefix}/playlists/")
                and m.name.endswith(".toml")
                and m.isfile()
            ]

            if playlists_dir:
                playlists_dir = Path(playlists_dir)
                playlists_dir.mkdir(parents=True, exist_ok=True)

            for member in pl_members:
                fobj = tar.extractfile(member)
                if not fobj:
                    continue

                toml_bytes = fobj.read()

                # Optionally write the TOML file to the playlists directory.
                if playlists_dir:
                    filename = Path(member.name).name
                    (playlists_dir / filename).write_bytes(toml_bytes)

                # Import into the store via a temp file because load_playlist()
                # expects a path, not a file-like object.
                tmp_path: Optional[Path] = None
                try:
                    fd, tmp_path_str = tempfile.mkstemp(suffix=".toml")
                    tmp_path = Path(tmp_path_str)
                    with open(fd, "wb") as tmp_f:
                        tmp_f.write(toml_bytes)

                    pl_id = import_playlist_toml(
                        store,
                        tmp_path,
                        overwrite=not merge,
                    )
                    if pl_id is None:
                        result.playlists_skipped += 1
                    else:
                        result.playlists_imported += 1
                except Exception as exc:
                    result.errors.append(
                        f"Playlist {Path(member.name).name}: {exc}"
                    )
                finally:
                    if tmp_path is not None:
                        tmp_path.unlink(missing_ok=True)

    return result

# tests/test_import.py
"""
Tests for the import and restore counterparts to the export utilities:

    import_favorites_toml()   — load favorites from an exported TOML file
    import_playlist_toml()    — load a single playlist TOML into the store
    restore_from_tar()        — selective or full restore from a backup archive
"""
import json
import tarfile
import io
from pathlib import Path

import pytest

from kabooz.local.store import LocalStore
from kabooz.local.playlist import (
    LocalPlaylist, LocalPlaylistTrack, save_playlist,
)
from kabooz.local.export import (
    backup_to_tar,
    export_favorites_toml,
    import_favorites_toml,
    import_playlist_toml,
    restore_from_tar,
    ImportResult,
)


# ── Shared fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "library.db")


@pytest.fixture
def config_path(tmp_path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text('[credentials]\napp_id = ""\napp_secret = ""\npool = ""\n')
    return p


def _make_playlist_toml(path: Path, name: str = "My Playlist") -> Path:
    """Write a minimal qobuz-playlist TOML to path and return it."""
    pl = LocalPlaylist(name=name, description="A test playlist")
    pl.add_track(LocalPlaylistTrack(
        id="11111", title="One More Time", artist="Daft Punk",
        album="Discovery", duration=320,
    ))
    pl.add_track(LocalPlaylistTrack(
        id="22222", title="Harder Better Faster Stronger", artist="Daft Punk",
        album="Discovery", duration=224,
    ))
    save_playlist(pl, path)
    return path


# ── import_favorites_toml ──────────────────────────────────────────────────

def test_import_favorites_toml_roundtrip(tmp_path, store):
    """Export then re-import — counts and titles must match."""
    store.add_favorite("1", "track", title="Track A", artist="Artist A")
    store.add_favorite("2", "album", title="Album B", artist="Artist B")

    toml_path = tmp_path / "favs.toml"
    export_favorites_toml(store, toml_path)

    # Import into a fresh store.
    store2 = LocalStore(tmp_path / "library2.db")
    n = import_favorites_toml(store2, toml_path)

    assert n == 2
    tracks = store2.get_favorites("track")
    albums = store2.get_favorites("album")
    assert len(tracks) == 1
    assert len(albums) == 1
    assert tracks[0]["title"] == "Track A"
    assert albums[0]["title"] == "Album B"


def test_import_favorites_toml_merge_keeps_existing(tmp_path, store):
    """merge=True (default) does not wipe records absent from the file."""
    store.add_favorite("99", "track", title="Existing Track")

    store2 = LocalStore(tmp_path / "src.db")
    store2.add_favorite("1", "track", title="New Track")
    toml_path = tmp_path / "favs.toml"
    export_favorites_toml(store2, toml_path)

    import_favorites_toml(store, toml_path, merge=True)

    tracks = store.get_favorites("track")
    ids = {t["id"] for t in tracks}
    assert "99" in ids
    assert "1" in ids


def test_import_favorites_toml_no_merge_replaces(tmp_path, store):
    """merge=False clears existing records of the same type first."""
    store.add_favorite("99", "track", title="Old Track")

    store2 = LocalStore(tmp_path / "src.db")
    store2.add_favorite("1", "track", title="New Track")
    toml_path = tmp_path / "favs.toml"
    export_favorites_toml(store2, toml_path)

    import_favorites_toml(store, toml_path, merge=False)

    tracks = store.get_favorites("track")
    ids = {t["id"] for t in tracks}
    assert "99" not in ids
    assert "1" in ids


def test_import_favorites_toml_type_isolation(tmp_path, store):
    """Clearing tracks (merge=False) does not touch albums."""
    store.add_favorite("10", "track", title="Track")
    store.add_favorite("20", "album", title="Album")

    # Export only the track.
    store2 = LocalStore(tmp_path / "src.db")
    store2.add_favorite("99", "track", title="Replacement")
    toml_path = tmp_path / "tracks_only.toml"
    export_favorites_toml(store2, toml_path, type="track")

    import_favorites_toml(store, toml_path, merge=False)

    # Track "10" should be gone, "99" present; album "20" untouched.
    tracks = store.get_favorites("track")
    albums = store.get_favorites("album")
    assert {t["id"] for t in tracks} == {"99"}
    assert {a["id"] for a in albums} == {"20"}


def test_import_favorites_toml_missing_file(tmp_path, store):
    with pytest.raises(FileNotFoundError):
        import_favorites_toml(store, tmp_path / "does_not_exist.toml")


def test_import_favorites_toml_invalid_file(tmp_path, store):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is not valid toml = = =")
    with pytest.raises(ValueError, match="valid favorites export"):
        import_favorites_toml(store, bad)


def test_import_favorites_toml_empty_file(tmp_path, store):
    """A file with only the meta section imports 0 favorites."""
    store2 = LocalStore(tmp_path / "empty.db")
    toml_path = tmp_path / "empty.toml"
    export_favorites_toml(store2, toml_path)

    n = import_favorites_toml(store, toml_path)
    assert n == 0


# ── import_playlist_toml ───────────────────────────────────────────────────

def test_import_playlist_toml_basic(tmp_path, store):
    toml_path = _make_playlist_toml(tmp_path / "pl.toml")

    pl_id = import_playlist_toml(store, toml_path)

    assert pl_id is not None
    pl = store.get_playlist(pl_id)
    assert pl["name"] == "My Playlist"
    tracks = store.get_playlist_tracks(pl_id)
    assert len(tracks) == 2
    assert tracks[0]["title"] == "One More Time"
    assert tracks[1]["title"] == "Harder Better Faster Stronger"


def test_import_playlist_toml_track_fields(tmp_path, store):
    """Artist, album, duration and ISRC are preserved."""
    pl = LocalPlaylist(name="Tagged Playlist")
    pl.add_track(LocalPlaylistTrack(
        id="99999", title="Some Track", artist="Some Artist",
        album="Some Album", duration=180, isrc="USRC10000001",
    ))
    path = tmp_path / "tagged.toml"
    save_playlist(pl, path)

    pl_id = import_playlist_toml(store, path)
    tracks = store.get_playlist_tracks(pl_id)

    assert tracks[0]["artist"] == "Some Artist"
    assert tracks[0]["album"]  == "Some Album"
    assert tracks[0]["duration"] == 180
    assert tracks[0]["isrc"] == "USRC10000001"


def test_import_playlist_toml_skip_existing(tmp_path, store):
    """By default an existing playlist with the same name is skipped."""
    toml_path = _make_playlist_toml(tmp_path / "pl.toml")

    pl_id_1 = import_playlist_toml(store, toml_path)
    assert pl_id_1 is not None

    # Second import: should return None (skipped).
    pl_id_2 = import_playlist_toml(store, toml_path)
    assert pl_id_2 is None

    # Only one playlist should exist.
    assert len(store.list_playlists()) == 1


def test_import_playlist_toml_overwrite(tmp_path, store):
    """overwrite=True replaces the existing playlist."""
    toml_path = _make_playlist_toml(tmp_path / "pl.toml")
    pl_id_1 = import_playlist_toml(store, toml_path)
    assert pl_id_1 is not None

    pl_id_2 = import_playlist_toml(store, toml_path, overwrite=True)
    assert pl_id_2 is not None
    assert pl_id_2 != pl_id_1       # new ID after delete+recreate

    assert len(store.list_playlists()) == 1
    tracks = store.get_playlist_tracks(pl_id_2)
    assert len(tracks) == 2


def test_import_playlist_toml_track_order_preserved(tmp_path, store):
    """Tracks must come back in the same order they were saved in."""
    pl = LocalPlaylist(name="Order Test")
    for i, title in enumerate(["Alpha", "Beta", "Gamma", "Delta"], start=1):
        pl.add_track(LocalPlaylistTrack(id=str(i), title=title))
    path = tmp_path / "order.toml"
    save_playlist(pl, path)

    pl_id = import_playlist_toml(store, path)
    titles = [t["title"] for t in store.get_playlist_tracks(pl_id)]
    assert titles == ["Alpha", "Beta", "Gamma", "Delta"]


def test_import_playlist_toml_invalid_file(tmp_path, store):
    bad = tmp_path / "bad.toml"
    bad.write_text("not valid toml = = =")
    with pytest.raises(ValueError):
        import_playlist_toml(store, bad)


def test_import_playlist_toml_missing_file(tmp_path, store):
    with pytest.raises(FileNotFoundError):
        import_playlist_toml(store, tmp_path / "gone.toml")


# ── restore_from_tar ───────────────────────────────────────────────────────

@pytest.fixture
def backup(tmp_path, store, config_path) -> Path:
    """Build a populated backup archive and return its path."""
    store.add_favorite("1", "track", title="Track A", artist="Artist A")
    store.add_favorite("2", "album", title="Album B", artist="Artist B")

    pl_id = store.create_playlist("Restored PL")
    store.add_track_to_playlist(pl_id, "1", title="Track A", artist="Artist A")

    output = tmp_path / "backup.tar.gz"
    backup_to_tar(
        store=store,
        config_path=config_path,
        playlists_dir=tmp_path / "no_such_dir",  # forces inline export
        output_path=output,
    )
    return output


def test_restore_favorites_from_tar(tmp_path, backup):
    """Favorites are loaded into a fresh store from the archive."""
    store2 = LocalStore(tmp_path / "fresh.db")
    result = restore_from_tar(store2, backup, restore_playlists=False)

    assert result.ok
    assert result.favorites_imported == 2
    assert result.playlists_imported == 0

    tracks = store2.get_favorites("track")
    albums = store2.get_favorites("album")
    assert len(tracks) == 1
    assert len(albums) == 1
    assert tracks[0]["title"] == "Track A"


def test_restore_playlists_from_tar(tmp_path, backup):
    """Playlists are loaded into a fresh store from the archive."""
    store2 = LocalStore(tmp_path / "fresh.db")
    result = restore_from_tar(store2, backup, restore_favorites=False)

    assert result.ok
    assert result.playlists_imported == 1
    assert result.favorites_imported == 0

    pls = store2.list_playlists()
    assert len(pls) == 1
    assert pls[0]["name"] == "Restored PL"
    tracks = store2.get_playlist_tracks(pls[0]["id"])
    assert len(tracks) == 1
    assert tracks[0]["title"] == "Track A"


def test_restore_playlists_skipped_on_duplicate(tmp_path, backup):
    """Existing playlists are skipped (merge=True default)."""
    store2 = LocalStore(tmp_path / "fresh.db")
    restore_from_tar(store2, backup, restore_favorites=False)

    # Second restore: playlist already exists, should be skipped.
    result = restore_from_tar(store2, backup, restore_favorites=False)
    assert result.playlists_skipped == 1
    assert result.playlists_imported == 0

    # Still only one playlist.
    assert len(store2.list_playlists()) == 1


def test_restore_playlists_overwrite_on_no_merge(tmp_path, backup):
    """merge=False causes existing playlists to be overwritten."""
    store2 = LocalStore(tmp_path / "fresh.db")
    restore_from_tar(store2, backup, restore_favorites=False)

    result = restore_from_tar(
        store2, backup, restore_favorites=False, merge=False
    )
    assert result.playlists_imported == 1
    assert result.playlists_skipped == 0
    assert len(store2.list_playlists()) == 1


def test_restore_extracts_toml_files(tmp_path, backup):
    """When playlists_dir is set, TOML files are written to disk."""
    store2 = LocalStore(tmp_path / "fresh.db")
    out_dir = tmp_path / "playlists_out"

    restore_from_tar(
        store2, backup,
        restore_favorites=False,
        playlists_dir=out_dir,
    )

    toml_files = list(out_dir.glob("*.toml"))
    assert len(toml_files) == 1
    content = toml_files[0].read_text()
    assert "Restored PL" in content


def test_restore_db_replaces_database(tmp_path, backup):
    """restore_db=True swaps out the live library.db atomically."""
    store2 = LocalStore(tmp_path / "target.db")
    store2.add_favorite("99", "track", title="Should Disappear")

    result = restore_from_tar(store2, backup, restore_db=True)

    assert result.db_restored is True
    assert result.ok

    # Re-open the store from the same path; it should now contain the
    # data from the backup, not the "Should Disappear" record.
    store3 = LocalStore(tmp_path / "target.db")
    track_ids = {t["id"] for t in store3.get_favorites("track")}
    assert "99" not in track_ids
    assert "1" in track_ids


def test_restore_db_missing_in_archive(tmp_path, config_path):
    """restore_db=True with an archive that has no library.db records an error."""
    store_src = LocalStore(tmp_path / "src.db")

    # Build an archive without a DB file (store DB does not exist yet
    # because no operations have been performed — create a minimal one
    # then delete it before archiving to simulate a DB-less archive).
    minimal_archive = tmp_path / "no_db.tar.gz"
    prefix = "qobuz-backup-test"
    with tarfile.open(minimal_archive, "w:gz") as tar:
        manifest = json.dumps({"version": "1", "created_at": "x", "counts": {}})
        content  = manifest.encode()
        buf      = io.BytesIO(content)
        info     = tarfile.TarInfo(name=f"{prefix}/manifest.json")
        info.size = len(content)
        tar.addfile(info, buf)

    store2 = LocalStore(tmp_path / "target.db")
    result = restore_from_tar(store2, minimal_archive, restore_db=True)

    assert result.db_restored is False
    assert len(result.errors) == 1
    assert "no library.db" in result.errors[0]


def test_restore_full_roundtrip(tmp_path, backup):
    """All data round-trips through backup + restore with no loss."""
    store2 = LocalStore(tmp_path / "fresh.db")
    result = restore_from_tar(store2, backup)

    assert result.ok
    assert result.favorites_imported == 2
    assert result.playlists_imported == 1

    assert store2.count_favorites("track") == 1
    assert store2.count_favorites("album") == 1
    pls = store2.list_playlists()
    assert len(pls) == 1
    assert pls[0]["track_count"] == 1


def test_restore_from_missing_archive(tmp_path, store):
    with pytest.raises(FileNotFoundError):
        restore_from_tar(store, tmp_path / "nonexistent.tar.gz")


def test_restore_from_invalid_archive(tmp_path, store):
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tar file at all")
    with pytest.raises(ValueError):
        restore_from_tar(store, bad)


def test_restore_from_archive_without_manifest(tmp_path, store):
    """An archive with no manifest.json is rejected with ValueError."""
    arc = tmp_path / "no_manifest.tar.gz"
    with tarfile.open(arc, "w:gz") as tar:
        content = b"some data"
        buf  = io.BytesIO(content)
        info = tarfile.TarInfo(name="prefix/something.txt")
        info.size = len(content)
        tar.addfile(info, buf)

    with pytest.raises(ValueError, match="manifest.json"):
        restore_from_tar(store, arc)


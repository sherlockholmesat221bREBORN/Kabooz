# tests/test_local.py
"""
Tests for the local data layer: LocalStore (SQLite), LocalPlaylist (TOML),
and the backup/export utilities.
"""
import json
import tarfile
from pathlib import Path

import pytest

from kabooz.local.store import LocalStore
from kabooz.local.playlist import (
    LocalPlaylist, LocalPlaylistTrack,
    load_playlist, save_playlist,
    playlist_from_store_tracks, PLAYLIST_VERSION,
)
from kabooz.local.export import backup_to_tar, export_favorites_toml


# ── LocalStore — favorites ─────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "library.db")


def test_add_and_get_favorite(store):
    store.add_favorite("12345", "track", title="One More Time", artist="Daft Punk")
    favs = store.get_favorites("track")
    assert len(favs) == 1
    assert favs[0]["title"] == "One More Time"
    assert favs[0]["artist"] == "Daft Punk"


def test_add_favorite_idempotent(store):
    store.add_favorite("12345", "track", title="Old Title")
    store.add_favorite("12345", "track", title="New Title")
    favs = store.get_favorites("track")
    assert len(favs) == 1
    assert favs[0]["title"] == "New Title"


def test_remove_favorite(store):
    store.add_favorite("12345", "track", title="One More Time")
    removed = store.remove_favorite("12345", "track")
    assert removed is True
    assert store.get_favorites("track") == []


def test_remove_nonexistent_returns_false(store):
    assert store.remove_favorite("99999", "track") is False


def test_is_favorite(store):
    store.add_favorite("12345", "track")
    assert store.is_favorite("12345", "track") is True
    assert store.is_favorite("12345", "album") is False
    assert store.is_favorite("99999", "track") is False


def test_count_favorites(store):
    store.add_favorite("1", "track")
    store.add_favorite("2", "track")
    store.add_favorite("3", "album")
    assert store.count_favorites("track") == 2
    assert store.count_favorites("album") == 1
    assert store.count_favorites() == 3


def test_clear_favorites_by_type(store):
    store.add_favorite("1", "track")
    store.add_favorite("2", "album")
    store.clear_favorites("track")
    assert store.count_favorites("track") == 0
    assert store.count_favorites("album") == 1


def test_favorites_filter_by_type(store):
    store.add_favorite("1", "track", title="T1")
    store.add_favorite("2", "album", title="A1")
    tracks = store.get_favorites("track")
    albums = store.get_favorites("album")
    assert len(tracks) == 1
    assert len(albums) == 1
    assert tracks[0]["title"] == "T1"


def test_sync_favorites_from_api(store):
    items = [
        {"id": 111, "title": "Track A", "performer": {"name": "Artist A"},
         "album": {"title": "Album A"}},
        {"id": 222, "title": "Track B", "performer": {"name": "Artist B"},
         "album": {"title": "Album B"}},
    ]
    n = store.sync_favorites_from_api(items, "track")
    assert n == 2
    favs = store.get_favorites("track")
    assert len(favs) == 2


def test_sync_favorites_clear_first(store):
    store.add_favorite("999", "track", title="Old")
    items = [{"id": 111, "title": "New", "performer": {"name": "A"},
              "album": {"title": "B"}}]
    store.sync_favorites_from_api(items, "track", clear_first=True)
    favs = store.get_favorites("track")
    assert len(favs) == 1
    assert favs[0]["title"] == "New"


# ── LocalStore — playlists ─────────────────────────────────────────────────

def test_create_and_get_playlist(store):
    pl_id = store.create_playlist("My Playlist", "A description")
    pl = store.get_playlist(pl_id)
    assert pl is not None
    assert pl["name"] == "My Playlist"
    assert pl["description"] == "A description"


def test_get_playlist_by_name(store):
    store.create_playlist("Beethoven")
    pl = store.get_playlist_by_name("Beethoven")
    assert pl is not None
    assert pl["name"] == "Beethoven"


def test_get_playlist_by_name_missing(store):
    assert store.get_playlist_by_name("Nonexistent") is None


def test_list_playlists(store):
    store.create_playlist("PL1")
    store.create_playlist("PL2")
    pls = store.list_playlists()
    assert len(pls) == 2


def test_rename_playlist(store):
    pl_id = store.create_playlist("Old Name")
    store.rename_playlist(pl_id, "New Name")
    pl = store.get_playlist(pl_id)
    assert pl["name"] == "New Name"


def test_delete_playlist(store):
    pl_id = store.create_playlist("To Delete")
    deleted = store.delete_playlist(pl_id)
    assert deleted is True
    assert store.get_playlist(pl_id) is None


def test_delete_nonexistent_playlist(store):
    assert store.delete_playlist("nonexistent-id") is False


def test_add_track_to_playlist(store):
    pl_id = store.create_playlist("Test")
    store.add_track_to_playlist(pl_id, "12345",
                                title="One More Time", artist="Daft Punk")
    tracks = store.get_playlist_tracks(pl_id)
    assert len(tracks) == 1
    assert tracks[0]["title"] == "One More Time"


def test_add_track_appends_position(store):
    pl_id = store.create_playlist("Test")
    store.add_track_to_playlist(pl_id, "1", title="A")
    store.add_track_to_playlist(pl_id, "2", title="B")
    tracks = store.get_playlist_tracks(pl_id)
    assert tracks[0]["title"] == "A"
    assert tracks[1]["title"] == "B"
    assert tracks[0]["position"] < tracks[1]["position"]


def test_remove_track_from_playlist(store):
    pl_id = store.create_playlist("Test")
    store.add_track_to_playlist(pl_id, "12345")
    removed = store.remove_track_from_playlist(pl_id, "12345")
    assert removed is True
    assert store.get_playlist_tracks(pl_id) == []


def test_delete_playlist_cascades_tracks(store):
    pl_id = store.create_playlist("Test")
    store.add_track_to_playlist(pl_id, "12345")
    store.delete_playlist(pl_id)
    # Tracks should be gone (CASCADE).
    assert store.get_playlist_tracks(pl_id) == []


def test_playlist_track_count_in_list(store):
    pl_id = store.create_playlist("Test")
    store.add_track_to_playlist(pl_id, "1")
    store.add_track_to_playlist(pl_id, "2")
    pls = store.list_playlists()
    assert pls[0]["track_count"] == 2


# ── LocalStore — history ───────────────────────────────────────────────────

def test_log_and_get_history(store):
    store.log_play("12345", title="One More Time", artist="Daft Punk")
    history = store.get_history(limit=10)
    assert len(history) == 1
    assert history[0]["title"] == "One More Time"


def test_history_ordered_by_recency(store):
    store.log_play("1", title="First")
    store.log_play("2", title="Second")
    history = store.get_history(limit=10)
    # Most recent first.
    assert history[0]["title"] == "Second"


def test_clear_history(store):
    store.log_play("1")
    store.log_play("2")
    n = store.clear_history()
    assert n == 2
    assert store.get_history() == []


# ── LocalPlaylist — TOML format ────────────────────────────────────────────

def make_lpl(name="Test Playlist") -> LocalPlaylist:
    pl = LocalPlaylist(name=name, description="A test")
    pl.add_track(LocalPlaylistTrack(
        id="12345", title="One More Time",
        artist="Daft Punk", album="Discovery",
        duration=320, isrc="GBDCE0000001",
    ))
    pl.add_track(LocalPlaylistTrack(
        id="67890", title="Harder Better Faster Stronger",
        artist="Daft Punk", album="Discovery",
        duration=224,
    ))
    return pl


def test_playlist_track_count():
    pl = make_lpl()
    assert pl.track_count == 2


def test_playlist_total_duration():
    pl = make_lpl()
    assert pl.total_duration == 544


def test_playlist_add_track_no_duplicate():
    pl = make_lpl()
    pl.add_track(LocalPlaylistTrack(id="12345", title="Dup"))
    assert pl.track_count == 2


def test_playlist_remove_track():
    pl = make_lpl()
    removed = pl.remove_track("12345")
    assert removed is True
    assert pl.track_count == 1


def test_playlist_remove_nonexistent():
    pl = make_lpl()
    assert pl.remove_track("99999") is False


def test_save_and_load_playlist(tmp_path):
    pl = make_lpl("Evening Classical")
    path = tmp_path / "evening.toml"
    save_playlist(pl, path)
    loaded = load_playlist(path)
    assert loaded.name == "Evening Classical"
    assert loaded.track_count == 2
    assert loaded.tracks[0].id == "12345"
    assert loaded.tracks[0].isrc == "GBDCE0000001"


def test_load_playlist_magic_comment_stripped(tmp_path):
    """Files with the # qobuz-playlist v1 header parse correctly."""
    pl = make_lpl()
    path = tmp_path / "pl.toml"
    save_playlist(pl, path)
    text = path.read_text()
    assert text.startswith("# qobuz-playlist v1")
    # Still loads correctly.
    loaded = load_playlist(path)
    assert loaded.track_count == 2


def test_load_invalid_file_raises(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("this is not valid toml = = =")
    with pytest.raises(ValueError):
        load_playlist(path)


def test_load_missing_playlist_section_raises(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[something_else]\nkey = 'value'\n")
    with pytest.raises(ValueError, match="missing \\[playlist\\]"):
        load_playlist(path)


def test_playlist_from_store_tracks(store):
    pl_id = store.create_playlist("From Store")
    store.add_track_to_playlist(pl_id, "1", title="A", artist="X")
    store.add_track_to_playlist(pl_id, "2", title="B", artist="Y")
    tracks = store.get_playlist_tracks(pl_id)
    lpl = playlist_from_store_tracks("From Store", tracks)
    assert lpl.name == "From Store"
    assert lpl.track_count == 2


def test_playlist_version():
    pl = make_lpl()
    assert pl.version == PLAYLIST_VERSION


# ── export utilities ───────────────────────────────────────────────────────

def test_export_favorites_toml(tmp_path, store):
    store.add_favorite("1", "track", title="Track A", artist="Artist A")
    store.add_favorite("2", "album", title="Album B", artist="Artist B")

    path = tmp_path / "favorites.toml"
    result = export_favorites_toml(store, path)

    assert result == path
    assert path.exists()
    text = path.read_text()
    assert "Track A" in text
    assert "Album B" in text


def test_export_favorites_toml_type_filter(tmp_path, store):
    store.add_favorite("1", "track", title="Track A")
    store.add_favorite("2", "album", title="Album B")

    path = tmp_path / "tracks_only.toml"
    export_favorites_toml(store, path, type="track")

    text = path.read_text()
    assert "Track A" in text
    assert "Album B" not in text


def test_backup_to_tar(tmp_path, store):
    store.add_favorite("1", "track", title="Test Track")
    pl_id = store.create_playlist("Test PL")
    store.add_track_to_playlist(pl_id, "1", title="Test Track")

    # Write a minimal config file.
    config_path = tmp_path / "config.toml"
    config_path.write_text('[credentials]\napp_id = ""\napp_secret = ""\npool = ""\n')

    playlists_dir = tmp_path / "playlists"

    output = tmp_path / "backup.tar.gz"
    result = backup_to_tar(
        store=store,
        config_path=config_path,
        playlists_dir=playlists_dir,
        output_path=output,
    )

    assert result == output
    assert output.exists()
    assert output.stat().st_size > 0

    # Verify archive contents.
    with tarfile.open(output, "r:gz") as tar:
        names = tar.getnames()

    assert any("manifest.json" in n for n in names)
    assert any("library.db" in n for n in names)
    assert any("tracks.json" in n for n in names)


def test_backup_manifest_counts(tmp_path, store):
    store.add_favorite("1", "track", title="T1")
    store.add_favorite("2", "track", title="T2")
    store.add_favorite("3", "album", title="A1")

    config_path = tmp_path / "config.toml"
    config_path.write_text('[credentials]\napp_id = ""\napp_secret = ""\npool = ""\n')

    output = tmp_path / "backup.tar.gz"
    backup_to_tar(store=store, config_path=config_path,
                  playlists_dir=tmp_path / "pl", output_path=output)

    with tarfile.open(output, "r:gz") as tar:
        for member in tar.getmembers():
            if "manifest.json" in member.name:
                f = tar.extractfile(member)
                manifest = json.loads(f.read())
                break

    assert manifest["counts"]["favorite_tracks"] == 2
    assert manifest["counts"]["favorite_albums"] == 1


def test_backup_strips_credentials(tmp_path, store):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[credentials]\napp_id = "SECRET_ID"\napp_secret = "SECRET"\npool = ""\n'
    )

    output = tmp_path / "backup.tar.gz"
    backup_to_tar(store=store, config_path=config_path,
                  playlists_dir=tmp_path / "pl", output_path=output)

    with tarfile.open(output, "r:gz") as tar:
        for member in tar.getmembers():
            if "config.toml" in member.name:
                f = tar.extractfile(member)
                content = f.read().decode()
                break

    assert "SECRET_ID" not in content
    assert "SECRET" not in content


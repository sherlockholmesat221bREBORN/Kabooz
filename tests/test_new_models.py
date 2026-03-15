# tests/test_new_models.py
"""
Tests for models and client methods added after the initial release:
Release/ReleasesList, UserFavorites, UserFavoriteIds, LabelDetail,
PoolModeError guard, and the new catalog endpoints.
"""
import pytest
import respx
import httpx

from kabooz import QobuzClient
from kabooz.exceptions import PoolModeError
from kabooz.models.release import Release, ReleasesList
from kabooz.models.favorites import UserFavorites, UserFavoriteIds, LabelDetail

BASE = "https://www.qobuz.com/api.json/0.2"


def authed() -> QobuzClient:
    c = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    c.login(token="TOK", user_id="1")
    return c


# ── Release model ──────────────────────────────────────────────────────────

RELEASE_PAYLOAD = {
    "id": "abc123",
    "title": "Discovery",
    "version": "Remastered",
    "release_type": "album",
    "parental_warning": False,
    "artist": {"name": {"display": "Daft Punk"}},
    "dates": {
        "original": "2001-03-07",
        "stream":   "2014-01-01",
        "download": "2014-01-01",
    },
    "duration": 3540,
    "tracks_count": 14,
    "rights": {
        "streamable": True,
        "downloadable": True,
        "purchasable": False,
        "hires_streamable": True,
        "hires_purchasable": False,
    },
    "image": {
        "large": "https://example.com/large.jpg",
        "small": "https://example.com/small.jpg",
        "thumbnail": "https://example.com/thumb.jpg",
    },
}

def test_release_parses_correctly():
    r = Release.from_dict(RELEASE_PAYLOAD)
    assert r.id == "abc123"
    assert r.title == "Discovery"
    assert r.version == "Remastered"
    assert r.release_type == "album"

def test_release_display_title():
    r = Release.from_dict(RELEASE_PAYLOAD)
    assert r.display_title == "Discovery (Remastered)"

def test_release_display_title_no_version():
    r = Release.from_dict({**RELEASE_PAYLOAD, "version": None})
    assert r.display_title == "Discovery"

def test_release_artist_display_name():
    r = Release.from_dict(RELEASE_PAYLOAD)
    assert r.artist.display_name == "Daft Punk"

def test_release_dates():
    r = Release.from_dict(RELEASE_PAYLOAD)
    assert r.dates.original == "2001-03-07"

def test_release_rights():
    r = Release.from_dict(RELEASE_PAYLOAD)
    assert r.rights.streamable is True
    assert r.rights.purchasable is False

def test_release_date_property():
    r = Release.from_dict(RELEASE_PAYLOAD)
    assert r.release_date == "2001-03-07"

def test_releases_list_from_dict():
    data = {
        "has_more": True,
        "items": [RELEASE_PAYLOAD, RELEASE_PAYLOAD],
    }
    rl = ReleasesList.from_dict(data)
    assert rl.has_more is True
    assert len(rl.items) == 2
    assert all(isinstance(r, Release) for r in rl.items)

def test_releases_list_empty():
    rl = ReleasesList.from_dict({"has_more": False, "items": []})
    assert rl.has_more is False
    assert rl.items == []


# ── UserFavorites model ────────────────────────────────────────────────────

FAV_PAYLOAD = {
    "tracks": {
        "offset": 0, "limit": 50, "total": 1,
        "items": [{"id": 12345, "title": "One More Time",
                   "duration": 320, "track_number": 1, "media_number": 1}],
    },
    "albums": {
        "offset": 0, "limit": 50, "total": 0, "items": [],
    },
    "artists": {
        "offset": 0, "limit": 50, "total": 0, "items": [],
    },
}

def test_user_favorites_parses_tracks():
    fav = UserFavorites.from_dict(FAV_PAYLOAD)
    assert fav.tracks is not None
    assert fav.tracks.total == 1
    assert len(fav.tracks.items) == 1

def test_user_favorites_empty_albums():
    fav = UserFavorites.from_dict(FAV_PAYLOAD)
    assert fav.albums is not None
    assert fav.albums.total == 0

def test_user_favorites_missing_section():
    fav = UserFavorites.from_dict({"tracks": FAV_PAYLOAD["tracks"]})
    assert fav.albums is None
    assert fav.artists is None


# ── UserFavoriteIds model ──────────────────────────────────────────────────

def test_user_favorite_ids_parses():
    data = {
        "tracks":  [12345, 67890],
        "albums":  ["abc123", "def456"],
        "artists": [999],
        "articles": [],
    }
    ids = UserFavoriteIds.from_dict(data)
    assert ids.tracks == [12345, 67890]
    assert ids.albums == ["abc123", "def456"]
    assert ids.artists == [999]

def test_user_favorite_ids_empty():
    ids = UserFavoriteIds.from_dict({})
    assert ids.tracks == []
    assert ids.albums == []


# ── LabelDetail model ─────────────────────────────────────────────────────

LABEL_PAYLOAD = {
    "id": 1,
    "name": "ECM Records",
    "slug": "ecm-records",
    "supplier_id": 5,
    "description": "Founded by Manfred Eicher.",
    "description_language": "en",
    "albums_count": 350,
    "albums": {
        "offset": 0, "limit": 25, "total": 350,
        "items": [{"id": "xyz", "title": "Köln Concert",
                   "tracks_count": 2, "duration": 3600}],
    },
}

def test_label_detail_parses():
    label = LabelDetail.from_dict(LABEL_PAYLOAD)
    assert label.id == 1
    assert label.name == "ECM Records"
    assert label.albums_count == 350

def test_label_detail_albums():
    label = LabelDetail.from_dict(LABEL_PAYLOAD)
    assert label.albums is not None
    assert label.albums.total == 350
    assert label.albums.items[0].title == "Köln Concert"

def test_label_detail_no_albums():
    label = LabelDetail.from_dict({k: v for k, v in LABEL_PAYLOAD.items() if k != "albums"})
    assert label.albums is None


# ── PoolModeError guard ────────────────────────────────────────────────────

def test_add_favorite_raises_in_pool_mode(tmp_path):
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("123\nabc\nTOKEN\n")
    client = QobuzClient.from_token_pool(pool_file, validate=False)
    with pytest.raises(PoolModeError):
        client.add_favorite(track_ids=["12345"])

def test_remove_favorite_raises_in_pool_mode(tmp_path):
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("123\nabc\nTOKEN\n")
    client = QobuzClient.from_token_pool(pool_file, validate=False)
    with pytest.raises(PoolModeError):
        client.remove_favorite(track_ids=["12345"])

def test_is_pool_mode_true_for_pool(tmp_path):
    pool_file = tmp_path / "pool.txt"
    pool_file.write_text("123\nabc\nTOKEN\n")
    client = QobuzClient.from_token_pool(pool_file, validate=False)
    assert client.is_pool_mode is True

def test_is_pool_mode_false_for_credentials():
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    assert client.is_pool_mode is False


# ── New catalog endpoints ──────────────────────────────────────────────────

@respx.mock
def test_get_label_returns_label_detail():
    respx.get(f"{BASE}/label/get").mock(
        return_value=httpx.Response(200, json=LABEL_PAYLOAD)
    )
    client = authed()
    label = client.get_label(1)
    assert isinstance(label, LabelDetail)
    assert label.name == "ECM Records"


@respx.mock
def test_get_release_list_returns_releases_list():
    respx.get(f"{BASE}/artist/getReleasesList").mock(
        return_value=httpx.Response(200, json={
            "has_more": False,
            "items": [RELEASE_PAYLOAD],
        })
    )
    client = authed()
    rl = client.get_release_list(999)
    assert isinstance(rl, ReleasesList)
    assert len(rl.items) == 1
    assert rl.items[0].title == "Discovery"


@respx.mock
def test_get_user_favorites_returns_user_favorites():
    respx.get(f"{BASE}/favorite/getUserFavorites").mock(
        return_value=httpx.Response(200, json=FAV_PAYLOAD)
    )
    client = authed()
    fav = client.get_user_favorites()
    assert isinstance(fav, UserFavorites)
    assert fav.tracks.total == 1


@respx.mock
def test_get_favorite_ids_returns_ids():
    respx.get(f"{BASE}/favorite/getUserFavoriteIds").mock(
        return_value=httpx.Response(200, json={
            "tracks": [12345],
            "albums": ["abc123"],
            "artists": [999],
        })
    )
    client = authed()
    ids = client.get_favorite_ids()
    assert isinstance(ids, UserFavoriteIds)
    assert 12345 in ids.tracks


@respx.mock
def test_search_tracks_endpoint():
    respx.get(f"{BASE}/track/search").mock(
        return_value=httpx.Response(200, json={"tracks": {"items": [], "total": 0}})
    )
    client = authed()
    result = client.search_tracks("beethoven")
    assert "tracks" in result


@respx.mock
def test_search_albums_endpoint():
    respx.get(f"{BASE}/album/search").mock(
        return_value=httpx.Response(200, json={"albums": {"items": [], "total": 0}})
    )
    client = authed()
    result = client.search_albums("discovery")
    assert "albums" in result


@respx.mock
def test_get_user_info_endpoint():
    respx.get(f"{BASE}/user/get").mock(
        return_value=httpx.Response(200, json={
            "id": 99999,
            "login": "testuser",
            "email": "test@example.com",
        })
    )
    client = authed()
    info = client.get_user_info()
    assert info["email"] == "test@example.com"


@respx.mock
def test_reset_password_no_auth_required():
    respx.get(f"{BASE}/user/resetPassword").mock(
        return_value=httpx.Response(200, json={"status": "success"})
    )
    # No login() call — should work without auth.
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    result = client.reset_password("test@example.com")
    assert result["status"] == "success"


@respx.mock
def test_iter_playlist_track_summaries_paginates():
    """iter_playlist_track_summaries should make multiple calls until
    it gets a page smaller than page_size."""
    page1 = {
        "id": 42, "name": "Big Playlist", "description": None,
        "tracks_count": 3, "duration": 0, "is_public": True,
        "is_collaborative": False, "users_count": 1,
        "created_at": 0, "updated_at": 0,
        "owner": {"id": 1, "name": "user"}, "genres": [], "images": [],
        "tracks": {
            "offset": 0, "limit": 2, "total": 3,
            "items": [
                {"id": 1, "title": "Track A", "duration": 100,
                 "track_number": 1, "media_number": 1, "position": 0,
                 "playlist_track_id": 1, "performer": {"id": 1, "name": "A"}},
                {"id": 2, "title": "Track B", "duration": 100,
                 "track_number": 2, "media_number": 1, "position": 1,
                 "playlist_track_id": 2, "performer": {"id": 1, "name": "A"}},
            ],
        },
    }
    page2 = {
        **page1,
        "tracks": {
            "offset": 2, "limit": 2, "total": 3,
            "items": [
                {"id": 3, "title": "Track C", "duration": 100,
                 "track_number": 3, "media_number": 1, "position": 2,
                 "playlist_track_id": 3, "performer": {"id": 1, "name": "A"}},
            ],
        },
    }

    call_count = 0
    def respond(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=page1 if call_count == 1 else page2)

    respx.get(f"{BASE}/playlist/get").mock(side_effect=respond)

    client = authed()
    tracks = list(client.iter_playlist_track_summaries(42, page_size=2))
    assert len(tracks) == 3
    assert call_count == 2
    assert tracks[0].title == "Track A"
    assert tracks[2].title == "Track C"


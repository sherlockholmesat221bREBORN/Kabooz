# tests/test_catalog.py
import pytest
import respx
import httpx
from kabooz import QobuzClient, Quality
from kabooz.exceptions import NoAuthError, NotStreamableError
from kabooz.models import Track, Album, Artist, Playlist

BASE = "https://www.qobuz.com/api.json/0.2"


def authenticated_client() -> QobuzClient:
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    client.login(token="TEST_TOKEN", user_id="99999")
    return client


# ── get_track ──────────────────────────────────────────────────────────────

@respx.mock
def test_get_track_returns_track_object():
    respx.get(f"{BASE}/track/get").mock(
        return_value=httpx.Response(200, json={
            "id": 12345,
            "title": "One More Time",
            "duration": 320,
            "track_number": 1,
            "media_number": 1,
        })
    )
    client = authenticated_client()
    track = client.get_track("12345")
    assert isinstance(track, Track)
    assert track.id == 12345
    assert track.title == "One More Time"


@respx.mock
def test_get_track_injects_auth_token():
    captured = {}

    def capture(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "id": 12345, "title": "Test",
            "duration": 100, "track_number": 1, "media_number": 1,
        })

    respx.get(f"{BASE}/track/get").mock(side_effect=capture)
    client = authenticated_client()
    client.get_track("12345")
    assert "TEST_TOKEN" in captured["url"]


@respx.mock
def test_get_track_not_found_raises():
    from kabooz.exceptions import NotFoundError
    respx.get(f"{BASE}/track/get").mock(
        return_value=httpx.Response(404, json={"message": "Track not found"})
    )
    client = authenticated_client()
    with pytest.raises(NotFoundError):
        client.get_track("99999999")


# ── get_album ──────────────────────────────────────────────────────────────

@respx.mock
def test_get_album_returns_album_object():
    respx.get(f"{BASE}/album/get").mock(
        return_value=httpx.Response(200, json={
            "id": "abc123",
            "title": "Discovery",
            "tracks_count": 14,
            "duration": 3540,
        })
    )
    client = authenticated_client()
    album = client.get_album("abc123")
    assert isinstance(album, Album)
    assert album.id == "abc123"
    assert album.title == "Discovery"


@respx.mock
def test_get_album_id_is_string():
    respx.get(f"{BASE}/album/get").mock(
        return_value=httpx.Response(200, json={
            "id": 999,
            "title": "Test",
            "tracks_count": 1,
            "duration": 100,
        })
    )
    client = authenticated_client()
    album = client.get_album("999")
    assert isinstance(album.id, str)


# ── search ─────────────────────────────────────────────────────────────────

@respx.mock
def test_search_returns_results():
    respx.get(f"{BASE}/catalog/search").mock(
        return_value=httpx.Response(200, json={
            "tracks": {"items": [{"id": 1, "title": "Test Track"}]},
            "query": "daft punk",
        })
    )
    client = authenticated_client()
    results = client.search("daft punk", type="tracks")
    assert results["query"] == "daft punk"


# ── get_track_url ──────────────────────────────────────────────────────────

@respx.mock
def test_get_track_url_returns_url():
    respx.get(f"{BASE}/track/getFileUrl").mock(
        return_value=httpx.Response(200, json={
            "url": "https://cdn.qobuz.com/some/signed/url.flac",
            "format_id": 27,
            "bit_depth": 24,
            "sampling_rate": 96.0,
            "mime_type": "audio/flac",
        })
    )
    client = authenticated_client()
    result = client.get_track_url("12345", quality=Quality.HI_RES)
    assert "url" in result
    assert result["bit_depth"] == 24


@respx.mock
def test_get_track_url_includes_signature():
    captured = {}

    def capture(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"url": "https://cdn.qobuz.com/file.flac"})

    respx.get(f"{BASE}/track/getFileUrl").mock(side_effect=capture)
    client = authenticated_client()
    client.get_track_url("12345")
    assert "request_ts" in captured["url"]
    assert "request_sig" in captured["url"]


@respx.mock
def test_get_track_url_not_streamable_raises():
    respx.get(f"{BASE}/track/getFileUrl").mock(
        return_value=httpx.Response(200, json={"message": "Restricted by label"})
    )
    client = authenticated_client()
    with pytest.raises(NotStreamableError):
        client.get_track_url("12345")


def test_get_track_url_without_auth_raises():
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    with pytest.raises(NoAuthError):
        client.get_track_url("12345")


@respx.mock
def test_rate_limit_raises():
    from kabooz.exceptions import RateLimitError
    respx.get(f"{BASE}/track/get").mock(
        return_value=httpx.Response(429, json={"message": "Too many requests"})
    )
    client = authenticated_client()
    with pytest.raises(RateLimitError):
        client.get_track("12345")


# ── get_artist ─────────────────────────────────────────────────────────────

@respx.mock
def test_get_artist_returns_artist_object():
    respx.get(f"{BASE}/artist/get").mock(
        return_value=httpx.Response(200, json={
            "id": 999,
            "name": "Daft Punk",
            "slug": "daft-punk",
            "image": None,
            "picture": None,
            "albums_count": 8,
            "albums_as_primary_artist_count": 5,
            "albums_as_primary_composer_count": 1,
            "similar_artist_ids": [],
        })
    )
    client = authenticated_client()
    artist = client.get_artist(999)
    assert isinstance(artist, Artist)
    assert artist.id == 999
    assert artist.name == "Daft Punk"


@respx.mock
def test_get_artist_with_albums():
    respx.get(f"{BASE}/artist/get").mock(
        return_value=httpx.Response(200, json={
            "id": 999,
            "name": "Daft Punk",
            "slug": "daft-punk",
            "image": None,
            "picture": None,
            "albums_count": 1,
            "albums_as_primary_artist_count": 1,
            "albums_as_primary_composer_count": 0,
            "similar_artist_ids": [],
            "albums": {
                "offset": 0,
                "limit": 25,
                "total": 1,
                "items": [{
                    "id": "abc123",
                    "title": "Discovery",
                    "duration": 3540,
                    "release_date_original": "2001-03-07",
                    "maximum_bit_depth": 16,
                    "maximum_sampling_rate": 44.1,
                    "tracks_count": 14,
                    "media_count": 1,
                    "artist": {"id": 999, "name": "Daft Punk", "slug": "daft-punk"},
                    "label": {"id": 1, "name": "Virgin", "slug": "virgin", "supplier_id": 5},
                    "genre": {"id": 10, "name": "Electronic", "slug": "electronic", "path": []},
                    "image": {"large": "https://example.com/large.jpg", "small": "https://example.com/small.jpg", "thumbnail": "https://example.com/thumb.jpg", "back": None},
                }],
            },
        })
    )
    client = authenticated_client()
    artist = client.get_artist(999, extras="albums")
    assert artist.albums is not None
    assert artist.albums.total == 1
    assert isinstance(artist.albums.items[0], Album)
    assert artist.albums.items[0].title == "Discovery"


@respx.mock
def test_get_artist_injects_auth_token():
    captured = {}

    def capture(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "id": 999, "name": "Daft Punk", "slug": "daft-punk",
            "image": None, "picture": None,
            "albums_count": 0, "albums_as_primary_artist_count": 0,
            "albums_as_primary_composer_count": 0, "similar_artist_ids": [],
        })

    respx.get(f"{BASE}/artist/get").mock(side_effect=capture)
    client = authenticated_client()
    client.get_artist(999)
    assert "TEST_TOKEN" in captured["url"]


@respx.mock
def test_get_artist_not_found_raises():
    from kabooz.exceptions import NotFoundError
    respx.get(f"{BASE}/artist/get").mock(
        return_value=httpx.Response(404, json={"message": "Artist not found"})
    )
    client = authenticated_client()
    with pytest.raises(NotFoundError):
        client.get_artist(0)


# ── get_playlist ───────────────────────────────────────────────────────────

@respx.mock
def test_get_playlist_returns_playlist_object():
    respx.get(f"{BASE}/playlist/get").mock(
        return_value=httpx.Response(200, json={
            "id": 42,
            "name": "My Favourites",
            "description": "A curated mix",
            "tracks_count": 0,
            "duration": 0,
            "is_public": True,
            "is_collaborative": False,
            "users_count": 1,
            "created_at": 1700000000,
            "updated_at": 1700001000,
            "owner": {"id": 7, "name": "maxxx"},
            "genres": [],
            "images": [],
        })
    )
    client = authenticated_client()
    pl = client.get_playlist(42)
    assert isinstance(pl, Playlist)
    assert pl.id == 42
    assert pl.name == "My Favourites"


@respx.mock
def test_get_playlist_with_tracks():
    respx.get(f"{BASE}/playlist/get").mock(
        return_value=httpx.Response(200, json={
            "id": 42,
            "name": "My Favourites",
            "description": None,
            "tracks_count": 1,
            "duration": 320,
            "is_public": True,
            "is_collaborative": False,
            "users_count": 1,
            "created_at": 1700000000,
            "updated_at": 1700001000,
            "owner": {"id": 7, "name": "maxxx"},
            "genres": [],
            "images": [],
            "tracks": {
                "offset": 0,
                "limit": 50,
                "total": 1,
                "items": [{
                    "id": 12345,
                    "title": "One More Time",
                    "duration": 320,
                    "track_number": 1,
                    "media_number": 1,
                    "position": 0,
                    "playlist_track_id": 9001,
                    "performers": "Daft Punk, performer",
                    "purchasable": True,
                    "streamable": True,
                    "downloadable": True,
                    "previewable": True,
                    "sampleable": True,
                    "displayable": True,
                    "hires": False,
                    "performer": {"id": 999, "name": "Daft Punk"},
                }],
            },
        })
    )
    client = authenticated_client()
    pl = client.get_playlist(42)
    assert pl.tracks is not None
    assert pl.tracks.total == 1
    assert pl.tracks.items[0].title == "One More Time"
    assert pl.tracks.items[0].playlist_track_id == 9001


@respx.mock
def test_get_playlist_not_found_raises():
    from kabooz.exceptions import NotFoundError
    respx.get(f"{BASE}/playlist/get").mock(
        return_value=httpx.Response(404, json={"message": "Playlist not found"})
    )
    client = authenticated_client()
    with pytest.raises(NotFoundError):
        client.get_playlist(0)

import hashlib
import pytest
import respx
import httpx
from kabooz import QobuzClient, Quality
from kabooz.exceptions import NoAuthError, NotStreamableError
from kabooz.models import Track, Album

BASE = "https://www.qobuz.com/api.json/0.2"


def authenticated_client() -> QobuzClient:
    client = QobuzClient.from_credentials(app_id="123", app_secret="abc")
    client.login(token="TEST_TOKEN", user_id="99999")
    return client


# ── get_track ──────────────────────────────────────────────────────────────

@respx.mock
def test_get_track_returns_track_object():
    """get_track() should now return a Track, not a raw dict."""
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
    """get_album() should return an Album, not a raw dict."""
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
    """Even if the API returns a numeric id, album.id must be a string."""
    respx.get(f"{BASE}/album/get").mock(
        return_value=httpx.Response(200, json={
            "id": 999,           # numeric — should be coerced to str
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
    # search still returns raw dict — no model for search results yet
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

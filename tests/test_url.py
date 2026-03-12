# tests/test_url.py
import pytest
from kabooz.url import parse_url


# ── www.qobuz.com — full slug+id paths ────────────────────────────────────

def test_album_url():
    assert parse_url("https://www.qobuz.com/album/discovery/abc123") == ("album", "abc123")

def test_track_url():
    assert parse_url("https://www.qobuz.com/track/one-more-time/12345678") == ("track", "12345678")

def test_artist_url():
    assert parse_url("https://www.qobuz.com/artist/daft-punk/999") == ("artist", "999")

def test_playlist_url():
    assert parse_url("https://www.qobuz.com/playlist/my-favs/42") == ("playlist", "42")


# ── Locale prefix variants ─────────────────────────────────────────────────

def test_gb_en_locale_prefix():
    assert parse_url("https://www.qobuz.com/gb-en/album/discovery/abc123") == ("album", "abc123")

def test_us_en_locale_prefix():
    assert parse_url("https://www.qobuz.com/us-en/track/one-more-time/12345678") == ("track", "12345678")

def test_fr_fr_locale_prefix():
    assert parse_url("https://www.qobuz.com/fr-fr/artist/daft-punk/999") == ("artist", "999")


# ── open.qobuz.com and play.qobuz.com — short id-only paths ───────────────

def test_open_qobuz_album():
    assert parse_url("https://open.qobuz.com/album/abc123") == ("album", "abc123")

def test_open_qobuz_track():
    assert parse_url("https://open.qobuz.com/track/12345678") == ("track", "12345678")

def test_play_qobuz_playlist():
    assert parse_url("https://play.qobuz.com/playlist/42") == ("playlist", "42")


# ── Error cases ────────────────────────────────────────────────────────────

def test_non_qobuz_url_raises():
    with pytest.raises(ValueError, match="Not a Qobuz URL"):
        parse_url("https://www.spotify.com/album/abc123")

def test_unknown_entity_type_raises():
    with pytest.raises(ValueError, match="Unknown Qobuz entity type"):
        parse_url("https://www.qobuz.com/label/virgin/1")

def test_no_id_segment_raises():
    with pytest.raises(ValueError):
        parse_url("https://www.qobuz.com/album")

def test_empty_path_raises():
    with pytest.raises(ValueError):
        parse_url("https://www.qobuz.com/")

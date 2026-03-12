# tests/test_tagger.py
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3

from kabooz.models.track import Track
from kabooz.models.album import Album
from kabooz.download.tagger import Tagger, _parse_lrc_to_sylt
from kabooz.download.lyrics import LyricsResult, fetch_lyrics, LRCLibProvider


# ── Fixtures ───────────────────────────────────────────────────────────────

def make_track(**kwargs) -> Track:
    defaults = {
        "id": 12345,
        "title": "One More Time",
        "duration": 320,
        "track_number": 1,
        "media_number": 1,
        "isrc": "GBDCE0000001",
        "copyright": "2001 Virgin",
        "performer": {"id": 999, "name": "Daft Punk"},
        "composer": {"id": 888, "name": "Thomas Bangalter"},
    }
    defaults.update(kwargs)
    return Track.from_dict(defaults)


def make_album(**kwargs) -> Album:
    defaults = {
        "id": "abc123",
        "title": "Discovery",
        "tracks_count": 14,
        "duration": 3540,
        "media_count": 1,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 96.0,
        "release_date_original": "2001-03-07",
        "artist": {"id": 999, "name": "Daft Punk", "slug": "daft-punk"},
        "label": {"id": 1, "name": "Virgin", "slug": "virgin", "supplier_id": 5},
        "genre": {"id": 10, "name": "Electronic", "slug": "electronic", "path": []},
        "upc": "724384960224",
    }
    defaults.update(kwargs)
    return Album.from_dict(defaults)


def make_flac(path: Path) -> None:
    # Minimal valid FLAC: "fLaC" marker + STREAMINFO metadata block
    path.write_bytes(bytes([
        0x66, 0x4C, 0x61, 0x43,  # fLaC
        0x80, 0x00, 0x00, 0x22,  # last block, STREAMINFO, length=34
        0x00, 0x12, 0x00, 0x12,  # min/max blocksize
        0x00, 0x00, 0x00,        # min framesize
        0x00, 0x00, 0x00,        # max framesize
        0x0A, 0xC4, 0x42, 0xF0, 0x00, 0x00, 0x00, 0x00,  # 44100Hz/2ch/16bit
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # MD5 part 1
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  # MD5 part 2
    ]))


def make_mp3(path: Path) -> None:
    """
    Create a minimal valid MP3 file at path for testing.
    We use a tiny silent MP3 frame (valid header, silent audio).
    """
    # Minimal valid MP3 frame: MPEG1, Layer3, 128kbps, 44100Hz, stereo
    silent_frame = bytes([
        0xFF, 0xFB, 0x90, 0x00,  # MP3 header
    ] + [0x00] * 413)            # silent frame data
    path.write_bytes(silent_frame)


# ── LRC parser tests ───────────────────────────────────────────────────────

def test_parse_lrc_basic():
    lrc = "[00:13.45] One more time\n[00:17.90] We're gonna celebrate"
    result = _parse_lrc_to_sylt(lrc)
    assert len(result) == 2
    assert result[0] == ("One more time", 13450)
    assert result[1] == ("We're gonna celebrate", 17900)


def test_parse_lrc_three_digit_ms():
    lrc = "[01:02.500] Line with three digit ms"
    result = _parse_lrc_to_sylt(lrc)
    assert result[0][1] == 62500


def test_parse_lrc_ignores_invalid_lines():
    lrc = "not a timestamp\n[00:05.00] Valid line\n[invalid"
    result = _parse_lrc_to_sylt(lrc)
    assert len(result) == 1
    assert result[0][0] == "Valid line"


def test_parse_lrc_empty_string():
    assert _parse_lrc_to_sylt("") == []


# ── LyricsResult tests ─────────────────────────────────────────────────────

def test_lyrics_result_not_found():
    result = LyricsResult()
    assert result.found is False
    assert result.synced is None
    assert result.plain is None


def test_lyrics_result_with_content():
    result = LyricsResult(synced="[00:01.00] Hello", plain="Hello", found=True)
    assert result.found is True
    assert result.synced is not None


# ── LRCLibProvider tests ───────────────────────────────────────────────────

def test_lrclib_returns_not_found_on_404():
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.status = 404

    with patch("urllib.request.urlopen", return_value=mock_response):
        provider = LRCLibProvider()
        result = provider.fetch("Unknown Song", "Unknown Artist")
    assert result.found is False


def test_lrclib_returns_lyrics_on_success():
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.status = 200
    mock_response.read.return_value = b'{"syncedLyrics": "[00:01.00] Hello", "plainLyrics": "Hello"}'

    with patch("urllib.request.urlopen", return_value=mock_response):
        provider = LRCLibProvider()
        result = provider.fetch("One More Time", "Daft Punk", "Discovery", 320)

    assert result.found is True
    assert result.synced == "[00:01.00] Hello"
    assert result.plain == "Hello"


def test_lrclib_falls_back_to_plain_when_no_synced():
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.status = 200
    mock_response.read.return_value = b'{"syncedLyrics": null, "plainLyrics": "Hello"}'

    with patch("urllib.request.urlopen", return_value=mock_response):
        provider = LRCLibProvider(fallback_to_plain=True)
        result = provider.fetch("One More Time", "Daft Punk")

    assert result.found is True
    assert result.synced is None
    assert result.plain == "Hello"


def test_lrclib_no_fallback_returns_not_found():
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.status = 200
    mock_response.read.return_value = b'{"syncedLyrics": null, "plainLyrics": "Hello"}'

    with patch("urllib.request.urlopen", return_value=mock_response):
        provider = LRCLibProvider(fallback_to_plain=False)
        result = provider.fetch("One More Time", "Daft Punk")

    assert result.found is False


def test_lrclib_network_error_returns_not_found():
    with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
        provider = LRCLibProvider()
        result = provider.fetch("One More Time", "Daft Punk")
    assert result.found is False


# ── FLAC tagger tests ──────────────────────────────────────────────────────

def test_flac_basic_tags(tmp_path):
    path = tmp_path / "track.flac"
    make_flac(path)

    tagger = Tagger()
    tagger.tag(path, make_track(), make_album(), embed_cover=False)

    audio = FLAC(path)
    assert audio["TITLE"] == ["One More Time"]
    assert audio["ARTIST"] == ["Daft Punk"]
    assert audio["ALBUM"] == ["Discovery"]
    assert audio["TRACKNUMBER"] == ["1"]
    assert audio["ISRC"] == ["GBDCE0000001"]


def test_flac_album_tags(tmp_path):
    path = tmp_path / "track.flac"
    make_flac(path)

    tagger = Tagger()
    tagger.tag(path, make_track(), make_album(), embed_cover=False)

    audio = FLAC(path)
    assert audio["ALBUMARTIST"] == ["Daft Punk"]
    assert audio["ORGANIZATION"] == ["Virgin"]
    assert audio["GENRE"] == ["Electronic"]
    assert audio["DATE"] == ["2001"]


def test_flac_plain_lyrics(tmp_path):
    path = tmp_path / "track.flac"
    make_flac(path)

    lyrics = LyricsResult(plain="One more time", found=True)
    tagger = Tagger()
    tagger.tag(path, make_track(), embed_cover=False, lyrics=lyrics)

    audio = FLAC(path)
    assert audio["LYRICS"] == ["One more time"]


def test_flac_synced_lyrics(tmp_path):
    path = tmp_path / "track.flac"
    make_flac(path)

    lyrics = LyricsResult(synced="[00:01.00] One more time", found=True)
    tagger = Tagger()
    tagger.tag(path, make_track(), embed_cover=False, lyrics=lyrics)

    audio = FLAC(path)
    assert audio["SYNCEDLYRICS"] == ["[00:01.00] One more time"]


def test_flac_cover_art(tmp_path):
    path = tmp_path / "track.flac"
    make_flac(path)

    fake_cover = b"\xff\xd8\xff" + b"\x00" * 100  # minimal JPEG-like bytes

    tagger = Tagger()
    with patch.object(tagger, "_fetch_cover", return_value=fake_cover):
        tagger.tag(path, make_track(), make_album(), embed_cover=True)

    audio = FLAC(path)
    assert len(audio.pictures) == 1
    assert audio.pictures[0].data == fake_cover


def test_flac_no_cover_when_embed_false(tmp_path):
    path = tmp_path / "track.flac"
    make_flac(path)

    tagger = Tagger()
    tagger.tag(path, make_track(), make_album(), embed_cover=False)

    audio = FLAC(path)
    assert len(audio.pictures) == 0


def test_flac_clears_existing_tags(tmp_path):
    """Tagging twice should not accumulate duplicate tags."""
    path = tmp_path / "track.flac"
    make_flac(path)

    tagger = Tagger()
    tagger.tag(path, make_track(), make_album(), embed_cover=False)
    tagger.tag(path, make_track(title="Different Title"), make_album(), embed_cover=False)

    audio = FLAC(path)
    assert audio["TITLE"] == ["Different Title"]
    assert len(audio["TITLE"]) == 1


# ── MP3 tagger tests ───────────────────────────────────────────────────────

def test_mp3_basic_tags(tmp_path):
    path = tmp_path / "track.mp3"
    make_mp3(path)

    tagger = Tagger()
    tagger.tag(path, make_track(), make_album(), embed_cover=False)

    tags = ID3(path)
    assert str(tags["TIT2"]) == "One More Time"
    assert str(tags["TPE1"]) == "Daft Punk"
    assert str(tags["TALB"]) == "Discovery"
    assert str(tags["TSRC"]) == "GBDCE0000001"


def test_mp3_album_tags(tmp_path):
    path = tmp_path / "track.mp3"
    make_mp3(path)

    tagger = Tagger()
    tagger.tag(path, make_track(), make_album(), embed_cover=False)

    tags = ID3(path)
    assert str(tags["TPE2"]) == "Daft Punk"
    assert str(tags["TPUB"]) == "Virgin"
    assert str(tags["TCON"]) == "Electronic"
    assert str(tags["TDRC"]) == "2001"


def test_mp3_cover_art(tmp_path):
    path = tmp_path / "track.mp3"
    make_mp3(path)

    fake_cover = b"\xff\xd8\xff" + b"\x00" * 100

    tagger = Tagger()
    with patch.object(tagger, "_fetch_cover", return_value=fake_cover):
        tagger.tag(path, make_track(), make_album(), embed_cover=True)

    tags = ID3(path)
    assert "APIC:Cover" in tags
    assert tags["APIC:Cover"].data == fake_cover


def test_unsupported_format_raises(tmp_path):
    path = tmp_path / "track.ogg"
    path.write_bytes(b"fake")

    tagger = Tagger()
    with pytest.raises(ValueError, match="Unsupported audio format"):
        tagger.tag(path, make_track(), embed_cover=False)


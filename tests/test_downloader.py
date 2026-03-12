# tests/test_downloader.py
import io
import pytest
import respx
import httpx
from pathlib import Path

from kabooz.download.downloader import Downloader, DownloadResult
from kabooz.download.naming import (
    sanitize, quality_tag, track_filename,
    album_track_filename, album_folder, resolve_track_path,
)
from kabooz.models.track import Track
from kabooz.models.album import Album


# ── Fixtures ───────────────────────────────────────────────────────────────

def make_track(**kwargs) -> Track:
    defaults = {
        "id": 12345,
        "title": "One More Time",
        "duration": 320,
        "track_number": 1,
        "media_number": 1,
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
    }
    defaults.update(kwargs)
    return Album.from_dict(defaults)


# ── Sanitization tests ─────────────────────────────────────────────────────

def test_sanitize_replaces_colon():
    assert "∶" in sanitize("AC/DC: Live")

def test_sanitize_replaces_slash():
    assert "∕" in sanitize("AC/DC")

def test_sanitize_strips_control_chars():
    assert sanitize("hello\x00world") == "helloworld"

def test_sanitize_clean_string_unchanged():
    assert sanitize("One More Time") == "One More Time"


# ── Quality tag tests ──────────────────────────────────────────────────────

def test_quality_tag_full():
    assert quality_tag(24, 96.0) == "FLAC 24bit 96kHz"

def test_quality_tag_whole_number_rate():
    assert quality_tag(16, 44.1) == "FLAC 16bit 44.1kHz"

def test_quality_tag_no_sampling_rate():
    assert quality_tag(24, None) == "FLAC 24bit"

def test_quality_tag_nothing():
    assert quality_tag(None, None) == "FLAC"


# ── Filename tests ─────────────────────────────────────────────────────────

def test_track_filename_single():
    track = make_track(title="One More Time")
    assert track_filename(track, ".flac") == "One More Time.flac"

def test_album_track_filename_pads_number():
    track = make_track(title="One More Time", track_number=1)
    assert album_track_filename(track, ".flac") == "01. One More Time.flac"

def test_album_track_filename_double_digit():
    track = make_track(title="Harder Better Faster Stronger", track_number=10)
    assert album_track_filename(track, ".flac") == "10. Harder Better Faster Stronger.flac"

def test_album_folder_format():
    album = make_album(title="Discovery", release_date_original="2001-03-07")
    folder = album_folder(album, bit_depth=24, sampling_rate=96.0)
    assert folder == "Discovery [FLAC 24bit 96kHz] [2001]"

def test_album_folder_no_date():
    album = make_album(title="Discovery", release_date_original=None)
    folder = album_folder(album, bit_depth=24, sampling_rate=96.0)
    assert folder == "Discovery [FLAC 24bit 96kHz]"


# ── resolve_track_path tests ───────────────────────────────────────────────

def test_resolve_single_track(tmp_path):
    track = make_track(title="One More Time")
    path = resolve_track_path(track, tmp_path, ".flac")
    assert path == tmp_path / "One More Time.flac"


def test_resolve_album_track(tmp_path):
    track = make_track(title="One More Time", track_number=1)
    album = make_album(title="Discovery", release_date_original="2001-03-07")
    path = resolve_track_path(
        track, tmp_path, ".flac",
        album=album, bit_depth=24, sampling_rate=96.0,
    )
    expected = (
        tmp_path
        / "Discovery [FLAC 24bit 96kHz] [2001]"
        / "01. One More Time.flac"
    )
    assert path == expected


def test_resolve_multi_disc_track(tmp_path):
    track = make_track(title="One More Time", track_number=1, media_number=2)
    album = make_album(
        title="Discovery",
        release_date_original="2001-03-07",
        media_count=2,
    )
    path = resolve_track_path(
        track, tmp_path, ".flac",
        album=album, is_multi_disc=True,
        bit_depth=24, sampling_rate=96.0,
    )
    expected = (
        tmp_path
        / "Discovery [FLAC 24bit 96kHz] [2001]"
        / "Disc 2"
        / "01. One More Time.flac"
    )
    assert path == expected


def test_resolve_custom_template(tmp_path):
    track = make_track(title="One More Time", isrc="GBDCE0000001")
    path = resolve_track_path(
        track, tmp_path, ".flac",
        filename_template=lambda t: f"{t.isrc}.flac",
    )
    assert path == tmp_path / "GBDCE0000001.flac"


def test_resolve_creates_directories(tmp_path):
    track = make_track(title="One More Time", track_number=1)
    album = make_album(title="Discovery", release_date_original="2001-03-07")
    path = resolve_track_path(
        track, tmp_path, ".flac",
        album=album, bit_depth=24, sampling_rate=96.0,
    )
    assert path.parent.exists()


# ── Downloader tests ───────────────────────────────────────────────────────

FAKE_URL = "https://cdn.qobuz.com/track.flac"
FAKE_CONTENT = b"FLAC" + b"\x00" * 100   # 104 bytes of fake audio data


@respx.mock
def test_download_to_file_writes_bytes():
    respx.get(FAKE_URL).mock(
        return_value=httpx.Response(
            200,
            content=FAKE_CONTENT,
            headers={"content-length": str(len(FAKE_CONTENT))},
        )
    )
    buf = io.BytesIO()
    downloader = Downloader()
    result = downloader.download_to_file(FAKE_URL, buf)
    assert buf.getvalue() == FAKE_CONTENT
    assert result.bytes_written == len(FAKE_CONTENT)


@respx.mock
def test_download_fresh(tmp_path):
    dest = tmp_path / "track.flac"
    respx.head(FAKE_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-length": str(len(FAKE_CONTENT))},
        )
    )
    respx.get(FAKE_URL).mock(
        return_value=httpx.Response(
            200,
            content=FAKE_CONTENT,
            headers={"content-length": str(len(FAKE_CONTENT))},
        )
    )
    downloader = Downloader()
    result = downloader._download_to_path(FAKE_URL, dest, None)
    assert dest.read_bytes() == FAKE_CONTENT
    assert result.skipped is False
    assert result.resumed is False
    assert result.bytes_written == len(FAKE_CONTENT)


@respx.mock
def test_download_skips_existing_complete_file(tmp_path):
    dest = tmp_path / "track.flac"
    dest.write_bytes(FAKE_CONTENT)   # file already complete
    respx.head(FAKE_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-length": str(len(FAKE_CONTENT))},
        )
    )
    downloader = Downloader()
    result = downloader._download_to_path(FAKE_URL, dest, None)
    assert result.skipped is True
    assert result.bytes_written == 0


@respx.mock
def test_download_resumes_partial_file(tmp_path):
    partial = FAKE_CONTENT[:50]
    remaining = FAKE_CONTENT[50:]
    dest = tmp_path / "track.flac"
    dest.write_bytes(partial)

    respx.head(FAKE_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-length": str(len(FAKE_CONTENT))},
        )
    )
    respx.get(FAKE_URL).mock(
        return_value=httpx.Response(
            206,
            content=remaining,
            headers={"content-length": str(len(remaining))},
        )
    )
    downloader = Downloader()
    result = downloader._download_to_path(FAKE_URL, dest, None)
    assert result.resumed is True
    assert result.bytes_written == len(remaining)
    assert dest.read_bytes() == FAKE_CONTENT


@respx.mock
def test_progress_callback_is_called(tmp_path):
    dest = tmp_path / "track.flac"
    respx.head(FAKE_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-length": str(len(FAKE_CONTENT))},
        )
    )
    respx.get(FAKE_URL).mock(
        return_value=httpx.Response(
            200,
            content=FAKE_CONTENT,
            headers={"content-length": str(len(FAKE_CONTENT))},
        )
    )
    calls = []
    downloader = Downloader()
    downloader._download_to_path(FAKE_URL, dest, on_progress=lambda d, t: calls.append((d, t)))
    assert len(calls) > 0
    # Last call should report full completion
    assert calls[-1][0] == calls[-1][1]


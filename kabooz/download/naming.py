# kabooz/download/naming.py
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from ..models.track import Track
from ..models.album import Album
from ..quality import Quality


# ── Character sanitization ─────────────────────────────────────────────────

# Maps illegal or problematic filename characters to safe Unicode lookalikes.
# These are visually similar to the originals but are valid in filenames on
# Windows, macOS, and Linux.
_LOOKALIKE_MAP = {
    "/":  "∕",   # DIVISION SLASH
    "\\":  "⧵",  # REVERSE SOLIDUS OPERATOR
    ":":  "∶",   # RATIO
    "*":  "∗",   # ASTERISK OPERATOR
    "?":  "？",  # FULLWIDTH QUESTION MARK
    '"':  "″",   # DOUBLE PRIME
    "<":  "﹤",  # SMALL LESS-THAN SIGN
    ">":  "﹥",  # SMALL GREATER-THAN SIGN
    "|":  "｜",  # FULLWIDTH VERTICAL LINE
}

# Characters that are outright removed rather than replaced.
_STRIP_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize(name: str) -> str:
    """
    Make a string safe to use as a filename component.
    Replaces illegal characters with safe Unicode lookalikes and strips
    ASCII control characters. Does not affect path separators that are
    intentionally part of a path — only call this on individual components
    (track title, album title, artist name), never on a full path.
    """
    for char, replacement in _LOOKALIKE_MAP.items():
        name = name.replace(char, replacement)
    name = _STRIP_CHARS.sub("", name)
    return name.strip()


# ── Quality label ──────────────────────────────────────────────────────────

def quality_tag(bit_depth: Optional[int], sampling_rate: Optional[float]) -> str:
    """
    Build the quality tag that appears in the album folder name, e.g.
    "[FLAC 24bit 96kHz]". Falls back gracefully if info is missing.
    """
    if bit_depth and sampling_rate:
        # sampling_rate comes from the API as a float (e.g. 96.0, 44.1).
        # Format as int if it's a whole number, otherwise keep the decimal.
        rate = int(sampling_rate) if sampling_rate == int(sampling_rate) else sampling_rate
        return f"[FLAC {bit_depth}bit {rate}kHz]"
    if bit_depth:
        return f"[FLAC {bit_depth}bit]"
    return "[FLAC]"


# ── Path builders ──────────────────────────────────────────────────────────

def track_filename(track: Track, extension: str) -> str:
    """
    Filename for a single track downloaded outside of an album context.

        One More Time.flac
    """
    title = sanitize(track.title)
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{title}{ext}"


def album_track_filename(track: Track, extension: str) -> str:
    """
    Filename for a track downloaded as part of an album.
    Zero-pads the track number to two digits.

        01. One More Time.flac
    """
    number = str(track.track_number).zfill(2)
    title = sanitize(track.title)
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{number}. {title}{ext}"


def album_folder(
    album: Album,
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
) -> str:
    """
    Folder name for an album download.

        Discovery [FLAC 24bit 96kHz] [2001]
    """
    title = sanitize(album.title)
    qtag = quality_tag(
        bit_depth or album.maximum_bit_depth,
        sampling_rate or album.maximum_sampling_rate,
    )
    year = ""
    if album.release_date_original:
        # release_date_original is "YYYY-MM-DD" — we only want the year.
        year = f" [{album.release_date_original[:4]}]"
    return f"{title} {qtag}{year}"


def disc_folder(disc_number: int) -> str:
    """
    Subfolder name for a single disc within a multi-disc album.

        Disc 1
    """
    return f"Disc {disc_number}"


def artist_folder(artist_name: str) -> str:
    """Folder name for an artist, used only in discography downloads."""
    return sanitize(artist_name)


def resolve_track_path(
    track: Track,
    dest_dir: Path,
    extension: str,
    album: Optional[Album] = None,
    is_multi_disc: bool = False,
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
    filename_template: Optional[Callable[[Track], str]] = None,
) -> Path:
    """
    Resolve the full destination path for a track file.

    This is the central path-building function. It handles all four
    download contexts:

    1. Single track, no album context:
           dest_dir/One More Time.flac

    2. Track with album context, single disc:
           dest_dir/Discovery [FLAC 24bit 96kHz] [2001]/01. One More Time.flac

    3. Track with album context, multi-disc:
           dest_dir/Discovery [FLAC 24bit 96kHz] [2001]/Disc 1/01. One More Time.flac

    4. Custom template (overrides everything above):
           dest_dir/<whatever template returns>

    Parameters:
        track:             The Track object being downloaded.
        dest_dir:          The root directory to place the file under.
        extension:         File extension, e.g. ".flac" or ".mp3".
        album:             The Album object, if downloading in album context.
        is_multi_disc:     If True and album is provided, adds a Disc N subfolder.
        bit_depth:         Actual bit depth from the URL response (overrides album value).
        sampling_rate:     Actual sampling rate from the URL response.
        filename_template: Optional callable(Track) -> str that overrides the
                           default filename logic entirely. The returned string
                           is used as the filename directly (not the full path).
                           The album folder is still created if album is provided.
    """
    parts: list[str] = []

    if album:
        parts.append(album_folder(album, bit_depth, sampling_rate))
        if is_multi_disc and track.media_number:
            parts.append(disc_folder(track.media_number))

    if filename_template is not None:
        filename = filename_template(track)
    elif album:
        filename = album_track_filename(track, extension)
    else:
        filename = track_filename(track, extension)

    parts.append(filename)

    path = dest_dir.joinpath(*parts)

    # Create all intermediate directories. exist_ok=True means this is
    # safe to call even if the directory already exists.
    path.parent.mkdir(parents=True, exist_ok=True)

    return path


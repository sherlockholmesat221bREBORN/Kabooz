# kabooz/download/naming.py
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..models.track import Track
from ..models.album import Album


# ── Character sanitization ─────────────────────────────────────────────────

_LOOKALIKE_MAP = {
    "/":  "∕",
    "\\":  "⧵",
    ":":  "∶",
    "*":  "∗",
    "?":  "？",
    '"':  "″",
    "<":  "﹤",
    ">":  "﹥",
    "|":  "｜",
}
_STRIP_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize(name: str) -> str:
    """
    Make a string safe for use as a single filename component.
    Replaces illegal characters with Unicode lookalikes, strips control
    characters. Never call on a full path — only on individual segments.
    """
    for char, replacement in _LOOKALIKE_MAP.items():
        name = name.replace(char, replacement)
    return _STRIP_CHARS.sub("", name).strip()


# ── Quality label ──────────────────────────────────────────────────────────

def quality_tag(bit_depth: Optional[int], sampling_rate: Optional[float]) -> str:
    """
    Build a human-readable quality string, e.g. "FLAC 24bit 96kHz".
    Note: no brackets — the template controls surrounding formatting.
    """
    if bit_depth and sampling_rate:
        rate = int(sampling_rate) if sampling_rate == int(sampling_rate) else sampling_rate
        return f"FLAC {bit_depth}bit {rate}kHz"
    if bit_depth:
        return f"FLAC {bit_depth}bit"
    return "FLAC"


# ── Template context ───────────────────────────────────────────────────────

class _SafeDict(dict):
    """
    A dict subclass used with str.format_map that returns the placeholder
    as-is when a key is missing, rather than raising a KeyError.
    This means a template with an unused placeholder (e.g. {playlist}
    in an album template) silently leaves it unreplaced rather than
    crashing — the caller can strip orphaned placeholders afterwards.
    """
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


def _build_context(
    track: Track,
    album: Optional[Album] = None,
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
    playlist_name: Optional[str] = None,
    playlist_index: Optional[int] = None,
) -> _SafeDict:
    """
    Build the substitution dict for a naming template.

    All string values are left raw here — sanitization is applied per
    path segment after rendering, not before, so that a template like
    "{albumartist}/{album}" can use "/" as a real path separator.

    Available placeholders
    ──────────────────────
    Always available:
        {title}          full display title: "{work} - {title} ({version})"
                         This is what you almost always want in filenames.
        {raw_title}      bare track title from the API, no work or version
        {work}           classical work name (empty string if absent)
        {version}        version string e.g. "2011 Remaster" (empty if absent)
        {artist}         primary track artist
        {track}          track number (int, supports format specs e.g. {track:02d})
        {disc}           disc number (int)
        {isrc}           ISRC code
        {bit_depth}      audio bit depth (int or empty string)
        {sampling_rate}  sampling rate (float or empty string)
        {quality}        e.g. "FLAC 24bit 96kHz"

    When album context is available:
        {album}          album display title (includes version if present)
        {raw_album}      bare album title from the API
        {albumartist}    album-level artist name
        {year}           4-digit release year
        {label}          record label
        {genre}          primary genre
        {upc}            UPC code

    For playlist downloads:
        {playlist}       playlist name
        {index}          track position in playlist (int)
    """
    bd = bit_depth or (album.maximum_bit_depth if album else None)
    sr = sampling_rate or (album.maximum_sampling_rate if album else None)

    artist = ""
    if track.performer:
        artist = track.performer.name
    elif album and album.artist:
        artist = album.artist.name

    albumartist  = (album.artist.name if album and album.artist else artist)
    album_title  = album.display_title if album else ""
    raw_album    = album.title.rstrip() if album else ""
    year         = ""
    if album and album.release_date_original:
        year = album.release_date_original[:4]
    label  = (album.label.name if album and album.label else "")
    genre  = (album.genre.name if album and album.genre else "")
    upc    = (album.upc        if album                 else "")

    return _SafeDict({
        # Title variants
        "title":         track.display_title,
        "raw_title":     track.title.rstrip(),
        "work":          track.work    or "",
        "version":       track.version or "",
        # Track metadata
        "artist":        artist,
        "track":         track.track_number or 0,
        "disc":          track.media_number or 1,
        "isrc":          track.isrc or "",
        # Quality
        "bit_depth":     bd or "",
        "sampling_rate": sr or "",
        "quality":       quality_tag(bd, sr),
        # Album metadata
        "album":         album_title,
        "raw_album":     raw_album,
        "albumartist":   albumartist,
        "year":          year,
        "label":         label,
        "genre":         genre,
        "upc":           upc,
        # Playlist
        "playlist":      playlist_name  or "",
        "index":         playlist_index or 0,
    })


# ── Template rendering ─────────────────────────────────────────────────────

# Matches leftover unreplaced placeholders like "{playlist}" after rendering.
_ORPHAN_RE = re.compile(r"\{[^}]+\}")


def render_template(
    template: str,
    track: Track,
    extension: str,
    album: Optional[Album] = None,
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
    playlist_name: Optional[str] = None,
    playlist_index: Optional[int] = None,
) -> Path:
    """
    Render a naming template into a relative Path (no leading slash, no
    output-dir prefix). The caller appends the result to dest_dir.

    Template syntax
    ───────────────
    Forward slashes in the template define subdirectory boundaries:

        "{albumartist}/{album} [{quality}] [{year}]/{track:02d}. {title}"

    Each segment is rendered independently and then sanitized, so data
    values that contain "/" are replaced with the lookalike "∕" rather
    than accidentally creating extra path components.

    Python format-spec mini-language is fully supported inside braces:

        {track:02d}   → "01", "02", …
        {index:03d}   → "001", "002", …
    """
    ctx = _build_context(
        track=track,
        album=album,
        bit_depth=bit_depth,
        sampling_rate=sampling_rate,
        playlist_name=playlist_name,
        playlist_index=playlist_index,
    )

    ext = extension if extension.startswith(".") else f".{extension}"

    raw_segments = template.split("/")
    rendered_segments: list[str] = []

    for i, segment in enumerate(raw_segments):
        try:
            rendered = segment.format_map(ctx)
        except (ValueError, KeyError):
            rendered = segment

        rendered = _ORPHAN_RE.sub("", rendered).strip()
        rendered = sanitize(rendered)

        if not rendered:
            continue

        if i == len(raw_segments) - 1:
            rendered = rendered + ext

        rendered_segments.append(rendered)

    if not rendered_segments:
        rendered_segments = [sanitize(track.display_title or "track") + ext]

    return Path(*rendered_segments)


# ── Legacy helpers (kept for backwards compatibility) ──────────────────────

def track_filename(track: Track, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{sanitize(track.display_title)}{ext}"


def album_track_filename(track: Track, extension: str) -> str:
    number = str(track.track_number).zfill(2)
    ext = extension if extension.startswith(".") else f".{extension}"
    return f"{number}. {sanitize(track.display_title)}{ext}"


def album_folder(
    album: Album,
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
) -> str:
    title = sanitize(album.display_title)
    qtag  = f"[{quality_tag(bit_depth or album.maximum_bit_depth, sampling_rate or album.maximum_sampling_rate)}]"
    year  = f" [{album.release_date_original[:4]}]" if album.release_date_original else ""
    return f"{title} {qtag}{year}"


def disc_folder(disc_number: int) -> str:
    return f"Disc {disc_number}"


def artist_folder(artist_name: str) -> str:
    return sanitize(artist_name)


def resolve_track_path(
    track: Track,
    dest_dir: Path,
    extension: str,
    album: Optional[Album] = None,
    is_multi_disc: bool = False,
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
    template: Optional[str] = None,
    playlist_name: Optional[str] = None,
    playlist_index: Optional[int] = None,
    # Legacy parameter — ignored if template is supplied.
    filename_template=None,
) -> Path:
    """
    Resolve the full destination path for a track file.

    When `template` is provided it is rendered via render_template and
    joined to dest_dir. Otherwise falls back to the legacy hardcoded
    folder/filename logic so existing call sites continue to work.
    """
    if template:
        rel = render_template(
            template=template,
            track=track,
            extension=extension,
            album=album,
            bit_depth=bit_depth,
            sampling_rate=sampling_rate,
            playlist_name=playlist_name,
            playlist_index=playlist_index,
        )
        path = dest_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ── Legacy path ────────────────────────────────────────────────────────
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
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

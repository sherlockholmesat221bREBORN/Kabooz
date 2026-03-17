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
    """Make a string safe for use as a single filename component."""
    for char, replacement in _LOOKALIKE_MAP.items():
        name = name.replace(char, replacement)
    return _STRIP_CHARS.sub("", name).strip()


# ── Quality label ──────────────────────────────────────────────────────────

def quality_tag(
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
    format_name: Optional[str] = None,
    bitrate_kbps: Optional[int] = None
) -> str:
    """Build a human-readable quality string."""
    if not format_name:
        if bitrate_kbps:
            format_name = "MP3"
        elif bit_depth or sampling_rate:
            format_name = "FLAC"
        else:
            format_name = "Unknown"

    parts = [format_name.upper()]

    if format_name.upper() == "FLAC":
        if bit_depth:
            parts.append(f"{bit_depth}bit")
        if sampling_rate:
            rate = int(sampling_rate) if sampling_rate == int(sampling_rate) else sampling_rate
            parts.append(f"{rate}kHz")
    else:  # MP3 / lossy
        if bitrate_kbps:
            parts.append(f"{bitrate_kbps}kbps")

    return " ".join(parts)


# ── Template context ───────────────────────────────────────────────────────

class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


def _build_context(
    track: Track,
    album: Optional[Album] = None,
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
    extension: Optional[str] = None,
    playlist_name: Optional[str] = None,
    playlist_index: Optional[int] = None,
    bitrate_kbps: Optional[int] = None,
) -> _SafeDict:
    # 1. Determine format first to decide if we should use High-Res metadata
    fmt = getattr(track, "codec", None)
    if not fmt and extension:
        ext = extension.lower().strip(".")
        fmt = "MP3" if ext == "mp3" else "FLAC"

    # 2. If it's MP3, we ignore the High-Res bit_depth/sampling_rate from the album
    use_fallback = (fmt != "MP3")
    bd = bit_depth or (album.maximum_bit_depth if album and use_fallback else None)
    sr = sampling_rate or (album.maximum_sampling_rate if album and use_fallback else None)
    final_bitrate = bitrate_kbps or getattr(track, "bitrate_kbps", None)

    artist = track.performer.name if track.performer else (album.artist.name if album and album.artist else "")
    albumartist = album.artist.name if album and album.artist else artist
    album_title = album.display_title if album else ""
    raw_album = album.title.rstrip() if album else ""
    year = album.release_date_original[:4] if album and album.release_date_original else ""
    label = album.label.name if album and album.label else ""
    genre = album.genre.name if album and album.genre else ""
    upc = album.upc if album else ""

    return _SafeDict({
        "title": track.display_title,
        "raw_title": track.title.rstrip(),
        "work": track.work or "",
        "version": track.version or "",
        "artist": artist,
        "track": track.track_number or 0,
        "disc": track.media_number or 1,
        "isrc": track.isrc or "",
        # Quality
        "bit_depth": bd or "",
        "sampling_rate": sr or "",
        "quality": quality_tag(bd, sr, format_name=fmt, bitrate_kbps=final_bitrate),
        # Album
        "album": album_title,
        "raw_album": raw_album,
        "albumartist": albumartist,
        "year": year,
        "label": label,
        "genre": genre,
        "upc": upc,
        # Playlist
        "playlist": playlist_name or "",
        "index": playlist_index or 0,
    })


# ── Template rendering ─────────────────────────────────────────────────────

_ORPHAN_RE = re.compile(r"\{[^}]+\}")


def render_template(
    template: str,
    track: Track,
    extension: str,
    album: Optional[Album] = None,
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
    bitrate_kbps: Optional[int] = None,
    playlist_name: Optional[str] = None,
    playlist_index: Optional[int] = None,
) -> Path:
    ctx = _build_context(
        track=track,
        album=album,
        bit_depth=bit_depth,
        sampling_rate=sampling_rate,
        extension=extension,
        bitrate_kbps=bitrate_kbps,
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
            rendered += ext

        rendered_segments.append(rendered)

    if not rendered_segments:
        rendered_segments = [sanitize(track.display_title or "track") + ext]

    return Path(*rendered_segments)


# ── Legacy helpers ────────────────────────────────────────────────────────

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
    bitrate_kbps: Optional[int] = None,
    format_name: Optional[str] = None,
) -> str:
    title = sanitize(album.display_title)
    
    # Force High-Res metadata ONLY if we aren't explicitly in MP3 mode
    use_fallback = (format_name != "MP3")
    final_bd = bit_depth or (album.maximum_bit_depth if use_fallback else None)
    final_sr = sampling_rate or (album.maximum_sampling_rate if use_fallback else None)

    qtag = f"[{quality_tag(final_bd, final_sr, format_name=format_name, bitrate_kbps=bitrate_kbps)}]"
    year = f" [{album.release_date_original[:4]}]" if album.release_date_original else ""
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
    bitrate_kbps: Optional[int] = None,
    template: Optional[str] = None,
    playlist_name: Optional[str] = None,
    playlist_index: Optional[int] = None,
    filename_template=None,
) -> Path:
    # Standardize extension
    ext = extension if extension.startswith(".") else f".{extension}"
    is_mp3 = ext.lower() == ".mp3"

    if template:
        rel = render_template(
            template=template,
            track=track,
            extension=ext,
            album=album,
            bit_depth=bit_depth,
            sampling_rate=sampling_rate,
            bitrate_kbps=bitrate_kbps,
            playlist_name=playlist_name,
            playlist_index=playlist_index,
        )
        path = dest_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    parts: list[str] = []

    if album:
        # Determine format for the folder name
        fmt = "MP3" if is_mp3 else "FLAC"
        # Kill the bit depth/rate variables if we are doing MP3
        folder_bd = None if is_mp3 else bit_depth
        folder_sr = None if is_mp3 else sampling_rate

        parts.append(album_folder(
            album, 
            bit_depth=folder_bd, 
            sampling_rate=folder_sr, 
            bitrate_kbps=bitrate_kbps,
            format_name=fmt
        ))
        
        if is_multi_disc and track.media_number:
            parts.append(disc_folder(track.media_number))

    if filename_template is not None:
        filename = filename_template(track)
    elif album:
        filename = album_track_filename(track, ext)
    else:
        filename = track_filename(track, ext)

    parts.append(filename)
    path = dest_dir.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

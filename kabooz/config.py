# kabooz/config.py
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .exceptions import ConfigError

import tomli_w

_CONFIG_DIR   = Path.home() / ".config" / "qobuz"
_CONFIG_PATH  = _CONFIG_DIR / "config.toml"
_SESSION_PATH = _CONFIG_DIR / "session.json"

# ── Sub-configs ────────────────────────────────────────────────────────────

@dataclass
class CredentialsConfig:
    app_id:     str = ""
    app_secret: str = ""
    # Path or URL to a token pool file. Empty = not using a pool.
    pool:       str = ""


@dataclass
class DownloadConfig:
    output_dir:          str   = "."
    quality:             str   = "hi_res"
    # 1 = sequential. Higher values parallelise track downloads within an
    # album or playlist. Be conservative — Qobuz rate-limits aggressively.
    max_workers:         int   = 1
    # Shell command template for an external downloader. Leave empty to use
    # the built-in httpx downloader. Placeholders: {url}, {output}.
    # Example: "aria2c -x 16 -s 16 -o {output} {url}"
    external_downloader: str   = ""
    read_timeout:        float = 300.0
    connect_timeout:     float = 10.0


@dataclass
class TaggingConfig:
    enabled:         bool = True
    embed_cover:     bool = True
    # Save a separate cover.jpg in the album folder.
    save_cover_file: bool = False
    fetch_lyrics:    bool = False


@dataclass
class NamingConfig:
    # Templates are relative paths from the output directory.
    # A "/" in the template creates a subdirectory.
    #
    # Available placeholders for all templates:
    #   {title}         track title
    #   {artist}        primary track artist
    #   {track}         track number  (supports format specs, e.g. {track:02d})
    #   {disc}          disc number
    #   {isrc}          ISRC code
    #   {bit_depth}     audio bit depth
    #   {sampling_rate} sampling rate (float)
    #   {quality}       formatted quality tag, e.g. "FLAC 24bit 96kHz"
    #
    # Additional placeholders when album context is available:
    #   {album}         album title
    #   {albumartist}   album-level artist
    #   {year}          4-digit release year
    #   {label}         record label name
    #   {genre}         primary genre
    #   {upc}           album UPC
    #
    # Additional placeholders for playlists:
    #   {playlist}      playlist name
    #   {index}         position in playlist (supports format specs)

    album:       str = "{albumartist}/{album} [{quality}] [{year}]/{track:02d}. {title}"
    single:      str = "{artist} - {title}"
    ep:          str = "{albumartist}/{album} [{quality}] [{year}]/{track:02d}. {title}"
    compilation: str = "{album} [{quality}] [{year}]/{track:02d}. {title}"
    playlist:    str = "{playlist} [{quality}]/{index:02d}. {title}"


@dataclass
class MusicBrainzConfig:
    # When enabled, every track download looks up its ISRC on MusicBrainz
    # and adds the recording MBID and artist MBID to the file tags.
    # Requires an internet connection and respects MB's 1 req/sec rate limit.
    enabled: bool = False


@dataclass
class QobuzConfig:
    credentials:  CredentialsConfig  = field(default_factory=CredentialsConfig)
    download:     DownloadConfig     = field(default_factory=DownloadConfig)
    tagging:      TaggingConfig      = field(default_factory=TaggingConfig)
    naming:       NamingConfig       = field(default_factory=NamingConfig)
    musicbrainz:  MusicBrainzConfig  = field(default_factory=MusicBrainzConfig)


# ── Read / write ───────────────────────────────────────────────────────────
_VALID_QUALITIES = {"mp3_320", "flac_16", "flac_24_96", "hi_res"}

def validate_config(cfg: QobuzConfig) -> None:
    """
    Raise ConfigError with a clear message on the first invalid value found.
    Called after loading or updating config so errors surface immediately,
    not at runtime mid-download.
    """
    d = cfg.download
    t = cfg.tagging
    n = cfg.naming

    if d.quality.lower() not in _VALID_QUALITIES:
        raise ConfigError(
            f"download.quality={d.quality!r} is invalid. "
            f"Valid values: {', '.join(sorted(_VALID_QUALITIES))}"
        )

    if d.max_workers < 1:
        raise ConfigError(
            f"download.max_workers={d.max_workers} must be >= 1."
        )

    if d.read_timeout <= 0:
        raise ConfigError(
            f"download.read_timeout={d.read_timeout} must be > 0."
        )

    if d.connect_timeout <= 0:
        raise ConfigError(
            f"download.connect_timeout={d.connect_timeout} must be > 0."
        )

    if not isinstance(t.enabled, bool):
        raise ConfigError(f"tagging.enabled must be true or false.")
    if not isinstance(t.embed_cover, bool):
        raise ConfigError(f"tagging.embed_cover must be true or false.")
    if not isinstance(t.save_cover_file, bool):
        raise ConfigError(f"tagging.save_cover_file must be true or false.")
    if not isinstance(t.fetch_lyrics, bool):
        raise ConfigError(f"tagging.fetch_lyrics must be true or false.")

    # Validate naming templates contain no unknown placeholders.
    _KNOWN_PLACEHOLDERS = {
        "title", "artist", "track", "disc", "isrc",
        "bit_depth", "sampling_rate", "quality",
        "album", "albumartist", "year", "label", "genre", "upc",
        "playlist", "index",
    }
    for tmpl_name, tmpl in {
        "naming.album":       n.album,
        "naming.single":      n.single,
        "naming.ep":          n.ep,
        "naming.compilation": n.compilation,
        "naming.playlist":    n.playlist,
    }.items():
        import string
        used = {
            f.split(":")[0]   # strip format spec e.g. {track:02d} → track
            for _, f, _, _ in string.Formatter().parse(tmpl)
            if f is not None
        }
        unknown = used - _KNOWN_PLACEHOLDERS
        if unknown:
            raise ConfigError(
                f"{tmpl_name} uses unknown placeholder(s): "
                f"{', '.join(f'{{{u}}}' for u in sorted(unknown))}"
            )
            
def load_config(path: Path = _CONFIG_PATH) -> QobuzConfig:
    """
    Read the TOML config file and return a QobuzConfig.
    Missing keys fall back to dataclass defaults, so a partial or empty
    config file is always valid.

    Environment variables override config file values for credentials:
        QOBUZ_APP_ID, QOBUZ_APP_SECRET
    """
    cfg = QobuzConfig()

    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        c = raw.get("credentials", {})
        cfg.credentials = CredentialsConfig(
            app_id     = c.get("app_id",     cfg.credentials.app_id),
            app_secret = c.get("app_secret", cfg.credentials.app_secret),
            pool       = c.get("pool",       cfg.credentials.pool),
        )

        d = raw.get("download", {})
        cfg.download = DownloadConfig(
            output_dir          = d.get("output_dir",          cfg.download.output_dir),
            quality             = d.get("quality",             cfg.download.quality),
            max_workers         = d.get("max_workers",         cfg.download.max_workers),
            external_downloader = d.get("external_downloader", cfg.download.external_downloader),
            read_timeout        = d.get("read_timeout",        cfg.download.read_timeout),
            connect_timeout     = d.get("connect_timeout",     cfg.download.connect_timeout),
        )

        t = raw.get("tagging", {})
        cfg.tagging = TaggingConfig(
            enabled         = t.get("enabled",         cfg.tagging.enabled),
            embed_cover     = t.get("embed_cover",     cfg.tagging.embed_cover),
            save_cover_file = t.get("save_cover_file", cfg.tagging.save_cover_file),
            fetch_lyrics    = t.get("fetch_lyrics",    cfg.tagging.fetch_lyrics),
        )

        n = raw.get("naming", {})
        cfg.naming = NamingConfig(
            album       = n.get("album",       cfg.naming.album),
            single      = n.get("single",      cfg.naming.single),
            ep          = n.get("ep",          cfg.naming.ep),
            compilation = n.get("compilation", cfg.naming.compilation),
            playlist    = n.get("playlist",    cfg.naming.playlist),
        )

        m = raw.get("musicbrainz", {})
        cfg.musicbrainz = MusicBrainzConfig(
            enabled = m.get("enabled", cfg.musicbrainz.enabled),
        )

    # Environment variables always win for credentials.
    env_id     = os.environ.get("QOBUZ_APP_ID")
    env_secret = os.environ.get("QOBUZ_APP_SECRET")
    if env_id:
        cfg.credentials.app_id = env_id
    if env_secret:
        cfg.credentials.app_secret = env_secret

    validate_config(cfg)
    return cfg


def save_config(cfg: QobuzConfig, path: Path = _CONFIG_PATH) -> None:
    """Serialise a QobuzConfig to TOML and write it to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "credentials": asdict(cfg.credentials),
        "download":    asdict(cfg.download),
        "tagging":     asdict(cfg.tagging),
        "naming":      asdict(cfg.naming),
        "musicbrainz": asdict(cfg.musicbrainz),
    }
    path.write_bytes(tomli_w.dumps(data).encode())


def update_config(updates: dict, path: Path = _CONFIG_PATH) -> QobuzConfig:
    """
    Load the current config, apply a dict of nested updates, save, and
    return the updated config.

    updates format mirrors the TOML structure:
        {"credentials": {"app_id": "123"}, "download": {"max_workers": 3}}
    """
    cfg = load_config(path)

    for section, values in updates.items():
        obj = getattr(cfg, section, None)
        if obj is None:
            continue
        for key, val in values.items():
            if hasattr(obj, key):
                setattr(obj, key, val)

    validate_config(cfg)
    save_config(cfg, path)
    return cfg


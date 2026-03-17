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
    pool:       str = ""


@dataclass
class DownloadConfig:
    output_dir:          str   = "."
    quality:             str   = "hi_res"
    max_workers:         int   = 1
    external_downloader: str   = ""
    read_timeout:        float = 300.0
    connect_timeout:     float = 10.0


@dataclass
class TaggingConfig:
    enabled:         bool = True
    embed_cover:     bool = True
    save_cover_file: bool = False
    fetch_lyrics:    bool = False


@dataclass
class NamingConfig:
    album:       str = "{albumartist}/{album} [{quality}] [{year}]/{track:02d}. {title}"
    single:      str = "{artist} - {title}"
    ep:          str = "{albumartist}/{album} [{quality}] [{year}]/{track:02d}. {title}"
    compilation: str = "{album} [{quality}] [{year}]/{track:02d}. {title}"
    playlist:    str = "{playlist} [{quality}]/{index:02d}. {title}"
    # Used by `qobuz artist` — the artist folder itself is injected by the
    # session layer, so this template must NOT start with {albumartist}.
    artist:      str = "{album} [{quality}] [{year}]/{track:02d}. {title}"


@dataclass
class MusicBrainzConfig:
    enabled: bool = False


@dataclass
class LocalDataConfig:
    """
    Configuration for the local user data store.

    The store works in ALL authentication modes, including token pool
    mode where writes to the Qobuz API are disabled. Pool users can
    maintain their own local favorites and playlists here.

    Fields:
        data_dir:            Root directory for the SQLite database,
                             local playlist TOML files, and exports.
        track_history:       Log every download to the history table.
        auto_sync_favorites: On login (personal session only), pull
                             all Qobuz favorites into the local store.
                             Pool mode ignores this setting.
        playlists_subdir:    Subdirectory under data_dir for playlist
                             TOML files. Relative paths are resolved
                             against data_dir.
    """
    data_dir:            str  = str(Path.home() / ".local" / "share" / "qobuz")
    track_history:       bool = True
    auto_sync_favorites: bool = False
    playlists_subdir:    str  = "playlists"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir).expanduser() / "library.db"

    @property
    def playlists_dir(self) -> Path:
        sub = Path(self.playlists_subdir)
        if sub.is_absolute():
            return sub
        return Path(self.data_dir).expanduser() / sub

    @property
    def exports_dir(self) -> Path:
        return Path(self.data_dir).expanduser() / "exports"


@dataclass
class QobuzConfig:
    credentials:  CredentialsConfig  = field(default_factory=CredentialsConfig)
    download:     DownloadConfig     = field(default_factory=DownloadConfig)
    tagging:      TaggingConfig      = field(default_factory=TaggingConfig)
    naming:       NamingConfig       = field(default_factory=NamingConfig)
    musicbrainz:  MusicBrainzConfig  = field(default_factory=MusicBrainzConfig)
    local_data:   LocalDataConfig    = field(default_factory=LocalDataConfig)


# ── Read / write ───────────────────────────────────────────────────────────
_VALID_QUALITIES = {"mp3_320", "flac_16", "flac_24_96", "hi_res"}


def validate_config(cfg: QobuzConfig) -> None:
    """
    Raise ConfigError with a clear message on the first invalid value found.
    """
    d = cfg.download
    t = cfg.tagging
    n = cfg.naming
    ld = cfg.local_data

    if d.quality.lower() not in _VALID_QUALITIES:
        raise ConfigError(
            f"download.quality={d.quality!r} is invalid. "
            f"Valid values: {', '.join(sorted(_VALID_QUALITIES))}"
        )
    if d.max_workers < 1:
        raise ConfigError(f"download.max_workers={d.max_workers} must be >= 1.")
    if d.read_timeout <= 0:
        raise ConfigError(f"download.read_timeout={d.read_timeout} must be > 0.")
    if d.connect_timeout <= 0:
        raise ConfigError(f"download.connect_timeout={d.connect_timeout} must be > 0.")

    for attr in ("enabled", "embed_cover", "save_cover_file", "fetch_lyrics"):
        if not isinstance(getattr(t, attr), bool):
            raise ConfigError(f"tagging.{attr} must be true or false.")

    for attr in ("track_history", "auto_sync_favorites"):
        if not isinstance(getattr(ld, attr), bool):
            raise ConfigError(f"local_data.{attr} must be true or false.")

    _KNOWN_PLACEHOLDERS = {
        "title", "raw_title", "work", "version",
        "artist", "track", "disc", "isrc",
        "bit_depth", "sampling_rate", "quality",
        "album", "raw_album", "albumartist", "year",
        "label", "genre", "upc",
        "playlist", "index",
    }
    for tmpl_name, tmpl in {
        "naming.album":       n.album,
        "naming.single":      n.single,
        "naming.ep":          n.ep,
        "naming.compilation": n.compilation,
        "naming.playlist":    n.playlist,
        "naming.artist":      n.artist,
    }.items():
        import string
        used = {
            f.split(":")[0]
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
    Missing keys fall back to dataclass defaults.

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
            artist      = n.get("artist",      cfg.naming.artist),
        )

        m = raw.get("musicbrainz", {})
        cfg.musicbrainz = MusicBrainzConfig(
            enabled = m.get("enabled", cfg.musicbrainz.enabled),
        )

        ld = raw.get("local_data", {})
        cfg.local_data = LocalDataConfig(
            data_dir            = ld.get("data_dir",            cfg.local_data.data_dir),
            track_history       = ld.get("track_history",       cfg.local_data.track_history),
            auto_sync_favorites = ld.get("auto_sync_favorites", cfg.local_data.auto_sync_favorites),
            playlists_subdir    = ld.get("playlists_subdir",    cfg.local_data.playlists_subdir),
        )

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
    # Exclude the computed property methods — asdict only captures fields.
    ld = asdict(cfg.local_data)
    data = {
        "credentials": asdict(cfg.credentials),
        "download":    asdict(cfg.download),
        "tagging":     asdict(cfg.tagging),
        "naming":      asdict(cfg.naming),
        "musicbrainz": asdict(cfg.musicbrainz),
        "local_data":  ld,
    }
    path.write_bytes(tomli_w.dumps(data).encode())


def update_config(updates: dict, path: Path = _CONFIG_PATH) -> QobuzConfig:
    """
    Load the current config, apply a dict of nested updates, save, and
    return the updated config.
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

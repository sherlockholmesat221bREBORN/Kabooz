# kabooz/session.py
"""
QobuzSession — the ONLY place where business logic lives.

Architectural rule
──────────────────
cli.py  and  tui.py  are presentation layers.
They call methods on QobuzSession and render the results.
They NEVER:
  · import from kabooz.client directly
  · call sess.client.anything
  · duplicate quality-parsing, URL-resolution, or reporting logic

Every operation a user can trigger from the CLI or TUI is a method here,
so it is equally callable from a plain Python script.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generator, Optional

from .client import QobuzClient
from .config import QobuzConfig, _CONFIG_PATH, _SESSION_PATH, load_config
from .download.downloader import Downloader, DownloadResult, GoodieResult
from .download.lyrics import fetch_lyrics
from .download.musicbrainz import lookup_isrc, apply_mb_tags
from .download.naming import sanitize
from .download.tagger import Tagger
from .exceptions import (
    APIError, ConfigError, NotFoundError, NotStreamableError,
    PoolModeError, TokenExpiredError, InvalidCredentialsError, NoAuthError,
)
from .local.export import (
    ImportResult, backup_to_tar, export_favorites_toml,
    import_favorites_toml, import_playlist_toml, restore_from_tar,
)
from .local.playlist import (
    LocalPlaylist, LocalPlaylistTrack,
    load_playlist, save_playlist, playlist_from_store_tracks,
)
from .local.store import LocalStore
from .models.album import Album
from .models.artist import Artist
from .models.favorites import UserFavorites, UserFavoriteIds, LabelDetail
from .models.playlist import Playlist
from .models.track import Track
from .models.user import UserProfile
from .quality import Quality, QUALITY_DESCENDING
from .url import parse_url


# ══════════════════════════════════════════════════════════════════════════
# Quality parsing helper
# ══════════════════════════════════════════════════════════════════════════

def _parse_quality(s: str) -> Quality:
    """
    Parse a quality string without requiring Quality.from_str().

    Accepts enum names (case-insensitive), common aliases, and raw
    format_id integers.  Raises ValueError on unknown input.
    """
    _ALIASES: dict[str, Quality] = {
        "mp3":        Quality.MP3_320,
        "320":        Quality.MP3_320,
        "cd":         Quality.FLAC_16,
        "lossless":   Quality.FLAC_16,
        "flac":       Quality.FLAC_16,
        "24bit":      Quality.FLAC_24_96,
        "24_96":      Quality.FLAC_24_96,
        "hires":      Quality.HI_RES,
        "hi_res":     Quality.HI_RES,
        "best":       Quality.HI_RES,
        "max":        Quality.HI_RES,
    }
    n = s.strip().lower().replace("-", "_")
    try:
        return Quality[n.upper()]
    except KeyError:
        pass
    if n in _ALIASES:
        return _ALIASES[n]
    try:
        return Quality(int(s))
    except (ValueError, KeyError):
        pass
    valid = ", ".join(m.name.lower() for m in Quality)
    raise ValueError(
        f"Unknown quality {s!r}. "
        f"Valid: {valid}. "
        f"Aliases: {', '.join(_ALIASES)}"
    )


# ══════════════════════════════════════════════════════════════════════════
# Public result types
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class StreamInfo:
    """
    Returned by prepare_stream(). Contains everything the TUI or any
    other player needs to start playback and report to Qobuz.
    """
    track_id:      str
    url:           str
    format_id:     int
    bit_depth:     Optional[int]   = None
    sampling_rate: Optional[float] = None
    mime_type:     str             = "audio/flac"


@dataclass
class TrackDownloadResult:
    download:     DownloadResult
    track:        Track
    album:        Optional[Album] = None
    tagged:       bool = False
    lyrics_found: bool = False
    mb_enriched:  bool = False


@dataclass
class AlbumDownloadResult:
    album:          Album
    tracks:         list[TrackDownloadResult] = field(default_factory=list)
    failed:         list[str] = field(default_factory=list)
    goodies_ok:     int = 0
    goodies_failed: int = 0

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.tracks if not r.download.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.tracks if r.download.skipped)


@dataclass
class PlaylistDownloadResult:
    name:   str
    tracks: list[TrackDownloadResult] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.tracks if not r.download.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.tracks if r.download.skipped)


# Callback signatures
ProgressCallback   = Callable[[int, int], None]           # (bytes_done, total_bytes)
TrackStartCallback = Callable[[str, int, int], None]       # (title, index, total)
TrackDoneCallback  = Callable[[TrackDownloadResult], None]
AlbumStartCallback = Callable[[str, int, int], None]       # (title, index, total)


# ══════════════════════════════════════════════════════════════════════════
# Internal stream reporter
# ══════════════════════════════════════════════════════════════════════════

class _StreamReporter:
    """
    Sends stream start/end events to the Qobuz API.
    Access only through QobuzSession.report_stream_* methods.
    """

    def __init__(self, client: QobuzClient, enabled: bool = True) -> None:
        self._client  = client
        self._enabled = enabled
        self._lock    = threading.Lock()
        self._active: dict[str, float] = {}   # track_id → wall-clock start

    def start(self, track_id: str, format_id: int = 27) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._active[track_id] = time.time()
        try:
            self._client._request(
                "GET", "/track/reportStreamingStart",
                params={"track_id": track_id, "format_id": format_id, "intent": "stream"},
            )
        except Exception:
            pass   # reporting is best-effort; never crash playback

    def end(self, track_id: str, duration_seconds: Optional[int] = None) -> None:
        if not self._enabled:
            return
        with self._lock:
            started = self._active.pop(track_id, None)
        elapsed = duration_seconds
        if elapsed is None and started is not None:
            elapsed = int(time.time() - started)
        try:
            params: dict[str, Any] = {"track_id": track_id}
            if elapsed is not None:
                params["duration"] = elapsed
            self._client._request("GET", "/track/reportStreamingEnd", params=params)
        except Exception:
            pass

    def cancel(self, track_id: str) -> None:
        """Cancel without reporting end (e.g. track skipped before 30 s)."""
        with self._lock:
            self._active.pop(track_id, None)


# ══════════════════════════════════════════════════════════════════════════
# QobuzSession
# ══════════════════════════════════════════════════════════════════════════

class QobuzSession:

    def __init__(
        self,
        client: QobuzClient,
        config: QobuzConfig,
        store:  Optional[LocalStore] = None,
        dev:    bool = False,
    ) -> None:
        self.client  = client
        self.config  = config
        self.store   = store or LocalStore(config.local_data.db_path)
        self._dev    = dev
        self._reporter = _StreamReporter(client, enabled=True)

    # ── Construction ───────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        config_path: Path = _CONFIG_PATH,
        dev: bool = False,
    ) -> "QobuzSession":
        cfg = load_config(config_path)
        if dev:
            from .dev import enable as _dev_enable
            _dev_enable()
        if cfg.credentials.pool:
            client = QobuzClient.from_token_pool(cfg.credentials.pool, dev=dev)
        else:
            client = QobuzClient.from_credentials(
                app_id=cfg.credentials.app_id,
                app_secret=cfg.credentials.app_secret,
                dev=dev,
            )
            client.load_session(_SESSION_PATH)
        return cls(client=client, config=cfg, dev=dev)

    @classmethod
    def from_client(
        cls,
        client: QobuzClient,
        config: Optional[QobuzConfig] = None,
    ) -> "QobuzSession":
        cfg = config or load_config()
        return cls(client=client, config=cfg)

    # ══════════════════════════════════════════════════════════════════════
    # Helpers — used by CLI, TUI, and scripts; never duplicated elsewhere
    # ══════════════════════════════════════════════════════════════════════

    def resolve_id(
        self,
        url_or_id: str,
        expected_type: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        Parse a Qobuz URL or bare numeric ID.

        Returns (entity_type, entity_id).
        For bare IDs entity_type is expected_type or 'unknown'.

        Raises ValueError if URL parses to a different type than expected.
        """
        if url_or_id.startswith("http"):
            entity_type, entity_id = parse_url(url_or_id)
            if expected_type and entity_type != expected_type:
                raise ValueError(
                    f"Expected a {expected_type} URL but got a {entity_type} URL."
                )
            return entity_type, entity_id
        return expected_type or "unknown", url_or_id

    def resolve_quality(
        self,
        s: Optional[str],
        default: Optional[Quality] = None,
    ) -> Quality:
        """
        Parse a quality string, falling back to config then HI_RES.

        Raises ConfigError (not ValueError) so callers get a consistent
        exception type regardless of where the bad string came from.
        """
        if not s:
            if default is not None:
                return default
            return _parse_quality(self.config.download.quality)
        try:
            return _parse_quality(s)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    # ══════════════════════════════════════════════════════════════════════
    # Stream reporting — public API for TUI/player
    # ══════════════════════════════════════════════════════════════════════

    def prepare_stream(
        self,
        track_id: str | int,
        quality: Quality = Quality.HI_RES,
    ) -> StreamInfo:
        """
        Resolve a CDN stream URL and report stream start to Qobuz.

        This is the ONLY way the TUI (or any player) should get a stream
        URL. It bundles URL resolution and stream reporting so reporting
        can never be accidentally skipped.

        Call report_stream_end() when playback stops normally.
        Call report_stream_cancel() when the track is skipped.
        """
        url_info = self.client.get_track_url(str(track_id), quality=quality)
        fmt_id   = url_info.get("format_id", int(quality))
        self._reporter.start(str(track_id), format_id=int(fmt_id))
        return StreamInfo(
            track_id      = str(track_id),
            url           = url_info["url"],
            format_id     = int(fmt_id),
            bit_depth     = url_info.get("bit_depth"),
            sampling_rate = url_info.get("sampling_rate"),
            mime_type     = url_info.get("mime_type", "audio/flac"),
        )

    def report_stream_end(
        self,
        track_id: str | int,
        duration_seconds: Optional[int] = None,
    ) -> None:
        """Report that playback of track_id ended normally."""
        self._reporter.end(str(track_id), duration_seconds)

    def report_stream_cancel(self, track_id: str | int) -> None:
        """Report that track_id was cancelled/skipped before finishing."""
        self._reporter.cancel(str(track_id))

    # ══════════════════════════════════════════════════════════════════════
    # Catalog reads — thin wrappers so CLI/TUI never import from .client
    # ══════════════════════════════════════════════════════════════════════

    def get_track(self, track_id: str | int) -> Track:
        return self.client.get_track(str(track_id))

    def get_album(
        self,
        album_id: str,
        extra: Optional[str] = None,
        limit: int = 1200,
        offset: int = 0,
    ) -> Album:
        return self.client.get_album(album_id, extra=extra, limit=limit, offset=offset)

    def get_artist(
        self,
        artist_id: str | int,
        extras: str = "albums",
        sort: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Artist:
        return self.client.get_artist(
            artist_id, extras=extras, sort=sort, limit=limit, offset=offset,
        )

    def get_playlist(
        self,
        playlist_id: str | int,
        limit: int = 500,
        offset: int = 0,
    ) -> Playlist:
        return self.client.get_playlist(playlist_id, limit=limit, offset=offset)

    def get_label(self, label_id: str | int, **kwargs) -> LabelDetail:
        return self.client.get_label(label_id, **kwargs)

    def search(
        self,
        query: str,
        search_type: str = "tracks",
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        """
        Search the Qobuz catalog.
        search_type: 'tracks', 'albums', 'artists', 'playlists'
        """
        return self.client.search(
            query=query, type=search_type, limit=limit, offset=offset,
        )

    def get_user_favorites(
        self,
        fav_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> UserFavorites:
        return self.client.get_user_favorites(
            type=fav_type, limit=limit, offset=offset,
        )

    def get_favorite_ids(self) -> UserFavoriteIds:
        return self.client.get_favorite_ids()

    def get_user_info(self) -> UserProfile:
        return self.client.get_user_info()

    def iter_user_playlists(self, page_size: int = 50) -> Generator[Playlist, None, None]:
        return self.client.iter_user_playlists(page_size=page_size)

    def iter_playlist_track_summaries(
        self, playlist_id: str | int, page_size: int = 500,
    ) -> Generator[Any, None, None]:
        return self.client.iter_playlist_track_summaries(playlist_id, page_size)

    def iter_releases(
        self,
        artist_id: str | int,
        release_type: Optional[str] = None,
        page_size: int = 50,
    ) -> Generator[Any, None, None]:
        return self.client.iter_releases(
            artist_id, release_type=release_type, page_size=page_size,
        )

    def get_similar_artists(
        self, artist_id: str | int, limit: int = 10,
    ) -> list[Artist]:
        return self.client.get_similar_artists(artist_id, limit=limit)

    # ── Discovery ──────────────────────────────────────────────────────────

    def get_new_releases(
        self,
        release_type: str = "new-releases",
        genre_id: Optional[int] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        return self.client.get_new_releases(
            type=release_type, genre_id=genre_id, limit=limit, offset=offset,
        )

    def get_featured_playlists(
        self,
        pl_type: str = "editor-picks",
        genre_id: Optional[int] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        return self.client.get_featured_playlists(
            type=pl_type, genre_id=genre_id, limit=limit, offset=offset,
        )

    def get_genres(self, parent_id: Optional[int] = None) -> dict:
        return self.client.get_genres(parent_id=parent_id)

    # ══════════════════════════════════════════════════════════════════════
    # Radio and recommendations
    # ══════════════════════════════════════════════════════════════════════

    def get_track_radio(
        self,
        track_id: str | int,
        limit: int = 25,
    ) -> list[Track]:
        """
        Build a radio queue seeded by a single track.

        Tries the official /track/getSuggestions endpoint first;
        falls back to similar-artist browsing if unavailable.
        """
        import random

        # Attempt 1 — official suggestions
        try:
            data  = self.client._request(
                "GET", "/track/getSuggestions",
                params={"track_id": str(track_id), "limit": limit},
            )
            items = (
                data.get("tracks", {}).get("items")
                or data.get("items")
                or []
            )
            if items:
                tracks = []
                for item in items[:limit]:
                    try:
                        tracks.append(Track.from_dict(item))
                    except Exception:
                        pass
                if tracks:
                    return tracks
        except Exception:
            pass

        # Attempt 2 — similar-artist fallback
        try:
            seed = self.client.get_track(str(track_id))
        except Exception:
            return []

        similar_ids: list[Any] = []
        if seed.performer:
            try:
                a = self.client.get_artist(
                    seed.performer.id, extras="", limit=1,
                )
                similar_ids = (a.similar_artist_ids or [])[:6]
            except Exception:
                pass

        tracks: list[Track] = []
        for sid in similar_ids:
            if len(tracks) >= limit:
                break
            try:
                artist = self.client.get_artist(sid, extras="albums", limit=5)
                if not artist.albums or not artist.albums.items:
                    continue
                alb = random.choice(artist.albums.items)
                full = self.client.get_album(alb.id, limit=20)
                if full.tracks and full.tracks.items:
                    for s in random.sample(
                        full.tracks.items, min(4, len(full.tracks.items))
                    ):
                        try:
                            tracks.append(self.client.get_track(str(s.id)))
                        except Exception:
                            pass
            except Exception:
                continue

        random.shuffle(tracks)
        return tracks[:limit]

    def get_artist_radio(
        self,
        artist_id: str | int,
        limit: int = 25,
    ) -> list[Track]:
        """
        Build a radio queue seeded by an artist.
        Picks tracks from the artist + their similar artists.
        """
        import random
        tracks: list[Track] = []

        # Artist's own releases
        try:
            for release in self.client.iter_releases(artist_id, page_size=10):
                if len(tracks) >= limit // 2 or not release.id:
                    break
                try:
                    alb = self.client.get_album(release.id, limit=10)
                    if alb.tracks and alb.tracks.items:
                        for s in alb.tracks.items[:3]:
                            tracks.append(self.client.get_track(str(s.id)))
                except Exception:
                    continue
        except Exception:
            pass

        # Fill from similar artists
        try:
            for sim in self.client.get_similar_artists(artist_id, limit=8):
                if len(tracks) >= limit:
                    break
                try:
                    for release in self.client.iter_releases(sim.id, page_size=3):
                        if not release.id:
                            continue
                        alb = self.client.get_album(release.id, limit=5)
                        if alb.tracks and alb.tracks.items:
                            tracks.append(
                                self.client.get_track(
                                    str(random.choice(alb.tracks.items).id)
                                )
                            )
                        break
                except Exception:
                    continue
        except Exception:
            pass

        random.shuffle(tracks)
        return tracks[:limit]

    def get_recommendations(self, limit: int = 20) -> dict[str, list]:
        """
        Return editorial and personalised recommendation lists.

        Keys:
            "new_releases"    — raw album dicts from the new-releases feed
            "press_awards"    — critic picks
            "featured"        — editorial playlist dicts
            "similar_artists" — Artist objects similar to your favourites
        """
        result: dict[str, list] = {
            "new_releases":    [],
            "press_awards":    [],
            "featured":        [],
            "similar_artists": [],
        }
        try:
            result["new_releases"] = (
                self.client.get_new_releases(type="new-releases", limit=limit)
                .get("albums", {}).get("items", [])
            )
        except Exception:
            pass
        try:
            result["press_awards"] = (
                self.client.get_new_releases(type="press-awards", limit=limit)
                .get("albums", {}).get("items", [])
            )
        except Exception:
            pass
        try:
            result["featured"] = (
                self.client.get_featured_playlists(type="editor-picks", limit=limit)
                .get("playlists", {}).get("items", [])
            )
        except Exception:
            pass
        try:
            fav_ids    = self.client.get_favorite_ids()
            artist_ids = (fav_ids.artist_ids or [])[:5]
            seen: set[str] = set()
            for aid in artist_ids:
                try:
                    for a in self.client.get_similar_artists(aid, limit=4):
                        if str(a.id) not in seen:
                            seen.add(str(a.id))
                            result["similar_artists"].append(a)
                except Exception:
                    continue
        except Exception:
            pass
        return result

    # ══════════════════════════════════════════════════════════════════════
    # Post-download pipeline (tagging, lyrics, MusicBrainz, history)
    # ══════════════════════════════════════════════════════════════════════

    def _post_download(
        self,
        result: DownloadResult,
        track:  Track,
        album:  Optional[Album] = None,
        embed_cover:       Optional[bool] = None,
        fetch_lyrics_flag: Optional[bool] = None,
        save_cover_file:   Optional[bool] = None,
    ) -> TrackDownloadResult:
        cfg = self.config
        t   = cfg.tagging
        mb  = cfg.musicbrainz

        _embed_cover  = embed_cover       if embed_cover       is not None else t.embed_cover
        _fetch_lyrics = fetch_lyrics_flag if fetch_lyrics_flag is not None else t.fetch_lyrics
        _save_cover   = save_cover_file   if save_cover_file   is not None else t.save_cover_file

        # ── Skip guard ────────────────────────────────────────────────────
        # The final file already existed before this run — the .part
        # convention guarantees both download and tagging completed on a
        # prior run, so there is nothing to do here.
        if result.skipped:
            return TrackDownloadResult(result, track, album)

        tagged = lyrics_found = mb_enriched = False

        if not t.enabled:
            # Tagging disabled — just rename .part → final and return.
            self._finalise(result)
            return TrackDownloadResult(result, track, album)

        lyrics_result = None
        if _fetch_lyrics:
            artist_name = (
                track.performer.name if track.performer
                else (album.artist.name if album and album.artist else "")
            )
            lyrics_result = fetch_lyrics(
                title=track.title,
                artist=artist_name,
                album=album.title if album else None,
                duration=track.duration,
            )
            lyrics_found = bool(lyrics_result and lyrics_result.found)

        if not result.dev_stub:
            Tagger().tag(
                path=result.path,
                track=track,
                album=album,
                lyrics=lyrics_result,
                embed_cover=_embed_cover,
            )
            tagged = True

        if mb.enabled and track.isrc and not result.dev_stub:
            mb_result = lookup_isrc(track.isrc)
            if mb_result.found:
                apply_mb_tags(result.path, mb_result)
                mb_enriched = True

        # ── Rename .part → final ──────────────────────────────────────────
        # Every pipeline step that writes to the file is done. Only now do
        # we rename to the final path — making "file exists" an unambiguous
        # "fully complete" signal, exactly like yt-dlp's convention.
        # If anything above raised an exception, the .part file stays on
        # disk. The next run will detect the complete .part, skip the
        # download, and retry the tagging pipeline automatically.
        if not result.dev_stub:
            self._finalise(result)

        # ── Save standalone cover file ────────────────────────────────────
        # Must happen after _finalise so result.path is the final path.
        if _save_cover and not result.dev_stub:
            if album:
                self._save_cover_file(result.path.parent, album)
            else:
                self._save_cover_file(
                    result.path.parent,
                    album=None,
                    track=track,
                    track_stem=result.path.stem,
                )

        if cfg.local_data.track_history and not result.dev_stub:
            try:
                self.store.log_play(
                    track_id=str(track.id),
                    title=track.display_title,
                    artist=track.performer.name if track.performer else "",
                    album=(album or track.album) and (
                        album.title if album else track.album.title  # type: ignore
                    ) or "",
                )
            except Exception:
                pass

        return TrackDownloadResult(
            result, track, album, tagged, lyrics_found, mb_enriched,
        )

    @staticmethod
    def _finalise(result: DownloadResult) -> None:
        """
        Rename result.path from <name>.part to <name> (mutates result.path).

        If the path doesn't end with '.part' (dev mode writes directly to
        the final path) this is a no-op, so it is always safe to call.
        """
        if result.path.name.endswith(".part"):
            final = result.path.with_name(result.path.name[:-5])
            result.path.rename(final)
            result.path = final

    # ══════════════════════════════════════════════════════════════════════
    # Downloads
    # ══════════════════════════════════════════════════════════════════════

    def download_track(
        self,
        url_or_id:         str,
        quality:           Optional[Quality] = None,
        dest_dir:          Optional[Path]    = None,
        template:          Optional[str]     = None,
        embed_cover:       Optional[bool]    = None,
        fetch_lyrics_flag: Optional[bool]    = None,
        save_cover_file:   Optional[bool]    = None,
        on_progress:       Optional[ProgressCallback] = None,
        workers:           Optional[int]     = None,
        external_downloader: Optional[str]   = None,
    ) -> TrackDownloadResult:
        cfg = self.config
        _, track_id = self.resolve_id(url_or_id, "track")
        q      = quality or _parse_quality(cfg.download.quality)
        dest   = dest_dir or Path(cfg.download.output_dir)
        tmpl   = template or cfg.naming.single
        n_work = workers  or cfg.download.max_workers
        ext_dl = external_downloader or cfg.download.external_downloader

        track_obj = self.client.get_track(track_id)
        url_info  = self.client.get_track_url(track_id, quality=q)

        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=n_work,
            external_downloader=ext_dl,
            naming_template=tmpl,
            dev=self._dev,
        ) as dl:
            result = dl.download_track(
                track=track_obj, url_info=url_info,
                dest_dir=dest, album=None, on_progress=on_progress,
            )

        return self._post_download(
            result, track_obj, None,
            embed_cover=embed_cover,
            fetch_lyrics_flag=fetch_lyrics_flag,
            save_cover_file=save_cover_file,
        )

    def download_album(
        self,
        url_or_id:         str,
        quality:           Optional[Quality] = None,
        dest_dir:          Optional[Path]    = None,
        template:          Optional[str]     = None,
        embed_cover:       Optional[bool]    = None,
        fetch_lyrics_flag: Optional[bool]    = None,
        save_cover_file:   Optional[bool]    = None,
        download_goodies:  bool              = True,
        on_track_start:    Optional[TrackStartCallback] = None,
        on_track_done:     Optional[TrackDoneCallback]  = None,
        on_progress:       Optional[ProgressCallback]   = None,
        workers:           Optional[int]     = None,
        external_downloader: Optional[str]   = None,
    ) -> AlbumDownloadResult:
        from .dev import dev_log

        cfg = self.config
        _, album_id = self.resolve_id(url_or_id, "album")
        q      = quality or _parse_quality(cfg.download.quality)
        dest   = dest_dir or Path(cfg.download.output_dir)
        n_work = workers  or cfg.download.max_workers
        ext_dl = external_downloader or cfg.download.external_downloader

        album_obj    = self.client.get_album(album_id)
        release_type = (album_obj.release_type or "album").lower()

        if template:
            tmpl = template
        elif release_type == "single":
            tmpl = cfg.naming.single
        elif release_type == "ep":
            tmpl = cfg.naming.ep
        elif release_type == "compilation":
            tmpl = cfg.naming.compilation
        else:
            tmpl = cfg.naming.album

        agg           = AlbumDownloadResult(album=album_obj)
        track_results: list[DownloadResult] = []

        summaries = (
            album_obj.tracks.items
            if album_obj.tracks
            and len(album_obj.tracks.items) >= (album_obj.tracks.total or 0)
            else list(self.client.iter_album_tracks(album_id))
        )
        total = len(summaries)

        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=n_work,
            external_downloader=ext_dl,
            naming_template=tmpl,
            dev=self._dev,
        ) as dl:
            for i, summary in enumerate(summaries, 1):
                if on_track_start:
                    title = getattr(summary, "display_title",
                                    getattr(summary, "title", ""))
                    on_track_start(title, i, total)

                try:
                    track_obj = self.client.get_track(str(summary.id))
                    url_info  = self.client.get_track_url(str(track_obj.id), quality=q)
                except NotStreamableError as exc:
                    _title = getattr(summary, "display_title", getattr(summary, "title", str(summary.id)))
                    dev_log(f"[yellow]track {summary.id} not streamable: {exc}[/yellow]")
                    agg.failed.append(f"{_title} — not streamable")
                    continue
                except APIError as exc:
                    _title = getattr(summary, "display_title", getattr(summary, "title", str(summary.id)))
                    dev_log(f"[red]track {summary.id} API error: {exc}[/red]")
                    agg.failed.append(f"{_title} — API error: {exc}")
                    continue

                try:
                    dl_result = dl.download_track(
                        track=track_obj, url_info=url_info,
                        dest_dir=dest, album=album_obj, on_progress=on_progress,
                    )
                except Exception as exc:
                    dev_log(f"[red]track {track_obj.id} download error: {exc}[/red]")
                    agg.failed.append(f"{track_obj.display_title} — {type(exc).__name__}: {exc}")
                    continue

                track_results.append(dl_result)
                tdr = self._post_download(
                    dl_result, track_obj, album_obj,
                    embed_cover=embed_cover,
                    fetch_lyrics_flag=fetch_lyrics_flag,
                    save_cover_file=save_cover_file,
                )
                agg.tracks.append(tdr)
                if on_track_done:
                    on_track_done(tdr)

            if download_goodies and album_obj.goodies:
                album_dir = dest
                for r in track_results:
                    candidate = r.path.parent
                    if album_obj.media_count and album_obj.media_count > 1:
                        candidate = candidate.parent
                    if candidate.is_dir():
                        album_dir = candidate
                        break
                for goodie in album_obj.goodies:
                    gr = dl.download_goodie(goodie, album_dir)
                    if gr.ok:
                        agg.goodies_ok += 1
                    else:
                        agg.goodies_failed += 1

        return agg
        
    def download_album_goodies(
        self,
        url_or_id: str,
        dest_dir:  Optional[Path] = None,
        on_progress_each: Optional[Callable[[str, int, int], None]] = None,
    ) -> list:
        """
        Download only the bonus files (goodies) for an album.

        Goodies are non-audio extras bundled with an album — typically a
        booklet PDF, but may also be hi-res videos or other digital files.
        They are placed in ``<dest_dir>/<Album [Format] [Year]>/``.

        Parameters:
            on_progress_each: Called for each goodie as it downloads:
                              (filename, bytes_done, total_bytes).

        Returns a list of GoodieResult objects. Empty list if no goodies.
        """
        from .download.downloader import Downloader
        from .download.naming import quality_tag

        cfg = self.config
        _, album_id = self.resolve_id(url_or_id, "album")
        dest = dest_dir or Path(cfg.download.output_dir)

        album_obj = self.client.get_album(album_id)
        if not album_obj.goodies:
            return []

        # Mirror the folder name that download_album would create so goodies
        # land next to any already-downloaded audio files.
        qtag = quality_tag(
            album_obj.maximum_bit_depth,
            album_obj.maximum_sampling_rate,
        )
        year = (
            f" [{album_obj.release_date_original[:4]}]"
            if album_obj.release_date_original else ""
        )
        album_dir = dest / sanitize(
            f"{album_obj.display_title} [{qtag}]{year}"
        )
        album_dir.mkdir(parents=True, exist_ok=True)

        # ── Pre-compute deduplicated filenames ─────────────────────────────
        # On case-insensitive filesystems (Android, macOS) two goodies whose
        # sanitized names differ only in case would collide.  We resolve
        # clashes upfront so every goodie gets a unique path.
        def _goodie_filename(goodie) -> str:
            url = goodie.original_url or goodie.url or ""
            url_filename = url.split("?")[0].rstrip("/").split("/")[-1]
            ext = ""
            if "." in url_filename:
                ext = "." + url_filename.rsplit(".", 1)[-1].lower()
            base = sanitize(goodie.name) if goodie.name else (
                sanitize(url_filename) if url_filename else f"goodie_{goodie.id}"
            )
            return base + ext

        used: dict[str, int] = {}
        dest_paths: list[Path] = []
        for goodie in album_obj.goodies:
            name = _goodie_filename(goodie)
            stem, _, ext = name.rpartition(".")
            ext = ("." + ext) if ext else ""
            key = stem.lower()
            if key in used:
                used[key] += 1
                unique_name = f"{stem} ({used[key]}){ext}"
            else:
                used[key] = 1
                unique_name = name
            dest_paths.append(album_dir / unique_name)

        # ── Download ───────────────────────────────────────────────────────
        results = []
        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            dev=self._dev,
        ) as dl:
            for goodie, path in zip(album_obj.goodies, dest_paths):
                progress_cb = None
                if on_progress_each:
                    name = path.name
                    progress_cb = lambda done, total, n=name: on_progress_each(n, done, total)
                url = goodie.original_url or goodie.url
                if not url:
                    results.append(GoodieResult(
                        path=path, goodie=goodie, error="No URL available",
                    ))
                    continue
                try:
                    r = dl._download_goodie_to_path(url, path, progress_cb)
                    results.append(GoodieResult(
                        path=r.path, goodie=goodie, skipped=r.skipped,
                    ))
                except Exception as exc:
                    results.append(GoodieResult(
                        path=path, goodie=goodie, error=str(exc),
                    ))

        return results    

    def download_artist_discography(
        self,
        url_or_id:      str,
        release_type:   Optional[str]   = None,
        quality:        Optional[Quality] = None,
        dest_dir:       Optional[Path]   = None,
        template:       Optional[str]    = None,
        on_album_start: Optional[AlbumStartCallback] = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done:  Optional[TrackDoneCallback]  = None,
        workers:        Optional[int]    = None,
    ) -> list[AlbumDownloadResult]:
        """
        Download every release in an artist's discography.

        All albums are rooted under <dest_dir>/<ArtistName>/ so that a
        bare `qobuz artist 123` produces a clean, self-contained folder
        rather than mixing albums from different artists in the same
        directory.  The artist name used for the folder is the canonical
        name returned by the API for the queried artist ID — not the
        per-album albumartist field, which can vary across releases.

        Template selection (when no explicit --template is passed):
          · album / other  → naming.artist   (no {albumartist} prefix;
                                              the artist folder is injected here)
          · single         → naming.single   (flat, e.g. "Artist - Title")
          · ep             → naming.ep
          · compilation    → naming.compilation

        on_album_start(album_title, album_index, album_total) is fired
        before each album so the CLI/TUI can print a clear header.
        """
        from .dev import dev_log

        cfg = self.config
        _, artist_id = self.resolve_id(url_or_id, "artist")

        # ── Resolve artist name for the top-level folder ───────────────────
        base_dest = dest_dir or Path(cfg.download.output_dir)
        try:
            artist_obj = self.client.get_artist(artist_id, extras="")
            artist_dir = base_dest / sanitize(artist_obj.name)
        except Exception as exc:
            dev_log(f"[yellow]Could not fetch artist name ({exc}), using base dest[/yellow]")
            artist_dir = base_dest

        # ── Enumerate releases ─────────────────────────────────────────────
        try:
            releases = [
                r for r in self.client.iter_releases(
                    artist_id, release_type=release_type, page_size=100,
                )
                if r.id
            ]
        except Exception as exc:
            raise RuntimeError(
                f"Failed to list releases for artist {artist_id}: {exc}"
            ) from exc

        total   = len(releases)
        results = []

        for i, release in enumerate(releases, 1):
            try:
                album_obj = self.client.get_album(release.id)
            except APIError as exc:
                dev_log(f"[yellow]album {release.id} fetch error (skipping): {exc}[/yellow]")
                continue

            if on_album_start:
                on_album_start(album_obj.display_title, i, total)

            if template:
                effective_template: Optional[str] = template
            else:
                rtype = (album_obj.release_type or "album").lower()
                if rtype == "single":
                    effective_template = None
                elif rtype == "ep":
                    effective_template = None
                elif rtype == "compilation":
                    effective_template = None
                else:
                    effective_template = cfg.naming.artist

            try:
                agg = self.download_album(
                    release.id,
                    quality=quality,
                    dest_dir=artist_dir,
                    template=effective_template,
                    on_track_start=on_track_start,
                    on_track_done=on_track_done,
                    workers=workers,
                )
                results.append(agg)
            except NotStreamableError:
                dev_log(f"[yellow]{album_obj.display_title!r} not streamable — skipped[/yellow]")
            except APIError as exc:
                dev_log(f"[red]{album_obj.display_title!r} API error: {exc}[/red]")
            except Exception as exc:
                dev_log(
                    f"[red]{album_obj.display_title!r} unexpected error "
                    f"({type(exc).__name__}): {exc}[/red]"
                )

        return results

    def download_playlist(
        self,
        url_or_id:         str,
        quality:           Optional[Quality] = None,
        dest_dir:          Optional[Path]    = None,
        template:          Optional[str]     = None,
        embed_cover:       Optional[bool]    = None,
        fetch_lyrics_flag: Optional[bool]    = None,
        save_cover_file:   Optional[bool]    = None,
        on_track_start:    Optional[TrackStartCallback] = None,
        on_track_done:     Optional[TrackDoneCallback]  = None,
        on_progress:       Optional[ProgressCallback]   = None,
        workers:           Optional[int]     = None,
        external_downloader: Optional[str]   = None,
        write_m3u:         bool              = False,
    ) -> PlaylistDownloadResult:
        cfg = self.config
        _, playlist_id = self.resolve_id(url_or_id, "playlist")
        q      = quality or _parse_quality(cfg.download.quality)
        dest   = dest_dir or Path(cfg.download.output_dir)
        tmpl   = template or cfg.naming.playlist
        n_work = workers  or cfg.download.max_workers
        ext_dl = external_downloader or cfg.download.external_downloader

        pl    = self.client.get_playlist(playlist_id, limit=1)
        total = pl.tracks_count
        agg   = PlaylistDownloadResult(name=pl.name)
        m3u:  list[str] = ["#EXTM3U"]

        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=n_work,
            external_downloader=ext_dl,
            naming_template=tmpl,
            dev=self._dev,
        ) as dl:
            for i, summary in enumerate(
                self.client.iter_playlist_track_summaries(playlist_id), 1
            ):
                if on_track_start:
                    on_track_start(summary.title, i, total)

                try:
                    track_obj = self.client.get_track(str(summary.id))
                    album_obj = None
                    if track_obj.album:
                        try:
                            album_obj = self.client.get_album(track_obj.album.id)
                        except Exception:
                            pass
                    url_info = self.client.get_track_url(str(track_obj.id), quality=q)
                except (NotStreamableError, APIError) as exc:
                    agg.failed.append(f"{summary.title} — {exc}")
                    continue

                try:
                    dl_result = dl.download_track(
                        track=track_obj, url_info=url_info,
                        dest_dir=dest, album=None, on_progress=on_progress,
                        playlist_name=pl.name, playlist_index=i,
                    )
                except Exception as exc:
                    agg.failed.append(f"{track_obj.display_title} — {type(exc).__name__}: {exc}")
                    continue

                tdr = self._post_download(
                    dl_result, track_obj, album_obj,
                    embed_cover=embed_cover,
                    fetch_lyrics_flag=fetch_lyrics_flag,
                    save_cover_file=save_cover_file,
                )
                agg.tracks.append(tdr)
                m3u.append(str(dl_result.path))
                if on_track_done:
                    on_track_done(tdr)

        if write_m3u and len(m3u) > 1:
            pl_dir = dest / sanitize(pl.name)
            pl_dir.mkdir(parents=True, exist_ok=True)
            (pl_dir / f"{sanitize(pl.name)}.m3u8").write_text(
                "\n".join(m3u), encoding="utf-8"
            )
        return agg

    def download_local_playlist(
        self,
        playlist_name_or_id: str,
        quality:        Optional[Quality] = None,
        dest_dir:       Optional[Path]    = None,
        template:       Optional[str]     = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done:  Optional[TrackDoneCallback]  = None,
        workers:        Optional[int]     = None,
    ) -> PlaylistDownloadResult:
        cfg = self.config
        pl  = self.store.get_playlist_by_name(playlist_name_or_id)
        if not pl:
            for c in self.store.list_playlists():
                if c["id"].startswith(playlist_name_or_id):
                    pl = c; break
        if not pl:
            raise ValueError(f"Local playlist not found: {playlist_name_or_id!r}")

        tracks = self.store.get_playlist_tracks(pl["id"])
        q      = quality or _parse_quality(cfg.download.quality)
        dest   = dest_dir or Path(cfg.download.output_dir)
        tmpl   = template or cfg.naming.playlist
        n_work = workers  or cfg.download.max_workers
        agg    = PlaylistDownloadResult(name=pl["name"])

        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=n_work,
            naming_template=tmpl,
            dev=self._dev,
        ) as dl:
            for i, t in enumerate(tracks, 1):
                if on_track_start:
                    on_track_start(t.get("title", t["track_id"]), i, len(tracks))
                try:
                    track_obj = self.client.get_track(t["track_id"])
                    url_info  = self.client.get_track_url(t["track_id"], quality=q)
                except Exception:
                    agg.failed += 1; continue
                try:
                    dl_result = dl.download_track(
                        track=track_obj, url_info=url_info,
                        dest_dir=dest, album=None,
                        playlist_name=pl["name"], playlist_index=i,
                    )
                except Exception:
                    agg.failed += 1; continue
                tdr = self._post_download(dl_result, track_obj)
                agg.tracks.append(tdr)
                if on_track_done:
                    on_track_done(tdr)
        return agg

    def download_favorites(
        self,
        fav_type:       str              = "tracks",
        quality:        Optional[Quality] = None,
        dest_dir:       Optional[Path]   = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done:  Optional[TrackDoneCallback]  = None,
        workers:        Optional[int]    = None,
    ) -> PlaylistDownloadResult:
        cfg  = self.config
        q    = quality or _parse_quality(cfg.download.quality)
        dest = dest_dir or Path(cfg.download.output_dir)
        agg  = PlaylistDownloadResult(name=f"Favorites ({fav_type})")

        if fav_type == "albums":
            for album_obj in self.client.iter_favorites(type="albums"):
                try:
                    r = self.download_album(
                        str(album_obj.id), quality=q, dest_dir=dest,
                        on_track_start=on_track_start, on_track_done=on_track_done,
                        workers=workers,
                    )
                    agg.tracks.extend(r.tracks)
                    agg.failed += r.failed
                except Exception as exc:
                    agg.failed.append(f"{album_obj.display_title} — {exc}")
            return agg

        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=workers or cfg.download.max_workers,
            naming_template=cfg.naming.single,
            dev=self._dev,
        ) as dl:
            for i, track_obj in enumerate(
                self.client.iter_favorites(type="tracks"), 1
            ):
                if on_track_start:
                    on_track_start(track_obj.display_title, i, 0)
                try:
                    url_info  = self.client.get_track_url(str(track_obj.id), quality=q)
                    dl_result = dl.download_track(
                        track=track_obj, url_info=url_info, dest_dir=dest, album=None,
                    )
                except Exception as exc:
                    agg.failed.append(f"{getattr(track_obj, 'display_title', '?')} — {exc}"); continue
                tdr = self._post_download(dl_result, track_obj)
                agg.tracks.append(tdr)
                if on_track_done:
                    on_track_done(tdr)
        return agg

    def download_purchases(
        self,
        purchase_type:  str              = "albums",
        quality:        Optional[Quality] = None,
        dest_dir:       Optional[Path]   = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done:  Optional[TrackDoneCallback]  = None,
        workers:        Optional[int]    = None,
    ) -> PlaylistDownloadResult:
        cfg  = self.config
        q    = quality or _parse_quality(cfg.download.quality)
        dest = dest_dir or Path(cfg.download.output_dir)
        agg  = PlaylistDownloadResult(name=f"Purchases ({purchase_type})")

        if purchase_type == "albums":
            for album_obj in self.client.iter_purchases(type="albums"):
                try:
                    r = self.download_album(
                        str(album_obj.id), quality=q, dest_dir=dest,
                        on_track_start=on_track_start, on_track_done=on_track_done,
                        workers=workers,
                    )
                    agg.tracks.extend(r.tracks)
                    agg.failed += r.failed
                except Exception as exc:
                    agg.failed.append(f"{album_obj.display_title} — {exc}")
            return agg

        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=workers or cfg.download.max_workers,
            naming_template=cfg.naming.single,
            dev=self._dev,
        ) as dl:
            for i, track_obj in enumerate(
                self.client.iter_purchases(type="tracks"), 1
            ):
                if on_track_start:
                    on_track_start(
                        getattr(track_obj, "display_title", str(track_obj)), i, 0
                    )
                try:
                    url_info  = self.client.get_track_url(str(track_obj.id), quality=q)
                    dl_result = dl.download_track(
                        track=track_obj, url_info=url_info, dest_dir=dest, album=None,
                    )
                except Exception as exc:
                    agg.failed.append(f"{getattr(track_obj, 'display_title', '?')} — {exc}"); continue
                tdr = self._post_download(dl_result, track_obj)
                agg.tracks.append(tdr)
                if on_track_done:
                    on_track_done(tdr)
        return agg

    # ══════════════════════════════════════════════════════════════════════
    # Favourites
    # ══════════════════════════════════════════════════════════════════════

    def add_favorite(
        self, entity_id: str, entity_type: str, remote: bool = False,
    ) -> None:
        title = artist = extra = ""
        try:
            if entity_type == "track":
                obj    = self.client.get_track(entity_id)
                title  = obj.display_title
                artist = obj.performer.name if obj.performer else ""
                extra  = obj.album.title if obj.album else ""
            elif entity_type == "album":
                obj    = self.client.get_album(entity_id)
                title  = obj.display_title
                artist = obj.artist.name if obj.artist else ""
                extra  = obj.genre.name if obj.genre else ""
            elif entity_type == "artist":
                obj   = self.client.get_artist(entity_id, extras="")
                title = obj.name
        except Exception:
            pass
        self.store.add_favorite(
            entity_id, entity_type, title=title, artist=artist, extra=extra,
        )
        if remote:
            kwargs: dict[str, Any] = {}
            if entity_type == "track":
                kwargs = {"track_ids":  [entity_id]}
            elif entity_type == "album":
                kwargs = {"album_ids":  [entity_id]}
            elif entity_type == "artist":
                kwargs = {"artist_ids": [entity_id]}
            self.client.add_favorite(**kwargs)

    def remove_favorite(
        self, entity_id: str, entity_type: str, remote: bool = False,
    ) -> bool:
        removed = self.store.remove_favorite(entity_id, entity_type)
        if remote:
            kwargs: dict[str, Any] = {}
            if entity_type == "track":
                kwargs = {"track_ids":  [entity_id]}
            elif entity_type == "album":
                kwargs = {"album_ids":  [entity_id]}
            elif entity_type == "artist":
                kwargs = {"artist_ids": [entity_id]}
            self.client.remove_favorite(**kwargs)
        return removed

    def sync_favorites(
        self, fav_type: Optional[str] = None, clear: bool = False,
    ) -> dict[str, int]:
        if self.client.is_pool_mode:
            raise PoolModeError("sync_favorites() requires a personal session.")
        types    = [fav_type] if fav_type else ["track", "album", "artist"]
        result   = {}
        fav      = self.client.get_user_favorites()
        type_map = {"track": fav.tracks, "album": fav.albums, "artist": fav.artists}
        for t in types:
            page = type_map.get(t)
            if not page or not page.items:
                result[t] = 0; continue
            raw = [dataclasses.asdict(i) for i in page.items]
            result[t] = self.store.sync_favorites_from_api(raw, t, clear_first=clear)
        return result

    # ══════════════════════════════════════════════════════════════════════
    # Remote playlist management
    # ══════════════════════════════════════════════════════════════════════

    def clone_playlist(self, url_or_id: str, name: Optional[str] = None) -> str:
        _, playlist_id = self.resolve_id(url_or_id, "playlist")
        pl         = self.client.get_playlist(playlist_id, limit=1)
        local_name = name or pl.name
        if self.store.get_playlist_by_name(local_name):
            local_name = f"{local_name} (cloned)"
        local_id = self.store.create_playlist(local_name, pl.description or "")
        for i, t in enumerate(
            self.client.iter_playlist_track_summaries(playlist_id)
        ):
            self.store.add_track_to_playlist(
                local_id, str(t.id),
                title=t.title,
                artist=t.performer.name if t.performer else "",
                album=t.album.title if t.album else "",
                duration=t.duration or 0,
                isrc=getattr(t, "isrc", "") or "",
                position=i,
            )
        return local_id

    def share_playlist(
        self,
        playlist_name_or_id: str,
        output: Optional[Path] = None,
        author: str = "",
    ) -> Path:
        pl = self._find_local_playlist(playlist_name_or_id)
        if not pl:
            raise ValueError(f"Local playlist not found: {playlist_name_or_id!r}")
        tracks = self.store.get_playlist_tracks(pl["id"])
        lpl    = playlist_from_store_tracks(
            name=pl["name"], tracks=tracks,
            description=pl.get("description", ""), author=author,
        )
        if output is None:
            self.config.local_data.playlists_dir.mkdir(parents=True, exist_ok=True)
            output = (
                self.config.local_data.playlists_dir
                / f"{sanitize(pl['name'])}.toml"
            )
        save_playlist(lpl, output)
        return output

    def import_playlist(self, file: Path, overwrite: bool = False) -> str:
        pl_id = import_playlist_toml(self.store, file, overwrite=overwrite)
        if pl_id is not None:
            return pl_id
        lpl      = load_playlist(file)
        new_name = f"{lpl.name} (imported)"
        local_id = self.store.create_playlist(new_name, lpl.description)
        for i, t in enumerate(lpl.tracks):
            self.store.add_track_to_playlist(
                local_id, t.id,
                title=t.title, artist=t.artist, album=t.album,
                duration=t.duration, isrc=t.isrc, position=i,
            )
        return local_id

    def create_remote_playlist(
        self,
        name:               str,
        description:        str  = "",
        is_public:          bool = False,
        is_collaborative:   bool = False,
        also_save_locally:  bool = True,
    ) -> Playlist:
        pl = self.client.create_remote_playlist(
            name=name, description=description,
            is_public=is_public, is_collaborative=is_collaborative,
        )
        if also_save_locally:
            self.store.create_playlist(name, description)
        return pl

    def update_remote_playlist(
        self, playlist_id: str | int, **kwargs: Any,
    ) -> Playlist:
        return self.client.update_remote_playlist(
            playlist_id=playlist_id, **kwargs,
        )

    def delete_remote_playlist(self, playlist_id: str | int) -> None:
        self.client.delete_remote_playlist(playlist_id)

    def add_tracks_to_remote_playlist(
        self, playlist_id: str | int, track_ids: list[str | int],
        no_duplicate: bool = True,
    ) -> None:
        self.client.add_tracks_to_remote_playlist(
            playlist_id=playlist_id, track_ids=track_ids, no_duplicate=no_duplicate,
        )

    def remove_tracks_from_remote_playlist(
        self, playlist_id: str | int, playlist_track_ids: list[int],
    ) -> None:
        self.client.remove_tracks_from_remote_playlist(
            playlist_id=playlist_id, playlist_track_ids=playlist_track_ids,
        )

    def follow_playlist(self, playlist_id: str | int) -> None:
        self.client.subscribe_to_playlist(playlist_id)

    def unfollow_playlist(self, playlist_id: str | int) -> None:
        self.client.unsubscribe_from_playlist(playlist_id)

    # ══════════════════════════════════════════════════════════════════════
    # Account
    # ══════════════════════════════════════════════════════════════════════

    def get_profile(self) -> UserProfile:
        return self.client.get_user_info()

    def update_profile(self, **kwargs: Any) -> UserProfile:
        return self.client.update_user(**kwargs)

    def change_password(
        self, current_password: str, new_password: str,
    ) -> None:
        self.client.update_password(current_password, new_password)

    # ══════════════════════════════════════════════════════════════════════
    # Export / backup
    # ══════════════════════════════════════════════════════════════════════

    def backup(self, output: Optional[Path] = None) -> Path:
        if output is None:
            from datetime import datetime, timezone
            self.config.local_data.exports_dir.mkdir(parents=True, exist_ok=True)
            date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            output = self.config.local_data.exports_dir / f"qobuz-backup-{date}.tar.gz"
        return backup_to_tar(
            store=self.store,
            config_path=_CONFIG_PATH,
            playlists_dir=self.config.local_data.playlists_dir,
            output_path=output,
        )

    def export_favorites(
        self, output: Optional[Path] = None, fav_type: Optional[str] = None,
    ) -> Path:
        if output is None:
            from datetime import datetime, timezone
            self.config.local_data.exports_dir.mkdir(parents=True, exist_ok=True)
            date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            output = self.config.local_data.exports_dir / f"favorites-{date}.toml"
        return export_favorites_toml(self.store, output, type=fav_type)

    def import_favorites(self, file: Path, merge: bool = True) -> int:
        return import_favorites_toml(self.store, Path(file), merge=merge)

    def restore(
        self,
        archive_path:      Path,
        restore_favorites: bool = True,
        restore_playlists: bool = True,
        restore_db:        bool = False,
        merge:             bool = True,
    ) -> ImportResult:
        return restore_from_tar(
            self.store, Path(archive_path),
            restore_favorites=restore_favorites,
            restore_playlists=restore_playlists,
            restore_db=restore_db,
            merge=merge,
            playlists_dir=(
                self.config.local_data.playlists_dir if restore_playlists else None
            ),
        )

    # ══════════════════════════════════════════════════════════════════════
    # Private helpers
    # ══════════════════════════════════════════════════════════════════════

    def _find_local_playlist(self, name_or_id: str) -> Optional[dict]:
        pl = self.store.get_playlist_by_name(name_or_id)
        if pl:
            return pl
        for c in self.store.list_playlists():
            if c["id"].startswith(name_or_id):
                return c
        return None

    def _save_cover_file(
        self,
        folder: Path,
        album: Optional[Album] = None,
        track: Optional[Track] = None,
        track_stem: Optional[str] = None,
    ) -> None:
        """
        Save cover art as a JPEG file alongside the audio.

        For album downloads (album is not None):
            Writes  <folder>/cover.jpg
            All tracks in the folder share the same release, so one
            canonical cover file is the right behaviour.

        For single-track downloads (album is None, track_stem provided):
            Writes  <folder>/<track_stem>.jpg
            Multiple singles may land in the same dest_dir, so we name
            the cover after the audio file to avoid collisions:
                "01. One More Time.flac"  →  "01. One More Time.jpg"

        Image URL resolution order:
            1. album.image (full Album object, highest quality)
            2. track.album.image (TrackAlbum embedded in the Track)
        """
        url: Optional[str] = None

        if album and album.image:
            url = album.image.large or album.image.small
        elif track and track.album and track.album.image:
            img = track.album.image
            if hasattr(img, "large"):
                url = img.large or img.small
            elif isinstance(img, dict):
                url = img.get("large") or img.get("small")

        if not url:
            return

        filename = "cover.jpg" if track_stem is None else f"{track_stem}.jpg"
        cover_path = folder / filename

        if cover_path.exists():
            return

        try:
            import httpx
            with httpx.Client(follow_redirects=True, timeout=30) as c:
                r = c.get(url)
                r.raise_for_status()
                cover_path.write_bytes(r.content)
        except Exception:
            pass

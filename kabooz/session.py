# kabooz/session.py
"""
QobuzSession — high-level facade over QobuzClient + LocalStore.

This is the primary library entry point for programmatic use. All
operations that the CLI exposes are available here as regular Python
method calls, with no dependency on typer or any CLI framework.

Usage:
    from kabooz import QobuzSession, Quality

    session = QobuzSession.from_config()          # load ~/.config/qobuz/config.toml
    session.download_track("12345")
    session.download_album("https://open.qobuz.com/album/abc123")
    session.clone_playlist("8898080", name="My Copy")
    session.add_favorite("12345", "track", remote=True)
    session.backup()
    session.restore("qobuz-backup-2026-03-15.tar.gz")
    session.import_favorites("favorites-2026-03-15.toml")
    session.import_playlist("evening-classical.toml")

    # Iterate without downloading:
    for track in session.client.iter_playlist_track_summaries("8898080"):
        print(track.title)

All methods return typed objects or raise QobuzError subclasses —
never sys.exit(), never print to stdout (use the callbacks for progress).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generator, Optional

from .client import QobuzClient
from .config import QobuzConfig, _CONFIG_PATH, _SESSION_PATH, load_config
from .download.downloader import Downloader, DownloadResult
from .download.lyrics import fetch_lyrics
from .download.musicbrainz import lookup_isrc, apply_mb_tags
from .download.naming import sanitize
from .download.tagger import Tagger
from .exceptions import (
    APIError, NotStreamableError, PoolModeError,
    TokenExpiredError, InvalidCredentialsError, NoAuthError,
)
from .local.store import LocalStore
from .local.playlist import (
    LocalPlaylist, LocalPlaylistTrack,
    load_playlist, save_playlist, playlist_from_store_tracks,
)
from .local.export import (
    backup_to_tar,
    export_favorites_toml,
    import_favorites_toml,
    import_playlist_toml,
    restore_from_tar,
    ImportResult,
)
from .models.album import Album
from .models.track import Track
from .models.playlist import PlaylistTrack
from .models.favorites import UserFavorites
from .quality import Quality
from .url import parse_url


# ── Result types ───────────────────────────────────────────────────────────

@dataclass
class TrackDownloadResult:
    """Result of a single track download + post-processing."""
    download: DownloadResult
    track: Track
    album: Optional[Album] = None
    tagged: bool = False
    lyrics_found: bool = False
    mb_enriched: bool = False


@dataclass
class AlbumDownloadResult:
    """Aggregate result of downloading a full album."""
    album: Album
    tracks: list[TrackDownloadResult] = field(default_factory=list)
    goodies_ok: int = 0
    goodies_failed: int = 0

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.tracks if not r.download.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.tracks if r.download.skipped)

    @property
    def failed(self) -> int:
        return len(self.tracks) == 0 and 0 or 0


@dataclass
class PlaylistDownloadResult:
    """Aggregate result of downloading a playlist."""
    name: str
    tracks: list[TrackDownloadResult] = field(default_factory=list)
    failed: int = 0

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.tracks if not r.download.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.tracks if r.download.skipped)


# ── Progress callback type ─────────────────────────────────────────────────

ProgressCallback  = Callable[[int, int], None]
TrackStartCallback = Callable[[str, int, int], None]
TrackDoneCallback  = Callable[[TrackDownloadResult], None]


# ── Session ────────────────────────────────────────────────────────────────

class QobuzSession:
    """
    High-level Qobuz session combining API access, local storage,
    download, and tagging into a single programmable interface.

    All CLI commands are thin wrappers around methods of this class.
    Everything here is usable from library code with no CLI dependency.
    """

    def __init__(
        self,
        client: QobuzClient,
        config: QobuzConfig,
        store: Optional[LocalStore] = None,
        dev: bool = False,
    ) -> None:
        self.client = client
        self.config = config
        self.store  = store or LocalStore(config.local_data.db_path)
        self._dev   = dev

    # ── Factory methods ────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        config_path: Path = _CONFIG_PATH,
        dev: bool = False,
    ) -> QobuzSession:
        """
        Create a session from the standard config file.
        Loads credentials and session token automatically.

        Raises:
            ConfigError       — if the config file is invalid.
            FileNotFoundError — if the session file is missing (not logged in).
        """
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
    ) -> QobuzSession:
        """
        Wrap an existing QobuzClient. Useful when you've already
        authenticated manually.
        """
        cfg = config or load_config()
        return cls(client=client, config=cfg)

    # ── URL / ID resolution ────────────────────────────────────────────────

    def resolve_id(self, url_or_id: str, expected_type: Optional[str] = None) -> tuple[str, str]:
        """
        Parse a Qobuz URL or bare ID.
        Returns (entity_type, entity_id).
        For bare IDs, entity_type is expected_type or 'unknown'.
        """
        if url_or_id.startswith("http"):
            entity_type, entity_id = parse_url(url_or_id)
            if expected_type and entity_type != expected_type:
                raise ValueError(
                    f"Expected a {expected_type} URL but got a {entity_type} URL."
                )
            return entity_type, entity_id
        return expected_type or "unknown", url_or_id

    # ── Post-download pipeline ─────────────────────────────────────────────

    def post_download(
        self,
        result: DownloadResult,
        track: Track,
        album: Optional[Album] = None,
        embed_cover: Optional[bool] = None,
        fetch_lyrics_flag: Optional[bool] = None,
        save_cover_file: Optional[bool] = None,
    ) -> TrackDownloadResult:
        """
        Run the full post-download pipeline on a downloaded file:
        tagging, lyrics, MusicBrainz enrichment, cover art, history log.

        All parameters default to config values when None.
        Returns a TrackDownloadResult with flags indicating what ran.
        """
        cfg = self.config
        t   = cfg.tagging
        mb  = cfg.musicbrainz

        _embed_cover  = embed_cover        if embed_cover        is not None else t.embed_cover
        _fetch_lyrics = fetch_lyrics_flag  if fetch_lyrics_flag  is not None else t.fetch_lyrics
        _save_cover   = save_cover_file    if save_cover_file     is not None else t.save_cover_file

        tagged = lyrics_found = mb_enriched = False

        if not t.enabled:
            return TrackDownloadResult(result, track, album, tagged, lyrics_found, mb_enriched)

        lyrics_result = None
        if _fetch_lyrics:
            artist = (
                track.performer.name if track.performer
                else (album.artist.name if album and album.artist else "")
            )
            lyrics_result = fetch_lyrics(
                title=track.title,
                artist=artist,
                album=album.title if album else None,
                duration=track.duration,
            )
            lyrics_found = lyrics_result.found if lyrics_result else False

        if not result.dev_stub:
            tagger = Tagger()
            tagger.tag(
                path=result.path,
                track=track,
                album=album,
                lyrics=lyrics_result,
                embed_cover=_embed_cover,
            )
            tagged = True

            if _save_cover and album and album.image:
                self._save_cover_file(result.path.parent, album)

        if mb.enabled and track.isrc and not result.dev_stub:
            mb_result = lookup_isrc(track.isrc)
            if mb_result.found:
                apply_mb_tags(result.path, mb_result)
                mb_enriched = True

        if cfg.local_data.track_history and not result.dev_stub:
            try:
                self.store.log_play(
                    track_id=str(track.id),
                    title=track.display_title,
                    artist=track.performer.name if track.performer else "",
                    album=album.title if album else (
                        track.album.title if track.album else ""
                    ),
                )
            except Exception:
                pass

        return TrackDownloadResult(result, track, album, tagged, lyrics_found, mb_enriched)

    # ── Track download ─────────────────────────────────────────────────────

    def download_track(
        self,
        url_or_id: str,
        quality: Optional[Quality] = None,
        dest_dir: Optional[Path] = None,
        template: Optional[str] = None,
        embed_cover: Optional[bool] = None,
        fetch_lyrics_flag: Optional[bool] = None,
        save_cover_file: Optional[bool] = None,
        on_progress: Optional[ProgressCallback] = None,
        workers: Optional[int] = None,
        external_downloader: Optional[str] = None,
    ) -> TrackDownloadResult:
        """Download a single track and run the full post-processing pipeline."""
        cfg = self.config
        _, track_id = self.resolve_id(url_or_id, "track")

        q      = quality or Quality[cfg.download.quality.upper()]
        dest   = dest_dir or Path(cfg.download.output_dir)
        tmpl   = template or cfg.naming.single
        n_work = workers or cfg.download.max_workers
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
                track=track_obj,
                url_info=url_info,
                dest_dir=dest,
                album=None,
                on_progress=on_progress,
            )

        return self.post_download(
            result, track_obj, None,
            embed_cover=embed_cover,
            fetch_lyrics_flag=fetch_lyrics_flag,
            save_cover_file=save_cover_file,
        )

    # ── Album download ─────────────────────────────────────────────────────

    def download_album(
        self,
        url_or_id: str,
        quality: Optional[Quality] = None,
        dest_dir: Optional[Path] = None,
        template: Optional[str] = None,
        embed_cover: Optional[bool] = None,
        fetch_lyrics_flag: Optional[bool] = None,
        save_cover_file: Optional[bool] = None,
        download_goodies: bool = True,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done: Optional[TrackDoneCallback] = None,
        on_progress: Optional[ProgressCallback] = None,
        workers: Optional[int] = None,
        external_downloader: Optional[str] = None,
    ) -> AlbumDownloadResult:
        """Download a full album and run post-processing on each track."""
        cfg = self.config
        _, album_id = self.resolve_id(url_or_id, "album")

        q      = quality or Quality[cfg.download.quality.upper()]
        dest   = dest_dir or Path(cfg.download.output_dir)
        n_work = workers or cfg.download.max_workers
        ext_dl = external_downloader or cfg.download.external_downloader

        album_obj = self.client.get_album(album_id)

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
        total         = album_obj.tracks.total if album_obj.tracks else 0

        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=n_work,
            external_downloader=ext_dl,
            naming_template=tmpl,
            dev=self._dev,
        ) as dl:
            for i, summary in enumerate(
                self.client.iter_album_tracks(album_id) if total > 50
                else (album_obj.tracks.items if album_obj.tracks else []),
                1,
            ):
                if on_track_start:
                    title = getattr(summary, "display_title", getattr(summary, "title", ""))
                    on_track_start(title, i, total)

                try:
                    track_obj = self.client.get_track(str(summary.id))
                    url_info  = self.client.get_track_url(str(track_obj.id), quality=q)
                except (NotStreamableError, APIError):
                    continue

                try:
                    dl_result = dl.download_track(
                        track=track_obj,
                        url_info=url_info,
                        dest_dir=dest,
                        album=album_obj,
                        on_progress=on_progress,
                    )
                except Exception:
                    continue

                track_results.append(dl_result)
                tdr = self.post_download(
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

    # ── Playlist download ──────────────────────────────────────────────────

    def download_playlist(
        self,
        url_or_id: str,
        quality: Optional[Quality] = None,
        dest_dir: Optional[Path] = None,
        template: Optional[str] = None,
        embed_cover: Optional[bool] = None,
        fetch_lyrics_flag: Optional[bool] = None,
        save_cover_file: Optional[bool] = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done: Optional[TrackDoneCallback] = None,
        on_progress: Optional[ProgressCallback] = None,
        workers: Optional[int] = None,
        external_downloader: Optional[str] = None,
        write_m3u: bool = False,
    ) -> PlaylistDownloadResult:
        """Download a full Qobuz playlist (all pages, pagination-proof)."""
        cfg = self.config
        _, playlist_id = self.resolve_id(url_or_id, "playlist")

        q      = quality or Quality[cfg.download.quality.upper()]
        dest   = dest_dir or Path(cfg.download.output_dir)
        tmpl   = template or cfg.naming.playlist
        n_work = workers or cfg.download.max_workers
        ext_dl = external_downloader or cfg.download.external_downloader

        pl    = self.client.get_playlist(playlist_id, limit=1)
        total = pl.tracks_count
        agg   = PlaylistDownloadResult(name=pl.name)
        m3u_lines: list[str] = ["#EXTM3U"]

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
                except (NotStreamableError, APIError):
                    agg.failed += 1
                    continue

                try:
                    dl_result = dl.download_track(
                        track=track_obj,
                        url_info=url_info,
                        dest_dir=dest,
                        album=None,
                        on_progress=on_progress,
                        playlist_name=pl.name,
                        playlist_index=i,
                    )
                except Exception:
                    agg.failed += 1
                    continue

                tdr = self.post_download(
                    dl_result, track_obj, album_obj,
                    embed_cover=embed_cover,
                    fetch_lyrics_flag=fetch_lyrics_flag,
                    save_cover_file=save_cover_file,
                )
                agg.tracks.append(tdr)
                m3u_lines.append(str(dl_result.path))
                if on_track_done:
                    on_track_done(tdr)

        if write_m3u and len(m3u_lines) > 1:
            pl_dir = dest / sanitize(pl.name)
            pl_dir.mkdir(parents=True, exist_ok=True)
            m3u_path = pl_dir / f"{sanitize(pl.name)}.m3u8"
            m3u_path.write_text("\n".join(m3u_lines), encoding="utf-8")

        return agg

    def download_local_playlist(
        self,
        playlist_name_or_id: str,
        quality: Optional[Quality] = None,
        dest_dir: Optional[Path] = None,
        template: Optional[str] = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done: Optional[TrackDoneCallback] = None,
        workers: Optional[int] = None,
    ) -> PlaylistDownloadResult:
        """Download all tracks in a local (SQLite) playlist."""
        cfg = self.config

        pl = self.store.get_playlist_by_name(playlist_name_or_id)
        if not pl:
            for candidate in self.store.list_playlists():
                if candidate["id"].startswith(playlist_name_or_id):
                    pl = candidate
                    break
        if not pl:
            raise ValueError(f"Local playlist not found: {playlist_name_or_id!r}")

        tracks = self.store.get_playlist_tracks(pl["id"])
        q      = quality or Quality[cfg.download.quality.upper()]
        dest   = dest_dir or Path(cfg.download.output_dir)
        tmpl   = template or cfg.naming.playlist
        n_work = workers or cfg.download.max_workers
        total  = len(tracks)
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
                    on_track_start(t.get("title", t["track_id"]), i, total)
                try:
                    track_obj = self.client.get_track(t["track_id"])
                    url_info  = self.client.get_track_url(t["track_id"], quality=q)
                except Exception:
                    agg.failed += 1
                    continue
                try:
                    dl_result = dl.download_track(
                        track=track_obj, url_info=url_info,
                        dest_dir=dest, album=None,
                        playlist_name=pl["name"], playlist_index=i,
                    )
                except Exception:
                    agg.failed += 1
                    continue

                tdr = self.post_download(dl_result, track_obj)
                agg.tracks.append(tdr)
                if on_track_done:
                    on_track_done(tdr)

        return agg

    # ── Playlist management ────────────────────────────────────────────────

    def clone_playlist(
        self,
        url_or_id: str,
        name: Optional[str] = None,
    ) -> str:
        """
        Clone a Qobuz playlist into the local store.
        Returns the new local playlist ID.
        Pagination-proof: fetches all pages regardless of playlist size.
        """
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
                local_id,
                str(t.id),
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
        """
        Export a local playlist as a shareable TOML file.
        Returns the path of the written file.
        """
        pl = self.store.get_playlist_by_name(playlist_name_or_id)
        if not pl:
            for candidate in self.store.list_playlists():
                if candidate["id"].startswith(playlist_name_or_id):
                    pl = candidate
                    break
        if not pl:
            raise ValueError(f"Local playlist not found: {playlist_name_or_id!r}")

        tracks = self.store.get_playlist_tracks(pl["id"])
        lpl    = playlist_from_store_tracks(
            name=pl["name"],
            tracks=tracks,
            description=pl.get("description", ""),
            author=author,
        )

        if output is None:
            self.config.local_data.playlists_dir.mkdir(parents=True, exist_ok=True)
            output = self.config.local_data.playlists_dir / f"{sanitize(pl['name'])}.toml"

        save_playlist(lpl, output)
        return output

    def import_playlist(
        self,
        file: Path,
        overwrite: bool = False,
    ) -> str:
        """
        Import a shared TOML playlist file into the local store.

        Parameters:
            file:      Path to a qobuz-playlist TOML file.
            overwrite: If True, replace any existing playlist with the same
                       name. If False (default), append " (imported)" to the
                       name when a conflict exists.

        Returns the new local playlist ID.
        Raises ValueError if the file is not a valid qobuz-playlist.
        """
        pl_id = import_playlist_toml(self.store, file, overwrite=overwrite)
        if pl_id is not None:
            return pl_id

        # Playlist already existed and overwrite=False: use the " (imported)"
        # suffix strategy so the call always returns a usable ID.
        lpl       = load_playlist(file)
        new_name  = f"{lpl.name} (imported)"
        local_id  = self.store.create_playlist(new_name, lpl.description)
        for i, t in enumerate(lpl.tracks):
            self.store.add_track_to_playlist(
                local_id, t.id,
                title=t.title, artist=t.artist, album=t.album,
                duration=t.duration, isrc=t.isrc,
                position=i,
            )
        return local_id

    # ── Favourites ─────────────────────────────────────────────────────────

    def add_favorite(
        self,
        id: str,
        type: str,
        remote: bool = False,
    ) -> None:
        """
        Add a track, album, or artist to local favorites.

        Parameters:
            id:     Qobuz entity ID.
            type:   'track', 'album', or 'artist'.
            remote: Also add to the Qobuz account (personal session only).
                    Raises PoolModeError if client is in pool mode.
        """
        title = artist = extra = ""
        try:
            if type == "track":
                obj    = self.client.get_track(id)
                title  = obj.display_title
                artist = obj.performer.name if obj.performer else ""
                extra  = obj.album.title if obj.album else ""
            elif type == "album":
                obj    = self.client.get_album(id)
                title  = obj.display_title
                artist = obj.artist.name if obj.artist else ""
                extra  = obj.genre.name if obj.genre else ""
            elif type == "artist":
                obj    = self.client.get_artist(id, extras="")
                title  = obj.name
        except Exception:
            pass

        self.store.add_favorite(id, type, title=title, artist=artist, extra=extra)

        if remote:
            kwargs: dict[str, Any] = {}
            if type == "track":
                kwargs = {"track_ids": [id]}
            elif type == "album":
                kwargs = {"album_ids": [id]}
            elif type == "artist":
                kwargs = {"artist_ids": [id]}
            self.client.add_favorite(**kwargs)

    def remove_favorite(
        self,
        id: str,
        type: str,
        remote: bool = False,
    ) -> bool:
        """
        Remove a track, album, or artist from local favorites.
        Returns True if removed, False if it wasn't present.
        """
        removed = self.store.remove_favorite(id, type)
        if remote:
            kwargs: dict[str, Any] = {}
            if type == "track":
                kwargs = {"track_ids": [id]}
            elif type == "album":
                kwargs = {"album_ids": [id]}
            elif type == "artist":
                kwargs = {"artist_ids": [id]}
            self.client.remove_favorite(**kwargs)
        return removed

    def sync_favorites(
        self,
        type: Optional[str] = None,
        clear: bool = False,
    ) -> dict[str, int]:
        """
        Pull favorites from the Qobuz account into the local store.
        Returns a dict of {type: count} for items synced.
        Raises PoolModeError if client is in pool mode.
        """
        if self.client.is_pool_mode:
            raise PoolModeError(
                "sync_favorites() requires a personal session. "
                "Pool mode clients don't have a personal account to sync from."
            )

        types  = [type] if type else ["track", "album", "artist"]
        result = {}

        fav      = self.client.get_user_favorites()
        type_map = {"track": fav.tracks, "album": fav.albums, "artist": fav.artists}

        for t in types:
            page = type_map.get(t)
            if not page or not page.items:
                result[t] = 0
                continue
            raw = [dataclasses.asdict(i) for i in page.items]
            n   = self.store.sync_favorites_from_api(raw, t, clear_first=clear)
            result[t] = n

        return result

    # ── Export / backup ────────────────────────────────────────────────────

    def backup(self, output: Optional[Path] = None) -> Path:
        """
        Create a full .tar.gz backup of the local library.
        Returns the path of the written archive.
        """
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
        self,
        output: Optional[Path] = None,
        type: Optional[str] = None,
    ) -> Path:
        """
        Export local favorites to a TOML file.
        Returns the path of the written file.
        """
        if output is None:
            from datetime import datetime, timezone
            self.config.local_data.exports_dir.mkdir(parents=True, exist_ok=True)
            date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            output = self.config.local_data.exports_dir / f"favorites-{date}.toml"

        return export_favorites_toml(self.store, output, type=type)

    # ── Import / restore ───────────────────────────────────────────────────

    def import_favorites(
        self,
        file: Path,
        merge: bool = True,
    ) -> int:
        """
        Import favorites from a TOML file produced by export_favorites().

        Parameters:
            file:  Path to a favorites TOML export file.
            merge: If True (default), upsert — existing favorites not in the
                   file are preserved.  If False, each type present in the
                   file is cleared before inserting.

        Returns the number of favorites imported.
        Raises FileNotFoundError if the file does not exist.
        Raises ValueError if the file is not a valid favorites export.
        """
        return import_favorites_toml(self.store, Path(file), merge=merge)

    def restore(
        self,
        archive_path: Path,
        *,
        restore_favorites: bool = True,
        restore_playlists: bool = True,
        restore_db: bool = False,
        merge: bool = True,
    ) -> ImportResult:
        """
        Restore from a backup archive created by backup().

        By default only favorites and playlists are restored (merged into
        the live store), so existing data is not destroyed.  Pass
        restore_db=True for a full atomic database replacement — the store
        should be re-opened after that call.

        Parameters:
            archive_path:      Path to the .tar.gz backup archive.
            restore_favorites: Import favorites from the archive.
            restore_playlists: Import playlists from the archive and also
                               extract TOML files to the configured
                               playlists directory.
            restore_db:        Replace the entire library.db atomically.
            merge:             Upsert behavior for favorites and playlists
                               (True) vs. clear-then-insert (False).

        Returns an ImportResult with counts and any non-fatal errors.
        Raises FileNotFoundError if the archive does not exist.
        Raises ValueError if the archive is not a valid qobuz backup.
        """
        playlists_dir = self.config.local_data.playlists_dir if restore_playlists else None
        return restore_from_tar(
            self.store,
            Path(archive_path),
            restore_favorites=restore_favorites,
            restore_playlists=restore_playlists,
            restore_db=restore_db,
            merge=merge,
            playlists_dir=playlists_dir,
        )

    # ── Artist discography ─────────────────────────────────────────────────

    def download_artist_discography(
        self,
        url_or_id: str,
        release_type: Optional[str] = None,
        quality: Optional[Quality] = None,
        dest_dir: Optional[Path] = None,
        template: Optional[str] = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done: Optional[TrackDoneCallback] = None,
        workers: Optional[int] = None,
    ) -> list[AlbumDownloadResult]:
        """
        Download all albums in an artist's discography.

        Parameters:
            url_or_id:    Artist ID or Qobuz URL.
            release_type: Filter releases: 'album', 'live', 'compilation',
                          'epSingle', 'other', 'download', or None for all.
        """
        _, artist_id = self.resolve_id(url_or_id, "artist")
        results: list[AlbumDownloadResult] = []

        for release in self.client.iter_releases(
            artist_id, release_type=release_type
        ):
            if not release.id:
                continue
            try:
                agg = self.download_album(
                    release.id,
                    quality=quality,
                    dest_dir=dest_dir,
                    template=template,
                    on_track_start=on_track_start,
                    on_track_done=on_track_done,
                    workers=workers,
                )
                results.append(agg)
            except Exception:
                continue

        return results

    # ── Favorites download ─────────────────────────────────────────────────

    def download_favorites(
        self,
        type: str = "tracks",
        quality: Optional[Quality] = None,
        dest_dir: Optional[Path] = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done: Optional[TrackDoneCallback] = None,
        workers: Optional[int] = None,
    ) -> PlaylistDownloadResult:
        """Download all favorited tracks or albums."""
        cfg  = self.config
        q    = quality or Quality[cfg.download.quality.upper()]
        dest = dest_dir or Path(cfg.download.output_dir)
        agg  = PlaylistDownloadResult(name=f"Favorites ({type})")

        if type == "albums":
            for album_obj in self.client.iter_favorites(type="albums"):
                try:
                    result = self.download_album(
                        str(album_obj.id),
                        quality=quality,
                        dest_dir=dest_dir,
                        on_track_start=on_track_start,
                        on_track_done=on_track_done,
                        workers=workers,
                    )
                    agg.tracks.extend(result.tracks)
                except Exception:
                    agg.failed += 1
            return agg

        n_work = workers or cfg.download.max_workers
        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=n_work,
            naming_template=cfg.naming.single,
            dev=self._dev,
        ) as dl:
            for i, track_obj in enumerate(
                self.client.iter_favorites(type="tracks"), 1
            ):
                if on_track_start:
                    on_track_start(track_obj.display_title, i, 0)
                try:
                    url_info = self.client.get_track_url(str(track_obj.id), quality=q)
                    dl_result = dl.download_track(
                        track=track_obj, url_info=url_info,
                        dest_dir=dest, album=None,
                    )
                except Exception:
                    agg.failed += 1
                    continue

                tdr = self.post_download(dl_result, track_obj)
                agg.tracks.append(tdr)
                if on_track_done:
                    on_track_done(tdr)

        return agg

    # ── Purchases download ─────────────────────────────────────────────────

    def download_purchases(
        self,
        type: str = "albums",
        quality: Optional[Quality] = None,
        dest_dir: Optional[Path] = None,
        on_track_start: Optional[TrackStartCallback] = None,
        on_track_done: Optional[TrackDoneCallback] = None,
        workers: Optional[int] = None,
    ) -> PlaylistDownloadResult:
        """Download all purchased albums or tracks."""
        cfg  = self.config
        q    = quality or Quality[cfg.download.quality.upper()]
        dest = dest_dir or Path(cfg.download.output_dir)
        agg  = PlaylistDownloadResult(name=f"Purchases ({type})")

        if type == "albums":
            for album_obj in self.client.iter_purchases(type="albums"):
                try:
                    result = self.download_album(
                        str(album_obj.id),
                        quality=quality,
                        dest_dir=dest_dir,
                        on_track_start=on_track_start,
                        on_track_done=on_track_done,
                        workers=workers,
                    )
                    agg.tracks.extend(result.tracks)
                except Exception:
                    agg.failed += 1
            return agg

        n_work = workers or cfg.download.max_workers
        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=n_work,
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
                    url_info = self.client.get_track_url(str(track_obj.id), quality=q)
                    dl_result = dl.download_track(
                        track=track_obj, url_info=url_info,
                        dest_dir=dest, album=None,
                    )
                except Exception:
                    agg.failed += 1
                    continue

                tdr = self.post_download(dl_result, track_obj)
                agg.tracks.append(tdr)
                if on_track_done:
                    on_track_done(tdr)

        return agg

    # ── Internals ──────────────────────────────────────────────────────────

    def _save_cover_file(self, folder: Path, album: Album) -> None:
        url = None
        if album.image:
            url = album.image.large or album.image.small
        if not url:
            return
        cover_path = folder / "cover.jpg"
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

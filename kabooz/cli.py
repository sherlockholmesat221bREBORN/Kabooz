# kabooz/cli.py
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

try:
    import typer
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.table import Table
except ImportError:
    print(
        "CLI dependencies are not installed.\n"
        "Run: pip install 'qobuz-py[cli]'",
        file=sys.stderr,
    )
    sys.exit(1)

from .client import QobuzClient
from .config import (
    QobuzConfig,
    _CONFIG_PATH,
    _SESSION_PATH,
    load_config,
    save_config,
    update_config,
)
from .download.downloader import Downloader, DownloadResult
from .download.lyrics import fetch_lyrics
from .download.musicbrainz import lookup_isrc, apply_mb_tags
from .download.naming import sanitize
from .download.tagger import Tagger
from .exceptions import (
    APIError,
    InvalidCredentialsError,
    NoAuthError,
    NotFoundError,
    NotStreamableError,
    PoolModeError,
    TokenExpiredError,
    ConfigError,
)
from .local.store import LocalStore
from .local.playlist import (
    LocalPlaylist, LocalPlaylistTrack,
    load_playlist, save_playlist,
    playlist_from_store_tracks,
)
from .local.export import backup_to_tar, export_favorites_toml
from .models.track import Track
from .models.album import Album
from .quality import Quality
from .url import parse_url

# ── Apps ───────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="qobuz",
    help="Unofficial Qobuz CLI — download tracks, albums, playlists, and more.",
    add_completion=False,
)

library_app = typer.Typer(help="Manage local and remote favourites.")
lpl_app     = typer.Typer(help="Create, manage, and share local playlists.")
export_app  = typer.Typer(help="Export and back up your library.")

app.add_typer(library_app, name="library")
app.add_typer(lpl_app,     name="lpl")
app.add_typer(export_app,  name="export")

console     = Console()
err_console = Console(stderr=True)

_dev: bool = False


@app.callback()
def main(
    dev: bool = typer.Option(
        False,
        "--dev",
        envvar="QOBUZ_DEV",
        help=(
            "Developer mode: cache API responses to ~/.cache/qobuz/ and "
            "write dev audio instead of downloading real files."
        ),
        is_eager=True,
    ),
) -> None:
    """Unofficial Qobuz CLI — download tracks, albums, playlists, and more."""
    global _dev
    _dev = dev
    if dev:
        from . import dev as _dev_module
        _dev_module.enable()
        err_console.print(
            "[yellow bold][DEV MODE][/yellow bold] "
            "API responses cached · Dev audio enabled · "
            f"Cache: [dim]{_dev_module.CACHE_DIR}[/dim]"
        )


# ── Config helpers ─────────────────────────────────────────────────────────

def _cfg() -> QobuzConfig:
    return load_config()


def _store(cfg: Optional[QobuzConfig] = None) -> LocalStore:
    cfg = cfg or _cfg()
    return LocalStore(cfg.local_data.db_path)


def _build_client(cfg: Optional[QobuzConfig] = None) -> QobuzClient:
    cfg = cfg or _cfg()
    creds = cfg.credentials

    if creds.pool:
        try:
            return QobuzClient.from_token_pool(creds.pool, dev=_dev)
        except Exception as exc:
            err_console.print(f"[red]Failed to load token pool:[/red] {exc}")
            raise typer.Exit(code=1)

    app_id     = creds.app_id
    app_secret = creds.app_secret

    if not app_id or not app_secret:
        err_console.print(
            "[red]App credentials not found.[/red]\n"
            "Run [bold]qobuz login[/bold] to configure them."
        )
        raise typer.Exit(code=1)

    if not _SESSION_PATH.exists():
        err_console.print(
            "[red]Not logged in.[/red] Run [bold]qobuz login[/bold] first."
        )
        raise typer.Exit(code=1)

    client = QobuzClient.from_credentials(
        app_id=app_id,
        app_secret=app_secret,
        dev=_dev,
    )
    try:
        client.load_session(_SESSION_PATH)
    except Exception as exc:
        err_console.print(f"[red]Failed to load session:[/red] {exc}")
        raise typer.Exit(code=1)

    return client


def _build_downloader(cfg: QobuzConfig, template: Optional[str] = None) -> Downloader:
    return Downloader(
        read_timeout        = cfg.download.read_timeout,
        connect_timeout     = cfg.download.connect_timeout,
        max_workers         = cfg.download.max_workers,
        external_downloader = cfg.download.external_downloader,
        naming_template     = template or None,
        dev                 = _dev,
    )


def _handle_auth_error(exc: Exception) -> None:
    err_console.print(
        f"[red]Authentication error:[/red] {exc}\n"
        "Run [bold]qobuz login[/bold] to refresh your session."
    )
    raise typer.Exit(code=1)


# ── Metadata helpers ───────────────────────────────────────────────────────

def _needs_tagging(path: Path, check_cover: bool = True) -> bool:
    try:
        suffix = path.suffix.lower()
        if suffix == ".flac":
            from mutagen.flac import FLAC
            audio = FLAC(path)
            return (
                not audio.get("title") or
                not audio.get("artist") or
                (check_cover and not audio.pictures)
            )
        elif suffix == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return True
            return (
                "TIT2" not in tags or
                "TPE1" not in tags or
                (check_cover and not any(k.startswith("APIC") for k in tags))
            )
    except Exception:
        return True
    return False


def _fetch_lyrics_for(track_obj: Track, album_obj: Optional[Album] = None):
    artist = (
        track_obj.performer.name if track_obj.performer
        else (album_obj.artist.name if album_obj and album_obj.artist else "")
    )
    return fetch_lyrics(
        title=track_obj.title,
        artist=artist,
        album=album_obj.title if album_obj else None,
        duration=track_obj.duration,
    )


def _post_download(
    result: DownloadResult,
    track_obj: Track,
    album_obj: Optional[Album],
    cfg: QobuzConfig,
    embed_cover: bool,
    fetch_lyrics_flag: bool,
    save_cover_file: bool,
) -> None:
    if not cfg.tagging.enabled:
        return

    lyrics_result = None
    if fetch_lyrics_flag:
        lyrics_result = _fetch_lyrics_for(track_obj, album_obj)

    if not result.dev_stub:
        tagger = Tagger()
        tagger.tag(
            path=result.path,
            track=track_obj,
            album=album_obj,
            lyrics=lyrics_result,
            embed_cover=embed_cover,
        )
        if save_cover_file and album_obj and album_obj.image:
            _save_cover_file(result.path.parent, album_obj)
    else:
        from .dev import dev_log
        dev_log("[yellow]stub file — tagging skipped (install ffmpeg)[/yellow]")

    if cfg.musicbrainz.enabled and track_obj.isrc:
        if not result.dev_stub:
            mb = lookup_isrc(track_obj.isrc)
            apply_mb_tags(result.path, mb)
        else:
            from .dev import dev_log
            dev_log(f"MusicBrainz lookup isrc={track_obj.isrc!r} (tags not written — stub)")
            lookup_isrc(track_obj.isrc)

    # Log to local history.
    if cfg.local_data.track_history and not result.dev_stub:
        try:
            store = _store(cfg)
            store.log_play(
                track_id=str(track_obj.id),
                title=track_obj.display_title,
                artist=track_obj.performer.name if track_obj.performer else "",
                album=album_obj.title if album_obj else (
                    track_obj.album.title if track_obj.album else ""
                ),
            )
        except Exception:
            pass


def _save_cover_file(folder: Path, album: Album) -> None:
    url = None
    if album.image:
        url = album.image.large or album.image.small
    if not url:
        return
    cover_path = folder / "cover.jpg"
    if cover_path.exists():
        return
    try:
        import httpx as _httpx
        with _httpx.Client(follow_redirects=True, timeout=30) as c:
            r = c.get(url)
            r.raise_for_status()
            cover_path.write_bytes(r.content)
    except Exception:
        pass


# ── Shared UI helpers ──────────────────────────────────────────────────────

def _make_progress() -> Progress:
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )


def _resolve_id(url_or_id: str, expected_type: Optional[str] = None) -> str:
    if url_or_id.startswith("http"):
        try:
            entity_type, entity_id = parse_url(url_or_id)
        except ValueError as exc:
            err_console.print(f"[red]Invalid URL:[/red] {exc}")
            raise typer.Exit(code=1)
        if expected_type and entity_type != expected_type:
            err_console.print(
                f"[red]Expected a {expected_type} URL but got a {entity_type} URL.[/red]"
            )
            raise typer.Exit(code=1)
        return entity_id
    return url_or_id


# ── Core commands ──────────────────────────────────────────────────────────

@app.command()
def login(
    username:   Optional[str] = typer.Option(None, "--username", "-u"),
    password:   Optional[str] = typer.Option(None, "--password", "-p", hide_input=True),
    token:      Optional[str] = typer.Option(None, "--token"),
    user_id:    Optional[str] = typer.Option(None, "--user-id"),
    pool:       Optional[str] = typer.Option(None, "--pool"),
    app_id:     Optional[str] = typer.Option(None, "--app-id"),
    app_secret: Optional[str] = typer.Option(None, "--app-secret", hide_input=True),
) -> None:
    """
    Authenticate with Qobuz. Three modes:

    \b
    1. Username + password (interactive):
         qobuz login
         qobuz login -u me@example.com -p secret

    \b
    2. Direct auth token:
         qobuz login --token TOKEN --user-id 12345

    \b
    3. Token pool file or URL:
         qobuz login --pool ~/.config/qobuz/pool.txt
         qobuz login --pool https://example.com/pool.txt
    """
    cfg = _cfg()

    if pool:
        try:
            QobuzClient.from_token_pool(pool)
        except Exception as exc:
            err_console.print(f"[red]Failed to load token pool:[/red] {exc}")
            raise typer.Exit(code=1)
        cfg.credentials.pool = pool
        save_config(cfg)
        console.print(f"[green]Token pool saved.[/green] Config at [bold]{_CONFIG_PATH}[/bold].")
        return

    resolved_app_id     = app_id     or os.environ.get("QOBUZ_APP_ID")     or cfg.credentials.app_id
    resolved_app_secret = app_secret or os.environ.get("QOBUZ_APP_SECRET") or cfg.credentials.app_secret

    if not resolved_app_id:
        resolved_app_id = typer.prompt("App ID")
    if not resolved_app_secret:
        resolved_app_secret = typer.prompt("App Secret", hide_input=True)

    cfg.credentials.app_id     = resolved_app_id
    cfg.credentials.app_secret = resolved_app_secret
    cfg.credentials.pool = ""

    client = QobuzClient.from_credentials(
        app_id=resolved_app_id,
        app_secret=resolved_app_secret,
    )

    if token:
        if not user_id:
            user_id = typer.prompt("User ID")
        try:
            session = client.login(token=token, user_id=user_id)
        except Exception as exc:
            err_console.print(f"[red]Login failed:[/red] {exc}")
            raise typer.Exit(code=1)
    else:
        if not username:
            username = typer.prompt("Qobuz username (email)")
        if not password:
            password = typer.prompt("Password", hide_input=True)
        try:
            session = client.login(username=username, password=password)
        except Exception as exc:
            err_console.print(f"[red]Login failed:[/red] {exc}")
            raise typer.Exit(code=1)

    save_config(cfg)
    try:
        client.save_session(_SESSION_PATH)
    except Exception as exc:
        err_console.print(f"[red]Could not save session:[/red] {exc}")
        raise typer.Exit(code=1)

    # Auto-sync favorites to local store if enabled.
    if cfg.local_data.auto_sync_favorites:
        console.print("[dim]Syncing favorites to local store…[/dim]")
        try:
            store = _store(cfg)
            fav = client.get_user_favorites()
            for t, page in [
                ("track",  fav.tracks),
                ("album",  fav.albums),
                ("artist", fav.artists),
            ]:
                if page and page.items:
                    import dataclasses
                    raw_items = [dataclasses.asdict(i) for i in page.items]
                    n = store.sync_favorites_from_api(raw_items, t)
                    console.print(f"  [dim]{n} {t}(s) synced[/dim]")
        except Exception as exc:
            err_console.print(f"[yellow]Auto-sync failed:[/yellow] {exc}")

    console.print(
        f"[green]Logged in as[/green] {session.user_email or session.user_id}. "
        f"Session saved to [bold]{_SESSION_PATH}[/bold]."
    )


@app.command()
def config(
    show: bool = typer.Option(False, "--show", help="Print current config"),
    set_: Optional[str] = typer.Option(
        None, "--set",
        help="Set a config value. Format: section.key=value"
    ),
) -> None:
    """
    View or update the configuration file.

    \b
    Examples:
        qobuz config --show
        qobuz config --set download.max_workers=4
        qobuz config --set local_data.track_history=true
        qobuz config --set local_data.auto_sync_favorites=true
        qobuz config --set local_data.data_dir=/sdcard/qobuz-data
    """
    if show:
        cfg = _cfg()
        import dataclasses
        console.print_json(
            __import__("json").dumps(dataclasses.asdict(cfg), indent=2)
        )
        return

    if set_:
        if "=" not in set_:
            err_console.print("[red]Format must be section.key=value[/red]")
            raise typer.Exit(code=1)
        key_path, _, value = set_.partition("=")
        parts = key_path.strip().split(".")
        if len(parts) != 2:
            err_console.print("[red]Key must be in section.key format[/red]")
            raise typer.Exit(code=1)
        section, key = parts

        coerced: object = value
        if value.lower() in ("true", "yes", "1"):
            coerced = True
        elif value.lower() in ("false", "no", "0"):
            coerced = False
        else:
            try:
                coerced = int(value)
            except ValueError:
                try:
                    coerced = float(value)
                except ValueError:
                    coerced = value

        try:
            update_config({section: {key: coerced}})
        except ConfigError as exc:
            err_console.print(f"[red]Invalid config value:[/red] {exc}")
            raise typer.Exit(code=1)
        console.print(f"[green]Set[/green] {section}.{key} = {coerced!r}")
        return

    console.print("Use [bold]--show[/bold] to view config or [bold]--set section.key=value[/bold] to update it.")
    console.print(f"Config file: [bold]{_CONFIG_PATH}[/bold]")


@app.command("dev-cache")
def dev_cache(
    clear: bool = typer.Option(False, "--clear", help="Delete all cached API responses"),
    show:  bool = typer.Option(False, "--show",  help="Print cache directory and stats"),
) -> None:
    """Manage the dev mode API response cache."""
    from .dev import CACHE_DIR, clear_cache

    if clear:
        n = clear_cache()
        console.print(f"[green]Cleared {n} cached response(s).[/green]")
        return

    files = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
    console.print(f"Cache directory: [bold]{CACHE_DIR}[/bold]")
    console.print(f"Cached responses: [bold]{len(files)}[/bold]")
    if files:
        total_kb = sum(f.stat().st_size for f in files) / 1024
        console.print(f"Total size: [bold]{total_kb:.1f} KB[/bold]")


@app.command()
def track(
    url_or_id:   str            = typer.Argument(..., help="Track ID or Qobuz URL"),
    output:      Optional[Path] = typer.Option(None,  "-o", "--output"),
    quality:     Optional[str]  = typer.Option(None,  "-q", "--quality"),
    lyrics:      Optional[bool] = typer.Option(None,  "--lyrics/--no-lyrics"),
    cover:       Optional[bool] = typer.Option(None,  "--cover/--no-cover"),
    save_cover:  Optional[bool] = typer.Option(None,  "--save-cover/--no-save-cover"),
    workers:     Optional[int]  = typer.Option(None,  "--workers", "-j"),
    downloader:  Optional[str]  = typer.Option(None,  "--downloader"),
    template:    Optional[str]  = typer.Option(None,  "--template"),
) -> None:
    """Download a single track."""
    cfg = _cfg()

    dest_dir    = output      or Path(cfg.download.output_dir)
    q_str       = quality     or cfg.download.quality
    embed_cover = cover       if cover      is not None else cfg.tagging.embed_cover
    save_cov    = save_cover  if save_cover is not None else cfg.tagging.save_cover_file
    do_lyrics   = lyrics      if lyrics     is not None else cfg.tagging.fetch_lyrics
    n_workers   = workers     or cfg.download.max_workers
    ext_dl      = downloader  or cfg.download.external_downloader

    try:
        q = Quality[q_str.upper()]
    except KeyError:
        err_console.print(f"[red]Unknown quality:[/red] {q_str}")
        raise typer.Exit(code=1)

    track_id = _resolve_id(url_or_id, expected_type="track")
    client   = _build_client(cfg)

    try:
        track_obj = client.get_track(track_id)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except NotFoundError:
        err_console.print(f"[red]Track not found:[/red] {track_id}")
        raise typer.Exit(code=1)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    try:
        url_info = client.get_track_url(track_id, quality=q)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except NotStreamableError as exc:
        err_console.print(f"[red]Not streamable:[/red] {exc}")
        raise typer.Exit(code=1)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[cyan]Downloading[/cyan] {track_obj.display_title}")
    tmpl = template or cfg.naming.single

    with _make_progress() as progress:
        task = progress.add_task(track_obj.title, total=None)

        def on_progress(done: int, total: int) -> None:
            progress.update(task, completed=done, total=total or None)

        with Downloader(
            read_timeout=cfg.download.read_timeout,
            connect_timeout=cfg.download.connect_timeout,
            max_workers=n_workers,
            external_downloader=ext_dl,
            naming_template=tmpl,
            dev=_dev,
        ) as dl:
            result = dl.download_track(
                track=track_obj,
                url_info=url_info,
                dest_dir=dest_dir,
                album=None,
                on_progress=on_progress,
            )

    if result.skipped and not result.dev_stub and not _needs_tagging(result.path, check_cover=embed_cover):
        console.print(f"[yellow]Skipped[/yellow] (complete + tagged): {result.path}")
        return

    if result.skipped and not result.dev_stub:
        console.print("    [yellow]File complete but missing tags — re-tagging.[/yellow]")

    _post_download(result, track_obj, None, cfg, embed_cover, do_lyrics, save_cov)
    console.print(f"[green]Done:[/green] {result.path}")


@app.command()
def album(
    url_or_id:   str            = typer.Argument(..., help="Album ID or Qobuz URL"),
    output:      Optional[Path] = typer.Option(None,  "-o", "--output"),
    quality:     Optional[str]  = typer.Option(None,  "-q", "--quality"),
    lyrics:      Optional[bool] = typer.Option(None,  "--lyrics/--no-lyrics"),
    cover:       Optional[bool] = typer.Option(None,  "--cover/--no-cover"),
    save_cover:  Optional[bool] = typer.Option(None,  "--save-cover/--no-save-cover"),
    goodies:     Optional[bool] = typer.Option(None,  "--goodies/--no-goodies"),
    workers:     Optional[int]  = typer.Option(None,  "--workers", "-j"),
    downloader:  Optional[str]  = typer.Option(None,  "--downloader"),
    template:    Optional[str]  = typer.Option(None,  "--template"),
) -> None:
    """Download a full album by ID or URL."""
    cfg = _cfg()

    dest_dir    = output      or Path(cfg.download.output_dir)
    q_str       = quality     or cfg.download.quality
    embed_cover = cover       if cover      is not None else cfg.tagging.embed_cover
    save_cov    = save_cover  if save_cover is not None else cfg.tagging.save_cover_file
    do_lyrics   = lyrics      if lyrics     is not None else cfg.tagging.fetch_lyrics
    do_goodies  = goodies     if goodies    is not None else True
    n_workers   = workers     or cfg.download.max_workers
    ext_dl      = downloader  or cfg.download.external_downloader

    try:
        q = Quality[q_str.upper()]
    except KeyError:
        err_console.print(f"[red]Unknown quality:[/red] {q_str}")
        raise typer.Exit(code=1)

    album_id = _resolve_id(url_or_id, expected_type="album")
    client   = _build_client(cfg)

    try:
        album_obj = client.get_album(album_id)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except NotFoundError:
        err_console.print(f"[red]Album not found:[/red] {album_id}")
        raise typer.Exit(code=1)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    if not album_obj.tracks or not album_obj.tracks.items:
        err_console.print("[red]Album has no tracks.[/red]")
        raise typer.Exit(code=1)

    release_type = (album_obj.release_type or "album").lower()
    if release_type == "single":
        tmpl = template or cfg.naming.single
    elif release_type == "ep":
        tmpl = template or cfg.naming.ep
    elif release_type == "compilation":
        tmpl = template or cfg.naming.compilation
    else:
        tmpl = template or cfg.naming.album

    artist_name = album_obj.artist.name if album_obj.artist else "Unknown Artist"
    console.print(
        f"[cyan]Downloading album:[/cyan] {album_obj.display_title} "
        f"by {artist_name} ({album_obj.tracks.total} tracks)"
    )
    if album_obj.goodies and do_goodies:
        console.print(f"  [dim]+{len(album_obj.goodies)} goodie(s)[/dim]")

    total         = album_obj.tracks.total
    succeeded     = 0
    skipped       = 0
    failed        = 0
    track_results: list[DownloadResult] = []

    with Downloader(
        read_timeout=cfg.download.read_timeout,
        connect_timeout=cfg.download.connect_timeout,
        max_workers=n_workers,
        external_downloader=ext_dl,
        naming_template=tmpl,
        dev=_dev,
    ) as dl:
        for i, track_summary in enumerate(album_obj.tracks.items, 1):
            console.print(f"  [{i}/{total}] {track_summary.display_title}")

            try:
                track_obj = client.get_track(str(track_summary.id))
            except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
                _handle_auth_error(exc)
            except Exception as exc:
                err_console.print(f"    [red]Could not fetch track metadata: {exc}[/red]")
                failed += 1
                continue

            try:
                url_info = client.get_track_url(str(track_obj.id), quality=q)
            except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
                _handle_auth_error(exc)
            except NotStreamableError:
                err_console.print("    [yellow]Not streamable, skipping.[/yellow]")
                failed += 1
                continue
            except APIError as exc:
                err_console.print(f"    [red]API error: {exc}[/red]")
                failed += 1
                continue

            with _make_progress() as progress:
                task = progress.add_task(track_obj.title, total=None)

                def on_progress(
                    done: int, total_bytes: int,
                    _task=task, _progress=progress,
                ) -> None:
                    _progress.update(_task, completed=done, total=total_bytes or None)

                try:
                    result = dl.download_track(
                        track=track_obj,
                        url_info=url_info,
                        dest_dir=dest_dir,
                        album=album_obj,
                        on_progress=on_progress,
                    )
                except Exception as exc:
                    err_console.print(f"    [red]Download failed: {exc}[/red]")
                    failed += 1
                    continue

            track_results.append(result)

            if result.skipped and not result.dev_stub:
                if not _needs_tagging(result.path, check_cover=embed_cover):
                    console.print("    [yellow]Already complete, skipped.[/yellow]")
                    skipped += 1
                    continue
                console.print("    [yellow]File complete but missing tags — re-tagging.[/yellow]")

            _post_download(result, track_obj, album_obj, cfg, embed_cover, do_lyrics, save_cov)
            succeeded += 1

        if do_goodies and album_obj.goodies:
            album_dir = dest_dir
            for r in track_results:
                candidate = r.path.parent
                if album_obj.media_count and album_obj.media_count > 1:
                    candidate = candidate.parent
                if candidate.is_dir():
                    album_dir = candidate
                    break
            console.print(f"  [dim]Downloading {len(album_obj.goodies)} goodie(s)…[/dim]")
            for goodie in album_obj.goodies:
                gr = dl.download_goodie(goodie, album_dir)
                if gr.ok:
                    status = "[yellow]skipped[/yellow]" if gr.skipped else "[green]ok[/green]"
                    console.print(f"    {goodie.name}: {status} → {gr.path.name}")
                else:
                    err_console.print(f"    {goodie.name}: [red]{gr.error}[/red]")

    console.print(
        f"\n[green]Done.[/green] "
        f"{succeeded} downloaded, {skipped} skipped, {failed} failed."
    )


@app.command()
def playlist(
    url_or_id:  str            = typer.Argument(..., help="Playlist ID or Qobuz URL"),
    output:     Optional[Path] = typer.Option(None, "-o", "--output"),
    quality:    Optional[str]  = typer.Option(None, "-q", "--quality"),
    lyrics:     Optional[bool] = typer.Option(None, "--lyrics/--no-lyrics"),
    cover:      Optional[bool] = typer.Option(None, "--cover/--no-cover"),
    save_cover: Optional[bool] = typer.Option(None, "--save-cover/--no-save-cover"),
    workers:    Optional[int]  = typer.Option(None, "--workers", "-j"),
    downloader: Optional[str]  = typer.Option(None, "--downloader"),
    template:   Optional[str]  = typer.Option(None, "--template"),
    m3u:        bool           = typer.Option(False, "--m3u"),
) -> None:
    """Download a full playlist by ID or URL."""
    cfg = _cfg()

    dest_dir    = output     or Path(cfg.download.output_dir)
    q_str       = quality    or cfg.download.quality
    embed_cover = cover      if cover      is not None else cfg.tagging.embed_cover
    save_cov    = save_cover if save_cover is not None else cfg.tagging.save_cover_file
    do_lyrics   = lyrics     if lyrics     is not None else cfg.tagging.fetch_lyrics
    n_workers   = workers    or cfg.download.max_workers
    ext_dl      = downloader or cfg.download.external_downloader
    tmpl        = template   or cfg.naming.playlist

    try:
        q = Quality[q_str.upper()]
    except KeyError:
        err_console.print(f"[red]Unknown quality:[/red] {q_str}")
        raise typer.Exit(code=1)

    playlist_id = _resolve_id(url_or_id, expected_type="playlist")
    client      = _build_client(cfg)

    try:
        pl = client.get_playlist(playlist_id)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except NotFoundError:
        err_console.print(f"[red]Playlist not found:[/red] {playlist_id}")
        raise typer.Exit(code=1)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    if not pl.tracks or not pl.tracks.items:
        err_console.print("[red]Playlist is empty.[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"[cyan]Downloading playlist:[/cyan] {pl.name} ({pl.tracks_count} tracks)"
    )

    succeeded  = 0
    skipped    = 0
    failed     = 0
    m3u_lines: list[str] = ["#EXTM3U"]

    with Downloader(
        read_timeout=cfg.download.read_timeout,
        connect_timeout=cfg.download.connect_timeout,
        max_workers=n_workers,
        external_downloader=ext_dl,
        naming_template=tmpl,
        dev=_dev,
    ) as dl:
        for i, pl_track in enumerate(pl.tracks.items, 1):
            console.print(f"  [{i}/{pl.tracks_count}] {pl_track.display_title}")

            try:
                track_obj = client.get_track(str(pl_track.id))
            except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
                _handle_auth_error(exc)
            except Exception as exc:
                err_console.print(f"    [red]Could not fetch track metadata: {exc}[/red]")
                failed += 1
                continue

            album_obj = None
            if track_obj.album:
                try:
                    album_obj = client.get_album(track_obj.album.id)
                except Exception:
                    pass

            try:
                url_info = client.get_track_url(str(track_obj.id), quality=q)
            except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
                _handle_auth_error(exc)
            except NotStreamableError:
                err_console.print("    [yellow]Not streamable, skipping.[/yellow]")
                failed += 1
                continue
            except APIError as exc:
                err_console.print(f"    [red]API error: {exc}[/red]")
                failed += 1
                continue

            with _make_progress() as progress:
                task = progress.add_task(track_obj.title, total=None)

                def on_progress(
                    done: int, total_bytes: int,
                    _task=task, _progress=progress,
                ) -> None:
                    _progress.update(_task, completed=done, total=total_bytes or None)

                try:
                    result = dl.download_track(
                        track=track_obj,
                        url_info=url_info,
                        dest_dir=dest_dir,
                        album=None,
                        on_progress=on_progress,
                        playlist_name=pl.name,
                        playlist_index=i,
                    )
                except Exception as exc:
                    err_console.print(f"    [red]Download failed: {exc}[/red]")
                    failed += 1
                    continue

            if result.skipped and not result.dev_stub:
                if not _needs_tagging(result.path, check_cover=embed_cover):
                    console.print("    [yellow]Already complete, skipped.[/yellow]")
                    skipped += 1
                    m3u_lines.append(str(result.path))
                    continue
                console.print("    [yellow]File complete but missing tags — re-tagging.[/yellow]")

            _post_download(result, track_obj, album_obj, cfg, embed_cover, do_lyrics, save_cov)
            m3u_lines.append(str(result.path))
            succeeded += 1

    if m3u and len(m3u_lines) > 1:
        pl_dir  = dest_dir / sanitize(pl.name)
        pl_dir.mkdir(parents=True, exist_ok=True)
        m3u_path = pl_dir / f"{sanitize(pl.name)}.m3u8"
        m3u_path.write_text("\n".join(m3u_lines), encoding="utf-8")
        console.print(f"  [dim]M3U8 written: {m3u_path}[/dim]")

    console.print(
        f"\n[green]Done.[/green] "
        f"{succeeded} downloaded, {skipped} skipped, {failed} failed."
    )


@app.command()
def search(
    query:       str  = typer.Argument(..., help="Search query"),
    type:        str  = typer.Option("tracks", "--type", "-t",
                                     help="tracks, albums, artists, playlists"),
    limit:       int  = typer.Option(10, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Search the Qobuz catalog."""
    valid_types = {"tracks", "albums", "artists", "playlists"}
    if type not in valid_types:
        err_console.print(f"[red]Invalid type:[/red] {type!r}. Choose from: {', '.join(sorted(valid_types))}")
        raise typer.Exit(code=1)

    client = _build_client()

    try:
        results = client.search(query=query, type=type, limit=limit)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except APIError as exc:
        err_console.print(f"[red]Search failed:[/red] {exc}")
        raise typer.Exit(code=1)

    if json_output:
        import json
        console.print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    items = results.get(type, {}).get("items", [])
    if not items:
        console.print(f"No results for [bold]{query!r}[/bold].")
        return

    table = Table(title=f'Search: "{query}" ({type})', show_lines=False)

    if type == "tracks":
        table.add_column("ID",    style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Artist")
        table.add_column("Album", style="dim")
        for item in items:
            artist = (item.get("performer") or {}).get("name", "")
            alb    = (item.get("album") or {}).get("title", "")
            table.add_row(str(item.get("id", "")), item.get("title", ""), artist, alb)
    elif type == "albums":
        table.add_column("ID",    style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Artist")
        table.add_column("Year",  style="dim")
        for item in items:
            artist = (item.get("artist") or {}).get("name", "")
            year   = (item.get("release_date_original") or "")[:4]
            table.add_row(str(item.get("id", "")), item.get("title", ""), artist, year)
    elif type == "artists":
        table.add_column("ID",     style="dim", no_wrap=True)
        table.add_column("Name",   style="bold")
        table.add_column("Albums", style="dim")
        for item in items:
            table.add_row(str(item.get("id", "")), item.get("name", ""), str(item.get("albums_count", "")))
    elif type == "playlists":
        table.add_column("ID",     style="dim", no_wrap=True)
        table.add_column("Name",   style="bold")
        table.add_column("Tracks", style="dim")
        table.add_column("Owner")
        for item in items:
            owner = (item.get("owner") or {}).get("name", "")
            table.add_row(str(item.get("id", "")), item.get("name", ""), str(item.get("tracks_count", "")), owner)

    console.print(table)


# ── library subcommands ────────────────────────────────────────────────────

@library_app.command("show")
def library_show(
    type: str = typer.Option("all", "--type", "-t",
                             help="track, album, artist, or all"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """Show local favorites."""
    cfg   = _cfg()
    store = _store(cfg)

    types = ["track", "album", "artist"] if type == "all" else [type]
    for t in types:
        items = store.get_favorites(t, limit=limit)
        if not items:
            continue
        table = Table(title=f"Favorite {t}s ({len(items)})", show_lines=False)
        table.add_column("ID",     style="dim", no_wrap=True)
        table.add_column("Title",  style="bold")
        table.add_column("Artist")
        if t == "track":
            table.add_column("Album", style="dim")
        for item in items:
            row = [item.get("id", ""), item.get("title", ""), item.get("artist", "")]
            if t == "track":
                row.append(item.get("extra", ""))
            table.add_row(*row)
        console.print(table)


@library_app.command("add")
def library_add(
    url_or_id: str = typer.Argument(..., help="Track/album/artist ID or Qobuz URL"),
    type: str = typer.Option("track", "--type", "-t", help="track, album, or artist"),
    remote: bool = typer.Option(False, "--remote/--local-only",
                                help="Also add to Qobuz account (personal session only)"),
) -> None:
    """
    Add a track, album, or artist to local favorites.

    Works in all modes including token pool. Use --remote to also
    add to your Qobuz account (requires a personal session, not a pool).
    """
    cfg      = _cfg()
    store    = _store(cfg)
    item_id  = _resolve_id(url_or_id, expected_type=type if url_or_id.startswith("http") else None)
    client   = _build_client(cfg)

    # Fetch metadata so we can store a useful display name.
    title = artist = extra = ""
    try:
        if type == "track":
            obj    = client.get_track(item_id)
            title  = obj.display_title
            artist = obj.performer.name if obj.performer else ""
            extra  = obj.album.title if obj.album else ""
        elif type == "album":
            obj    = client.get_album(item_id)
            title  = obj.display_title
            artist = obj.artist.name if obj.artist else ""
            extra  = obj.genre.name if obj.genre else ""
        elif type == "artist":
            obj    = client.get_artist(item_id, extras="")
            title  = obj.name
    except Exception as exc:
        err_console.print(f"[yellow]Could not fetch metadata: {exc} — storing ID only.[/yellow]")

    store.add_favorite(item_id, type, title=title, artist=artist, extra=extra)
    console.print(f"[green]Added[/green] {type}: {title or item_id}")

    if remote:
        try:
            kwargs: dict = {}
            if type == "track":
                kwargs = {"track_ids": [item_id]}
            elif type == "album":
                kwargs = {"album_ids": [item_id]}
            elif type == "artist":
                kwargs = {"artist_ids": [item_id]}
            client.add_favorite(**kwargs)
            console.print(f"  [dim]Also added to Qobuz account.[/dim]")
        except PoolModeError:
            err_console.print(
                "  [yellow]--remote is not available in pool mode.[/yellow]"
            )
        except Exception as exc:
            err_console.print(f"  [yellow]Remote add failed: {exc}[/yellow]")


@library_app.command("remove")
def library_remove(
    url_or_id: str = typer.Argument(..., help="Track/album/artist ID or Qobuz URL"),
    type: str = typer.Option("track", "--type", "-t"),
    remote: bool = typer.Option(False, "--remote/--local-only"),
) -> None:
    """Remove a track, album, or artist from local favorites."""
    cfg     = _cfg()
    store   = _store(cfg)
    item_id = _resolve_id(url_or_id)

    removed = store.remove_favorite(item_id, type)
    if removed:
        console.print(f"[green]Removed[/green] {type} {item_id} from local favorites.")
    else:
        console.print(f"[yellow]{type} {item_id} was not in local favorites.[/yellow]")

    if remote:
        client = _build_client(cfg)
        try:
            kwargs: dict = {}
            if type == "track":
                kwargs = {"track_ids": [item_id]}
            elif type == "album":
                kwargs = {"album_ids": [item_id]}
            elif type == "artist":
                kwargs = {"artist_ids": [item_id]}
            client.remove_favorite(**kwargs)
            console.print(f"  [dim]Also removed from Qobuz account.[/dim]")
        except PoolModeError:
            err_console.print("  [yellow]--remote is not available in pool mode.[/yellow]")
        except Exception as exc:
            err_console.print(f"  [yellow]Remote remove failed: {exc}[/yellow]")


@library_app.command("sync")
def library_sync(
    type: str  = typer.Option("all", "--type", "-t", help="track, album, artist, or all"),
    clear: bool = typer.Option(False, "--clear", help="Clear local favorites before syncing"),
) -> None:
    """
    Sync favorites from your Qobuz account into the local store.

    Requires a personal session (not available in pool mode).
    """
    cfg    = _cfg()
    client = _build_client(cfg)
    store  = _store(cfg)

    if client.is_pool_mode:
        err_console.print(
            "[red]sync is not available in pool mode.[/red]\n"
            "Pool mode clients don't have a personal account to sync from."
        )
        raise typer.Exit(code=1)

    types = ["track", "album", "artist"] if type == "all" else [type]

    try:
        fav = client.get_user_favorites()
    except Exception as exc:
        err_console.print(f"[red]Failed to fetch favorites: {exc}[/red]")
        raise typer.Exit(code=1)

    import dataclasses
    for t in types:
        page = getattr(fav, f"{t}s" if not t.endswith("s") else t, None)
        # UserFavorites has .tracks, .albums, .artists
        page = getattr(fav, {"track": "tracks", "album": "albums", "artist": "artists"}[t], None)
        if not page or not page.items:
            console.print(f"  [dim]No {t} favorites found on account.[/dim]")
            continue
        raw = [dataclasses.asdict(i) for i in page.items]
        n = store.sync_favorites_from_api(raw, t, clear_first=clear)
        console.print(f"[green]Synced[/green] {n} {t}(s) → local store.")


@library_app.command("history")
def library_history(
    limit: int  = typer.Option(20, "--limit", "-n"),
    clear: bool = typer.Option(False, "--clear", help="Clear all history"),
) -> None:
    """Show or clear the local download/play history."""
    cfg   = _cfg()
    store = _store(cfg)

    if clear:
        n = store.clear_history()
        console.print(f"[green]Cleared {n} history entries.[/green]")
        return

    rows = store.get_history(limit=limit)
    if not rows:
        console.print("[dim]No history yet.[/dim]")
        return

    table = Table(title=f"Recent history (last {limit})", show_lines=False)
    table.add_column("Time",   style="dim", no_wrap=True)
    table.add_column("Title",  style="bold")
    table.add_column("Artist")
    table.add_column("Album",  style="dim")
    for row in rows:
        table.add_row(
            row.get("played_at", "")[:16],
            row.get("title", ""),
            row.get("artist", ""),
            row.get("album", ""),
        )
    console.print(table)


# ── lpl (local playlist) subcommands ──────────────────────────────────────

@lpl_app.command("list")
def lpl_list() -> None:
    """List all local playlists."""
    store = _store()
    pls   = store.list_playlists()
    if not pls:
        console.print("[dim]No local playlists yet. Use [bold]qobuz lpl create[/bold] to make one.[/dim]")
        return
    table = Table(title="Local playlists", show_lines=False)
    table.add_column("ID",     style="dim", no_wrap=True, max_width=10)
    table.add_column("Name",   style="bold")
    table.add_column("Tracks", style="dim")
    table.add_column("Updated", style="dim")
    for pl in pls:
        table.add_row(
            pl["id"][:8],
            pl["name"],
            str(pl.get("track_count", 0)),
            pl.get("updated_at", "")[:10],
        )
    console.print(table)


@lpl_app.command("create")
def lpl_create(
    name:        str = typer.Argument(..., help="Playlist name"),
    description: str = typer.Option("", "--desc", "-d"),
) -> None:
    """Create a new local playlist."""
    store = _store()
    pl_id = store.create_playlist(name, description)
    console.print(f"[green]Created[/green] playlist [bold]{name}[/bold] (id: {pl_id[:8]})")

@lpl_app.command("clone")
def lpl_clone(
    url_or_id: str = typer.Argument(..., help="Qobuz playlist ID or URL"),
    name: Optional[str] = typer.Option(None, "--name", "-n",
                                       help="Override playlist name"),
) -> None:
    """
    Clone a Qobuz playlist into the local store by ID or URL.

    Fetches all tracks from the playlist and saves them locally.
    Works in all modes including token pool.

    \b
    Examples:
        qobuz lpl clone 12345678
        qobuz lpl clone https://open.qobuz.com/playlist/12345678
        qobuz lpl clone 12345678 --name "My Copy"
    """
    cfg         = _cfg()
    store       = _store(cfg)
    client      = _build_client(cfg)
    playlist_id = _resolve_id(url_or_id, expected_type="playlist")

    try:
        pl = client.get_playlist(playlist_id)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except NotFoundError:
        err_console.print(f"[red]Playlist not found:[/red] {playlist_id}")
        raise typer.Exit(code=1)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    if not pl.tracks or not pl.tracks.items:
        err_console.print("[red]Playlist is empty.[/red]")
        raise typer.Exit(code=1)

    local_name = name or pl.name
    # Avoid name collision.
    if store.get_playlist_by_name(local_name):
        local_name = f"{local_name} (cloned)"

    local_id = store.create_playlist(local_name, pl.description or "")
    console.print(
        f"[cyan]Cloning[/cyan] [bold]{pl.name}[/bold] "
        f"({pl.tracks_count} tracks) → local playlist [bold]{local_name}[/bold]"
    )

    added = 0
    for i, t in enumerate(pl.tracks.items):
        store.add_track_to_playlist(
            local_id,
            str(t.id),
            title=t.title,
            artist=t.performer.name if t.performer else "",
            album=t.album.title if t.album else "",
            duration=t.duration or 0,
            isrc=getattr(t, "isrc", "") or "",
            position=i,
        )
        added += 1

    console.print(f"[green]Done.[/green] {added} tracks saved locally.")
    console.print(
        f"  Download with: [bold]qobuz lpl download {local_name!r}[/bold]"
    )

@lpl_app.command("show")
def lpl_show(name: str = typer.Argument(..., help="Playlist name or ID prefix")) -> None:
    """Show tracks in a local playlist."""
    store = _store()
    pl    = store.get_playlist_by_name(name)
    if not pl:
        # Try by ID prefix
        for candidate in store.list_playlists():
            if candidate["id"].startswith(name):
                pl = candidate
                break
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {name}")
        raise typer.Exit(code=1)

    tracks = store.get_playlist_tracks(pl["id"])
    console.print(f"[bold]{pl['name']}[/bold]  [dim]{pl.get('description','')}[/dim]")
    console.print(f"[dim]{len(tracks)} track(s)[/dim]")
    if not tracks:
        return
    table = Table(show_lines=False, show_header=True)
    table.add_column("#",      style="dim", width=4)
    table.add_column("ID",     style="dim", no_wrap=True)
    table.add_column("Title",  style="bold")
    table.add_column("Artist")
    table.add_column("Album",  style="dim")
    for t in tracks:
        table.add_row(
            str(t["position"] + 1),
            str(t["track_id"]),
            t.get("title", ""),
            t.get("artist", ""),
            t.get("album", ""),
        )
    console.print(table)


@lpl_app.command("add")
def lpl_add(
    playlist: str = typer.Argument(..., help="Playlist name or ID prefix"),
    track:    str = typer.Argument(..., help="Track ID or Qobuz URL"),
) -> None:
    """Add a track to a local playlist (fetches metadata from Qobuz)."""
    cfg      = _cfg()
    store    = _store(cfg)
    track_id = _resolve_id(track, expected_type="track")

    pl = store.get_playlist_by_name(playlist)
    if not pl:
        for candidate in store.list_playlists():
            if candidate["id"].startswith(playlist):
                pl = candidate
                break
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {playlist}")
        raise typer.Exit(code=1)

    title = artist = album = ""
    duration = 0
    isrc = ""
    try:
        client    = _build_client(cfg)
        track_obj = client.get_track(track_id)
        title     = track_obj.display_title
        artist    = track_obj.performer.name if track_obj.performer else ""
        album     = track_obj.album.title if track_obj.album else ""
        duration  = track_obj.duration or 0
        isrc      = track_obj.isrc or ""
    except Exception as exc:
        err_console.print(f"[yellow]Could not fetch metadata: {exc}[/yellow]")

    store.add_track_to_playlist(
        pl["id"], track_id,
        title=title, artist=artist, album=album,
        duration=duration, isrc=isrc,
    )
    console.print(f"[green]Added[/green] {title or track_id} → [bold]{pl['name']}[/bold]")


@lpl_app.command("remove")
def lpl_remove(
    playlist: str = typer.Argument(..., help="Playlist name or ID prefix"),
    track:    str = typer.Argument(..., help="Track ID"),
) -> None:
    """Remove a track from a local playlist."""
    store = _store()
    pl    = store.get_playlist_by_name(playlist) or next(
        (c for c in store.list_playlists() if c["id"].startswith(playlist)), None
    )
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {playlist}")
        raise typer.Exit(code=1)
    removed = store.remove_track_from_playlist(pl["id"], track)
    if removed:
        console.print(f"[green]Removed[/green] track {track} from [bold]{pl['name']}[/bold].")
    else:
        console.print(f"[yellow]Track {track} was not in that playlist.[/yellow]")


@lpl_app.command("delete")
def lpl_delete(
    name:    str  = typer.Argument(..., help="Playlist name or ID prefix"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a local playlist."""
    store = _store()
    pl    = store.get_playlist_by_name(name) or next(
        (c for c in store.list_playlists() if c["id"].startswith(name)), None
    )
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {name}")
        raise typer.Exit(code=1)
    if not confirm:
        typer.confirm(f"Delete playlist '{pl['name']}'?", abort=True)
    store.delete_playlist(pl["id"])
    console.print(f"[green]Deleted[/green] playlist [bold]{pl['name']}[/bold].")


@lpl_app.command("download")
def lpl_download(
    name:       str            = typer.Argument(..., help="Playlist name or ID prefix"),
    output:     Optional[Path] = typer.Option(None, "-o", "--output"),
    quality:    Optional[str]  = typer.Option(None, "-q", "--quality"),
    workers:    Optional[int]  = typer.Option(None, "--workers", "-j"),
    template:   Optional[str]  = typer.Option(None, "--template"),
) -> None:
    """Download all tracks in a local playlist."""
    cfg   = _cfg()
    store = _store(cfg)

    pl = store.get_playlist_by_name(name) or next(
        (c for c in store.list_playlists() if c["id"].startswith(name)), None
    )
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {name}")
        raise typer.Exit(code=1)

    tracks = store.get_playlist_tracks(pl["id"])
    if not tracks:
        err_console.print("[red]Playlist is empty.[/red]")
        raise typer.Exit(code=1)

    dest_dir  = output    or Path(cfg.download.output_dir)
    q_str     = quality   or cfg.download.quality
    n_workers = workers   or cfg.download.max_workers
    tmpl      = template  or cfg.naming.playlist

    try:
        q = Quality[q_str.upper()]
    except KeyError:
        err_console.print(f"[red]Unknown quality:[/red] {q_str}")
        raise typer.Exit(code=1)

    client = _build_client(cfg)
    console.print(f"[cyan]Downloading local playlist:[/cyan] {pl['name']} ({len(tracks)} tracks)")

    succeeded = skipped = failed = 0

    with Downloader(
        read_timeout=cfg.download.read_timeout,
        connect_timeout=cfg.download.connect_timeout,
        max_workers=n_workers,
        naming_template=tmpl,
        dev=_dev,
    ) as dl:
        for i, t in enumerate(tracks, 1):
            track_id = t["track_id"]
            console.print(f"  [{i}/{len(tracks)}] {t.get('title', track_id)}")

            try:
                track_obj = client.get_track(track_id)
                url_info  = client.get_track_url(track_id, quality=q)
            except Exception as exc:
                err_console.print(f"    [red]{exc}[/red]")
                failed += 1
                continue

            try:
                result = dl.download_track(
                    track=track_obj, url_info=url_info,
                    dest_dir=dest_dir, album=None,
                    playlist_name=pl["name"], playlist_index=i,
                )
            except Exception as exc:
                err_console.print(f"    [red]Download failed: {exc}[/red]")
                failed += 1
                continue

            if result.skipped and not result.dev_stub:
                skipped += 1
                continue

            _post_download(
                result, track_obj, None, cfg,
                cfg.tagging.embed_cover,
                cfg.tagging.fetch_lyrics,
                cfg.tagging.save_cover_file,
            )
            succeeded += 1

    console.print(
        f"\n[green]Done.[/green] "
        f"{succeeded} downloaded, {skipped} skipped, {failed} failed."
    )


@lpl_app.command("share")
def lpl_share(
    name:   str            = typer.Argument(..., help="Playlist name or ID prefix"),
    output: Optional[Path] = typer.Option(None, "-o", "--output",
                                          help="Output path (default: <name>.toml in playlists dir)"),
    author: str            = typer.Option("", "--author", "-a"),
) -> None:
    """
    Export a local playlist as a shareable TOML file.

    The exported file contains track IDs and display metadata.
    Anyone with qobuz-py can import it with [bold]qobuz lpl import[/bold].
    """
    cfg   = _cfg()
    store = _store(cfg)

    pl = store.get_playlist_by_name(name) or next(
        (c for c in store.list_playlists() if c["id"].startswith(name)), None
    )
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {name}")
        raise typer.Exit(code=1)

    tracks = store.get_playlist_tracks(pl["id"])
    lpl = playlist_from_store_tracks(
        name=pl["name"],
        tracks=tracks,
        description=pl.get("description", ""),
        author=author,
    )

    if output is None:
        cfg.local_data.playlists_dir.mkdir(parents=True, exist_ok=True)
        safe = sanitize(pl["name"])
        output = cfg.local_data.playlists_dir / f"{safe}.toml"

    save_playlist(lpl, output)
    console.print(f"[green]Exported[/green] [bold]{pl['name']}[/bold] → {output}")
    console.print(f"  [dim]{lpl.track_count} tracks — share this file with anyone using qobuz-py[/dim]")


@lpl_app.command("import")
def lpl_import(
    file: Path = typer.Argument(..., help="Path to a .toml playlist file"),
) -> None:
    """
    Import a shared TOML playlist file into the local store.

    Works offline — no Qobuz account needed to import.
    Track IDs can be used to download later with [bold]qobuz lpl download[/bold].
    """
    if not file.exists():
        err_console.print(f"[red]File not found:[/red] {file}")
        raise typer.Exit(code=1)

    try:
        lpl = load_playlist(file)
    except (ValueError, Exception) as exc:
        err_console.print(f"[red]Failed to parse playlist:[/red] {exc}")
        raise typer.Exit(code=1)

    store = _store()

    # Check for name conflict.
    existing = store.get_playlist_by_name(lpl.name)
    if existing:
        new_name = f"{lpl.name} (imported)"
        console.print(f"[yellow]Name conflict — importing as '{new_name}'[/yellow]")
        lpl.name = new_name

    pl_id = store.create_playlist(lpl.name, lpl.description)
    for i, t in enumerate(lpl.tracks):
        store.add_track_to_playlist(
            pl_id, t.id,
            title=t.title, artist=t.artist, album=t.album,
            duration=t.duration, isrc=t.isrc,
            position=i,
        )

    console.print(
        f"[green]Imported[/green] [bold]{lpl.name}[/bold] "
        f"({len(lpl.tracks)} tracks)"
    )
    if lpl.author:
        console.print(f"  [dim]Author: {lpl.author}[/dim]")


# ── export subcommands ─────────────────────────────────────────────────────

@export_app.command("backup")
def export_backup(
    output: Optional[Path] = typer.Option(
        None, "-o", "--output",
        help="Output .tar.gz path (default: exports/qobuz-backup-{date}.tar.gz)",
    ),
) -> None:
    """
    Create a full backup of your local library as a .tar.gz archive.

    Archive contains:
      - library.db (SQLite database)
      - config.toml (with credentials stripped)
      - playlists/*.toml
      - favorites/tracks.json, albums.json, artists.json
      - manifest.json

    Restore by extracting the archive and replacing library.db.
    """
    cfg   = _cfg()
    store = _store(cfg)

    if output is None:
        cfg.local_data.exports_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        output = cfg.local_data.exports_dir / f"qobuz-backup-{date}.tar.gz"

    console.print(f"[cyan]Creating backup…[/cyan]")
    try:
        path = backup_to_tar(
            store=store,
            config_path=_CONFIG_PATH,
            playlists_dir=cfg.local_data.playlists_dir,
            output_path=output,
        )
    except Exception as exc:
        err_console.print(f"[red]Backup failed:[/red] {exc}")
        raise typer.Exit(code=1)

    size_kb = path.stat().st_size / 1024
    console.print(f"[green]Backup saved:[/green] {path}  [dim]({size_kb:.1f} KB)[/dim]")


@export_app.command("favorites")
def export_favorites(
    output: Optional[Path] = typer.Option(
        None, "-o", "--output",
        help="Output .toml path (default: exports/favorites-{date}.toml)",
    ),
    type: str = typer.Option("all", "--type", "-t",
                             help="track, album, artist, or all"),
) -> None:
    """Export local favorites to a human-readable TOML file."""
    cfg   = _cfg()
    store = _store(cfg)

    if output is None:
        cfg.local_data.exports_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        output = cfg.local_data.exports_dir / f"favorites-{date}.toml"

    t = None if type == "all" else type
    try:
        path = export_favorites_toml(store, output, type=t)
    except Exception as exc:
        err_console.print(f"[red]Export failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]Favorites exported:[/green] {path}")


@export_app.command("playlist")
def export_playlist(
    name:   str            = typer.Argument(..., help="Playlist name or ID prefix"),
    output: Optional[Path] = typer.Option(None, "-o", "--output"),
    author: str            = typer.Option("", "--author", "-a"),
) -> None:
    """Export a local playlist to a shareable TOML file (alias for lpl share)."""
    # Delegate to lpl_share for consistent behaviour.
    lpl_share(name=name, output=output, author=author)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()

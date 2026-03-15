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
        BarColumn, DownloadColumn, Progress,
        TextColumn, TimeRemainingColumn, TransferSpeedColumn,
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
    QobuzConfig, _CONFIG_PATH, _SESSION_PATH,
    load_config, save_config, update_config,
)
from .exceptions import (
    APIError, ConfigError, InvalidCredentialsError,
    NoAuthError, NotFoundError, NotStreamableError,
    PoolModeError, TokenExpiredError,
)
from .quality import Quality
from .session import QobuzSession
from .url import parse_url

# ── Sub-apps ───────────────────────────────────────────────────────────────

app          = typer.Typer(name="qobuz", add_completion=False,
                           help="Unofficial Qobuz CLI.")
library_app  = typer.Typer(help="Manage local and remote favourites.")
lpl_app      = typer.Typer(help="Create, manage, and share local playlists.")
export_app   = typer.Typer(help="Export, import, and back up your library.")
account_app  = typer.Typer(help="View and update your Qobuz account profile.")
remote_app   = typer.Typer(help="Manage playlists on your Qobuz account.")

app.add_typer(library_app, name="library")
app.add_typer(lpl_app,     name="lpl")
app.add_typer(export_app,  name="export")
app.add_typer(account_app, name="account")
app.add_typer(remote_app,  name="remote")

console     = Console()
err_console = Console(stderr=True)
_dev: bool  = False


# ── Global callback ────────────────────────────────────────────────────────

@app.callback()
def main(
    dev: bool = typer.Option(
        False, "--dev", envvar="QOBUZ_DEV", is_eager=True,
        help="Developer mode: cache API responses and write dev audio.",
    ),
) -> None:
    """Unofficial Qobuz CLI."""
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


# ── Shared helpers ─────────────────────────────────────────────────────────

def _cfg() -> QobuzConfig:
    return load_config()


def _session(cfg: Optional[QobuzConfig] = None) -> QobuzSession:
    return QobuzSession.from_config(dev=_dev)


def _handle_auth_error(exc: Exception) -> None:
    err_console.print(
        f"[red]Authentication error:[/red] {exc}\n"
        "Run [bold]qobuz login[/bold] to refresh your session."
    )
    raise typer.Exit(code=1)


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


def _make_progress() -> Progress:
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(), DownloadColumn(),
        TransferSpeedColumn(), TimeRemainingColumn(),
        console=console, transient=True,
    )


def _track_start_cb(title: str, index: int, total: int) -> None:
    console.print(f"  [{index}/{total}] {title}")


def _track_done_cb(result) -> None:
    if result.download.skipped and not result.download.dev_stub:
        console.print("    [yellow]Already complete, skipped.[/yellow]")


def _yn(v: bool) -> str:
    return "[green]yes[/green]" if v else "[dim]no[/dim]"


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
    1. Username + password:  qobuz login -u me@example.com -p secret
    2. Direct token:         qobuz login --token TOKEN --user-id 12345
    3. Token pool:           qobuz login --pool ~/.config/qobuz/pool.txt
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
        console.print(f"[green]Token pool saved.[/green] Config: [bold]{_CONFIG_PATH}[/bold]")
        return

    resolved_app_id     = app_id     or os.environ.get("QOBUZ_APP_ID")     or cfg.credentials.app_id
    resolved_app_secret = app_secret or os.environ.get("QOBUZ_APP_SECRET") or cfg.credentials.app_secret

    if not resolved_app_id:
        resolved_app_id = typer.prompt("App ID")
    if not resolved_app_secret:
        resolved_app_secret = typer.prompt("App Secret", hide_input=True)

    cfg.credentials.app_id     = resolved_app_id
    cfg.credentials.app_secret = resolved_app_secret
    cfg.credentials.pool       = ""

    client = QobuzClient.from_credentials(
        app_id=resolved_app_id, app_secret=resolved_app_secret,
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

    if cfg.local_data.auto_sync_favorites and not cfg.credentials.pool:
        console.print("[dim]Syncing favorites to local store…[/dim]")
        try:
            s = QobuzSession.from_client(client, cfg)
            counts = s.sync_favorites()
            for t, n in counts.items():
                if n:
                    console.print(f"  [dim]{n} {t}(s) synced[/dim]")
        except Exception as exc:
            err_console.print(f"[yellow]Auto-sync failed:[/yellow] {exc}")

    console.print(
        f"[green]Logged in as[/green] {session.user_email or session.user_id}. "
        f"Session saved to [bold]{_SESSION_PATH}[/bold]."
    )


@app.command()
def config(
    show: bool = typer.Option(False, "--show"),
    set_: Optional[str] = typer.Option(None, "--set",
        help="section.key=value  e.g. local_data.track_history=true"),
) -> None:
    """
    View or update the configuration file.

    \b
    Examples:
        qobuz config --show
        qobuz config --set download.max_workers=4
        qobuz config --set local_data.data_dir=/sdcard/qobuz-data
        qobuz config --set local_data.auto_sync_favorites=true
    """
    if show:
        import dataclasses
        console.print_json(
            __import__("json").dumps(dataclasses.asdict(_cfg()), indent=2)
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

    console.print("Use [bold]--show[/bold] or [bold]--set section.key=value[/bold].")
    console.print(f"Config: [bold]{_CONFIG_PATH}[/bold]")


@app.command("dev-cache")
def dev_cache(
    clear: bool = typer.Option(False, "--clear"),
    show:  bool = typer.Option(False, "--show"),
) -> None:
    """Manage the dev mode API response cache."""
    from .dev import CACHE_DIR, clear_cache
    if clear:
        console.print(f"[green]Cleared {clear_cache()} cached response(s).[/green]")
        return
    files = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
    console.print(
        f"Cache: [bold]{CACHE_DIR}[/bold]  ({len(files)} files"
        + (f", {sum(f.stat().st_size for f in files)/1024:.1f} KB)" if files else ")")
    )


# ── Download commands ──────────────────────────────────────────────────────

@app.command()
def track(
    url_or_id:  str            = typer.Argument(...),
    output:     Optional[Path] = typer.Option(None,  "-o"),
    quality:    Optional[str]  = typer.Option(None,  "-q"),
    lyrics:     Optional[bool] = typer.Option(None,  "--lyrics/--no-lyrics"),
    cover:      Optional[bool] = typer.Option(None,  "--cover/--no-cover"),
    save_cover: Optional[bool] = typer.Option(None,  "--save-cover/--no-save-cover"),
    workers:    Optional[int]  = typer.Option(None,  "-j"),
    template:   Optional[str]  = typer.Option(None,  "--template"),
) -> None:
    """Download a single track."""
    cfg = _cfg()
    q   = None
    if quality:
        try:
            q = Quality[quality.upper()]
        except KeyError:
            err_console.print(f"[red]Unknown quality:[/red] {quality}")
            raise typer.Exit(code=1)

    sess = _session(cfg)
    task_ref: list = []

    def on_progress(done: int, total: int) -> None:
        if task_ref:
            prog, task = task_ref
            prog.update(task, completed=done, total=total or None)

    with _make_progress() as prog:
        try:
            track_obj = sess.client.get_track(_resolve_id(url_or_id, "track"))
            task = prog.add_task(track_obj.title, total=None)
            task_ref.extend([prog, task])
            result = sess.download_track(
                url_or_id,
                quality=q,
                dest_dir=output or Path(cfg.download.output_dir),
                template=template,
                embed_cover=cover,
                fetch_lyrics_flag=lyrics,
                save_cover_file=save_cover,
                on_progress=on_progress,
                workers=workers,
            )
        except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
            _handle_auth_error(exc)
        except NotStreamableError as exc:
            err_console.print(f"[red]Not streamable:[/red] {exc}")
            raise typer.Exit(code=1)
        except APIError as exc:
            err_console.print(f"[red]API error:[/red] {exc}")
            raise typer.Exit(code=1)

    if result.download.skipped and not result.download.dev_stub:
        console.print(f"[yellow]Skipped[/yellow] (already complete): {result.download.path}")
        return
    console.print(f"[green]Done:[/green] {result.download.path}")


@app.command()
def album(
    url_or_id:  str            = typer.Argument(...),
    output:     Optional[Path] = typer.Option(None,  "-o"),
    quality:    Optional[str]  = typer.Option(None,  "-q"),
    lyrics:     Optional[bool] = typer.Option(None,  "--lyrics/--no-lyrics"),
    cover:      Optional[bool] = typer.Option(None,  "--cover/--no-cover"),
    save_cover: Optional[bool] = typer.Option(None,  "--save-cover/--no-save-cover"),
    goodies:    Optional[bool] = typer.Option(None,  "--goodies/--no-goodies"),
    workers:    Optional[int]  = typer.Option(None,  "-j"),
    template:   Optional[str]  = typer.Option(None,  "--template"),
) -> None:
    """Download a full album."""
    cfg = _cfg()
    q   = None
    if quality:
        try:
            q = Quality[quality.upper()]
        except KeyError:
            err_console.print(f"[red]Unknown quality:[/red] {quality}")
            raise typer.Exit(code=1)

    sess = _session(cfg)
    try:
        result = sess.download_album(
            url_or_id,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            template=template,
            embed_cover=cover,
            fetch_lyrics_flag=lyrics,
            save_cover_file=save_cover,
            download_goodies=goodies if goodies is not None else True,
            on_track_start=_track_start_cb,
            on_track_done=_track_done_cb,
            workers=workers,
        )
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{result.succeeded} downloaded, {result.skipped} skipped."
    )


@app.command()
def playlist(
    url_or_id:  str            = typer.Argument(...),
    output:     Optional[Path] = typer.Option(None, "-o"),
    quality:    Optional[str]  = typer.Option(None, "-q"),
    lyrics:     Optional[bool] = typer.Option(None, "--lyrics/--no-lyrics"),
    cover:      Optional[bool] = typer.Option(None, "--cover/--no-cover"),
    save_cover: Optional[bool] = typer.Option(None, "--save-cover/--no-save-cover"),
    workers:    Optional[int]  = typer.Option(None, "-j"),
    template:   Optional[str]  = typer.Option(None, "--template"),
    m3u:        bool           = typer.Option(False, "--m3u"),
) -> None:
    """Download a full playlist (all pages, pagination-proof)."""
    cfg = _cfg()
    q   = None
    if quality:
        try:
            q = Quality[quality.upper()]
        except KeyError:
            err_console.print(f"[red]Unknown quality:[/red] {quality}")
            raise typer.Exit(code=1)

    sess = _session(cfg)
    try:
        result = sess.download_playlist(
            url_or_id,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            template=template,
            embed_cover=cover,
            fetch_lyrics_flag=lyrics,
            save_cover_file=save_cover,
            on_track_start=_track_start_cb,
            on_track_done=_track_done_cb,
            workers=workers,
            write_m3u=m3u,
        )
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{result.succeeded} downloaded, {result.skipped} skipped, {result.failed} failed."
    )


@app.command()
def artist(
    url_or_id:    str            = typer.Argument(..., help="Artist ID or Qobuz URL"),
    output:       Optional[Path] = typer.Option(None,  "-o", "--output"),
    quality:      Optional[str]  = typer.Option(None,  "-q", "--quality"),
    release_type: Optional[str]  = typer.Option(
        None, "--type", "-t",
        help="album, live, compilation, epSingle, other, download. "
             "Omit for all. Combine with commas.",
    ),
    workers:      Optional[int]  = typer.Option(None,  "-j", "--workers"),
    template:     Optional[str]  = typer.Option(None,  "--template"),
) -> None:
    """
    Download an artist's full discography.

    \b
    Examples:
        qobuz artist 999
        qobuz artist https://open.qobuz.com/artist/999
        qobuz artist 999 --type album
        qobuz artist 999 --type album,live -q flac_16
    """
    cfg = _cfg()
    q   = None
    if quality:
        try:
            q = Quality[quality.upper()]
        except KeyError:
            err_console.print(f"[red]Unknown quality:[/red] {quality}")
            raise typer.Exit(code=1)

    sess = _session(cfg)
    try:
        artist_obj = sess.client.get_artist(_resolve_id(url_or_id, "artist"), extras="")
        console.print(
            f"[cyan]Downloading discography:[/cyan] {artist_obj.name}"
            + (f" [dim](type: {release_type})[/dim]" if release_type else "")
        )
    except Exception as exc:
        err_console.print(f"[red]Could not fetch artist:[/red] {exc}")
        raise typer.Exit(code=1)

    total_albums = 0
    total_tracks = 0
    total_failed = 0

    def on_start(title: str, index: int, total: int) -> None:
        console.print(f"  [{index}] {title}")

    def on_done(result) -> None:
        nonlocal total_tracks
        if result.download.skipped and not result.download.dev_stub:
            console.print("    [yellow]skipped[/yellow]")
        else:
            total_tracks += 1

    try:
        results = sess.download_artist_discography(
            url_or_id,
            release_type=release_type,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            template=template,
            on_track_start=on_start,
            on_track_done=on_done,
            workers=workers,
        )
        total_albums = len(results)
        total_failed = sum(r.failed for r in results)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{total_albums} albums, {total_tracks} tracks downloaded."
        + (f"  [yellow]{total_failed} failed.[/yellow]" if total_failed else "")
    )


@app.command()
def favorites(
    type:    str            = typer.Option("tracks", "--type", "-t",
                                           help="tracks or albums"),
    output:  Optional[Path] = typer.Option(None, "-o", "--output"),
    quality: Optional[str]  = typer.Option(None, "-q", "--quality"),
    workers: Optional[int]  = typer.Option(None, "-j", "--workers"),
) -> None:
    """
    Download all favorited tracks or albums.

    \b
    Examples:
        qobuz favorites
        qobuz favorites --type albums
        qobuz favorites --type tracks -q flac_16
    """
    if type not in ("tracks", "albums"):
        err_console.print("[red]--type must be 'tracks' or 'albums'[/red]")
        raise typer.Exit(code=1)

    cfg = _cfg()
    q   = None
    if quality:
        try:
            q = Quality[quality.upper()]
        except KeyError:
            err_console.print(f"[red]Unknown quality:[/red] {quality}")
            raise typer.Exit(code=1)

    sess = _session(cfg)
    console.print(f"[cyan]Downloading favorite {type}…[/cyan]")
    try:
        result = sess.download_favorites(
            type=type,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            on_track_start=_track_start_cb,
            on_track_done=_track_done_cb,
            workers=workers,
        )
    except PoolModeError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{result.succeeded} downloaded, {result.skipped} skipped, {result.failed} failed."
    )


@app.command()
def purchases(
    type:    str            = typer.Option("albums", "--type", "-t",
                                           help="albums or tracks"),
    output:  Optional[Path] = typer.Option(None, "-o", "--output"),
    quality: Optional[str]  = typer.Option(None, "-q", "--quality"),
    workers: Optional[int]  = typer.Option(None, "-j", "--workers"),
) -> None:
    """
    Download all purchased albums or tracks.

    \b
    Examples:
        qobuz purchases
        qobuz purchases --type tracks
        qobuz purchases --type albums -q hi_res
    """
    if type not in ("albums", "tracks"):
        err_console.print("[red]--type must be 'albums' or 'tracks'[/red]")
        raise typer.Exit(code=1)

    cfg = _cfg()
    q   = None
    if quality:
        try:
            q = Quality[quality.upper()]
        except KeyError:
            err_console.print(f"[red]Unknown quality:[/red] {quality}")
            raise typer.Exit(code=1)

    sess = _session(cfg)
    console.print(f"[cyan]Downloading purchased {type}…[/cyan]")
    try:
        result = sess.download_purchases(
            type=type,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            on_track_start=_track_start_cb,
            on_track_done=_track_done_cb,
            workers=workers,
        )
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{result.succeeded} downloaded, {result.skipped} skipped, {result.failed} failed."
    )


@app.command()
def search(
    query:       str  = typer.Argument(...),
    type:        str  = typer.Option("tracks", "--type", "-t"),
    limit:       int  = typer.Option(10, "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Search the Qobuz catalog."""
    valid = {"tracks", "albums", "artists", "playlists"}
    if type not in valid:
        err_console.print(f"[red]Invalid type.[/red] Choose from: {', '.join(sorted(valid))}")
        raise typer.Exit(code=1)

    try:
        results = _session().client.search(query=query, type=type, limit=limit)
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
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Artist")
        table.add_column("Album", style="dim")
        for i in items:
            table.add_row(str(i.get("id", "")), i.get("title", ""),
                          (i.get("performer") or {}).get("name", ""),
                          (i.get("album") or {}).get("title", ""))
    elif type == "albums":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Artist")
        table.add_column("Year", style="dim")
        for i in items:
            table.add_row(str(i.get("id", "")), i.get("title", ""),
                          (i.get("artist") or {}).get("name", ""),
                          (i.get("release_date_original") or "")[:4])
    elif type == "artists":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Albums", style="dim")
        for i in items:
            table.add_row(str(i.get("id", "")), i.get("name", ""),
                          str(i.get("albums_count", "")))
    elif type == "playlists":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Tracks", style="dim")
        table.add_column("Owner")
        for i in items:
            table.add_row(str(i.get("id", "")), i.get("name", ""),
                          str(i.get("tracks_count", "")),
                          (i.get("owner") or {}).get("name", ""))
    console.print(table)


# ── Discovery commands ─────────────────────────────────────────────────────

@app.command("new-releases")
def cmd_new_releases(
    type:     str           = typer.Option("new-releases", "--type", "-t",
                                           help="Feed type: new-releases, press-awards, "
                                                "editor-picks, most-streamed, best-sellers, etc."),
    genre_id: Optional[int] = typer.Option(None, "--genre", help="Filter by genre ID."),
    limit:    int           = typer.Option(25, "-n"),
) -> None:
    """
    Browse new or editorially featured album releases.

    \b
    Examples:
        qobuz new-releases
        qobuz new-releases --type press-awards
        qobuz new-releases --type most-streamed --genre 14
    """
    sess = _session()
    try:
        data = sess.get_new_releases(type=type, genre_id=genre_id, limit=limit)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    albums = data.get("albums", {}).get("items", [])
    if not albums:
        console.print("[dim]No results.[/dim]")
        return

    table = Table(title=f"New releases ({type})", show_lines=False)
    table.add_column("ID",     style="dim", no_wrap=True)
    table.add_column("Title",  style="bold")
    table.add_column("Artist")
    table.add_column("Date",   style="dim")
    for a in albums:
        table.add_row(
            str(a.get("id", "")), a.get("title", ""),
            (a.get("artist") or {}).get("name", ""),
            (a.get("release_date_original") or "")[:10],
        )
    console.print(table)


@app.command("featured")
def cmd_featured(
    type:     str           = typer.Option("editor-picks", "--type", "-t",
                                           help="Curation type: editor-picks, last-created, best-of."),
    genre_id: Optional[int] = typer.Option(None, "--genre", help="Filter by genre ID."),
    limit:    int           = typer.Option(25, "-n"),
) -> None:
    """
    Browse editorially curated playlists.

    \b
    Examples:
        qobuz featured
        qobuz featured --type last-created
        qobuz featured --genre 14
    """
    sess = _session()
    try:
        data = sess.get_featured_playlists(type=type, genre_id=genre_id, limit=limit)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    playlists = data.get("playlists", {}).get("items", [])
    if not playlists:
        console.print("[dim]No results.[/dim]")
        return

    table = Table(title=f"Featured playlists ({type})", show_lines=False)
    table.add_column("ID",     style="dim", no_wrap=True)
    table.add_column("Name",   style="bold")
    table.add_column("Tracks", style="dim")
    table.add_column("Owner")
    for p in playlists:
        table.add_row(
            str(p.get("id", "")), p.get("name", ""),
            str(p.get("tracks_count", "")),
            (p.get("owner") or {}).get("name", ""),
        )
    console.print(table)


@app.command("genres")
def cmd_genres(
    parent: Optional[int] = typer.Option(None, "--parent",
                                         help="Show sub-genres of this genre ID."),
) -> None:
    """
    List Qobuz genres (or sub-genres of a parent).

    \b
    Examples:
        qobuz genres
        qobuz genres --parent 14
    """
    sess = _session()
    try:
        data = sess.get_genres(parent_id=parent)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    genres = data.get("genres", {}).get("items", [])
    if not genres:
        console.print("[dim]No genres found.[/dim]")
        return

    table = Table(title="Genres", show_lines=False)
    table.add_column("ID",   style="dim", width=6)
    table.add_column("Name", style="bold")
    table.add_column("Slug", style="dim")
    for g in genres:
        table.add_row(str(g.get("id", "")), g.get("name", ""), g.get("slug", ""))
    console.print(table)


# ── library subcommands ────────────────────────────────────────────────────

@library_app.command("show")
def library_show(
    type:  str = typer.Option("all", "--type", "-t"),
    limit: int = typer.Option(50, "-n"),
) -> None:
    """Show local favorites."""
    store = _session().store
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
    url_or_id: str  = typer.Argument(...),
    type:      str  = typer.Option("track", "--type", "-t"),
    remote:    bool = typer.Option(False, "--remote/--local-only"),
) -> None:
    """Add a track, album, or artist to local favorites."""
    sess    = _session()
    item_id = _resolve_id(url_or_id)
    try:
        sess.add_favorite(item_id, type, remote=remote)
        console.print(f"[green]Added[/green] {type} {item_id}.")
        if remote:
            console.print("  [dim]Also added to Qobuz account.[/dim]")
    except PoolModeError:
        err_console.print("[red]--remote not available in pool mode.[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)


@library_app.command("remove")
def library_remove(
    url_or_id: str  = typer.Argument(...),
    type:      str  = typer.Option("track", "--type", "-t"),
    remote:    bool = typer.Option(False, "--remote/--local-only"),
) -> None:
    """Remove a track, album, or artist from local favorites."""
    sess    = _session()
    item_id = _resolve_id(url_or_id)
    try:
        removed = sess.remove_favorite(item_id, type, remote=remote)
        if removed:
            console.print(f"[green]Removed[/green] {type} {item_id}.")
        else:
            console.print(f"[yellow]{type} {item_id} was not in local favorites.[/yellow]")
        if remote:
            console.print("  [dim]Also removed from Qobuz account.[/dim]")
    except PoolModeError:
        err_console.print("[red]--remote not available in pool mode.[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)


@library_app.command("sync")
def library_sync(
    type:  str  = typer.Option("all", "--type", "-t"),
    clear: bool = typer.Option(False, "--clear"),
) -> None:
    """Sync favorites from your Qobuz account into the local store."""
    sess = _session()
    try:
        counts = sess.sync_favorites(
            type=None if type == "all" else type,
            clear=clear,
        )
        for t, n in counts.items():
            console.print(f"[green]Synced[/green] {n} {t}(s) → local store.")
    except PoolModeError:
        err_console.print("[red]sync requires a personal session (not pool mode).[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_console.print(f"[red]Sync failed:[/red] {exc}")
        raise typer.Exit(code=1)


@library_app.command("history")
def library_history(
    limit: int  = typer.Option(20, "-n"),
    clear: bool = typer.Option(False, "--clear"),
) -> None:
    """Show or clear the local download/play history."""
    store = _session().store
    if clear:
        console.print(f"[green]Cleared {store.clear_history()} history entries.[/green]")
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


# ── lpl subcommands ────────────────────────────────────────────────────────

@lpl_app.command("list")
def lpl_list() -> None:
    """List all local playlists."""
    store = _session().store
    pls   = store.list_playlists()
    if not pls:
        console.print("[dim]No local playlists yet. Use [bold]qobuz lpl create[/bold].[/dim]")
        return
    table = Table(title="Local playlists", show_lines=False)
    table.add_column("ID",      style="dim", no_wrap=True, max_width=10)
    table.add_column("Name",    style="bold")
    table.add_column("Tracks",  style="dim")
    table.add_column("Updated", style="dim")
    for pl in pls:
        table.add_row(
            pl["id"][:8], pl["name"],
            str(pl.get("track_count", 0)), pl.get("updated_at", "")[:10],
        )
    console.print(table)


@lpl_app.command("create")
def lpl_create(
    name: str = typer.Argument(...),
    desc: str = typer.Option("", "--desc", "-d"),
) -> None:
    """Create a new local playlist."""
    store = _session().store
    pl_id = store.create_playlist(name, desc)
    console.print(f"[green]Created[/green] [bold]{name}[/bold] (id: {pl_id[:8]})")


@lpl_app.command("show")
def lpl_show(name: str = typer.Argument(...)) -> None:
    """Show tracks in a local playlist."""
    store = _session().store
    pl    = store.get_playlist_by_name(name) or next(
        (c for c in store.list_playlists() if c["id"].startswith(name)), None
    )
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {name}")
        raise typer.Exit(code=1)
    tracks = store.get_playlist_tracks(pl["id"])
    console.print(f"[bold]{pl['name']}[/bold]  [dim]{pl.get('description', '')}[/dim]")
    if not tracks:
        console.print("[dim]Empty.[/dim]")
        return
    table = Table(show_lines=False)
    table.add_column("#",      style="dim", width=4)
    table.add_column("ID",     style="dim", no_wrap=True)
    table.add_column("Title",  style="bold")
    table.add_column("Artist")
    for t in tracks:
        table.add_row(
            str(t["position"] + 1), str(t["track_id"]),
            t.get("title", ""), t.get("artist", ""),
        )
    console.print(table)


@lpl_app.command("add")
def lpl_add(
    playlist: str = typer.Argument(..., help="Playlist name or ID prefix"),
    track:    str = typer.Argument(..., help="Track ID or Qobuz URL"),
) -> None:
    """Add a track to a local playlist."""
    sess     = _session()
    track_id = _resolve_id(track, "track")
    store    = sess.store
    pl       = store.get_playlist_by_name(playlist) or next(
        (c for c in store.list_playlists() if c["id"].startswith(playlist)), None
    )
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {playlist}")
        raise typer.Exit(code=1)

    title = artist = album = ""
    duration = 0
    isrc = ""
    try:
        obj      = sess.client.get_track(track_id)
        title    = obj.display_title
        artist   = obj.performer.name if obj.performer else ""
        album    = obj.album.title if obj.album else ""
        duration = obj.duration or 0
        isrc     = obj.isrc or ""
    except Exception:
        pass

    store.add_track_to_playlist(
        pl["id"], track_id,
        title=title, artist=artist, album=album, duration=duration, isrc=isrc,
    )
    console.print(f"[green]Added[/green] {title or track_id} → [bold]{pl['name']}[/bold]")


@lpl_app.command("remove")
def lpl_remove(
    playlist: str = typer.Argument(...),
    track:    str = typer.Argument(...),
) -> None:
    """Remove a track from a local playlist."""
    store = _session().store
    pl    = store.get_playlist_by_name(playlist) or next(
        (c for c in store.list_playlists() if c["id"].startswith(playlist)), None
    )
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {playlist}")
        raise typer.Exit(code=1)
    if store.remove_track_from_playlist(pl["id"], track):
        console.print(f"[green]Removed[/green] track {track} from [bold]{pl['name']}[/bold].")
    else:
        console.print(f"[yellow]Track {track} was not in that playlist.[/yellow]")


@lpl_app.command("delete")
def lpl_delete(
    name:    str  = typer.Argument(...),
    confirm: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete a local playlist."""
    store = _session().store
    pl    = store.get_playlist_by_name(name) or next(
        (c for c in store.list_playlists() if c["id"].startswith(name)), None
    )
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {name}")
        raise typer.Exit(code=1)
    if not confirm:
        typer.confirm(f"Delete playlist '{pl['name']}'?", abort=True)
    store.delete_playlist(pl["id"])
    console.print(f"[green]Deleted[/green] [bold]{pl['name']}[/bold].")


@lpl_app.command("clone")
def lpl_clone(
    url_or_id: str           = typer.Argument(..., help="Qobuz playlist ID or URL"),
    name:      Optional[str] = typer.Option(None, "--name", "-n"),
) -> None:
    """
    Clone a Qobuz playlist into the local store (pagination-proof).

    \b
    Examples:
        qobuz lpl clone 8898080
        qobuz lpl clone https://open.qobuz.com/playlist/8898080
        qobuz lpl clone 8898080 --name "My Copy"
    """
    sess = _session()
    try:
        pl_id = sess.clone_playlist(url_or_id, name=name)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Clone failed:[/red] {exc}")
        raise typer.Exit(code=1)

    pl     = sess.store.get_playlist(pl_id)
    tracks = sess.store.get_playlist_tracks(pl_id)
    console.print(
        f"[green]Cloned[/green] [bold]{pl['name']}[/bold] "
        f"({len(tracks)} tracks saved locally)."
    )
    console.print(f"  Download with: [bold]qobuz lpl download {pl['name']!r}[/bold]")


@lpl_app.command("download")
def lpl_download(
    name:     str            = typer.Argument(..., help="Playlist name or ID prefix"),
    output:   Optional[Path] = typer.Option(None, "-o"),
    quality:  Optional[str]  = typer.Option(None, "-q"),
    workers:  Optional[int]  = typer.Option(None, "-j"),
    template: Optional[str]  = typer.Option(None, "--template"),
) -> None:
    """Download all tracks in a local playlist."""
    cfg = _cfg()
    q   = None
    if quality:
        try:
            q = Quality[quality.upper()]
        except KeyError:
            err_console.print(f"[red]Unknown quality:[/red] {quality}")
            raise typer.Exit(code=1)

    sess = _session(cfg)
    try:
        result = sess.download_local_playlist(
            name,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            template=template,
            on_track_start=_track_start_cb,
            on_track_done=_track_done_cb,
            workers=workers,
        )
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{result.succeeded} downloaded, {result.skipped} skipped, {result.failed} failed."
    )


@lpl_app.command("share")
def lpl_share(
    name:   str            = typer.Argument(...),
    output: Optional[Path] = typer.Option(None, "-o"),
    author: str            = typer.Option("", "--author", "-a"),
) -> None:
    """Export a local playlist as a shareable TOML file."""
    sess = _session()
    try:
        path = sess.share_playlist(name, output=output, author=author)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]Exported[/green] → {path}")


@lpl_app.command("import")
def lpl_import(
    file:      Path = typer.Argument(...),
    overwrite: bool = typer.Option(
        False, "--overwrite/--no-overwrite",
        help="Replace an existing playlist with the same name instead of "
             "appending '(imported)' to the name.",
    ),
) -> None:
    """
    Import a shared TOML playlist file into the local store.

    \b
    Examples:
        qobuz lpl import evening-classical.toml
        qobuz lpl import evening-classical.toml --overwrite
    """
    if not file.exists():
        err_console.print(f"[red]File not found:[/red] {file}")
        raise typer.Exit(code=1)
    sess = _session()
    try:
        pl_id = sess.import_playlist(file, overwrite=overwrite)
    except Exception as exc:
        err_console.print(f"[red]Import failed:[/red] {exc}")
        raise typer.Exit(code=1)
    pl     = sess.store.get_playlist(pl_id)
    tracks = sess.store.get_playlist_tracks(pl_id)
    console.print(f"[green]Imported[/green] [bold]{pl['name']}[/bold] ({len(tracks)} tracks)")


# ── export subcommands ─────────────────────────────────────────────────────

@export_app.command("backup")
def export_backup(
    output: Optional[Path] = typer.Option(None, "-o"),
) -> None:
    """Create a full .tar.gz backup of your local library."""
    sess = _session()
    try:
        path = sess.backup(output)
    except Exception as exc:
        err_console.print(f"[red]Backup failed:[/red] {exc}")
        raise typer.Exit(code=1)
    size_kb = path.stat().st_size / 1024
    console.print(f"[green]Backup saved:[/green] {path}  [dim]({size_kb:.1f} KB)[/dim]")


@export_app.command("favorites")
def export_favorites(
    output: Optional[Path] = typer.Option(None, "-o"),
    type:   str            = typer.Option("all", "--type", "-t"),
) -> None:
    """Export local favorites to a TOML file."""
    sess = _session()
    try:
        path = sess.export_favorites(output, type=None if type == "all" else type)
    except Exception as exc:
        err_console.print(f"[red]Export failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Favorites exported:[/green] {path}")


@export_app.command("playlist")
def export_playlist(
    name:   str            = typer.Argument(...),
    output: Optional[Path] = typer.Option(None, "-o"),
    author: str            = typer.Option("", "--author", "-a"),
) -> None:
    """Export a local playlist to a shareable TOML file."""
    lpl_share(name=name, output=output, author=author)


@export_app.command("import-favorites")
def export_import_favorites(
    file:     Path = typer.Argument(..., help="Path to a favorites TOML export file"),
    no_merge: bool = typer.Option(
        False, "--replace/--merge",
        help="--replace clears each type present in the file before importing. "
             "--merge (default) upserts without removing existing records.",
    ),
) -> None:
    """
    Import favorites from a TOML file created by 'export favorites'.

    \b
    Examples:
        qobuz export import-favorites favorites-2026-03-15.toml
        qobuz export import-favorites favorites-2026-03-15.toml --replace
    """
    if not file.exists():
        err_console.print(f"[red]File not found:[/red] {file}")
        raise typer.Exit(code=1)
    sess = _session()
    try:
        n = sess.import_favorites(file, merge=not no_merge)
    except ValueError as exc:
        err_console.print(f"[red]Invalid file:[/red] {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_console.print(f"[red]Import failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Imported[/green] {n} favorite(s) from {file.name}.")


@export_app.command("restore")
def export_restore(
    archive:       Path           = typer.Argument(..., help="Path to a .tar.gz backup archive"),
    favorites:     bool           = typer.Option(True,  "--favorites/--no-favorites",
                                                 help="Restore favorites from the archive."),
    playlists:     bool           = typer.Option(True,  "--playlists/--no-playlists",
                                                 help="Restore playlists from the archive."),
    db:            bool           = typer.Option(False, "--db",
                                                 help="Full DB replacement (destructive). "
                                                      "Re-open the app after this."),
    replace:       bool           = typer.Option(False, "--replace/--merge",
                                                 help="--replace clears existing data before "
                                                      "restoring. --merge (default) upserts."),
    playlists_dir: Optional[Path] = typer.Option(
        None, "--playlists-dir",
        help="Also extract playlist TOML files to this directory.",
    ),
) -> None:
    """
    Restore from a backup archive created by 'export backup'.

    By default favorites and playlists are merged into the live store so
    existing data is preserved. Use --replace to clear before restoring.
    Use --db for a full atomic database swap (everything is replaced).

    \b
    Examples:
        qobuz export restore qobuz-backup-2026-03-15.tar.gz
        qobuz export restore backup.tar.gz --no-favorites
        qobuz export restore backup.tar.gz --replace
        qobuz export restore backup.tar.gz --db
        qobuz export restore backup.tar.gz --playlists-dir ~/Music/playlists
    """
    if not archive.exists():
        err_console.print(f"[red]File not found:[/red] {archive}")
        raise typer.Exit(code=1)

    sess = _session()
    try:
        result = sess.restore(
            archive,
            restore_favorites=favorites,
            restore_playlists=playlists,
            restore_db=db,
            merge=not replace,
        )
        # Also honour --playlists-dir if given
        if playlists_dir and not db:
            from .local.export import restore_from_tar
            restore_from_tar(
                sess.store, archive,
                restore_favorites=False,
                restore_playlists=True,
                merge=not replace,
                playlists_dir=playlists_dir,
            )
    except (FileNotFoundError, ValueError) as exc:
        err_console.print(f"[red]Restore failed:[/red] {exc}")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_console.print(f"[red]Restore failed:[/red] {exc}")
        raise typer.Exit(code=1)

    if result.db_restored:
        console.print("[green]Database restored.[/green] Please re-open the application.")
        return

    console.print(
        f"[green]Restore complete.[/green]  "
        f"Favorites: {result.favorites_imported}  "
        f"Playlists: {result.playlists_imported} imported, "
        f"{result.playlists_skipped} skipped."
    )
    if result.errors:
        err_console.print(f"[yellow]{len(result.errors)} non-fatal error(s):[/yellow]")
        for e in result.errors:
            err_console.print(f"  [dim]{e}[/dim]")


# ── account subcommands ────────────────────────────────────────────────────

@account_app.command("show")
def account_show(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """
    Show your Qobuz account profile and subscription tier.

    \b
    Examples:
        qobuz account show
        qobuz account show --json
    """
    sess = _session()
    try:
        profile = sess.get_profile()
    except Exception as exc:
        err_console.print(f"[red]Failed to fetch profile:[/red] {exc}")
        raise typer.Exit(code=1)

    if json_output:
        import json as _json
        console.print(_json.dumps(profile._raw, indent=2, ensure_ascii=False))
        return

    table = Table(title="Account profile", show_lines=False, box=None)
    table.add_column("Field", style="dim", width=20)
    table.add_column("Value", style="bold")
    for label, value in [
        ("ID",           str(profile.id)),
        ("Login",        profile.login),
        ("Email",        profile.email or "—"),
        ("Name",         profile.full_name),
        ("Display name", profile.display_name or "—"),
        ("Country",      profile.country_code or "—"),
        ("Language",     profile.language_code or "—"),
        ("Newsletter",   "yes" if profile.newsletter else "no"),
        ("Created",      profile.creation_date or "—"),
    ]:
        table.add_row(label, value)
    console.print(table)

    if profile.credential:
        cred = profile.credential
        console.print()
        sub = Table(title="Subscription", show_lines=False, box=None)
        sub.add_column("Field", style="dim", width=20)
        sub.add_column("Value", style="bold")
        sub.add_row("Plan",              cred.label)
        sub.add_row("Description",       cred.description)
        sub.add_row("Max quality",       cred.max_audio_quality)
        sub.add_row("Hi-res streaming",  _yn(cred.hires_streaming))
        sub.add_row("Mobile streaming",  _yn(cred.mobile_streaming))
        sub.add_row("Offline listening", _yn(cred.offline_listening))
        console.print(sub)


@account_app.command("update")
def account_update(
    email:        Optional[str]  = typer.Option(None, "--email",
                                                help="New email address."),
    firstname:    Optional[str]  = typer.Option(None, "--firstname",
                                                help="Given name."),
    lastname:     Optional[str]  = typer.Option(None, "--lastname",
                                                help="Family name."),
    display_name: Optional[str]  = typer.Option(None, "--display-name",
                                                help="Public display name."),
    country:      Optional[str]  = typer.Option(None, "--country",
                                                help="ISO 3166-1 alpha-2 country code, e.g. GB."),
    language:     Optional[str]  = typer.Option(None, "--language",
                                                help="Preferred language code, e.g. en."),
    newsletter:   Optional[bool] = typer.Option(None, "--newsletter/--no-newsletter",
                                                help="Subscribe or unsubscribe from the newsletter."),
) -> None:
    """
    Update your Qobuz account profile fields.

    \b
    Examples:
        qobuz account update --firstname Alice --lastname Doe
        qobuz account update --email newemail@example.com
        qobuz account update --country GB --language en
        qobuz account update --no-newsletter
    """
    if not any([email, firstname, lastname, display_name, country, language,
                newsletter is not None]):
        err_console.print("[red]Provide at least one field to update.[/red]")
        raise typer.Exit(code=1)

    sess = _session()
    try:
        profile = sess.update_profile(
            email=email,
            firstname=firstname,
            lastname=lastname,
            display_name=display_name,
            country_code=country,
            language_code=language,
            newsletter=newsletter,
        )
    except Exception as exc:
        err_console.print(f"[red]Update failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Profile updated.[/green] "
        f"Name: {profile.full_name}  Email: {profile.email or '—'}"
    )


@account_app.command("password")
def account_password(
    current: Optional[str] = typer.Option(None, "--current", hide_input=True,
                                           help="Current password (prompted if omitted)."),
    new:     Optional[str] = typer.Option(None, "--new",     hide_input=True,
                                           help="New password (prompted if omitted)."),
    confirm: Optional[str] = typer.Option(None, "--confirm", hide_input=True,
                                           help="Confirm new password (prompted if omitted)."),
) -> None:
    """
    Change your Qobuz account password.

    \b
    Example:
        qobuz account password
    """
    if current is None:
        current = typer.prompt("Current password", hide_input=True)
    if new is None:
        new = typer.prompt("New password", hide_input=True)
    if confirm is None:
        confirm = typer.prompt("Confirm new password", hide_input=True)

    if new != confirm:
        err_console.print("[red]New passwords do not match.[/red]")
        raise typer.Exit(code=1)

    sess = _session()
    try:
        sess.change_password(current, new)
    except Exception as exc:
        err_console.print(f"[red]Password change failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        "[green]Password changed successfully.[/green] "
        "You may need to log in again on other devices."
    )


@account_app.command("subscription")
def account_subscription() -> None:
    """Show your current subscription tier and capabilities."""
    sess = _session()
    try:
        profile = sess.get_profile()
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    if not profile.credential:
        console.print("[yellow]No subscription information available.[/yellow]")
        return

    cred  = profile.credential
    table = Table(title="Subscription", show_lines=False, box=None)
    table.add_column("", style="dim", width=28)
    table.add_column("", style="bold")
    table.add_row("Plan",                   cred.label)
    table.add_row("Description",            cred.description)
    table.add_row("Max quality",            cred.max_audio_quality)
    table.add_row("Lossy streaming",        _yn(cred.lossy_streaming))
    table.add_row("Lossless streaming",     _yn(cred.lossless_streaming))
    table.add_row("Hi-res streaming",       _yn(cred.hires_streaming))
    table.add_row("Hi-res purchases",       _yn(cred.hires_purchases_streaming))
    table.add_row("Mobile streaming",       _yn(cred.mobile_streaming))
    table.add_row("Offline listening",      _yn(cred.offline_listening))
    console.print(table)


# ── remote subcommands ─────────────────────────────────────────────────────

@remote_app.command("list")
def remote_list(
    limit: int = typer.Option(50, "-n"),
) -> None:
    """List playlists on your Qobuz account."""
    sess = _session()
    try:
        pls = list(sess.client.iter_user_playlists(page_size=limit))
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    if not pls:
        console.print("[dim]No playlists on your Qobuz account.[/dim]")
        return

    table = Table(title="Remote playlists", show_lines=False)
    table.add_column("ID",      style="dim", no_wrap=True)
    table.add_column("Name",    style="bold")
    table.add_column("Tracks",  style="dim")
    table.add_column("Public",  style="dim")
    table.add_column("Owner",   style="dim")
    for pl in pls:
        table.add_row(
            str(pl.id), pl.name, str(pl.tracks_count),
            "yes" if pl.is_public else "no",
            pl.owner.name if pl.owner else "—",
        )
    console.print(table)


@remote_app.command("show")
def remote_show(
    playlist_id: str = typer.Argument(...),
    limit:       int = typer.Option(50, "-n"),
) -> None:
    """Show tracks in a Qobuz account playlist (with playlist_track_id column)."""
    sess = _session()
    try:
        pl = sess.client.get_playlist(playlist_id, limit=limit)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"[bold]{pl.name}[/bold]  "
        f"[dim]{pl.tracks_count} tracks · "
        f"{'public' if pl.is_public else 'private'} · "
        f"owner: {pl.owner.name}[/dim]"
    )
    if pl.description:
        console.print(f"[dim]{pl.description}[/dim]")
    if not pl.tracks or not pl.tracks.items:
        console.print("[dim]Empty.[/dim]")
        return

    table = Table(show_lines=False)
    table.add_column("Pos",      style="dim", width=4)
    table.add_column("PT-ID",    style="dim", no_wrap=True, width=10,
                     header_style="dim",
                     footer="Use PT-ID with 'qobuz remote remove'")
    table.add_column("Track ID", style="dim", no_wrap=True)
    table.add_column("Title",    style="bold")
    table.add_column("Artist")
    for t in pl.tracks.items:
        table.add_row(
            str(t.position or "—"),
            str(t.playlist_track_id or "—"),
            str(t.id),
            t.title,
            t.performer.name if t.performer else "—",
        )
    console.print(table)


@remote_app.command("create")
def remote_create(
    name:          str  = typer.Argument(...),
    desc:          str  = typer.Option("",    "--desc",            "-d"),
    public:        bool = typer.Option(False, "--public/--private",
                                       help="Make playlist publicly visible."),
    collaborative: bool = typer.Option(False, "--collaborative/--no-collaborative"),
    save_locally:  bool = typer.Option(True,  "--save-locally/--no-save-locally",
                                       help="Also create a matching local playlist."),
) -> None:
    """
    Create a new playlist on your Qobuz account.

    \b
    Examples:
        qobuz remote create "Evening Classical"
        qobuz remote create "Shared Mix" --public --collaborative
    """
    sess = _session()
    try:
        pl = sess.create_remote_playlist(
            name=name, description=desc,
            is_public=public, is_collaborative=collaborative,
            also_save_locally=save_locally,
        )
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Created[/green] remote playlist [bold]{pl.name}[/bold] (id: {pl.id})")
    if save_locally:
        console.print("  [dim]Also saved to local store.[/dim]")


@remote_app.command("update")
def remote_update(
    playlist_id:   str           = typer.Argument(..., help="Playlist ID"),
    name:          Optional[str] = typer.Option(None, "--name",          "-n"),
    desc:          Optional[str] = typer.Option(None, "--desc",          "-d"),
    public:        Optional[bool]= typer.Option(None, "--public/--private"),
    collaborative: Optional[bool]= typer.Option(None, "--collaborative/--no-collaborative"),
) -> None:
    """
    Update a Qobuz playlist's metadata.

    \b
    Examples:
        qobuz remote update 12345 --name "New Name"
        qobuz remote update 12345 --public
    """
    if not any([name, desc, public is not None, collaborative is not None]):
        err_console.print("[red]Provide at least one field to update.[/red]")
        raise typer.Exit(code=1)
    sess = _session()
    try:
        pl = sess.update_remote_playlist(
            playlist_id, name=name, description=desc,
            is_public=public, is_collaborative=collaborative,
        )
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Updated[/green] [bold]{pl.name}[/bold].")


@remote_app.command("delete")
def remote_delete(
    playlist_id: str  = typer.Argument(...),
    confirm:     bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Permanently delete a playlist from your Qobuz account."""
    if not confirm:
        typer.confirm(
            f"Delete remote playlist {playlist_id!r}? This cannot be undone.", abort=True
        )
    sess = _session()
    try:
        sess.delete_remote_playlist(playlist_id)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Deleted[/green] remote playlist {playlist_id}.")


@remote_app.command("add")
def remote_add(
    playlist_id: str       = typer.Argument(..., help="Playlist ID"),
    track_ids:   list[str] = typer.Argument(..., help="One or more track IDs or URLs"),
    duplicates:  bool      = typer.Option(False, "--allow-duplicates"),
) -> None:
    """
    Add one or more tracks to a Qobuz playlist.

    \b
    Example:
        qobuz remote add 12345 111111 222222 333333
    """
    resolved = [_resolve_id(t, "track") for t in track_ids]
    sess = _session()
    try:
        sess.add_tracks_to_remote_playlist(
            playlist_id, resolved, no_duplicate=not duplicates,
        )
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Added[/green] {len(resolved)} track(s) to playlist {playlist_id}.")


@remote_app.command("remove")
def remote_remove(
    playlist_id:        str       = typer.Argument(..., help="Playlist ID"),
    playlist_track_ids: list[int] = typer.Argument(
        ...,
        help="playlist_track_id value(s) from 'qobuz remote show' — NOT track IDs.",
    ),
) -> None:
    """
    Remove tracks from a Qobuz playlist by playlist_track_id.

    The PT-ID column in 'qobuz remote show' gives the playlist_track_id.
    This is the join-table row identifier, not the track ID.

    \b
    Example:
        qobuz remote remove 12345 9901 9902
    """
    sess = _session()
    try:
        sess.remove_tracks_from_remote_playlist(playlist_id, playlist_track_ids)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(
        f"[green]Removed[/green] {len(playlist_track_ids)} track(s) "
        f"from playlist {playlist_id}."
    )


@remote_app.command("follow")
def remote_follow(
    playlist_id: str = typer.Argument(..., help="Playlist ID to follow"),
) -> None:
    """Follow a public Qobuz playlist so it appears in your library."""
    sess = _session()
    try:
        sess.follow_playlist(playlist_id)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Following[/green] playlist {playlist_id}.")


@remote_app.command("unfollow")
def remote_unfollow(
    playlist_id: str  = typer.Argument(...),
    confirm:     bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Unfollow a Qobuz playlist."""
    if not confirm:
        typer.confirm(f"Unfollow playlist {playlist_id!r}?", abort=True)
    sess = _session()
    try:
        sess.unfollow_playlist(playlist_id)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)
    console.print(f"[green]Unfollowed[/green] playlist {playlist_id}.")


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()

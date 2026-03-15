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

# ── Apps ───────────────────────────────────────────────────────────────────

app         = typer.Typer(name="qobuz", add_completion=False,
                          help="Unofficial Qobuz CLI.")
library_app = typer.Typer(help="Manage local and remote favourites.")
lpl_app     = typer.Typer(help="Create, manage, and share local playlists.")
export_app  = typer.Typer(help="Export and back up your library.")

app.add_typer(library_app, name="library")
app.add_typer(lpl_app,     name="lpl")
app.add_typer(export_app,  name="export")

console     = Console()
err_console = Console(stderr=True)
_dev: bool  = False


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


# ── Helpers ────────────────────────────────────────────────────────────────

def _cfg() -> QobuzConfig:
    return load_config()


def _session(cfg: Optional[QobuzConfig] = None) -> QobuzSession:
    cfg = cfg or _cfg()
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
    console.print(f"Cache: [bold]{CACHE_DIR}[/bold]  ({len(files)} files"
                  + (f", {sum(f.stat().st_size for f in files)/1024:.1f} KB)" if files else ")"))


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
            table.add_row(str(i.get("id","")), i.get("title",""),
                          (i.get("performer") or {}).get("name",""),
                          (i.get("album") or {}).get("title",""))
    elif type == "albums":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Artist")
        table.add_column("Year", style="dim")
        for i in items:
            table.add_row(str(i.get("id","")), i.get("title",""),
                          (i.get("artist") or {}).get("name",""),
                          (i.get("release_date_original") or "")[:4])
    elif type == "artists":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Albums", style="dim")
        for i in items:
            table.add_row(str(i.get("id","")), i.get("name",""), str(i.get("albums_count","")))
    elif type == "playlists":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Tracks", style="dim")
        table.add_column("Owner")
        for i in items:
            table.add_row(str(i.get("id","")), i.get("name",""),
                          str(i.get("tracks_count","")),
                          (i.get("owner") or {}).get("name",""))
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
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Artist")
        if t == "track":
            table.add_column("Album", style="dim")
        for item in items:
            row = [item.get("id",""), item.get("title",""), item.get("artist","")]
            if t == "track":
                row.append(item.get("extra",""))
            table.add_row(*row)
        console.print(table)


@library_app.command("add")
def library_add(
    url_or_id: str = typer.Argument(...),
    type:      str = typer.Option("track", "--type", "-t"),
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
            row.get("played_at","")[:16],
            row.get("title",""),
            row.get("artist",""),
            row.get("album",""),
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
        table.add_row(pl["id"][:8], pl["name"],
                      str(pl.get("track_count",0)), pl.get("updated_at","")[:10])
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
    console.print(f"[bold]{pl['name']}[/bold]  [dim]{pl.get('description','')}[/dim]")
    if not tracks:
        console.print("[dim]Empty.[/dim]")
        return
    table = Table(show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Title", style="bold")
    table.add_column("Artist")
    for t in tracks:
        table.add_row(str(t["position"]+1), str(t["track_id"]),
                      t.get("title",""), t.get("artist",""))
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

    store.add_track_to_playlist(pl["id"], track_id,
        title=title, artist=artist, album=album, duration=duration, isrc=isrc)
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

    pl = sess.store.get_playlist(pl_id)
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
def lpl_import(file: Path = typer.Argument(...)) -> None:
    """Import a shared TOML playlist file into the local store."""
    if not file.exists():
        err_console.print(f"[red]File not found:[/red] {file}")
        raise typer.Exit(code=1)
    sess = _session()
    try:
        pl_id = sess.import_playlist(file)
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


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()

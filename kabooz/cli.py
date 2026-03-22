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
        "Run: pip install 'kabooz[cli]'",
        file=sys.stderr,
    )
    sys.exit(1)

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
from .session import QobuzSession, _parse_quality
from .url import parse_url

# ── Sub-apps ───────────────────────────────────────────────────────────────

app         = typer.Typer(name="kabooz", add_completion=False,
                          help="Unofficial Qobuz CLI.")
library_app = typer.Typer(help="Manage local and remote favourites.")
lpl_app     = typer.Typer(help="Create, manage, and share local playlists.")
export_app  = typer.Typer(help="Export, import, and back up your library.")
account_app = typer.Typer(help="View and update your Qobuz account profile.")
remote_app  = typer.Typer(help="Manage playlists on your Qobuz account.")

app.add_typer(library_app, name="library")
app.add_typer(lpl_app,     name="lpl")
app.add_typer(export_app,  name="export")
app.add_typer(account_app, name="account")
app.add_typer(remote_app,  name="remote")

console     = Console()
err_console = Console(stderr=True)
_dev: bool  = False


# ══════════════════════════════════════════════════════════════════════════
# Presentation helpers — display concerns only, no business logic
# ══════════════════════════════════════════════════════════════════════════

def _quality(s: Optional[str]) -> Optional[Quality]:
    """
    Parse a quality flag string. Exits with a helpful error on bad input.
    Single source of truth — never duplicate this try/except in commands.
    """
    if not s:
        return None
    try:
        return _parse_quality(s)
    except (ValueError, ConfigError) as exc:
        err_console.print(f"[red]Invalid quality:[/red] {exc}")
        raise typer.Exit(code=1)


def _resolve_id(url_or_id: str, expected_type: Optional[str] = None) -> str:
    """Parse a Qobuz URL or bare ID and return the entity ID string."""
    if url_or_id.startswith("http"):
        try:
            entity_type, entity_id = parse_url(url_or_id)
        except ValueError as exc:
            err_console.print(f"[red]Invalid URL:[/red] {exc}")
            raise typer.Exit(code=1)
        if expected_type and entity_type != expected_type:
            err_console.print(
                f"[red]Expected a {expected_type} URL but got {entity_type}.[/red]"
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


def _yn(v: bool) -> str:
    return "[green]yes[/green]" if v else "[dim]no[/dim]"


def _on_track_start(title: str, index: int, total: int) -> None:
    pad = f"/{total}" if total else ""
    console.print(f"  [{index}{pad}] {title}")


def _on_track_done(result) -> None:
    if result.download.skipped and not result.download.dev_stub:
        console.print("    [yellow]Already complete — skipped.[/yellow]")


def _on_album_start(title: str, index: int, total: int) -> None:
    console.print(
        f"\n[bold cyan][{index}/{total}][/bold cyan]  [bold]{title}[/bold]"
    )


# ── Global callback ────────────────────────────────────────────────────────


def _version_callback(value: bool) -> None:
    if value:
        try:
            from importlib.metadata import version
            v = version("kabooz")
        except Exception:
            v = "0.1.0"
        console.print(f"[bold]Kabooz[/bold] v{v}")
        raise typer.Exit()


def _credits_callback(value: bool) -> None:
    if not value:
        return
    from rich.panel import Panel
    lines = [
        "[bold]Kabooz[/bold] — unofficial Qobuz client\n",
        "[bold cyan]A word on Qobuz[/bold cyan]",
        "  This tool exists because Qobuz is genuinely one of the best",
        "  things to happen to music. Hi-res lossless, a serious catalog,",
        "  no ads, fair artist payouts — it deserves your subscription.",
        "  [dim]https://www.qobuz.com[/dim]\n",
        "[bold cyan]Technical thanks[/bold cyan]",
        "  [bold]tmxkwpn[/bold]  (@tmxkwpn on Telegram) — technical help and guidance\n",
        "[bold cyan]Services used[/bold cyan]",
        "  [bold]MusicBrainz[/bold]   musicbrainz.org — open music encyclopedia,",
        "                    used for ISRC lookup and tag enrichment",
        "  [bold]LRCLIB[/bold]        lrclib.net — free, open synced lyrics database\n",
        "[bold cyan]Built with[/bold cyan]",
        "  [bold]httpx[/bold]         HTTP client            github.com/encode/httpx",
        "  [bold]mutagen[/bold]       audio metadata         github.com/quodlibet/mutagen",
        "  [bold]Typer[/bold]         CLI framework          github.com/tiangolo/typer",
        "  [bold]Rich[/bold]          terminal formatting    github.com/Textualize/rich",
        "  [bold]tomli-w[/bold]       TOML writer            github.com/hukkin/tomli-w\n",
        "[bold cyan]Tinker, extend, improve[/bold cyan]",
        "  Kabooz is intentionally designed to be hackable. The business",
        "  logic lives entirely in session.py, models are plain dataclasses,",
        "  and the CLI is a thin presentation layer over the library.",
        "  If something is missing, slow, or broken — please add it.",
        "  PRs, forks, and feature branches are all welcome.\n",
        "[bold cyan]License[/bold cyan]",
        "  AGPL-3.0-or-later\n",
        "[dim]Kabooz is not affiliated with, endorsed by, or connected",
        "to Qobuz or any of the services listed above.[/dim]",
    ]
    console.print(Panel("\n".join(lines), expand=False, border_style="cyan"))
    raise typer.Exit()

@app.callback()
def main(
    dev: bool = typer.Option(
        False, "--dev", envvar="KABOOZ_DEV", is_eager=True,
        help="Developer mode: cache API responses, write dev audio.",
    ),
    version: bool = typer.Option(
        False, "--version", "-V", is_eager=True,
        callback=_version_callback, expose_value=False,
        help="Show version and exit.",
    ),
    credits: bool = typer.Option(
        False, "--credits", is_eager=True,
        callback=_credits_callback, expose_value=False,
        help="Show credits and exit.",
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


# ── Shared session factory ─────────────────────────────────────────────────

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


# ══════════════════════════════════════════════════════════════════════════
# Core commands
# ══════════════════════════════════════════════════════════════════════════

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
    Authenticate with Qobuz.

    \b
    1. Username + password:  qobuz login -u me@example.com -p secret
    2. Direct token:         qobuz login --token TOKEN --user-id 12345
    3. Token pool:           qobuz login --pool ~/.config/qobuz/pool.txt
    """
    from .client import QobuzClient
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
            s      = QobuzSession.from_client(client, cfg)
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
    show: bool            = typer.Option(False, "--show"),
    set_: Optional[str]   = typer.Option(None, "--set",
        help="section.key=value  e.g. download.max_workers=4"),
) -> None:
    """
    View or update the configuration file.

    \b
    Examples:
        kabooz config --show
        kabooz config --set download.max_workers=4
        kabooz config --set streaming.report_streams=false
        kabooz config --set download.quality=flac_16
    """
    if show:
        import dataclasses, json
        console.print_json(json.dumps(dataclasses.asdict(_cfg()), indent=2))
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


# ══════════════════════════════════════════════════════════════════════════
# Download commands
# ══════════════════════════════════════════════════════════════════════════

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
    cfg  = _cfg()
    q    = _quality(quality)
    sess = _session(cfg)

    with _make_progress() as prog:
        task = prog.add_task("[dim]Downloading…[/dim]", total=None)

        def on_progress(written: int, total: int) -> None:
            prog.update(task, completed=written, total=total or None)

        try:
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

    dl = result.download
    if dl.skipped and not dl.dev_stub:
        console.print(f"[yellow]Skipped[/yellow] (already complete): {dl.path}")
    else:
        suffix = ""
        if result.lyrics_found:
            suffix += "  [dim]lyrics embedded[/dim]"
        console.print(f"[green]Done:[/green] {dl.path}{suffix}")


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
    cfg  = _cfg()
    q    = _quality(quality)
    sess = _session(cfg)

    # current_task holds the Rich task ID for the active track's progress bar.
    # We use a list as a mutable container so the closures below can mutate it.
    current_task: list = []

    with _make_progress() as prog:

        def on_progress(written: int, total: int) -> None:
            if current_task:
                prog.update(current_task[0], completed=written, total=total or None)

        def on_track_start(title: str, index: int, total_tracks: int) -> None:
            _on_track_start(title, index, total_tracks)
            # Replace the progress bar for each new track.
            if current_task:
                prog.remove_task(current_task[0])
                current_task.clear()
            current_task.append(
                prog.add_task(f"  [dim]{title[:60]}[/dim]", total=None)
            )

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
                on_track_start=on_track_start,
                on_track_done=_on_track_done,
                on_progress=on_progress,
                workers=workers,
            )
        except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
            _handle_auth_error(exc)
        except APIError as exc:
            err_console.print(f"[red]API error:[/red] {exc}")
            raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{result.succeeded} downloaded, {result.skipped} skipped"
        + (f", [yellow]{result.failed} failed[/yellow]" if result.failed else "") + "."
    )


# ══════════════════════════════════════════════════════════════════════════
# Metadata and goodies
# ══════════════════════════════════════════════════════════════════════════

def _fmt_dur(seconds: int) -> str:
    """Format an integer number of seconds as m:ss or h:mm:ss."""
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


@app.command()
def info(
    url_or_id:   str           = typer.Argument(..., help="Qobuz URL or bare ID"),
    info_type:   Optional[str] = typer.Option(
        None, "--type", "-t",
        help="Entity type when passing a bare ID: album | track | artist",
    ),
    json_output: bool          = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """
    Display metadata for an album, track, or artist — no download.

    Entity type is auto-detected from Qobuz URLs. When passing a bare
    numeric ID, use --type to specify album, track, or artist.

    Examples
    ────────
      kabooz info https://open.qobuz.com/album/0093046758769
      kabooz info 0093046758769 --type album
      kabooz info https://open.qobuz.com/track/12345
    """
    from rich.panel import Panel

    # ── Resolve entity type and ID ─────────────────────────────────────────
    if url_or_id.startswith("http"):
        try:
            entity_type, entity_id = parse_url(url_or_id)
        except ValueError as exc:
            err_console.print(f"[red]Invalid URL:[/red] {exc}")
            raise typer.Exit(code=1)
    else:
        if not info_type:
            err_console.print(
                "[red]Bare IDs require --type.[/red]  "
                "Use: --type album | track | artist"
            )
            raise typer.Exit(code=1)
        entity_type = info_type.lower()
        entity_id   = url_or_id

    sess = _session()

    # ── Fetch ──────────────────────────────────────────────────────────────
    try:
        if entity_type == "album":
            obj = sess.get_album(entity_id, limit=500)
        elif entity_type == "track":
            obj = sess.get_track(entity_id)
        elif entity_type == "artist":
            obj = sess.get_artist(entity_id, extras="albums", limit=50)
        else:
            err_console.print(
                f"[red]Unknown type:[/red] {entity_type!r}. "
                "Use album, track, or artist."
            )
            raise typer.Exit(code=1)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except NotFoundError:
        err_console.print(f"[red]Not found:[/red] {entity_id}")
        raise typer.Exit(code=1)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    if json_output:
        import json, dataclasses
        console.print_json(json.dumps(dataclasses.asdict(obj), indent=2, default=str))
        return

    # ── Display ────────────────────────────────────────────────────────────
    if entity_type == "album":
        _display_album_info(obj)
    elif entity_type == "track":
        _display_track_info(obj)
    elif entity_type == "artist":
        _display_artist_info(obj)


def _display_album_info(album) -> None:
    from rich.panel import Panel
    from rich.text  import Text

    # ── Header panel ───────────────────────────────────────────────────────
    artist = album.artist.name if album.artist else ""
    label  = album.label.name  if album.label  else ""
    genre  = album.genre.name  if album.genre  else (
        album.genres_list[0] if album.genres_list else ""
    )
    year   = album.release_date_original[:4] if album.release_date_original else ""

    from .download.naming import quality_tag
    fmt = quality_tag(album.maximum_bit_depth, album.maximum_sampling_rate)

    # Build subtitle line: artist · genre · year · label
    meta_parts = [p for p in [artist, genre, year, label] if p]
    meta_line  = "  ·  ".join(meta_parts)

    flags = []
    if album.hires:            flags.append("Hi-Res")
    if album.hires_streamable: flags.append("Hi-Res Stream")
    if album.downloadable:     flags.append("DL")
    if album.parental_warning: flags.append("Explicit")
    flag_line = "  ".join(f"[dim]{f}[/dim]" for f in flags)

    goodies_line = ""
    if album.goodies:
        names = ", ".join(g.name or "file" for g in album.goodies)
        goodies_line = f"\n  [bold]Goodies:[/bold] {len(album.goodies)} — {names}"

    upc_line = f"  UPC [dim]{album.upc}[/dim]" if album.upc else ""

    header = (
        f"[bold]{album.display_title}[/bold]\n"
        f"  {meta_line}\n"
        f"  [cyan]{fmt}[/cyan]  ·  "
        f"{album.tracks_count} track{'s' if album.tracks_count != 1 else ''}  ·  "
        f"{_fmt_dur(album.duration)}"
        + (f"\n  {flag_line}" if flag_line else "")
        + (f"\n{upc_line}" if upc_line else "")
        + goodies_line
    )
    console.print(Panel(header, expand=False, border_style="cyan"))

    # ── Track listing ──────────────────────────────────────────────────────
    if not album.tracks or not album.tracks.items:
        console.print("[dim]No track data available.[/dim]")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("#",      style="dim",  no_wrap=True, width=4)
    table.add_column("Title",  style="bold", ratio=4)
    table.add_column("Dur",    style="dim",  no_wrap=True, width=6)
    table.add_column("ISRC",   style="dim",  no_wrap=True, width=14)

    for t in album.tracks.items:
        num  = f"{t.track_number:02d}"
        if album.media_count and album.media_count > 1:
            num = f"{t.media_number}-{t.track_number:02d}"
        table.add_row(
            num,
            t.display_title,
            _fmt_dur(t.duration),
            t.isrc or "",
        )

    console.print(table)
    console.print(
        f"\n[dim]ID: {album.id}"
        + (f"  ·  Released: {album.release_date_original}" if album.release_date_original else "")
        + "[/dim]"
    )


def _display_track_info(track) -> None:
    from rich.panel import Panel
    from .download.naming import quality_tag

    artist  = track.performer.name if track.performer else ""
    composer = track.composer.name if track.composer  else ""
    album_t = ""
    fmt     = ""
    if track.album:
        album_t = track.album.display_title
        fmt = quality_tag(track.album.maximum_bit_depth, track.album.maximum_sampling_rate)

    lines = [f"[bold]{track.display_title}[/bold]"]
    if artist:
        lines.append(f"  {artist}")
    if composer and composer != artist:
        lines.append(f"  [dim]Composer:[/dim] {composer}")
    if track.work:
        lines.append(f"  [dim]Work:[/dim] {track.work}")
    if album_t:
        lines.append(f"  [dim]Album:[/dim] {album_t}")

    detail_parts = []
    if track.track_number:
        disc = f"Disc {track.media_number}  " if track.media_number and track.media_number > 1 else ""
        detail_parts.append(f"{disc}Track {track.track_number}")
    detail_parts.append(_fmt_dur(track.duration or 0))
    if fmt:
        detail_parts.append(f"[cyan]{fmt}[/cyan]")
    if track.isrc:
        detail_parts.append(f"ISRC [dim]{track.isrc}[/dim]")
    lines.append("  " + "  ·  ".join(detail_parts))

    flags = []
    if track.hires:        flags.append("Hi-Res")
    if track.downloadable: flags.append("DL")
    if track.streamable:   flags.append("Stream")
    if track.parental_warning: flags.append("Explicit")
    if flags:
        lines.append("  " + "  ".join(f"[dim]{f}[/dim]" for f in flags))

    console.print(Panel("\n".join(lines), expand=False, border_style="cyan"))
    console.print(f"[dim]ID: {track.id}[/dim]")


def _display_artist_info(artist) -> None:
    from rich.panel import Panel

    header = f"[bold]{artist.name}[/bold]"
    if artist.albums_count:
        header += f"\n  {artist.albums_count} albums"

    console.print(Panel(header, expand=False, border_style="cyan"))

    if not artist.albums or not artist.albums.items:
        console.print("[dim]No album data.[/dim]")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("ID",    style="dim",  no_wrap=True, width=14)
    table.add_column("Title", style="bold", ratio=4)
    table.add_column("Year",  style="dim",  no_wrap=True, width=6)
    table.add_column("Type",  style="dim",  no_wrap=True, width=12)

    for a in artist.albums.items:
        year = (a.release_date_original or "")[:4] if hasattr(a, "release_date_original") else ""
        rtype = (getattr(a, "release_type", None) or "").capitalize()
        table.add_row(str(a.id), a.display_title, year, rtype)

    console.print(table)
    console.print(f"[dim]ID: {artist.id}[/dim]")


@app.command()
def goodies(
    url_or_id: str            = typer.Argument(..., help="Album URL or bare album ID"),
    output:    Optional[Path] = typer.Option(None, "-o", "--output",
                                             help="Destination directory"),
) -> None:
    """
    Download only the bonus files (goodies) for an album.

    Goodies are non-audio extras bundled with an album purchase —
    typically a booklet PDF, but may also include hi-res videos or
    other digital files. Audio tracks are not downloaded.

    Files are placed in  <output>/<Album [Format] [Year]>/

    Examples
    ────────
      kabooz goodies https://open.qobuz.com/album/0093046758769
      kabooz goodies 0093046758769 -o ~/Downloads
    """
    cfg  = _cfg()
    sess = _session(cfg)

    try:
        with _make_progress() as prog:
            current: list = []

            def on_progress(filename: str, done: int, total: int) -> None:
                if not current:
                    current.append(
                        prog.add_task(f"  [dim]{filename[:60]}[/dim]", total=None)
                    )
                prog.update(current[0], completed=done, total=total or None)

            def on_next(filename: str, done: int, total: int) -> None:
                # Reset task for each new file
                if current:
                    prog.remove_task(current[0])
                    current.clear()
                on_progress(filename, done, total)

            results = sess.download_album_goodies(
                url_or_id,
                dest_dir=output or Path(cfg.download.output_dir),
                on_progress_each=on_next,
            )
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except NotFoundError:
        err_console.print(f"[red]Album not found:[/red] {url_or_id}")
        raise typer.Exit(code=1)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    if not results:
        console.print("[yellow]This album has no goodies.[/yellow]")
        return

    ok      = [r for r in results if r.ok and not r.skipped]
    skipped = [r for r in results if r.ok and r.skipped]
    failed  = [r for r in results if not r.ok]

    for r in ok:
        console.print(f"[green]Downloaded:[/green] {r.path.name}")
    for r in skipped:
        console.print(f"[yellow]Skipped[/yellow] (already complete): {r.path.name}")
    for r in failed:
        console.print(f"[red]Failed:[/red] {r.goodie.name} — {r.error}")

    console.print(
        f"\n[green]Done.[/green] "
        f"{len(ok)} downloaded, {len(skipped)} skipped"
        + (f", [red]{len(failed)} failed[/red]" if failed else "")
        + "."
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
    cfg  = _cfg()
    q    = _quality(quality)
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
            on_track_start=_on_track_start,
            on_track_done=_on_track_done,
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
        f"{result.succeeded} downloaded, {result.skipped} skipped, "
        f"{result.failed} failed."
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
        kabooz artist 298
        kabooz artist https://open.qobuz.com/artist/298 --type album
        kabooz artist 298 --type album,live -q flac_16
    """
    cfg  = _cfg()
    q    = _quality(quality)
    sess = _session(cfg)

    try:
        artist_id  = _resolve_id(url_or_id, "artist")
        artist_obj = sess.get_artist(artist_id, extras="")
        console.print(
            f"[cyan]Downloading discography:[/cyan] [bold]{artist_obj.name}[/bold]"
            + (f"  [dim](type: {release_type})[/dim]" if release_type else "")
        )
    except Exception as exc:
        err_console.print(f"[red]Could not fetch artist:[/red] {exc}")
        raise typer.Exit(code=1)

    grand_ok   = 0
    grand_skip = 0
    grand_fail = 0

    def on_done(r) -> None:
        nonlocal grand_ok, grand_skip
        if r.download.skipped:
            grand_skip += 1
            console.print("       [yellow]↩ already complete[/yellow]")
        else:
            grand_ok += 1

    try:
        results = sess.download_artist_discography(
            url_or_id,
            release_type=release_type,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            template=template,
            on_album_start=_on_album_start,
            on_track_start=_on_track_start,
            on_track_done=on_done,
            workers=workers,
        )
        grand_fail = sum(r.failed for r in results)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{len(results)} albums · {grand_ok} tracks downloaded · "
        f"{grand_skip} skipped"
        + (f" · [yellow]{grand_fail} failed[/yellow]" if grand_fail else "") + "."
    )


@app.command()
def favorites(
    fav_type: str            = typer.Option("tracks", "--type", "-t",
                                            help="tracks or albums"),
    output:   Optional[Path] = typer.Option(None, "-o", "--output"),
    quality:  Optional[str]  = typer.Option(None, "-q", "--quality"),
    workers:  Optional[int]  = typer.Option(None, "-j", "--workers"),
) -> None:
    """
    Download all favorited tracks or albums.

    \b
    Examples:
        kabooz favorites
        kabooz favorites --type albums -q flac_16
    """
    if fav_type not in ("tracks", "albums"):
        err_console.print("[red]--type must be 'tracks' or 'albums'[/red]")
        raise typer.Exit(code=1)

    cfg  = _cfg()
    q    = _quality(quality)
    sess = _session(cfg)
    console.print(f"[cyan]Downloading favorite {fav_type}…[/cyan]")
    try:
        result = sess.download_favorites(
            fav_type=fav_type,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            on_track_start=_on_track_start,
            on_track_done=_on_track_done,
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
        f"{result.succeeded} downloaded, {result.skipped} skipped, "
        f"{result.failed} failed."
    )


@app.command()
def purchases(
    purchase_type: str            = typer.Option("albums", "--type", "-t",
                                                 help="albums or tracks"),
    output:        Optional[Path] = typer.Option(None, "-o", "--output"),
    quality:       Optional[str]  = typer.Option(None, "-q", "--quality"),
    workers:       Optional[int]  = typer.Option(None, "-j", "--workers"),
) -> None:
    """Download all purchased albums or tracks."""
    if purchase_type not in ("albums", "tracks"):
        err_console.print("[red]--type must be 'albums' or 'tracks'[/red]")
        raise typer.Exit(code=1)

    cfg  = _cfg()
    q    = _quality(quality)
    sess = _session(cfg)
    console.print(f"[cyan]Downloading purchased {purchase_type}…[/cyan]")
    try:
        result = sess.download_purchases(
            purchase_type=purchase_type,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            on_track_start=_on_track_start,
            on_track_done=_on_track_done,
            workers=workers,
        )
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{result.succeeded} downloaded, {result.skipped} skipped, "
        f"{result.failed} failed."
    )


@app.command()
def search(
    query:       str  = typer.Argument(...),
    search_type: str  = typer.Option("tracks", "--type", "-t"),
    limit:       int  = typer.Option(10, "-n"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Search the Qobuz catalog."""
    valid = {"tracks", "albums", "artists", "playlists"}
    if search_type not in valid:
        err_console.print(
            f"[red]Invalid type.[/red] Choose from: {', '.join(sorted(valid))}"
        )
        raise typer.Exit(code=1)

    sess = _session()
    try:
        results = sess.search(query=query, search_type=search_type, limit=limit)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except APIError as exc:
        err_console.print(f"[red]Search failed:[/red] {exc}")
        raise typer.Exit(code=1)

    if json_output:
        import json, dataclasses
        console.print_json(json.dumps(dataclasses.asdict(results), indent=2, default=str))
        return

    # Pull items from the typed result object based on what was requested.
    if search_type == "tracks":
        page = results.tracks
    elif search_type == "albums":
        page = results.albums
    elif search_type == "artists":
        page = results.artists
    else:
        page = results.playlists

    items = page.items if page else []
    if not items:
        console.print(f"No results for [bold]{query!r}[/bold].")
        return

    table = Table(title=f'Search: "{query}" ({search_type})', show_lines=False)
    if search_type == "tracks":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Artist")
        table.add_column("Album", style="dim")
        for t in items:
            table.add_row(
                str(t.id),
                t.display_title,
                t.performer.name if t.performer else "",
                t.album.display_title if t.album else "",
            )
    elif search_type == "albums":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
        table.add_column("Artist")
        table.add_column("Year", style="dim")
        for a in items:
            table.add_row(
                str(a.id),
                a.display_title,
                a.artist.name if a.artist else "",
                (a.release_date_original or "")[:4],
            )
    elif search_type == "artists":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Albums", style="dim")
        for a in items:
            table.add_row(str(a.id), a.name, str(a.albums_count or ""))
    elif search_type == "playlists":
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Tracks", style="dim")
        table.add_column("Owner")
        for p in items:
            table.add_row(
                str(p.id),
                p.name,
                str(p.tracks_count or ""),
                p.owner.get("name", "") if isinstance(p.owner, dict) else "",
            )
    console.print(table)


# ══════════════════════════════════════════════════════════════════════════
# Discovery commands
# ══════════════════════════════════════════════════════════════════════════

@app.command("new-releases")
def cmd_new_releases(
    release_type: str           = typer.Option("new-releases", "--type", "-t"),
    genre_id:     Optional[int] = typer.Option(None, "--genre"),
    limit:        int           = typer.Option(25, "-n"),
) -> None:
    """Browse new or featured album releases."""
    sess = _session()
    try:
        data = sess.get_new_releases(release_type=release_type,
                                     genre_id=genre_id, limit=limit)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)

    albums = data.get("albums", {}).get("items", [])
    if not albums:
        console.print("[dim]No results.[/dim]"); return

    table = Table(title=f"New releases ({release_type})", show_lines=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Title", style="bold")
    table.add_column("Artist")
    table.add_column("Date", style="dim")
    for a in albums:
        table.add_row(str(a.get("id", "")), a.get("title", ""),
                      (a.get("artist") or {}).get("name", ""),
                      (a.get("release_date_original") or "")[:10])
    console.print(table)


@app.command("featured")
def cmd_featured(
    pl_type:  str           = typer.Option("editor-picks", "--type", "-t"),
    genre_id: Optional[int] = typer.Option(None, "--genre"),
    limit:    int           = typer.Option(25, "-n"),
) -> None:
    """Browse editorially curated playlists."""
    sess = _session()
    try:
        data = sess.get_featured_playlists(pl_type=pl_type,
                                           genre_id=genre_id, limit=limit)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)

    pls = data.get("playlists", {}).get("items", [])
    if not pls:
        console.print("[dim]No results.[/dim]"); return

    table = Table(title=f"Featured playlists ({pl_type})", show_lines=False)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("Tracks", style="dim")
    table.add_column("Owner")
    for p in pls:
        table.add_row(str(p.get("id", "")), p.get("name", ""),
                      str(p.get("tracks_count", "")),
                      (p.get("owner") or {}).get("name", ""))
    console.print(table)


@app.command("genres")
def cmd_genres(
    parent: Optional[int] = typer.Option(None, "--parent"),
) -> None:
    """List Qobuz genres (or sub-genres of a parent)."""
    sess = _session()
    try:
        data = sess.get_genres(parent_id=parent)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)

    genres = data.get("genres", {}).get("items", [])
    if not genres:
        console.print("[dim]No genres found.[/dim]"); return

    table = Table(title="Genres", show_lines=False)
    table.add_column("ID",   style="dim", width=6)
    table.add_column("Name", style="bold")
    table.add_column("Slug", style="dim")
    for g in genres:
        table.add_row(str(g.get("id", "")), g.get("name", ""), g.get("slug", ""))
    console.print(table)


# ══════════════════════════════════════════════════════════════════════════
# library subcommands
# ══════════════════════════════════════════════════════════════════════════

@library_app.command("show")
def library_show(
    show_type: str = typer.Option("all", "--type", "-t"),
    limit:     int = typer.Option(50, "-n"),
) -> None:
    """Show local favorites."""
    store = _session().store
    types = ["track", "album", "artist"] if show_type == "all" else [show_type]
    for t in types:
        items = store.get_favorites(t, limit=limit)
        if not items:
            continue
        table = Table(title=f"Favorite {t}s ({len(items)})", show_lines=False)
        table.add_column("ID",    style="dim", no_wrap=True)
        table.add_column("Title", style="bold")
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
    url_or_id:   str  = typer.Argument(...),
    entity_type: str  = typer.Option("track", "--type", "-t"),
    remote:      bool = typer.Option(False, "--remote/--local-only"),
) -> None:
    """Add a track, album, or artist to local favorites."""
    sess      = _session()
    entity_id = _resolve_id(url_or_id)
    try:
        sess.add_favorite(entity_id, entity_type, remote=remote)
        console.print(f"[green]Added[/green] {entity_type} {entity_id}.")
        if remote:
            console.print("  [dim]Also added to Qobuz account.[/dim]")
    except PoolModeError:
        err_console.print("[red]--remote not available in pool mode.[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)


@library_app.command("remove")
def library_remove(
    url_or_id:   str  = typer.Argument(...),
    entity_type: str  = typer.Option("track", "--type", "-t"),
    remote:      bool = typer.Option(False, "--remote/--local-only"),
) -> None:
    """Remove a track, album, or artist from local favorites."""
    sess      = _session()
    entity_id = _resolve_id(url_or_id)
    try:
        removed = sess.remove_favorite(entity_id, entity_type, remote=remote)
        if removed:
            console.print(f"[green]Removed[/green] {entity_type} {entity_id}.")
        else:
            console.print(f"[yellow]{entity_type} {entity_id} was not in local favorites.[/yellow]")
        if remote:
            console.print("  [dim]Also removed from Qobuz account.[/dim]")
    except PoolModeError:
        err_console.print("[red]--remote not available in pool mode.[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)


@library_app.command("sync")
def library_sync(
    sync_type: str  = typer.Option("all", "--type", "-t"),
    clear:     bool = typer.Option(False, "--clear"),
) -> None:
    """Sync favorites from your Qobuz account into the local store."""
    sess = _session()
    try:
        counts = sess.sync_favorites(
            fav_type=None if sync_type == "all" else sync_type,
            clear=clear,
        )
        for t, n in counts.items():
            console.print(f"[green]Synced[/green] {n} {t}(s) → local store.")
    except PoolModeError:
        err_console.print("[red]sync requires a personal session (not pool mode).[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:
        err_console.print(f"[red]Sync failed:[/red] {exc}"); raise typer.Exit(code=1)


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
        console.print("[dim]No history yet.[/dim]"); return
    table = Table(title=f"Recent history (last {limit})", show_lines=False)
    table.add_column("Time",   style="dim", no_wrap=True)
    table.add_column("Title",  style="bold")
    table.add_column("Artist")
    table.add_column("Album",  style="dim")
    for row in rows:
        table.add_row(row.get("played_at", "")[:16], row.get("title", ""),
                      row.get("artist", ""), row.get("album", ""))
    console.print(table)


# ══════════════════════════════════════════════════════════════════════════
# lpl subcommands
# ══════════════════════════════════════════════════════════════════════════

@lpl_app.command("list")
def lpl_list() -> None:
    """List all local playlists."""
    store = _session().store
    pls   = store.list_playlists()
    if not pls:
        console.print("[dim]No local playlists yet.[/dim]"); return
    table = Table(title="Local playlists", show_lines=False)
    table.add_column("ID",      style="dim", no_wrap=True, max_width=10)
    table.add_column("Name",    style="bold")
    table.add_column("Tracks",  style="dim")
    table.add_column("Updated", style="dim")
    for pl in pls:
        table.add_row(pl["id"][:8], pl["name"],
                      str(pl.get("track_count", 0)), pl.get("updated_at", "")[:10])
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
    sess  = _session()
    pl    = sess._find_local_playlist(name)
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {name}")
        raise typer.Exit(code=1)
    tracks = sess.store.get_playlist_tracks(pl["id"])
    console.print(f"[bold]{pl['name']}[/bold]  [dim]{pl.get('description', '')}[/dim]")
    if not tracks:
        console.print("[dim]Empty.[/dim]"); return
    table = Table(show_lines=False)
    table.add_column("#",      style="dim", width=4)
    table.add_column("ID",     style="dim", no_wrap=True)
    table.add_column("Title",  style="bold")
    table.add_column("Artist")
    for t in tracks:
        table.add_row(str(t["position"] + 1), str(t["track_id"]),
                      t.get("title", ""), t.get("artist", ""))
    console.print(table)


@lpl_app.command("add")
def lpl_add(
    playlist: str = typer.Argument(..., help="Playlist name or ID prefix"),
    track_arg: str = typer.Argument(..., help="Track ID or Qobuz URL"),
) -> None:
    """Add a track to a local playlist."""
    sess     = _session()
    track_id = _resolve_id(track_arg, "track")
    pl       = sess._find_local_playlist(playlist)
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {playlist}")
        raise typer.Exit(code=1)

    title = artist = album = isrc = ""
    duration = 0
    try:
        obj      = sess.get_track(track_id)
        title    = obj.display_title
        artist   = obj.performer.name if obj.performer else ""
        album    = obj.album.title if obj.album else ""
        duration = obj.duration or 0
        isrc     = obj.isrc or ""
    except Exception:
        pass

    sess.store.add_track_to_playlist(
        pl["id"], track_id,
        title=title, artist=artist, album=album, duration=duration, isrc=isrc,
    )
    console.print(f"[green]Added[/green] {title or track_id} → [bold]{pl['name']}[/bold]")


@lpl_app.command("remove")
def lpl_remove(
    playlist:  str = typer.Argument(...),
    track_arg: str = typer.Argument(...),
) -> None:
    """Remove a track from a local playlist."""
    sess  = _session()
    pl    = sess._find_local_playlist(playlist)
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {playlist}")
        raise typer.Exit(code=1)
    if sess.store.remove_track_from_playlist(pl["id"], track_arg):
        console.print(f"[green]Removed[/green] track {track_arg} from [bold]{pl['name']}[/bold].")
    else:
        console.print(f"[yellow]Track {track_arg} was not in that playlist.[/yellow]")


@lpl_app.command("delete")
def lpl_delete(
    name:    str  = typer.Argument(...),
    confirm: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete a local playlist."""
    sess = _session()
    pl   = sess._find_local_playlist(name)
    if not pl:
        err_console.print(f"[red]Playlist not found:[/red] {name}")
        raise typer.Exit(code=1)
    if not confirm:
        typer.confirm(f"Delete playlist '{pl['name']}'?", abort=True)
    sess.store.delete_playlist(pl["id"])
    console.print(f"[green]Deleted[/green] [bold]{pl['name']}[/bold].")


@lpl_app.command("clone")
def lpl_clone(
    url_or_id: str           = typer.Argument(...),
    name:      Optional[str] = typer.Option(None, "--name", "-n"),
) -> None:
    """Clone a Qobuz playlist into the local store."""
    sess = _session()
    try:
        pl_id = sess.clone_playlist(url_or_id, name=name)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Clone failed:[/red] {exc}"); raise typer.Exit(code=1)

    pl     = sess.store.get_playlist(pl_id)
    tracks = sess.store.get_playlist_tracks(pl_id)
    console.print(
        f"[green]Cloned[/green] [bold]{pl['name']}[/bold] "
        f"({len(tracks)} tracks saved locally)."
    )


@lpl_app.command("download")
def lpl_download(
    name:     str            = typer.Argument(...),
    output:   Optional[Path] = typer.Option(None, "-o"),
    quality:  Optional[str]  = typer.Option(None, "-q"),
    workers:  Optional[int]  = typer.Option(None, "-j"),
    template: Optional[str]  = typer.Option(None, "--template"),
) -> None:
    """Download all tracks in a local playlist."""
    cfg  = _cfg()
    q    = _quality(quality)
    sess = _session(cfg)
    try:
        result = sess.download_local_playlist(
            name,
            quality=q,
            dest_dir=output or Path(cfg.download.output_dir),
            template=template,
            on_track_start=_on_track_start,
            on_track_done=_on_track_done,
            workers=workers,
        )
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]"); raise typer.Exit(code=1)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)

    console.print(
        f"\n[green]Done.[/green] "
        f"{result.succeeded} downloaded, {result.skipped} skipped, "
        f"{result.failed} failed."
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
        err_console.print(f"[red]{exc}[/red]"); raise typer.Exit(code=1)
    console.print(f"[green]Exported[/green] → {path}")


@lpl_app.command("import")
def lpl_import(
    file:      Path = typer.Argument(...),
    overwrite: bool = typer.Option(False, "--overwrite/--no-overwrite"),
) -> None:
    """Import a shared TOML playlist file into the local store."""
    if not file.exists():
        err_console.print(f"[red]File not found:[/red] {file}")
        raise typer.Exit(code=1)
    sess = _session()
    try:
        pl_id = sess.import_playlist(file, overwrite=overwrite)
    except Exception as exc:
        err_console.print(f"[red]Import failed:[/red] {exc}"); raise typer.Exit(code=1)
    pl     = sess.store.get_playlist(pl_id)
    tracks = sess.store.get_playlist_tracks(pl_id)
    console.print(f"[green]Imported[/green] [bold]{pl['name']}[/bold] ({len(tracks)} tracks)")


# ══════════════════════════════════════════════════════════════════════════
# export subcommands
# ══════════════════════════════════════════════════════════════════════════

@export_app.command("backup")
def export_backup(output: Optional[Path] = typer.Option(None, "-o")) -> None:
    """Create a full .tar.gz backup of your local library."""
    sess = _session()
    try:
        path = sess.backup(output)
    except Exception as exc:
        err_console.print(f"[red]Backup failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(
        f"[green]Backup saved:[/green] {path}  "
        f"[dim]({path.stat().st_size / 1024:.1f} KB)[/dim]"
    )


@export_app.command("favorites")
def export_favorites_cmd(
    output:   Optional[Path] = typer.Option(None, "-o"),
    fav_type: str            = typer.Option("all", "--type", "-t"),
) -> None:
    """Export local favorites to a TOML file."""
    sess = _session()
    try:
        path = sess.export_favorites(output, fav_type=None if fav_type == "all" else fav_type)
    except Exception as exc:
        err_console.print(f"[red]Export failed:[/red] {exc}"); raise typer.Exit(code=1)
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
    file:     Path = typer.Argument(...),
    no_merge: bool = typer.Option(False, "--replace/--merge"),
) -> None:
    """Import favorites from a TOML file created by 'export favorites'."""
    if not file.exists():
        err_console.print(f"[red]File not found:[/red] {file}")
        raise typer.Exit(code=1)
    sess = _session()
    try:
        n = sess.import_favorites(file, merge=not no_merge)
    except (ValueError, Exception) as exc:
        err_console.print(f"[red]Import failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(f"[green]Imported[/green] {n} favorite(s) from {file.name}.")


@export_app.command("restore")
def export_restore(
    archive:       Path           = typer.Argument(...),
    do_favorites:  bool           = typer.Option(True,  "--favorites/--no-favorites"),
    do_playlists:  bool           = typer.Option(True,  "--playlists/--no-playlists"),
    db:            bool           = typer.Option(False, "--db"),
    replace:       bool           = typer.Option(False, "--replace/--merge"),
) -> None:
    """Restore from a backup archive created by 'export backup'."""
    if not archive.exists():
        err_console.print(f"[red]File not found:[/red] {archive}")
        raise typer.Exit(code=1)
    sess = _session()
    try:
        result = sess.restore(
            archive,
            restore_favorites=do_favorites,
            restore_playlists=do_playlists,
            restore_db=db,
            merge=not replace,
        )
    except Exception as exc:
        err_console.print(f"[red]Restore failed:[/red] {exc}"); raise typer.Exit(code=1)

    if result.db_restored:
        console.print("[green]Database restored.[/green] Please re-open the application.")
        return
    console.print(
        f"[green]Restore complete.[/green]  "
        f"Favorites: {result.favorites_imported}  "
        f"Playlists: {result.playlists_imported} imported, "
        f"{result.playlists_skipped} skipped."
    )
    for e in result.errors:
        err_console.print(f"  [dim yellow]{e}[/dim yellow]")


# ══════════════════════════════════════════════════════════════════════════
# account subcommands
# ══════════════════════════════════════════════════════════════════════════

@account_app.command("show")
def account_show(json_output: bool = typer.Option(False, "--json")) -> None:
    """Show your Qobuz account profile and subscription tier."""
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
        sub  = Table(title="Subscription", show_lines=False, box=None)
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
    email:        Optional[str]  = typer.Option(None, "--email"),
    firstname:    Optional[str]  = typer.Option(None, "--firstname"),
    lastname:     Optional[str]  = typer.Option(None, "--lastname"),
    display_name: Optional[str]  = typer.Option(None, "--display-name"),
    country:      Optional[str]  = typer.Option(None, "--country"),
    language:     Optional[str]  = typer.Option(None, "--language"),
    newsletter:   Optional[bool] = typer.Option(None, "--newsletter/--no-newsletter"),
) -> None:
    """Update your Qobuz account profile fields."""
    if not any([email, firstname, lastname, display_name, country, language,
                newsletter is not None]):
        err_console.print("[red]Provide at least one field to update.[/red]")
        raise typer.Exit(code=1)
    sess = _session()
    try:
        profile = sess.update_profile(
            email=email, firstname=firstname, lastname=lastname,
            display_name=display_name, country_code=country,
            language_code=language, newsletter=newsletter,
        )
    except Exception as exc:
        err_console.print(f"[red]Update failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(
        f"[green]Profile updated.[/green] "
        f"Name: {profile.full_name}  Email: {profile.email or '—'}"
    )


@account_app.command("password")
def account_password(
    current: Optional[str] = typer.Option(None, "--current", hide_input=True),
    new:     Optional[str] = typer.Option(None, "--new",     hide_input=True),
    confirm: Optional[str] = typer.Option(None, "--confirm", hide_input=True),
) -> None:
    """Change your Qobuz account password."""
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
    console.print("[green]Password changed successfully.[/green]")


@account_app.command("subscription")
def account_subscription() -> None:
    """Show your current subscription tier and capabilities."""
    sess = _session()
    try:
        profile = sess.get_profile()
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    if not profile.credential:
        console.print("[yellow]No subscription information available.[/yellow]"); return
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
    table.add_row("Mobile streaming",       _yn(cred.mobile_streaming))
    table.add_row("Offline listening",      _yn(cred.offline_listening))
    console.print(table)


# ══════════════════════════════════════════════════════════════════════════
# remote subcommands
# ══════════════════════════════════════════════════════════════════════════

@remote_app.command("list")
def remote_list(limit: int = typer.Option(50, "-n")) -> None:
    """List playlists on your Qobuz account."""
    sess = _session()
    try:
        pls = list(sess.iter_user_playlists(page_size=limit))
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    if not pls:
        console.print("[dim]No playlists on your Qobuz account.[/dim]"); return
    table = Table(title="Remote playlists", show_lines=False)
    table.add_column("ID",     style="dim", no_wrap=True)
    table.add_column("Name",   style="bold")
    table.add_column("Tracks", style="dim")
    table.add_column("Public", style="dim")
    table.add_column("Owner",  style="dim")
    for pl in pls:
        table.add_row(str(pl.id), pl.name, str(pl.tracks_count),
                      "yes" if pl.is_public else "no",
                      pl.owner.name if pl.owner else "—")
    console.print(table)


@remote_app.command("show")
def remote_show(
    playlist_id: str = typer.Argument(...),
    limit:       int = typer.Option(50, "-n"),
) -> None:
    """Show tracks in a Qobuz account playlist."""
    sess = _session()
    try:
        pl = sess.get_playlist(playlist_id, limit=limit)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(
        f"[bold]{pl.name}[/bold]  "
        f"[dim]{pl.tracks_count} tracks · "
        f"{'public' if pl.is_public else 'private'} · "
        f"owner: {pl.owner.name}[/dim]"
    )
    if not pl.tracks or not pl.tracks.items:
        console.print("[dim]Empty.[/dim]"); return
    table = Table(show_lines=False)
    table.add_column("Pos",      style="dim", width=4)
    table.add_column("PT-ID",    style="dim", no_wrap=True, width=10)
    table.add_column("Track ID", style="dim", no_wrap=True)
    table.add_column("Title",    style="bold")
    table.add_column("Artist")
    for t in pl.tracks.items:
        table.add_row(str(t.position or "—"), str(t.playlist_track_id or "—"),
                      str(t.id), t.title,
                      t.performer.name if t.performer else "—")
    console.print(table)


@remote_app.command("create")
def remote_create(
    name:          str  = typer.Argument(...),
    desc:          str  = typer.Option("",    "--desc",            "-d"),
    public:        bool = typer.Option(False, "--public/--private"),
    collaborative: bool = typer.Option(False, "--collaborative/--no-collaborative"),
    save_locally:  bool = typer.Option(True,  "--save-locally/--no-save-locally"),
) -> None:
    """Create a new playlist on your Qobuz account."""
    sess = _session()
    try:
        pl = sess.create_remote_playlist(
            name=name, description=desc,
            is_public=public, is_collaborative=collaborative,
            also_save_locally=save_locally,
        )
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(f"[green]Created[/green] remote playlist [bold]{pl.name}[/bold] (id: {pl.id})")


@remote_app.command("update")
def remote_update(
    playlist_id:   str            = typer.Argument(...),
    name:          Optional[str]  = typer.Option(None, "--name",  "-n"),
    desc:          Optional[str]  = typer.Option(None, "--desc",  "-d"),
    public:        Optional[bool] = typer.Option(None, "--public/--private"),
    collaborative: Optional[bool] = typer.Option(None, "--collaborative/--no-collaborative"),
) -> None:
    """Update a Qobuz playlist's metadata."""
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
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(f"[green]Updated[/green] [bold]{pl.name}[/bold].")


@remote_app.command("delete")
def remote_delete(
    playlist_id: str  = typer.Argument(...),
    confirm:     bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Permanently delete a playlist from your Qobuz account."""
    if not confirm:
        typer.confirm(f"Delete remote playlist {playlist_id!r}?", abort=True)
    sess = _session()
    try:
        sess.delete_remote_playlist(playlist_id)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(f"[green]Deleted[/green] remote playlist {playlist_id}.")


@remote_app.command("add")
def remote_add(
    playlist_id: str       = typer.Argument(...),
    track_ids:   list[str] = typer.Argument(...),
    duplicates:  bool      = typer.Option(False, "--allow-duplicates"),
) -> None:
    """Add one or more tracks to a Qobuz playlist."""
    resolved = [_resolve_id(t, "track") for t in track_ids]
    sess = _session()
    try:
        sess.add_tracks_to_remote_playlist(
            playlist_id, resolved, no_duplicate=not duplicates,
        )
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(f"[green]Added[/green] {len(resolved)} track(s) to playlist {playlist_id}.")


@remote_app.command("remove")
def remote_remove(
    playlist_id:        str       = typer.Argument(...),
    playlist_track_ids: list[int] = typer.Argument(...),
) -> None:
    """Remove tracks from a Qobuz playlist by playlist_track_id (PT-ID from 'remote show')."""
    sess = _session()
    try:
        sess.remove_tracks_from_remote_playlist(playlist_id, playlist_track_ids)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(
        f"[green]Removed[/green] {len(playlist_track_ids)} track(s) "
        f"from playlist {playlist_id}."
    )


@remote_app.command("follow")
def remote_follow(playlist_id: str = typer.Argument(...)) -> None:
    """Follow a public Qobuz playlist."""
    sess = _session()
    try:
        sess.follow_playlist(playlist_id)
    except Exception as exc:
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
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
        err_console.print(f"[red]Failed:[/red] {exc}"); raise typer.Exit(code=1)
    console.print(f"[green]Unfollowed[/green] playlist {playlist_id}.")


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()

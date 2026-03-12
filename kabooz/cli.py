# kabooz/cli.py
from __future__ import annotations

import json
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
from .download.downloader import Downloader
from .download.lyrics import fetch_lyrics
from .download.tagger import Tagger
from .exceptions import (
    APIError,
    AuthError,
    InvalidCredentialsError,
    NoAuthError,
    NotFoundError,
    NotStreamableError,
    TokenExpiredError,
)
from .quality import Quality
from .url import parse_url

# ── App and shared paths ───────────────────────────────────────────────────

app = typer.Typer(
    name="qobuz",
    help="Unofficial Qobuz CLI — download tracks, albums, and search the catalog.",
    add_completion=False,
)
console     = Console()
err_console = Console(stderr=True)

_CONFIG_DIR   = Path.home() / ".config" / "qobuz"
_CONFIG_PATH  = _CONFIG_DIR / "config.json"
_SESSION_PATH = _CONFIG_DIR / "session.json"


# ── Config file helpers ────────────────────────────────────────────────────

def _read_config() -> dict:
    """Read ~/.config/qobuz/config.json, returning {} if absent or unreadable."""
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _write_config(data: dict) -> None:
    """Merge data into the existing config file and write it back."""
    existing = _read_config()
    existing.update(data)
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(existing, indent=2))


# ── Credential helpers ─────────────────────────────────────────────────────

def _load_app_credentials() -> tuple[str, str]:
    """
    Load app_id and app_secret.
    Priority: environment variables > ~/.config/qobuz/config.json.
    Exits with a clear message if neither source provides both values.
    """
    cfg        = _read_config()
    app_id     = os.environ.get("QOBUZ_APP_ID")    or cfg.get("app_id")
    app_secret = os.environ.get("QOBUZ_APP_SECRET") or cfg.get("app_secret")

    if not app_id or not app_secret:
        err_console.print(
            "[red]App credentials not found.[/red]\n"
            "Set [bold]QOBUZ_APP_ID[/bold] and [bold]QOBUZ_APP_SECRET[/bold] "
            "environment variables, or run [bold]qobuz login[/bold] and supply "
            "them when prompted."
        )
        raise typer.Exit(code=1)

    return app_id, app_secret


def _build_client() -> QobuzClient:
    """
    Construct an authenticated QobuzClient.

    Authentication source priority:
      1. Token pool (if `pool` key is present in config.json)
      2. Session file (if ~/.config/qobuz/session.json exists)

    Exits with a helpful message if neither is available.
    401 errors from actual API calls propagate naturally and are caught
    in each command handler with a prompt to re-login.
    """
    app_id, app_secret = _load_app_credentials()
    cfg = _read_config()

    pool_source = cfg.get("pool")

    if pool_source:
        try:
            return QobuzClient.from_token_pool(pool_source)
        except Exception as exc:
            err_console.print(f"[red]Failed to load token pool:[/red] {exc}")
            raise typer.Exit(code=1)

    if not _SESSION_PATH.exists():
        err_console.print(
            "[red]Not logged in.[/red] Run [bold]qobuz login[/bold] first."
        )
        raise typer.Exit(code=1)

    client = QobuzClient.from_credentials(app_id=app_id, app_secret=app_secret)
    try:
        client.load_session(_SESSION_PATH)
    except Exception as exc:
        err_console.print(f"[red]Failed to load session:[/red] {exc}")
        raise typer.Exit(code=1)

    return client


def _handle_auth_error(exc: Exception) -> None:
    """Print a consistent message for auth failures and exit."""
    err_console.print(
        f"[red]Authentication error:[/red] {exc}\n"
        "Run [bold]qobuz login[/bold] to refresh your session."
    )
    raise typer.Exit(code=1)


# ── Utility helpers ────────────────────────────────────────────────────────

def _resolve_id(url_or_id: str, expected_type: Optional[str] = None) -> str:
    """Accept a raw entity ID or a full Qobuz URL. Returns the entity ID."""
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
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )


def _fetch_lyrics_for(track_obj, album_obj=None):
    """Fetch lyrics for a track, returning a LyricsResult or None."""
    artist_name = (
        track_obj.performer.name if track_obj.performer
        else (album_obj.artist.name if album_obj and album_obj.artist else "")
    )
    return fetch_lyrics(
        title=track_obj.title,
        artist=artist_name,
        album=album_obj.title if album_obj else None,
        duration=track_obj.duration,
    )


# ── Commands ───────────────────────────────────────────────────────────────

@app.command()
def login(
    username: Optional[str] = typer.Option(
        None, "--username", "-u", help="Qobuz account email"
    ),
    password: Optional[str] = typer.Option(
        None, "--password", "-p", help="Qobuz account password"
    ),
    token: Optional[str] = typer.Option(
        None, "--token", help="Use a pre-existing user auth token directly"
    ),
    user_id: Optional[str] = typer.Option(
        None, "--user-id", help="User ID (required when using --token)"
    ),
    pool: Optional[str] = typer.Option(
        None, "--pool",
        help="Path or URL to a token pool file. Saved to config for all future commands."
    ),
    app_id: Optional[str] = typer.Option(
        None, "--app-id", help="Qobuz App ID (saved to config if provided)"
    ),
    app_secret: Optional[str] = typer.Option(
        None, "--app-secret", help="Qobuz App Secret (saved to config if provided)"
    ),
) -> None:
    """
    Authenticate with Qobuz. Three modes:

    \b
    1. Username + password (default interactive mode):
         qobuz login
         qobuz login --username me@example.com --password secret

    \b
    2. Direct auth token:
         qobuz login --token MY_TOKEN --user-id 12345

    \b
    3. Token pool file or URL:
         qobuz login --pool ~/.config/qobuz/pool.txt
         qobuz login --pool https://example.com/pool.txt

    App credentials (app_id + app_secret) can be passed via --app-id /
    --app-secret, set as QOBUZ_APP_ID / QOBUZ_APP_SECRET environment
    variables, or entered interactively.
    """
    cfg = _read_config()

    resolved_app_id     = app_id     or os.environ.get("QOBUZ_APP_ID")     or cfg.get("app_id")
    resolved_app_secret = app_secret or os.environ.get("QOBUZ_APP_SECRET")  or cfg.get("app_secret")

    if not resolved_app_id:
        resolved_app_id = typer.prompt("App ID")
    if not resolved_app_secret:
        resolved_app_secret = typer.prompt("App Secret", hide_input=True)

    config_update: dict = {
        "app_id":     resolved_app_id,
        "app_secret": resolved_app_secret,
    }

    # ── Token pool mode ────────────────────────────────────────────────────
    if pool:
        config_update["pool"] = pool
        _write_config(config_update)
        try:
            QobuzClient.from_token_pool(pool)
        except Exception as exc:
            err_console.print(f"[red]Failed to load token pool:[/red] {exc}")
            raise typer.Exit(code=1)
        console.print(
            f"[green]Token pool loaded.[/green] "
            f"Pool path/URL saved to [bold]{_CONFIG_PATH}[/bold]."
        )
        return

    # Remove any previously stored pool when switching to session auth.
    config_update["pool"] = None
    _write_config({**cfg, **config_update})

    client = QobuzClient.from_credentials(
        app_id=resolved_app_id,
        app_secret=resolved_app_secret,
    )

    # ── Direct token mode ──────────────────────────────────────────────────
    if token:
        if not user_id:
            user_id = typer.prompt("User ID")
        try:
            session = client.login(token=token, user_id=user_id)
        except Exception as exc:
            err_console.print(f"[red]Login failed:[/red] {exc}")
            raise typer.Exit(code=1)

    # ── Username + password mode ───────────────────────────────────────────
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

    try:
        client.save_session(_SESSION_PATH)
    except Exception as exc:
        err_console.print(f"[red]Could not save session:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(
        f"[green]Logged in as[/green] {session.user_email or session.user_id}. "
        f"Session saved to [bold]{_SESSION_PATH}[/bold]."
    )


@app.command()
def track(
    url_or_id: str = typer.Argument(..., help="Track ID or Qobuz URL"),
    output: Path = typer.Option(
        Path("."), "--output", "-o", help="Directory to download into"
    ),
    quality: Quality = typer.Option(
        Quality.HI_RES, "--quality", "-q", help="Download quality"
    ),
    lyrics: bool = typer.Option(
        False, "--lyrics", help="Fetch and embed lyrics from LRCLib"
    ),
    cover: bool = typer.Option(
        True, "--cover/--no-cover", help="Embed album cover art"
    ),
) -> None:
    """
    Download a single track by ID or URL.

    The track is treated as a standalone single — it is placed directly
    in the output directory with no album subfolder, regardless of what
    album it belongs to on Qobuz.
    """
    track_id = _resolve_id(url_or_id, expected_type="track")
    client   = _build_client()

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
        url_info = client.get_track_url(track_id, quality=quality)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except NotStreamableError as exc:
        err_console.print(f"[red]Not streamable:[/red] {exc}")
        raise typer.Exit(code=1)
    except APIError as exc:
        err_console.print(f"[red]API error:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[cyan]Downloading[/cyan] {track_obj.title}")

    with _make_progress() as progress:
        task = progress.add_task(track_obj.title, total=None)

        def on_progress(done: int, total: int) -> None:
            progress.update(task, completed=done, total=total or None)

        with Downloader() as dl:
            # album=None intentionally — single track goes flat into output dir.
            result = dl.download_track(
                track=track_obj,
                url_info=url_info,
                dest_dir=output,
                album=None,
                on_progress=on_progress,
            )

    if result.skipped:
        console.print(f"[yellow]Skipped[/yellow] (already complete): {result.path}")
        return

    lyrics_result = None
    if lyrics:
        lyrics_result = _fetch_lyrics_for(track_obj)
        if lyrics_result.found:
            console.print("[green]Lyrics found[/green]")

    tagger = Tagger()
    tagger.tag(
        path=result.path,
        track=track_obj,
        album=None,
        lyrics=lyrics_result,
        embed_cover=cover,
    )

    console.print(f"[green]Done:[/green] {result.path}")


@app.command()
def album(
    url_or_id: str = typer.Argument(..., help="Album ID or Qobuz URL"),
    output: Path = typer.Option(
        Path("."), "--output", "-o", help="Directory to download into"
    ),
    quality: Quality = typer.Option(
        Quality.HI_RES, "--quality", "-q", help="Download quality"
    ),
    lyrics: bool = typer.Option(
        False, "--lyrics", help="Fetch and embed lyrics for each track"
    ),
    cover: bool = typer.Option(
        True, "--cover/--no-cover", help="Embed album cover art"
    ),
) -> None:
    """Download a full album by ID or URL."""
    album_id = _resolve_id(url_or_id, expected_type="album")
    client   = _build_client()

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

    artist_name = album_obj.artist.name if album_obj.artist else "Unknown Artist"
    console.print(
        f"[cyan]Downloading album:[/cyan] {album_obj.title} "
        f"by {artist_name} ({album_obj.tracks.total} tracks)"
    )

    tagger    = Tagger()
    total     = album_obj.tracks.total
    succeeded = 0
    skipped   = 0
    failed    = 0

    for i, track_summary in enumerate(album_obj.tracks.items, 1):
        console.print(f"  [{i}/{total}] {track_summary.title}")

        # Fetch the full Track object — TrackSummary is too lightweight for
        # tagging (missing copyright, performers, composer, etc.).
        try:
            track_obj = client.get_track(str(track_summary.id))
        except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
            _handle_auth_error(exc)
        except Exception as exc:
            err_console.print(f"    [red]Could not fetch track metadata: {exc}[/red]")
            failed += 1
            continue

        try:
            url_info = client.get_track_url(str(track_obj.id), quality=quality)
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

            with Downloader() as dl:
                try:
                    result = dl.download_track(
                        track=track_obj,
                        url_info=url_info,
                        dest_dir=output,
                        album=album_obj,
                        on_progress=on_progress,
                    )
                except Exception as exc:
                    err_console.print(f"    [red]Download failed: {exc}[/red]")
                    failed += 1
                    continue

        if result.skipped:
            console.print("    [yellow]Already complete, skipped.[/yellow]")
            skipped += 1
            continue

        lyrics_result = None
        if lyrics:
            lyrics_result = _fetch_lyrics_for(track_obj, album_obj)

        try:
            tagger.tag(
                path=result.path,
                track=track_obj,
                album=album_obj,
                lyrics=lyrics_result,
                embed_cover=cover,
            )
        except Exception as exc:
            err_console.print(f"    [red]Tagging failed: {exc}[/red]")

        succeeded += 1

    console.print(
        f"\n[green]Done.[/green] "
        f"{succeeded} downloaded, {skipped} skipped, {failed} failed."
    )


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    type: str = typer.Option(
        "tracks", "--type", "-t",
        help="Entity type: tracks, albums, artists, playlists"
    ),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results to show"),
) -> None:
    """Search the Qobuz catalog and print results as a table."""
    valid_types = {"tracks", "albums", "artists", "playlists"}
    if type not in valid_types:
        err_console.print(
            f"[red]Invalid type:[/red] {type!r}. "
            f"Choose from: {', '.join(sorted(valid_types))}"
        )
        raise typer.Exit(code=1)

    client = _build_client()

    try:
        results = client.search(query=query, type=type, limit=limit)
    except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
        _handle_auth_error(exc)
    except APIError as exc:
        err_console.print(f"[red]Search failed:[/red] {exc}")
        raise typer.Exit(code=1)

    items = results.get(type, {}).get("items", [])
    if not items:
        console.print(f"No results for [bold]{query!r}[/bold].")
        return

    table = Table(title=f'Search: "{query}" ({type})', show_lines=False)

    if type == "tracks":
        table.add_column("ID",     style="dim",  no_wrap=True)
        table.add_column("Title",  style="bold")
        table.add_column("Artist")
        table.add_column("Album",  style="dim")
        for item in items:
            artist = (item.get("performer") or {}).get("name", "")
            alb    = (item.get("album") or {}).get("title", "")
            table.add_row(str(item.get("id", "")), item.get("title", ""), artist, alb)

    elif type == "albums":
        table.add_column("ID",     style="dim",  no_wrap=True)
        table.add_column("Title",  style="bold")
        table.add_column("Artist")
        table.add_column("Year",   style="dim")
        for item in items:
            artist = (item.get("artist") or {}).get("name", "")
            year   = (item.get("release_date_original") or "")[:4]
            table.add_row(str(item.get("id", "")), item.get("title", ""), artist, year)

    elif type == "artists":
        table.add_column("ID",           style="dim",  no_wrap=True)
        table.add_column("Name",         style="bold")
        table.add_column("Albums count", style="dim")
        for item in items:
            table.add_row(
                str(item.get("id", "")),
                item.get("name", ""),
                str(item.get("albums_count", "")),
            )

    elif type == "playlists":
        table.add_column("ID",     style="dim",  no_wrap=True)
        table.add_column("Name",   style="bold")
        table.add_column("Tracks", style="dim")
        table.add_column("Owner")
        for item in items:
            owner = (item.get("owner") or {}).get("name", "")
            table.add_row(
                str(item.get("id", "")),
                item.get("name", ""),
                str(item.get("tracks_count", "")),
                owner,
            )

    console.print(table)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()

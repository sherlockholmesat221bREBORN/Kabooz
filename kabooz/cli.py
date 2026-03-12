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
    TokenExpiredError,
)
from .models.track import Track
from .models.album import Album
from .quality import Quality
from .url import parse_url

# ── App ────────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="qobuz",
    help="Unofficial Qobuz CLI — download tracks, albums, playlists, and more.",
    add_completion=False,
)
console     = Console()
err_console = Console(stderr=True)


# ── Config helpers ─────────────────────────────────────────────────────────

def _cfg() -> QobuzConfig:
    return load_config()


def _build_client(cfg: Optional[QobuzConfig] = None) -> QobuzClient:
    cfg = cfg or _cfg()
    creds = cfg.credentials

    # Pool mode — credentials live inside the pool file, not the config.
    if creds.pool:
        try:
            return QobuzClient.from_token_pool(creds.pool)
        except Exception as exc:
            err_console.print(f"[red]Failed to load token pool:[/red] {exc}")
            raise typer.Exit(code=1)

    # Session mode — app credentials required.
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

    client = QobuzClient.from_credentials(app_id=app_id, app_secret=app_secret)
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
    )


def _handle_auth_error(exc: Exception) -> None:
    err_console.print(
        f"[red]Authentication error:[/red] {exc}\n"
        "Run [bold]qobuz login[/bold] to refresh your session."
    )
    raise typer.Exit(code=1)


# ── Metadata helpers ───────────────────────────────────────────────────────

def _needs_tagging(path: Path, check_cover: bool = True) -> bool:
    """Return True if the file is missing title/artist tags or cover art."""
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
    """Tag, optionally fetch lyrics, optionally run MusicBrainz lookup."""
    if not cfg.tagging.enabled:
        return

    lyrics_result = None
    if fetch_lyrics_flag:
        lyrics_result = _fetch_lyrics_for(track_obj, album_obj)

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

    if cfg.musicbrainz.enabled and track_obj.isrc:
        mb = lookup_isrc(track_obj.isrc)
        apply_mb_tags(result.path, mb)


def _save_cover_file(folder: Path, album: Album) -> None:
    """Download cover art as cover.jpg into the given folder."""
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


# ── Commands ───────────────────────────────────────────────────────────────

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

    # ── Token pool mode — handled first, no app credentials needed ───────
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
    
    # Only prompt for app credentials if not using a pool
    resolved_app_id     = app_id     or os.environ.get("QOBUZ_APP_ID")     or cfg.credentials.app_id
    resolved_app_secret = app_secret or os.environ.get("QOBUZ_APP_SECRET")  or cfg.credentials.app_secret
    
    if not resolved_app_id:
        resolved_app_id = typer.prompt("App ID")
    if not resolved_app_secret:
        resolved_app_secret = typer.prompt("App Secret", hide_input=True)

    cfg.credentials.app_id     = resolved_app_id
    cfg.credentials.app_secret = resolved_app_secret
    cfg.credentials.pool       = ""

    # ── Direct token mode ──────────────────────────────────────────────────
    if token:
        if not user_id:
            user_id = typer.prompt("User ID")
        try:
            session = client.login(token=token, user_id=user_id)
        except Exception as exc:
            err_console.print(f"[red]Login failed:[/red] {exc}")
            raise typer.Exit(code=1)

    # ── Username + password ────────────────────────────────────────────────
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

    console.print(
        f"[green]Logged in as[/green] {session.user_email or session.user_id}. "
        f"Session saved to [bold]{_SESSION_PATH}[/bold]."
    )


@app.command()
def config(
    show: bool = typer.Option(False, "--show", help="Print current config"),
    set_: Optional[str] = typer.Option(
        None, "--set",
        help="Set a config value. Format: section.key=value  e.g. download.max_workers=4"
    ),
) -> None:
    """
    View or update the configuration file.

    \b
    Examples:
        qobuz config --show
        qobuz config --set download.max_workers=4
        qobuz config --set download.external_downloader="aria2c -x 16 -s 16 -d {dir} -o {filename} {url}"
        qobuz config --set naming.album="{albumartist}/{album} [{quality}]/{track:02d}. {title}"
        qobuz config --set tagging.save_cover_file=true
        qobuz config --set musicbrainz.enabled=true
    """
    if show:
        cfg = _cfg()
        import tomli_w, dataclasses
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

        # Coerce common types.
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
                    coerced = value  # keep as string

        update_config({section: {key: coerced}})
        console.print(f"[green]Set[/green] {section}.{key} = {coerced!r}")
        return

    # No flags — print help.
    console.print("Use [bold]--show[/bold] to view config or [bold]--set section.key=value[/bold] to update it.")
    console.print(f"Config file: [bold]{_CONFIG_PATH}[/bold]")


@app.command()
def track(
    url_or_id:   str          = typer.Argument(..., help="Track ID or Qobuz URL"),
    output:      Optional[Path] = typer.Option(None,  "-o", "--output"),
    quality:     Optional[str]  = typer.Option(None,  "-q", "--quality"),
    lyrics:      Optional[bool] = typer.Option(None,  "--lyrics/--no-lyrics"),
    cover:       Optional[bool] = typer.Option(None,  "--cover/--no-cover"),
    save_cover:  Optional[bool] = typer.Option(None,  "--save-cover/--no-save-cover"),
    workers:     Optional[int]  = typer.Option(None,  "--workers", "-j"),
    downloader:  Optional[str]  = typer.Option(None,  "--downloader"),
    template:    Optional[str]  = typer.Option(None,  "--template", help="Naming template override"),
) -> None:
    """
    Download a single track. Treated as a standalone single — no album
    subfolder regardless of what album it belongs to on Qobuz.
    """
    cfg = _cfg()

    dest_dir     = output      or Path(cfg.download.output_dir)
    q_str        = quality     or cfg.download.quality
    embed_cover  = cover       if cover     is not None else cfg.tagging.embed_cover
    save_cov     = save_cover  if save_cover is not None else cfg.tagging.save_cover_file
    do_lyrics    = lyrics      if lyrics    is not None else cfg.tagging.fetch_lyrics
    n_workers    = workers     or cfg.download.max_workers
    ext_dl       = downloader  or cfg.download.external_downloader

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

    console.print(f"[cyan]Downloading[/cyan] {track_obj.title}")

    # Use single template for standalone track downloads.
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
        ) as dl:
            result = dl.download_track(
                track=track_obj,
                url_info=url_info,
                dest_dir=dest_dir,
                album=None,         # single — no album context
                on_progress=on_progress,
            )

    if result.skipped and not _needs_tagging(result.path, check_cover=embed_cover):
        console.print(f"[yellow]Skipped[/yellow] (complete + tagged): {result.path}")
        return

    if result.skipped:
        console.print("    [yellow]File complete but missing tags — re-tagging.[/yellow]")

    _post_download(result, track_obj, None, cfg, embed_cover, do_lyrics, save_cov)
    console.print(f"[green]Done:[/green] {result.path}")


@app.command()
def album(
    url_or_id:   str           = typer.Argument(..., help="Album ID or Qobuz URL"),
    output:      Optional[Path] = typer.Option(None,  "-o", "--output"),
    quality:     Optional[str]  = typer.Option(None,  "-q", "--quality"),
    lyrics:      Optional[bool] = typer.Option(None,  "--lyrics/--no-lyrics"),
    cover:       Optional[bool] = typer.Option(None,  "--cover/--no-cover"),
    save_cover:  Optional[bool] = typer.Option(None,  "--save-cover/--no-save-cover"),
    goodies:     Optional[bool] = typer.Option(None,  "--goodies/--no-goodies", help="Download bonus files"),
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
    if release_type in ("single",):
        tmpl = template or cfg.naming.single
    elif release_type == "ep":
        tmpl = template or cfg.naming.ep
    elif release_type == "compilation":
        tmpl = template or cfg.naming.compilation
    else:
        tmpl = template or cfg.naming.album

    artist_name = album_obj.artist.name if album_obj.artist else "Unknown Artist"
    console.print(
        f"[cyan]Downloading album:[/cyan] {album_obj.title} "
        f"by {artist_name} ({album_obj.tracks.total} tracks)"
    )
    if album_obj.goodies and do_goodies:
        console.print(f"  [dim]+{len(album_obj.goodies)} goodie(s)[/dim]")

    tagger    = Tagger()
    total     = album_obj.tracks.total
    succeeded = 0
    skipped   = 0
    failed    = 0

    with Downloader(
        read_timeout=cfg.download.read_timeout,
        connect_timeout=cfg.download.connect_timeout,
        max_workers=n_workers,
        external_downloader=ext_dl,
        naming_template=tmpl,
    ) as dl:
        for i, track_summary in enumerate(album_obj.tracks.items, 1):
            console.print(f"  [{i}/{total}] {track_summary.title}")

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

            if result.skipped:
                if not _needs_tagging(result.path, check_cover=embed_cover):
                    console.print("    [yellow]Already complete, skipped.[/yellow]")
                    skipped += 1
                    continue
                console.print("    [yellow]File complete but missing tags — re-tagging.[/yellow]")

            _post_download(result, track_obj, album_obj, cfg, embed_cover, do_lyrics, save_cov)
            succeeded += 1

        # ── Goodies ───────────────────────────────────────────────────────
        if do_goodies and album_obj.goodies:
            # Resolve album dir from first downloaded/skipped track.
            album_dir = dest_dir
            all_results = [r for r in (locals().get("result"),) if r is not None]
            # Walk to find actual album folder.
            for r in all_results:
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
    m3u:        bool           = typer.Option(False, "--m3u", help="Write an .m3u8 playlist file"),
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
    ) as dl:
        for i, pl_track in enumerate(pl.tracks.items, 1):
            console.print(f"  [{i}/{pl.tracks_count}] {pl_track.title}")

            try:
                track_obj = client.get_track(str(pl_track.id))
            except (TokenExpiredError, InvalidCredentialsError, NoAuthError) as exc:
                _handle_auth_error(exc)
            except Exception as exc:
                err_console.print(f"    [red]Could not fetch track metadata: {exc}[/red]")
                failed += 1
                continue

            # Fetch album for cover and full metadata.
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
                        album=None,         # playlist uses flat structure via template
                        on_progress=on_progress,
                        playlist_name=pl.name,
                        playlist_index=i,
                    )
                except Exception as exc:
                    err_console.print(f"    [red]Download failed: {exc}[/red]")
                    failed += 1
                    continue

            if result.skipped:
                if not _needs_tagging(result.path, check_cover=embed_cover):
                    console.print("    [yellow]Already complete, skipped.[/yellow]")
                    skipped += 1
                    m3u_lines.append(str(result.path))
                    continue
                console.print("    [yellow]File complete but missing tags — re-tagging.[/yellow]")

            _post_download(result, track_obj, album_obj, cfg, embed_cover, do_lyrics, save_cov)
            m3u_lines.append(str(result.path))
            succeeded += 1

    # ── Write .m3u8 ───────────────────────────────────────────────────────
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
    query: str  = typer.Argument(..., help="Search query"),
    type:  str  = typer.Option("tracks", "--type", "-t",
                               help="tracks, albums, artists, playlists"),
    limit: int  = typer.Option(10, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
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
        table.add_column("ID",     style="dim", no_wrap=True)
        table.add_column("Title",  style="bold")
        table.add_column("Artist")
        table.add_column("Year",   style="dim")
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


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()

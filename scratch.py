#!/usr/bin/env python3
"""
scratch.py — exemplary kabooz usage script.

A runnable reference showing best practices:
  · session construction (config file or env vars)
  · error handling patterns
  · all major operations with clean rich output

Quick start
───────────
  Option A — use existing login:
      python scratch.py

  Option B — env var credentials (CI / quick tests):
      QOBUZ_APP_ID=xxx QOBUZ_APP_SECRET=yyy \\
      QOBUZ_TOKEN=zzz QOBUZ_USER_ID=12345 \\
      python scratch.py

  Option C — login first, then run:
      qobuz login -u me@example.com -p secret
      python scratch.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn, DownloadColumn, Progress,
    SpinnerColumn, TextColumn, TransferSpeedColumn,
)
from rich.table import Table

try:
    from kabooz import QobuzSession, Quality
    from kabooz.client import QobuzClient
    from kabooz.exceptions import (
        QobuzError, APIError, NotStreamableError,
        InvalidCredentialsError, NoAuthError,
    )
except ImportError:
    print("kabooz not found — run: pip install -e .", file=sys.stderr)
    sys.exit(1)

console = Console()
err     = Console(stderr=True)

# ═══════════════════════════════════════════════════════════════════════════
# Session helpers
# ═══════════════════════════════════════════════════════════════════════════

def _session_from_env() -> QobuzSession | None:
    """Build a session from env vars. Returns None if vars are missing."""
    app_id     = os.environ.get("QOBUZ_APP_ID")
    app_secret = os.environ.get("QOBUZ_APP_SECRET")
    token      = os.environ.get("QOBUZ_TOKEN")
    user_id    = os.environ.get("QOBUZ_USER_ID")

    if not all([app_id, app_secret, token, user_id]):
        return None

    client = QobuzClient.from_credentials(app_id=app_id, app_secret=app_secret)
    client.login(token=token, user_id=user_id)
    console.print("[dim]Auth: environment variables[/dim]")
    return QobuzSession.from_client(client)


def get_session() -> QobuzSession:
    """
    Return a live QobuzSession, env vars first then saved config.
    Exits cleanly with instructions if neither is available.
    """
    sess = _session_from_env()
    if sess:
        return sess

    try:
        sess = QobuzSession.from_config()
        console.print("[dim]Auth: config file[/dim]")
        return sess
    except FileNotFoundError:
        err.print(
            "\n[red bold]Not logged in.[/red bold]\n\n"
            "Option A — run [bold]qobuz login -u EMAIL -p PASSWORD[/bold]\n"
            "Option B — set QOBUZ_APP_ID / QOBUZ_APP_SECRET / QOBUZ_TOKEN / QOBUZ_USER_ID\n"
        )
        sys.exit(1)
    except QobuzError as exc:
        err.print(f"[red]Session error:[/red] {exc}")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# Demo functions
# ═══════════════════════════════════════════════════════════════════════════

def demo_profile(sess: QobuzSession) -> None:
    """Fetch and display the authenticated user's profile."""
    console.rule("[bold cyan]User profile")
    try:
        p = sess.get_profile()
    except QobuzError as exc:
        err.print(f"[red]profile failed:[/red] {exc}")
        return

    lines = [
        f"[bold]{p.full_name}[/bold]  ({p.login})",
        f"Email:     {p.email or '—'}",
        f"Country:   {p.country_code or '—'}",
    ]
    if p.credential:
        lines += [
            f"Plan:      [cyan]{p.credential.label}[/cyan]",
            f"Quality:   {p.credential.max_audio_quality}",
            f"Hi-res:    {'✓' if p.credential.hires_streaming else '✗'}",
        ]
    console.print(Panel("\n".join(lines), title="[bold]Account[/bold]", expand=False))


def demo_search(sess: QobuzSession) -> None:
    """Search the catalog and show results in a table."""
    console.rule("[bold cyan]Search")
    query = "Daft Punk"
    console.print(f"Searching albums: [bold]{query!r}[/bold]…")

    results = sess.client.search(query=query, type="albums", limit=6)
    albums  = results.get("albums", {}).get("items", [])
    total   = results.get("albums", {}).get("total", 0)

    table = Table(title=f'Albums matching "{query}"  ({total} total)', show_lines=False)
    table.add_column("ID",      style="dim",  no_wrap=True)
    table.add_column("Title",   style="bold")
    table.add_column("Artist")
    table.add_column("Year",    style="dim",  width=6)
    table.add_column("Quality", style="cyan", width=14)

    for a in albums:
        bd  = a.get("maximum_bit_depth")
        sr  = a.get("maximum_sampling_rate")
        q   = f"{bd}bit / {int(sr) if sr == int(sr) else sr}kHz" if bd and sr else "CD"
        table.add_row(
            str(a.get("id", "")),
            a.get("title", ""),
            (a.get("artist") or {}).get("name", ""),
            (a.get("release_date_original") or "")[:4],
            q,
        )
    console.print(table)


def demo_track_info(sess: QobuzSession, track_id: str = "64868887") -> None:
    """Fetch a single track and pretty-print its metadata."""
    console.rule("[bold cyan]Track detail")
    console.print(f"Fetching track [bold]{track_id}[/bold]…")

    try:
        t = sess.client.get_track(track_id)
    except NotFoundError:
        err.print(f"[red]Track {track_id} not found.[/red]")
        return
    except QobuzError as exc:
        err.print(f"[red]API error:[/red] {exc}")
        return

    dur = f"{t.duration // 60}:{t.duration % 60:02d}" if t.duration else "—"
    lines = [
        f"[bold]{t.display_title}[/bold]",
        f"Artist:    {t.performer.name if t.performer else '—'}",
        f"Album:     {t.album.title if t.album else '—'}",
        f"Duration:  {dur}",
        f"ISRC:      {t.isrc or '—'}",
        f"Track #:   {t.track_number} / Disc {t.media_number}",
    ]
    console.print(Panel("\n".join(lines), title="Track", expand=False))


def demo_download_track(
    sess: QobuzSession,
    dest: Path,
    track_id: str = "64868887",
) -> None:
    """Download a single track with a live progress bar."""
    console.rule("[bold cyan]Single track download")
    console.print(f"Track [bold]{track_id}[/bold] → [dim]{dest}[/dim]")

    task_holder: list = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        console=console,
        transient=True,
    ) as prog:
        def on_progress(done: int, total: int) -> None:
            if task_holder:
                prog.update(task_holder[0], completed=done, total=total or None)

        task_holder.append(prog.add_task("downloading…", total=None))

        try:
            result = sess.download_track(
                track_id,
                quality=Quality.FLAC_16,
                dest_dir=dest,
                on_progress=on_progress,
            )
        except NotStreamableError as exc:
            err.print(f"[red]Not streamable:[/red] {exc}")
            return
        except QobuzError as exc:
            err.print(f"[red]Download failed:[/red] {exc}")
            return

    dl = result.download
    if dl.skipped:
        console.print(f"[yellow]Already complete:[/yellow] {dl.path}")
    else:
        mb = dl.total_bytes / 1_048_576
        console.print(f"[green]✓[/green] {dl.path}  [dim]({mb:.1f} MB)[/dim]")

    flags = []
    if result.tagged:       flags.append("tagged")
    if result.lyrics_found: flags.append("lyrics")
    if result.mb_enriched:  flags.append("MusicBrainz")
    if flags:
        console.print(f"   [dim]Post-processing: {', '.join(flags)}[/dim]")


def demo_download_album(
    sess: QobuzSession,
    dest: Path,
    album_id: str = "0060253780893",  # Random Access Memories
) -> None:
    """Download a full album with per-track status lines."""
    console.rule("[bold cyan]Album download")

    # Fetch album info first so we can show the title
    try:
        album = sess.client.get_album(album_id)
    except QobuzError as exc:
        err.print(f"[red]Failed to fetch album:[/red] {exc}")
        return

    console.print(
        f"[bold]{album.display_title}[/bold]  "
        f"[dim]{album.artist.name if album.artist else ''}  "
        f"({album.tracks_count} tracks)[/dim]"
    )
    console.print(f"Destination: [dim]{dest}[/dim]\n")

    def on_start(title: str, idx: int, total: int) -> None:
        console.print(f"  [{idx:>2}/{total}] {title}")

    def on_done(r) -> None:
        dl = r.download
        if dl.skipped:
            console.print("       [yellow]↩ already complete[/yellow]")
        else:
            mb = dl.total_bytes / 1_048_576
            console.print(f"       [green]✓[/green] [dim]{mb:.1f} MB[/dim]")

    try:
        result = sess.download_album(
            album_id,
            quality=Quality.FLAC_16,
            dest_dir=dest,
            on_track_start=on_start,
            on_track_done=on_done,
        )
    except QobuzError as exc:
        err.print(f"[red]Album download failed:[/red] {exc}")
        return

    console.print(
        f"\n[green]Done.[/green]  "
        f"{result.succeeded} downloaded · "
        f"{result.skipped} skipped · "
        f"{result.failed} failed"
    )
    if result.goodies_ok:
        console.print(f"  [dim]{result.goodies_ok} goodie(s) saved[/dim]")


def demo_artist_discography(
    sess: QobuzSession,
    dest: Path,
    artist_id: str = "999",          # Daft Punk
    release_type: str = "album",     # 'album' | 'live' | 'epSingle' | None
) -> None:
    """
    Download an artist discography with album-level progress headers.

    This is the pattern to use — fetch the artist info upfront so you
    always know the total before the loop starts.
    """
    console.rule("[bold cyan]Artist discography")

    # ── 1. Get artist info and release count first ─────────────────────────
    try:
        artist_obj = sess.client.get_artist(artist_id, extras="")
    except QobuzError as exc:
        err.print(f"[red]Cannot fetch artist:[/red] {exc}")
        return

    # Use iter_releases to count pages without downloading everything
    releases = list(sess.client.iter_releases(
        artist_id, release_type=release_type, page_size=100,
    ))
    total_releases = len(releases)

    console.print(
        f"Artist: [bold]{artist_obj.name}[/bold]  "
        f"→  [bold]{total_releases}[/bold] releases"
        + (f" (type: {release_type})" if release_type else "")
    )
    console.print(f"Destination: [dim]{dest}[/dim]\n")

    # ── 2. Download each album with a clear header ─────────────────────────
    album_num   = 0
    grand_ok    = 0
    grand_skip  = 0
    grand_fail  = 0

    def on_track(title: str, idx: int, total: int) -> None:
        console.print(f"    [{idx:>2}/{total}] {title}")

    def on_done(r) -> None:
        dl = r.download
        if dl.skipped:
            console.print("         [yellow]↩ skipped[/yellow]")
        else:
            mb = dl.total_bytes / 1_048_576
            console.print(f"         [green]✓[/green] [dim]{mb:.1f} MB[/dim]")

    for release in releases:
        if not release.id:
            continue
        album_num += 1

        # Fetch the Album object so we have the title before downloading
        try:
            album_obj = sess.client.get_album(release.id)
        except QobuzError:
            grand_fail += 1
            continue

        console.print(
            f"\n[bold cyan][{album_num}/{total_releases}][/bold cyan]  "
            f"[bold]{album_obj.display_title}[/bold]  "
            f"[dim]({album_obj.tracks_count} tracks, "
            f"{(album_obj.release_date_original or '')[:4]})[/dim]"
        )

        try:
            result = sess.download_album(
                release.id,
                quality=Quality.FLAC_16,
                dest_dir=dest,
                on_track_start=on_track,
                on_track_done=on_done,
            )
            grand_ok   += result.succeeded
            grand_skip += result.skipped
            grand_fail += result.failed
        except NotStreamableError:
            console.print("  [yellow]⚠ album not streamable — skipping[/yellow]")
        except QobuzError as exc:
            console.print(f"  [red]⚠ error:[/red] {exc}")
            grand_fail += 1

    # ── 3. Final summary ───────────────────────────────────────────────────
    console.print(
        f"\n[green]Discography done.[/green]  "
        f"{album_num} albums · {grand_ok} tracks · "
        f"{grand_skip} skipped · {grand_fail} failed"
    )


def demo_favorites(sess: QobuzSession) -> None:
    """List the first page of the user's favourite tracks."""
    console.rule("[bold cyan]Favorites")
    console.print("Fetching favourite tracks (first page)…")

    try:
        favs = sess.client.get_user_favorites(type="tracks", limit=8)
    except QobuzError as exc:
        err.print(f"[red]Failed:[/red] {exc}")
        return

    if not favs.tracks or not favs.tracks.items:
        console.print("[dim]No favourite tracks.[/dim]")
        return

    t = Table(
        title=f"Favourite tracks (showing {len(favs.tracks.items)} of {favs.tracks.total})",
        show_lines=False,
    )
    t.add_column("ID",     style="dim",  no_wrap=True)
    t.add_column("Title",  style="bold")
    t.add_column("Artist")
    t.add_column("Album",  style="dim")

    for track in favs.tracks.items:
        t.add_row(
            str(track.id),
            track.display_title,
            track.performer.name if track.performer else "—",
            track.album.title    if track.album     else "—",
        )
    console.print(t)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

DOWNLOAD_DIR = Path("./qobuz-scratch-downloads")

def main() -> None:
    console.print(Panel(
        "[bold cyan]kabooz · scratch.py[/bold cyan]\n"
        "[dim]Edit the IDs / flags below to test different operations.[/dim]",
        expand=False,
    ))

    sess = get_session()
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    # ── Read-only demos (safe, always run) ────────────────────────────────
    demo_profile(sess)
    demo_search(sess)
    demo_track_info(sess, track_id="64868887")   # Daft Punk — One More Time
    demo_favorites(sess)

    # ── Download demos (comment out to skip) ─────────────────────────────
    # demo_download_track(sess, DOWNLOAD_DIR, track_id="64868887")
    # demo_download_album(sess, DOWNLOAD_DIR, album_id="0060253780893")
    # demo_artist_discography(sess, DOWNLOAD_DIR, artist_id="999", release_type="album")

    console.print("\n[green]All demos complete.[/green]")


if __name__ == "__main__":
    # Lazy import — only needed in demo_track_info
    from kabooz.exceptions import NotFoundError
    main()

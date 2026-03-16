#!/usr/bin/env python3
# kabooz/tui.py
"""
Qobuz TUI — terminal music player.

Architecture rule: presentation layer only.  Never imports from
kabooz.client, never calls sess.client.anything, never duplicates
quality / URL logic.  Everything goes through QobuzSession methods.

Requirements
────────────
    pip install 'textual>=0.61'
    pkg install mpv      (Termux)
    apt install mpv      (Debian/Ubuntu)
    brew install mpv     (macOS)

Run from the project root:
    python -m kabooz.tui
    python -m kabooz.tui --dev

Layout
──────
    ┌─────────────────────────────────────┐
    │  Header                             │
    ├─────────────────────────────────────┤
    │  🔍 Search: ___________________     │  ← always visible, focused on start
    │  / = focus  Esc = results           │
    ├─────────────────────────────────────┤
    │  Results │ Album │ Queue │ For You  │
    │  (tab content)                      │
    ├─────────────────────────────────────┤
    │  Now playing bar                    │
    └─────────────────────────────────────┘

Threading rule
──────────────
Every @work(thread=True) body runs in an OS thread.  Textual's DOM is
NOT thread-safe.  Inside widget workers, all DOM access must go through
self.app.call_from_thread(fn).  Inside App workers, self.call_from_thread(fn).
Never call query_one / set reactives directly from a worker thread.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── Loud import errors ────────────────────────────────────────────────────
try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.message import Message
    from textual.reactive import reactive
    from textual.widget import Widget
    from textual.widgets import (
        DataTable, Footer, Header, Input,
        Label, Static, TabbedContent, TabPane,
    )
    from textual import on, work
except ImportError as _e:
    print(f"\n[tui] textual not installed: {_e}", file=sys.stderr)
    print("  Fix: pip install 'textual>=0.61'\n", file=sys.stderr)
    sys.exit(1)

try:
    from kabooz.session import QobuzSession, StreamInfo
    from kabooz.quality import Quality
    from kabooz.exceptions import QobuzError, NotStreamableError
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from kabooz.session import QobuzSession, StreamInfo
        from kabooz.quality import Quality
        from kabooz.exceptions import QobuzError, NotStreamableError
    except ImportError as _e:
        print(f"\n[tui] kabooz not found: {_e}", file=sys.stderr)
        print("  Fix: cd project_root && pip install -e .\n", file=sys.stderr)
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════
# mpv IPC backend
# ══════════════════════════════════════════════════════════════════════════

class MpvPlayer:
    """Controls an mpv subprocess via its JSON IPC socket."""

    def __init__(self) -> None:
        self._sock = Path(tempfile.gettempdir()) / "qobuz-tui-mpv.sock"
        self._proc: Optional[subprocess.Popen] = None
        self.available: bool = bool(shutil.which("mpv"))

    def start(self) -> bool:
        if not self.available:
            return False
        if self._proc and self._proc.poll() is None:
            return True
        self._sock.unlink(missing_ok=True)
        self._proc = subprocess.Popen(
            [
                "mpv", "--no-video", "--idle=yes", "--keep-open=yes",
                f"--input-ipc-server={self._sock}",
                "--no-terminal", "--really-quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            time.sleep(0.1)
            if self._sock.exists():
                return True
        return False

    def quit(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._ipc("quit")
            try:
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()

    def _ipc(self, *args: Any) -> Any:
        if not self._sock.exists():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(str(self._sock))
                s.sendall((json.dumps({"command": list(args)}) + "\n").encode())
                buf = b""
                deadline = time.monotonic() + 2.0
                while b"\n" not in buf and time.monotonic() < deadline:
                    try:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    except socket.timeout:
                        break
                for line in buf.split(b"\n"):
                    line = line.strip()
                    if line:
                        try:
                            return json.loads(line).get("data")
                        except Exception:
                            pass
        except Exception:
            pass
        return None

    def load(self, url: str) -> None:       self._ipc("loadfile", url, "replace")
    def toggle_pause(self) -> None:         self._ipc("cycle", "pause")
    def set_pause(self, v: bool) -> None:   self._ipc("set_property", "pause", v)
    def stop(self) -> None:                 self._ipc("stop")
    def seek(self, s: float) -> None:       self._ipc("seek", s, "relative")
    def set_volume(self, v: int) -> None:   self._ipc("set_property", "volume", max(0, min(150, v)))
    def position(self) -> float:            return float(self._ipc("get_property", "time-pos") or 0)
    def duration(self) -> float:            return float(self._ipc("get_property", "duration") or 0)
    def is_paused(self) -> bool:            return bool(self._ipc("get_property", "pause"))
    def is_idle(self) -> bool:              return self._ipc("get_property", "idle-active") is True
    def volume(self) -> int:                return int(self._ipc("get_property", "volume") or 80)


# ══════════════════════════════════════════════════════════════════════════
# Queue model
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class QueueEntry:
    track_id: str
    title:    str
    artist:   str
    album:    str = ""
    duration: int = 0
    stream:   Optional[StreamInfo] = None


class TrackQueue:
    def __init__(self) -> None:
        self._items: list[QueueEntry] = []
        self._index: int = -1

    def add(self, e: QueueEntry) -> None:           self._items.append(e)
    def extend(self, es: list[QueueEntry]) -> None: self._items.extend(es)
    def clear(self) -> None:                        self._items.clear(); self._index = -1

    def current(self) -> Optional[QueueEntry]:
        return self._items[self._index] if 0 <= self._index < len(self._items) else None

    def advance(self) -> Optional[QueueEntry]:
        if self._index + 1 < len(self._items):
            self._index += 1
            return self.current()
        return None

    def back(self) -> Optional[QueueEntry]:
        if self._index > 0:
            self._index -= 1
            return self.current()
        return None

    def play_index(self, i: int) -> Optional[QueueEntry]:
        if 0 <= i < len(self._items):
            self._index = i
            return self.current()
        return None

    def remove_index(self, i: int) -> None:
        if 0 <= i < len(self._items):
            del self._items[i]
            if self._index >= i and self._index > 0:
                self._index -= 1

    @property
    def items(self) -> list[QueueEntry]: return list(self._items)
    @property
    def current_index(self) -> int:      return self._index
    def __len__(self) -> int:            return len(self._items)


# ══════════════════════════════════════════════════════════════════════════
# Search bar — always docked at the top, outside the tabs
# ══════════════════════════════════════════════════════════════════════════

class SearchBar(Widget):
    """
    Persistent top bar. Always visible, focused at startup.
    Pressing / anywhere in the app refocuses it.
    Typing any printable character while something else has focus also
    redirects here (handled in QobuzTUI.on_key).
    """
    DEFAULT_CSS = """
    SearchBar {
        height: 3;
        dock: top;
        background: $surface;
        border-bottom: solid $primary-darken-2;
        padding: 0 1;
        layout: horizontal;
    }
    #sb-label { width: 12; height: 3; content-align: left middle;
                color: $primary; text-style: bold; }
    #sb-input { width: 1fr; height: 3; }
    #sb-hint  { width: 28; height: 3; content-align: right middle;
                color: $text-muted; text-style: dim; }
    """

    class Submitted(Message):
        def __init__(self, query: str) -> None:
            super().__init__()
            self.query = query

    def compose(self) -> ComposeResult:
        yield Static("🔍 Search:", id="sb-label")
        yield Input(placeholder="Type an artist, album or track…  then press Enter", id="sb-input")
        yield Static("/ = focus here  Esc = results", id="sb-hint")

    def on_mount(self) -> None:
        self.query_one("#sb-input", Input).focus()

    def focus_input(self) -> None:
        self.query_one("#sb-input", Input).focus()

    def get_input(self) -> Input:
        return self.query_one("#sb-input", Input)

    @on(Input.Submitted, "#sb-input")
    def _submitted(self, ev: Input.Submitted) -> None:
        q = ev.value.strip()
        if q:
            self.post_message(self.Submitted(q))


# ══════════════════════════════════════════════════════════════════════════
# Now-playing bar — always docked at the bottom
# ══════════════════════════════════════════════════════════════════════════

class NowPlayingBar(Widget):
    DEFAULT_CSS = """
    NowPlayingBar {
        height: 5;
        dock: bottom;
        background: $surface;
        border-top: tall $primary-darken-3;
        padding: 0 2;
        layout: vertical;
    }
    NowPlayingBar Static { height: 1; }
    #np-title    { color: $text;       text-style: bold; }
    #np-sub      { color: $text-muted; }
    #np-progress { color: $primary;    }
    #np-controls { color: $text-muted; text-style: dim; }
    """

    title   = reactive("No track loaded")
    artist  = reactive("")
    album   = reactive("")
    quality = reactive("")
    pos     = reactive(0.0)
    dur     = reactive(0.0)
    paused  = reactive(True)
    volume  = reactive(80)

    def compose(self) -> ComposeResult:
        yield Static("", id="np-title")
        yield Static("", id="np-sub")
        yield Static("", id="np-progress")
        yield Static("", id="np-controls")

    @staticmethod
    def _fmt(s: float) -> str:
        s = int(max(0, s))
        return f"{s // 60}:{s % 60:02d}"

    def _redraw(self) -> None:
        title_line = self.title
        if self.quality:
            title_line += f"  [dim cyan]{self.quality}[/dim cyan]"
        self.query_one("#np-title", Static).update(title_line)

        sub = self.artist
        if self.album:
            sub += f"  [dim]—[/dim]  {self.album}"
        self.query_one("#np-sub", Static).update(sub)

        bar_width = 40
        if self.dur > 0:
            frac   = min(self.pos / self.dur, 1.0)
            filled = int(frac * bar_width)
            bar    = f"[bold cyan]{'█' * filled}[/bold cyan][dim]{'░' * (bar_width - filled)}[/dim]"
            times  = f"  {self._fmt(self.pos)} [dim]/[/dim] {self._fmt(self.dur)}"
        else:
            bar   = f"[dim]{'░' * bar_width}[/dim]"
            times = "  --:-- [dim]/[/dim] --:--"
        self.query_one("#np-progress", Static).update(bar + times)

        play_icon = "[green]▶ PLAYING[/green]" if not self.paused else "[yellow]⏸ PAUSED[/yellow]"
        self.query_one("#np-controls", Static).update(
            f"{play_icon}  "
            "[dim]space[/dim]=pause  [dim]←/→[/dim]=±10s  "
            f"[dim]\\[/\\][/dim]=vol [bold]{self.volume}%[/bold]  "
            "[dim]n[/dim]=next  [dim]p[/dim]=prev  "
            "[dim]r[/dim]=radio  [dim]q[/dim]=quit"
        )

    def watch_title(self,   _: Any) -> None: self._redraw()
    def watch_artist(self,  _: Any) -> None: self._redraw()
    def watch_album(self,   _: Any) -> None: self._redraw()
    def watch_quality(self, _: Any) -> None: self._redraw()
    def watch_pos(self,     _: Any) -> None: self._redraw()
    def watch_dur(self,     _: Any) -> None: self._redraw()
    def watch_paused(self,  _: Any) -> None: self._redraw()
    def watch_volume(self,  _: Any) -> None: self._redraw()


# ══════════════════════════════════════════════════════════════════════════
# Results pane  (search results — Input lives in SearchBar, not here)
# ══════════════════════════════════════════════════════════════════════════

_WELCOME = (
    "[dim]Type in the search bar above and press [bold white]Enter[/bold white] to search.\n"
    "Results will appear below.\n\n"
    "[bold white]Shortcuts:[/bold white]  "
    "[bold]/[/bold]=focus search  [bold]Enter[/bold]=play/browse  "
    "[bold]↑↓[/bold]=navigate  [bold]Esc[/bold]=back to search  "
    "[bold]space[/bold]=pause  [bold]n/p[/bold]=next/prev  "
    "[bold]←/→[/bold]=±10s  [bold][][/bold]=volume  "
    "[bold]r[/bold]=radio  [bold]q[/bold]=quit[/dim]"
)


class ResultsPane(Widget):
    # No competing 1fr widgets — status is auto-height, table fills the rest.
    DEFAULT_CSS = """
    ResultsPane           { width: 100%; height: 100%; layout: vertical; }
    #rp-status            { height: auto; margin: 1 2 0 2; color: $text-muted; }
    ResultsPane DataTable { height: 1fr;  margin: 0 2; }
    """

    class TrackChosen(Message):
        def __init__(self, entry: QueueEntry) -> None:
            super().__init__(); self.entry = entry

    class AlbumChosen(Message):
        def __init__(self, entity_id: str, title: str) -> None:
            super().__init__(); self.entity_id = entity_id; self.title = title

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []

    def compose(self) -> ComposeResult:
        # Single status label whose text we swap; no separate welcome widget
        # so there is no competing height that could steal space from the table.
        yield Static(_WELCOME, id="rp-status")
        yield DataTable(id="rp-table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#rp-table", DataTable).add_columns(
            "", "Title / Name", "Artist / Owner", "Context", "ID"
        )

    # ── Called from the main thread by the App ─────────────────────────

    def show_searching(self, query: str) -> None:
        self.query_one("#rp-status", Static).update(
            f"[dim]Searching for [bold]{query}[/bold]…[/dim]"
        )
        self.query_one("#rp-table", DataTable).clear()

    def show_results(self, rows: list[tuple], new_rows: list[dict]) -> None:
        self._rows = new_rows
        table = self.query_one("#rp-table", DataTable)
        table.clear()   # guard against duplicate keys on rapid re-searches
        for i, r in enumerate(rows):
            table.add_row(*r, key=str(i))
        count = len(rows)
        self.query_one("#rp-status", Static).update(
            f"[dim]{count} result{'s' if count != 1 else ''} — "
            "↑↓=navigate  Enter=play/browse  Esc=back to search[/dim]"
            if count else "[dim]No results.[/dim]"
        )
        if count:
            table.focus()

    def show_error(self, msg: str) -> None:
        self.query_one("#rp-status", Static).update(
            f"[red]{msg}[/red]\n\n{_WELCOME}"
        )

    @on(DataTable.RowSelected, "#rp-table")
    def _selected(self, ev: DataTable.RowSelected) -> None:
        try:
            idx = int(str(ev.row_key.value))
        except (TypeError, ValueError):
            return
        if idx >= len(self._rows):
            return
        item = self._rows[idx]
        if item["type"] == "track":
            t = item["data"]
            self.post_message(self.TrackChosen(QueueEntry(
                track_id=str(t.get("id", "")),
                title=t.get("title", ""),
                artist=(t.get("performer") or {}).get("name", ""),
                album=(t.get("album") or {}).get("title", ""),
                duration=t.get("duration", 0),
            )))
        elif item["type"] in ("album", "artist"):
            d         = item["data"]
            aid       = str(d.get("id", ""))
            entity_id = f"artist:{aid}" if item["type"] == "artist" else aid
            name      = d.get("title") or d.get("name", "")
            self.post_message(self.AlbumChosen(entity_id, name))

    def on_key(self, ev: Any) -> None:
        if ev.key == "escape":
            self.app.query_one(SearchBar).focus_input()
            ev.stop()


# ══════════════════════════════════════════════════════════════════════════
# Album / artist drill-down pane
# ══════════════════════════════════════════════════════════════════════════

class AlbumPane(Widget):
    DEFAULT_CSS = """
    AlbumPane           { width: 100%; height: 100%; }
    #a-header           { margin: 1 2 0 2; color: $primary; text-style: bold; }
    #a-hint             { margin: 0 2;     color: $text-muted; text-style: dim; }
    AlbumPane DataTable { margin: 1 2;     height: 1fr; }
    """

    class TrackChosen(Message):
        def __init__(self, entry: QueueEntry) -> None:
            super().__init__(); self.entry = entry

    class AddAllChosen(Message):
        def __init__(self, entries: list[QueueEntry]) -> None:
            super().__init__(); self.entries = entries

    def __init__(self, session: QobuzSession) -> None:
        super().__init__()
        self._session = session
        self._entries: list[QueueEntry] = []

    def compose(self) -> ComposeResult:
        yield Static("Select an album or artist from Results", id="a-header")
        yield Static("[dim]Enter=play  A=add all  Esc=back to search[/dim]", id="a-hint")
        yield DataTable(id="a-table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#a-table", DataTable).add_columns("#", "Title", "Artist", "Duration")

    def load(self, entity_id: str, title: str) -> None:
        self.query_one("#a-header", Static).update(f"[dim]Loading:[/dim]  {title}")
        self.query_one("#a-hint",   Static).update("")
        self._load(entity_id, title)

    @work(thread=True)
    def _load(self, entity_id: str, title: str) -> None:
        entries: list[QueueEntry] = []
        header = title
        hint   = ""

        try:
            if entity_id.startswith("artist:"):
                aid    = entity_id.split(":", 1)[1]
                artist = self._session.get_artist(aid, extras="albums", limit=20)
                n_alb  = len(artist.albums.items) if artist.albums else 0
                header = f"{artist.name}  [dim]({n_alb} albums)[/dim]"
                hint   = "Enter=browse album  Esc=back to search"
                for alb in (artist.albums.items if artist.albums else [])[:20]:
                    alb_id    = str(alb.id)
                    alb_title = getattr(alb, "display_title", None) or getattr(alb, "title", alb_id)
                    entries.append(QueueEntry(
                        track_id=f"album:{alb_id}",
                        title=alb_title,
                        artist=artist.name,
                    ))
            else:
                album       = self._session.get_album(entity_id)
                artist_name = album.artist.name if album.artist else ""
                year        = (album.release_date_original or "")[:4]
                n_tracks    = album.tracks_count or 0
                header = (
                    f"{getattr(album, 'display_title', album.title)}"
                    f"  [dim]—[/dim]  {artist_name}"
                    f"  [dim]({n_tracks} tracks"
                    + (f", {year}" if year else "") + ")[/dim]"
                )
                hint = "Enter=play  A=add all  R=radio  Esc=back to search"
                for t in (album.tracks.items if album.tracks else []):
                    entries.append(QueueEntry(
                        track_id=str(t.id),
                        title=getattr(t, "display_title", None) or t.title,
                        artist=(t.performer.name if t.performer else artist_name),
                        album=getattr(album, "display_title", album.title),
                        duration=t.duration or 0,
                    ))
        except Exception as exc:
            def _err() -> None:
                self.query_one("#a-header", Static).update(f"[red]Load error: {exc}[/red]")
            self.app.call_from_thread(_err)
            return

        def _fill() -> None:
            self._entries = entries
            table = self.query_one("#a-table", DataTable)
            table.clear()
            self.query_one("#a-header", Static).update(header)
            self.query_one("#a-hint",   Static).update(f"[dim]{hint}[/dim]" if hint else "")
            for i, e in enumerate(entries):
                dur = f"{e.duration // 60}:{e.duration % 60:02d}" if e.duration else "—"
                table.add_row(str(i + 1), e.title, e.artist, dur, key=str(i))
            if entries:
                table.focus()

        self.app.call_from_thread(_fill)

    @on(DataTable.RowSelected, "#a-table")
    def _selected(self, ev: DataTable.RowSelected) -> None:
        try:
            idx = int(str(ev.row_key.value))
        except (TypeError, ValueError):
            return
        if idx >= len(self._entries):
            return
        e = self._entries[idx]
        if e.track_id.startswith("album:"):
            self.load(e.track_id, e.title)
        else:
            self.post_message(self.TrackChosen(e))

    def on_key(self, ev: Any) -> None:
        if ev.key == "a" and self._entries:
            real = [e for e in self._entries if not e.track_id.startswith("album:")]
            if real:
                self.post_message(self.AddAllChosen(real))
        elif ev.key == "escape":
            self.app.query_one(SearchBar).focus_input()
            ev.stop()


# ══════════════════════════════════════════════════════════════════════════
# Queue pane
# ══════════════════════════════════════════════════════════════════════════

class QueuePane(Widget):
    DEFAULT_CSS = """
    QueuePane           { width: 100%; height: 100%; }
    #q-label            { margin: 1 2 0 2; color: $text-muted; }
    QueuePane DataTable { margin: 1 2;     height: 1fr; }
    """

    class JumpTo(Message):
        def __init__(self, index: int) -> None:
            super().__init__(); self.index = index

    def __init__(self) -> None:
        super().__init__()
        self._queue: Optional[TrackQueue] = None

    def compose(self) -> ComposeResult:
        yield Label("Queue  [dim](Enter=jump  D=remove  Esc=search)[/dim]", id="q-label")
        yield DataTable(id="q-table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#q-table", DataTable).add_columns("", "Title", "Artist", "Duration")

    def attach(self, q: TrackQueue) -> None:
        self._queue = q
        self.refresh_table()

    def refresh_table(self) -> None:
        if not self._queue:
            return
        table = self.query_one("#q-table", DataTable)
        table.clear()
        for i, e in enumerate(self._queue.items):
            marker = "[bold green]▶[/bold green]" if i == self._queue.current_index else " "
            dur    = f"{e.duration // 60}:{e.duration % 60:02d}" if e.duration else "—"
            table.add_row(marker, e.title, e.artist, dur, key=str(i))

    @on(DataTable.RowSelected, "#q-table")
    def _selected(self, ev: DataTable.RowSelected) -> None:
        try:
            idx = int(str(ev.row_key.value))
        except (TypeError, ValueError):
            return
        self.post_message(self.JumpTo(idx))

    def on_key(self, ev: Any) -> None:
        if ev.key == "d" and self._queue:
            table = self.query_one("#q-table", DataTable)
            if table.cursor_row is not None:
                self._queue.remove_index(table.cursor_row)
                self.refresh_table()
        elif ev.key == "escape":
            self.app.query_one(SearchBar).focus_input()
            ev.stop()


# ══════════════════════════════════════════════════════════════════════════
# For You / Recommendations pane
# ══════════════════════════════════════════════════════════════════════════

class RecsPane(Widget):
    DEFAULT_CSS = """
    RecsPane           { width: 100%; height: 100%; }
    #r-label           { margin: 1 2 0 2; color: $text-muted; }
    RecsPane DataTable { margin: 1 2;     height: 1fr; }
    """

    class AlbumChosen(Message):
        def __init__(self, entity_id: str, title: str) -> None:
            super().__init__(); self.entity_id = entity_id; self.title = title

    def __init__(self, session: QobuzSession) -> None:
        super().__init__()
        self._session = session
        self._rows:   list[dict] = []

    def compose(self) -> ComposeResult:
        yield Label("For You  [dim](loading…)[/dim]", id="r-label")
        yield DataTable(id="r-table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#r-table", DataTable).add_columns("Category", "Title", "Artist", "Year")
        self._fetch()

    @work(thread=True)
    def _fetch(self) -> None:
        try:
            recs = self._session.get_recommendations(limit=10)
        except Exception as exc:
            def _err() -> None:
                self.query_one("#r-label", Label).update(
                    f"[red]Recommendations failed: {exc}[/red]"
                )
            self.app.call_from_thread(_err)
            return

        rows:     list[tuple] = []
        new_rows: list[dict]  = []

        for item in recs.get("press_awards", [])[:5]:
            iid   = str(item.get("id", ""))
            title = item.get("title", "")
            new_rows.append({"id": iid, "title": title})
            rows.append(("🏆 Press award", title,
                         (item.get("artist") or {}).get("name", ""),
                         (item.get("release_date_original") or "")[:4]))

        for item in recs.get("new_releases", [])[:5]:
            iid   = str(item.get("id", ""))
            title = item.get("title", "")
            new_rows.append({"id": iid, "title": title})
            rows.append(("🆕 New release", title,
                         (item.get("artist") or {}).get("name", ""),
                         (item.get("release_date_original") or "")[:4]))

        for item in recs.get("similar_artists", [])[:4]:
            aid  = str(getattr(item, "id",   "") or "")
            name = str(getattr(item, "name", "") or "")
            new_rows.append({"id": f"artist:{aid}", "title": name})
            rows.append(("🎤 Similar artist", name, "", ""))

        for item in recs.get("featured", [])[:4]:
            iid   = str(item.get("id",   ""))
            name  = str(item.get("name", ""))
            owner = (item.get("owner") or {}).get("name", "")
            new_rows.append({"id": iid, "title": name})
            rows.append(("📋 Featured", name, owner, ""))

        def _fill() -> None:
            self._rows = new_rows
            table = self.query_one("#r-table", DataTable)
            table.clear()
            for i, r in enumerate(rows):
                table.add_row(*r, key=str(i))
            self.query_one("#r-label", Label).update(
                f"For You  [dim]({len(rows)} items — Enter=browse  R=refresh  Esc=search)[/dim]"
            )
            if rows:
                table.focus()

        self.app.call_from_thread(_fill)

    @on(DataTable.RowSelected, "#r-table")
    def _selected(self, ev: DataTable.RowSelected) -> None:
        try:
            idx = int(str(ev.row_key.value))
        except (TypeError, ValueError):
            return
        if idx >= len(self._rows):
            return
        item = self._rows[idx]
        self.post_message(self.AlbumChosen(item["id"], item["title"]))

    def on_key(self, ev: Any) -> None:
        if ev.key == "r":
            self._rows = []
            self._fetch()
        elif ev.key == "escape":
            self.app.query_one(SearchBar).focus_input()
            ev.stop()


# ══════════════════════════════════════════════════════════════════════════
# Main App
# ══════════════════════════════════════════════════════════════════════════

CSS = """
Screen     { background: $background; }
#main-tabs { height: 1fr; }
"""


class QobuzTUI(App):
    TITLE = "Qobuz"
    CSS   = CSS

    BINDINGS = [
        Binding("q",            "quit",         "Quit",       priority=False),
        Binding("slash",        "focus_search", "/=Search",   priority=True),
        Binding("space",        "toggle_pause", "Play/Pause", priority=False),
        Binding("n",            "next_track",   "Next"),
        Binding("p",            "prev_track",   "Prev"),
        Binding("r",            "radio",        "Radio",      priority=False),
        Binding("right",        "seek_fwd",     "+10s",       show=False),
        Binding("left",         "seek_back",    "−10s",       show=False),
        Binding("bracketright", "vol_up",       "Vol+",       show=False),
        Binding("bracketleft",  "vol_down",     "Vol−",       show=False),
    ]

    def __init__(self, session: QobuzSession) -> None:
        super().__init__()
        self._session    = session
        self._mpv        = MpvPlayer()
        self._queue      = TrackQueue()
        self._poll_task: Optional[asyncio.Task] = None
        self._prev_entry: Optional[QueueEntry]  = None
        self._advancing  = False

    # ── Compose ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        yield SearchBar()                          # always visible, docked top
        with TabbedContent(id="main-tabs"):
            with TabPane("Results",   id="tab-results"):
                yield ResultsPane()
            with TabPane("💿 Album",  id="tab-album"):
                yield AlbumPane(self._session)
            with TabPane("📋 Queue",  id="tab-queue"):
                yield QueuePane()
            with TabPane("✨ For You", id="tab-recs"):
                yield RecsPane(self._session)
        yield NowPlayingBar()
        yield Footer()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.query_one(QueuePane).attach(self._queue)
        if not self._mpv.available:
            self.notify(
                "mpv not found — playback disabled.\n"
                "Termux: pkg install mpv  |  Linux: apt install mpv  |  macOS: brew install mpv",
                severity="warning", timeout=12,
            )
        elif not self._mpv.start():
            self.notify("mpv failed to start", severity="error")
        self._poll_task = asyncio.create_task(self._poll_loop())
        self.query_one(SearchBar).focus_input()

    def on_unmount(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        current = self._queue.current()
        if current:
            self._session.report_stream_cancel(current.track_id)
        self._mpv.quit()

    # ── Global key redirect ────────────────────────────────────────────────
    # Typing a printable character while a DataTable (or similar) has focus
    # silently redirects keystrokes to the search bar so the user never has
    # to think about focus management.

    def on_key(self, ev: Any) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            return   # Input already handles its own keys
        key = ev.key
        # Single printable character that isn't a reserved binding
        if len(key) == 1 and key.isprintable() and key not in ("q", " ", "n", "p", "r"):
            inp = self.query_one(SearchBar).get_input()
            inp.focus()
            inp.value += key
            inp.cursor_position = len(inp.value)
            ev.stop()

    # ── mpv poll loop ──────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        bar      = self.query_one(NowPlayingBar)
        loop     = asyncio.get_running_loop()
        was_idle = True

        while True:
            await asyncio.sleep(0.5)
            try:
                pos    = await loop.run_in_executor(None, self._mpv.position)
                dur    = await loop.run_in_executor(None, self._mpv.duration)
                paused = await loop.run_in_executor(None, self._mpv.is_paused)
                vol    = await loop.run_in_executor(None, self._mpv.volume)
                idle   = await loop.run_in_executor(None, self._mpv.is_idle)

                bar.pos    = pos
                bar.dur    = dur
                bar.paused = paused
                bar.volume = vol

                if idle and not was_idle and dur > 0 and not self._advancing:
                    self._advancing = True
                    self.call_later(self._auto_advance)

                was_idle = idle
            except Exception:
                pass

    def _auto_advance(self) -> None:
        self._advancing = False
        self.action_next_track()

    # ── Search ─────────────────────────────────────────────────────────────

    @on(SearchBar.Submitted)
    def _search_submitted(self, ev: SearchBar.Submitted) -> None:
        self.query_one(TabbedContent).active = "tab-results"
        self.query_one(ResultsPane).show_searching(ev.query)
        self._run_search(ev.query)

    @work(thread=True)
    def _run_search(self, query: str) -> None:
        try:
            tr = self._session.search(query, search_type="tracks",  limit=15)
            al = self._session.search(query, search_type="albums",  limit=6)
            ar = self._session.search(query, search_type="artists", limit=4)
        except Exception as exc:
            self.call_from_thread(
                self.query_one(ResultsPane).show_error, f"Search error: {exc}"
            )
            return

        rows:     list[tuple] = []
        new_rows: list[dict]  = []

        for t in tr.get("tracks", {}).get("items", []):
            new_rows.append({"type": "track", "data": t})
            rows.append((
                "🎵",
                t.get("title", ""),
                (t.get("performer") or {}).get("name", ""),
                (t.get("album") or {}).get("title", ""),
                str(t.get("id", "")),
            ))
        for a in al.get("albums", {}).get("items", []):
            new_rows.append({"type": "album", "data": a})
            rows.append((
                "💿",
                a.get("title", ""),
                (a.get("artist") or {}).get("name", ""),
                (a.get("release_date_original") or "")[:4],
                str(a.get("id", "")),
            ))
        for a in ar.get("artists", {}).get("items", []):
            new_rows.append({"type": "artist", "data": a})
            rows.append((
                "👤",
                a.get("name", ""),
                f"{a.get('albums_count', '')} albums",
                "",
                str(a.get("id", "")),
            ))

        self.call_from_thread(
            self.query_one(ResultsPane).show_results, rows, new_rows
        )

    # ── Playback ──────────────────────────────────────────────────────────

    def _set_bar(self, **kw: Any) -> None:
        bar = self.query_one(NowPlayingBar)
        for k, v in kw.items():
            setattr(bar, k, v)

    @work(thread=True)
    def _play_entry(self, entry: QueueEntry, prev: Optional[QueueEntry] = None) -> None:
        self.call_from_thread(
            self._set_bar,
            title=f"[dim]⟳ Resolving…[/dim]  {entry.title}",
            artist=entry.artist,
            album=entry.album,
            quality="",
            pos=0.0,
            dur=float(entry.duration),
            paused=False,
        )

        if prev and prev.track_id != entry.track_id and prev.stream:
            try:
                self._session.report_stream_end(prev.track_id)
            except Exception:
                pass

        try:
            stream_info = self._session.prepare_stream(
                entry.track_id, quality=Quality.HI_RES,
            )
        except NotStreamableError:
            def _ns() -> None:
                self.notify(f"Not streamable: {entry.title}", severity="warning")
                self._set_bar(title="Not streamable", paused=True)
            self.call_from_thread(_ns)
            return
        except Exception as exc:
            def _err() -> None:
                self.notify(f"Stream error: {exc}", severity="error")
                self._set_bar(title="Stream error", paused=True)
            self.call_from_thread(_err)
            return

        entry.stream = stream_info

        if stream_info.bit_depth and stream_info.sampling_rate:
            sr     = stream_info.sampling_rate
            sr_str = f"{int(sr)}kHz" if sr == int(sr) else f"{sr}kHz"
            quality_str = f"{stream_info.bit_depth}bit / {sr_str}"
        elif stream_info.format_id == 5:
            quality_str = "MP3 320"
        elif stream_info.format_id == 6:
            quality_str = "CD FLAC"
        else:
            quality_str = ""

        self._mpv.load(stream_info.url)

        def _ready() -> None:
            self._set_bar(title=entry.title, quality=quality_str, paused=False)
            self.query_one(QueuePane).refresh_table()

        self.call_from_thread(_ready)

    def _start_entry(self, entry: QueueEntry) -> None:
        prev = self._prev_entry
        self._prev_entry = entry
        self._play_entry(entry, prev=prev)

    # ── Message routing ────────────────────────────────────────────────────

    @on(ResultsPane.TrackChosen)
    def _rp_track(self, ev: ResultsPane.TrackChosen) -> None:
        self._queue.add(ev.entry)
        self._queue.play_index(len(self._queue) - 1)
        self.notify(f"▶  {ev.entry.title}", timeout=2)
        self._start_entry(ev.entry)
        self.query_one(QueuePane).refresh_table()

    @on(ResultsPane.AlbumChosen)
    def _rp_album(self, ev: ResultsPane.AlbumChosen) -> None:
        self.query_one(AlbumPane).load(ev.entity_id, ev.title)
        self.query_one(TabbedContent).active = "tab-album"

    @on(AlbumPane.TrackChosen)
    def _a_track(self, ev: AlbumPane.TrackChosen) -> None:
        self._queue.add(ev.entry)
        self._queue.play_index(len(self._queue) - 1)
        self.notify(f"▶  {ev.entry.title}", timeout=2)
        self._start_entry(ev.entry)
        self.query_one(QueuePane).refresh_table()

    @on(AlbumPane.AddAllChosen)
    def _a_all(self, ev: AlbumPane.AddAllChosen) -> None:
        start = len(self._queue)
        self._queue.extend(ev.entries)
        if self._queue.current_index < 0 or self._queue.current() is None:
            self._queue.play_index(start)
            self._start_entry(ev.entries[0])
        self.notify(f"Added {len(ev.entries)} tracks to queue", timeout=2)
        self.query_one(QueuePane).refresh_table()

    @on(QueuePane.JumpTo)
    def _q_jump(self, ev: QueuePane.JumpTo) -> None:
        entry = self._queue.play_index(ev.index)
        if entry:
            self._start_entry(entry)

    @on(RecsPane.AlbumChosen)
    def _r_album(self, ev: RecsPane.AlbumChosen) -> None:
        self.query_one(AlbumPane).load(ev.entity_id, ev.title)
        self.query_one(TabbedContent).active = "tab-album"

    # ── Actions ────────────────────────────────────────────────────────────

    def action_focus_search(self) -> None:
        self.query_one(SearchBar).focus_input()

    def action_toggle_pause(self) -> None:
        self._mpv.toggle_pause()

    def action_next_track(self) -> None:
        current = self._queue.current()
        if current and current.stream:
            try:
                self._session.report_stream_end(current.track_id)
            except Exception:
                pass
        entry = self._queue.advance()
        if entry:
            self._start_entry(entry)
        else:
            self.notify("End of queue — press [bold]r[/bold] for radio.", timeout=5)

    def action_prev_track(self) -> None:
        bar = self.query_one(NowPlayingBar)
        if bar.pos > 3.0:
            self._mpv.seek(-bar.pos)
        else:
            current = self._queue.current()
            if current and current.stream:
                try:
                    self._session.report_stream_cancel(current.track_id)
                except Exception:
                    pass
            entry = self._queue.back()
            if entry:
                self._start_entry(entry)

    def action_radio(self) -> None:
        current = self._queue.current()
        if not current:
            self.notify("Play a track first to seed radio.", severity="warning")
            return
        self.notify(f"Building radio from: {current.title}…", timeout=3)
        self._build_radio(current.track_id)

    @work(thread=True)
    def _build_radio(self, track_id: str) -> None:
        try:
            tracks = self._session.get_track_radio(track_id, limit=20)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Radio failed: {exc}", severity="error")
            return

        entries = []
        for t in tracks:
            try:
                entries.append(QueueEntry(
                    track_id=str(t.id),
                    title=getattr(t, "display_title", None) or t.title,
                    artist=t.performer.name if t.performer else "",
                    album=t.album.title if t.album else "",
                    duration=t.duration or 0,
                ))
            except Exception:
                continue

        def _add() -> None:
            self._queue.extend(entries)
            self.query_one(QueuePane).refresh_table()
            self.notify(f"Radio: added {len(entries)} tracks", timeout=3)

        self.call_from_thread(_add)

    def action_seek_fwd(self)  -> None: self._mpv.seek(10)
    def action_seek_back(self) -> None: self._mpv.seek(-10)

    def action_vol_up(self) -> None:
        bar = self.query_one(NowPlayingBar)
        v   = min(150, bar.volume + 5)
        self._mpv.set_volume(v)
        bar.volume = v

    def action_vol_down(self) -> None:
        bar = self.query_one(NowPlayingBar)
        v   = max(0, bar.volume - 5)
        self._mpv.set_volume(v)
        bar.volume = v

    def action_quit(self) -> None:
        self._mpv.quit()
        self.exit()


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════

def main(dev: bool = False) -> None:
    try:
        sess = QobuzSession.from_config(dev=dev)
    except FileNotFoundError:
        print(
            "\n[tui] Not logged in.\n"
            "  Run:  qobuz login -u EMAIL -p PASSWORD\n",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"\n[tui] Session error: {exc}\n", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    try:
        QobuzTUI(session=sess).run()
    except Exception as exc:
        print(f"\n[tui] Crash: {exc}\n", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Qobuz TUI player")
    ap.add_argument("--dev", action="store_true")
    args = ap.parse_args()
    main(dev=args.dev)

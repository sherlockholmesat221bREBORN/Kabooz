#!/usr/bin/env python3
"""
kabooz/tui.py — Terminal music player for Qobuz.

Requirements
────────────
    pip install textual>=0.61
    mpv must be on PATH  (brew install mpv  /  apt install mpv)

Usage
─────
    python -m kabooz.tui
    qobuz tui               # if registered in pyproject.toml
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── Dependency checks ──────────────────────────────────────────────────────
try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical
    from textual.message import Message
    from textual.reactive import reactive
    from textual.widget import Widget
    from textual.widgets import (
        DataTable, Footer, Header, Input, Label,
        LoadingIndicator, Static, TabbedContent, TabPane,
    )
    from textual import on, work
    from textual.worker import WorkerError
except ImportError:
    print(
        "Textual ≥ 0.61 is required.\n"
        "Install: pip install 'textual>=0.61'",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from kabooz.session import QobuzSession
    from kabooz.quality import Quality
    from kabooz.exceptions import QobuzError, NotStreamableError
except ImportError:
    try:
        import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
        from kabooz.session import QobuzSession
        from kabooz.quality import Quality
        from kabooz.exceptions import QobuzError, NotStreamableError
    except ImportError:
        print("kabooz not found. pip install -e .", file=sys.stderr)
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════
# Audio backend — mpv via JSON IPC
# ══════════════════════════════════════════════════════════════════════════

class MpvPlayer:
    """
    Controls an mpv subprocess via its JSON IPC socket.
    All methods are safe to call even when mpv is not running.
    """

    def __init__(self) -> None:
        self._sock_path = Path(tempfile.gettempdir()) / "qobuz-tui-mpv.sock"
        self._proc: Optional[subprocess.Popen] = None
        self.available: bool = bool(shutil.which("mpv"))

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start mpv in idle mode. Returns True when the socket is ready."""
        if not self.available:
            return False
        if self._proc and self._proc.poll() is None:
            return True
        self._sock_path.unlink(missing_ok=True)
        self._proc = subprocess.Popen(
            [
                "mpv", "--no-video", "--idle=yes", "--keep-open=yes",
                f"--input-ipc-server={self._sock_path}",
                "--no-terminal",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30):          # wait up to 3 s for socket
            time.sleep(0.1)
            if self._sock_path.exists():
                return True
        return False

    def quit(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._cmd("quit")
            try:
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()

    # ── IPC ────────────────────────────────────────────────────────────────

    def _cmd(self, *args: Any) -> Any:
        if not self._sock_path.exists():
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect(str(self._sock_path))
                payload = json.dumps({"command": list(args)}) + "\n"
                s.sendall(payload.encode())
                buf = b""
                while b"\n" not in buf:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
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

    # ── Playback control ───────────────────────────────────────────────────

    def load(self, url: str) -> None:
        self._cmd("loadfile", url, "replace")

    def toggle_pause(self) -> None:
        self._cmd("cycle", "pause")

    def set_pause(self, paused: bool) -> None:
        self._cmd("set_property", "pause", paused)

    def stop(self) -> None:
        self._cmd("stop")

    def seek(self, seconds: float) -> None:
        self._cmd("seek", seconds, "relative")

    def set_volume(self, vol: int) -> None:
        self._cmd("set_property", "volume", max(0, min(150, vol)))

    # ── State queries ──────────────────────────────────────────────────────

    def position(self) -> float:
        v = self._cmd("get_property", "time-pos")
        return float(v) if v is not None else 0.0

    def duration(self) -> float:
        v = self._cmd("get_property", "duration")
        return float(v) if v is not None else 0.0

    def is_paused(self) -> bool:
        return bool(self._cmd("get_property", "pause"))

    def is_idle(self) -> bool:
        return self._cmd("get_property", "idle-active") is True

    def volume(self) -> int:
        v = self._cmd("get_property", "volume")
        return int(v) if v is not None else 80


# ══════════════════════════════════════════════════════════════════════════
# Queue model
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class QueueEntry:
    track_id: str
    title: str
    artist: str
    album: str = ""
    duration: int = 0
    url: str = ""       # filled in just before playback


class TrackQueue:
    def __init__(self) -> None:
        self._items: list[QueueEntry] = []
        self._index: int = -1

    def add(self, entry: QueueEntry) -> None:
        self._items.append(entry)

    def clear(self) -> None:
        self._items.clear()
        self._index = -1

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

    @property
    def items(self) -> list[QueueEntry]:
        return list(self._items)

    @property
    def current_index(self) -> int:
        return self._index

    def __len__(self) -> int:
        return len(self._items)


# ══════════════════════════════════════════════════════════════════════════
# Widgets
# ══════════════════════════════════════════════════════════════════════════

class NowPlayingBar(Widget):
    """Bottom bar: track name, scrubber, volume, controls."""

    DEFAULT_CSS = """
    NowPlayingBar {
        height: 4;
        background: $surface;
        border-top: solid $primary-darken-2;
        padding: 0 2;
    }
    NowPlayingBar .np-title   { color: $text; text-style: bold; height: 1; }
    NowPlayingBar .np-sub     { color: $text-muted; height: 1; }
    NowPlayingBar .np-bar     { color: $primary; height: 1; }
    NowPlayingBar .np-controls{ color: $text-muted; height: 1; }
    """

    title   = reactive("No track loaded")
    artist  = reactive("")
    pos     = reactive(0.0)
    dur     = reactive(0.0)
    paused  = reactive(True)
    volume  = reactive(80)

    def compose(self) -> ComposeResult:
        yield Static("", classes="np-title",    id="np-title")
        yield Static("", classes="np-sub",      id="np-sub")
        yield Static("", classes="np-bar",      id="np-bar")
        yield Static("", classes="np-controls", id="np-controls")

    def _fmt_time(self, secs: float) -> str:
        s = int(secs)
        return f"{s // 60}:{s % 60:02d}"

    def watch_title(self, v: str)  -> None: self._refresh_all()
    def watch_artist(self, v: str) -> None: self._refresh_all()
    def watch_pos(self, v: float)  -> None: self._refresh_bar()
    def watch_dur(self, v: float)  -> None: self._refresh_bar()
    def watch_paused(self, v: bool)-> None: self._refresh_controls()
    def watch_volume(self, v: int) -> None: self._refresh_controls()

    def _refresh_all(self) -> None:
        self._refresh_bar()
        self._refresh_controls()
        t = self.query_one("#np-title", Static)
        t.update(self.title)
        s = self.query_one("#np-sub", Static)
        s.update(self.artist)

    def _refresh_bar(self) -> None:
        bar_w = 40
        if self.dur > 0:
            frac  = min(self.pos / self.dur, 1.0)
            filled = int(frac * bar_w)
            bar   = "█" * filled + "░" * (bar_w - filled)
            times = f"  {self._fmt_time(self.pos)} / {self._fmt_time(self.dur)}"
        else:
            bar   = "░" * bar_w
            times = "  --:-- / --:--"
        self.query_one("#np-bar", Static).update(bar + times)

    def _refresh_controls(self) -> None:
        icon = "⏸" if not self.paused else "▶"
        vol  = f"🔊 {self.volume}%"
        self.query_one("#np-controls", Static).update(
            f"{icon}  [space] play/pause   [←/→] seek   [↑/↓] volume   {vol}"
        )


class SearchPane(Widget):
    """Search input + results table."""

    DEFAULT_CSS = """
    SearchPane {
        width: 100%;
        height: 100%;
    }
    SearchPane Input {
        margin: 1 2;
        border: solid $primary-darken-1;
    }
    SearchPane DataTable {
        margin: 0 2;
        height: 1fr;
        border: solid $primary-darken-2;
    }
    """

    class TrackSelected(Message):
        def __init__(self, entry: QueueEntry) -> None:
            super().__init__()
            self.entry = entry

    class AlbumSelected(Message):
        def __init__(self, album_id: str, title: str) -> None:
            super().__init__()
            self.album_id = album_id
            self.title    = title

    def __init__(self, session: QobuzSession) -> None:
        super().__init__()
        self._session = session
        self._results: list[dict] = []
        self._mode = "tracks"

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Search tracks, albums, artists… (Enter to search)", id="search-input")
        yield DataTable(id="search-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#search-table", DataTable)
        table.add_columns("Type", "Title / Name", "Artist / Owner", "Album / Year", "ID")

    @on(Input.Submitted, "#search-input")
    def do_search(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            return
        self.run_search(event.value.strip())

    @work(thread=True)
    def run_search(self, query: str) -> None:
        table = self.query_one("#search-table", DataTable)
        self.app.call_from_thread(table.clear)
        self._results = []

        try:
            tracks = self._session.client.search(query=query, type="tracks", limit=10)
            albums = self._session.client.search(query=query, type="albums", limit=5)
        except QobuzError as exc:
            self.app.call_from_thread(
                self.app.notify, f"Search failed: {exc}", severity="error"
            )
            return

        rows: list[tuple] = []

        for t in tracks.get("tracks", {}).get("items", []):
            self._results.append({"type": "track", "data": t})
            rows.append((
                "🎵 track",
                t.get("title", ""),
                (t.get("performer") or {}).get("name", ""),
                (t.get("album") or {}).get("title", ""),
                str(t.get("id", "")),
            ))

        for a in albums.get("albums", {}).get("items", []):
            self._results.append({"type": "album", "data": a})
            rows.append((
                "💿 album",
                a.get("title", ""),
                (a.get("artist") or {}).get("name", ""),
                (a.get("release_date_original") or "")[:4],
                str(a.get("id", "")),
            ))

        def _fill():
            for row in rows:
                table.add_row(*row)
        self.app.call_from_thread(_fill)

    @on(DataTable.RowSelected, "#search-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        row_key = event.row_key.value
        if row_key is None:
            return
        # DataTable row keys default to the ordinal index
        try:
            idx = list(event.data_table._row_locations.keys()).index(event.row_key)
        except Exception:
            # Fallback: parse the key as an index
            try:
                idx = int(str(event.row_key.value))
            except Exception:
                return

        if idx >= len(self._results):
            return
        item = self._results[idx]

        if item["type"] == "track":
            t = item["data"]
            entry = QueueEntry(
                track_id=str(t.get("id", "")),
                title=t.get("title", ""),
                artist=(t.get("performer") or {}).get("name", ""),
                album=(t.get("album") or {}).get("title", ""),
                duration=t.get("duration", 0),
            )
            self.post_message(self.TrackSelected(entry))
        else:
            a = item["data"]
            self.post_message(self.AlbumSelected(
                album_id=str(a.get("id", "")),
                title=a.get("title", ""),
            ))


class QueuePane(Widget):
    """Shows the current playback queue."""

    DEFAULT_CSS = """
    QueuePane {
        width: 100%;
        height: 100%;
    }
    QueuePane DataTable {
        margin: 1 2;
        height: 1fr;
        border: solid $primary-darken-2;
    }
    QueuePane #queue-label {
        margin: 1 2 0 2;
        color: $text-muted;
    }
    """

    class PlayRequested(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def __init__(self) -> None:
        super().__init__()
        self._queue: TrackQueue | None = None

    def compose(self) -> ComposeResult:
        yield Label("Queue  (Enter to jump to track, D to remove)", id="queue-label")
        yield DataTable(id="queue-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        table.add_columns("", "Title", "Artist", "Duration")

    def attach_queue(self, queue: TrackQueue) -> None:
        self._queue = queue
        self.refresh_table()

    def refresh_table(self) -> None:
        if not self._queue:
            return
        table = self.query_one("#queue-table", DataTable)
        table.clear()
        for i, entry in enumerate(self._queue.items):
            marker = "▶" if i == self._queue.current_index else " "
            dur = f"{entry.duration // 60}:{entry.duration % 60:02d}" if entry.duration else "—"
            table.add_row(marker, entry.title, entry.artist, dur)

    @on(DataTable.RowSelected, "#queue-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            idx = int(str(event.row_key.value))
        except Exception:
            return
        self.post_message(self.PlayRequested(idx))

    def on_key(self, event) -> None:
        if event.key == "d" and self._queue:
            table = self.query_one("#queue-table", DataTable)
            if table.cursor_row is not None:
                # Remove from queue (simple list manipulation)
                try:
                    del self._queue._items[table.cursor_row]
                    self.refresh_table()
                except IndexError:
                    pass


class AlbumPane(Widget):
    """Shows tracks in a selected album."""

    DEFAULT_CSS = """
    AlbumPane {
        width: 100%;
        height: 100%;
    }
    AlbumPane #album-title {
        margin: 1 2 0 2;
        text-style: bold;
        color: $primary;
    }
    AlbumPane DataTable {
        margin: 1 2;
        height: 1fr;
        border: solid $primary-darken-2;
    }
    """

    class TrackSelected(Message):
        def __init__(self, entry: QueueEntry) -> None:
            super().__init__()
            self.entry = entry

    class AddAllRequested(Message):
        def __init__(self, entries: list[QueueEntry]) -> None:
            super().__init__()
            self.entries = entries

    def __init__(self, session: QobuzSession) -> None:
        super().__init__()
        self._session = session
        self._entries: list[QueueEntry] = []

    def compose(self) -> ComposeResult:
        yield Label("No album selected", id="album-title")
        yield DataTable(id="album-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#album-table", DataTable)
        table.add_columns("#", "Title", "Duration")

    @work(thread=True)
    def load_album(self, album_id: str, title: str) -> None:
        self.app.call_from_thread(
            self.query_one("#album-title", Label).update,
            f"Loading {title}…",
        )
        try:
            album = self._session.client.get_album(album_id)
        except QobuzError as exc:
            self.app.call_from_thread(
                self.app.notify, f"Album error: {exc}", severity="error"
            )
            return

        entries = []
        if album.tracks and album.tracks.items:
            for t in album.tracks.items:
                entries.append(QueueEntry(
                    track_id=str(t.id),
                    title=getattr(t, "display_title", t.title),
                    artist=t.performer.name if t.performer else (
                        album.artist.name if album.artist else ""
                    ),
                    album=album.display_title,
                    duration=t.duration or 0,
                ))

        def _fill():
            self._entries = entries
            table = self.query_one("#album-table", DataTable)
            table.clear()
            title_label = self.query_one("#album-title", Label)
            title_label.update(
                f"{album.display_title}  —  {album.artist.name if album.artist else ''}  "
                f"[dim]({album.tracks_count} tracks, "
                f"{(album.release_date_original or '')[:4]})[/dim]"
                f"  [grey50](A=add all  Enter=play)[/grey50]"
            )
            for e in entries:
                n   = entries.index(e) + 1
                dur = f"{e.duration // 60}:{e.duration % 60:02d}" if e.duration else "—"
                table.add_row(str(n), e.title, dur)

        self.app.call_from_thread(_fill)

    @on(DataTable.RowSelected, "#album-table")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            idx = int(str(event.row_key.value))
        except Exception:
            return
        if 0 <= idx < len(self._entries):
            self.post_message(self.TrackSelected(self._entries[idx]))

    def on_key(self, event) -> None:
        if event.key == "a" and self._entries:
            self.post_message(self.AddAllRequested(list(self._entries)))


# ══════════════════════════════════════════════════════════════════════════
# Main app
# ══════════════════════════════════════════════════════════════════════════

CSS = """
Screen {
    background: $background;
}

#main-tabs {
    height: 1fr;
}

.tab-content {
    height: 1fr;
}

NowPlayingBar {
    dock: bottom;
    height: 5;
}

Header {
    background: $primary-darken-3;
}
"""


class QobuzTUI(App):
    """Qobuz TUI — terminal music player."""

    TITLE = "Qobuz TUI"
    CSS   = CSS

    BINDINGS = [
        Binding("q",         "quit",          "Quit"),
        Binding("space",     "toggle_pause",  "Play/Pause"),
        Binding("n",         "next_track",    "Next"),
        Binding("p",         "prev_track",    "Prev"),
        Binding("right",     "seek_fwd",      "→ 10s",  show=False),
        Binding("left",      "seek_back",     "← 10s",  show=False),
        Binding("up",        "vol_up",        "Vol +",  show=False),
        Binding("down",      "vol_down",      "Vol -",  show=False),
        Binding("ctrl+a",    "add_to_queue",  "Add to queue"),
    ]

    def __init__(self, session: QobuzSession) -> None:
        super().__init__()
        self._session  = session
        self._mpv      = MpvPlayer()
        self._queue    = TrackQueue()
        self._poll_task: asyncio.Task | None = None

    # ── Compose ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="main-tabs"):
            with TabPane("Search", id="tab-search"):
                yield SearchPane(self._session)
            with TabPane("Album", id="tab-album"):
                yield AlbumPane(self._session)
            with TabPane("Queue", id="tab-queue"):
                yield QueuePane()
        yield NowPlayingBar()
        yield Footer()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        queue_pane = self.query_one(QueuePane)
        queue_pane.attach_queue(self._queue)

        if not self._mpv.available:
            self.notify(
                "mpv not found — audio playback disabled.\n"
                "Install: brew install mpv  /  apt install mpv",
                severity="warning",
                timeout=8,
            )
        else:
            self._mpv.start()
            self._poll_task = asyncio.create_task(self._poll_loop())

    def on_unmount(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        self._mpv.quit()

    # ── Poll mpv state ────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        bar = self.query_one(NowPlayingBar)
        while True:
            await asyncio.sleep(0.5)
            try:
                pos    = await asyncio.get_event_loop().run_in_executor(None, self._mpv.position)
                dur    = await asyncio.get_event_loop().run_in_executor(None, self._mpv.duration)
                paused = await asyncio.get_event_loop().run_in_executor(None, self._mpv.is_paused)
                vol    = await asyncio.get_event_loop().run_in_executor(None, self._mpv.volume)
                bar.pos    = pos
                bar.dur    = dur
                bar.paused = paused
                bar.volume = vol

                # Auto-advance when track ends
                if dur > 0 and pos >= dur - 1.5:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._mpv.is_idle
                    )
                    if self._mpv.is_idle():
                        self.action_next_track()
            except Exception:
                pass

    # ── Track playback ─────────────────────────────────────────────────────

    @work(thread=True)
    def _play_entry(self, entry: QueueEntry) -> None:
        """Resolve the CDN URL then load it in mpv."""
        bar = self.query_one(NowPlayingBar)
        self.app.call_from_thread(bar.__setattr__, "title",  f"⟳ Resolving…  {entry.title}")
        self.app.call_from_thread(bar.__setattr__, "artist", entry.artist)
        self.app.call_from_thread(bar.__setattr__, "pos",    0.0)
        self.app.call_from_thread(bar.__setattr__, "dur",    0.0)

        try:
            url_info = self._session.client.get_track_url(
                entry.track_id, quality=Quality.FLAC_16
            )
        except NotStreamableError:
            self.app.call_from_thread(
                self.notify, f"Not streamable: {entry.title}", severity="warning"
            )
            return
        except QobuzError as exc:
            self.app.call_from_thread(
                self.notify, f"URL error: {exc}", severity="error"
            )
            return

        entry.url = url_info.get("url", "")
        if not entry.url:
            self.app.call_from_thread(self.notify, "No URL returned", severity="error")
            return

        self._mpv.load(entry.url)
        self.app.call_from_thread(bar.__setattr__, "title",  entry.title)
        self.app.call_from_thread(bar.__setattr__, "artist", entry.artist)
        self.app.call_from_thread(bar.__setattr__, "paused", False)

        # Refresh queue pane to update the ▶ marker
        queue_pane = self.query_one(QueuePane)
        self.app.call_from_thread(queue_pane.refresh_table)

    # ── Message handlers ────────────────────────────────────────────────────

    @on(SearchPane.TrackSelected)
    def on_track_selected(self, event: SearchPane.TrackSelected) -> None:
        self._queue.add(event.entry)
        entry = self._queue.play_index(len(self._queue) - 1)
        self.notify(f"Playing: {event.entry.title}")
        self._play_entry(event.entry)
        self.query_one(QueuePane).refresh_table()

    @on(SearchPane.AlbumSelected)
    def on_album_selected(self, event: SearchPane.AlbumSelected) -> None:
        album_pane = self.query_one(AlbumPane)
        album_pane.load_album(event.album_id, event.title)
        # Switch to Album tab
        tabs = self.query_one(TabbedContent)
        tabs.active = "tab-album"

    @on(AlbumPane.TrackSelected)
    def on_album_track_selected(self, event: AlbumPane.TrackSelected) -> None:
        self._queue.add(event.entry)
        self._queue.play_index(len(self._queue) - 1)
        self.notify(f"Playing: {event.entry.title}")
        self._play_entry(event.entry)
        self.query_one(QueuePane).refresh_table()

    @on(AlbumPane.AddAllRequested)
    def on_add_all(self, event: AlbumPane.AddAllRequested) -> None:
        for e in event.entries:
            self._queue.add(e)
        self.notify(f"Added {len(event.entries)} tracks to queue")
        self.query_one(QueuePane).refresh_table()

    @on(QueuePane.PlayRequested)
    def on_queue_play(self, event: QueuePane.PlayRequested) -> None:
        entry = self._queue.play_index(event.index)
        if entry:
            self._play_entry(entry)

    # ── Key actions ────────────────────────────────────────────────────────

    def action_toggle_pause(self) -> None:
        self._mpv.toggle_pause()

    def action_next_track(self) -> None:
        entry = self._queue.advance()
        if entry:
            self._play_entry(entry)
        else:
            self.notify("End of queue", severity="information")

    def action_prev_track(self) -> None:
        bar = self.query_one(NowPlayingBar)
        if bar.pos > 3.0:
            self._mpv.seek(-bar.pos)   # restart current
        else:
            entry = self._queue.back()
            if entry:
                self._play_entry(entry)

    def action_seek_fwd(self) -> None:
        self._mpv.seek(10)

    def action_seek_back(self) -> None:
        self._mpv.seek(-10)

    def action_vol_up(self) -> None:
        bar = self.query_one(NowPlayingBar)
        new_vol = min(150, bar.volume + 5)
        self._mpv.set_volume(new_vol)
        bar.volume = new_vol

    def action_vol_down(self) -> None:
        bar = self.query_one(NowPlayingBar)
        new_vol = max(0, bar.volume - 5)
        self._mpv.set_volume(new_vol)
        bar.volume = new_vol

    def action_add_to_queue(self) -> None:
        self.notify("Select a track from Search or Album to add", timeout=2)

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
            "Not logged in. Run: qobuz login -u EMAIL -p PASSWORD",
            file=sys.stderr,
        )
        sys.exit(1)
    except QobuzError as exc:
        print(f"Session error: {exc}", file=sys.stderr)
        sys.exit(1)

    app = QobuzTUI(session=sess)
    app.run()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Qobuz TUI player")
    p.add_argument("--dev", action="store_true", help="Enable dev mode (cached responses)")
    args = p.parse_args()
    main(dev=args.dev)

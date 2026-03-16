#!/usr/bin/env python3
# kabooz/tui_curses.py
"""
Qobuz TUI — curses edition.

Zero extra dependencies (curses is stdlib).  Instant startup, instant
keypresses, no layout engine.

Requirements
────────────
    pkg install mpv      (Termux)
    apt install mpv      (Debian/Ubuntu)
    brew install mpv     (macOS)

Run:
    python -m kabooz.tui_curses
    python -m kabooz.tui_curses --dev

Keys
────
    Type            → search (always lands in the search bar)
    Enter           → run search / play track / browse album
    ↑ ↓             → navigate list
    Tab             → cycle views: Results → Album → Queue → For You
    Esc             → clear search / back to top of list
    Space           → play / pause
    n               → next track
    p               → previous track
    r               → build radio queue from current track
    [ ]             → volume down / up
    ← →             → seek −10s / +10s
    a               → add all (album view)
    d               → delete selected (queue view)
    q / Ctrl-C      → quit
"""
from __future__ import annotations

import curses
import json
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════
# mpv IPC
# ══════════════════════════════════════════════════════════════════════════

class MpvPlayer:
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
            ["mpv", "--no-video", "--idle=yes", "--keep-open=yes",
             f"--input-ipc-server={self._sock}",
             "--no-terminal", "--really-quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
                s.settimeout(1.5)
                s.connect(str(self._sock))
                s.sendall((json.dumps({"command": list(args)}) + "\n").encode())
                buf = b""
                deadline = time.monotonic() + 1.5
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

    def load(self, url: str) -> None:     self._ipc("loadfile", url, "replace")
    def toggle_pause(self) -> None:       self._ipc("cycle", "pause")
    def seek(self, s: float) -> None:     self._ipc("seek", s, "relative")
    def set_volume(self, v: int) -> None: self._ipc("set_property", "volume", max(0, min(150, v)))
    def position(self) -> float:          return float(self._ipc("get_property", "time-pos") or 0)
    def duration(self) -> float:          return float(self._ipc("get_property", "duration") or 0)
    def is_paused(self) -> bool:          return bool(self._ipc("get_property", "pause"))
    def is_idle(self) -> bool:            return self._ipc("get_property", "idle-active") is True
    def volume(self) -> int:              return int(self._ipc("get_property", "volume") or 80)


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

    def add(self, e: QueueEntry) -> None:
        self._items.append(e)

    def extend(self, es: list[QueueEntry]) -> None:
        self._items.extend(es)

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
    def index(self) -> int:              return self._index
    def __len__(self) -> int:            return len(self._items)


# ══════════════════════════════════════════════════════════════════════════
# Result rows  (unified type for all list views)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Row:
    icon:     str
    col1:     str   # title / name
    col2:     str   # artist / owner
    col3:     str   # album / year / count
    kind:     str   # "track" | "album" | "artist" | "queue" | "recs_album" | "recs_artist"
    data:     Any   # original dict or QueueEntry


# ══════════════════════════════════════════════════════════════════════════
# Curses UI
# ══════════════════════════════════════════════════════════════════════════

VIEW_RESULTS = 0
VIEW_ALBUM   = 1
VIEW_QUEUE   = 2
VIEW_RECS    = 3
VIEW_NAMES   = ["Results", "Album", "Queue", "For You"]

# colour pair indices
C_NORMAL  = 0
C_HEADER  = 1
C_SEL     = 2
C_DIM     = 3
C_STATUS  = 4
C_PLAYING = 5
C_TITLE   = 6
C_ERR     = 7


class UI:
    def __init__(self, stdscr: "curses.window", session: QobuzSession) -> None:
        self.scr      = stdscr
        self.session  = session
        self.mpv      = MpvPlayer()
        self.queue    = TrackQueue()

        # View state
        self.view:      int  = VIEW_RESULTS
        self.rows:      list[Row] = []
        self.cursor:    int  = 0
        self.scroll:    int  = 0          # first visible row index

        # Album drill-down
        self._album_entries: list[QueueEntry] = []

        # Search bar
        self.search_buf:  str  = ""
        self.search_focus: bool = True    # True = typing goes to search bar

        # Status / notification
        self.status:    str  = ""
        self.status_ts: float = 0.0

        # Now-playing state (polled from mpv thread)
        self.np_title:   str   = ""
        self.np_artist:  str   = ""
        self.np_album:   str   = ""
        self.np_quality: str   = ""
        self.np_pos:     float = 0.0
        self.np_dur:     float = 0.0
        self.np_paused:  bool  = True
        self.np_vol:     int   = 80

        # Prev entry for stream reporting
        self._prev_entry: Optional[QueueEntry] = None
        self._advancing   = False

        # Thread-safe result delivery
        self._ui_queue: queue.Queue = queue.Queue()

        # Redraw flag
        self._dirty = True

    # ── Colour setup ──────────────────────────────────────────────────

    def _init_colors(self) -> None:
        curses.start_color()
        curses.use_default_colors()
        bg = -1
        curses.init_pair(C_HEADER,  curses.COLOR_CYAN,    bg)
        curses.init_pair(C_SEL,     curses.COLOR_BLACK,   curses.COLOR_CYAN)
        curses.init_pair(C_DIM,     curses.COLOR_WHITE,   bg)   # will render dim
        curses.init_pair(C_STATUS,  curses.COLOR_YELLOW,  bg)
        curses.init_pair(C_PLAYING, curses.COLOR_GREEN,   bg)
        curses.init_pair(C_TITLE,   curses.COLOR_WHITE,   bg)
        curses.init_pair(C_ERR,     curses.COLOR_RED,     bg)

    # ── Helpers ───────────────────────────────────────────────────────

    def _h(self) -> int: return self.scr.getmaxyx()[0]
    def _w(self) -> int: return self.scr.getmaxyx()[1]

    def _clip(self, s: str, width: int) -> str:
        """Truncate string to fit in `width` columns, replacing overflow with …"""
        if len(s) <= width:
            return s
        return s[: max(0, width - 1)] + "…"

    def _addstr(self, y: int, x: int, s: str, attr: int = 0) -> None:
        """Safe addstr — never raises on clipping."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        available = w - x
        if available <= 0:
            return
        try:
            self.scr.addstr(y, x, self._clip(s, available), attr)
        except curses.error:
            pass

    def _hline(self, y: int, ch: str = "─") -> None:
        try:
            self.scr.hline(y, 0, ord(ch), self._w())
        except curses.error:
            pass

    def _notify(self, msg: str) -> None:
        self.status    = msg
        self.status_ts = time.monotonic()
        self._dirty    = True

    # ── Drawing ───────────────────────────────────────────────────────

    def _draw(self) -> None:
        h, w = self.scr.getmaxyx()
        self.scr.erase()

        # Row 0 — search bar
        prefix = " 🔍 "
        self._addstr(0, 0, prefix, curses.color_pair(C_HEADER) | curses.A_BOLD)
        buf_display = self.search_buf
        cursor_x = len(prefix) + len(buf_display)
        self._addstr(0, len(prefix), buf_display, curses.A_BOLD)
        if self.search_focus:
            # Draw a blinking block cursor
            if cursor_x < w:
                self._addstr(0, cursor_x, " ", curses.A_REVERSE)
            hint = "Enter=search  Tab=views  Esc=clear"
        else:
            hint = "/ or any key = search  Esc=back"
        hint_x = w - len(hint) - 1
        if hint_x > cursor_x + 2:
            self._addstr(0, hint_x, hint, curses.color_pair(C_DIM) | curses.A_DIM)

        # Row 1 — tab bar
        x = 1
        for i, name in enumerate(VIEW_NAMES):
            label = f" {name} "
            if i == self.view:
                self._addstr(1, x, label, curses.color_pair(C_SEL) | curses.A_BOLD)
            else:
                self._addstr(1, x, label, curses.color_pair(C_DIM))
            x += len(label) + 1
        tab_hint = "Tab=switch"
        self._addstr(1, w - len(tab_hint) - 1, tab_hint, curses.color_pair(C_DIM) | curses.A_DIM)

        # Row 2 — separator
        self._hline(2)

        # Rows 3 .. h-8 — list content
        NP_ROWS   = 5   # now-playing area at bottom
        list_top  = 3
        list_bot  = h - NP_ROWS - 1   # exclusive
        list_h    = list_bot - list_top

        if list_h > 0:
            self._draw_list(list_top, list_h, w)

        # Row h-NP_ROWS-1 — separator
        self._hline(h - NP_ROWS - 1)

        # Rows h-NP_ROWS .. h-1 — now-playing
        self._draw_nowplaying(h - NP_ROWS, w)

        # Status notification (drawn over last NP row)
        if self.status and (time.monotonic() - self.status_ts) < 4.0:
            self._addstr(h - 1, 0, f" {self.status} ",
                         curses.color_pair(C_STATUS) | curses.A_BOLD)

        # Move physical cursor to search bar when focused
        if self.search_focus and cursor_x < w:
            try:
                self.scr.move(0, cursor_x)
            except curses.error:
                pass

        self.scr.refresh()
        self._dirty = False

    def _draw_list(self, top: int, height: int, width: int) -> None:
        rows = self._current_rows()
        if not rows:
            msg = {
                VIEW_RESULTS: "No results yet — type a search above and press Enter",
                VIEW_ALBUM:   "Browse an album or artist from the Results tab",
                VIEW_QUEUE:   "Queue is empty — search for tracks and press Enter to play",
                VIEW_RECS:    "Loading recommendations…",
            }.get(self.view, "")
            self._addstr(top + height // 2, 2, msg, curses.color_pair(C_DIM) | curses.A_DIM)
            return

        # Clamp scroll so cursor is visible
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + height:
            self.scroll = self.cursor - height + 1
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - height)))

        # Column widths
        w1 = max(20, width * 45 // 100)
        w2 = max(12, width * 28 // 100)
        w3 = width - w1 - w2 - 6   # icon(2) + gaps

        for i, row in enumerate(rows[self.scroll: self.scroll + height]):
            abs_i = i + self.scroll
            y     = top + i
            is_sel = abs_i == self.cursor

            # Mark playing track in queue view
            playing_marker = ""
            if self.view == VIEW_QUEUE and abs_i == self.queue.index:
                playing_marker = "▶ "

            col1 = self._clip(playing_marker + row.col1, w1)
            col2 = self._clip(row.col2, w2)
            col3 = self._clip(row.col3, max(0, w3))

            line = f" {row.icon} {col1:<{w1}} {col2:<{w2}} {col3}"

            if is_sel:
                # Pad to full width so highlight covers the row
                line = line.ljust(width)
                self._addstr(y, 0, line, curses.color_pair(C_SEL) | curses.A_BOLD)
            else:
                self._addstr(y, 0, line, curses.color_pair(C_NORMAL))

        # Scrollbar
        if len(rows) > height:
            bar_h = max(1, height * height // len(rows))
            bar_y = top + (self.scroll * (height - bar_h)) // max(1, len(rows) - height)
            for dy in range(height):
                ch = "█" if bar_y <= dy < bar_y + bar_h else "░"
                self._addstr(top + dy, width - 1, ch, curses.color_pair(C_DIM))

    def _draw_nowplaying(self, top: int, width: int) -> None:
        # Line 0 — title + quality
        title = self.np_title or "No track loaded"
        quality = f"  [{self.np_quality}]" if self.np_quality else ""
        self._addstr(top, 0, f" {self._clip(title + quality, width - 2)}",
                     curses.color_pair(C_TITLE) | curses.A_BOLD)

        # Line 1 — artist — album
        sub = self.np_artist
        if self.np_album:
            sub += f"  —  {self.np_album}"
        self._addstr(top + 1, 0, f" {self._clip(sub, width - 2)}",
                     curses.color_pair(C_DIM) | curses.A_DIM)

        # Line 2 — progress bar
        bar_w = min(40, width - 20)
        if self.np_dur > 0:
            frac   = min(self.np_pos / self.np_dur, 1.0)
            filled = int(frac * bar_w)
            bar    = "█" * filled + "░" * (bar_w - filled)
            pos_s  = f"{int(self.np_pos) // 60}:{int(self.np_pos) % 60:02d}"
            dur_s  = f"{int(self.np_dur) // 60}:{int(self.np_dur) % 60:02d}"
            prog   = f" {bar}  {pos_s} / {dur_s}"
        else:
            prog = f" {'░' * bar_w}  --:-- / --:--"
        self._addstr(top + 2, 0, prog, curses.color_pair(C_HEADER))

        # Line 3 — controls
        play_icon = "⏸ PAUSED" if self.np_paused else "▶ PLAYING"
        controls  = (
            f" {play_icon}  "
            f"space=pause  n=next  p=prev  r=radio  "
            f"[/]=vol {self.np_vol}%  ←/→=±10s  q=quit"
        )
        attr = curses.color_pair(C_PLAYING) if not self.np_paused else curses.color_pair(C_STATUS)
        self._addstr(top + 3, 0, self._clip(controls, width - 1), attr)

    # ── List data helpers ─────────────────────────────────────────────

    def _current_rows(self) -> list[Row]:
        if self.view == VIEW_RESULTS:
            return self.rows
        if self.view == VIEW_ALBUM:
            out = []
            for e in self._album_entries:
                if e.track_id.startswith("album:"):
                    dur = ""
                else:
                    dur = f"{e.duration // 60}:{e.duration % 60:02d}" if e.duration else ""
                icon = "💿" if e.track_id.startswith("album:") else "🎵"
                out.append(Row(icon, e.title, e.artist, dur, "album_track", e))
            return out
        if self.view == VIEW_QUEUE:
            out = []
            for e in self.queue.items:
                dur = f"{e.duration // 60}:{e.duration % 60:02d}" if e.duration else ""
                out.append(Row("🎵", e.title, e.artist, dur, "queue", e))
            return out
        # VIEW_RECS
        return self.rows

    # ── Playback ──────────────────────────────────────────────────────

    def _start_entry(self, entry: QueueEntry) -> None:
        prev = self._prev_entry
        self._prev_entry = entry
        threading.Thread(target=self._play_worker, args=(entry, prev), daemon=True).start()

    def _play_worker(self, entry: QueueEntry, prev: Optional[QueueEntry]) -> None:
        self._ui_queue.put(("status", f"⟳ Resolving: {entry.title}"))
        self._ui_queue.put(("np_title",  f"⟳ {entry.title}"))
        self._ui_queue.put(("np_artist", entry.artist))
        self._ui_queue.put(("np_album",  entry.album))
        self._ui_queue.put(("np_quality", ""))

        if prev and prev.track_id != entry.track_id and prev.stream:
            try:
                self.session.report_stream_end(prev.track_id)
            except Exception:
                pass

        try:
            stream = self.session.prepare_stream(entry.track_id, quality=Quality.HI_RES)
        except NotStreamableError:
            self._ui_queue.put(("status", f"✗ Not streamable: {entry.title}"))
            self._ui_queue.put(("np_title", "Not streamable"))
            return
        except Exception as exc:
            self._ui_queue.put(("status", f"✗ Stream error: {exc}"))
            self._ui_queue.put(("np_title", "Stream error"))
            return

        entry.stream = stream

        if stream.bit_depth and stream.sampling_rate:
            sr  = stream.sampling_rate
            sr_s = f"{int(sr)}kHz" if sr == int(sr) else f"{sr}kHz"
            q_str = f"{stream.bit_depth}bit/{sr_s}"
        elif stream.format_id == 5:
            q_str = "MP3 320"
        elif stream.format_id == 6:
            q_str = "CD"
        else:
            q_str = ""

        self.mpv.load(stream.url)
        self._ui_queue.put(("np_title",   entry.title))
        self._ui_queue.put(("np_quality", q_str))
        self._ui_queue.put(("status",     f"▶ {entry.title}"))
        self._ui_queue.put(("refresh_queue", None))

    # ── Background threads ────────────────────────────────────────────

    def _mpv_poll_thread(self) -> None:
        while True:
            time.sleep(0.5)
            try:
                pos    = self.mpv.position()
                dur    = self.mpv.duration()
                paused = self.mpv.is_paused()
                vol    = self.mpv.volume()
                idle   = self.mpv.is_idle()
                self._ui_queue.put(("mpv_state", (pos, dur, paused, vol, idle)))
            except Exception:
                pass

    def _search_thread(self, query: str) -> None:
        self._ui_queue.put(("status", f"Searching: {query}…"))
        try:
            tr = self.session.search(query, search_type="tracks",  limit=15)
            al = self.session.search(query, search_type="albums",  limit=6)
            ar = self.session.search(query, search_type="artists", limit=4)
        except Exception as exc:
            self._ui_queue.put(("status", f"✗ Search error: {exc}"))
            return

        rows: list[Row] = []
        for t in tr.get("tracks", {}).get("items", []):
            rows.append(Row(
                "🎵", t.get("title", ""),
                (t.get("performer") or {}).get("name", ""),
                (t.get("album") or {}).get("title", ""),
                "track", t,
            ))
        for a in al.get("albums", {}).get("items", []):
            rows.append(Row(
                "💿", a.get("title", ""),
                (a.get("artist") or {}).get("name", ""),
                (a.get("release_date_original") or "")[:4],
                "album", a,
            ))
        for a in ar.get("artists", {}).get("items", []):
            rows.append(Row(
                "👤", a.get("name", ""),
                f"{a.get('albums_count', '')} albums", "",
                "artist", a,
            ))
        self._ui_queue.put(("search_results", rows))

    def _album_thread(self, entity_id: str, title: str) -> None:
        self._ui_queue.put(("status", f"Loading: {title}…"))
        entries: list[QueueEntry] = []
        header = title
        try:
            if entity_id.startswith("artist:"):
                aid    = entity_id.split(":", 1)[1]
                artist = self.session.get_artist(aid, extras="albums", limit=20)
                header = f"{artist.name} — {len(artist.albums.items) if artist.albums else 0} albums"
                for alb in (artist.albums.items if artist.albums else [])[:20]:
                    alb_id    = str(alb.id)
                    alb_title = getattr(alb, "display_title", None) or getattr(alb, "title", alb_id)
                    entries.append(QueueEntry(
                        track_id=f"album:{alb_id}",
                        title=alb_title,
                        artist=artist.name,
                    ))
            else:
                album       = self.session.get_album(entity_id)
                artist_name = album.artist.name if album.artist else ""
                year        = (album.release_date_original or "")[:4]
                header = (
                    f"{getattr(album, 'display_title', album.title)}"
                    f"  —  {artist_name}"
                    + (f"  ({year})" if year else "")
                )
                for t in (album.tracks.items if album.tracks else []):
                    entries.append(QueueEntry(
                        track_id=str(t.id),
                        title=getattr(t, "display_title", None) or t.title,
                        artist=t.performer.name if t.performer else artist_name,
                        album=getattr(album, "display_title", album.title),
                        duration=t.duration or 0,
                    ))
        except Exception as exc:
            self._ui_queue.put(("status", f"✗ Load error: {exc}"))
            return
        self._ui_queue.put(("album_loaded", (entries, header)))

    def _recs_thread(self) -> None:
        self._ui_queue.put(("status", "Loading recommendations…"))
        try:
            recs = self.session.get_recommendations(limit=10)
        except Exception as exc:
            self._ui_queue.put(("status", f"✗ Recommendations failed: {exc}"))
            return
        rows: list[Row] = []
        for item in recs.get("press_awards", [])[:5]:
            rows.append(Row("🏆", item.get("title",""),
                            (item.get("artist") or {}).get("name",""),
                            (item.get("release_date_original") or "")[:4],
                            "recs_album", item))
        for item in recs.get("new_releases", [])[:5]:
            rows.append(Row("🆕", item.get("title",""),
                            (item.get("artist") or {}).get("name",""),
                            (item.get("release_date_original") or "")[:4],
                            "recs_album", item))
        for item in recs.get("similar_artists", [])[:4]:
            aid  = str(getattr(item, "id", "") or "")
            name = str(getattr(item, "name","") or "")
            rows.append(Row("🎤", name, "", "", "recs_artist",
                            {"id": aid, "name": name}))
        for item in recs.get("featured", [])[:4]:
            rows.append(Row("📋", item.get("name",""),
                            (item.get("owner") or {}).get("name",""), "",
                            "recs_album", item))
        self._ui_queue.put(("recs_loaded", rows))

    def _radio_thread(self, track_id: str) -> None:
        self._ui_queue.put(("status", "Building radio…"))
        try:
            tracks = self.session.get_track_radio(track_id, limit=20)
        except Exception as exc:
            self._ui_queue.put(("status", f"✗ Radio failed: {exc}"))
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
        self._ui_queue.put(("radio_loaded", entries))

    # ── UI-queue drain (called each main-loop tick) ───────────────────

    _was_idle = True   # for auto-advance detection

    def _drain_queue(self) -> None:
        try:
            while True:
                msg, data = self._ui_queue.get_nowait()
                self._dirty = True
                if msg == "status":
                    self._notify(data)
                elif msg == "np_title":
                    self.np_title  = data
                elif msg == "np_artist":
                    self.np_artist = data
                elif msg == "np_album":
                    self.np_album  = data
                elif msg == "np_quality":
                    self.np_quality = data
                elif msg == "mpv_state":
                    pos, dur, paused, vol, idle = data
                    self.np_pos    = pos
                    self.np_dur    = dur
                    self.np_paused = paused
                    self.np_vol    = vol
                    if idle and not self._was_idle and dur > 0 and not self._advancing:
                        self._advancing = True
                        self._do_next()
                    self._was_idle = idle
                elif msg == "search_results":
                    self.rows   = data
                    self.cursor = 0
                    self.scroll = 0
                    self.view   = VIEW_RESULTS
                    self._notify(f"{len(data)} result{'s' if len(data)!=1 else ''}")
                    self.search_focus = False
                elif msg == "album_loaded":
                    entries, header = data
                    self._album_entries = entries
                    self.cursor = 0
                    self.scroll = 0
                    self.view   = VIEW_ALBUM
                    self._notify(header)
                elif msg == "recs_loaded":
                    self.rows   = data
                    self.cursor = 0
                    self.scroll = 0
                    self._notify(f"{len(data)} recommendations")
                elif msg == "radio_loaded":
                    self.queue.extend(data)
                    self._notify(f"Radio: added {len(data)} tracks")
                elif msg == "refresh_queue":
                    pass   # just dirty flag is enough
        except queue.Empty:
            pass

    # ── Actions ───────────────────────────────────────────────────────

    def _do_enter(self) -> None:
        """Enter key — context-dependent action."""
        if self.search_focus:
            # Run search
            q = self.search_buf.strip()
            if q:
                threading.Thread(target=self._search_thread, args=(q,), daemon=True).start()
            return

        rows = self._current_rows()
        if not rows or self.cursor >= len(rows):
            return
        row = rows[self.cursor]

        if row.kind == "track":
            t = row.data
            entry = QueueEntry(
                track_id=str(t.get("id","")),
                title=t.get("title",""),
                artist=(t.get("performer") or {}).get("name",""),
                album=(t.get("album") or {}).get("title",""),
                duration=t.get("duration", 0),
            )
            self.queue.add(entry)
            self.queue.play_index(len(self.queue) - 1)
            self._start_entry(entry)

        elif row.kind == "album":
            a = row.data
            threading.Thread(
                target=self._album_thread, args=(str(a.get("id","")), a.get("title","")),
                daemon=True,
            ).start()

        elif row.kind == "artist":
            a = row.data
            threading.Thread(
                target=self._album_thread,
                args=(f"artist:{a.get('id','')}", a.get("name","")),
                daemon=True,
            ).start()

        elif row.kind == "album_track":
            e = row.data
            if e.track_id.startswith("album:"):
                threading.Thread(
                    target=self._album_thread, args=(e.track_id, e.title), daemon=True
                ).start()
            else:
                self.queue.add(e)
                self.queue.play_index(len(self.queue) - 1)
                self._start_entry(e)

        elif row.kind == "queue":
            idx = self.queue.items.index(row.data) if row.data in self.queue.items else -1
            if idx >= 0:
                entry = self.queue.play_index(idx)
                if entry:
                    self._start_entry(entry)

        elif row.kind in ("recs_album", "recs_artist"):
            d  = row.data
            eid = (f"artist:{d.get('id','')}" if row.kind == "recs_artist"
                   else str(d.get("id","")))
            name = d.get("title") or d.get("name","")
            threading.Thread(
                target=self._album_thread, args=(eid, name), daemon=True
            ).start()

    def _do_next(self) -> None:
        self._advancing = False
        cur = self.queue.current()
        if cur and cur.stream:
            try:
                self.session.report_stream_end(cur.track_id)
            except Exception:
                pass
        entry = self.queue.advance()
        if entry:
            self._start_entry(entry)
        else:
            self._notify("End of queue — press r for radio")
        self._dirty = True

    def _do_prev(self) -> None:
        if self.np_pos > 3.0:
            self.mpv.seek(-self.np_pos)
        else:
            cur = self.queue.current()
            if cur and cur.stream:
                try:
                    self.session.report_stream_cancel(cur.track_id)
                except Exception:
                    pass
            entry = self.queue.back()
            if entry:
                self._start_entry(entry)

    def _do_add_all(self) -> None:
        real = [e for e in self._album_entries if not e.track_id.startswith("album:")]
        if not real:
            return
        start = len(self.queue)
        self.queue.extend(real)
        if self.queue.current() is None:
            self.queue.play_index(start)
            self._start_entry(real[0])
        self._notify(f"Added {len(real)} tracks to queue")
        self._dirty = True

    def _do_delete(self) -> None:
        rows = self._current_rows()
        if self.view != VIEW_QUEUE or not rows or self.cursor >= len(rows):
            return
        self.queue.remove_index(self.cursor)
        if self.cursor >= len(self.queue.items):
            self.cursor = max(0, len(self.queue.items) - 1)
        self._dirty = True

    def _do_radio(self) -> None:
        cur = self.queue.current()
        if not cur:
            self._notify("Play a track first to seed radio")
            return
        threading.Thread(target=self._radio_thread, args=(cur.track_id,), daemon=True).start()

    # ── Input handling ────────────────────────────────────────────────

    def _handle_key(self, key: int) -> bool:
        """Return False to quit."""
        self._dirty = True
        rows = self._current_rows()
        n    = len(rows)

        # ── navigation (always active) ──
        if key == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
            self.search_focus = False
            return True
        if key == curses.KEY_DOWN:
            self.cursor = min(max(0, n - 1), self.cursor + 1)
            self.search_focus = False
            return True
        if key == curses.KEY_PPAGE:   # page up
            self.cursor = max(0, self.cursor - 10)
            self.search_focus = False
            return True
        if key == curses.KEY_NPAGE:   # page down
            self.cursor = min(max(0, n - 1), self.cursor + 10)
            self.search_focus = False
            return True

        # ── seek (left/right) ──
        if key == curses.KEY_LEFT:
            self.mpv.seek(-10); return True
        if key == curses.KEY_RIGHT:
            self.mpv.seek(10);  return True

        # ── Tab — cycle views ──
        if key == ord("\t"):
            self.view   = (self.view + 1) % 4
            self.cursor = 0
            self.scroll = 0
            self.search_focus = False
            if self.view == VIEW_RECS and not self.rows and self.view == VIEW_RECS:
                threading.Thread(target=self._recs_thread, daemon=True).start()
            return True

        # ── Escape ──
        if key == 27:
            if not self.search_focus:
                self.search_focus = True
            else:
                self.search_buf = ""
            return True

        # ── Enter ──
        if key in (curses.KEY_ENTER, 10, 13):
            self._do_enter()
            return True

        # ── Backspace ──
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.search_buf:
                self.search_buf = self.search_buf[:-1]
            self.search_focus = True
            return True

        # ── Playback controls (only when search bar is NOT being typed into) ──
        # These are single-char bindings that would conflict with search typing,
        # so they only fire when focus is NOT on the search bar.
        if not self.search_focus:
            if key == ord(" "):
                self.mpv.toggle_pause(); return True
            if key == ord("n"):
                self._do_next();  return True
            if key == ord("p"):
                self._do_prev();  return True
            if key == ord("r"):
                self._do_radio(); return True
            if key == ord("a"):
                self._do_add_all(); return True
            if key == ord("d"):
                self._do_delete(); return True
            if key == ord("]"):
                v = min(150, self.np_vol + 5); self.mpv.set_volume(v); self.np_vol = v; return True
            if key == ord("["):
                v = max(0, self.np_vol - 5);   self.mpv.set_volume(v); self.np_vol = v; return True
            if key == ord("q"):
                return False

        # ── / — focus search ──
        if key == ord("/"):
            self.search_focus = True
            return True

        # ── Printable character → goes to search bar ──
        if 32 <= key <= 126:
            self.search_buf  += chr(key)
            self.search_focus = True
            return True

        return True

    # ── Main loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        self._init_colors()
        curses.curs_set(1)
        self.scr.nodelay(True)   # non-blocking getch
        self.scr.timeout(100)    # wake up every 100 ms to poll threads

        # Start mpv
        if not self.mpv.available:
            self._notify("mpv not found — install mpv for playback")
        elif not self.mpv.start():
            self._notify("mpv failed to start")

        # Background threads
        threading.Thread(target=self._mpv_poll_thread, daemon=True).start()

        # Kick off recommendations in background for the For You tab
        threading.Thread(target=self._recs_thread, daemon=True).start()

        try:
            while True:
                self._drain_queue()
                if self._dirty:
                    try:
                        self._draw()
                    except curses.error:
                        pass

                key = self.scr.getch()
                if key == curses.KEY_RESIZE:
                    self._dirty = True
                    continue
                if key == curses.ERR:
                    continue
                if not self._handle_key(key):
                    break
        finally:
            cur = self.queue.current()
            if cur:
                try:
                    self.session.report_stream_cancel(cur.track_id)
                except Exception:
                    pass
            self.mpv.quit()


# ══════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════

def main(dev: bool = False) -> None:
    try:
        sess = QobuzSession.from_config(dev=dev)
    except FileNotFoundError:
        print("\n[tui] Not logged in.\n  Run: qobuz login -u EMAIL -p PASSWORD\n",
              file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\n[tui] Session error: {exc}\n", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)

    def _run(stdscr: "curses.window") -> None:
        curses.cbreak()
        curses.noecho()
        stdscr.keypad(True)
        UI(stdscr, sess).run()

    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\n[tui] Crash: {exc}\n", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Qobuz TUI (curses)")
    ap.add_argument("--dev", action="store_true")
    main(dev=ap.parse_args().dev)

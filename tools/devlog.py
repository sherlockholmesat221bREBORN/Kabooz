#!/usr/bin/env python3  
"""  
tools/devlog.py — Interactive TUI for managing DEVLOG.md  
  
Usage:  
    python tools/devlog.py              # open TUI in current repo  
    python tools/devlog.py --repo /path # open TUI for a specific repo  
  
Install dependency:  
    pip install textual --break-system-packages  
  
Keybindings:  
    a           Add item to current tab  
    d / Delete  Delete selected item  
    Space       Toggle done/undone (DONE and TODO tabs)  
    Tab         Next tab  
    Shift+Tab   Previous tab  
    s           Save session to DEVLOG.md  
    q / Ctrl+C  Quit (warns if unsaved)  
"""  
from __future__ import annotations  
  
import argparse  
import json  
import re  
import sys  
from datetime import datetime, timezone  
from pathlib import Path  
from dataclasses import dataclass, field, asdict  
from typing import Optional  
  
# ── Data model ─────────────────────────────────────────────────────────────  
  
@dataclass  
class Item:  
    text: str  
    checked: bool = False  
    carried: bool = False   # True if brought forward from a previous session  
  
  
@dataclass  
class Session:  
    date:    str                          # YYYY-MM-DD UTC  
    author:  str  = ""  
    commits: str  = ""                    # e.g. "abc1234..def5678"  
    done:    list[Item] = field(default_factory=list)  
    todo:    list[Item] = field(default_factory=list)  
    bugs:    list[Item] = field(default_factory=list)  
    notes:   list[Item] = field(default_factory=list)  
  
    def to_dict(self) -> dict:  
        return {  
            "date":    self.date,  
            "author":  self.author,  
            "commits": self.commits,  
            "done":    [asdict(i) for i in self.done],  
            "todo":    [asdict(i) for i in self.todo],  
            "bugs":    [asdict(i) for i in self.bugs],  
            "notes":   [asdict(i) for i in self.notes],  
        }  
  
    @classmethod  
    def from_dict(cls, d: dict) -> Session:  
        s = cls(date=d["date"], author=d.get("author",""), commits=d.get("commits",""))  
        for key in ("done", "todo", "bugs", "notes"):  
            setattr(s, key, [Item(**i) for i in d.get(key, [])])  
        return s  
  
    def items_for(self, tab: str) -> list[Item]:  
        return getattr(self, tab.lower())  
  
  
# ── DEVLOG.md parser ────────────────────────────────────────────────────────  
  
_HEADER_RE  = re.compile(r"^### (\d{4}-\d{2}-\d{2})")  
_CHECKED_RE = re.compile(r"^- \[x\] (.+)", re.IGNORECASE)  
_OPEN_RE    = re.compile(r"^- \[ \] (.+)")  
_SECTION_RE = re.compile(r"^#### (DONE|TODO|BUGS|NOTES)", re.IGNORECASE)  
  
  
def parse_devlog(path: Path) -> tuple[str, list[Item]]:  
    """  
    Read DEVLOG.md and return:  
      - the full file text (to prepend when saving)  
      - unchecked TODO items from the most recent session (to carry forward)  
    """  
    if not path.exists():  
        return "", []  
  
    text = path.read_text(encoding="utf-8")  
    lines = text.splitlines()  
  
    # Find sessions in reverse order (most recent first).  
    session_starts = [i for i, l in enumerate(lines) if _HEADER_RE.match(l)]  
    if not session_starts:  
        return text, []  
  
    last_start = session_starts[-1]  
    session_lines = lines[last_start:]  
  
    # Extract unchecked TODOs from the last session.  
    in_todo = False  
    carried: list[Item] = []  
    for line in session_lines:  
        if _SECTION_RE.match(line):  
            in_todo = _SECTION_RE.match(line).group(1).upper() == "TODO"  
            continue  
        if _HEADER_RE.match(line) and line != session_lines[0]:  
            break  
        if in_todo:  
            m = _OPEN_RE.match(line.strip())  
            if m:  
                carried.append(Item(text=m.group(1), checked=False, carried=True))  
  
    return text, carried  
  
  
def render_session(session: Session) -> str:  
    """Render a Session to the markdown block that gets appended to DEVLOG.md."""  
    lines = [  
        f"### {session.date}",  
        f"**Author**: [{session.author}]  ",  
        f"**Commits Made**: {session.commits or '[not set]'}",  
    ]  
  
    sections = [  
        ("DONE",  session.done),  
        ("TODO",  session.todo),  
        ("BUGS",  session.bugs),  
        ("NOTES", session.notes),  
    ]  
  
    for heading, items in sections:  
        if not items:  
            continue  
        lines.append(f"#### {heading}")  
        for item in items:  
            if heading in ("DONE", "TODO"):  
                mark = "x" if item.checked else " "  
                lines.append(f"- [{mark}] {item.text}")  
            else:  
                lines.append(f"- {item.text}")  
  
    return "\n".join(lines) + "\n"  
  
  
# ── WIP persistence ─────────────────────────────────────────────────────────  
  
def load_wip(wip_path: Path) -> Optional[Session]:  
    if not wip_path.exists():  
        return None  
    try:  
        return Session.from_dict(json.loads(wip_path.read_text()))  
    except Exception:  
        return None  
  
  
def save_wip(session: Session, wip_path: Path) -> None:  
    wip_path.write_text(json.dumps(session.to_dict(), indent=2))  
  
  
def clear_wip(wip_path: Path) -> None:  
    if wip_path.exists():  
        wip_path.unlink()  
  
  
# ── Config ──────────────────────────────────────────────────────────────────  
  
def load_author(repo: Path) -> str:  
    """Read saved author alias from .devlog_author in repo root."""  
    p = repo / ".devlog_author"  
    if p.exists():  
        return p.read_text().strip()  
    return ""  
  
  
def save_author(author: str, repo: Path) -> None:  
    (repo / ".devlog_author").write_text(author)  
  
  
# ── TUI ─────────────────────────────────────────────────────────────────────  
  
try:  
    from textual.app import App, ComposeResult  
    from textual.binding import Binding  
    from textual.containers import Container, Horizontal, Vertical  
    from textual.css.query import NoMatches  
    from textual.reactive import reactive  
    from textual.screen import ModalScreen  
    from textual.widgets import (  
        Button,  
        Footer,  
        Header,  
        Input,  
        Label,  
        ListItem,  
        ListView,  
        Static,  
        TabbedContent,  
        TabPane,  
    )  
except ImportError:  
    print(  
        "Textual is not installed.\n"  
        "Run: pip install textual --break-system-packages",  
        file=sys.stderr,  
    )  
    sys.exit(1)  
  
  
TABS = ["DONE", "TODO", "BUGS", "NOTES"]  
  
CSS = """  
Screen {  
    background: $surface;  
}  
  
#header-bar {  
    height: 4;  
    background: $panel;  
    padding: 0 2;  
    border-bottom: solid $primary;  
}  
  
#header-bar Label {  
    color: $text-muted;  
    margin-right: 2;  
}  
  
#header-bar Input {  
    width: 30;  
    margin-right: 2;  
    height: 1;  
    border: none;  
    background: $surface;  
}  
  
#header-author {  
    width: 24;  
}  
  
#header-commits {  
    width: 30;  
}  
  
#date-label {  
    color: $primary;  
    text-style: bold;  
    margin-right: 2;  
}  
  
#item-list {  
    height: 1fr;  
    border: solid $panel-lighten-2;  
    padding: 0 1;  
}  
  
.item--carried {  
    color: $text-muted;  
    text-style: italic;  
}  
  
.item--checked {  
    color: $success;  
    text-style: strike;  
}  
  
.item--open {  
    color: $text;  
}  
  
.item--bug {  
    color: $warning;  
}  
  
.item--note {  
    color: $secondary;  
}  
  
#input-bar {  
    height: 3;  
    border-top: solid $panel-lighten-2;  
    padding: 0 1;  
}  
  
#add-input {  
    width: 1fr;  
    border: solid $primary;  
}  
  
#status-bar {  
    height: 1;  
    background: $panel;  
    padding: 0 2;  
    color: $text-muted;  
}  
  
.unsaved {  
    color: $warning;  
    text-style: bold;  
}  
"""  
  
  
class InputModal(ModalScreen[str]):  
    """A simple modal that captures a single line of text."""  
  
    DEFAULT_CSS = """  
    InputModal {  
        align: center middle;  
    }  
    #modal-box {  
        width: 60;  
        height: 7;  
        background: $panel;  
        border: solid $primary;  
        padding: 1 2;  
    }  
    #modal-label {  
        margin-bottom: 1;  
        color: $text;  
    }  
    #modal-input {  
        width: 1fr;  
    }  
    """  
  
    def __init__(self, prompt: str, default: str = "") -> None:  
        super().__init__()  
        self._prompt  = prompt  
        self._default = default  
  
    def compose(self) -> ComposeResult:  
        with Container(id="modal-box"):  
            yield Label(self._prompt, id="modal-label")  
            yield Input(value=self._default, id="modal-input")  
  
    def on_mount(self) -> None:  
        self.query_one("#modal-input", Input).focus()  
  
    def on_input_submitted(self, event: Input.Submitted) -> None:  
        self.dismiss(event.value.strip())  
  
    def on_key(self, event) -> None:  
        if event.key == "escape":  
            self.dismiss("")  
  
  
class ConfirmModal(ModalScreen[bool]):  
    """A yes/no confirmation modal."""  
  
    DEFAULT_CSS = """  
    ConfirmModal {  
        align: center middle;  
    }  
    #confirm-box {  
        width: 50;  
        height: 7;  
        background: $panel;  
        border: solid $warning;  
        padding: 1 2;  
    }  
    #confirm-label {  
        margin-bottom: 1;  
        color: $text;  
        text-align: center;  
    }  
    #confirm-buttons {  
        align: center middle;  
        height: 3;  
    }  
    Button {  
        margin: 0 2;  
    }  
    """  
  
    def __init__(self, message: str) -> None:  
        super().__init__()  
        self._message = message  
  
    def compose(self) -> ComposeResult:  
        with Container(id="confirm-box"):  
            yield Label(self._message, id="confirm-label")  
            with Horizontal(id="confirm-buttons"):  
                yield Button("Yes", variant="warning", id="yes")  
                yield Button("No",  variant="primary",  id="no")  
  
    def on_button_pressed(self, event: Button.Pressed) -> None:  
        self.dismiss(event.button.id == "yes")  
  
    def on_key(self, event) -> None:  
        if event.key == "escape":  
            self.dismiss(False)  
        elif event.key == "y":  
            self.dismiss(True)  
        elif event.key == "n":  
            self.dismiss(False)  
  
  
class DevlogApp(App):  
    """Main devlog TUI."""  
  
    TITLE   = "devlog"  
    CSS     = CSS  
    BINDINGS = [  
        Binding("a",          "add_item",    "Add"),  
        Binding("d",          "delete_item", "Delete"),  
        Binding("delete",     "delete_item", "Delete", show=False),  
        Binding("space",      "toggle_item", "Toggle", show=False),  
        Binding("s",          "save",        "Save"),  
        Binding("q",          "quit_safe",   "Quit"),  
        Binding("ctrl+c",     "quit_safe",   "Quit", show=False),  
        Binding("tab",        "next_tab",    "Next tab",  show=False),  
        Binding("shift+tab",  "prev_tab",    "Prev tab",  show=False),  
    ]  
  
    unsaved: reactive[bool] = reactive(False)  
  
    def __init__(self, repo: Path) -> None:  
        super().__init__()  
        self.repo        = repo  
        self.devlog_path = repo / "DEVLOG.md"  
        self.wip_path    = repo / ".devlog_wip.json"  
        self.session     = self._init_session()  
        self._current_tab: str = "DONE"  
  
    def _init_session(self) -> Session:  
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")  
  
        # Resume a WIP session if it exists for today.  
        wip = load_wip(self.wip_path)  
        if wip and wip.date == today:  
            return wip  
  
        # Start a new session, carrying forward unchecked TODOs.  
        _, carried = parse_devlog(self.devlog_path)  
        author     = load_author(self.repo)  
        session    = Session(date=today, author=author)  
        session.todo = carried  
        return session  
  
    # ── Layout ──────────────────────────────────────────────────────────────  
  
    def compose(self) -> ComposeResult:  
        yield Header(show_clock=False)  
  
        with Horizontal(id="header-bar"):  
            yield Label(f"📅 {self.session.date}", id="date-label")  
            yield Label("Author:")  
            yield Input(  
                value=self.session.author,  
                placeholder="alias",  
                id="header-author",  
            )  
            yield Label("Commits:")  
            yield Input(  
                value=self.session.commits,  
                placeholder="abc1234..def5678",  
                id="header-commits",  
            )  
  
        with TabbedContent(*TABS, id="tabs"):  
            for tab in TABS:  
                with TabPane(tab, id=f"pane-{tab.lower()}"):  
                    yield ListView(id=f"list-{tab.lower()}")  
  
        with Horizontal(id="input-bar"):  
            yield Input(placeholder="New item… (Enter to add, Esc to cancel)", id="add-input")  
  
        yield Static("", id="status-bar")  
        yield Footer()  
  
    def on_mount(self) -> None:  
        self._refresh_all_lists()  
        self._set_status("Session loaded. Press [a] to add, [s] to save, [q] to quit.")  
  
    # ── List rendering ───────────────────────────────────────────────────────  
  
    def _refresh_all_lists(self) -> None:  
        for tab in TABS:  
            self._refresh_list(tab)  
  
    def _refresh_list(self, tab: str) -> None:  
        lv = self.query_one(f"#list-{tab.lower()}", ListView)  
        lv.clear()  
        items = self.session.items_for(tab)  
        for idx, item in enumerate(items):  
            label = self._render_item_label(tab, item)  
            lv.append(ListItem(Label(label, markup=False)))  
  
    def _render_item_label(self, tab: str, item: Item) -> str:  
        if tab in ("DONE", "TODO"):  
            mark   = "✓" if item.checked else "○"  
            suffix = " ↩" if item.carried else ""  
            text   = f" {mark}  {item.text}{suffix}"  
        else:  
            prefix = "⚠ " if tab == "BUGS" else "• "  
            text   = f" {prefix} {item.text}"  
        return text  
  
    # ── Tab tracking ─────────────────────────────────────────────────────────  
  
    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:  
        label = str(event.tab.label)  
        if label in TABS:  
            self._current_tab = label  
  
    # ── Input: header fields ─────────────────────────────────────────────────  
  
    def on_input_changed(self, event: Input.Changed) -> None:  
        if event.input.id == "header-author":  
            self.session.author = event.value  
            self.unsaved = True  
        elif event.input.id == "header-commits":  
            self.session.commits = event.value  
            self.unsaved = True  
  
    # ── Input: add item via bottom bar ────────────────────────────────────────  
  
    def on_input_submitted(self, event: Input.Submitted) -> None:  
        if event.input.id != "add-input":  
            return  
        text = event.value.strip()  
        if not text:  
            return  
        self._add_item_text(text)  
        event.input.value = ""  
  
    # ── Actions ──────────────────────────────────────────────────────────────  
  
    def action_add_item(self) -> None:  
        """Focus the add-input bar."""  
        self.query_one("#add-input", Input).focus()  
  
    def _add_item_text(self, text: str) -> None:  
        tab   = self._current_tab  
        items = self.session.items_for(tab)  
        items.append(Item(text=text, checked=(tab == "DONE")))  
        self._refresh_list(tab)  
        self.unsaved = True  
        self._set_status(f"Added to {tab}.")  
        # Re-focus the list.  
        self.query_one(f"#list-{tab.lower()}", ListView).focus()  
  
    def action_delete_item(self) -> None:  
        tab = self._current_tab  
        lv  = self.query_one(f"#list-{tab.lower()}", ListView)  
        idx = lv.index  
        if idx is None:  
            return  
        items = self.session.items_for(tab)  
        if 0 <= idx < len(items):  
            removed = items.pop(idx)  
            self._refresh_list(tab)  
            self.unsaved = True  
            self._set_status(f"Deleted: {removed.text}")  
  
    def action_toggle_item(self) -> None:  
        tab = self._current_tab  
        if tab not in ("DONE", "TODO"):  
            return  
        lv  = self.query_one(f"#list-{tab.lower()}", ListView)  
        idx = lv.index  
        if idx is None:  
            return  
        items = self.session.items_for(tab)  
        if 0 <= idx < len(items):  
            items[idx].checked = not items[idx].checked  
            self._refresh_list(tab)  
            self.unsaved = True  
  
    def action_next_tab(self) -> None:  
        tc  = self.query_one("#tabs", TabbedContent)  
        idx = TABS.index(self._current_tab)  
        tc.active = f"pane-{TABS[(idx + 1) % len(TABS)].lower()}"  
  
    def action_prev_tab(self) -> None:  
        tc  = self.query_one("#tabs", TabbedContent)  
        idx = TABS.index(self._current_tab)  
        tc.active = f"pane-{TABS[(idx - 1) % len(TABS)].lower()}"  
  
    def action_save(self) -> None:  
        self._save_to_devlog()  
  
    def action_quit_safe(self) -> None:  
        if self.unsaved:  
            self.push_screen(  
                ConfirmModal("Unsaved changes. Quit anyway?"),  
                self._on_quit_confirm,  
            )  
        else:  
            self.exit()  
  
    def _on_quit_confirm(self, confirmed: bool) -> None:  
        if confirmed:  
            self.exit()  
  
    # ── Save logic ────────────────────────────────────────────────────────────  
  
    def _save_to_devlog(self) -> None:  
        if self.session.author:  
            save_author(self.session.author, self.repo)  
              
        new_entry = render_session(self.session)  
  
        if not self.devlog_path.exists():  
            self.devlog_path.write_text(_DEVLOG_HEADER + "\n" + new_entry + "\n", encoding="utf-8")  
            save_wip(self.session, self.wip_path)  
            self.unsaved = False  
            self._set_status(f"Saved to {self.devlog_path.name}.")  
            return  
  
        text  = self.devlog_path.read_text(encoding="utf-8")  
        lines = text.splitlines(keepends=True)  
  
        # Find the start and end of today's existing entry, if any.  
        today      = self.session.date  
        start_idx  = None  
        end_idx    = None  
  
        for i, line in enumerate(lines):  
            m = _HEADER_RE.match(line)  
            if m:  
                if m.group(1) == today and start_idx is None:  
                    start_idx = i  
                elif start_idx is not None:  
                    end_idx = i  
                    break  
      
        if start_idx is not None:  
            # Replace the existing entry for today.  
            end = end_idx if end_idx is not None else len(lines)  
            lines[start_idx:end] = [new_entry + "\n"]  
        else:  
            # No entry for today yet — insert before the first session entry.  
            insert_at = len(lines)  
            for i, line in enumerate(lines):  
                if _HEADER_RE.match(line):  
                    insert_at = i  
                    break  
            lines.insert(insert_at, new_entry + "\n")  
      
        self.devlog_path.write_text("".join(lines), encoding="utf-8")  
        save_wip(self.session, self.wip_path)  
        self.unsaved = False  
        self._set_status(f"Saved to {self.devlog_path.name}.")  
    # ── Status bar ────────────────────────────────────────────────────────────  
  
    def _set_status(self, msg: str) -> None:  
        try:  
            bar = self.query_one("#status-bar", Static)  
            bar.update(msg)  
        except NoMatches:  
            pass  
  
    def watch_unsaved(self, value: bool) -> None:  
        try:  
            bar = self.query_one("#status-bar", Static)  
            if value:  
                bar.add_class("unsaved")  
            else:  
                bar.remove_class("unsaved")  
        except NoMatches:  
            pass  
  
  
# ── Boilerplate header written to new DEVLOG.md files ───────────────────────  
  
_DEVLOG_HEADER = r"""\  
# DEV LOGS  
  
This file records logs written by the developer for future reference (self or collaborators).  
This is NOT user documentation; it exists purely for developer reference.  
Entries may be incomplete, outdated, or inaccurate.  
  
## Format  
1. An entry should be made every session.  
   A session usually corresponds to a single day.  
2. All dates must be in UTC. Mentioning time is discouraged; if used, it must also be in UTC.  
3. This file should be updated in the last commit of a session, and should be omitted in **Commits Made** list.  
4. A single session entry must follow this format:  
  
    ### YYYY-MM-DD  
    **Author**: [Your name / alias]  
    **Commits Made**: commit list or range  
  
    - Use bullet points.  
    - Use explicit sub-headings (####) such as:  
      - TODO  
      - DONE  
      - NOTES  
      - BUGS  
      - IDEAS  
  
---  
  
"""  
  
  
# ── Entry point ──────────────────────────────────────────────────────────────  
  
def main() -> None:  
    parser = argparse.ArgumentParser(description="Devlog TUI")  
    parser.add_argument(  
        "--repo", "-r",  
        type=Path,  
        default=Path("."),  
        help="Path to repo root (default: current directory)",  
    )  
    args = parser.parse_args()  
  
    repo = args.repo.resolve()  
    if not repo.is_dir():  
        print(f"Not a directory: {repo}", file=sys.stderr)  
        sys.exit(1)  
  
    DevlogApp(repo=repo).run()  
  
  
if __name__ == "__main__":  
    main()

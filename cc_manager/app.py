"""The cc-manager terminal UI."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, Static

from . import park as park_mod
from .launcher import LauncherError, resume
from .parser import EndState, SessionMeta
from .paths import all_project_dirs, project_dir_for
from .scanner import scan
from .util import format_absolute, format_relative, format_size

VIEW_ACTIVE, VIEW_PARKED, VIEW_ALL = "active", "parked", "all"
_VIEW_CYCLE = (VIEW_ACTIVE, VIEW_PARKED, VIEW_ALL)

_STATE_GLYPH = {
    EndState.LIVE: ("●", "bold #4ade80"),
    EndState.CRASHED: ("⚠", "bold #f87171"),
    EndState.INTERRUPTED: ("◐", "bold #fbbf24"),
    EndState.CLEAN: ("·", "#6b7280"),
}

_STATE_LABEL = {
    EndState.LIVE: "running now",
    EndState.CRASHED: "ended uncleanly (crash / power loss)",
    EndState.INTERRUPTED: "stopped mid-turn",
    EndState.CLEAN: "closed normally",
}


class TextPrompt(ModalScreen[str | None]):
    """One-line modal input."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, heading: str, value: str = "", placeholder: str = "") -> None:
        super().__init__()
        self._heading = heading
        self._value = value
        self._placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._heading, id="prompt-heading")
            yield Input(value=self._value, placeholder=self._placeholder, id="prompt-input")
            yield Label("enter confirm · esc cancel", id="prompt-hint")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen[bool]):
    """Yes/no modal."""

    BINDINGS = [
        Binding("escape,n", "no", "No", show=False),
        Binding("y,enter", "yes", "Yes", show=False),
    ]

    def __init__(self, heading: str, detail: str = "") -> None:
        super().__init__()
        self._heading = heading
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._heading, id="prompt-heading")
            if self._detail:
                yield Label(self._detail, id="prompt-detail")
            yield Label("y confirm · n / esc cancel", id="prompt-hint")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class CCManager(App[None]):
    """Browse, search, park and resume Claude Code sessions."""

    CSS = """
    Screen { background: #0b0f14; }

    #topbar {
        height: 1; padding: 0 1;
        background: #111827; color: #9ca3af;
    }
    #title { width: 1fr; color: #e5e7eb; text-style: bold; }
    #counts { width: auto; }

    #search { height: 3; border: none; background: #0f172a; color: #e5e7eb; }
    #search:focus { background: #111d33; }

    DataTable {
        height: 1fr;
        background: #0b0f14;
        color: #d1d5db;
        scrollbar-size-vertical: 1;
    }
    DataTable > .datatable--cursor { background: #1f3a5f; color: #ffffff; }
    DataTable > .datatable--header { background: #111827; color: #93c5fd; text-style: bold; }

    #detail {
        height: 7; padding: 0 1;
        background: #0f172a; color: #9ca3af;
        border-top: solid #1f2937;
    }

    #prompt-box {
        width: 72; height: auto; padding: 1 2;
        background: #111827; border: round #3b82f6;
    }
    #prompt-heading { color: #e5e7eb; text-style: bold; }
    #prompt-detail { color: #9ca3af; padding-top: 1; }
    #prompt-hint { color: #6b7280; padding-top: 1; }
    ModalScreen { align: center middle; }
    """

    BINDINGS = [
        Binding("enter", "resume", "Resume"),
        Binding("f", "resume_fork", "Fork"),
        Binding("p", "toggle_park", "Park"),
        Binding("n", "rename", "Rename"),
        Binding("v", "cycle_view", "View"),
        Binding("a", "toggle_all_projects", "All projects"),
        Binding("r", "rescan", "Rescan"),
        Binding("slash", "focus_search", "Search", key_display="/"),
        Binding("c", "copy_id", "Copy id"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, *, all_projects: bool = False, safe: bool = False,
                 cwd: str | None = None) -> None:
        super().__init__()
        self.all_projects = all_projects
        self.write_titles = not safe
        self.start_cwd = cwd or str(Path.cwd())
        self.sessions: list[SessionMeta] = []
        self.filtered: list[SessionMeta] = []
        self.view = VIEW_ACTIVE
        self.filter_text = ""
        self.scanning = False

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("cc-manager", id="title")
            yield Static("", id="counts")
        yield Input(placeholder="search  (branch, title, prompt, id)…", id="search")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
        yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_column("", width=2, key="state")
        table.add_column("Branch", width=20, key="branch")
        table.add_column("Last activity", width=14, key="when")
        table.add_column("Size", width=8, key="size")
        table.add_column("Summary", key="summary")
        table.focus()
        self.do_scan()

    # ------------------------------------------------------------------ scanning

    @work(exclusive=True, thread=True)
    def do_scan(self) -> None:
        """Full rescan of every transcript.  Runs off the UI thread."""
        dirs = all_project_dirs() if self.all_projects else [project_dir_for(self.start_cwd)]
        try:
            sessions = scan(dirs)
        except Exception as exc:  # a broken transcript must not kill the UI
            self.call_from_thread(self.notify, f"scan failed: {exc}", severity="error")
            sessions = []
        self.call_from_thread(self._apply_scan, sessions)

    def _apply_scan(self, sessions: list[SessionMeta]) -> None:
        self.sessions = sessions
        self.refresh_table()
        crashed = sum(1 for s in sessions if s.end_state == EndState.CRASHED)
        if crashed:
            self.notify(
                f"{crashed} session(s) ended uncleanly — marked ⚠, resume to recover",
                severity="warning",
                timeout=6,
            )

    # ------------------------------------------------------------------ filtering

    def _matches(self, meta: SessionMeta) -> bool:
        if self.view == VIEW_ACTIVE and meta.parked:
            return False
        if self.view == VIEW_PARKED and not meta.parked:
            return False
        needle = self.filter_text.strip().lower()
        if not needle:
            return True
        hay = " ".join(
            x for x in (
                meta.title, meta.summary, meta.branch, meta.session_id,
                meta.cwd, meta.project_dir, meta.first_prompt, meta.live_status,
            ) if x
        ).lower()
        return all(term in hay for term in needle.split())

    def refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        previous = self._selected_id()
        table.clear()

        self.filtered = [m for m in self.sessions if self._matches(m)]
        now = datetime.now(timezone.utc)

        for meta in self.filtered:
            glyph, style = _STATE_GLYPH[meta.end_state]
            if meta.parked:
                glyph, style = "⏸", "#a78bfa"

            branch = Text(meta.branch[:20], style="#7dd3fc" if meta.branch != "-" else "#4b5563")
            when = Text(format_relative(meta.last_activity, now=now), style="#9ca3af")
            size = Text(format_size(meta.size), style="#4b5563")

            summary = Text(no_control(meta.title), style="#e5e7eb")
            if self.all_projects and meta.cwd:
                summary.append(f"  ({Path(meta.cwd).name})", style="#4b5563")
            if meta.is_live and meta.live_status:
                summary.append(f"  [{meta.live_status}]", style="#4ade80")

            table.add_row(Text(glyph, style=style), branch, when, size, summary,
                          key=meta.session_id)

        self._update_counts()
        if previous:
            self._select_id(previous)
        self._update_detail()

    def _update_counts(self) -> None:
        total = len(self.sessions)
        parked = sum(1 for s in self.sessions if s.parked)
        live = sum(1 for s in self.sessions if s.is_live)
        bad = sum(1 for s in self.sessions if s.end_state == EndState.CRASHED)

        scope = "all projects" if self.all_projects else Path(self.start_cwd).name
        self.query_one("#title", Static).update(
            Text.assemble(("cc-manager", "bold #93c5fd"), ("  ", ""), (scope, "#6b7280"))
        )
        counts = Text.assemble(
            (f"{len(self.filtered)}/{total}", "#e5e7eb"), (" shown  ", "#6b7280"),
            (f"●{live}", "#4ade80"), ("  ", ""),
            (f"⚠{bad}", "#f87171"), ("  ", ""),
            (f"⏸{parked}", "#a78bfa"), ("   ", ""),
            (f"view:{self.view}", "#fbbf24"),
        )
        self.query_one("#counts", Static).update(counts)

    # ------------------------------------------------------------------ selection

    def _selected_id(self) -> str | None:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
        except Exception:
            return None
        return row_key.value

    def _select_id(self, session_id: str) -> None:
        table = self.query_one("#table", DataTable)
        try:
            index = table.get_row_index(session_id)
        except Exception:
            return
        table.move_cursor(row=index)

    def current(self) -> SessionMeta | None:
        sid = self._selected_id()
        if sid is None:
            return None
        return next((m for m in self.filtered if m.session_id == sid), None)

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self._update_detail()

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_resume()

    def _update_detail(self) -> None:
        panel = self.query_one("#detail", Static)
        meta = self.current()
        if meta is None:
            panel.update(Text("no sessions match", style="#4b5563"))
            return

        state_style = _STATE_GLYPH[meta.end_state][1]
        body = Text()
        body.append(no_control(meta.title) + "\n", style="bold #e5e7eb")
        body.append(no_control(meta.summary) + "\n", style="#9ca3af")
        body.append(f"{meta.session_id}  ", style="#4b5563")
        body.append(_STATE_LABEL[meta.end_state], style=state_style)
        if meta.is_live and meta.live_pid:
            body.append(f" (pid {meta.live_pid})", style="#4ade80")
        if meta.parked:
            body.append("  ⏸ parked", style="#a78bfa")
            if meta.park_note:
                body.append(f": {meta.park_note}", style="#a78bfa")
        body.append("\n")
        body.append(
            f"{format_absolute(meta.last_activity)}   branch {meta.branch}   "
            f"{format_size(meta.size)}   {meta.cwd or '?'}",
            style="#4b5563",
        )
        panel.update(body)

    # ------------------------------------------------------------------- search

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.filter_text = event.value
            self.refresh_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.query_one("#table", DataTable).focus()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""
        self.query_one("#table", DataTable).focus()

    # ------------------------------------------------------------------ actions

    def action_cycle_view(self) -> None:
        self.view = _VIEW_CYCLE[(_VIEW_CYCLE.index(self.view) + 1) % len(_VIEW_CYCLE)]
        self.refresh_table()

    def action_toggle_all_projects(self) -> None:
        self.all_projects = not self.all_projects
        self.notify("scanning all projects…" if self.all_projects else "scanning this project…")
        self.do_scan()

    def action_rescan(self) -> None:
        self.notify("rescanning…")
        self.do_scan()

    def action_copy_id(self) -> None:
        meta = self.current()
        if meta is None:
            return
        try:
            self.copy_to_clipboard(meta.session_id)
            self.notify(f"copied {meta.short_id}")
        except Exception:
            self.notify(meta.session_id, title="session id", timeout=10)

    def action_resume(self) -> None:
        self._resume(fork=False)

    def action_resume_fork(self) -> None:
        self._resume(fork=True)

    def _resume(self, *, fork: bool) -> None:
        meta = self.current()
        if meta is None:
            return
        if meta.is_live and not fork:
            self.push_screen(
                Confirm(
                    "That session is open in another terminal.",
                    f"pid {meta.live_pid} · resuming may conflict. "
                    "Press f instead to fork a copy. Resume anyway?",
                ),
                lambda ok: self._do_resume(meta, fork=False) if ok else None,
            )
            return
        self._do_resume(meta, fork=fork)

    def _do_resume(self, meta: SessionMeta, *, fork: bool) -> None:
        try:
            with self.suspend():
                print(f"\n→ resuming {meta.short_id}  ({meta.title})\n")
                resume(meta, fork=fork)
        except LauncherError as exc:
            self.notify(str(exc), severity="error", timeout=10)
            return
        self.notify("back from session — rescanning")
        self.do_scan()

    def action_toggle_park(self) -> None:
        meta = self.current()
        if meta is None:
            return

        if meta.parked:
            try:
                restored = park_mod.unpark(meta, write_titles=self.write_titles)
            except OSError as exc:
                self.notify(f"unpark failed: {exc}", severity="error")
                return
            self.notify(f"unparked → {restored or meta.short_id}")
            self.do_scan()
            return

        def on_note(note: str | None) -> None:
            if note is None:
                return
            try:
                applied = park_mod.park(meta, note, write_titles=self.write_titles)
            except OSError as exc:
                self.notify(f"park failed: {exc}", severity="error")
                return
            self.notify(f"parked as {applied}")
            self.do_scan()

        self.push_screen(
            TextPrompt("Park this session as…", value="",
                       placeholder=park_mod.slugify(meta.title, meta.short_id)),
            on_note,
        )

    def action_rename(self) -> None:
        meta = self.current()
        if meta is None:
            return

        def on_title(title: str | None) -> None:
            if not title:
                return
            try:
                park_mod.rename(meta, title, write_titles=self.write_titles)
            except OSError as exc:
                self.notify(f"rename failed: {exc}", severity="error")
                return
            self.notify(f"renamed → {title}")
            self.do_scan()

        self.push_screen(TextPrompt("Rename session", value=meta.title), on_title)


def no_control(text: str) -> str:
    """Collapse whitespace so a value cannot break a single-line table cell."""
    return " ".join(text.split()) if text else ""

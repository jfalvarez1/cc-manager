"""Desktop launcher for Claude Code sessions.

A two-pane window: projects on the left, that project's sessions on the right.
Pick one and it opens in a fresh terminal running ``claude --resume``.

Built on tkinter so it runs on a stock Python with nothing to install.
"""

from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import time
import webbrowser

from . import config as cc_config
from . import park as park_mod
from .launcher import LauncherError, find_wt, spawn_terminal, terminal_command
from .parser import EndState, SessionMeta
from .paths import all_project_dirs, state_dir
from .scanner import scan
from .util import format_absolute, format_relative, format_size

# --- palette -----------------------------------------------------------------
BG = "#0b0f14"
PANEL = "#111827"
FIELD = "#0f172a"
LINE = "#1f2937"
TEXT = "#e5e7eb"
MUTED = "#9ca3af"
DIM = "#6b7280"
ACCENT = "#3b82f6"
SELECT = "#1f3a5f"

STATE_COLOR = {
    EndState.LIVE: "#4ade80",
    EndState.CRASHED: "#f87171",
    EndState.INTERRUPTED: "#fbbf24",
    EndState.CLEAN: "#9ca3af",
}
STATE_GLYPH = {
    EndState.LIVE: "● live",
    EndState.CRASHED: "⚠ crashed",
    EndState.INTERRUPTED: "◐ stopped",
    EndState.CLEAN: "· ok",
}

TERMINALS = [
    ("Tab in current window", "tab-here"),
    ("Tab in cc-manager window", "tab"),
    ("New window", "wt"),
    ("Isolated window", "isolated"),
    ("PowerShell 7", "pwsh"),
]

# A session takes a few seconds to appear in the registry after launching, so
# a just-opened one is remembered locally until the scan catches up.
OPENING_GRACE_SECONDS = 90
AUTO_REFRESH_MS = 20_000


def _settings_file() -> Path:
    return state_dir() / "gui.json"


def load_settings() -> dict:
    try:
        return json.loads(_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        _settings_file().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


class CCManagerGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("cc-manager — Claude Code sessions")
        self.geometry("1180x680")
        self.minsize(900, 480)
        self.configure(bg=BG)

        self.settings = load_settings()
        self.sessions: list[SessionMeta] = []
        self._scanning = False
        self.project: str | None = None       # None == all projects
        self.rows: list[SessionMeta] = []
        self._queue: queue.Queue = queue.Queue()
        # session id -> when we launched it, so a second double-click on a
        # session already opening does not start it twice.
        self.launched: dict[str, float] = {}

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_sessions())
        self.show_parked = tk.BooleanVar(value=self.settings.get("show_parked", False))
        default_term = self.settings.get("terminal") or (
            "tab-here" if find_wt() else "isolated")
        self.terminal = tk.StringVar(value=default_term)

        self._style()
        self._build()
        self.after(50, self._drain)
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self.rescan()

    # ------------------------------------------------------------ open state

    def is_open(self, meta: SessionMeta) -> bool:
        """Already running, or launched so recently the scan has not seen it.

        A background job is excluded: it runs without a terminal, so attaching
        to it is always valid and is the only way to see it.
        """
        if meta.is_background:
            return False
        if meta.is_live:
            return True
        started = self.launched.get(meta.session_id)
        return started is not None and (time.time() - started) < OPENING_GRACE_SECONDS

    def is_opening(self, meta: SessionMeta) -> bool:
        """Launched by us, but not yet visible in the registry."""
        return not meta.is_live and self.is_open(meta)

    def _auto_refresh(self) -> None:
        # Keep live/parked state current without the user pressing Rescan.
        if not self._scanning:
            self.rescan()
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    # ---------------------------------------------------------------- styling

    def _style(self) -> None:
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=BG, foreground=TEXT,
                    fieldbackground=FIELD, borderwidth=0)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED)
        s.configure("Dim.TLabel", background=PANEL, foreground=DIM)
        s.configure("Head.TLabel", background=PANEL, foreground="#93c5fd",
                    font=("Segoe UI", 10, "bold"))
        s.configure("TButton", background=PANEL, foreground=TEXT,
                    padding=(10, 5), relief="flat")
        s.map("TButton",
              background=[("active", SELECT), ("disabled", PANEL)],
              foreground=[("disabled", DIM)])
        s.configure("Go.TButton", background=ACCENT, foreground="#ffffff",
                    font=("Segoe UI", 9, "bold"))
        s.map("Go.TButton", background=[("active", "#2563eb")])
        s.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                    insertcolor=TEXT, padding=6)
        s.configure("TCheckbutton", background=BG, foreground=MUTED)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT,
                    background=PANEL, arrowcolor=MUTED, padding=4)
        s.configure("Treeview", background=BG, fieldbackground=BG,
                    foreground=TEXT, rowheight=26, borderwidth=0)
        s.configure("Treeview.Heading", background=PANEL, foreground="#93c5fd",
                    relief="flat", padding=6)
        s.map("Treeview.Heading", background=[("active", PANEL)])
        s.map("Treeview", background=[("selected", SELECT)],
              foreground=[("selected", "#ffffff")])

    # ----------------------------------------------------------------- layout

    def _build(self) -> None:
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="cc-manager", style="Head.TLabel").pack(side="left")
        self.counts = ttk.Label(bar, text="", style="Dim.TLabel")
        self.counts.pack(side="left", padx=(12, 0))

        ttk.Label(bar, text="open in", style="Dim.TLabel").pack(side="left", padx=(24, 6))
        combo = ttk.Combobox(bar, width=18, state="readonly",
                             values=[label for label, _ in TERMINALS])
        combo.set(next(l for l, v in TERMINALS if v == self.terminal.get()))
        combo.bind("<<ComboboxSelected>>",
                   lambda e: self._set_terminal(combo.get()))
        combo.pack(side="left")

        ttk.Button(bar, text="Rescan", command=self.rescan).pack(side="right")
        ttk.Checkbutton(bar, text="show parked", variable=self.show_parked,
                        command=self.refresh_sessions).pack(side="right", padx=10)

        search = ttk.Frame(self, padding=(12, 8, 12, 4))
        search.pack(fill="x")
        entry = ttk.Entry(search, textvariable=self.search_var)
        entry.pack(fill="x")
        entry.insert(0, "")
        self._placeholder(entry, "search  (title, prompt, branch, id)")

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=(4, 0))

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        self.projects = tk.Listbox(
            left, bg=BG, fg=TEXT, selectbackground=SELECT, selectforeground="#fff",
            highlightthickness=0, borderwidth=0, activestyle="none",
            font=("Segoe UI", 9),
        )
        self.projects.pack(fill="both", expand=True)
        self.projects.bind("<<ListboxSelect>>", self._pick_project)

        right = ttk.Frame(panes)
        panes.add(right, weight=4)
        cols = ("state", "branch", "when", "size", "title")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="browse")
        for key, label, width, anchor in (
            ("state", "", 90, "w"), ("branch", "Branch", 150, "w"),
            ("when", "Last activity", 110, "w"), ("size", "Size", 80, "e"),
            ("title", "Session", 520, "w"),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "title"))
        vs = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        self.tree.bind("<Double-1>", lambda e: self.launch())
        self.tree.bind("<Return>", lambda e: self.launch())
        self.tree.bind("<Button-3>", self._popup)
        for state, color in STATE_COLOR.items():
            self.tree.tag_configure(state, foreground=color)
        self.tree.tag_configure("parked", foreground="#a78bfa")
        self.tree.tag_configure("opening", foreground="#38bdf8")

        self.detail = tk.Text(self, height=5, bg=FIELD, fg=MUTED, bd=0,
                              highlightthickness=0, wrap="word",
                              font=("Consolas", 9), padx=12, pady=8)
        self.detail.pack(fill="x", padx=12, pady=(8, 0))
        self.detail.configure(state="disabled")

        # The exact command for this session, so you can run it in your own
        # terminal instead of one cc-manager opens for you.
        cmdbar = ttk.Frame(self, style="Panel.TFrame", padding=(12, 6))
        cmdbar.pack(fill="x", padx=12, pady=(6, 0))
        ttk.Button(cmdbar, text="Copy", width=7,
                   command=lambda: self._copy(self.command_var.get())).pack(side="right")
        self.command_var = tk.StringVar(value="")
        cmd_entry = ttk.Entry(cmdbar, textvariable=self.command_var,
                              font=("Consolas", 9))
        cmd_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        cmd_entry.bind("<FocusIn>", lambda e: cmd_entry.select_range(0, "end"))
        self.cmd_entry = cmd_entry

        actions = ttk.Frame(self, padding=(12, 8))
        actions.pack(fill="x")
        self.go = ttk.Button(actions, text="Open session  ▸", style="Go.TButton",
                             command=self.launch)
        self.go.pack(side="left")
        for text, fn in (("Fork", lambda: self.launch(fork=True)),
                         ("Park / Unpark", self.toggle_park),
                         ("Rename", self.rename),
                         ("Open folder", self.open_folder)):
            ttk.Button(actions, text=text, command=fn).pack(side="left", padx=(8, 0))
        self.status = ttk.Label(actions, text="", style="Muted.TLabel")
        self.status.pack(side="right")

    def _placeholder(self, entry: ttk.Entry, text: str) -> None:
        def on_in(_):
            if not self.search_var.get():
                entry.configure(foreground=TEXT)

        def on_out(_):
            if not self.search_var.get():
                entry.configure(foreground=DIM)

        entry.configure(foreground=DIM)
        entry.bind("<FocusIn>", on_in)
        entry.bind("<FocusOut>", on_out)

    def _set_terminal(self, label: str) -> None:
        value = next(v for l, v in TERMINALS if l == label)
        self.terminal.set(value)
        self.settings["terminal"] = value
        save_settings(self.settings)

    # --------------------------------------------------------------- scanning

    def rescan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self.status.configure(text="scanning…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            found = scan(all_project_dirs())
            self._queue.put(("ok", found))
        except Exception as exc:                      # never kill the UI
            self._queue.put(("err", exc))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._scanning = False
                if kind == "ok":
                    self.sessions = payload
                    # Once a launched session shows up live, stop tracking it
                    # locally and let the registry be the source of truth.
                    for meta in payload:
                        if meta.is_live:
                            self.launched.pop(meta.session_id, None)
                    self.refresh_projects()
                    self.refresh_sessions()
                    self.status.configure(text=f"{len(payload)} sessions")
                else:
                    self.status.configure(text=f"scan failed: {payload}")
        except queue.Empty:
            pass
        self.after(120, self._drain)

    # --------------------------------------------------------------- rendering

    def _label_for(self, meta: SessionMeta) -> str:
        return Path(meta.cwd).name if meta.cwd else meta.project_dir

    def refresh_projects(self) -> None:
        counts: dict[str, int] = {}
        for meta in self.sessions:
            counts[self._label_for(meta)] = counts.get(self._label_for(meta), 0) + 1

        self.projects.delete(0, "end")
        self._project_names = [None] + sorted(counts, key=str.lower)
        self.projects.insert("end", f"  All projects ({len(self.sessions)})")
        for name in self._project_names[1:]:
            self.projects.insert("end", f"  {name} ({counts[name]})")
        index = self._project_names.index(self.project) if self.project in self._project_names else 0
        self.projects.selection_clear(0, "end")
        self.projects.selection_set(index)

    def _pick_project(self, _event=None) -> None:
        sel = self.projects.curselection()
        if sel:
            self.project = self._project_names[sel[0]]
            self.refresh_sessions()

    def _matches(self, meta: SessionMeta) -> bool:
        if meta.parked and not self.show_parked.get():
            return False
        if self.project and self._label_for(meta) != self.project:
            return False
        needle = self.search_var.get().strip().lower()
        if not needle:
            return True
        hay = " ".join(x for x in (meta.title, meta.summary, meta.branch,
                                   meta.session_id, meta.cwd) if x).lower()
        return all(t in hay for t in needle.split())

    def refresh_sessions(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows = [m for m in self.sessions if self._matches(m)]
        for i, meta in enumerate(self.rows):
            if self.is_opening(meta):
                tag, label = "opening", "◌ opening"
            elif meta.parked:
                tag, label = "parked", "⏸ parked"
            else:
                tag, label = meta.end_state, STATE_GLYPH[meta.end_state]
            title = meta.title
            if meta.remote_control:
                title = f"{title}   ⌁ remote"
            self.tree.insert(
                "", "end", iid=str(i), tags=(tag,),
                values=(label, meta.branch, format_relative(meta.last_activity),
                        format_size(meta.size), title),
            )
        live = sum(1 for m in self.sessions if m.is_live)
        bad = sum(1 for m in self.sessions if m.end_state == EndState.CRASHED)
        self.counts.configure(
            text=f"{len(self.rows)} shown · {live} live · {bad} crashed"
        )
        if self.rows:
            self.tree.selection_set("0")
            self.tree.focus("0")
        self._show_detail()

    def current(self) -> SessionMeta | None:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self.rows[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _show_detail(self) -> None:
        meta = self.current()
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if meta is None:
            self.detail.insert("1.0", "No session selected.")
            self.command_var.set("")
        else:
            self.command_var.set(meta.full_command)
            if meta.is_live:
                live = f"  ·  open now (pid {meta.live_pid})"
            elif self.is_opening(meta):
                live = "  ·  opening…"
            else:
                live = ""
            remote = "  ·  ⌁ Remote Control" if meta.remote_control else ""
            bg = "  ·  background job" if meta.is_background else ""
            # Flag anything not on the configured defaults, rather than
            # assuming every session inherited them.
            want_model = str(cc_config.get("model") or "").split("[")[0].lower()
            model = meta.model or "?"
            if want_model and want_model not in model.lower():
                model = f"{model} (not {want_model})"
            perm = meta.permission_mode or "?"
            if perm not in ("?", (cc_config.get("permissions") or {}).get(
                    "defaultMode", "bypassPermissions")):
                perm = f"{perm} !"
            self.detail.insert("1.0", (
                f"{meta.summary}\n"
                f"{meta.session_id}{live}{remote}{bg}\n"
                f"{format_absolute(meta.last_activity)}   branch {meta.branch}"
                f"   {format_size(meta.size)}   {model}   {perm}\n"
                f"{meta.cwd or '?'}"
            ))
        self.detail.configure(state="disabled")

        if meta is None:
            self.go.configure(state="disabled", text="Open session  ▸")
        elif meta.is_background:
            self.go.configure(state="normal", text="Attach  ▸")
        elif self.is_open(meta):
            self.go.configure(state="disabled", text="Already open")
        else:
            self.go.configure(state="normal", text="Open session  ▸")

    # ---------------------------------------------------------------- actions

    def launch(self, fork: bool = False, mode: str | None = None) -> None:
        meta = self.current()
        if meta is None:
            return

        if self.is_open(meta) and not fork:
            if self.is_opening(meta):
                self.status.configure(text=f"{meta.short_id} is already opening…")
                self.bell()
                return
            if not messagebox.askyesno(
                "Session is already open",
                f"“{meta.title}” is already running (pid {meta.live_pid}).\n\n"
                "Opening it again gives you two terminals driving one session, "
                "and Remote Control stays with the first.\n\n"
                "Fork it instead to work from a copy.\n\nOpen anyway?",
                parent=self, default="no", icon="warning",
            ):
                return

        try:
            spawn_terminal(meta, fork=fork, mode=mode or self.terminal.get())
        except LauncherError as exc:
            messagebox.showerror("Could not open terminal", str(exc), parent=self)
            return

        if not fork:
            self.launched[meta.session_id] = time.time()
        self.refresh_sessions()
        self.status.configure(
            text=f"opened {meta.short_id}" + (" (forked)" if fork else "")
        )
        # Pick the new session up in the registry once it has registered.
        self.after(6000, self.rescan)

    # ------------------------------------------------------------ right click

    def _popup(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.tree.focus(row)
            self._show_detail()
        meta = self.current()
        if meta is None:
            return

        m = tk.Menu(self, tearoff=0, bg=PANEL, fg=TEXT,
                    activebackground=SELECT, activeforeground="#ffffff",
                    bd=0, font=("Segoe UI", 9))

        already = self.is_open(meta) and not meta.is_background
        m.add_command(label="Open as tab here",
                      state="disabled" if already else "normal",
                      command=lambda: self.launch(mode="tab-here"))
        m.add_command(label="Open in cc-manager window",
                      state="disabled" if already else "normal",
                      command=lambda: self.launch(mode="tab"))
        m.add_command(label="Open in new window", state="disabled" if already else "normal",
                      command=lambda: self.launch(mode="wt"))
        m.add_command(label="Open in isolated window",
                      state="disabled" if already else "normal",
                      command=lambda: self.launch(mode="isolated"))
        if already:
            m.add_command(label="   (already open)", state="disabled")
        m.add_separator()
        m.add_command(label="Fork into a new tab",
                      command=lambda: self.launch(fork=True, mode="tab"))
        m.add_separator()

        rc = tk.Menu(m, tearoff=0, bg=PANEL, fg=TEXT,
                     activebackground=SELECT, activeforeground="#ffffff", bd=0)
        if meta.remote_control:
            rc.add_command(label="Connected — open on claude.ai",
                           command=lambda: webbrowser.open(meta.remote_url))
            rc.add_command(label="Copy session URL",
                           command=lambda: self._copy(meta.remote_url))
        else:
            rc.add_command(label="Not connected", state="disabled")
        rc.add_separator()
        on = bool(cc_config.get("remoteControlAtStartup"))
        rc.add_command(
            label=("✓ " if on else "   ") + "Auto-connect every new session",
            command=lambda: self._toggle_setting("remoteControlAtStartup"))
        m.add_cascade(label="Remote Control", menu=rc)

        m.add_command(label="Unpark" if meta.parked else "Park…",
                      command=self.toggle_park)
        m.add_command(label="Rename…", command=self.rename)
        m.add_separator()
        m.add_command(label="Copy session id",
                      command=lambda: self._copy(meta.session_id))
        m.add_command(label="Copy resume command",
                      command=lambda: self._copy(meta.resume_command))
        m.add_command(label="Copy with cd",
                      command=lambda: self._copy(meta.full_command))
        m.add_command(label="Copy launch command",
                      command=lambda: self._copy(" ".join(
                          terminal_command(meta, mode=self.terminal.get()))))
        m.add_command(label="Open folder", command=self.open_folder)

        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def _copy(self, text: str | None) -> None:
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.configure(text="copied")

    def _toggle_setting(self, key: str) -> None:
        try:
            new = cc_config.toggle(key)
        except (KeyError, OSError) as exc:
            messagebox.showerror("Could not change setting", str(exc), parent=self)
            return
        label = cc_config.TOGGLES[key][0]
        self.status.configure(text=f"{label}: {'on' if new else 'off'}")

    def toggle_park(self) -> None:
        meta = self.current()
        if meta is None:
            return
        try:
            if meta.parked:
                park_mod.unpark(meta)
            else:
                note = simpledialog.askstring(
                    "Park session", "Park as:", parent=self,
                    initialvalue=park_mod.slugify(meta.title, meta.short_id))
                if note is None:
                    return
                park_mod.park(meta, note)
        except OSError as exc:
            messagebox.showerror("Park failed", str(exc), parent=self)
            return
        self.rescan()

    def rename(self) -> None:
        meta = self.current()
        if meta is None:
            return
        title = simpledialog.askstring("Rename session", "New title:",
                                       parent=self, initialvalue=meta.title)
        if not title:
            return
        try:
            park_mod.rename(meta, title)
        except OSError as exc:
            messagebox.showerror("Rename failed", str(exc), parent=self)
            return
        self.rescan()

    def open_folder(self) -> None:
        meta = self.current()
        if meta is None or not meta.cwd:
            return
        try:
            import os
            os.startfile(meta.cwd)                      # noqa: S606 (Windows only)
        except (AttributeError, OSError) as exc:
            messagebox.showerror("Could not open folder", str(exc), parent=self)


def main() -> int:
    CCManagerGUI().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Desktop launcher for Claude Code sessions.

A two-pane window: projects on the left, that project's sessions on the right.
Pick one and it opens in a fresh terminal running ``claude --resume``.

Built on tkinter so it runs on a stock Python with nothing to install.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import time
import webbrowser

from . import config as cc_config
from . import park as park_mod
from .banner import ThemeBanner
from .tray import Tray, available as tray_available
from .launcher import LauncherError, find_wt, spawn_terminal, terminal_command
from .parser import EndState, SessionMeta
from .paths import all_project_dirs, state_dir
from .scanner import scan
from .util import format_absolute, format_relative, format_size

# --- themes ------------------------------------------------------------------
# Surface hues follow the chiptune tracker's own themes so the two apps agree,
# and every palette is contrast-checked rather than eyeballed -- that project
# audits its themes for a 4.5:1 text ratio, and a palette that looks right in
# a mockup can still be unreadable in place. tests/test_gui.py enforces it.
THEMES: dict[str, dict[str, str]] = {
    "midnight": {
        "label": "Midnight",
        "bg": "#0b0f14", "panel": "#111827", "field": "#0f172a",
        "text": "#e5e7eb", "muted": "#9ca3af", "dim": "#6b7280",
        "accent": "#3b82f6", "select": "#1f3a5f", "line": "#1f2937",
        "live": "#4ade80", "crashed": "#f87171", "interrupted": "#fbbf24",
        "clean": "#9ca3af", "parked": "#a78bfa", "opening": "#38bdf8",
        "font": "Segoe UI",
    },
    "matrix": {
        "label": "Matrix",
        "bg": "#08100a", "panel": "#0e1910", "field": "#050b07",
        "text": "#00ff41", "muted": "#00c633", "dim": "#00902f",
        "accent": "#00ff41", "select": "#0d3a18", "line": "#124a1e",
        "live": "#7cff5a", "crashed": "#ff6b60", "interrupted": "#e8ff36",
        "clean": "#00c633", "parked": "#3ae8c8", "opening": "#9dff8a",
        "font": "Consolas",
    },
    "synthwave": {
        "label": "Synthwave",
        "bg": "#0e0818", "panel": "#1b0e2a", "field": "#0a0512",
        "text": "#f4e9ff", "muted": "#c39dff", "dim": "#9878cf",
        "accent": "#ff2d95", "select": "#3d1a63", "line": "#2f1a4a",
        "live": "#00e5ff", "crashed": "#ff5fa8", "interrupted": "#ffb038",
        "clean": "#c39dff", "parked": "#c86bff", "opening": "#5ef0ff",
        "font": "Segoe UI",
    },
    "retro": {
        "label": "Retro Terminal",
        "bg": "#050500", "panel": "#140f00", "field": "#0a0800",
        "text": "#ffb000", "muted": "#d99500", "dim": "#a87400",
        "accent": "#ffb000", "select": "#3a2600", "line": "#4a3100",
        "live": "#ffd45e", "crashed": "#ff8a5c", "interrupted": "#ffe066",
        "clean": "#d99500", "parked": "#e8a55e", "opening": "#ffcf80",
        "font": "Consolas",
    },
    "aero": {
        "label": "Frutiger Aero",
        "bg": "#e0edf8", "panel": "#c9dff2", "field": "#f2f8fd",
        "text": "#0b2740", "muted": "#27506d", "dim": "#3d6b8a",
        "accent": "#0a6ea8", "select": "#a8d4ef", "line": "#9dc4e0",
        "live": "#0f6b2e", "crashed": "#a3201a", "interrupted": "#7a4f00",
        "clean": "#27506d", "parked": "#5b34a0", "opening": "#0a5f8f",
        "font": "Segoe UI",
    },
}
DEFAULT_THEME = "midnight"


def _srgb(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


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


def icon_path() -> Path | None:
    """The .ico, whether running from source or from a PyInstaller bundle."""
    roots = [Path(__file__).resolve().parent.parent]
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        roots.insert(0, Path(bundled))
    for root in roots:
        candidate = root / "cc_manager.ico"
        if candidate.is_file():
            return candidate
    return None


def claim_taskbar_identity() -> None:
    """Tell Windows this is its own application.

    Without an explicit AppUserModelID every process started by python.exe /
    pythonw.exe shares the interpreter's identity, so the taskbar groups
    unrelated Python apps under one generic Python icon and pinning pins the
    interpreter rather than this app. Harmless everywhere else.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "cc-manager.sessions")
    except Exception:
        pass


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
        # Settings and palette first: everything below paints with self.C.
        self.settings = load_settings()
        theme = self.settings.get("theme")
        self.theme_name = theme if theme in THEMES else DEFAULT_THEME
        self.C = THEMES[self.theme_name]

        self.title("cc-manager — Claude Code sessions")
        self.minsize(900, 480)
        self.geometry(self._restore_geometry())
        self.configure(bg=self.C["bg"])

        ico = icon_path()
        if ico:
            try:
                self.iconbitmap(default=str(ico))
            except tk.TclError:
                pass

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
        # Persist it: this used to be read at startup but never written back,
        # so the checkbox silently reset on every launch.
        self.show_parked.trace_add("write", lambda *_: self._remember(
            "show_parked", self.show_parked.get()))

        default_term = self.settings.get("terminal") or (
            "tab-here" if find_wt() else "isolated")
        self.terminal = tk.StringVar(value=default_term)
        self.project = self.settings.get("project") or None
        self.animated = tk.BooleanVar(value=self.settings.get("animated", True))
        self.animated.trace_add("write", lambda *_: self._toggle_animation())
        # Default to hiding rather than quitting: this is a thing you keep
        # around and glance at, so the X should park it in the tray. The
        # toolbar checkbox turns that off, and the tray menu's Quit always
        # really exits.
        self.close_to_tray = tk.BooleanVar(
            value=self.settings.get("close_to_tray", True))
        self.close_to_tray.trace_add("write", lambda *_: self._remember(
            "close_to_tray", self.close_to_tray.get()))
        self._tray = Tray(self, icon_path(), on_show=self.restore_from_tray,
                          on_quit=self.quit_app, on_rescan=self.rescan)

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Animate only while this window is the one being looked at.
        self.bind("<FocusIn>", lambda e: self._set_awake(True))
        self.bind("<FocusOut>", lambda e: self._set_awake(False))
        self.bind("<Map>", lambda e: self._set_awake(True))
        self.bind("<Unmap>", lambda e: self._set_awake(False))
        self.after(50, self._drain)
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self.rescan()

    # ---------------------------------------------------------------- settings

    def _remember(self, key: str, value) -> None:
        if self.settings.get(key) != value:
            self.settings[key] = value
            save_settings(self.settings)

    def _restore_geometry(self) -> str:
        """Reuse the last window size and position, if it is still on screen.

        A saved position from a monitor that is no longer attached would put
        the window somewhere unreachable, so anything off the current desktop
        falls back to a centred default.
        """
        saved = self.settings.get("geometry")
        default = "1180x680"
        if not isinstance(saved, str) or "x" not in saved:
            return default
        try:
            size, _, pos = saved.partition("+")
            w, h = (int(v) for v in size.split("x"))
        except ValueError:
            return default
        if not (600 <= w <= 10000 and 400 <= h <= 10000):
            return default
        if pos:
            try:
                x, y = (int(v) for v in pos.split("+"))
            except ValueError:
                return f"{w}x{h}"
            if not (-50 <= x <= self.winfo_screenwidth() - 200
                    and -50 <= y <= self.winfo_screenheight() - 150):
                return f"{w}x{h}"
        return saved

    def _on_close(self) -> None:
        """The window's X. Either hides to the tray or really quits."""
        if self.close_to_tray.get() and tray_available():
            if self.hide_to_tray():
                return
        self.quit_app()

    def _save_window_state(self) -> None:
        try:
            if self.state() == "normal":
                self._remember("geometry", self.winfo_geometry())
            self._remember("project", self.project)
        except tk.TclError:
            pass

    def hide_to_tray(self) -> bool:
        """Withdraw the window into the notification area."""
        if not self._tray.show():
            self.status.configure(text="tray unavailable")
            return False
        self._save_window_state()
        # Nothing is on screen, so stop burning frames on the animation.
        self.banner.set_enabled(False)
        self.withdraw()
        return True

    def restore_from_tray(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()
        self._tray.hide()
        if self.animated.get():
            self.banner.set_enabled(True)
        self.rescan()

    def quit_app(self) -> None:
        try:
            self._save_window_state()
            self._tray.hide()
            banner = getattr(self, "banner", None)
            if banner is not None:
                banner.stop()   # cancel the pending after() before teardown
        finally:
            self.destroy()

    # ------------------------------------------------------------------ themes

    def apply_theme(self, name: str) -> None:
        """Switch palette and repaint. ttk styles alone are not enough --
        the classic tk widgets (Listbox, Text, Menu) carry their own colours."""
        if name not in THEMES:
            return
        self.theme_name = name
        self.C = THEMES[name]
        self._remember("theme", name)

        self.configure(bg=self.C["bg"])
        self._style()
        self.projects.configure(
            bg=self.C["bg"], fg=self.C["text"],
            selectbackground=self.C["select"], selectforeground=self.C["text"],
            font=(self.C["font"], 9))
        self.detail.configure(bg=self.C["field"], fg=self.C["muted"])
        self.cmd_entry.configure(font=(self._mono(), 9))
        for state in (EndState.LIVE, EndState.CRASHED,
                      EndState.INTERRUPTED, EndState.CLEAN):
            self.tree.tag_configure(state, foreground=self.C[state])
        self.tree.tag_configure("parked", foreground=self.C["parked"])
        self.tree.tag_configure("opening", foreground=self.C["opening"])
        self.banner.set_theme(self.C, self.theme_name)
        # Keep the picker in step when the theme is set from anywhere other
        # than the picker itself.
        box = getattr(self, "theme_box", None)
        if box is not None and box.get() != self.C["label"]:
            box.set(self.C["label"])
        self.refresh_sessions()

    def ask_text(self, title: str, prompt: str, initial: str = "") -> str | None:
        """A themed replacement for simpledialog.askstring.

        tkinter's own dialog is a plain Toplevel with system colours, which is
        a white box in the middle of a dark theme.
        """
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=self.C["panel"], padx=16, pady=14)
        win.resizable(False, False)
        win.transient(self)
        ico = icon_path()
        if ico:
            try:
                win.iconbitmap(str(ico))
            except tk.TclError:
                pass

        result: dict[str, str | None] = {"value": None}

        tk.Label(win, text=prompt, bg=self.C["panel"], fg=self.C["text"],
                 font=(self.C["font"], 10)).pack(anchor="w")
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(win, textvariable=var, width=46,
                          font=(self._mono(), 9))
        entry.pack(fill="x", pady=(8, 12))
        entry.select_range(0, "end")

        def ok(*_):
            result["value"] = var.get()
            win.destroy()

        row = ttk.Frame(win, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(row, text="OK", style="Go.TButton",
                   command=ok).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        win.geometry("+%d+%d" % (
            self.winfo_rootx() + (self.winfo_width() - win.winfo_width()) // 2,
            self.winfo_rooty() + 140))
        entry.focus_set()
        win.grab_set()
        self.wait_window(win)
        return result["value"]

    def _set_awake(self, awake: bool) -> None:
        banner = getattr(self, "banner", None)
        if banner is not None:
            banner.set_awake(awake)

    def _toggle_animation(self) -> None:
        on = self.animated.get()
        self._remember("animated", on)
        banner = getattr(self, "banner", None)
        if banner is not None:
            banner.set_enabled(on)

    def _mono(self) -> str:
        return "Consolas" if self.C["font"] != "Consolas" else "Consolas"

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
        s.configure(".", background=self.C["bg"], foreground=self.C["text"],
                    fieldbackground=self.C["field"], borderwidth=0)
        s.configure("TFrame", background=self.C["bg"])
        s.configure("Panel.TFrame", background=self.C["panel"])
        s.configure("TLabel", background=self.C["bg"], foreground=self.C["text"])
        s.configure("Muted.TLabel", background=self.C["bg"], foreground=self.C["muted"])
        s.configure("Dim.TLabel", background=self.C["panel"], foreground=self.C["dim"])
        s.configure("Head.TLabel", background=self.C["panel"],
                    foreground=self.C["accent"],
                    font=(self.C["font"], 10, "bold"))
        s.configure("TButton", background=self.C["panel"], foreground=self.C["text"],
                    padding=(10, 5), relief="flat")
        s.map("TButton",
              background=[("active", self.C["select"]), ("disabled", self.C["panel"])],
              foreground=[("disabled", self.C["dim"])])
        go_fg = "#ffffff" if luminance(self.C["accent"]) < 0.4 else "#08111c"
        s.configure("Go.TButton", background=self.C["accent"], foreground=go_fg,
                    font=(self.C["font"], 9, "bold"))
        s.map("Go.TButton", background=[("active", self.C["select"])],
              foreground=[("active", self.C["text"])])
        # clam draws its own 3-D edges from these, and they default to near
        # white. Left alone, every entry, combobox and scrollbar keeps a bright
        # border and trough that is glaring on the dark themes.
        edge = {"bordercolor": self.C["line"], "lightcolor": self.C["line"],
                "darkcolor": self.C["line"]}

        s.configure("TEntry", fieldbackground=self.C["field"],
                    foreground=self.C["text"], insertcolor=self.C["text"],
                    padding=6, **edge)
        s.map("TEntry", fieldbackground=[("readonly", self.C["field"])],
              foreground=[("readonly", self.C["text"])],
              bordercolor=[("focus", self.C["accent"])],
              lightcolor=[("focus", self.C["accent"])])

        s.configure("TCheckbutton", background=self.C["bg"],
                    foreground=self.C["muted"],
                    indicatorcolor=self.C["field"], **edge)
        s.map("TCheckbutton",
              background=[("active", self.C["bg"])],
              indicatorcolor=[("selected", self.C["accent"])],
              foreground=[("active", self.C["text"])])

        s.configure("TCombobox", fieldbackground=self.C["field"],
                    foreground=self.C["text"], background=self.C["panel"],
                    arrowcolor=self.C["muted"], padding=4, **edge)
        s.map("TCombobox",
              fieldbackground=[("readonly", self.C["field"]),
                               ("disabled", self.C["panel"])],
              foreground=[("readonly", self.C["text"])],
              selectbackground=[("readonly", self.C["field"])],
              selectforeground=[("readonly", self.C["text"])],
              arrowcolor=[("active", self.C["accent"])])

        # The dropdown itself is a classic Tk Listbox that ttk never touches,
        # so it stays white-on-black unless told otherwise. It is created when
        # first opened, hence the option database rather than a direct config.
        for opt, value in (("background", self.C["field"]),
                           ("foreground", self.C["text"]),
                           ("selectBackground", self.C["select"]),
                           ("selectForeground", self.C["text"])):
            self.option_add(f"*TCombobox*Listbox.{opt}", value)

        # Scrollbars: the trough is the part that reads as a white stripe.
        for orient in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            s.configure(orient, background=self.C["panel"],
                        troughcolor=self.C["bg"], arrowcolor=self.C["muted"],
                        gripcount=0, **edge)
            s.map(orient,
                  background=[("active", self.C["select"]),
                              ("pressed", self.C["accent"])])

        s.configure("TPanedwindow", background=self.C["bg"])
        s.configure("Sash", sashthickness=6, gripcount=0,
                    background=self.C["bg"], bordercolor=self.C["bg"],
                    lightcolor=self.C["line"], darkcolor=self.C["line"])
        s.configure("Treeview", background=self.C["bg"], fieldbackground=self.C["bg"],
                    foreground=self.C["text"], rowheight=26, borderwidth=0,
                    relief="flat", **edge)
        s.configure("Treeview.Heading", background=self.C["panel"],
                    foreground=self.C["accent"],
                    relief="flat", padding=6, **edge)
        s.map("Treeview.Heading", background=[("active", self.C["select"])])
        # Selected-row text must contrast with the selection colour, which on
        # the light theme is pale - white on it was unreadable.
        sel_fg = "#ffffff" if luminance(self.C["select"]) < 0.4 else self.C["text"]
        s.map("Treeview", background=[("selected", self.C["select"])],
              foreground=[("selected", sel_fg)])

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

        theme_box = ttk.Combobox(bar, width=15, state="readonly",
                                 values=[t["label"] for t in THEMES.values()])
        theme_box.set(self.C["label"])
        theme_box.bind("<<ComboboxSelected>>", lambda e: self._pick_theme(theme_box.get()))
        theme_box.pack(side="right", padx=(0, 12))
        self.theme_box = theme_box
        ttk.Label(bar, text="theme", style="Dim.TLabel").pack(side="right", padx=(16, 6))
        ttk.Checkbutton(bar, text="show parked", variable=self.show_parked,
                        command=self.refresh_sessions).pack(side="right", padx=10)
        ttk.Checkbutton(bar, text="animate",
                        variable=self.animated).pack(side="right", padx=(0, 4))
        if tray_available():
            ttk.Checkbutton(bar, text="close to tray",
                            variable=self.close_to_tray).pack(side="right", padx=(0, 4))
            ttk.Button(bar, text="Hide", width=6,
                       command=self.hide_to_tray).pack(side="right", padx=(0, 8))

        self.banner = ThemeBanner(self, self.C, self.theme_name,
                                  enabled=self.animated.get())
        self.banner.pack(fill="x")

        search = ttk.Frame(self, padding=(12, 8, 12, 4))
        search.pack(fill="x")
        # A label rather than placeholder text in the entry: real placeholder
        # text has to live in the same variable the filter reads, and the
        # earlier attempt only recoloured the entry without ever showing a hint.
        ttk.Label(search, text="Search", style="Muted.TLabel").pack(side="left",
                                                                    padx=(0, 8))
        entry = ttk.Entry(search, textvariable=self.search_var)
        entry.pack(side="left", fill="x", expand=True)
        self.search_entry = entry

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12, pady=(4, 0))

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        self.projects = tk.Listbox(
            left, bg=self.C["bg"], fg=self.C["text"], selectbackground=self.C["select"], selectforeground="#fff",
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
        for state in (EndState.LIVE, EndState.CRASHED,
                      EndState.INTERRUPTED, EndState.CLEAN):
            self.tree.tag_configure(state, foreground=self.C[state])
        self.tree.tag_configure("parked", foreground=self.C["parked"])
        self.tree.tag_configure("opening", foreground=self.C["opening"])

        self.detail = tk.Text(self, height=5, bg=self.C["field"], fg=self.C["muted"], bd=0,
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

    def _pick_theme(self, label: str) -> None:
        for key, theme in THEMES.items():
            if theme["label"] == label:
                self.apply_theme(key)
                self.status.configure(text=f"theme: {label}")
                return

    def _set_terminal(self, label: str) -> None:
        value = next(v for l, v in TERMINALS if l == label)
        self.terminal.set(value)
        self._remember("terminal", value)

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

        m = tk.Menu(self, tearoff=0, bg=self.C["panel"], fg=self.C["text"],
                    activebackground=self.C["select"], activeforeground="#ffffff",
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

        rc = tk.Menu(m, tearoff=0, bg=self.C["panel"], fg=self.C["text"],
                     activebackground=self.C["select"], activeforeground="#ffffff", bd=0)
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
                note = self.ask_text(
                    "Park session", "Park as:",
                    park_mod.slugify(meta.title, meta.short_id))
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
        title = self.ask_text("Rename session", "New title:", meta.title)
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
    claim_taskbar_identity()   # must precede the first window
    CCManagerGUI().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

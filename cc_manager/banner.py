"""Animated theme banner.

tkinter widgets are opaque and cannot be composited over a canvas, so a real
full-window background would simply be hidden behind the panels. Instead the
layout gives up a strip along the top and each theme draws its own motif
there: Matrix rain, the Synthwave sun and grid, Frutiger Aero bubbles and
grass, Retro Terminal scanlines.

Canvas items are created once and then moved with coords()/itemconfigure().
Deleting and recreating them every frame is what makes tkinter animation
stutter; reusing a fixed pool keeps this at a few percent of one core.
"""

from __future__ import annotations

import math
import random

import tkinter as tk

# 14fps rather than 20: these are slow motions - drifting bubbles, swaying
# grass, falling rain - and the extra frames cost real CPU for no visible
# gain. Tk repaints the whole strip on any change, so frame rate and total
# item count are the only two levers that matter.
FPS = 14
FRAME_MS = 1000 // FPS
HEIGHT = 74

# Grass blades are updated in this many interleaved groups, one per frame.
GRASS_GROUPS = 3

# Mostly half-width katakana and digits, as in the film. Heavy glyphs like
# @ # $ % & are deliberately absent: they are far denser than everything else,
# so uniform picking made them read as blobs rather than falling code.
MATRIX_GLYPHS = (
    "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ"
    "ｦｧｨｩｪｫｬｭｮｯｰﾞﾟ"
    "0123456789"
    "|:.=*-+<>^"
)


def _mix(a: str, b: str, t: float) -> str:
    """Blend two #rrggbb colours."""
    t = max(0.0, min(1.0, t))
    ah, bh = a.lstrip("#"), b.lstrip("#")
    out = []
    for i in (0, 2, 4):
        ca, cb = int(ah[i:i + 2], 16), int(bh[i:i + 2], 16)
        out.append(round(ca + (cb - ca) * t))
    return "#%02x%02x%02x" % tuple(out)


class ThemeBanner(tk.Canvas):
    """A strip that animates according to the active theme."""

    def __init__(self, master, colours: dict, theme: str, enabled: bool = True):
        super().__init__(master, height=HEIGHT, highlightthickness=0, bd=0,
                         bg=colours["bg"])
        self.C = colours
        self.theme = theme
        self.enabled = enabled
        self._job = None
        self._awake = True
        self._cw = 0
        self._items: list = []
        self._state: list = []
        self._tick = 0
        self.bind("<Configure>", self._on_resize)

    # ------------------------------------------------------------------ public

    def set_theme(self, colours: dict, theme: str) -> None:
        self.C = colours
        self.theme = theme
        self.configure(bg=colours["bg"])
        self._rebuild()

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if on:
            self._rebuild()
            self._schedule()
        else:
            self._cancel()
            self.delete("all")
            self._items, self._state = [], []

    def set_awake(self, awake: bool) -> None:
        """Pause without tearing down, for when the window is not being looked
        at. Animating behind another window is pure waste, and this is by far
        the largest saving available - the strip costs nothing while parked."""
        if awake == self._awake:
            return
        self._awake = awake
        if awake and self.enabled:
            self._schedule()
        elif not awake:
            self._cancel()

    def stop(self) -> None:
        self._cancel()

    # ----------------------------------------------------------------- internal

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _schedule(self) -> None:
        self._cancel()
        if self.enabled and self._awake:
            self._job = self.after(FRAME_MS, self._step)

    def _on_resize(self, event) -> None:
        # Also rebuild when the strip is empty, so a first Configure that
        # arrives before the canvas has a usable width still gets drawn once
        # the real width shows up.
        if abs(event.width - self._cw) < 8 and self._items:
            return
        self._cw = event.width
        self._rebuild()
        self._schedule()

    def _rebuild(self) -> None:
        # Check the width BEFORE clearing. Clearing first meant a narrow
        # Configure event during layout wiped the strip and, because resize
        # only rebuilds on a real width change, it never came back.
        w = self._cw or self.winfo_width()
        if w < 10 or not self.enabled:
            return
        self.delete("all")
        self._items, self._state = [], []
        builder = {
            "matrix": self._build_matrix,
            "synthwave": self._build_synthwave,
            "aero": self._build_aero,
            "retro": self._build_retro,
        }.get(self.theme, self._build_drift)
        builder(w, HEIGHT)

    def _step(self) -> None:
        if not self.winfo_exists():
            return
        self._tick += 1
        try:
            stepper = {
                "matrix": self._step_matrix,
                "synthwave": self._step_synthwave,
                "aero": self._step_aero,
                "retro": self._step_retro,
            }.get(self.theme, self._step_drift)
            stepper(self._cw or self.winfo_width(), HEIGHT)
        except tk.TclError:
            return                      # widget went away mid-frame
        self._schedule()

    # -------------------------------------------------------------- matrix rain

    def _build_matrix(self, w, h):
        # Two canvas items per column, not one per glyph: a bright head and the
        # trail as a single multi-line text item. Tk repaints text expensively,
        # and one item per glyph put this at 80% of a core on its own.
        # Column spacing is the cost dial: every glyph in every trail is text
        # Tk has to lay out and repaint. 15px looked right and cost 32% of a
        # core; 21 is nearly as dense for two thirds less.
        step = 21
        for cx in range(6, w, step):
            trail = random.randint(3, 5)
            head = self.create_text(cx, -20, text=random.choice(MATRIX_GLYPHS),
                                    font=("Consolas", 11), fill=self.C["live"])
            tail = self.create_text(
                cx, -20, anchor="n", justify="center",
                text="\n".join(random.choice(MATRIX_GLYPHS) for _ in range(trail)),
                font=("Consolas", 11),
                fill=_mix(self.C["live"], self.C["bg"], 0.62))
            self._items.append((head, tail))
            # x lives here too: reading it back with coords() cost a Tcl
            # round-trip per item per frame.
            self._state.append([random.uniform(-h, h),
                                random.uniform(1.4, 3.6), trail, float(cx)])

    def _step_matrix(self, w, h):
        morph = self._tick % 4 == 0
        for (head, tail), st in zip(self._items, self._state):
            st[0] += st[1]
            if st[0] - st[2] * 13 > h:
                st[0] = random.uniform(-h * 0.7, -10)
                st[1] = random.uniform(1.4, 3.6)
            x, y = st[3], st[0]
            self.coords(head, x, y)
            self.coords(tail, x, y - st[2] * 13 - 6)
            if morph and random.random() < 0.30:
                self.itemconfigure(head, text=random.choice(MATRIX_GLYPHS))
                self.itemconfigure(tail, text="\n".join(
                    random.choice(MATRIX_GLYPHS) for _ in range(st[2])))

    # ------------------------------------------------------- synthwave sun/grid

    def _build_synthwave(self, w, h):
        horizon = h * 0.62
        cx = w / 2
        r = h * 0.40

        # sun, with the classic horizontal slits cut out of its lower half
        self.create_oval(cx - r, horizon - r, cx + r, horizon + r * 0.55,
                         fill=self.C["accent"], outline="")
        for i in range(5):
            y = horizon - r * 0.34 + i * (r * 0.20)
            self.create_rectangle(cx - r, y, cx + r, y + r * 0.085,
                                  fill=self.C["bg"], outline="")
        self.create_rectangle(0, horizon, w, h, fill=self.C["bg"], outline="")
        self.create_line(0, horizon, w, horizon,
                         fill=_mix(self.C["accent"], self.C["bg"], 0.3), width=2)

        # perspective lines converging on the vanishing point
        for i in range(-9, 10):
            self.create_line(cx + i * (w / 9), h, cx + i * 6, horizon,
                             fill=_mix(self.C["accent"], self.C["bg"], 0.62))

        # horizontal grid lines, scrolling toward the viewer
        self._items = [self.create_line(0, horizon, w, horizon,
                                        fill=_mix(self.C["live"], self.C["bg"], 0.5))
                       for _ in range(7)]
        self._state = [[i / 7.0] for i in range(7)]

        # Chasers: light streaks sweeping the horizon. Evenly phased and all
        # at one speed - random positions and speeds read as stray orbs rather
        # than something chasing. Each is a bright core over a wider glow.
        chasers = []
        for _ in range(3):
            glow = self.create_line(0, horizon, 0, horizon, width=7,
                                    fill=_mix(self.C["live"], self.C["bg"], 0.55),
                                    capstyle="round")
            core = self.create_line(0, horizon, 0, horizon, width=3,
                                    fill=self.C["live"], capstyle="round")
            chasers.append((glow, core))
        self._items.append(chasers)
        self._state.append([[i / 3.0] for i in range(3)])

    def _step_synthwave(self, w, h):
        horizon = h * 0.62
        for line, st in zip(self._items[:-1], self._state[:-1]):
            st[0] = (st[0] + 0.012) % 1.0
            # square the position so spacing compresses toward the horizon
            y = horizon + (h - horizon) * (st[0] ** 2)
            self.coords(line, 0, y, w, y)
            self.itemconfigure(
                line, fill=_mix(self.C["live"], self.C["bg"], 1 - st[0] * 0.85))

        span = w * 0.13
        for (glow, core), st in zip(self._items[-1], self._state[-1]):
            st[0] = (st[0] + 0.007) % 1.0
            # travel across a wider range than the canvas so they enter and
            # leave off-screen instead of popping in at the edge
            x = -span + st[0] * (w + span * 2)
            self.coords(glow, x - span, horizon, x, horizon)
            self.coords(core, x - span * 0.45, horizon, x, horizon)

    # --------------------------------------------------------- aero bubbles

    def _build_aero(self, w, h):
        # Two layers so the verge has depth: a darker, shorter bank behind and
        # a brighter, taller one in front. Both sway, which is most of what
        # sells it as grass rather than a row of ticks.
        self._grass, self._grass_state = [], []
        for layer, (colour_t, lo, hi, step, width) in enumerate((
            (0.55, 12, 26, 10, 2),  # back
            (0.18, 18, 40, 8, 2),   # front
        )):
            blade = _mix(self.C["live"], self.C["bg"], colour_t)
            for x in range(-4, w + 10, step):
                base = x + random.uniform(-1.5, 1.5)
                lean = random.uniform(-9, 9)
                height = random.uniform(lo, hi)
                item = self.create_line(base, h, base, h - height * 0.5,
                                        base + lean, h - height,
                                        smooth=True, width=width, fill=blade)
                self._grass.append(item)
                self._grass_state.append(
                    [base, height, lean, random.uniform(0, math.tau),
                     random.uniform(0.55, 1.5) * (0.6 if layer == 0 else 1.0)])

        for _ in range(14):
            r = random.uniform(3, 11)
            x = random.uniform(0, w)
            y = random.uniform(0, h)
            body = self.create_oval(x - r, y - r, x + r, y + r,
                                    outline=_mix(self.C["accent"], self.C["bg"], 0.35),
                                    fill=_mix(self.C["field"], self.C["accent"], 0.18))
            shine = self.create_oval(0, 0, 0, 0, fill=self.C["field"], outline="")
            self._items.append((body, shine))
            self._state.append([x, y, r, random.uniform(0.25, 0.85),
                                random.uniform(0, math.tau)])

    def _step_aero(self, w, h):
        # Grass sways first, so bubbles draw over it.
        #
        # Only a third of the blades move on any frame. Moving all ~540 every
        # frame meant Tk repainting the whole strip and cost 165% of a core;
        # the sway is slow enough that staggering it is invisible.
        breeze = self._tick * 0.045
        group = self._tick % GRASS_GROUPS
        for idx in range(group, len(self._grass), GRASS_GROUPS):
            blade = self._grass[idx]
            base, height, lean, phase, amp = self._grass_state[idx]
            sway = math.sin(breeze + phase) * amp * 2.4
            self.coords(blade,
                        base, h,
                        base + (lean + sway) * 0.35, h - height * 0.5,
                        base + lean + sway, h - height)

        for (body, shine), st in zip(self._items, self._state):
            st[1] -= st[3]
            st[4] += 0.05
            if st[1] + st[2] < 0:
                st[0] = random.uniform(0, w)
                st[1] = h + st[2]
                st[2] = random.uniform(3, 11)
            x = st[0] + math.sin(st[4]) * 3.5
            y, r = st[1], st[2]
            self.coords(body, x - r, y - r, x + r, y + r)
            sr = r * 0.28
            self.coords(shine, x - r * 0.42 - sr, y - r * 0.42 - sr,
                        x - r * 0.42 + sr, y - r * 0.42 + sr)

    # ------------------------------------------------------- retro scanlines

    def _build_retro(self, w, h):
        # 0.88 toward the background left these effectively invisible against
        # near-black; the strip just looked empty.
        for y in range(0, h, 3):
            self.create_line(0, y, w, y,
                             fill=_mix(self.C["accent"], self.C["bg"], 0.72))
        self._items = [self.create_rectangle(0, 0, w, 12, outline="",
                                             fill=_mix(self.C["accent"],
                                                       self.C["bg"], 0.45))]
        self._state = [[-20.0]]

    def _step_retro(self, w, h):
        st = self._state[0]
        st[0] += 1.6
        if st[0] > h + 20:
            st[0] = -20.0
        self.coords(self._items[0], 0, st[0], w, st[0] + 10)

    # ------------------------------------------------- midnight: quiet drift

    def _build_drift(self, w, h):
        for _ in range(26):
            x, y = random.uniform(0, w), random.uniform(0, h)
            r = random.uniform(0.7, 1.9)
            dot = self.create_oval(x - r, y - r, x + r, y + r,
                                   fill=_mix(self.C["accent"], self.C["bg"],
                                             random.uniform(0.35, 0.75)),
                                   outline="")
            self._items.append(dot)
            self._state.append([x, y, r, random.uniform(0.06, 0.30)])

    def _step_drift(self, w, h):
        for dot, st in zip(self._items, self._state):
            st[0] += st[3]
            if st[0] - st[2] > w:
                st[0] = -st[2]
                st[1] = random.uniform(0, h)
            x, y, r = st[0], st[1], st[2]
            self.coords(dot, x - r, y - r, x + r, y + r)

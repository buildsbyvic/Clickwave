"""On-screen popup for hotkey feedback (click-through, never steals focus).

Shows over borderless/windowed games. Exclusive-fullscreen games draw
over everything, so the optional sound cue covers that case.
"""

import tkinter as tk

from ..winutil import make_overlay, work_area
from . import aa
from .theme import F, S, T, blend
from .widgets import measure


class Toast:
    def __init__(self, root):
        self.root = root
        self.win = None
        self._hide_job = None
        self._fade_job = None

    def _ensure(self):
        if self.win is not None and self.win.winfo_exists():
            return
        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.win.wm_overrideredirect(True)
        self.win.attributes("-topmost", True)
        key = "#010203"
        self.win.configure(bg=key)
        self.win.attributes("-transparentcolor", key)
        self.canvas = tk.Canvas(self.win, bg=key, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.win.update_idletasks()
        make_overlay(self.win)

    def show(self, text, color=None, position="top", duration=1300):
        self._ensure()
        for job in (self._hide_job, self._fade_job):
            if job:
                self.root.after_cancel(job)
        self._hide_job = self._fade_job = None

        font = F["toast"]
        h = S(46)
        w = measure(font, text) + S(64)
        c = self.canvas
        c.configure(width=w, height=h)
        c.delete("all")
        # Edge pixels blend toward the (transparent) key color, which reads
        # as a soft dark rim - fine over games.
        aa.rrect(c, 0, 0, w, h, h / 2, T.card, blend(T.card, color or T.accent, 0.55), S(1.5), bg="#010203")
        cx, cy = S(26), h / 2
        aa.circle(c, cx, cy, S(5), color or T.accent, bg=T.card)
        c.create_text(cx + S(16), cy, text=text, anchor="w", fill=T.text, font=font)

        left, top, right, bottom = work_area()
        margin = S(36)
        if position == "bottom":
            x, y = (left + right - w) // 2, bottom - h - margin
        elif position == "top-right":
            x, y = right - w - margin, top + margin
        elif position == "bottom-right":
            x, y = right - w - margin, bottom - h - margin
        else:
            x, y = (left + right - w) // 2, top + margin
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.attributes("-alpha", 0.97)
        self.win.deiconify()
        self.win.lift()
        self._hide_job = self.root.after(duration, self._fade)

    def _fade(self, alpha=0.97):
        alpha -= 0.12
        if alpha <= 0 or self.win is None:
            if self.win is not None:
                self.win.withdraw()
            self._fade_job = None
            return
        self.win.attributes("-alpha", alpha)
        self._fade_job = self.root.after(22, lambda: self._fade(alpha))

    def destroy(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None

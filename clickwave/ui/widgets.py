"""Custom themed Tk widgets, canvas-drawn with antialiased shapes."""

import collections
import tkinter as tk
import tkinter.font as tkfont

from . import aa
from .theme import ACCENTS, F, I, S, T, blend, scale

_font_cache = {}


def measure(font, text):
    f = _font_cache.get(font)
    if f is None:
        f = _font_cache[font] = tkfont.Font(font=font)
    return f.measure(text)


def ellipsize(font, text, width):
    if measure(font, text) <= width:
        return text
    while text and measure(font, text + "…") > width:
        text = text[:-1]
    return text + "…"


def label(parent, text, font="body", fg=None, **kw):
    return tk.Label(parent, text=text, font=F[font], bg=parent["bg"], fg=fg or T.text, **kw)


def link(parent, text, command, font="small", fg=None):
    base = fg or T.muted
    lbl = label(parent, text, font=font, fg=base, cursor="hand2")
    lbl.bind("<Enter>", lambda e: lbl.config(fg=T.accent_hover))
    lbl.bind("<Leave>", lambda e: lbl.config(fg=base))
    lbl.bind("<ButtonRelease-1>", lambda e: command())
    return lbl


def separator(parent):
    return tk.Frame(parent, height=1, bg=T.border)


def is_descendant(widget, ancestor):
    while widget is not None:
        if widget is ancestor:
            return True
        widget = getattr(widget, "master", None)
    return False


def round_window(window):
    """Native rounded corners + shadow on Windows 11 popups."""
    try:
        import ctypes
        hwnd = ctypes.c_void_p(int(window.wm_frame(), 16))
        pref = ctypes.c_int(3)  # DWMWCP_ROUNDSMALL
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


class _Canvas(tk.Canvas):
    def __init__(self, parent, **kw):
        kw.setdefault("bg", parent["bg"])
        super().__init__(parent, highlightthickness=0, bd=0, **kw)

    def size(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            w, h = int(self["width"]), int(self["height"])
        return w, h


# ============================================================
# Containers
# ============================================================

class Card(_Canvas):
    """A rounded panel. Put widgets in `.inner`."""

    def __init__(self, parent, pad=14, radius=12):
        super().__init__(parent, width=S(100), height=S(40))
        self.pad, self.radius = S(pad), S(radius)
        self.inner = tk.Frame(self, bg=T.card)
        self._win = self.create_window(self.pad, self.pad, window=self.inner, anchor="nw")
        self._size = (0, 0)
        self.inner.bind("<Configure>", self._relayout, add="+")
        self.bind("<Configure>", self._relayout, add="+")

    def _relayout(self, _=None):
        w = self.winfo_width()
        if w < 4:
            return
        self.itemconfigure(self._win, width=max(1, w - 2 * self.pad))
        h = self.inner.winfo_reqheight() + 2 * self.pad
        if int(self["height"]) != h:
            self.configure(height=h)
        if self._size != (w, h):
            self._size = (w, h)
            self.delete("bg")
            aa.rrect(self, 0, 0, w, h, self.radius, T.card, T.border, 1, tags="bg")
            self.tag_lower("bg")


class ScrollPage(tk.Frame):
    """Vertically scrollable page with a slim auto-hiding scrollbar."""

    def __init__(self, parent):
        super().__init__(parent, bg=T.bg)
        self.canvas = tk.Canvas(self, bg=T.bg, highlightthickness=0, bd=0, yscrollincrement=S(24))
        self.bar = _Canvas(self, width=S(10), bg=T.bg)
        self.inner = tk.Frame(self.canvas, bg=T.bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self._update())
        self.canvas.bind("<Configure>", self._on_canvas)
        self.bar.bind("<ButtonPress-1>", self._bar_press)
        self.bar.bind("<B1-Motion>", self._bar_drag)
        self.bar.bind("<ButtonRelease-1>", lambda e: self._end_drag())
        self.bar.bind("<Enter>", lambda e: self._set_hover(True))
        self.bar.bind("<Leave>", lambda e: self._set_hover(False))
        self._drag = None
        self._hover = False

    def _set_hover(self, h):
        self._hover = h
        self._draw_bar()

    def _end_drag(self):
        self._drag = None
        self._draw_bar()

    def _on_canvas(self, e):
        self.canvas.itemconfigure(self._win, width=e.width)
        self._update()

    def _update(self):
        h = max(self.inner.winfo_reqheight(), 1)
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), h))
        self._draw_bar()

    def overflow(self):
        return self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1

    def scroll(self, delta):
        if self.overflow():
            self.canvas.yview_scroll(int(-delta / 120 * 2), "units")
            self._draw_bar()

    def scroll_to(self, fraction):
        self.canvas.yview_moveto(fraction)
        self._draw_bar()

    def _draw_bar(self):
        self.bar.delete("all")
        if not self.overflow():
            return
        top, bottom = self.canvas.yview()
        h, w = self.bar.winfo_height(), self.bar.winfo_width()
        active = self._hover or self._drag
        bw = S(6) if active else S(4)
        x1 = (w - bw) // 2
        y1, y2 = int(top * h) + S(4), int(bottom * h) - S(4)
        if y2 - y1 > bw:
            aa.rrect(self.bar, x1, y1, x1 + bw, y2, bw / 2, T.tick_strong if active else T.tick)

    def _bar_press(self, e):
        top, bottom = self.canvas.yview()
        h = max(1, self.bar.winfo_height())
        if not (top * h <= e.y <= bottom * h):
            self.canvas.yview_moveto(e.y / h - (bottom - top) / 2)
        self._drag = (e.y, self.canvas.yview()[0])
        self._draw_bar()

    def _bar_drag(self, e):
        if self._drag:
            y0, top0 = self._drag
            self.canvas.yview_moveto(top0 + (e.y - y0) / max(1, self.bar.winfo_height()))
            self._draw_bar()


# ============================================================
# Controls
# ============================================================

class RoundedButton(_Canvas):
    """variant: default | primary | success | danger | ghost"""

    def __init__(self, parent, text="", command=None, height=34, width=None, radius=9,
                 font=None, fill=None, hover=None, press=None, fg=None, icon=None, variant="default"):
        self._font = font or F["semi"]
        self._text = text
        self._icon = icon
        if width is None:
            width = self._content_width() / scale() + (26 if text else 18)
        super().__init__(parent, height=S(height), width=S(width), cursor="hand2")
        self.command = command
        self._radius = S(radius)
        self.variant = variant
        self._custom = (fill, hover, press, fg)
        self._hover = self._down = False
        self.enabled = True
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, down=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(down=True))
        self.bind("<ButtonRelease-1>", self._release)
        self._redraw()

    def _content_width(self):
        w = measure(self._font, self._text) if self._text else 0
        if self._icon:
            w += measure(F["icon"], self._icon) + (S(8) if self._text else 0)
        return w

    def _palette(self):
        fill, hover, press, fg = self._custom
        if self.variant == "primary":
            base, text, outline = T.accent, T.accent_text, None
        elif self.variant == "success":
            base, text, outline = T.green, "#ffffff", None
        elif self.variant == "danger":
            base, text, outline = T.red, "#ffffff", None
        elif self.variant == "ghost":
            base, text, outline = self["bg"], T.muted, None
        else:
            base, text, outline = T.raised, T.text, T.outline
        base = fill or base
        hov = hover or (T.raised_hover if self.variant in ("default", "ghost") else blend(base, "#ffffff", 0.09))
        prs = press or blend(base, "#000000", 0.14)
        return base, hov, prs, fg or text, outline

    def style(self, text=None, fill=None, hover=None, press=None, fg=None, variant=None, icon=None):
        if text is not None:
            self._text = text
        if icon is not None:
            self._icon = icon
        if variant is not None:
            self.variant = variant
            self._custom = (None, None, None, None)
        if any(v is not None for v in (fill, hover, press, fg)):
            self._custom = (fill, hover, press, fg)
        self._redraw()

    def set_enabled(self, enabled):
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._redraw()

    def _set(self, hover=None, down=None):
        if hover is not None:
            self._hover = hover
        if down is not None:
            self._down = down
        self._redraw()

    def _release(self, e):
        fire = self._down and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        self._set(down=False)
        if fire and self.command and self.enabled:
            self.command()

    def _redraw(self):
        self.delete("all")
        w, h = self.size()
        base, hov, prs, fg, outline = self._palette()
        if not self.enabled:
            color, fg, outline = T.raised, T.faint, T.outline
        else:
            color = prs if self._down else (hov if self._hover else base)
            if outline and self._hover:
                outline = T.outline_strong
        if self.variant != "ghost" or self._hover:
            aa.rrect(self, 0, 0, w, h, self._radius, color, outline)
        cw = self._content_width()
        x = (w - cw) / 2
        if self._icon:
            iw = measure(F["icon"], self._icon)
            self.create_text(x + iw / 2, h / 2 + 1, text=self._icon, fill=fg, font=F["icon"])
            x += iw + (S(8) if self._text else 0)
        if self._text:
            self.create_text(x, h / 2, text=self._text, fill=fg, font=self._font, anchor="w")


def primary_button(parent, text, command, **kw):
    return RoundedButton(parent, text, command, variant="primary", **kw)


class ToggleSwitch(_Canvas):
    def __init__(self, parent, value=False, command=None):
        self.w, self.h = S(40), S(22)
        super().__init__(parent, width=self.w, height=self.h, cursor="hand2")
        self.value = bool(value)
        self.command = command
        self._pos = 1.0 if self.value else 0.0
        self._anim = None
        self._hover = False
        self.bind("<ButtonRelease-1>", lambda e: self.toggle())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._redraw()

    def _set_hover(self, h):
        self._hover = h
        self._redraw()

    def toggle(self):
        self.set(not self.value)
        if self.command:
            self.command(self.value)

    def set(self, value):
        self.value = bool(value)
        self._animate()

    def _animate(self):
        target = 1.0 if self.value else 0.0
        if self._anim:
            self.after_cancel(self._anim)
            self._anim = None
        step = 0.25 if target > self._pos else -0.25
        self._pos = max(0.0, min(1.0, self._pos + step))
        if abs(self._pos - target) < 1e-6:
            self._pos = target
        self._redraw()
        if self._pos != target:
            self._anim = self.after(14, self._animate)

    def _redraw(self):
        self.delete("all")
        w, h, p = self.w, self.h, self._pos
        off_fill = T.raised_hover if self._hover else T.track
        on_fill = T.accent_hover if self._hover else T.accent
        fill = blend(off_fill, on_fill, p)
        off_line = T.outline_strong if not self._hover else T.tick_strong
        on_line = blend(T.accent, "#000000", 0.18)
        aa.rrect(self, 0, 0, w, h, h / 2, fill, blend(off_line, on_line, p))
        m = S(4)
        r = h / 2 - m
        cx = m + r + (w - 2 * m - 2 * r) * p
        knob = blend(T.knob_off, T.knob, p)
        aa.circle(self, cx, h / 2, r, knob, None if T.dark else T.outline_strong, 1, bg=fill)


class Segmented(_Canvas):
    """A pill-shaped tab switcher."""

    def __init__(self, parent, options, command, index=0):
        super().__init__(parent, height=S(36), width=S(100), cursor="hand2")
        self.options = options
        self.command = command
        self.index = index
        self._hover = -1
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Motion>", lambda e: self._set_hover(self._seg_at(e.x)))
        self.bind("<Leave>", lambda e: self._set_hover(-1))
        self.bind("<ButtonRelease-1>", lambda e: self.select(self._seg_at(e.x)))

    def _seg_at(self, x):
        w = max(1, self.winfo_width())
        return max(0, min(len(self.options) - 1, int(x / (w / len(self.options)))))

    def _set_hover(self, i):
        if i != self._hover:
            self._hover = i
            self._redraw()

    def select(self, i, notify=True):
        if i != self.index:
            self.index = i
            self._redraw()
            if notify:
                self.command(i)

    def _redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        aa.rrect(self, 0, 0, w, h, S(11), T.card, T.border)
        seg = (w - 2 * S(4)) / len(self.options)
        m = S(4)
        for i, text in enumerate(self.options):
            x1, x2 = m + i * seg, m + (i + 1) * seg
            if i == self.index:
                aa.rrect(self, x1, m, x2, h - m, S(8), T.raised_hover, T.outline, bg=T.card)
            elif i == self._hover:
                aa.rrect(self, x1, m, x2, h - m, S(8), blend(T.card, T.raised_hover, 0.5), bg=T.card)
            fg = T.text if (i == self.index or i == self._hover) else T.muted
            self.create_text((x1 + x2) / 2, h / 2, text=text, fill=fg, font=F["semi"])


class StatusPill(_Canvas):
    def __init__(self, parent):
        super().__init__(parent, height=S(28), width=S(110))
        self.state = None
        self.set("stopped")

    def set(self, state):
        """state: 'listening' | 'stopped' | 'paused' (app filter mismatch)."""
        self.state = state
        self.delete("all")
        text, color = {
            "listening": ("Listening", T.ok),
            "paused": ("Waiting for app", T.warn),
        }.get(state, ("Stopped", T.faint))
        font = F["small_semi"]
        w = measure(font, text) + S(34)
        h = int(self["height"])
        self.configure(width=w)
        fill = blend(T.bg, color, 0.13)
        aa.rrect(self, 0, 0, w, h, h / 2, fill, blend(T.bg, color, 0.38))
        cx, cy = S(14), h / 2
        aa.circle(self, cx, cy, S(3.5), color, bg=fill)
        self.create_text(cx + S(10), cy, text=text, anchor="w",
                         fill=T.text if state != "stopped" else T.muted, font=font)


class MarkerSlider(_Canvas):
    """Slider with labelled, low-contrast markers it snaps to.

    If `center` is given, that value sits exactly in the middle of the
    track (each half scaled independently). `suffix` is appended to the
    marker labels' tooltip-free readout by the owner, not drawn here."""

    def __init__(self, parent, markers, vmin, vmax, value, center=None, default=None,
                 command=None, on_release=None, snap=True, compact=False):
        self.compact = compact
        super().__init__(parent, height=S(42 if compact else 50), width=S(200), takefocus=1, cursor="hand2")
        self.markers = tuple(markers)
        self.vmin, self.vmax, self.center = vmin, vmax, center
        self.default = default
        self.value = int(value)
        self.command = command
        self.on_release = on_release
        self.snap = snap
        self._hover = self._drag = self._focus = False
        self.pad = S(14)
        self.track_y = S(13)
        self.r = S(7)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Enter>", lambda e: self._set_flag("_hover", True))
        self.bind("<Leave>", lambda e: self._set_flag("_hover", False))
        self.bind("<FocusIn>", lambda e: self._set_flag("_focus", True))
        self.bind("<FocusOut>", lambda e: self._set_flag("_focus", False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", self._double)
        self.bind("<Left>", lambda e: self._nudge(-1))
        self.bind("<Right>", lambda e: self._nudge(1))
        self.bind("<Control-Left>", lambda e: self.step_marker(-1))
        self.bind("<Control-Right>", lambda e: self.step_marker(1))

    def _x0(self):
        return self.pad

    def _x1(self):
        return max(self.pad + 1, self.winfo_width() - self.pad)

    def _frac(self, v):
        if self.center is not None:
            if v <= self.center:
                return 0.5 * (v - self.vmin) / (self.center - self.vmin)
            return 0.5 + 0.5 * (v - self.center) / (self.vmax - self.center)
        return (v - self.vmin) / (self.vmax - self.vmin)

    def _unfrac(self, f):
        f = max(0.0, min(1.0, f))
        if self.center is not None:
            if f <= 0.5:
                return self.vmin + (f / 0.5) * (self.center - self.vmin)
            return self.center + ((f - 0.5) / 0.5) * (self.vmax - self.center)
        return self.vmin + f * (self.vmax - self.vmin)

    def v2x(self, v):
        return self._x0() + self._frac(v) * (self._x1() - self._x0())

    def _value_at(self, x, fine=False):
        if self.snap and not fine:
            for m in self.markers:
                if abs(self.v2x(m) - x) <= S(8):
                    return m
        return int(round(self._unfrac((x - self._x0()) / (self._x1() - self._x0()))))

    def set(self, v, notify=False):
        v = max(self.vmin, min(self.vmax, int(v)))
        if v == self.value:
            return
        self.value = v
        self.redraw()
        if notify and self.command:
            self.command(v)

    def _set_flag(self, name, val):
        setattr(self, name, val)
        self.redraw()

    def _finish(self):
        if self.on_release:
            self.on_release(self.value)

    @staticmethod
    def _fine(e):
        return bool(e.state & 0x0001)  # Shift held

    def _press(self, e):
        self.focus_set()
        if e.y >= self.track_y + S(16):
            m = min(self.markers, key=lambda m: abs(self.v2x(m) - e.x))
            if abs(self.v2x(m) - e.x) <= S(18):
                self.set(m, notify=True)
                self._finish()
                return
        self._drag = True
        if abs(e.x - self.v2x(self.value)) > self.r + S(3):
            self.set(self._value_at(e.x, self._fine(e)), notify=True)
        self.redraw()

    def _motion(self, e):
        if self._drag:
            self.set(self._value_at(e.x, self._fine(e)), notify=True)

    def _release(self, e):
        if self._drag:
            self._drag = False
            self.redraw()
            self._finish()

    def _double(self, e):
        if self.default is not None and abs(e.x - self.v2x(self.value)) <= self.r + S(4):
            self.set(self.default, notify=True)
            self._finish()

    def _nudge(self, d):
        self.set(self.value + d, notify=True)
        self._finish()

    def step_marker(self, d):
        if d > 0:
            nxt = [m for m in self.markers if m > self.value]
            target = nxt[0] if nxt else self.vmax
        else:
            prv = [m for m in self.markers if m < self.value]
            target = prv[-1] if prv else self.vmin
        self.set(target, notify=True)
        self._finish()

    def wheel(self, e):
        d = 1 if e.delta > 0 else -1
        if self._fine(e):
            self._nudge(d)
        else:
            self.step_marker(d)

    def redraw(self):
        self.delete("all")
        if self.winfo_width() < 4:
            return
        y = self.track_y
        x0, x1 = int(self._x0()), int(self._x1())
        xv = self.v2x(self.value)
        th = S(4)
        bg = self["bg"]
        aa.rrect(self, x0 - th / 2, y - th / 2, x1 + th / 2, y + th / 2, th / 2, T.track)
        if xv > x0 + 1:
            aa.rrect(self, x0 - th / 2, y - th / 2, xv, y + th / 2, th / 2, T.accent)
        for m in self.markers:
            x = int(round(self.v2x(m)))
            active = m == self.value
            strong = m == self.default
            color = T.accent if active else (T.tick_strong if strong else T.tick)
            tw = S(2) if (active or strong) else 1
            self.create_rectangle(x - tw // 2, y + S(9), x - tw // 2 + tw, y + S(14), fill=color, outline="")
            self.create_text(x, y + S(24), text=str(m),
                             fill=T.text if active else (T.muted if strong else T.faint), font=F["tiny"])
        r = self.r + (S(1.5) if (self._hover or self._drag) else 0)
        if self._drag or self._focus:
            aa.circle(self, xv, y, r + S(5), blend(bg, T.accent, 0.22))
            halo = blend(bg, T.accent, 0.22)
        else:
            halo = bg
        aa.circle(self, xv, y, r, T.knob, T.accent, S(2.5), bg=halo)


class Chip(_Canvas):
    """A rounded tag. style: "normal" | "add" | "listening".
    `active` lights it up (live key feedback); `marked` adds a dot."""

    def __init__(self, parent, text, on_remove=None, on_click=None, on_menu=None,
                 style="normal", marked=False, font=None, icon=None):
        self._font = font or F["small"]
        self._text = text
        self._icon = icon
        self.h = S(30)
        self._x_w = S(22) if on_remove else 0
        self._dot_w = S(10) if marked else 0
        self._icon_w = (measure(F["icon_small"], icon) + S(6)) if icon else 0
        w = measure(self._font, text) + S(24) + self._x_w + self._dot_w + self._icon_w
        super().__init__(parent, width=w, height=self.h, cursor="hand2")
        self.w = w
        self.on_remove, self.on_click, self.on_menu = on_remove, on_click, on_menu
        self.chip_style = style
        self.marked = marked
        self._hover = self._hover_x = False
        self.active = 0.0
        self._fade_job = None
        self.pulse = 0.0
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set(False, False))
        self.bind("<ButtonRelease-1>", self._release)
        if on_menu:
            self.bind("<ButtonRelease-3>", lambda e: on_menu(self))
        self.redraw()

    def _in_x(self, x):
        return self.on_remove is not None and x >= self.w - self._x_w - S(4)

    def _motion(self, e):
        self._set(True, self._in_x(e.x))

    def _set(self, hover, hover_x):
        if (hover, hover_x) != (self._hover, self._hover_x):
            self._hover, self._hover_x = hover, hover_x
            self.redraw()

    def _release(self, e):
        if not (0 <= e.x <= self.w and 0 <= e.y <= self.h):
            return
        if self._in_x(e.x):
            self.on_remove()
        elif self.on_click:
            self.on_click()
        elif self.on_menu:
            self.on_menu(self)

    def set_active(self, pressed):
        if self._fade_job:
            self.after_cancel(self._fade_job)
            self._fade_job = None
        if pressed:
            self.active = 1.0
            self.redraw()
        else:
            self._fade()

    def _fade(self):
        self.active = max(0.0, self.active - 0.2)
        self.redraw()
        if self.active > 0:
            self._fade_job = self.after(25, self._fade)

    def redraw(self):
        self.delete("all")
        w, h = self.w, self.h
        if self.chip_style == "add":
            fill = blend(T.card, T.accent, 0.12 if self._hover else 0.06)
            outline = T.accent if self._hover else T.accent_dim
            fg = T.accent if not T.dark else T.accent_hover
        elif self.chip_style == "listening":
            fill = blend(T.card, T.accent, 0.16 + 0.12 * self.pulse)
            outline = blend(T.accent_dim, T.accent, self.pulse)
            fg = T.text
        else:
            base = T.raised_hover if self._hover else T.raised
            fill = blend(base, T.accent, 0.88 * self.active)
            outline = blend(T.outline_strong if self._hover else T.outline, T.accent, self.active)
            fg = blend(T.text, T.accent_text, self.active)
        aa.rrect(self, 0, 0, w, h, S(8), fill, outline)
        x = S(12)
        if self._icon:
            iw = self._icon_w - S(6)
            self.create_text(x + iw / 2, h / 2 + 1, text=self._icon, fill=fg, font=F["icon_small"])
            x += self._icon_w
        tw = measure(self._font, self._text)
        self.create_text(x, h / 2, text=self._text, fill=fg, font=self._font, anchor="w")
        x += tw
        if self.marked:
            aa.circle(self, x + S(7), h / 2, S(2.5), T.accent if self.active < 0.5 else fg, bg=fill)
        if self.on_remove:
            xc = T.red_hover if self._hover_x else (T.muted if self._hover else T.faint)
            if self.active > 0.5:
                xc = fg
            self.create_text(w - S(14), h / 2 + 1, text=I.close, fill=xc, font=F["icon_small"])


class ChipFlow(tk.Frame):
    """Lays out child widgets left-to-right, wrapping onto new rows."""

    def __init__(self, parent, gap=6):
        super().__init__(parent, bg=parent["bg"], height=S(30))
        self.gap = S(gap)
        self._last_width = 0
        self.bind("<Configure>", self._on_configure)

    def clear(self):
        for child in self.winfo_children():
            child.destroy()

    def _on_configure(self, e):
        if e.width != self._last_width:
            self._last_width = e.width
            self.relayout()

    def relayout(self):
        avail = self.winfo_width()
        if avail <= 1:
            avail = S(330)
        x = y = row_h = 0
        for child in self.winfo_children():
            w, h = child.winfo_reqwidth(), child.winfo_reqheight()
            if x > 0 and x + w > avail:
                x = 0
                y += row_h + self.gap
                row_h = 0
            child.place(x=x, y=y)
            x += w + self.gap
            row_h = max(row_h, h)
        height = max(1, y + row_h)
        if int(self["height"]) != height:
            self.configure(height=height)


class Sparkline(_Canvas):
    """Tiny filled line chart of recent CPS."""

    def __init__(self, parent, points=60, height=36):
        super().__init__(parent, height=S(height), width=S(100))
        self.data = collections.deque([0] * points, maxlen=points)
        self.bind("<Configure>", lambda e: self.redraw())

    def push(self, value):
        self.data.append(value)
        self.redraw()

    def clear(self):
        for i in range(len(self.data)):
            self.data[i] = 0
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        top = max(10, max(self.data))
        n = len(self.data)
        label_band = S(12)
        pad = S(2)
        pts = []
        for i, v in enumerate(self.data):
            x = i * (w - 1) / (n - 1)
            y = h - pad - (h - label_band - pad) * (v / top)
            pts.extend((x, y))
        self.create_rectangle(0, h - 1, w, h, fill=T.border, outline="")
        if max(self.data) > 0:
            self.create_polygon([0, h] + pts + [w, h], fill=blend(self["bg"], T.accent, 0.16), outline="")
            self.create_line(pts, fill=T.accent, width=S(1.6), smooth=True)
        self.create_text(w - S(2), S(1), text=f"peak {max(self.data)}" if max(self.data) else "last 30 s",
                         anchor="ne", fill=T.faint, font=F["tiny"])


class Swatches(_Canvas):
    """Row of accent color circles."""

    def __init__(self, parent, value, command):
        self.size = S(22)
        self.gap = S(10)
        n = len(ACCENTS)
        super().__init__(parent, height=self.size + S(8), width=n * (self.size + self.gap) + S(4), cursor="hand2")
        self.value = value
        self.command = command
        self.bind("<ButtonRelease-1>", self._click)
        self.redraw()

    def _click(self, e):
        i = int((e.x - S(4)) // (self.size + self.gap))
        names = list(ACCENTS)
        if 0 <= i < len(names):
            self.value = names[i]
            self.redraw()
            self.command(self.value)

    def redraw(self):
        self.delete("all")
        r = self.size / 2
        for i, (name, color) in enumerate(ACCENTS.items()):
            cx = S(4) + i * (self.size + self.gap) + r
            cy = S(4) + r
            if name == self.value:
                aa.circle(self, cx, cy, r + S(3), self["bg"], color, S(2))
                aa.circle(self, cx, cy, r - S(1), color)
            else:
                aa.circle(self, cx, cy, r - S(1), color)


class Radio(_Canvas):
    def __init__(self, parent, selected):
        super().__init__(parent, width=S(18), height=S(18))
        c = S(9)
        if selected:
            aa.circle(self, c, c, S(8), self["bg"], T.accent, S(2))
            aa.circle(self, c, c, S(3.5), T.accent)
        else:
            aa.circle(self, c, c, S(8), self["bg"], T.tick_strong, S(1.5))


# ============================================================
# Popups
# ============================================================

class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _=None):
        self._cancel()
        self._job = self.widget.after(500, self._show)

    def _cancel(self):
        if self._job:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self):
        text = self.text() if callable(self.text) else self.text
        if self._tip or not text or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_pointerx() + S(12)
        y = self.widget.winfo_pointery() + S(16)
        tip = tk.Toplevel(self.widget)
        tip.withdraw()
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=T.raised)
        tk.Label(tip, text=text, bg=T.raised, fg=T.text, font=F["small"],
                 padx=S(10), pady=S(6), justify="left").pack()
        tip.wm_geometry(f"+{x}+{y}")
        tip.deiconify()
        round_window(tip)
        self._tip = tip

    def _hide(self, _=None):
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


class PopupMenu:
    """A themed dropdown/context menu.

    items: dicts with keys
        label, command, checked (bool), enabled (bool), detail (str),
        header (True -> section title), separator (True), color (swatch)
    """
    _current = None

    def __init__(self, root, items, x, y, min_width=None, above_y=None):
        if PopupMenu._current is not None:
            PopupMenu._current.close()
        PopupMenu._current = self
        self.root = root
        self.top = top = tk.Toplevel(root)
        top.withdraw()
        top.wm_overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=T.card)
        body = tk.Frame(top, bg=T.card, padx=S(4), pady=S(4))
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=T.card, highlightthickness=0, bd=0)
        self.rows = tk.Frame(self.canvas, bg=T.card)
        self.canvas.create_window(0, 0, window=self.rows, anchor="nw")
        self.canvas.pack(fill="both", expand=True)
        for item in items:
            self._add(item)
        top.update_idletasks()
        req_w = max(self.rows.winfo_reqwidth(), (min_width or 0) - S(8))
        req_h = self.rows.winfo_reqheight()
        view_h = min(req_h, S(400))
        self.canvas.configure(width=req_w, height=view_h, scrollregion=(0, 0, req_w, req_h))
        for row in self.rows.winfo_children():
            row.configure(width=req_w)
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        if y + h > sh - S(40) and above_y is not None:
            y = above_y - h
        x = min(max(0, x), sw - w - S(4)) if sw > w else x
        top.geometry(f"+{int(x)}+{int(max(0, y))}")
        top.deiconify()
        round_window(top)
        top.lift()
        top.focus_force()
        top.grab_set()
        top.bind("<ButtonPress>", self._outside_click)
        top.bind("<Escape>", lambda e: self.close())
        top.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120 * 2), "units"))
        top.bind("<FocusOut>", lambda e: top.after(50, self._check_focus))

    def _check_focus(self):
        try:
            if self.top.winfo_exists() and self.top.focus_get() is None:
                self.close()
        except (tk.TclError, KeyError):
            self.close()

    def _outside_click(self, e):
        x, y = e.x_root, e.y_root
        t = self.top
        if not (t.winfo_rootx() <= x < t.winfo_rootx() + t.winfo_width()
                and t.winfo_rooty() <= y < t.winfo_rooty() + t.winfo_height()):
            self.close()

    def _add(self, item):
        if item.get("separator"):
            tk.Frame(self.rows, height=1, bg=T.border).pack(fill="x", pady=S(4), padx=S(6))
            return
        if item.get("header"):
            tk.Label(self.rows, text=item["label"].upper(), bg=T.card, fg=T.faint, font=F["tiny"],
                     anchor="w", padx=S(10), pady=S(2)).pack(fill="x", pady=(S(4), 0))
            return
        enabled = item.get("enabled", True)
        row = tk.Frame(self.rows, bg=T.card, cursor="hand2" if enabled else "arrow", height=S(30))
        row.pack(fill="x")
        row.pack_propagate(False)
        check = tk.Label(row, text=I.check if item.get("checked") else "", width=2, bg=T.card,
                         fg=T.accent, font=F["icon_small"])
        check.pack(side="left", padx=(S(6), S(2)))
        widgets = [row, check]
        if item.get("color"):
            sw = _Canvas(row, width=S(12), height=S(12), bg=T.card)
            aa.circle(sw, S(6), S(6), S(5), item["color"])
            sw.pack(side="left", padx=(0, S(6)))
            widgets.append(sw)
        text = tk.Label(row, text=item["label"], bg=T.card, fg=T.text if enabled else T.faint,
                        font=F["small"], anchor="w")
        text.pack(side="left", padx=(0, S(18)))
        widgets.append(text)
        if item.get("detail"):
            det = tk.Label(row, text=item["detail"], bg=T.card, fg=T.faint, font=F["tiny"])
            det.pack(side="right", padx=(S(12), S(10)))
            widgets.append(det)
        row.update_idletasks()
        row.configure(width=sum(wd.winfo_reqwidth() for wd in widgets[1:]) + S(44))
        if not enabled:
            return

        def hover(on):
            color = T.raised_hover if on else T.card
            for wd in widgets:
                wd.configure(bg=color)

        def run(_=None):
            self.close()
            cmd = item.get("command")
            if cmd:
                self.root.after(1, cmd)

        for wd in widgets:
            wd.bind("<Enter>", lambda e: hover(True))
            wd.bind("<Leave>", lambda e: hover(False))
            wd.bind("<ButtonRelease-1>", run)

    def close(self):
        if PopupMenu._current is self:
            PopupMenu._current = None
        try:
            self.top.grab_release()
            self.top.destroy()
        except tk.TclError:
            pass


def popup_at_widget(widget, items, align="left", width=None):
    x = widget.winfo_rootx()
    if align == "right":
        x = widget.winfo_rootx() + widget.winfo_width() - (width or S(200))
    y = widget.winfo_rooty() + widget.winfo_height() + S(4)
    return PopupMenu(widget.winfo_toplevel(), items, x, y, min_width=width, above_y=widget.winfo_rooty() - S(4))


class Dropdown(_Canvas):
    """Select-one control: options are (value, label[, detail])."""

    def __init__(self, parent, options, value, command, height=34):
        super().__init__(parent, height=S(height), width=S(160), cursor="hand2")
        self.options = list(options)
        self.value = value
        self.command = command
        self._hover = False
        self.placeholder = ""
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", lambda e: self.open())

    def _set_hover(self, h):
        self._hover = h
        self.redraw()

    def set(self, value, options=None):
        if options is not None:
            self.options = list(options)
        self.value = value
        self.redraw()

    def current_label(self):
        for opt in self.options:
            if opt[0] == self.value:
                return opt[1]
        return self.placeholder or (str(self.value) if self.value is not None else "")

    def open(self):
        items = []
        for opt in self.options:
            items.append({"label": opt[1], "checked": opt[0] == self.value,
                          "detail": opt[2] if len(opt) > 2 else None,
                          "command": (lambda v=opt[0]: self._pick(v))})
        popup_at_widget(self, items, width=self.winfo_width())

    def _pick(self, value):
        self.value = value
        self.redraw()
        self.command(value)

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        fill = T.raised_hover if self._hover else T.raised
        aa.rrect(self, 0, 0, w, h, S(8), fill, T.outline_strong if self._hover else T.outline)
        text = ellipsize(F["small"], self.current_label(), w - S(40))
        self.create_text(S(12), h / 2, text=text, anchor="w", fill=T.text, font=F["small"])
        self.create_text(w - S(16), h / 2 + 1, text=I.chevron, font=F["icon_small"],
                         fill=T.text if self._hover else T.muted)


# ============================================================
# Dialogs
# ============================================================

class Dialog(tk.Toplevel):
    """Modal themed dialog base, centered over its parent."""

    def __init__(self, parent, title, width=360):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(bg=T.bg)
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.body = tk.Frame(self, bg=T.bg, padx=S(18), pady=S(16))
        self.body.pack(fill="both", expand=True)
        self._width = S(width)
        self.bind("<Escape>", lambda e: self.cancel())
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def present(self):
        from ..winutil import style_title_bar
        self.update_idletasks()
        parent = self.master.winfo_toplevel()
        w = max(self._width, self.winfo_reqwidth())
        h = self.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        y = parent.winfo_rooty() + max(S(40), (parent.winfo_height() - h) // 3)
        self.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        style_title_bar(self, T.dark, T.bg, T.text)
        self.grab_set()
        self.focus_force()

    def buttons(self, ok_text, on_ok, danger=False, cancel_text="Cancel"):
        row = tk.Frame(self.body, bg=T.bg)
        row.pack(fill="x", pady=(S(16), 0))
        ok = RoundedButton(row, ok_text, on_ok, height=34, variant="danger" if danger else "primary")
        ok.pack(side="right")
        if cancel_text:
            RoundedButton(row, cancel_text, self.cancel, height=34).pack(side="right", padx=(0, S(8)))
        self.bind("<Return>", lambda e: on_ok())
        return ok

    def cancel(self):
        self.grab_release()
        self.destroy()


def entry(parent, var, font="body"):
    frame = tk.Frame(parent, bg=T.outline_strong, padx=1, pady=1)
    e = tk.Entry(frame, textvariable=var, font=F[font], bg=T.raised, fg=T.text, insertbackground=T.text,
                 relief="flat", bd=S(6), selectbackground=T.accent, selectforeground=T.accent_text)
    e.pack(fill="x")
    e.bind("<FocusIn>", lambda ev: frame.configure(bg=T.accent))
    e.bind("<FocusOut>", lambda ev: frame.configure(bg=T.outline_strong))
    return frame, e


class PromptDialog(Dialog):
    def __init__(self, parent, title, message, initial="", validate=None, on_ok=None, ok_text="Save"):
        super().__init__(parent, title)
        self.validate = validate
        self.on_ok = on_ok
        label(self.body, message, font="small", fg=T.muted, justify="left",
              wraplength=S(320)).pack(anchor="w")
        self.var = tk.StringVar(value=initial)
        frame, self.entry = entry(self.body, self.var)
        frame.pack(fill="x", pady=(S(8), 0))
        self.error = label(self.body, "", font="tiny", fg=T.red_hover)
        self.error.pack(anchor="w", pady=(S(4), 0))
        self.buttons(ok_text, self._ok)
        self.present()
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def _ok(self):
        value = self.var.get().strip()
        problem = self.validate(value) if self.validate else None
        if problem:
            self.error.config(text=problem)
            return
        self.cancel()
        if self.on_ok:
            self.on_ok(value)


class ConfirmDialog(Dialog):
    def __init__(self, parent, title, message, ok_text="Delete", on_ok=None, danger=True):
        super().__init__(parent, title)
        self.on_ok = on_ok
        label(self.body, message, font="body", justify="left", wraplength=S(320)).pack(anchor="w")
        self.buttons(ok_text, self._ok, danger=danger)
        self.present()

    def _ok(self):
        self.cancel()
        if self.on_ok:
            self.on_ok()


class ErrorDialog(Dialog):
    """Short explanation up top, then a clearly marked 'How to fix'."""

    def __init__(self, parent, title, summary, fix=None, link_text=None, link_cmd=None):
        super().__init__(parent, title, width=400)
        label(self.body, summary, font="body", justify="left", wraplength=S(360)).pack(anchor="w")
        if fix:
            box = tk.Frame(self.body, bg=T.card, highlightthickness=1, highlightbackground=T.border)
            box.pack(fill="x", pady=(S(12), 0))
            label(box, "HOW TO FIX", font="tiny", fg=T.faint).pack(anchor="w", padx=S(12), pady=(S(8), 0))
            label(box, fix, font="small", justify="left", wraplength=S(340)).pack(anchor="w", padx=S(12),
                                                                                   pady=(S(2), S(10)))
        if link_text and link_cmd:
            link(self.body, link_text, link_cmd, font="small", fg=T.accent).pack(anchor="w", pady=(S(10), 0))
        self.buttons("OK", self.cancel, cancel_text=None)
        self.present()


class MessageDialog(Dialog):
    def __init__(self, parent, title, message, detail=None, link_text=None, link_cmd=None):
        super().__init__(parent, title, width=380)
        label(self.body, message, font="body", justify="left", wraplength=S(340)).pack(anchor="w")
        if detail:
            label(self.body, detail, font="tiny", fg=T.muted, justify="left",
                  wraplength=S(340)).pack(anchor="w", pady=(S(8), 0))
        if link_text and link_cmd:
            link(self.body, link_text, link_cmd, font="small", fg=T.accent).pack(anchor="w", pady=(S(8), 0))
        self.buttons("OK", self.cancel, cancel_text=None)
        self.present()

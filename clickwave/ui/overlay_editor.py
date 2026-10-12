"""Overlay designer: arrange and style the OBS input overlay, live."""

import copy
import tkinter as tk
from tkinter import colorchooser

from .. import overlay as ov
from ..inputs import input_label
from . import aa
from .theme import F, I, S, T, blend
from .widgets import (
    Dialog, Dropdown, MarkerSlider, PopupMenu, PromptDialog, RoundedButton, ToggleSwitch, label, link,
)

PREVIEW_BG = "#2b2e36"   # stands in for the stream behind the overlay
SNAP = 0.5


class OverlayEditor(Dialog):
    def __init__(self, app):
        super().__init__(app.root, "Customize overlay", width=760)
        self.app = app
        self.style = ov.merge_style(app.settings["overlay_style"])
        self._rects = []       # (x1, y1, x2, y2, ident)
        self._drag = None
        self._hover = None
        b = self.body

        top = tk.Frame(b, bg=T.bg)
        top.pack(fill="both", expand=True)

        # ---------------- preview ----------------
        left = tk.Frame(top, bg=T.bg)
        left.pack(side="left", fill="both", expand=True)
        frame = tk.Frame(left, bg=T.border, padx=1, pady=1)
        frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(frame, width=S(420), height=S(300), bg=PREVIEW_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda e: self._set_hover(None))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<ButtonRelease-3>", self._right)
        label(left, "Drag keys to arrange them · right-click a key to rename or resize it",
              font="tiny", fg=T.faint).pack(anchor="w", pady=(S(6), 0))
        self.size_hint = label(left, "", font="tiny", fg=T.faint, justify="left", wraplength=S(420))
        self.size_hint.pack(anchor="w", pady=(S(2), 0))

        # ---------------- controls ----------------
        right = tk.Frame(top, bg=T.bg, width=S(270))
        right.pack(side="left", fill="y", padx=(S(16), 0))

        def section(title):
            label(right, title.upper(), font="tiny", fg=T.faint).pack(anchor="w", pady=(S(10), S(2)))

        def row(title):
            r = tk.Frame(right, bg=T.bg)
            r.pack(fill="x", pady=(S(4), 0))
            label(r, title, font="small").pack(side="left")
            return r

        section("Layout")
        r = row("Arrange")
        self.layout_dd = Dropdown(r, [("row", "Row"), ("column", "Column"), ("grid", "Grid"), ("custom", "Custom (dragged)")],
                                  self.style["layout"], lambda v: self.set("layout", v), height=30)
        self.layout_dd.configure(width=S(150))
        self.layout_dd.pack(side="right")
        self.cols = self._slider(right, "Grid columns", "columns", (1, 2, 3, 4, 6, 8), 1, 8, "")

        section("Size")
        self._slider(right, "Key size", "key_size", (32, 48, 64, 96, 128, 160), 32, 160, " px")
        self._slider(right, "Spacing", "gap", (0, 5, 10, 20, 30, 40), 0, 40, " px")
        self._slider(right, "Roundness", "radius", (0, 8, 14, 24, 36, 50), 0, 50, " px")
        self._slider(right, "Text size", "font", (60, 80, 100, 130, 160, 200), 60, 200, "%")

        section("Colors")
        sw = tk.Frame(right, bg=T.bg)
        sw.pack(fill="x", pady=(S(4), 0))
        self.swatches = {}
        for key, title in (("key_color", "Keys"), ("pressed_color", "Pressed"), ("text_color", "Text")):
            cell = tk.Frame(sw, bg=T.bg)
            cell.pack(side="left", padx=(0, S(12)))
            c = tk.Canvas(cell, width=S(30), height=S(30), bg=T.bg, highlightthickness=0, cursor="hand2")
            c.pack()
            c.bind("<ButtonRelease-1>", lambda e, k=key: self.pick_color(k))
            label(cell, title, font="tiny", fg=T.muted).pack()
            self.swatches[key] = c
        self._slider(right, "Key opacity", "key_opacity", (0, 25, 50, 75, 85, 100), 0, 100, "%")
        r = row("Key border")
        self.border_sw = ToggleSwitch(r, self.style["border"], lambda v: self.set("border", v))
        self.border_sw.pack(side="right")

        section("Stats")
        stats_row = tk.Frame(right, bg=T.bg)
        stats_row.pack(fill="x", pady=(S(4), 0))
        self.stat_toggles = {}
        for key, title in ov.STATS:
            cell = tk.Frame(stats_row, bg=T.bg)
            cell.pack(side="left", padx=(0, S(12)))
            t = ToggleSwitch(cell, key in self.style["stats"], lambda v, k=key: self.toggle_stat(k, v))
            t.pack(side="left")
            label(cell, title, font="small").pack(side="left", padx=(S(6), 0))
            self.stat_toggles[key] = t
        r = row("Position")
        self.pos_dd = Dropdown(r, [("below", "Below keys"), ("above", "Above keys"), ("right", "Right of keys"),
                                   ("left", "Left of keys"), ("hidden", "Hidden")],
                               self.style["stats_pos"], lambda v: self.set("stats_pos", v), height=30)
        self.pos_dd.configure(width=S(150))
        self.pos_dd.pack(side="right")

        section("In OBS")
        r = row("Fit to source size")
        self.fit_sw = ToggleSwitch(r, self.style["fit"], lambda v: self.set("fit", v))
        self.fit_sw.pack(side="right")
        r = row("Align")
        self.align_dd = Dropdown(r, [("left", "Left"), ("center", "Center"), ("right", "Right")],
                                 self.style["align"], lambda v: self.set("align", v), height=30)
        self.align_dd.configure(width=S(150))
        self.align_dd.pack(side="right")

        btns = tk.Frame(b, bg=T.bg)
        btns.pack(fill="x", pady=(S(14), 0))
        link(btns, "Reset to default", self.reset, font="small").pack(side="left")
        if app.overlay.running:
            RoundedButton(btns, "Open in browser", lambda: app.open_url(app.overlay.url), height=34,
                          font=F["small"], icon=I.open).pack(side="right", padx=(S(8), 0))
        RoundedButton(btns, "Done", self.cancel, height=34, variant="primary").pack(side="right")
        self.bind("<Return>", lambda e: self.cancel())
        self._update_controls()
        self.present()
        self.redraw()

    # ---------------- helpers ----------------
    def _slider(self, parent, title, key, marks, lo, hi, unit):
        head = tk.Frame(parent, bg=T.bg)
        head.pack(fill="x", pady=(S(4), 0))
        label(head, title, font="small").pack(side="left")
        readout = label(head, f"{self.style[key]}{unit}", font="small_semi")
        readout.pack(side="right")

        def changed(v):
            readout.config(text=f"{v}{unit}")
            self.set(key, v)
        s = MarkerSlider(parent, marks, lo, hi, self.style[key], command=changed, snap=False, compact=True)
        s.configure(bg=T.bg)
        s.pack(fill="x")
        s._readout, s._key, s._unit = readout, key, unit
        return s

    def keys(self):
        return [(i, input_label(i)) for i in self.app.settings.p("inputs")]

    def set(self, key, value):
        self.style[key] = value
        if key == "align" and self.style["layout"] == "custom":
            self._align_custom(value)
        self._update_controls()
        self.commit()

    def _align_custom(self, align):
        """Custom positions are stored, so alignment rewrites them: each row
        of keys snaps to the left / center / right of the widest row."""
        st = self.style
        items = [k for k in ov.compute_layout(self.keys(), st) if k["id"] in st["positions"]]
        ov.align_rows(items, align)
        for k in items:
            st["positions"][k["id"]] = [round(k["x"] * 4) / 4, k["y"]]
        self._normalize_positions()

    def commit(self):
        self.app.set_overlay_style(copy.deepcopy(self.style))
        self.redraw()

    def toggle_stat(self, key, on):
        stats = [k for k, _ in ov.STATS if (k == key and on) or (k != key and k in self.style["stats"])]
        self.style["stats"] = stats
        self.commit()

    def pick_color(self, key):
        current = self.style.get(key) or self.app.accent_color()
        _, hexcolor = colorchooser.askcolor(color=current, parent=self, title="Pick a color")
        if hexcolor:
            self.style[key] = hexcolor
            self._update_controls()
            self.commit()

    def reset(self):
        self.style = ov.merge_style({})
        self.layout_dd.set(self.style["layout"])
        self.pos_dd.set(self.style["stats_pos"])
        self.align_dd.set(self.style["align"])
        self.border_sw.set(self.style["border"])
        self.fit_sw.set(self.style["fit"])
        for k, t in self.stat_toggles.items():
            t.set(k in self.style["stats"])
        self._refresh_sliders()
        self._update_controls()
        self.commit()

    def _refresh_sliders(self):
        def walk(w):
            for c in w.winfo_children():
                if isinstance(c, MarkerSlider) and hasattr(c, "_readout"):
                    key = c._key
                    c.set(self.style[key])
                    c._readout.config(text=f"{self.style[key]}{c._unit}")
                walk(c)
        walk(self)

    def _update_controls(self):
        for key, c in self.swatches.items():
            c.delete("all")
            color = self.style.get(key) or self.app.accent_color()
            aa.rrect(c, 2, 2, S(28), S(28), S(7), color, T.outline_strong, bg=T.bg)
        self.cols.configure(cursor="hand2" if self.style["layout"] == "grid" else "arrow")

    # ---------------- preview geometry ----------------
    def _content(self):
        """Layout in base px (like the page), incl. stats block."""
        st = self.style
        size, gap = st["key_size"], st["gap"]
        keys = ov.compute_layout(self.keys(), st)
        kw = max([k["x"] * (size + gap) + k["w"] * size + (k["w"] - 1) * gap for k in keys] or [0])
        kh = max([k["y"] * (size + gap) + size for k in keys] or [0])
        stats = st["stats"] if st["stats_pos"] != "hidden" else []
        sbox_w, sbox_h = max(size * 1.1, size * 0.9), size * 0.95
        n = len(stats)
        if st["stats_pos"] in ("left", "right"):
            sw, sh = (sbox_w if n else 0), n * sbox_h + max(0, n - 1) * gap
        else:
            sw, sh = n * sbox_w + max(0, n - 1) * gap, (sbox_h if n else 0)
        return keys, kw, kh, stats, sbox_w, sbox_h, sw, sh

    def redraw(self):
        c = self.canvas
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 10:
            W, H = c.winfo_reqwidth(), c.winfo_reqheight()
        st = self.style
        size, gap = st["key_size"], st["gap"]
        keys, kw, kh, stats, bw, bh, sw, sh = self._content()
        pos = st["stats_pos"]
        between = gap if (stats and keys) else 0
        if pos in ("left", "right"):
            cw, ch = kw + between + sw, max(kh, sh)
        else:
            cw, ch = max(kw, sw), kh + between + sh
        cw, ch = cw + 2 * gap, ch + 2 * gap
        margin = S(20)
        scale = min((W - 2 * margin) / max(1, cw), (H - 2 * margin) / max(1, ch), 2.0)
        ox = (W - cw * scale) / 2 + gap * scale
        oy = (H - ch * scale) / 2 + gap * scale
        if pos == "above":
            kx, ky, sx, sy = ox, oy + (sh + between) * scale, ox, oy
        elif pos == "left":
            kx, ky, sx, sy = ox + (sw + between) * scale, oy, ox, oy
        elif pos == "right":
            kx, ky, sx, sy = ox, oy, ox + (kw + between) * scale, oy
        else:
            kx, ky, sx, sy = ox, oy, ox, oy + (kh + between) * scale
        if pos in ("above", "below", "hidden") or not stats:
            inner = max(kw, sw)
            lead = {"left": 0, "center": 0.5, "right": 1}[st["align"]]
            kx += (inner - kw) * lead * scale
            sx += (inner - sw) * lead * scale
        self._scale, self._origin = scale, (kx, ky)

        key_fill = blend(PREVIEW_BG, st["key_color"], st["key_opacity"] / 100)
        pressed = st["pressed_color"] or self.app.accent_color()
        border = blend(key_fill, "#ffffff", 0.14) if st["border"] else None
        radius = min(st["radius"], size / 2) * scale
        font_px = max(6, int(size * 0.27 * st["font"] / 100 * scale))
        self._rects = []
        for k in keys:
            x1 = kx + k["x"] * (size + gap) * scale
            y1 = ky + k["y"] * (size + gap) * scale
            x2 = x1 + (k["w"] * size + (k["w"] - 1) * gap) * scale
            y2 = y1 + size * scale
            down = self._hover == k["id"] or (self._drag and self._drag[0] == k["id"])
            fill = pressed if down else key_fill
            aa.rrect(c, x1, y1, x2, y2, radius, fill, pressed if down else border, max(1, int(scale)), bg=PREVIEW_BG)
            c.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=k["label"], fill="#ffffff" if down else st["text_color"],
                          font=("Segoe UI Semibold", -font_px))
            self._rects.append((x1, y1, x2, y2, k["id"]))
        names = dict(ov.STATS)
        for i, s in enumerate(stats):
            if pos in ("left", "right"):
                x1, y1 = sx, sy + i * (bh + gap) * scale
            else:
                x1, y1 = sx + i * (bw + gap) * scale, sy
            x2, y2 = x1 + bw * scale, y1 + bh * scale
            aa.rrect(c, x1, y1, x2, y2, radius, key_fill, border, max(1, int(scale)), bg=PREVIEW_BG)
            c.create_text((x1 + x2) / 2, y1 + (y2 - y1) * 0.42, text="0", fill=st["text_color"],
                          font=("Segoe UI Semibold", -max(6, int(font_px * 1.3))))
            c.create_text((x1 + x2) / 2, y1 + (y2 - y1) * 0.78, text=names[s].upper(), fill=blend(st["text_color"], PREVIEW_BG, 0.35),
                          font=("Segoe UI", -max(5, int(font_px * 0.62))))
        if not keys:
            c.create_text(W / 2, H / 2, text="Add watched inputs to see them here", fill=T.faint, font=F["small"])
        # Suggested OBS source size: 2x the content keeps it crisp when scaled down.
        bw_px, bh_px = int(cw * 2), int(ch * 2)
        self.size_hint.config(text=f"OBS Browser source size: about {bw_px} × {bh_px} looks sharp. "
                                   "Resize the source with its Width/Height properties rather than dragging "
                                   "its corners." if st["fit"] else
                              f"Fit is off: the overlay draws at {int(cw)} × {int(ch)} px.")

    # ---------------- interaction ----------------
    def _key_at(self, x, y):
        for x1, y1, x2, y2, ident in self._rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return ident
        return None

    def _set_hover(self, ident):
        if ident != self._hover:
            self._hover = ident
            self.canvas.configure(cursor="fleur" if ident else "")
            self.redraw()

    def _motion(self, e):
        if not self._drag:
            self._set_hover(self._key_at(e.x, e.y))

    def _press(self, e):
        ident = self._key_at(e.x, e.y)
        if ident:
            self._drag = (ident, e.x, e.y, None)

    def _drag_move(self, e):
        if not self._drag:
            return
        ident, x0, y0, start = self._drag
        st = self.style
        if start is None:
            # First move: freeze the current arrangement as a custom layout.
            current = {k["id"]: [k["x"], k["y"]] for k in ov.compute_layout(self.keys(), st)}
            if st["layout"] != "custom" or ident not in st["positions"]:
                st["positions"] = current
                st["layout"] = "custom"
                self.layout_dd.set("custom")
            start = list(st["positions"][ident])
            self._drag = (ident, x0, y0, start)
        unit = (st["key_size"] + st["gap"]) * self._scale
        nx = round((start[0] + (e.x - x0) / unit) / SNAP) * SNAP
        ny = round((start[1] + (e.y - y0) / unit) / SNAP) * SNAP
        if [nx, ny] != st["positions"].get(ident):
            st["positions"][ident] = [nx, ny]
            # Dragged past the left/top edge: push everything else over instead
            # of letting it go off-canvas, and rebase the drag so it keeps tracking.
            shift = self._normalize_positions(only_negative=True)
            if shift != (0, 0):
                start[0] += shift[0]
                start[1] += shift[1]
            self.redraw()

    def _normalize_positions(self, only_negative=False):
        """Shift custom positions so the top-left key sits at 0,0."""
        pos = self.style["positions"]
        keys = [i for i, _ in self.keys() if i in pos]
        if not keys:
            return (0, 0)
        mx, my = min(pos[i][0] for i in keys), min(pos[i][1] for i in keys)
        if only_negative:
            mx, my = min(mx, 0), min(my, 0)
        for i in pos:
            pos[i] = [pos[i][0] - mx, pos[i][1] - my]
        return (-mx, -my)

    def _release(self, e):
        if self._drag and self._drag[3] is not None:
            self._normalize_positions()
            self.commit()
        self._drag = None

    def _right(self, e):
        ident = self._key_at(e.x, e.y)
        if not ident:
            return
        cur = self.style["keys"].get(ident) or {}
        w = ov.key_width(self.style, ident)
        items = [{"header": True, "label": input_label(ident)},
                 {"label": "Rename…", "command": lambda: self.rename(ident)}]
        if cur.get("label"):
            items.append({"label": "Use the key's name", "command": lambda: self.set_key(ident, label=None)})
        items.append({"header": True, "label": "Width"})
        for val, text in ((1, "1 key"), (1.5, "1.5 keys"), (2, "2 keys"), (3, "3 keys"), (4, "4 keys")):
            items.append({"label": text, "checked": w == val, "command": lambda v=val: self.set_key(ident, w=v)})
        PopupMenu(self, items, e.x_root, e.y_root, min_width=S(170))

    def rename(self, ident):
        cur = (self.style["keys"].get(ident) or {}).get("label") or input_label(ident)
        PromptDialog(self, "Rename key", f"Text shown on the overlay for {input_label(ident)}:", initial=cur,
                     validate=lambda v: None if len(v) <= 16 else "Keep it under 16 characters",
                     on_ok=lambda v: self.set_key(ident, label=v or None))

    def set_key(self, ident, **changes):
        cur = dict(self.style["keys"].get(ident) or {})
        cur.update(changes)
        if not cur.get("label") and cur.get("w", 1) == 1:
            self.style["keys"].pop(ident, None)
        else:
            self.style["keys"][ident] = cur
        self.commit()

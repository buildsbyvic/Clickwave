"""Antialiased shapes for Tk canvases.

Tk draws polygons and ovals without antialiasing, so rounded corners and
circles look jagged. Here each corner/circle is rendered once into a
small PhotoImage with per-pixel coverage (signed distance), blended over
the known background color, and cached. Straight parts are plain
rectangles, so a big rounded card costs four tiny images.
"""

import math
import tkinter as tk

_cache = {}


def _rgb(color):
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _clamp(v):
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def _pixel(bg, outline, fill, a, inner):
    """Blend: bg <- outline band (a - inner) <- fill (inner)."""
    out = []
    for b, o, f in zip(bg, outline, fill):
        out.append(int(b * (1 - a) + o * (a - inner) + f * inner + 0.5))
    return "#%02x%02x%02x" % tuple(out)


def _image(rows):
    h = len(rows)
    w = len(rows[0]) if h else 0
    img = tk.PhotoImage(width=max(1, w), height=max(1, h))
    if w and h:
        img.put(" ".join("{" + " ".join(row) + "}" for row in rows))
    return img


def _corner_rows(r, fill, outline, width, bg):
    ri = max(1, int(math.ceil(r)))
    F, B = _rgb(fill), _rgb(bg)
    O = _rgb(outline) if outline else F
    rows = []
    for y in range(ri):
        row = []
        dy = max(0.0, r - (y + 0.5))
        for x in range(ri):
            dx = max(0.0, r - (x + 0.5))
            d = math.hypot(dx, dy)
            a = _clamp(r - d + 0.5)
            inner = _clamp(r - width - d + 0.5) if outline else a
            row.append(_pixel(B, O, F, a, inner))
        rows.append(row)
    return rows


def _corners(r, fill, outline, width, bg):
    key = ("corner", round(r, 2), fill, outline, width, bg)
    imgs = _cache.get(key)
    if imgs is None:
        tl = _corner_rows(r, fill, outline, width, bg)
        tr = [list(reversed(row)) for row in tl]
        bl = list(reversed(tl))
        br = [list(reversed(row)) for row in bl]
        imgs = tuple(_image(rows) for rows in (tl, tr, bl, br))
        _cache[key] = imgs
    return imgs


def rrect(canvas, x1, y1, x2, y2, r, fill, outline=None, width=1, bg=None, tags=None):
    """Antialiased rounded rectangle covering pixels [x1, x2) x [y1, y2)."""
    x1, y1, x2, y2 = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return
    bg = bg or canvas["bg"]
    if outline == fill:
        outline = None
    r = max(0.0, min(r, w / 2, h / 2))
    ri = int(math.ceil(r))
    kw = {"tags": tags} if tags else {}
    if ri == 0:
        canvas.create_rectangle(x1, y1, x2, y2, fill=outline or fill, outline="", **kw)
        if outline:
            canvas.create_rectangle(x1 + width, y1 + width, x2 - width, y2 - width, fill=fill, outline="", **kw)
        return
    # body
    if x2 - ri > x1 + ri:
        canvas.create_rectangle(x1 + ri, y1, x2 - ri, y2, fill=fill, outline="", width=0, **kw)
    if y2 - ri > y1 + ri:
        canvas.create_rectangle(x1, y1 + ri, x1 + ri, y2 - ri, fill=fill, outline="", width=0, **kw)
        canvas.create_rectangle(x2 - ri, y1 + ri, x2, y2 - ri, fill=fill, outline="", width=0, **kw)
    # straight outline edges
    if outline:
        if x2 - ri > x1 + ri:
            canvas.create_rectangle(x1 + ri, y1, x2 - ri, y1 + width, fill=outline, outline="", width=0, **kw)
            canvas.create_rectangle(x1 + ri, y2 - width, x2 - ri, y2, fill=outline, outline="", width=0, **kw)
        if y2 - ri > y1 + ri:
            canvas.create_rectangle(x1, y1 + ri, x1 + width, y2 - ri, fill=outline, outline="", width=0, **kw)
            canvas.create_rectangle(x2 - width, y1 + ri, x2, y2 - ri, fill=outline, outline="", width=0, **kw)
    tl, tr, bl, br = _corners(r, fill, outline, width, bg)
    canvas.create_image(x1, y1, image=tl, anchor="nw", **kw)
    canvas.create_image(x2 - ri, y1, image=tr, anchor="nw", **kw)
    canvas.create_image(x1, y2 - ri, image=bl, anchor="nw", **kw)
    canvas.create_image(x2 - ri, y2 - ri, image=br, anchor="nw", **kw)


def circle(canvas, cx, cy, r, fill, outline=None, width=1, bg=None, tags=None):
    """Antialiased circle centered on (cx, cy)."""
    bg = bg or canvas["bg"]
    if outline == fill:
        outline = None
    n = int(math.ceil(2 * r)) + 2
    ox = math.floor(cx - n / 2)
    oy = math.floor(cy - n / 2)
    fx = round((cx - ox) * 2) / 2   # center inside the image, snapped to 1/2 px
    fy = round((cy - oy) * 2) / 2
    key = ("circle", round(r, 2), fill, outline, width, bg, fx, fy, n)
    img = _cache.get(key)
    if img is None:
        F, B = _rgb(fill), _rgb(bg)
        O = _rgb(outline) if outline else F
        rows = []
        for y in range(n):
            row = []
            for x in range(n):
                d = math.hypot(x + 0.5 - fx, y + 0.5 - fy)
                a = _clamp(r - d + 0.5)
                inner = _clamp(r - width - d + 0.5) if outline else a
                row.append(_pixel(B, O, F, a, inner))
            rows.append(row)
        img = _image(rows)
        _cache[key] = img
    kw = {"tags": tags} if tags else {}
    canvas.create_image(ox, oy, image=img, anchor="nw", **kw)


def clear_cache():
    _cache.clear()

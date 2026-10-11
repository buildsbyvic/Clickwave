"""Colors, fonts, DPI scaling."""

import tkinter.font as tkfont

ACCENTS = {
    "violet": "#7b6cff",
    "blue": "#3b82f6",
    "cyan": "#06b6d4",
    "green": "#22c55e",
    "yellow": "#eab308",
    "orange": "#f97316",
    "pink": "#ec4899",
    "red": "#ef4444",
}

_PALETTES = {
    "dark": dict(
        bg="#0e1014", card="#161920", border="#232733", raised="#1f232d",
        raised_hover="#282d39", text="#eceef4", muted="#8c93a6", faint="#5d6476",
        tick="#383e4d", tick_strong="#555c6e", track="#262a35", knob="#ffffff",
        shadow="#08090c", outline="#2c3140", outline_strong="#3d4352", knob_off="#a3a9b8",
    ),
    "light": dict(
        bg="#eef0f5", card="#ffffff", border="#dfe2ea", raised="#f1f3f7",
        raised_hover="#e5e8ef", text="#151821", muted="#5b6275", faint="#8d93a3",
        tick="#c8ccd6", tick_strong="#959bab", track="#e3e6ec", knob="#ffffff",
        shadow="#c9ccd4", outline="#d6dae3", outline_strong="#c0c5d0", knob_off="#ffffff",
    ),
}


def blend(c1, c2, t):
    """Mix two #rrggbb colors; t=0 -> c1, t=1 -> c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(a, b))


class T:
    """Current theme colors (class attributes, swapped by apply_theme)."""
    dark = True
    green = "#1f9d60"
    green_hover = "#23af6b"
    green_press = "#1a8752"
    red = "#d9434b"
    red_hover = "#e5545b"
    red_press = "#bb3940"
    warn = "#e0a63c"
    ok = "#2fbf7c"


def apply_theme(theme="dark", accent="violet"):
    pal = _PALETTES.get(theme, _PALETTES["dark"])
    for key, value in pal.items():
        setattr(T, key, value)
    T.dark = theme != "light"
    a = ACCENTS.get(accent, ACCENTS["violet"])
    T.accent = a
    T.accent_hover = blend(a, "#ffffff", 0.16) if T.dark else blend(a, "#000000", 0.1)
    T.accent_dim = blend(a, T.card, 0.5)
    T.accent_soft = blend(T.card, a, 0.14)
    T.accent_text = "#ffffff" if accent not in ("yellow", "cyan", "green") else "#10131a"
    T.warn = "#e0a63c" if T.dark else "#b7791f"
    T.ok = "#2fbf7c" if T.dark else "#16935a"


apply_theme()

_SCALE = [1.0]
F = {}


def set_scale(value):
    _SCALE[0] = value


def scale():
    return _SCALE[0]


def S(value):
    """Scale a pixel measurement by the display's DPI."""
    return int(round(value * _SCALE[0]))


def init_fonts(root):
    families = set(tkfont.families(root))
    base = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"
    semi = "Segoe UI Semibold" if "Segoe UI Semibold" in families else base
    icon_family = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), base)
    F.update({
        "title": (semi, 16),
        "h2": (semi, 11),
        "body": (base, 10),
        "small": (base, 9),
        "tiny": (base, 8),
        "semi": (semi, 10),
        "small_semi": (semi, 9),
        "button": (semi, 11),
        "readout": (semi, 15),
        "stat": (semi, 14),
        "toast": (semi, 12),
        "mono": ("Consolas", 9),
        "icon": (icon_family, 10),
        "icon_small": (icon_family, 8),
        "icon_big": (icon_family, 12),
        "huge": (semi, 22),
    })


# Icon glyphs (Segoe Fluent Icons / Segoe MDL2 Assets)
class I:
    chevron = ""
    close = ""
    play = ""
    stop = ""
    more = ""
    add = ""
    check = ""
    mic = ""
    folder = ""
    keyboard = ""
    package = ""
    files = ""
    copy = ""
    open = ""
    refresh = ""
    bolt = ""
    trophy = ""
    export = ""
    reset = ""


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)

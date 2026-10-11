"""Render the Clickwave logo (pure Python, no dependencies).

Writes:
    assets/clickwave.ico        multi-size app icon (16..256 px, PNG entries)
    assets/logo_<n>.png         PNGs for the window header / docs

The logo: a dark rounded tile with a glowing keycap sending out two
sound-wave arcs. Every pixel is 4x4 supersampled from signed distance
functions, so edges are smooth at every size. Small sizes drop the outer
wave and thicken strokes so they stay legible.

Run:  py tools/make_icon.py
"""

import math
import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")

# Palette
TILE_TOP = (30, 35, 50)
TILE_BOTTOM = (11, 13, 19)
TILE_EDGE = (58, 66, 88)
ACCENT_A = (79, 140, 255)    # blue
ACCENT_B = (139, 108, 255)   # violet


def lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def clamp01(v):
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


# ---------------- signed distance functions (unit square, y down) ----------------

def sd_superellipse(x, y, cx, cy, r, n=5.0):
    """Approximate signed distance to a superellipse (squircle)."""
    dx, dy = abs(x - cx) / r, abs(y - cy) / r
    k = (dx ** n + dy ** n) ** (1.0 / n)
    return (k - 1.0) * r


def sd_round_rect(x, y, cx, cy, hw, hh, rad):
    qx = abs(x - cx) - hw + rad
    qy = abs(y - cy) - hh + rad
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - rad


def sd_arc(x, y, cx, cy, radius, a0, a1, half_width):
    """Arc stroke with round caps; angles in radians (math convention, y up)."""
    dx, dy = x - cx, cy - y
    ang = math.atan2(dy, dx)
    if a0 <= ang <= a1:
        return abs(math.hypot(dx, dy) - radius) - half_width
    best = 1e9
    for a in (a0, a1):
        px, py = cx + radius * math.cos(a), cy - radius * math.sin(a)
        best = min(best, math.hypot(x - px, y - py))
    return best - half_width


# ---------------- scene ----------------

def shade(x, y, size):
    """RGBA (floats 0..1 alpha) for one sample point in unit coords."""
    small = size <= 24
    medium = size <= 48

    # tile
    d_tile = sd_superellipse(x, y, 0.5, 0.5, 0.47)
    if d_tile > 0:
        return (0.0, 0.0, 0.0, 0.0)
    color = lerp(TILE_TOP, TILE_BOTTOM, clamp01((y - 0.05) / 0.9))
    edge_w = 0.012 if not small else 0.03
    if d_tile > -edge_w:                       # thin lighter rim
        color = lerp(color, TILE_EDGE, 0.75)
    # soft top sheen
    color = lerp(color, (255, 255, 255), 0.05 * clamp01(1 - y / 0.45))

    kc_x, kc_y = (0.385, 0.615) if not small else (0.40, 0.60)   # keycap center (group is centered)
    hw = 0.175 if not small else 0.21
    rad = 0.075 if not small else 0.08

    # waves (behind the keycap so its shadow can overlap them)
    wave_w = 0.028 if not medium else (0.042 if not small else 0.06)
    a0, a1 = math.radians(8), math.radians(82)
    waves = [(0.305 if not small else 0.34, 1.0)]
    if not small:
        waves.append((0.43, 0.5))
    for radius, strength in waves:
        d = sd_arc(x, y, kc_x, kc_y, radius, a0, a1, wave_w)
        if d < 0:
            t = clamp01((x - 0.4) / 0.5)
            color = lerp(color, lerp(ACCENT_A, ACCENT_B, t), strength)

    # keycap shadow (soft)
    d_sh = sd_round_rect(x, y, kc_x + 0.015, kc_y + 0.035, hw, hw, rad)
    if d_sh < 0.05:
        color = lerp(color, (0, 0, 0), 0.45 * clamp01(1 - (d_sh + 0.0) / 0.05) if d_sh > 0 else 0.45)

    # keycap body
    d_body = sd_round_rect(x, y, kc_x, kc_y, hw, hw, rad)
    if d_body < 0:
        t = clamp01(((x - (kc_x - hw)) + (y - (kc_y - hw))) / (4 * hw))
        body = lerp(ACCENT_A, ACCENT_B, t)
        color = lerp(body, (0, 0, 0), 0.18)    # sides a touch darker
        # top face (inset, shifted up) - the "dish" of the keycap
        inset = 0.045 if not small else 0.05
        d_top = sd_round_rect(x, y, kc_x, kc_y - 0.022, hw - inset, hw - inset - 0.006, rad * 0.75)
        if d_top < 0:
            face = lerp(body, (255, 255, 255), 0.10 + 0.12 * clamp01((kc_y - y) / hw))
            color = face
    return color + (1.0,)


def render(size, ss=4):
    rows = []
    inv = 1.0 / (size * ss)
    for py in range(size):
        row = bytearray()
        for px in range(size):
            r = g = b = a = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    x = (px * ss + sx + 0.5) * inv
                    y = (py * ss + sy + 0.5) * inv
                    cr, cg, cb, ca = shade(x, y, size)
                    r += cr * ca
                    g += cg * ca
                    b += cb * ca
                    a += ca
            n = ss * ss
            if a > 0:
                row += bytes((int(r / a + 0.5), int(g / a + 0.5), int(b / a + 0.5), int(a / n * 255 + 0.5)))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return rows


def png_bytes(rows, size):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def dib_bytes(rows, size):
    """Classic 32-bit BGRA icon entry (what every Windows icon reader
    understands). Rows are stored bottom-up, followed by an all-zero
    AND mask (the alpha channel does the masking)."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    pixels = bytearray()
    for row in reversed(rows):
        for i in range(0, len(row), 4):
            r, g, b, a = row[i:i + 4]
            pixels += bytes((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    return header + bytes(pixels) + bytes(mask_stride * size)


def ico_bytes(images):
    """images: [(size, entry_bytes)] - PNG for 256, DIB for the rest."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main():
    os.makedirs(OUT, exist_ok=True)
    images = []
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        rows = render(size)
        data = png_bytes(rows, size)
        images.append((size, data if size >= 256 else dib_bytes(rows, size)))
        if size in (32, 64, 256):
            with open(os.path.join(OUT, f"logo_{size}.png"), "wb") as f:
                f.write(data)
        print("rendered", size)
    with open(os.path.join(OUT, "clickwave.ico"), "wb") as f:
        f.write(ico_bytes(images))
    for size in (28, 512):
        with open(os.path.join(OUT, f"logo_{size}.png"), "wb") as f:
            f.write(png_bytes(render(size, ss=4 if size < 100 else 3), size))
    print("done ->", OUT)


if __name__ == "__main__":
    main()

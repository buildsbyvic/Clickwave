"""Stream overlay: a local web page for an OBS Browser Source.

Shows your watched inputs lighting up live (an input display) plus CPS
and click counters, updated over Server-Sent Events. Only listens on
127.0.0.1, so nothing is reachable from other machines.

Everything about its look - layout, key positions, sizes, colors, which
stats show and where - is set in Clickwave (Settings > Stream overlay >
Customize) and pushed to the page live. The page scales itself to fill
the OBS source, so it renders sharp at any size: change the Browser
source's width/height instead of stretching it.
"""

import copy
import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .common import APPDATA_DIR, log

STATS_DIR = os.path.join(APPDATA_DIR, "overlay")

STATS = (("cps", "CPS"), ("peak", "Peak"), ("clicks", "Clicks"))

DEFAULT_STYLE = {
    "layout": "row",            # row | column | grid | custom
    "columns": 3,               # grid layout
    "positions": {},            # custom layout: ident -> [x, y] in key units
    "keys": {},                 # ident -> {"label": str|None, "w": float}
    "key_size": 64,             # px at 1x
    "gap": 10,
    "radius": 14,
    "font": 100,                # % of the default text size
    "key_color": "#161920",
    "key_opacity": 85,          # %
    "pressed_color": None,      # None = the app's accent color
    "text_color": "#eceef4",
    "border": True,
    "stats": ["cps", "peak", "clicks"],
    "stats_pos": "below",       # below | above | left | right | hidden
    "fit": True,                # scale to fill the browser source (sharp at any size)
    "align": "center",          # where the overlay sits when fit leaves spare room
}


def merge_style(style):
    """DEFAULT_STYLE overlaid with saved values (unknown keys dropped)."""
    out = copy.deepcopy(DEFAULT_STYLE)
    if isinstance(style, dict):
        for key, default in DEFAULT_STYLE.items():
            if key in style and (default is None or isinstance(style[key], type(default))
                                 or (key == "pressed_color" and isinstance(style[key], str))):
                out[key] = copy.deepcopy(style[key])
    out["columns"] = max(1, min(12, int(out["columns"])))
    out["key_size"] = max(24, min(200, int(out["key_size"])))
    out["gap"] = max(0, min(60, int(out["gap"])))
    out["radius"] = max(0, min(100, int(out["radius"])))
    out["font"] = max(50, min(250, int(out["font"])))
    out["key_opacity"] = max(0, min(100, int(out["key_opacity"])))
    out["stats"] = [s for s in out["stats"] if s in dict(STATS)]
    return out


def key_width(style, ident):
    w = (style["keys"].get(ident) or {}).get("w", 1)
    try:
        return max(0.5, min(6.0, float(w)))
    except (TypeError, ValueError):
        return 1.0


def key_label(style, ident, default):
    label = (style["keys"].get(ident) or {}).get("label")
    return label if isinstance(label, str) and label.strip() else default


def align_rows(items, how):
    """Line every row (keys sharing a y) up to the left / center / right of the
    widest row, in place. items: dicts with x, y, w. Rows stay intact; only
    their horizontal offset changes."""
    frac = {"left": 0.0, "center": 0.5, "right": 1.0}.get(how)
    if frac is None or not items:
        return
    rows = {}
    for k in items:
        rows.setdefault(round(k["y"] * 2), []).append(k)
    extents = {r: (min(k["x"] for k in ks), max(k["x"] + k["w"] for k in ks)) for r, ks in rows.items()}
    left = min(lo for lo, _ in extents.values())
    total = max(hi for _, hi in extents.values()) - left
    for r, ks in rows.items():
        lo, hi = extents[r]
        shift = left + (total - (hi - lo)) * frac - lo
        for k in ks:
            k["x"] += shift


def compute_layout(keys, style, normalize=True):
    """keys: [(ident, label)] -> [{id, label, x, y, w}] in key units
    (1 unit = one key plus one gap). Used by both the page and the
    in-app designer, so the preview always matches OBS.
    Automatic layouts line each row up per style["align"]; a custom (dragged)
    layout is left exactly as placed. normalize=False keeps absolute
    positions (the designer uses it while a key is being dragged)."""
    layout = style["layout"]
    out = []
    x = y = 0.0
    col = 0
    cols = style["columns"]
    for ident, label in keys:
        w = key_width(style, ident)
        if layout == "custom" and ident in style["positions"]:
            px, py = style["positions"][ident]
            out.append({"id": ident, "label": key_label(style, ident, label), "x": float(px), "y": float(py), "w": w})
            continue
        if layout == "column":
            pos = (0.0, y)
            y += 1
        elif layout == "grid":
            if col >= cols:
                col, x, y = 0, 0.0, y + 1
            pos = (x, y)
            x += w
            col += 1
        else:  # row (and custom keys without a saved spot)
            pos = (x, 0.0)
            x += w
        out.append({"id": ident, "label": key_label(style, ident, label), "x": pos[0], "y": pos[1], "w": w})
    if layout == "custom" and out:
        # keys without a saved spot go on a new row under the placed ones
        placed = [k for k in out if k["id"] in style["positions"]]
        if placed and len(placed) != len(out):
            bottom = max(k["y"] for k in placed) + 1
            x = min(k["x"] for k in placed)
            for k in out:
                if k["id"] not in style["positions"]:
                    k["x"], k["y"] = x, bottom
                    x += k["w"]
    if layout != "custom":
        align_rows(out, style["align"])
    if out and normalize:
        mx, my = min(k["x"] for k in out), min(k["y"] for k in out)
        for k in out:
            k["x"] -= mx
            k["y"] -= my
    return out


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Clickwave overlay</title>
<style>
  html, body { margin: 0; width: 100%; height: 100%; background: transparent; overflow: hidden;
    font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif; }
  #stage { position: absolute; left: 0; top: 0; transform-origin: 0 0; }
  #wrap { display: inline-flex; gap: var(--gap); padding: var(--gap); }
  #wrap.below { flex-direction: column; }
  #wrap.above { flex-direction: column-reverse; }
  #wrap.below.a-left, #wrap.above.a-left { align-items: flex-start; }
  #wrap.below.a-center, #wrap.above.a-center { align-items: center; }
  #wrap.below.a-right, #wrap.above.a-right { align-items: flex-end; }
  #wrap.right { flex-direction: row; align-items: flex-start; }
  #wrap.left  { flex-direction: row-reverse; align-items: flex-start; }
  #keys { position: relative; }
  .key { position: absolute; box-sizing: border-box; display: flex; align-items: center; justify-content: center;
    border-radius: var(--radius); color: var(--text); background: var(--keybg);
    border: var(--border); font-weight: 600; font-size: var(--font); white-space: nowrap; overflow: hidden;
    transition: background .08s ease-out, border-color .08s ease-out, transform .06s ease-out; }
  .key.down { background: var(--pressed); border-color: var(--pressed); color: #fff; transform: scale(.95); }
  #stats { display: flex; gap: var(--gap); }
  #wrap.left #stats, #wrap.right #stats { flex-direction: column; }
  .stat { background: var(--keybg); border: var(--border); border-radius: var(--radius); color: var(--text);
    padding: calc(var(--size) * .1) calc(var(--size) * .22); text-align: center; min-width: calc(var(--size) * 1.1);
    box-sizing: border-box; }
  .stat b { display: block; font-size: calc(var(--font) * 1.3); line-height: 1.1; font-variant-numeric: tabular-nums; }
  .stat span { font-size: calc(var(--font) * .62); opacity: .7; letter-spacing: .05em; text-transform: uppercase; }
  .hidden { display: none !important; }
</style></head>
<body><div id="stage"><div id="wrap" class="below"><div id="keys"></div><div id="stats"></div></div></div>
<script>
const stage = document.getElementById("stage"), wrap = document.getElementById("wrap");
const keysEl = document.getElementById("keys"), statsEl = document.getElementById("stats");
let style = null, keyEls = {}, statEls = {};
function hexA(hex, a) {
  const h = hex.replace("#", ""); const n = parseInt(h.length === 3 ? h.split("").map(c => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}
function fit() {
  if (!style) return;
  stage.style.transform = "none";
  const w = wrap.offsetWidth, h = wrap.offsetHeight;
  if (!style.fit || !w || !h) { stage.style.left = "0px"; stage.style.top = "0px"; return; }
  const s = Math.min(innerWidth / w, innerHeight / h);
  stage.style.transform = `scale(${s})`;
  const ox = style.align === "left" ? 0 : (innerWidth - w * s) / (style.align === "right" ? 1 : 2);
  stage.style.left = ox + "px"; stage.style.top = (innerHeight - h * s) / 2 + "px";
}
function render(m) {
  style = m.style;
  const r = document.documentElement.style, size = style.key_size, gap = style.gap;
  r.setProperty("--size", size + "px"); r.setProperty("--gap", gap + "px");
  r.setProperty("--radius", Math.min(style.radius, size / 2) + "px");
  r.setProperty("--font", (size * 0.27 * style.font / 100) + "px");
  r.setProperty("--keybg", hexA(style.key_color, style.key_opacity / 100));
  r.setProperty("--pressed", style.pressed_color || m.accent);
  r.setProperty("--text", style.text_color);
  r.setProperty("--border", style.border ? `${Math.max(1, size / 40)}px solid rgba(255,255,255,.14)` : "0");
  wrap.className = style.stats_pos + " a-" + style.align;
  keysEl.innerHTML = ""; keyEls = {};
  let maxX = 0, maxY = 0;
  for (const k of m.keys) {
    const d = document.createElement("div");
    d.className = "key" + (k.down ? " down" : "");
    d.textContent = k.label;
    const wpx = k.w * size + (k.w - 1) * gap;
    d.style.left = k.x * (size + gap) + "px"; d.style.top = k.y * (size + gap) + "px";
    d.style.width = wpx + "px"; d.style.height = size + "px";
    keysEl.appendChild(d); keyEls[k.id] = d;
    maxX = Math.max(maxX, k.x * (size + gap) + wpx); maxY = Math.max(maxY, k.y * (size + gap) + size);
  }
  keysEl.style.width = maxX + "px"; keysEl.style.height = maxY + "px";
  keysEl.classList.toggle("hidden", m.keys.length === 0);
  statsEl.innerHTML = ""; statEls = {};
  const names = {cps: "CPS", peak: "Peak", clicks: "Clicks"};
  for (const s of style.stats) {
    const d = document.createElement("div"); d.className = "stat";
    d.innerHTML = `<b>0</b><span>${names[s]}</span>`; statsEl.appendChild(d); statEls[s] = d.firstChild;
  }
  statsEl.classList.toggle("hidden", style.stats_pos === "hidden" || style.stats.length === 0);
  setStats(m.stats);
  requestAnimationFrame(fit);
}
function setStats(s) {
  for (const k in statEls) statEls[k].textContent = k === "clicks" ? s.clicks.toLocaleString() : s[k];
}
addEventListener("resize", fit);
function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "state") render(m);
    else if (m.type === "hit") { const el = keyEls[m.id]; if (el) el.classList.toggle("down", m.down); }
    else if (m.type === "stats") setStats(m);
  };
  es.onerror = () => { es.close(); setTimeout(connect, 1500); };
}
connect();
</script></body></html>"""


class OverlayServer:
    def __init__(self):
        self.port = None
        self._httpd = None
        self._thread = None
        self._clients = set()
        self._lock = threading.Lock()
        self._raw_keys = []
        self._down = set()
        self._style = copy.deepcopy(DEFAULT_STYLE)
        self._accent = "#7b6cff"
        self._stats = {"cps": 0, "peak": 0, "clicks": 0}

    @property
    def running(self):
        return self._httpd is not None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/" if self.port else ""

    @property
    def style(self):
        return copy.deepcopy(self._style)

    def state(self):
        keys = compute_layout(self._raw_keys, self._style)
        for k in keys:
            k["down"] = k["id"] in self._down
        return {"type": "state", "keys": keys, "stats": dict(self._stats), "accent": self._accent,
                "style": self._style}

    def start(self, port):
        if self._httpd:
            return True
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    q = queue.Queue(maxsize=500)
                    with server._lock:
                        server._clients.add(q)
                        first = json.dumps(server.state())
                    try:
                        self.wfile.write(f"data: {first}\n\n".encode())
                        self.wfile.flush()
                        while server._httpd is not None:
                            try:
                                msg = q.get(timeout=15)
                            except queue.Empty:
                                msg = None
                            if msg is None:
                                self.wfile.write(b": keepalive\n\n")
                            else:
                                self.wfile.write(f"data: {msg}\n\n".encode())
                            self.wfile.flush()
                    except (ConnectionError, OSError):
                        pass
                    finally:
                        with server._lock:
                            server._clients.discard(q)
                elif path == "/state":
                    self._send(200, "application/json", json.dumps(server.state()).encode())
                elif path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", PAGE.encode())
                else:
                    self._send(404, "text/plain", b"not found")

            def _send(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

        for candidate in range(port, port + 10):
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
                httpd.daemon_threads = True
                break
            except OSError:
                continue
        else:
            log.warning("Overlay: no free port near %d", port)
            return False
        self._httpd = httpd
        self.port = candidate
        self._thread = threading.Thread(target=httpd.serve_forever, name="ClickwaveOverlay", daemon=True)
        self._thread.start()
        log.info("Overlay running at %s", self.url)
        return True

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd:
            with self._lock:
                for q in self._clients:
                    try:
                        q.put_nowait(None)
                    except queue.Full:
                        pass
            httpd.shutdown()
            httpd.server_close()
        self.port = None

    def _broadcast(self, msg):
        text = json.dumps(msg)
        with self._lock:
            for q in list(self._clients):
                try:
                    q.put_nowait(text)
                except queue.Full:
                    pass

    def set_keys(self, keys, accent):
        """keys: [(ident, label)]"""
        self._raw_keys = list(keys)
        self._accent = accent
        self._broadcast(self.state())

    def set_style(self, style):
        self._style = merge_style(style)
        self._broadcast(self.state())

    def hit(self, ident, down):
        if down:
            self._down.add(ident)
        else:
            self._down.discard(ident)
        if self._httpd:
            self._broadcast({"type": "hit", "id": ident, "down": down})

    def stats(self, cps, peak, clicks):
        stats = {"cps": cps, "peak": peak, "clicks": clicks}
        if stats != self._stats:
            self._stats = stats
            if self._httpd:
                self._broadcast({"type": "stats", **stats})


def write_stat_files(cps, peak, session, lifetime, best):
    """Plain text files for OBS 'Text (GDI+) -> Read from file'."""
    try:
        os.makedirs(STATS_DIR, exist_ok=True)
        for name, value in (("cps", cps), ("peak", peak), ("session_clicks", session),
                            ("total_clicks", lifetime), ("best_cps", best)):
            with open(os.path.join(STATS_DIR, f"{name}.txt"), "w", encoding="utf-8") as f:
                f.write(str(value))
    except OSError as e:
        log.warning("Stat files: %s", e)

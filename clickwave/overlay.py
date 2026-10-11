"""Stream overlay: a local web page for an OBS Browser Source.

Shows your watched inputs lighting up live (an input display) plus CPS
and click counters, updated over Server-Sent Events. Only listens on
127.0.0.1, so nothing is reachable from other machines.

URL options (combine freely):
    ?keys=0        hide the input display
    ?cps=0         hide the CPS / clicks counters
    ?scale=1.5     size multiplier
    ?accent=ff8800 key highlight color (hex, no #)
    ?layout=column stack keys vertically
"""

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .common import APPDATA_DIR, log

STATS_DIR = os.path.join(APPDATA_DIR, "overlay")

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Clickwave overlay</title>
<style>
  :root { --accent: #7b6cff; --scale: 1; }
  html, body { margin: 0; background: transparent; overflow: hidden;
    font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif; }
  #wrap { display: inline-flex; flex-direction: column; gap: calc(10px * var(--scale));
    padding: calc(12px * var(--scale)); }
  #keys { display: flex; gap: calc(8px * var(--scale)); flex-wrap: wrap; }
  #keys.column { flex-direction: column; }
  .key { min-width: calc(54px * var(--scale)); height: calc(54px * var(--scale));
    padding: 0 calc(12px * var(--scale)); box-sizing: border-box;
    display: flex; align-items: center; justify-content: center; flex-direction: column;
    border-radius: calc(12px * var(--scale)); color: #eceef4;
    background: rgba(22, 25, 32, .82); border: calc(1.5px * var(--scale)) solid rgba(255,255,255,.12);
    font-weight: 600; font-size: calc(17px * var(--scale));
    transition: background .09s ease-out, border-color .09s ease-out, transform .06s ease-out; }
  .key small { font-weight: 500; font-size: calc(11px * var(--scale)); opacity: .65; margin-top: 1px; }
  .key.down { background: var(--accent); border-color: var(--accent); color: #fff; transform: scale(.96); }
  #stats { display: flex; gap: calc(8px * var(--scale)); }
  .stat { background: rgba(22, 25, 32, .82); border: calc(1.5px * var(--scale)) solid rgba(255,255,255,.12);
    border-radius: calc(12px * var(--scale)); padding: calc(6px * var(--scale)) calc(14px * var(--scale));
    color: #eceef4; text-align: center; min-width: calc(64px * var(--scale)); }
  .stat b { display: block; font-size: calc(22px * var(--scale)); line-height: 1.1; font-variant-numeric: tabular-nums; }
  .stat span { font-size: calc(11px * var(--scale)); opacity: .65; letter-spacing: .04em; text-transform: uppercase; }
  .hidden { display: none !important; }
</style></head>
<body><div id="wrap"><div id="keys"></div>
<div id="stats"><div class="stat"><b id="cps">0</b><span>CPS</span></div>
<div class="stat"><b id="peak">0</b><span>Peak</span></div>
<div class="stat"><b id="clicks">0</b><span>Clicks</span></div></div></div>
<script>
const q = new URLSearchParams(location.search);
const root = document.documentElement;
if (q.get("scale")) root.style.setProperty("--scale", q.get("scale"));
if (q.get("accent")) root.style.setProperty("--accent", "#" + q.get("accent").replace("#", ""));
if (q.get("keys") === "0") document.getElementById("keys").classList.add("hidden");
if (q.get("cps") === "0") document.getElementById("stats").classList.add("hidden");
if (q.get("layout") === "column") document.getElementById("keys").classList.add("column");
const keysEl = document.getElementById("keys");
function render(keys) {
  keysEl.innerHTML = "";
  for (const k of keys) {
    const d = document.createElement("div");
    d.className = "key" + (k.down ? " down" : "");
    d.id = "k_" + btoa(k.id).replace(/=/g, "");
    d.textContent = k.label;
    keysEl.appendChild(d);
  }
}
function setStats(s) {
  document.getElementById("cps").textContent = s.cps;
  document.getElementById("peak").textContent = s.peak;
  document.getElementById("clicks").textContent = s.clicks.toLocaleString();
}
function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "state") { if (!q.get("accent")) root.style.setProperty("--accent", m.accent); render(m.keys); setStats(m.stats); }
    else if (m.type === "hit") {
      const el = document.getElementById("k_" + btoa(m.id).replace(/=/g, ""));
      if (el) el.classList.toggle("down", m.down);
    } else if (m.type === "stats") setStats(m);
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
        self._state = {"type": "state", "keys": [], "stats": {"cps": 0, "peak": 0, "clicks": 0},
                       "accent": "#7b6cff"}

    @property
    def running(self):
        return self._httpd is not None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/" if self.port else ""

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
                        first = json.dumps(server._state)
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
                    body = json.dumps(server._state).encode()
                    self._send(200, "application/json", body)
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
        self._state["keys"] = [{"id": i, "label": l, "down": False} for i, l in keys]
        self._state["accent"] = accent
        self._broadcast(self._state)

    def hit(self, ident, down):
        for k in self._state["keys"]:
            if k["id"] == ident:
                k["down"] = down
        if self._httpd:
            self._broadcast({"type": "hit", "id": ident, "down": down})

    def stats(self, cps, peak, clicks):
        stats = {"cps": cps, "peak": peak, "clicks": clicks}
        if stats != self._state["stats"]:
            self._state["stats"] = stats
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

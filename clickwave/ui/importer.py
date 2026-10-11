"""'Import recording' dialog: turn a recording of key presses into a pack.

Detection is automatic (see clickwave/slicer.py) but everything can be
tuned or overridden:
  * sliders for sensitivity and a 'Fine tune' panel for every threshold
  * click a clip to hear it, right-click for press / release / skip /
    delete, drag a clip's edges to trim it, Shift+drag on empty space to
    add a clip by hand, scroll to zoom, drag to pan
  * a recording-quality check that explains what to change when the
    recording itself is the problem (noise, clipping, too quiet)
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog

from .. import sounds, slicer
from ..common import log
from ..decode import AUDIO_EXTENSIONS, NEEDS_WEB_MEDIA_EXTENSIONS, DecodeError, decode_any
from .theme import F, I, S, T, blend
from .widgets import (
    Dialog, Dropdown, MarkerSlider, PopupMenu, RoundedButton, ToggleSwitch, ellipsize, entry, label, link,
)

MAX_SECONDS = 600
EDGE_PX = 5


class Waveform(tk.Canvas):
    """Envelope view with clips. Wheel = zoom, drag = pan, click a clip =
    listen, right-click = menu, drag a clip edge = trim, Shift+drag on
    empty space = add a clip."""

    def __init__(self, parent, dialog):
        super().__init__(parent, height=S(160), width=S(600), bg=T.card, highlightthickness=0, bd=0)
        self.dlg = dialog
        self.analysis = None
        self.view = (0, 1)          # visible range in analysis frames
        self._hover = None          # (index, part) part: body|start|end
        self._drag = None
        self.selected = None
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag_move)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<ButtonRelease-3>", self._right)

    # ---- coordinates (analysis frames <-> pixels) ----
    def load(self, analysis):
        self.analysis = analysis
        self.view = (0, max(1, analysis.frames))
        self.redraw()

    def _x(self, frame):
        a, b = self.view
        return (frame - a) / max(1, b - a) * self.winfo_width()

    def _f(self, x):
        a, b = self.view
        return a + x / max(1, self.winfo_width()) * (b - a)

    def _hit_frames(self, h):
        win = self.analysis.win
        return h.start / win, h.end / win

    def _locate(self, x):
        if self.analysis is None:
            return None
        for i, h in enumerate(self.dlg.hits):
            fs, fe = self._hit_frames(h)
            xs, xe = self._x(fs), self._x(fe)
            if abs(x - xs) <= EDGE_PX:
                return i, "start"
            if abs(x - xe) <= EDGE_PX:
                return i, "end"
            if xs <= x <= xe:
                return i, "body"
        return None

    def _set_hover(self, hv):
        if hv != self._hover:
            self._hover = hv
            cursor = ""
            if hv:
                cursor = "sb_h_double_arrow" if hv[1] in ("start", "end") else "hand2"
            self.configure(cursor=cursor)
            self.redraw()

    def _motion(self, e):
        if not self._drag:
            self._set_hover(self._locate(e.x))

    def _press(self, e):
        shift = bool(e.state & 0x0001)
        loc = self._locate(e.x)
        if shift and (loc is None or loc[1] == "body"):
            self._drag = ("add", self._f(e.x), self._f(e.x))
        elif loc and loc[1] in ("start", "end"):
            self._drag = ("edge", loc[0], loc[1])
        else:
            self._drag = ("pan", e.x, self.view, False, loc)

    def _drag_move(self, e):
        if not self._drag or self.analysis is None:
            return
        kind = self._drag[0]
        win = self.analysis.win
        if kind == "edge":
            _, i, part = self._drag
            h = self.dlg.hits[i]
            frame = max(0, min(self.analysis.frames, self._f(e.x)))
            if part == "start":
                h.start = int(min(frame * win, h.end - 4 * win))
            else:
                h.end = int(max(frame * win, h.start + 4 * win))
            h.manual = True
            self.redraw()
        elif kind == "add":
            self._drag = ("add", self._drag[1], self._f(e.x))
            self.redraw()
        else:
            _, x0, (a, b), _, loc = self._drag
            if abs(e.x - x0) > 3:
                span = b - a
                shift = (x0 - e.x) / max(1, self.winfo_width()) * span
                a2 = max(0, min(self.analysis.frames - span, a + shift))
                self.view = (a2, a2 + span)
                self._drag = ("pan", x0, (a, b), True, loc)
                self.redraw()

    def _release(self, e):
        drag, self._drag = self._drag, None
        if not drag:
            return
        if drag[0] == "edge":
            self.dlg.hit_edited(drag[1])
        elif drag[0] == "add":
            f0, f1 = sorted((drag[1], drag[2]))
            if f1 - f0 >= 4:
                self.dlg.add_manual(int(f0), int(f1))
            self.redraw()
        elif drag[0] == "pan" and not drag[3]:
            loc = drag[4]
            if loc:
                self.selected = loc[0]
                self.dlg.preview_hit(loc[0])
                self.redraw()

    def _right(self, e):
        loc = self._locate(e.x)
        if loc:
            self.selected = loc[0]
            self.redraw()
            self.dlg.hit_menu(loc[0], e.x_root, e.y_root)

    def wheel(self, e):
        if self.analysis is None:
            return
        a, b = self.view
        center = self._f(e.x_root - self.winfo_rootx())
        factor = 0.8 if e.delta > 0 else 1.25
        span = max(60, min(self.analysis.frames, (b - a) * factor))
        a2 = max(0, min(self.analysis.frames - span, center - (center - a) * span / (b - a)))
        self.view = (a2, a2 + span)
        self.redraw()

    def zoom_to(self, i):
        h = self.dlg.hits[i]
        fs, fe = self._hit_frames(h)
        span = max(300, (fe - fs) * 6)
        a = max(0, min(self.analysis.frames - span, (fs + fe) / 2 - span / 2))
        self.view = (a, a + span)
        self.selected = i
        self.redraw()

    # ---- drawing ----
    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        mid = h / 2
        an = self.analysis
        if an is None:
            self.create_text(w / 2, mid, text="No recording loaded", fill=T.faint, font=F["small"])
            return
        a, b = self.view
        span = b - a
        win = an.win
        for i, hit in enumerate(self.dlg.hits):
            fs, fe = hit.start / win, hit.end / win
            x1, x2 = self._x(fs), self._x(fe)
            if x2 < 0 or x1 > w:
                continue
            color = T.accent if hit.kind == "press" else T.ok
            hov = self._hover and self._hover[0] == i
            sel = i == self.selected
            if not hit.enabled:
                fill, outline = blend(T.card, T.faint, 0.12), T.faint
            else:
                fill = blend(T.card, color, 0.30 if (hov or sel) else 0.16)
                outline = color if (hov or sel) else blend(T.card, color, 0.55)
            self.create_rectangle(x1, S(4), max(x1 + 2, x2), h - S(4), fill=fill, outline=outline,
                                  width=2 if sel else 1)
            if hov and self._hover[1] in ("start", "end"):
                ex = x1 if self._hover[1] == "start" else x2
                self.create_line(ex, S(4), ex, h - S(4), fill=T.text, width=2)
            if hit.manual and x2 - x1 > S(10):
                self.create_text(x1 + S(4), S(8), text="✎", anchor="nw", fill=color, font=F["tiny"])
        if self._drag and self._drag[0] == "add":
            xa, xb = sorted((self._x(self._drag[1]), self._x(self._drag[2])))
            self.create_rectangle(xa, S(4), xb, h - S(4), outline=T.accent, dash=(3, 2))
        env = an.env
        top = max(1, max(env))
        for px in range(w):
            lo = int(a + px * span / w)
            hi = max(lo + 1, int(a + (px + 1) * span / w))
            seg = env[lo:hi]
            if not seg:
                continue
            amp = max(seg) / top * (mid - S(8))
            if amp >= 1:
                self.create_line(px, mid - amp, px, mid + amp, fill=T.muted)
        self.create_line(0, mid, w, mid, fill=T.border)
        secs = span * win / an.rate
        self.create_text(w - S(6), S(6), text=f"{secs:.1f} s", anchor="ne", fill=T.faint, font=F["tiny"])


class ImportRecordingDialog(Dialog):
    def __init__(self, app, path=None):
        super().__init__(app.root, "Import recording", width=640)
        self.app = app
        self.analysis = None
        self.hits = []
        self.opts = slicer.DetectOptions()
        self.normalize = True
        self.fade_ms = 20
        self._play_jobs = []
        self._detect_job = None
        self._path = None
        b = self.body

        top = tk.Frame(b, bg=T.bg)
        top.pack(fill="x")
        RoundedButton(top, "Choose file…", self.choose, height=32, font=F["small"], icon=I.folder).pack(side="left")
        self.file_label = label(top, "", font="small", fg=T.muted)
        self.file_label.pack(side="left", padx=(S(10), 0))

        self.hint = label(b, "Record yourself pressing a key 10-20 times (Voice Recorder, OBS, Audacity,\n"
                             "your phone…) with a short pause between presses, then choose the file.\n"
                             "WAV, MP3, OGG, M4A, FLAC, WMA and video files all work.",
                          font="tiny", fg=T.faint, justify="left")
        self.hint.pack(anchor="w", pady=(S(10), S(8)))

        frame = tk.Frame(b, bg=T.border, padx=1, pady=1)
        frame.pack(fill="x")
        self.wave = Waveform(frame, self)
        self.wave.pack(fill="x")

        info = tk.Frame(b, bg=T.bg)
        info.pack(fill="x", pady=(S(6), 0))
        self.summary = label(info, "", font="small_semi")
        self.summary.pack(side="left")
        label(info, "click = listen · right-click = options · drag edges = trim · Shift+drag = add",
              font="tiny", fg=T.faint).pack(side="right")
        self.quality = label(b, "", font="tiny", fg=T.faint, justify="left", wraplength=S(600))
        self.quality.pack(anchor="w", pady=(S(4), 0))

        # --- main controls ---
        controls = tk.Frame(b, bg=T.bg)
        controls.pack(fill="x", pady=(S(10), 0))
        left = tk.Frame(controls, bg=T.bg)
        left.pack(side="left", fill="x", expand=True)
        head = tk.Frame(left, bg=T.bg)
        head.pack(fill="x")
        label(head, "Sensitivity", font="small", fg=T.muted).pack(side="left")
        label(head, "finds quieter clicks →", font="tiny", fg=T.faint).pack(side="right")
        self.sens = self._slider(left, (0, 25, 50, 75, 100), 0, 100, self.opts.sensitivity, 60,
                                 lambda v: self.set_opt("sensitivity", v))
        right = tk.Frame(controls, bg=T.bg)
        right.pack(side="left", padx=(S(16), 0), anchor="n")
        label(right, "Detect", font="small", fg=T.muted).pack(anchor="w")
        self.mode_dd = Dropdown(right, [("auto", "Auto"), ("pairs", "Press + release pairs"),
                                        ("press", "Presses only")], "auto", lambda v: self.set_opt("mode", v))
        self.mode_dd.configure(width=S(190))
        self.mode_dd.pack(anchor="w", pady=(S(4), 0))
        norm = tk.Frame(right, bg=T.bg)
        norm.pack(anchor="w", pady=(S(10), 0))
        ToggleSwitch(norm, True, self.on_normalize).pack(side="left")
        label(norm, "Even out loudness", font="small").pack(side="left", padx=(S(8), 0))

        # --- fine tune ---
        tune_head = tk.Frame(b, bg=T.bg)
        tune_head.pack(fill="x", pady=(S(8), 0))
        self.tune_link = link(tune_head, "▸ Fine tune", self.toggle_tune, font="small")
        self.tune_link.pack(side="left")
        link(tune_head, "Reset to defaults", self.reset_opts, font="tiny").pack(side="right")
        self.tune = tk.Frame(b, bg=T.bg)
        self.tune_open = False
        grid = self.tune
        specs = [
            ("group_ms", "Merge sounds closer than", "ms", (0, 15, 30, 50, 80), 0, 80, 30,
             "A keystroke can be several sounds (finger tap + bottom-out). Lower it for very fast spam."),
            ("tail_db", "Clip tail", "dB", (2, 6, 10, 15, 20), 2, 20, 6,
             "Where a clip ends: lower = let the ring-out decay fully, higher = shorter, tighter clips."),
            ("max_len_ms", "Longest clip", "ms", (80, 200, 350, 500, 800), 80, 800, 350,
             "Hard limit on clip length."),
            ("preroll_ms", "Keep before attack", "ms", (0, 3, 6, 10, 15), 0, 15, 3,
             "A few ms of lead-in keep the very start of the attack crisp."),
            ("pair_window_ms", "Release within", "ms", (60, 120, 180, 250, 400), 60, 400, 180,
             "A sound this soon after a press counts as its release (pairs / auto mode)."),
            ("decay_x10", "Separation", "", (2, 4, 6, 9, 12), 2, 12, 6,
             "How quickly a click's ring-out stops masking the next sound. Higher finds sounds inside tails."),
            ("fade_ms", "Fade out", "ms", (5, 10, 20, 35, 50), 5, 50, 20,
             "Smooths each clip's last moments so cuts are inaudible."),
        ]
        self.tune_sliders = {}
        for row_i, (key, title, unit, marks, lo, hi, default, tip) in enumerate(specs):
            cell = tk.Frame(grid, bg=T.bg)
            cell.grid(row=row_i // 2, column=row_i % 2, sticky="ew", padx=(0 if row_i % 2 == 0 else S(16), 0),
                      pady=(S(4), 0))
            grid.columnconfigure(row_i % 2, weight=1, uniform="tune")
            h2 = tk.Frame(cell, bg=T.bg)
            h2.pack(fill="x")
            label(h2, title, font="tiny", fg=T.muted).pack(side="left")
            readout = label(h2, "", font="tiny", fg=T.text)
            readout.pack(side="right")
            value = self._opt_value(key)
            slider = self._slider(cell, marks, lo, hi, value, default,
                                  lambda v, k=key, ro=readout, u=unit: self._tune_changed(k, v, ro, u))
            readout.config(text=self._fmt(key, value, unit))
            from .widgets import Tooltip
            Tooltip(slider, tip)
            self.tune_sliders[key] = (slider, readout, unit, default)

        # --- playback + name ---
        plays = tk.Frame(b, bg=T.bg)
        plays.pack(fill="x", pady=(S(10), 0))
        RoundedButton(plays, "All presses", lambda: self.play_all("press"), height=30,
                      font=F["small"], icon=I.play).pack(side="left")
        RoundedButton(plays, "All releases", lambda: self.play_all("release"), height=30,
                      font=F["small"], icon=I.play).pack(side="left", padx=(S(8), 0))
        RoundedButton(plays, "", self.stop_playing, height=30, icon=I.stop).pack(side="left", padx=(S(8), 0))

        name_row = tk.Frame(b, bg=T.bg)
        name_row.pack(fill="x", pady=(S(14), 0))
        label(name_row, "Pack name", font="small", fg=T.muted).pack(side="left")
        self.name_var = tk.StringVar(value="")
        frame, _ = entry(name_row, self.name_var)
        frame.pack(side="left", fill="x", expand=True, padx=(S(10), 0))

        self.ok_button = self.buttons("Create pack", self.create)
        self.ok_button.set_enabled(False)
        self.bind("<MouseWheel>", lambda e: self.wave.wheel(e))
        self.present()
        if path:
            self.after(50, lambda: self.load(path))

    # ---------------- helpers ----------------
    def _slider(self, parent, marks, lo, hi, value, default, command):
        s = MarkerSlider(parent, marks, lo, hi, value, default=default, command=command, snap=False, compact=True)
        s.configure(bg=T.bg)
        s.pack(fill="x")
        return s

    def _opt_value(self, key):
        if key == "decay_x10":
            return int(round(self.opts.decay_db_per_ms * 10))
        if key == "fade_ms":
            return self.fade_ms
        return int(getattr(self.opts, key))

    @staticmethod
    def _fmt(key, value, unit):
        if key == "decay_x10":
            return f"{value / 10:.1f} dB/ms"
        if key == "tail_db":
            return f"+{value} dB"
        return f"{value} {unit}".strip()

    def _tune_changed(self, key, value, readout, unit):
        readout.config(text=self._fmt(key, value, unit))
        if key == "decay_x10":
            self.set_opt("decay_db_per_ms", value / 10)
        elif key == "fade_ms":
            self.fade_ms = value
        else:
            self.set_opt(key, value)

    def toggle_tune(self):
        self.tune_open = not self.tune_open
        if self.tune_open:
            self.tune.pack(fill="x", after=self.tune_link.master)
            self.tune_link.config(text="▾ Fine tune")
        else:
            self.tune.pack_forget()
            self.tune_link.config(text="▸ Fine tune")
        self._refit()

    def _refit(self):
        self.update_idletasks()
        self.geometry(f"{max(self._width, self.winfo_reqwidth())}x{self.winfo_reqheight()}")

    def reset_opts(self):
        self.opts = slicer.DetectOptions(mode=self.opts.mode)
        self.fade_ms = 20
        self.sens.set(self.opts.sensitivity)
        for key, (slider, readout, unit, default) in self.tune_sliders.items():
            slider.set(default)
            readout.config(text=self._fmt(key, default, unit))
        self.detect()

    def set_opt(self, key, value):
        setattr(self.opts, key, value)
        if self._detect_job:
            self.after_cancel(self._detect_job)
        self._detect_job = self.after(40, self.detect)

    # ---------------- loading ----------------
    def choose(self):
        exts = " ".join("*" + e for e in AUDIO_EXTENSIONS)
        path = filedialog.askopenfilename(parent=self, title="Choose a recording",
                                          filetypes=[("Audio / video", exts), ("All files", "*.*")])
        if path:
            self.load(path)

    def load(self, path):
        self.file_label.config(text="Reading the recording…", fg=T.muted)
        self.ok_button.set_enabled(False)

        def work():
            try:
                samples, nch, rate = decode_any(path, max_seconds=MAX_SECONDS)
                analysis = slicer.Analysis(samples, nch, rate)
                self.after(0, lambda: self._loaded(path, analysis))
            except (DecodeError, OSError, EOFError, ValueError) as e:
                msg = str(e)
                self.after(0, lambda: self._failed(path, msg))
            except MemoryError:
                self.after(0, lambda: self._failed(path, "the file is too large to load"))
            except Exception as e:
                log.exception("Import decode failed")
                msg = str(e)
                self.after(0, lambda: self._failed(path, msg))

        threading.Thread(target=work, daemon=True).start()

    def _failed(self, path, message):
        if not self.winfo_exists():
            return
        self.file_label.config(text=f"Couldn't read {os.path.basename(path)}: {message}", fg=T.red_hover)
        if NEEDS_WEB_MEDIA_EXTENSIONS in message:
            self.app.explain_ogg(self)

    def _loaded(self, path, analysis):
        if not self.winfo_exists():
            return
        self.analysis = analysis
        self._path = path
        name = os.path.basename(path)
        cut = " (first 10 minutes)" if analysis.seconds >= MAX_SECONDS - 1 else ""
        self.file_label.config(text=ellipsize(F["small"], f"{name}  ·  {analysis.seconds:.1f} s{cut}", S(440)),
                               fg=T.text)
        if not self.name_var.get():
            self.name_var.set(sounds.unique_pack_name(os.path.splitext(name)[0]))
        self.hint.pack_forget()
        self.wave.load(analysis)
        self.hits = []
        self.detect()
        self._refit()

    # ---------------- detection ----------------
    def detect(self):
        self._detect_job = None
        if self.analysis is None:
            return
        manual = [h for h in self.hits if h.manual]
        auto = self.analysis.detect(self.opts)
        # Hand-made / hand-edited clips win over detected ones they overlap.
        keep = [h for h in auto if not any(h.start < m.end and m.start < h.end for m in manual)]
        self.hits = sorted(keep + manual, key=lambda h: h.start)
        slicer.classify(self.hits, self.analysis.rate, self.opts)
        self.wave.selected = None
        self.wave.redraw()
        self.update_summary()

    def update_summary(self):
        presses = sum(1 for h in self.hits if h.enabled and h.kind == "press")
        releases = sum(1 for h in self.hits if h.enabled and h.kind == "release")
        skipped = sum(1 for h in self.hits if not h.enabled)
        if not self.hits:
            text = "No clicks found - raise the sensitivity" if self.analysis else ""
        else:
            text = f"{presses} press · {releases} release" + (f" · {skipped} skipped" if skipped else "")
        self.summary.config(text=text, fg=T.text if presses else T.warn)
        self.ok_button.set_enabled(presses > 0)
        self.quality.config(**self.quality_report())

    def quality_report(self):
        """One line about the recording itself, with the fix if it's bad."""
        a = self.analysis
        if a is None:
            return {"text": ""}
        headroom = a.top_db - a.noise_db
        clipped = max(a.env) >= 32700 if a.env else False
        n = len(self.hits)
        if clipped:
            return {"text": "⚠ The recording is clipping (too loud), so clicks will sound distorted. "
                            "Fix: lower the mic/input volume (or move it back a little) and record again.",
                    "fg": T.warn}
        if headroom < 18:
            return {"text": f"⚠ Noisy recording: clicks are only {headroom:.0f} dB above the background, so some "
                            "can't be told apart from noise. Fix: record closer to the keys, in a quieter room, with "
                            "noise suppression turned off - or raise Sensitivity and check each clip.",
                    "fg": T.warn}
        if a.top_db < -45:
            return {"text": "⚠ Very quiet recording - the clips will be boosted, which also boosts hiss. "
                            "Fix: raise the mic volume or record closer.", "fg": T.warn}
        if n > 0 and a.seconds / n < 0.04:
            return {"text": "⚠ That's a lot of clicks for this length - probably noise. Lower Sensitivity "
                            "or raise 'Merge sounds closer than' in Fine tune.", "fg": T.warn}
        return {"text": f"✓ Clean recording: background {a.noise_db:.0f} dB, loudest click {a.top_db:.0f} dB "
                        f"({headroom:.0f} dB of separation).", "fg": T.faint}

    def on_normalize(self, value):
        self.normalize = value

    # ---------------- per-clip editing ----------------
    def hit_menu(self, i, x, y):
        h = self.hits[i]
        items = [
            {"header": True, "label": f"Clip {i + 1}  ·  {(h.end - h.start) / self.analysis.rate * 1000:.0f} ms"},
            {"label": "Play", "command": lambda: self.preview_hit(i)},
            {"label": "Zoom to clip", "command": lambda: self.wave.zoom_to(i)},
            {"separator": True},
            {"label": "Press", "checked": h.kind == "press", "command": lambda: self._set_kind(i, "press")},
            {"label": "Release", "checked": h.kind == "release", "command": lambda: self._set_kind(i, "release")},
            {"separator": True},
            {"label": "Include" if not h.enabled else "Skip", "command": lambda: self.toggle_hit(i)},
            {"label": "Delete", "command": lambda: self.delete_hit(i)},
        ]
        PopupMenu(self, items, x, y, min_width=S(180))

    def _set_kind(self, i, kind):
        self.hits[i].kind = kind
        self.hits[i].manual = True
        self.wave.redraw()
        self.update_summary()

    def toggle_hit(self, i):
        self.hits[i].enabled = not self.hits[i].enabled
        self.hits[i].manual = True
        self.wave.redraw()
        self.update_summary()

    def delete_hit(self, i):
        del self.hits[i]
        self.wave.selected = None
        self.wave.redraw()
        self.update_summary()

    def hit_edited(self, i):
        self.hits.sort(key=lambda h: h.start)
        self.update_summary()
        self.preview_hit(min(i, len(self.hits) - 1))

    def add_manual(self, f0, f1):
        win = self.analysis.win
        db = self.analysis.db[f0:f1 + 1] or [-120.0]
        h = slicer.Hit(f0 * win, f1 * win, max(db), kind="press", manual=True)
        self.hits = sorted(self.hits + [h], key=lambda x: x.start)
        idx = self.hits.index(h)
        self.wave.selected = idx
        self.update_summary()
        self.preview_hit(idx)

    # ---------------- preview ----------------
    def _clip(self, hit):
        was = hit.enabled
        hit.enabled = True
        try:
            p, r = slicer.extract(self.analysis, [hit], normalize=self.normalize, fade_out_ms=self.fade_ms)
        finally:
            hit.enabled = was
        return (p or r or [None])[0]

    def preview_hit(self, i):
        if not (0 <= i < len(self.hits)):
            return
        clip = self._clip(self.hits[i])
        if clip is not None:
            self.app.engine.play_samples(clip, self.analysis.nch, self.analysis.rate,
                                         volume=max(0.3, self.app.engine.volume))

    def play_all(self, kind):
        self.stop_playing()
        idxs = [i for i, h in enumerate(self.hits) if h.enabled and h.kind == kind]
        for n, i in enumerate(idxs):
            self._play_jobs.append(self.after(n * 260, lambda i=i: self.preview_hit(i)))

    def stop_playing(self):
        for job in self._play_jobs:
            self.after_cancel(job)
        self._play_jobs = []

    # ---------------- save ----------------
    def create(self):
        if self.analysis is None or not self.ok_button.enabled:
            return
        name = sounds.safe_pack_name(self.name_var.get()) or "Recorded pack"
        name = sounds.unique_pack_name(name)
        press, release = slicer.extract(self.analysis, self.hits, normalize=self.normalize, fade_out_ms=self.fade_ms)
        if not press:
            return
        try:
            sounds.save_pack(name, press, release, self.analysis.nch, self.analysis.rate)
        except OSError as e:
            self.file_label.config(text=f"Couldn't save the pack: {e}", fg=T.red_hover)
            return
        self.stop_playing()
        self.cancel()
        self.app.on_pack_created(name, f"{len(press)} press · {len(release)} release clips")

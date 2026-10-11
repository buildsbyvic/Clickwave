"""Clickwave main window."""

import copy
import ctypes
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog

from .. import sounds
from ..audio import AudioManager
from .. import updates
from ..common import (
    ISSUES_URL, REPO_URL, APP_NAME, APP_TAGLINE, APP_VERSION, APPDATA_DIR, ICON_PATH, LOG_PATH, LOGO_PATH, PACKS_DIR, log,
    migrate_from_gd_click, setup_logging,
)
from ..decode import AUDIO_EXTENSIONS, NEEDS_WEB_MEDIA_EXTENSIONS, DecodeError
from ..engine import ClickEngine
from ..inputs import input_label, split_combo
from ..overlay import STATS_DIR, OverlayServer, write_stat_files
from ..settings import DEFAULT_INPUTS, HOTKEY_ACTIONS, Settings
from ..tray import TrayIcon
from .. import winutil
from .importer import ImportRecordingDialog
from .pack_editor import PackEditor
from .theme import F, I, S, T, apply_theme, init_fonts, set_scale
from .toast import Toast
from ..errors import friendly
from .widgets import (
    ErrorDialog, Card, Chip, ChipFlow, ConfirmDialog, Dropdown, MarkerSlider, MessageDialog,
    PromptDialog, Radio, RoundedButton, ScrollPage, Segmented, Sparkline, StatusPill, Swatches,
    ToggleSwitch, Tooltip, is_descendant, label, link, popup_at_widget, separator,
)

VOLUME_MARKERS = (0, 50, 75, 100, 125, 150, 200)
RELEASE_MARKERS = (0, 25, 50, 75, 100)
LEVELS = (25, 50, 75, 125, 150, 200)
PANS = ((-100, "Left"), (-50, "Left 50%"), (0, "Center"), (50, "Right 50%"), (100, "Right"))
OUTPUT_MARKERS = (0, 25, 50, 75, 100, 125, 150)
SOFT_MARKERS = (60, 90, 120, 150, 200, 250)
CPS_TEST_SECONDS = 10
TABS = ["Main", "Sounds", "Settings"]
OGG_STORE_URL = "ms-windows-store://pdp/?ProductId=9N5TDP8VCMHS"


class App:
    def __init__(self, root, startup=False):
        self.root = root
        # Tk prints callback errors to stderr, which doesn't exist in the
        # windowed .exe - send them to the log instead.
        root.report_callback_exception = lambda *exc: log.error("UI callback error", exc_info=exc)
        self.settings = Settings()
        self.events = queue.Queue()
        self.toast = Toast(root)
        self.show_requests = winutil.ShowRequests()
        self.fg = winutil.ForegroundTracker()
        self.tray = None

        migrated = sounds.migrate_legacy_sounds()
        if migrated:
            self.settings.pset("pack", migrated)

        self.audio = AudioManager(self.settings["backend"])
        self.engine = ClickEngine(self.events, self.audio)
        self.lifetime_base = self.settings["lifetime_clicks"]
        self.overlay = OverlayServer()
        self.cps_test = None         # None | {"state": "armed"|"running", "count", "start"}
        self.test_result = None      # (text, color, until)

        # UI state
        self.capture = None          # None | "input" | ("hotkey", action)
        self.capture_token = None
        self.peak_cps = 0
        self.listen_started = None
        self.listen_total = 0.0
        self.output_note = ("", T.faint)
        self._jobs = {}
        self._tick_n = 0
        self._last_geom = None
        self._quitting = False
        self.w = {}                  # widget references for the current build
        self.update_text = self.update_color = self.update_url = None   # read by build_settings

        self.apply_engine_config()
        self.apply_devices()
        self.engine.rebuild()
        self.engine.start_listeners()
        if self.settings["overlay"]:
            self.overlay.start(self.settings["overlay_port"])

        root.title(APP_NAME)
        self.build_ui()
        self.setup_tray()
        self.sync_autostart()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind_all("<MouseWheel>", self._route_wheel)
        root.bind("<Unmap>", self._on_unmap)
        root.bind("<Configure>", self._on_configure)

        if self.settings["check_updates"]:
            root.after(4000, lambda: self.check_updates(manual=False))
        if self.settings["start_on_launch"]:
            self.set_running(True, feedback=False)
        self._poll()
        self._tick()
        self._slow_tick()

    # ============================================================
    # Engine configuration
    # ============================================================
    def apply_engine_config(self):
        s, e = self.settings, self.engine
        e.volume = s.p("volume") / 100.0
        e.release_volume = s.p("release_volume") / 100.0
        e.pack = s.p("pack")
        e.pitch = s.p("pitch_variation")
        e.humanize = s.p("humanize")
        e.overrides = copy.deepcopy(s.p("overrides"))
        e.set_inputs(s.p("inputs"))
        e.app_filter = frozenset(s.p("apps")) if (s.p("app_filter") and s.p("apps")) else None
        e.hotkeys = {combo: action for action, combo in s["hotkeys"].items() if combo}
        e.dynamics = s.p("dynamics")
        e.soft_s = s.p("soft_ms") / 1000.0

    def apply_devices(self):
        s, a = self.settings, self.audio
        notes = []
        monitor = a.default()
        if s["monitor_device"]:
            found = a.find(s["monitor_device"])
            if found is None:
                notes.append(("Selected speakers aren't connected - using system default.", T.warn))
            else:
                monitor = found
        st = s["stream_device"]
        if st == "auto":
            stream = a.find_cable()
        elif st is None:
            stream = None
        else:
            stream = a.find(st)
            if stream is None:
                notes.append(("Selected stream device isn't connected.", T.warn))
        keys = []
        if s["hear_myself"] and monitor:
            keys.append(monitor)
        if s["send_to_stream"] and stream and stream not in keys:
            keys.append(stream)
        self.engine.output_keys = keys
        self.engine.cue_key = monitor
        levels = {}
        if stream:
            levels[stream] = s["stream_level"] / 100.0
        if monitor:
            levels[monitor] = s["monitor_level"] / 100.0
        self.engine.device_levels = levels
        self.output_keys = keys
        if not keys:
            notes.append(("No output enabled - clicks are muted.", T.warn))
        elif s["send_to_stream"] and stream:
            notes.append(("✓ Routing clicks to the stream device", T.ok))
        elif st == "auto" and stream is None and s["send_to_stream"]:
            notes.append(("No virtual cable found (VB-CABLE / Voicemeeter).", T.faint))
        self.output_note = notes[0] if notes else ("", T.faint)

    def rebuild_soon(self, delay=30):
        self._after("rebuild", delay, self.engine.rebuild)

    def _after(self, name, delay, fn):
        job = self._jobs.pop(name, None)
        if job:
            self.root.after_cancel(job)

        def run():
            self._jobs.pop(name, None)
            fn()
        self._jobs[name] = self.root.after(delay, run)

    def schedule_save(self):
        self._after("save", 400, self.save)

    def save(self):
        self.settings["lifetime_clicks"] = self.lifetime_base + self.engine.lifetime
        if not self.settings.save() and not getattr(self, "_save_warned", False):
            self._save_warned = True
            self.root.after(0, lambda: self.show_error(getattr(self.settings, "last_error", ""),
                                                       "Couldn't save your settings"))

    # ============================================================
    # UI construction
    # ============================================================
    def build_ui(self):
        s = self.settings
        apply_theme(s["theme"], s["accent"])
        root = self.root
        scroll_pos = [p.canvas.yview()[0] for p in self.w.get("pages", [])]
        for child in root.winfo_children():
            if not isinstance(child, tk.Toplevel):
                child.destroy()
        self.w = {}
        root.configure(bg=T.bg)

        outer = tk.Frame(root, bg=T.bg)
        outer.pack(fill="both", expand=True, padx=(S(16), S(8)), pady=(S(14), S(8)))
        header = tk.Frame(outer, bg=T.bg)
        header.pack(fill="x", padx=(0, S(8)))
        logo = self.logo_image()
        if logo is not None:
            tk.Label(header, image=logo, bg=T.bg, bd=0).pack(side="left", anchor="n", padx=(0, S(10)), pady=(S(3), 0))
        titles = tk.Frame(header, bg=T.bg)
        titles.pack(side="left")
        label(titles, APP_NAME, font="title").pack(anchor="w")
        self.w["subtitle"] = label(titles, "", font="small", fg=T.muted)
        self.w["subtitle"].pack(anchor="w")
        self.w["pill"] = StatusPill(header)
        self.w["pill"].pack(side="right", anchor="n", pady=(S(6), 0))

        self.w["tabs"] = Segmented(outer, TABS, self.show_tab, index=s["tab"])
        self.w["tabs"].pack(fill="x", pady=(S(12), S(10)), padx=(0, S(8)))

        footer = tk.Frame(outer, bg=T.bg)
        footer.pack(side="bottom", fill="x", pady=(S(6), 0), padx=(0, S(8)))
        label(footer, f"v{APP_VERSION}", font="tiny", fg=T.faint).pack(side="left")
        self.w["footer"] = label(footer, "", font="tiny", fg=T.faint)
        self.w["footer"].pack(side="right")

        holder = tk.Frame(outer, bg=T.bg)
        holder.pack(fill="both", expand=True)
        pages = [ScrollPage(holder) for _ in TABS]
        self.w["pages"] = pages
        for builder, page in zip((self.build_main, self.build_sounds, self.build_settings), pages):
            builder(page.inner)
        self.show_tab(s["tab"])
        self.refresh_state()
        winutil.style_title_bar(root, T.dark, T.bg, T.text)
        if scroll_pos:
            root.after(30, lambda: [p.scroll_to(y) for p, y in zip(pages, scroll_pos)])

    def show_tab(self, index):
        self.settings["tab"] = index
        for i, page in enumerate(self.w["pages"]):
            if i == index:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        self.schedule_save()

    def card(self, page, title=None, first=False):
        card = Card(page)
        card.pack(fill="x", pady=(0 if first else S(10), 0), padx=(0, S(4)))
        inner = card.inner
        head = None
        if title:
            head = tk.Frame(inner, bg=T.card)
            head.pack(fill="x")
            label(head, title, font="semi").pack(side="left")
        return inner, head

    def row(self, inner, title, subtitle=None, pady=(S(10), 0)):
        row = tk.Frame(inner, bg=T.card)
        row.pack(fill="x", pady=pady)
        text = tk.Frame(row, bg=T.card)
        text.pack(side="left", fill="x", expand=True)
        label(text, title).pack(anchor="w")
        if subtitle:
            label(text, subtitle, font="tiny", fg=T.faint, justify="left").pack(anchor="w")
        return row

    def toggle_row(self, inner, title, key, subtitle=None, command=None, profile=False):
        row = self.row(inner, title, subtitle)
        value = self.settings.p(key) if profile else self.settings[key]

        def changed(v):
            if profile:
                self.settings.pset(key, v)
            else:
                self.settings[key] = v
            if command:
                command(v)
            self.schedule_save()
        sw = ToggleSwitch(row, value, changed)
        sw.pack(side="right")
        return sw

    # ---------------- Main tab ----------------
    def build_main(self, page):
        s = self.settings

        # Profile bar
        bar = tk.Frame(page, bg=T.bg)
        bar.pack(fill="x", pady=(0, S(10)), padx=(0, S(4)))
        label(bar, "Profile", font="small", fg=T.muted).pack(side="left")
        dd = Dropdown(bar, [(n, n) for n in s.profile_names()], s.profile_name, self.switch_profile, height=32)
        dd.pack(side="left", fill="x", expand=True, padx=(S(10), S(8)))
        more = RoundedButton(bar, "", None, height=32, icon=I.more)
        more.command = lambda: self.profile_menu(more)
        more.pack(side="right")
        Tooltip(more, "New, duplicate, rename or delete profiles")

        if not s["onboarded"]:
            self.build_welcome(page)

        # Control
        inner, _ = self.card(page, first=s["onboarded"])
        self.w["start"] = RoundedButton(inner, "Start listening", self.toggle_running, height=46,
                                        font=F["button"], variant="success", radius=11)
        self.w["start"].pack(fill="x")
        stats = tk.Frame(inner, bg=T.card, cursor="hand2")
        stats.pack(fill="x", pady=(S(12), 0))
        self.w["stats"] = {}
        for i, (key, text) in enumerate((("cps", "CPS"), ("peak", "Peak"), ("session", "Session"),
                                         ("lifetime", "All-time"))):
            stats.columnconfigure(i, weight=1, uniform="stats")
            cell = tk.Frame(stats, bg=T.card, cursor="hand2")
            cell.grid(row=0, column=i, sticky="ew")
            value = label(cell, "0", font="stat", cursor="hand2")
            value.pack()
            cap = label(cell, text, font="tiny", fg=T.faint, cursor="hand2")
            cap.pack()
            self.w["stats"][key] = value
            for wd in (cell, value, cap):
                wd.bind("<ButtonRelease-1>", lambda e: self.reset_stats())
                Tooltip(wd, self.stats_tooltip)
        self.w["spark"] = Sparkline(inner)
        self.w["spark"].pack(fill="x", pady=(S(10), 0))
        foot = tk.Frame(inner, bg=T.card)
        foot.pack(fill="x", pady=(S(8), 0))
        self.w["hint"] = label(foot, "", font="small", fg=T.faint)
        self.w["hint"].pack(side="left")
        test = RoundedButton(foot, "CPS test", self.start_cps_test, height=28, font=F["small"], icon=I.bolt)
        test.pack(side="right")
        Tooltip(test, lambda: f"{CPS_TEST_SECONDS}-second clicks-per-second test using your watched inputs\n"
                              f"Personal best: {self.settings['best_test']:.1f} CPS")

        # Volume
        inner, head = self.card(page, "Volume")
        test = RoundedButton(head, "Test", self.preview_sound, height=28, radius=8, font=F["small"], icon=I.play)
        test.pack(side="right")
        Tooltip(test, "Play a press + release at the current volume")
        self.w["vol_readout"] = label(head, "", font="readout")
        self.w["vol_readout"].pack(side="right", padx=(0, S(10)))
        self.w["vol"] = MarkerSlider(inner, VOLUME_MARKERS, 0, 200, s.p("volume"), center=100, default=100,
                                     command=self.on_volume, snap=s["snap"])
        self.w["vol"].pack(fill="x", pady=(S(6), 0))
        label(inner, "Snaps to markers · Shift = fine · scroll to step · double-click = 100%",
              font="tiny", fg=T.faint).pack(anchor="w")
        separator(inner).pack(fill="x", pady=S(12))
        head = tk.Frame(inner, bg=T.card)
        head.pack(fill="x")
        label(head, "Release click", font="semi").pack(side="left")
        self.w["rel_readout"] = label(head, "", font="semi")
        self.w["rel_readout"].pack(side="right")
        label(inner, "Level of the key-up sound, relative to the press", font="tiny", fg=T.faint).pack(anchor="w")
        self.w["rel"] = MarkerSlider(inner, RELEASE_MARKERS, 0, 100, s.p("release_volume"),
                                     command=self.on_release_volume, snap=s["snap"])
        self.w["rel"].pack(fill="x", pady=(S(6), 0))
        self.update_readouts()

        # Inputs
        inner, head = self.card(page, "Watched inputs")
        link(head, "Reset", self.reset_inputs).pack(side="right")
        self.w["chips"] = ChipFlow(inner)
        self.w["chips"].pack(fill="x", pady=(S(10), 0))
        self.w["input_hint"] = label(inner, "", font="tiny", fg=T.faint, justify="left")
        self.w["input_hint"].pack(anchor="w", pady=(S(8), 0))
        separator(inner).pack(fill="x", pady=S(12))
        row = self.row(inner, "Only click in these apps", "Stay silent while you type in Discord, etc.",
                       pady=(0, 0))
        ToggleSwitch(row, s.p("app_filter"), self.on_app_filter).pack(side="right")
        self.w["apps"] = ChipFlow(inner)
        self.w["apps"].pack(fill="x", pady=(S(8), 0))
        self.refresh_chips()
        self.refresh_apps()

    # ---------------- Sounds tab ----------------
    def build_sounds(self, page):
        s = self.settings
        inner, head = self.card(page, "Sound packs", first=True)
        link(head, "Open packs folder", self.open_packs_folder).pack(side="right")
        label(inner, "Pick the sound for this profile. Click an input on the Main tab\n"
                     "to give just that input its own pack or level.",
              font="tiny", fg=T.faint, justify="left").pack(anchor="w", pady=(S(4), S(6)))
        self.w["packs"] = tk.Frame(inner, bg=T.card)
        self.w["packs"].pack(fill="x")
        self.refresh_packs()

        inner, _ = self.card(page, "Add sounds")
        for icon, title, sub, cmd in (
            (I.mic, "Import a recording", "Auto-chops a recording of you pressing a key into clips",
             self.import_recording),
            (I.package, "Import a pack (.zip)", "GD clickpacks, Mechvibes packs, or packs shared from Clickwave",
             self.import_zip),
            (I.folder, "Import a pack folder", "Same as above, for an unzipped folder",
             self.import_folder),
            (I.files, "New pack from audio files", "WAV, MP3, M4A, FLAC... silence trimmed automatically",
             self.import_files),
        ):
            row = tk.Frame(inner, bg=T.card)
            row.pack(fill="x", pady=(S(10), 0))
            RoundedButton(row, title, cmd, height=34, width=196, font=F["small"], icon=icon).pack(side="left")
            label(row, sub, font="tiny", fg=T.faint, justify="left", wraplength=S(140)).pack(
                side="left", padx=(S(10), 0), fill="x")
        self.w["pack_status"] = label(inner, "", font="tiny", fg=T.ok, justify="left")
        self.w["pack_status"].pack(anchor="w", pady=(S(10), 0))

        inner, _ = self.card(page, "Sound options")
        self.toggle_row(inner, "Spam dynamics", "dynamics",
                        "Fast taps play softer clicks, like a real switch. Uses the\n"
                        "pack's soft clicks, or generates them if it has none.",
                        command=lambda v: self.on_dynamics(), profile=True)
        head = tk.Frame(inner, bg=T.card)
        head.pack(fill="x", pady=(S(10), 0))
        label(head, "Soft when taps are closer than", font="small", fg=T.muted).pack(side="left")
        self.w["soft_readout"] = label(head, f"{s.p('soft_ms')} ms", font="small_semi")
        self.w["soft_readout"].pack(side="right")
        self.w["soft"] = MarkerSlider(inner, SOFT_MARKERS, 40, 250, s.p("soft_ms"), default=120,
                                      command=self.on_soft_ms, snap=s["snap"], compact=True)
        self.w["soft"].pack(fill="x")
        separator(inner).pack(fill="x", pady=(S(8), 0))
        self.toggle_row(inner, "Pitch variation", "pitch_variation", "Subtle random pitch so repeats sound natural",
                        command=lambda v: self.rebuild_soon(0), profile=True)
        self.toggle_row(inner, "Humanize volume", "humanize", "Small random loudness changes per click",
                        command=lambda v: self.rebuild_soon(0), profile=True)
        label(inner, "These apply to the current profile.", font="tiny", fg=T.faint).pack(anchor="w", pady=(S(10), 0))

    # ---------------- Settings tab ----------------
    def build_settings(self, page):
        s = self.settings

        inner, head = self.card(page, "Audio output", first=True)
        self.w["latency"] = label(head, "", font="tiny", fg=T.faint)
        self.w["latency"].pack(side="right")
        row = self.row(inner, "Output engine")
        dd = Dropdown(row, [("auto", "Low latency (WASAPI)"), ("legacy", "Compatibility (waveOut)")],
                      "legacy" if s["backend"] == "legacy" else "auto", self.on_backend, height=30)
        dd.configure(width=S(190))
        dd.pack(side="right")
        separator(inner).pack(fill="x", pady=(S(12), 0))
        self.toggle_row(inner, "Hear clicks myself", "hear_myself", "Your speakers / headphones",
                        command=lambda v: self.on_devices_changed())
        self.w["monitor_dd"] = self.device_row(inner, self.on_monitor, "monitor")
        self.level_slider(inner, "My level", "monitor_level")
        separator(inner).pack(fill="x", pady=(S(12), 0))
        self.toggle_row(inner, "Send clicks to stream", "send_to_stream", "Virtual cable that OBS captures",
                        command=lambda v: self.on_devices_changed())
        self.w["stream_dd"] = self.device_row(inner, self.on_stream, "stream")
        self.level_slider(inner, "Stream level", "stream_level")
        foot = tk.Frame(inner, bg=T.card)
        foot.pack(fill="x", pady=(S(10), 0))
        self.w["output_status"] = label(foot, "", font="tiny", fg=T.faint, justify="left", wraplength=S(240))
        self.w["output_status"].pack(side="left")
        Tooltip(self.w["output_status"], self.output_tooltip)
        link(foot, "Refresh devices", self.refresh_devices, font="tiny").pack(side="right", anchor="n")
        self.refresh_output()

        inner, _ = self.card(page, "Hotkeys")
        label(inner, "Work while the game is focused. Modifiers like Ctrl + Shift are supported.",
              font="tiny", fg=T.faint).pack(anchor="w", pady=(S(4), 0))
        self.w["hotkeys"] = {}
        for action, title in HOTKEY_ACTIONS:
            row = self.row(inner, title)
            holder = tk.Frame(row, bg=T.card)
            holder.pack(side="right")
            self.w["hotkeys"][action] = holder
            self.refresh_hotkey(action)

        inner, _ = self.card(page, "Stream overlay")
        self.toggle_row(inner, "Input display + CPS for OBS", "overlay",
                        "Your keys light up live on stream, with CPS and clicks",
                        command=self.on_overlay)
        self.w["overlay_box"] = tk.Frame(inner, bg=T.card)
        self.w["overlay_box"].pack(fill="x")
        self.build_overlay_box()
        separator(inner).pack(fill="x", pady=(S(12), 0))
        row = self.row(inner, "Write stats to text files", "For OBS Text sources (Read from file)")
        ToggleSwitch(row, s["stats_files"], lambda v: self.set_global("stats_files", v)).pack(side="right")
        link(inner, "Open stats folder", self.open_stats_folder, font="tiny").pack(anchor="w", pady=(S(6), 0))

        inner, _ = self.card(page, "Feedback")
        row = self.row(inner, "On-screen popup", "Shows hotkey changes over your game")
        ToggleSwitch(row, s["toast"], lambda v: self.set_global("toast", v)).pack(side="right")
        row = self.row(inner, "Popup position")
        dd = Dropdown(row, [("top", "Top center"), ("bottom", "Bottom center"), ("top-right", "Top right"),
                            ("bottom-right", "Bottom right")], s["toast_position"],
                      lambda v: (self.set_global("toast_position", v),
                                 self.toast.show("Popup position", T.accent, v)), height=30)
        dd.configure(width=S(150))
        dd.pack(side="right")
        self.toggle_row(inner, "Sound cue on start / stop", "cue_sound",
                        "Soft blips on your speakers only - never sent to the stream")

        inner, _ = self.card(page, "Window & startup")
        self.toggle_row(inner, "Always on top", "always_on_top",
                        command=lambda v: self.root.attributes("-topmost", v))
        self.toggle_row(inner, "Start listening on launch", "start_on_launch")
        self.toggle_row(inner, "Show tray icon", "tray", command=lambda v: self.setup_tray())
        self.toggle_row(inner, "Minimize to tray", "minimize_to_tray")
        self.toggle_row(inner, "Close button hides to tray", "close_to_tray", "Quit from the tray menu instead")
        self.toggle_row(inner, "Launch with Windows", "launch_on_startup", command=lambda v: self.sync_autostart())
        self.toggle_row(inner, "Start minimized", "start_minimized", "When launched with Windows or manually")

        inner, _ = self.card(page, "Appearance")
        row = self.row(inner, "Theme")
        dd = Dropdown(row, [("dark", "Dark"), ("light", "Light")], s["theme"], self.on_theme, height=30)
        dd.configure(width=S(120))
        dd.pack(side="right")
        row = self.row(inner, "Accent color")
        Swatches(inner, s["accent"], self.on_accent).pack(anchor="w", pady=(S(6), 0))
        self.toggle_row(inner, "Snap sliders to markers", "snap", command=self.on_snap)

        inner, head = self.card(page, "About")
        label(head, f"{APP_NAME} {APP_VERSION}", font="tiny", fg=T.faint).pack(side="right")
        web = tk.Frame(inner, bg=T.card)
        web.pack(fill="x", pady=(S(10), 0))
        RoundedButton(web, "GitHub page", lambda: winutil.open_path(REPO_URL), height=30, font=F["small"],
                      icon=I.open).pack(side="left")
        report = RoundedButton(web, "Report a problem", self.report_problem, height=30, font=F["small"])
        report.pack(side="left", padx=(S(8), 0))
        Tooltip(report, "Opens a short form on GitHub with your Clickwave version and setup already\n"
                        "filled in - just describe what happened. Nothing is sent until you submit.")
        RoundedButton(web, "Check for updates", lambda: self.check_updates(manual=True), height=30,
                      font=F["small"], icon=I.refresh).pack(side="left", padx=(S(8), 0))
        self.w["update_status"] = label(inner, self.update_text or "", font="tiny",
                                        fg=self.update_color or T.faint, justify="left", wraplength=S(330))
        self.w["update_status"].pack(anchor="w", pady=(S(6), 0))
        if self.update_url:
            self._make_update_link()
        self.toggle_row(inner, "Check for updates on launch", "check_updates",
                        "Only asks GitHub for the latest version number")
        btns = tk.Frame(inner, bg=T.card)
        btns.pack(fill="x", pady=(S(12), 0))
        RoundedButton(btns, "Data folder", lambda: winutil.open_path(APPDATA_DIR), height=30,
                      font=F["small"], icon=I.folder).pack(side="left")
        RoundedButton(btns, "Log", self.open_log, height=30, font=F["small"], icon=I.open).pack(
            side="left", padx=(S(8), 0))
        link(btns, "Reset all settings", self.reset_settings, font="small", fg=T.red_hover).pack(side="right")
        fair = tk.Frame(inner, bg=T.card)
        fair.pack(fill="x", pady=(S(12), 0))
        label(fair, "Fair play", font="small_semi").pack(anchor="w")
        label(fair, "Clicks only play for physical key and mouse presses, at the moment they happen. "
                    "Input simulated by macros or bots is ignored, packs can't add delay (silence before "
                    "a click is trimmed), and previews never reach the stream output.",
              font="tiny", fg=T.faint, justify="left", wraplength=S(330)).pack(anchor="w", pady=(S(2), 0))
        label(inner, "Settings, packs and the log live in %APPDATA%\\Clickwave.",
              font="tiny", fg=T.faint).pack(anchor="w", pady=(S(10), 0))

    # ============================================================
    # State refresh
    # ============================================================
    def refresh_state(self):
        if "pill" not in self.w:
            return
        running = self.engine.running
        state = "stopped"
        if running:
            state = "listening"
            if self.engine.app_filter is not None:
                pid, exe = self.fg.current()
                if pid != winutil.OWN_PID and exe not in self.engine.app_filter:
                    state = "paused"
        if self.w["pill"].state != state:
            self.w["pill"].set(state)
        btn = self.w["start"]
        want = "Stop listening" if running else "Start listening"
        if btn._text != want:
            btn.style(want, variant="danger" if running else "success")
        profiles = self.settings.profile_names()
        sub = f"Profile: {self.settings.profile_name}" if len(profiles) > 1 else APP_TAGLINE
        if self.w["subtitle"].cget("text") != sub:
            self.w["subtitle"].config(text=sub)
        if self.tray:
            self.tray.update(running, profiles, self.settings.profile_name)

    def level_slider(self, inner, title, key):
        head = tk.Frame(inner, bg=T.card)
        head.pack(fill="x", pady=(S(8), 0))
        label(head, title, font="small", fg=T.muted).pack(side="left")
        readout = label(head, f"{self.settings[key]}%", font="small_semi")
        readout.pack(side="right")

        def changed(v):
            self.settings[key] = v
            readout.config(text=f"{v}%")
            self.apply_devices()
            self.rebuild_soon()
            self.schedule_save()
        MarkerSlider(inner, OUTPUT_MARKERS, 0, 150, self.settings[key], default=100, command=changed,
                     snap=self.settings["snap"], compact=True).pack(fill="x")

    def on_dynamics(self):
        self.apply_engine_config()
        self.rebuild_soon(0)

    def on_soft_ms(self, value):
        self.settings.pset("soft_ms", value)
        self.engine.soft_s = value / 1000.0
        self.w["soft_readout"].config(text=f"{value} ms")
        self.schedule_save()

    # ---------------- overlay ----------------
    def build_overlay_box(self):
        box = self.w.get("overlay_box")
        if box is None:
            return
        for child in box.winfo_children():
            child.destroy()
        if not self.overlay.running:
            if self.settings["overlay"]:
                label(box, f"\u26a0 Couldn't start the overlay: ports {self.settings['overlay_port']}-"
                           f"{self.settings['overlay_port'] + 9} are all in use by other programs. Close the "
                           "program using them (or restart your PC), then turn this off and on again.", font="tiny",
                      wraplength=S(320), justify="left",
                      fg=T.warn).pack(anchor="w", pady=(S(6), 0))
            return
        row = tk.Frame(box, bg=T.card)
        row.pack(fill="x", pady=(S(8), 0))
        url = tk.Entry(row, font=F["small"], bg=T.raised, fg=T.text, relief="flat", bd=S(5),
                       readonlybackground=T.raised, highlightthickness=1, highlightbackground=T.outline)
        url.insert(0, self.overlay.url)
        url.configure(state="readonly")
        url.pack(side="left", fill="x", expand=True)
        RoundedButton(row, "", self.copy_overlay_url, height=30, icon=I.copy).pack(side="left", padx=(S(6), 0))
        RoundedButton(row, "", lambda: winutil.open_path(self.overlay.url), height=30, icon=I.open).pack(
            side="left", padx=(S(6), 0))
        label(box, "In OBS: Sources > + > Browser, paste the URL, size 600 x 220. "
                   "Add ?keys=0, ?cps=0, ?scale=1.5 or ?layout=column to customize it.",
              font="tiny", fg=T.faint, justify="left", wraplength=S(320)).pack(anchor="w", pady=(S(6), 0))

    def on_overlay(self, value):
        if value:
            self.overlay.start(self.settings["overlay_port"])
            self.overlay.set_keys([(i, input_label(i)) for i in self.settings.p("inputs")], T.accent)
        else:
            self.overlay.stop()
        self.build_overlay_box()

    def copy_overlay_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.overlay.url)
        self.notify_inline("Overlay URL copied - paste it into an OBS Browser source")

    def open_stats_folder(self):
        os.makedirs(STATS_DIR, exist_ok=True)
        winutil.open_path(STATS_DIR)

    def stats_tooltip(self):
        s = self.settings
        return (f"All-time best CPS: {max(s['best_cps'], self.peak_cps)}\n"
                f"CPS test best: {s['best_test']:.1f}\n"
                "Click to reset session stats")

    # ---------------- CPS test ----------------
    def start_cps_test(self):
        if not self.settings.p("inputs"):
            self.notify_inline("Add a watched input first")
            return
        self.cps_test = {"state": "armed", "count": 0, "start": None}
        self.test_result = None
        self.update_hint()

    def cps_test_hit(self):
        t = self.cps_test
        if t is None:
            return
        if t["state"] == "armed":
            t["state"], t["start"], t["count"] = "running", time.perf_counter(), 1
        else:
            t["count"] += 1

    def cps_test_text(self):
        t = self.cps_test
        if t is not None:
            if t["state"] == "armed":
                return f"CPS test: start pressing your inputs ({CPS_TEST_SECONDS} s)", T.accent
            left = CPS_TEST_SECONDS - (time.perf_counter() - t["start"])
            if left <= 0:
                self.finish_cps_test()
            else:
                return f"CPS test: {left:4.1f} s left  \u00b7  {t['count']} clicks", T.accent
        if self.test_result and time.time() < self.test_result[2]:
            return self.test_result[0], self.test_result[1]
        return None, None

    def finish_cps_test(self):
        t, self.cps_test = self.cps_test, None
        cps = t["count"] / CPS_TEST_SECONDS
        best = self.settings["best_test"]
        if cps > best:
            self.settings["best_test"] = round(cps, 2)
            text = f"\u2605 New best: {cps:.1f} CPS ({t['count']} clicks)"
            color = T.ok
            self.schedule_save()
        else:
            text = f"CPS test: {cps:.1f} CPS ({t['count']} clicks)  \u00b7  best {best:.1f}"
            color = T.text
        self.test_result = (text, color, time.time() + 8)
        self.notify(text, color)

    def update_readouts(self):
        self.w["vol_readout"].config(text=f"{self.settings.p('volume')}%")
        rv = self.settings.p("release_volume")
        self.w["rel_readout"].config(text="Off" if rv == 0 else f"{rv}%", fg=T.faint if rv == 0 else T.text)

    def update_hint(self):
        text, color = self.cps_test_text()
        if text:
            if self.w["hint"].cget("text") != text or self.w["hint"].cget("fg") != color:
                self.w["hint"].config(text=text, fg=color)
            return
        if self.w["hint"].cget("fg") != T.faint:
            self.w["hint"].config(fg=T.faint)
        hk = self.settings["hotkeys"].get("toggle")
        parts = [f"{input_label(hk)} starts / stops" if hk else "No start/stop hotkey set"]
        total = self.listen_total + (time.time() - self.listen_started if self.listen_started else 0)
        if total >= 1:
            m, sec = divmod(int(total), 60)
            h, m = divmod(m, 60)
            parts.append(f"listening {h}:{m:02d}:{sec:02d}" if h else f"listening {m}:{sec:02d}")
        text = "  ·  ".join(parts)
        if self.w["hint"].cget("text") != text:
            self.w["hint"].config(text=text)

    # ============================================================
    # Running / stats
    # ============================================================
    def toggle_running(self):
        self.set_running(not self.engine.running, feedback=False)

    def set_running(self, running, feedback=True):
        self.engine.running = running
        if running:
            self.listen_started = time.time()
        elif self.listen_started:
            self.listen_total += time.time() - self.listen_started
            self.listen_started = None
        if self.settings["cue_sound"]:
            self.engine.play_cue(running)
        if feedback:
            self.notify("Listening" if running else "Stopped", T.ok if running else T.faint)
        self.refresh_state()

    def notify(self, text, color=None):
        if self.settings["toast"]:
            self.toast.show(text, color, self.settings["toast_position"])

    def reset_stats(self):
        self.engine.reset_stats()
        self.peak_cps = 0
        self.listen_total = 0.0
        if self.listen_started:
            self.listen_started = time.time()
        self.w["spark"].clear()

    # ============================================================
    # Volume
    # ============================================================
    def on_volume(self, value):
        self.settings.pset("volume", value)
        self.engine.volume = value / 100.0
        self.update_readouts()
        self.rebuild_soon()
        self.schedule_save()

    def on_release_volume(self, value):
        self.settings.pset("release_volume", value)
        self.engine.release_volume = value / 100.0
        self.update_readouts()
        self.rebuild_soon()
        self.schedule_save()

    def preview_sound(self):
        def play():
            self.engine.preview(None, "press")
            self.root.after(110, lambda: self.engine.preview(None, "release"))
        if "rebuild" in self._jobs:
            self.root.after_cancel(self._jobs.pop("rebuild"))
            self.engine.rebuild()
            self.root.after(120, play)
        else:
            play()

    def on_snap(self, value):
        self.w["vol"].snap = value
        self.w["rel"].snap = value

    def _route_wheel(self, e):
        w = self.root.winfo_containing(e.x_root, e.y_root)
        if w is None:
            return
        if isinstance(w, MarkerSlider):
            w.wheel(e)
            return
        for page in self.w.get("pages", []):
            if is_descendant(w, page):
                page.scroll(e.delta)
                return

    # ============================================================
    # Inputs
    # ============================================================
    def refresh_chips(self):
        flow = self.w["chips"]
        flow.clear()
        self.w["chip_map"] = {}
        overrides = self.settings.p("overrides")
        for ident in self.settings.p("inputs"):
            chip = Chip(flow, input_label(ident), on_remove=lambda i=ident: self.remove_input(i),
                        on_menu=lambda c, i=ident: self.input_menu(c, i), marked=ident in overrides)
            Tooltip(chip, lambda i=ident: self.input_tooltip(i))
            self.w["chip_map"][ident] = chip
        if self.capture == "input":
            self.w["listening_chip"] = Chip(flow, "Listening…", on_click=self.cancel_capture, style="listening")
        else:
            self.w["listening_chip"] = None
            Chip(flow, "+  Add input", on_click=self.begin_input_capture, style="add")
        flow.relayout()
        self.set_input_hint()
        self.overlay.set_keys([(i, input_label(i)) for i in self.settings.p("inputs")], T.accent)

    def input_tooltip(self, ident):
        lines = [input_label(ident), f"Pressed {self.engine.counts.get(ident, 0):,} times this session"]
        ov = self.settings.p("overrides").get(ident)
        if ov:
            if ov.get("pack"):
                lines.append(f"Sound pack: {ov['pack']}")
            if ov.get("volume") is not None:
                lines.append(f"Level: {ov['volume']}% of main volume")
            if ov.get("pan"):
                lines.append("Pan: " + dict(PANS).get(ov["pan"], f"{ov['pan']}"))
        lines.append("Click for sound options")
        return "\n".join(lines)

    def set_input_hint(self, text=None, color=None, temporary=False):
        job = self._jobs.pop("hint", None)
        if job:
            self.root.after_cancel(job)
        if text is None:
            if self.capture == "input":
                text, color = "Press any key or mouse button…  (Esc to cancel)", T.accent
            elif not self.settings.p("inputs"):
                text, color = "No inputs watched - nothing will click. Add one above.", T.warn
            else:
                text, color = "Click an input for its own pack/level · × removes it", T.faint
        if "input_hint" in self.w:
            self.w["input_hint"].config(text=text, fg=color or T.faint)
        if temporary:
            self._after("hint", 2600, self.set_input_hint)

    def input_menu(self, chip, ident):
        s = self.settings
        ov = dict(s.p("overrides").get(ident) or {})
        items = [{"header": True, "label": f"{input_label(ident)} sound"}]
        items.append({"label": f"Profile pack ({s.p('pack')})", "checked": not ov.get("pack"),
                      "command": lambda: self.set_override(ident, pack=None)})
        for pack in sounds.list_packs():
            if pack.name == s.p("pack"):
                continue
            items.append({"label": pack.name, "checked": ov.get("pack") == pack.name,
                          "command": lambda n=pack.name: self.set_override(ident, pack=n)})
        items.append({"header": True, "label": "Level"})
        items.append({"label": "Same as main volume", "checked": ov.get("volume") is None,
                      "command": lambda: self.set_override(ident, volume=None)})
        for lvl in LEVELS:
            items.append({"label": f"{lvl}%", "checked": ov.get("volume") == lvl,
                          "command": lambda v=lvl: self.set_override(ident, volume=v)})
        items.append({"header": True, "label": "Pan (stereo position)"})
        for value, text in PANS:
            items.append({"label": text, "checked": (ov.get("pan") or 0) == value,
                          "command": lambda v=value: self.set_override(ident, pan=v)})
        items.append({"separator": True})
        items.append({"label": "Remove input", "command": lambda: self.remove_input(ident)})
        popup_at_widget(chip, items, width=S(220))

    _UNSET = object()

    def set_override(self, ident, pack=_UNSET, volume=_UNSET, pan=_UNSET):
        overrides = dict(self.settings.p("overrides"))
        ov = dict(overrides.get(ident) or {"pack": None, "volume": None, "pan": 0})
        if pack is not App._UNSET:
            ov["pack"] = pack
        if volume is not App._UNSET:
            ov["volume"] = volume
        if pan is not App._UNSET:
            ov["pan"] = pan
        if ov.get("pack") or ov.get("volume") is not None or ov.get("pan"):
            overrides[ident] = ov
        else:
            overrides.pop(ident, None)
        self.settings.pset("overrides", overrides)
        self.engine.overrides = copy.deepcopy(overrides)
        self.engine.rebuild()
        self.refresh_chips()
        self.schedule_save()
        self.root.after(150, lambda: self.engine.preview(ident, "press"))

    def begin_input_capture(self):
        self.cancel_capture()
        self.capture = "input"
        self.capture_token = self.engine.begin_capture("input")
        self.refresh_chips()
        self._start_pulse()

    def begin_hotkey_capture(self, action):
        was = self.capture
        self.cancel_capture()
        if was == ("hotkey", action):
            return
        self.capture = ("hotkey", action)
        self.capture_token = self.engine.begin_capture("hotkey")
        self.refresh_hotkey(action)
        self._start_pulse()

    def cancel_capture(self):
        if not self.capture:
            return
        was = self.capture
        self.capture = None
        self.capture_token = None
        self.engine.cancel_capture()
        self._stop_pulse()
        if was == "input":
            self.refresh_chips()
        else:
            self.refresh_hotkey(was[1])

    def on_captured(self, token, target, ident, pos):
        if token != self.capture_token or not self.capture:
            return
        was = self.capture
        listening = self.w.get("listening_chip")
        if ident and ident.startswith("mouse:") and pos and listening is not None:
            try:
                if self.root.winfo_containing(int(pos[0]), int(pos[1])) is listening:
                    ident = None
            except (tk.TclError, KeyError):
                pass
        self.capture = None
        self.capture_token = None
        self.engine.cancel_capture()
        self._stop_pulse()
        s = self.settings
        if was == "input":
            self.refresh_chips()
            if ident is None:
                return
            if ident in s.p("inputs"):
                self.set_input_hint(f"{input_label(ident)} is already watched", T.warn, temporary=True)
            elif ident in s["hotkeys"].values():
                self.set_input_hint(f"{input_label(ident)} is used as a hotkey", T.warn, temporary=True)
            else:
                s.pset("inputs", s.p("inputs") + [ident])
                self.engine.set_inputs(s.p("inputs"))
                self.refresh_chips()
                self.set_input_hint(f"Added {input_label(ident)}", T.ok, temporary=True)
                self.schedule_save()
        else:
            action = was[1]
            if ident is not None:
                clash = [a for a, c in s["hotkeys"].items() if c == ident and a != action]
                base = split_combo(ident)[1]
                if base in s.p("inputs"):
                    self.notify_inline(f"{input_label(base)} is a watched input - pick another key")
                elif clash:
                    self.notify_inline(f"{input_label(ident)} is already a hotkey")
                else:
                    self.set_hotkey(action, ident)
            self.refresh_hotkey(action)

    def notify_inline(self, text):
        self.w["footer"].config(text=text, fg=T.warn)
        self._after("footer", 3000, lambda: self.w["footer"].config(text=""))

    def remove_input(self, ident):
        s = self.settings
        s.pset("inputs", [i for i in s.p("inputs") if i != ident])
        overrides = dict(s.p("overrides"))
        if overrides.pop(ident, None) is not None:
            s.pset("overrides", overrides)
            self.engine.overrides = copy.deepcopy(overrides)
            self.engine.rebuild()
        self.engine.set_inputs(s.p("inputs"))
        self.refresh_chips()
        self.schedule_save()

    def reset_inputs(self):
        self.cancel_capture()
        s = self.settings
        s.pset("inputs", list(DEFAULT_INPUTS))
        s.pset("overrides", {})
        self.apply_engine_config()
        self.engine.rebuild()
        self.refresh_chips()
        self.set_input_hint("Inputs reset to W, R, Up Arrow", T.ok, temporary=True)
        self.schedule_save()

    def _start_pulse(self):
        self._stop_pulse()
        self._pulse_t = 0.0
        self._pulse()

    def _pulse(self):
        import math
        self._pulse_t += 0.12
        value = (math.sin(self._pulse_t * 2.2) + 1) / 2
        chips = [self.w.get("listening_chip")] + list(self.w.get("hotkey_chips", {}).values())
        for chip in chips:
            if chip is not None and chip.winfo_exists() and chip.chip_style == "listening":
                chip.pulse = value
                chip.redraw()
        self._jobs["pulse"] = self.root.after(40, self._pulse)

    def _stop_pulse(self):
        job = self._jobs.pop("pulse", None)
        if job:
            self.root.after_cancel(job)

    # ---------------- app filter ----------------
    def refresh_apps(self):
        flow = self.w["apps"]
        flow.clear()
        for exe in self.settings.p("apps"):
            Chip(flow, exe, on_remove=lambda x=exe: self.remove_app(x))
        add = Chip(flow, "+  Add app", style="add")
        add.on_click = lambda: self.app_menu(add)
        flow.relayout()

    def app_menu(self, anchor):
        items = [{"header": True, "label": "Running apps"}]
        current = set(self.settings.p("apps"))
        for exe, title in winutil.running_apps()[:25]:
            if exe in current:
                continue
            short = title if len(title) <= 38 else title[:37] + "…"
            items.append({"label": exe, "detail": short, "command": lambda x=exe: self.add_app(x)})
        items.append({"separator": True})
        items.append({"label": "Browse for an .exe…", "command": self.browse_app})
        popup_at_widget(anchor, items, width=S(300))

    def browse_app(self):
        path = filedialog.askopenfilename(parent=self.root, title="Choose the game's .exe",
                                          filetypes=[("Programs", "*.exe")])
        if path:
            self.add_app(os.path.basename(path))

    def add_app(self, exe):
        apps = self.settings.p("apps")
        if exe.lower() not in apps:
            self.settings.pset("apps", apps + [exe.lower()])
        if not self.settings.p("app_filter"):
            self.settings.pset("app_filter", True)
            self.build_ui()   # flip the toggle
        self.apply_engine_config()
        self.refresh_apps()
        self.schedule_save()

    def remove_app(self, exe):
        self.settings.pset("apps", [a for a in self.settings.p("apps") if a != exe])
        self.apply_engine_config()
        self.refresh_apps()
        self.schedule_save()

    def on_app_filter(self, value):
        self.settings.pset("app_filter", value)
        self.apply_engine_config()
        if value and not self.settings.p("apps"):
            self.set_input_hint("Add the game below - until then this filter is off", T.warn, temporary=True)
        self.schedule_save()

    # ============================================================
    # Profiles
    # ============================================================
    def profile_menu(self, anchor):
        s = self.settings
        multiple = len(s.profile_names()) > 1
        items = [
            {"label": "New profile…", "command": self.new_profile},
            {"label": "Duplicate profile…", "command": lambda: self.new_profile(copy_current=True)},
            {"label": "Rename profile…", "command": self.rename_profile},
            {"separator": True},
            {"label": "Delete profile", "enabled": multiple, "command": self.delete_profile},
        ]
        popup_at_widget(anchor, items, align="right", width=S(200))

    def _validate_profile(self, name, current=None):
        if not name:
            return "Give it a name"
        if len(name) > 32:
            return "Keep it under 32 characters"
        if name != current and name in self.settings.profile_names():
            return "A profile with that name already exists"
        return None

    def new_profile(self, copy_current=False):
        s = self.settings
        base = f"{s.profile_name} copy" if copy_current else "New profile"
        PromptDialog(self.root, "New profile",
                     "Profiles keep their own inputs, volume, sound pack and app filter." if not copy_current
                     else f"Duplicate '{s.profile_name}' as:",
                     initial=base, validate=self._validate_profile, ok_text="Create",
                     on_ok=lambda name: self._create_profile(name, s.profile_name if copy_current else None))

    def _create_profile(self, name, copy_from):
        self.settings.create_profile(name, copy_from)
        self._after_profile_change(f"Profile: {name}")

    def rename_profile(self):
        old = self.settings.profile_name
        PromptDialog(self.root, "Rename profile", "New name:", initial=old,
                     validate=lambda n: self._validate_profile(n, old),
                     on_ok=lambda n: (self.settings.rename_profile(old, n), self._after_profile_change()))

    def delete_profile(self):
        name = self.settings.profile_name
        ConfirmDialog(self.root, "Delete profile", f"Delete the profile '{name}'? This can't be undone.",
                      on_ok=lambda: (self.settings.delete_profile(name),
                                     self._after_profile_change(f"Profile: {self.settings.profile_name}")))

    def switch_profile(self, name, toast=False):
        if name == self.settings.profile_name:
            return
        self.settings.switch_profile(name)
        self._after_profile_change(f"Profile: {name}" if toast else None)

    def next_profile(self):
        names = self.settings.profile_names()
        if len(names) < 2:
            self.notify("Only one profile", T.faint)
            return
        i = names.index(self.settings.profile_name)
        self.switch_profile(names[(i + 1) % len(names)], toast=True)

    def _after_profile_change(self, toast=None):
        self.cancel_capture()
        self.apply_engine_config()
        self.engine.rebuild()
        self.build_ui()
        self.schedule_save()
        if toast:
            self.notify(toast, T.accent)

    # ============================================================
    # Sound packs
    # ============================================================
    def refresh_packs(self):
        holder = self.w.get("packs")
        if holder is None:
            return
        for child in holder.winfo_children():
            child.destroy()
        current = self.settings.p("pack")
        for pack in sounds.list_packs():
            self._pack_row(holder, pack, pack.name == current)

    def _pack_row(self, holder, pack, selected):
        n_press, n_release, tiers = pack.summary()
        bg = T.accent_soft if selected else T.card
        row = tk.Frame(holder, bg=bg, cursor="hand2", highlightthickness=1,
                       highlightbackground=T.accent_dim if selected else T.card)
        row.pack(fill="x", pady=(S(4), 0))
        holder_bg = tk.Frame(row, bg=bg)
        holder_bg.pack(side="left", padx=(S(10), S(10)), pady=S(10))
        radio = Radio(holder_bg, selected)
        radio.pack()
        text = tk.Frame(row, bg=bg)
        text.pack(side="left", fill="x", expand=True)
        name = label(text, pack.name + ("  ·  built-in" if pack.builtin else ""), font="semi")
        name.configure(bg=bg)
        name.pack(anchor="w")
        detail = f"{n_press} press · {n_release} release"
        if tiers:
            detail += " · " + ", ".join(tiers) + " clicks"
        errors = self.engine.last_errors.get(pack.name)
        if errors:
            detail += f" · {len(errors)} unreadable"
        info = label(text, detail, font="tiny", fg=T.warn if errors else T.faint)
        info.configure(bg=bg)
        info.pack(anchor="w")
        if errors:
            Tooltip(info, "\n".join(errors[:6]))
        menu_btn = RoundedButton(row, "", None, height=28, icon=I.more)
        menu_btn.configure(bg=bg)
        menu_btn.command = lambda: self.pack_menu(menu_btn, pack)
        menu_btn.pack(side="right", padx=(S(4), S(8)))
        play = RoundedButton(row, "", lambda: self.engine.preview_pack(pack.name), height=28, icon=I.play)
        play.configure(bg=bg)
        play.pack(side="right")
        Tooltip(play, "Preview")
        for wd in (row, holder_bg, radio, text, name, info):
            wd.bind("<ButtonRelease-1>", lambda e, n=pack.name: self.select_pack(n))

    def select_pack(self, name):
        if self.settings.p("pack") == name:
            return
        self.settings.pset("pack", name)
        self.engine.pack = name
        self.engine.rebuild()
        self.refresh_packs()
        self.schedule_save()
        self.root.after(250, lambda: self.engine.preview(None, "press"))

    def pack_menu(self, anchor, pack):
        items = [
            {"label": "Use for this profile", "command": lambda: self.select_pack(pack.name)},
            {"label": "View clips" if pack.builtin else "Edit clips\u2026",
             "command": lambda: PackEditor(self, pack.name)},
            {"label": "Open folder", "command": lambda: winutil.open_path(pack.path)},
            {"label": "Duplicate", "command": lambda: self._duplicate_pack(pack.name)},
            {"label": "Export as .zip\u2026", "command": lambda: self._export_pack(pack.name)},
            {"label": "Rename…", "enabled": not pack.builtin, "command": lambda: self._rename_pack(pack.name)},
            {"separator": True},
            {"label": "Delete", "enabled": not pack.builtin, "command": lambda: self._delete_pack(pack.name)},
        ]
        popup_at_widget(anchor, items, align="right", width=S(190))

    def _duplicate_pack(self, name):
        new = sounds.duplicate_pack(name)
        self.refresh_packs()
        self.pack_status(f"✓ Created '{new}' - add or swap files in its folder, then reload")

    def _validate_pack(self, name, current=None):
        clean = sounds.safe_pack_name(name)
        if not clean:
            return "Give it a name"
        if clean != current and clean.lower() in {p.name.lower() for p in sounds.list_packs()}:
            return "A pack with that name already exists"
        return None

    def _rename_pack(self, old):
        def do(new):
            new = sounds.safe_pack_name(new)
            try:
                sounds.rename_pack(old, new)
            except OSError as e:
                MessageDialog(self.root, "Couldn't rename", str(e))
                return
            self.settings.rename_pack_everywhere(old, new)
            self.engine.banks.pop(old, None)
            self.apply_engine_config()
            self.engine.rebuild()
            self.refresh_packs()
            self.refresh_chips()
            self.schedule_save()
        PromptDialog(self.root, "Rename pack", "New name:", initial=old,
                     validate=lambda n: self._validate_pack(n, old), on_ok=do)

    def _delete_pack(self, name):
        def do():
            sounds.delete_pack(name)
            self.settings.rename_pack_everywhere(name, sounds.DEFAULT_PACK)
            self.engine.banks.pop(name, None)
            self.apply_engine_config()
            self.engine.rebuild()
            self.refresh_packs()
            self.refresh_chips()
            self.schedule_save()
        ConfirmDialog(self.root, "Delete pack", f"Delete the pack '{name}' and its sound files?\n"
                                                "Profiles using it switch back to Default.", on_ok=do)

    def pack_status(self, text, color=None):
        if "pack_status" in self.w:
            self.w["pack_status"].config(text=text, fg=color or T.ok)

    def on_pack_edited(self, name):
        self.engine.banks.pop(name, None)
        self.engine.rebuild()
        self.refresh_packs()

    def build_welcome(self, page):
        inner, head = self.card(page, "Welcome to Clickwave", first=True)
        hk = input_label(self.settings["hotkeys"].get("toggle")) if self.settings["hotkeys"].get("toggle") else None
        steps = [
            f"Press Start (or {hk} anywhere) to begin listening." if hk else "Press Start to begin listening.",
            "Add the keys and mouse buttons you play with under Watched inputs.",
            "Streaming? Pick your virtual cable in Settings > Audio output.",
            "Want other sounds? Import a pack or a recording on the Sounds tab.",
        ]
        for i, text in enumerate(steps, 1):
            row = tk.Frame(inner, bg=T.card)
            row.pack(fill="x", pady=(S(8) if i == 1 else S(5), 0))
            badge = tk.Canvas(row, width=S(20), height=S(20), bg=T.card, highlightthickness=0)
            from . import aa
            aa.circle(badge, S(10), S(10), S(9), T.accent_soft, T.accent_dim, 1)
            badge.create_text(S(10), S(10), text=str(i), fill=T.accent, font=F["tiny"])
            badge.pack(side="left", anchor="n")
            label(row, text, font="small", justify="left", wraplength=S(300)).pack(side="left", padx=(S(8), 0))
        btns = tk.Frame(inner, bg=T.card)
        btns.pack(fill="x", pady=(S(12), 0))
        RoundedButton(btns, "Got it", self.dismiss_welcome, height=30, font=F["small"], variant="primary").pack(side="right")

    def dismiss_welcome(self):
        self.settings["onboarded"] = True
        self.schedule_save()
        self.build_ui()

    def device_row(self, inner, command, which):
        row = tk.Frame(inner, bg=T.card)
        row.pack(fill="x", pady=(S(8), 0))
        test = RoundedButton(row, "", lambda: self.test_device(which), height=34, icon=I.play)
        test.pack(side="right", padx=(S(6), 0))
        Tooltip(test, "Play a click on just this device - handy for checking that OBS hears it")
        dd = Dropdown(row, [], None, command)
        dd.pack(side="left", fill="x", expand=True)
        return dd

    def test_device(self, which):
        s, a = self.settings, self.audio
        if which == "monitor":
            key = (a.find(s["monitor_device"]) if s["monitor_device"] else None) or a.default()
            level = s["monitor_level"] / 100.0
        else:
            st = s["stream_device"]
            key = a.find_cable() if st == "auto" else (a.find(st) if st else None)
            level = s["stream_level"] / 100.0
        if not key:
            self.notify_inline("No device selected")
            return
        if not self.engine.preview_device(key, level):
            self.notify_inline("One test click per second")
            return
        self.notify_inline(f"Played a click on {a.name_of(key) or 'the device'}")

    def on_pack_created(self, name, detail=""):
        self.settings.pset("pack", name)
        self.engine.pack = name
        self.engine.rebuild()
        self.refresh_packs()
        self.pack_status(f"✓ Created '{name}'" + (f" ({detail})" if detail else "") + " and selected it")
        self.schedule_save()
        self.root.after(400, lambda: self.engine.preview(None, "press"))

    def on_import_result(self, result):
        if not result.packs:
            first = result.errors[0] if result.errors else "Nothing usable was found."
            self._import_failed(first, ogg=NEEDS_WEB_MEDIA_EXTENSIONS in first)
            return
        first = result.packs[0][0]
        self.settings.pset("pack", first)
        self.engine.pack = first
        self.engine.rebuild()
        self.refresh_packs()
        label_kind = {"clickpack": "GD clickpack", "mechvibes": "Mechvibes pack"}.get(result.kind, "pack")
        extra = ""
        if len(result.packs) > 1:
            extra = " - set Player 2's pack on an input (click its chip on the Main tab)"
        skipped = f" · {len(result.errors)} file(s) skipped" if result.errors else ""
        self.pack_status(f"✓ Imported {label_kind}: {result.message}{skipped}{extra}")
        self.schedule_save()
        self.root.after(400, lambda: self.engine.preview(None, "press"))

    def _run_import(self, work, ogg_hint=False):
        self.pack_status("Importing\u2026", T.muted)

        def runner():
            try:
                result = work()
                self.root.after(0, lambda: self.on_import_result(result))
            except (DecodeError, OSError, ValueError, KeyError) as e:
                msg = str(e)
                ogg = NEEDS_WEB_MEDIA_EXTENSIONS in msg
                self.root.after(0, lambda: self._import_failed(msg, ogg=ogg))
            except Exception as e:
                log.exception("Import failed")
                msg = str(e)
                self.root.after(0, lambda: self._import_failed(msg))
        import threading
        threading.Thread(target=runner, daemon=True).start()

    def import_zip(self):
        path = filedialog.askopenfilename(parent=self.root, title="Choose a pack .zip",
                                          initialdir=self._mechvibes_dir(),
                                          filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")])
        if path:
            self._run_import(lambda: sounds.import_zip(path))

    def import_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title="Choose a pack folder",
                                         initialdir=self._mechvibes_dir())
        if folder:
            self._run_import(lambda: sounds.import_folder(folder))

    def _export_pack(self, name):
        path = filedialog.asksaveasfilename(parent=self.root, title="Export pack", defaultextension=".zip",
                                            initialfile=f"{name}.zip", filetypes=[("Zip archive", "*.zip")])
        if not path:
            return
        try:
            sounds.export_pack(name, path)
            self.pack_status(f"✓ Exported '{name}' - anyone with Clickwave can import that .zip")
        except OSError as e:
            MessageDialog(self.root, "Export failed", str(e))

    def open_packs_folder(self):
        os.makedirs(PACKS_DIR, exist_ok=True)
        winutil.open_path(PACKS_DIR)

    def import_recording(self):
        ImportRecordingDialog(self)

    def _mechvibes_dir(self):
        for path in (os.path.join(os.path.expanduser("~"), "mechvibes_custom"),
                     os.path.join(os.path.expanduser("~"), "Downloads")):
            if os.path.isdir(path):
                return path
        return os.path.expanduser("~")

    def _import_failed(self, message, ogg=False):
        self.pack_status("")
        if ogg:
            self.explain_ogg(self.root)
        else:
            self.show_error(message, "Import failed")

    def show_error(self, error, title="Something went wrong", parent=None):
        t, summary, fix = friendly(error, title)
        log.warning("%s: %s", t, error)
        ErrorDialog(parent or self.root, t, summary, fix)

    def explain_ogg(self, parent):
        ErrorDialog(parent, "OGG audio needs a free add-on",
                    "This sound is OGG Vorbis audio, and this PC has no Vorbis decoder yet.",
                    "Install Microsoft's free Web Media Extensions from the Microsoft Store, then try again. "
                    "Clickwave reads the OGG file itself and uses that decoder - nothing else is needed.",
                    link_text="Open Web Media Extensions in the Microsoft Store",
                    link_cmd=lambda: winutil.open_path(OGG_STORE_URL))

    def import_files(self):
        exts = " ".join("*" + e for e in AUDIO_EXTENSIONS)
        paths = filedialog.askopenfilenames(parent=self.root, title="Choose click sounds",
                                            filetypes=[("Audio", exts), ("All files", "*.*")])
        if not paths:
            return

        def do(name):
            self.pack_status("Importing…", T.muted)

            self._run_import(lambda: sounds.import_files(list(paths), name))

        PromptDialog(self.root, "New pack", "Name the pack. Files with 'release' or 'up' in their name\n"
                                            "become release sounds, 'soft'/'hard' pick the click tier;\n"
                                            "everything else is a normal press.",
                     initial=sounds.unique_pack_name("My pack"), validate=self._validate_pack,
                     ok_text="Import", on_ok=do)

    # ============================================================
    # Hotkeys
    # ============================================================
    def refresh_hotkey(self, action):
        holder = self.w.get("hotkeys", {}).get(action)
        if holder is None:
            return
        for child in holder.winfo_children():
            child.destroy()
        chips = self.w.setdefault("hotkey_chips", {})
        combo = self.settings["hotkeys"].get(action)
        if self.capture == ("hotkey", action):
            chip = Chip(holder, "Press keys…", on_click=lambda: self.begin_hotkey_capture(action),
                        style="listening")
        elif combo:
            chip = Chip(holder, input_label(combo), on_click=lambda: self.begin_hotkey_capture(action),
                        on_remove=lambda: (self.set_hotkey(action, None), self.refresh_hotkey(action)))
            Tooltip(chip, "Click to change · × to clear")
        else:
            chip = Chip(holder, "+  Set", on_click=lambda: self.begin_hotkey_capture(action), style="add")
        chip.pack()
        chips[action] = chip

    def set_hotkey(self, action, combo):
        self.settings["hotkeys"][action] = combo
        self.apply_engine_config()
        self.schedule_save()

    def do_action(self, action):
        if action == "toggle":
            self.set_running(not self.engine.running)
        elif action in ("vol_up", "vol_down"):
            slider = self.w.get("vol")
            if slider is not None:
                slider.step_marker(1 if action == "vol_up" else -1)
            self.notify(f"Volume {self.settings.p('volume')}%", T.accent)
        elif action == "next_profile":
            self.next_profile()
        elif action == "mute_self":
            value = not self.settings["hear_myself"]
            self.settings["hear_myself"] = value
            self.on_devices_changed()
            self.build_ui()
            self.notify("Hearing clicks" if value else "Muted for you (stream still hears)",
                        T.ok if value else T.warn)
            self.schedule_save()

    # ============================================================
    # Devices
    # ============================================================
    def device_options(self):
        return [(name, name) for _, name in self.audio.devices]

    def refresh_output(self):
        s, a = self.settings, self.audio
        if "monitor_dd" not in self.w:
            return
        default_name = a.name_of(a.default_key) if a.backend == "wasapi" else None
        mon_opts = [(None, "System default" + (f" ({default_name})" if default_name else ""))] + self.device_options()
        self.w["monitor_dd"].set(s["monitor_device"], mon_opts)
        if s["monitor_device"] and a.find(s["monitor_device"]) is None:
            self.w["monitor_dd"].placeholder = f"{s['monitor_device']} (not connected)"
        cable = a.name_of(a.find_cable()) if a.find_cable() else None
        st_opts = [("auto", f"Auto · {cable or 'no cable found'}"), (None, "None")] + self.device_options()
        self.w["stream_dd"].set(s["stream_device"], st_opts)
        if s["stream_device"] not in ("auto", None) and a.find(s["stream_device"]) is None:
            self.w["stream_dd"].placeholder = f"{s['stream_device']} (not connected)"
        text, color = self.output_note
        self.w["output_status"].config(text=text, fg=color, cursor="")
        self.w["output_status"].unbind("<ButtonRelease-1>")
        self.update_latency()

    # ---------------- problem reports ----------------
    def diagnostics(self, log_lines=15):
        """A short, privacy-friendly summary for bug reports."""
        import platform
        s, e, a = self.settings, self.engine, self.audio
        home = os.path.expanduser("~")
        lats = [d.latency_ms for d in e._devices if getattr(d, "latency_ms", None)]
        outs = []
        for key in self.output_keys:
            role = "you" if key == e.cue_key else "stream"
            outs.append(f"{a.name_of(key) or key} [{role}]")
        lines = [
            f"Clickwave {APP_VERSION} ({'exe' if getattr(sys, 'frozen', False) else 'source'})",
            f"Windows {platform.version()} ({platform.machine()})",
            f"Audio: {a.backend}" + (f", ~{max(lats):.0f} ms" if lats else "") +
            f" | outputs: {', '.join(outs) or 'none'} | {len(a.devices)} devices found",
            f"Listening: {'yes' if e.running else 'no'} | inputs: {len(s.p('inputs'))} | pack: "
            f"{'custom' if s.p('pack') != 'Default' else 'Default'} | dynamics: {'on' if s.p('dynamics') else 'off'}"
            f" | app filter: {'on' if s.p('app_filter') else 'off'}",
            f"Overlay: {'on' if s['overlay'] else 'off'} | tray: {'on' if s['tray'] else 'off'} | "
            f"ignored simulated presses: {e.injected_ignored}",
        ]
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-log_lines:]
            if tail:
                lines.append("Recent log:")
                lines += [ln.rstrip()[:220] for ln in tail]
        except OSError:
            lines.append("Recent log: (none)")
        # Mask the Windows user name in any paths.
        return "\n".join(lines).replace(home, "%USERPROFILE%")

    def report_problem(self):
        from urllib.parse import quote
        lines = 15
        while True:
            diag = self.diagnostics(lines)
            url = f"{ISSUES_URL}/new?template=bug_report.yml&diagnostics={quote(diag)}"
            if len(url) < 7000 or lines == 0:
                break
            lines = max(0, lines - 5)
        self.root.clipboard_clear()
        self.root.clipboard_append(diag)
        winutil.open_path(url)
        self.notify_inline("Report form opened in your browser - details are filled in (and copied)")

    # ---------------- updates ----------------
    def check_updates(self, manual=True):
        if manual:
            self._set_update_status("Checking GitHub\u2026", T.muted)

        def work():
            try:
                info = updates.check()
                self.root.after(0, lambda: self._update_result(info, manual))
            except OSError as e:
                msg = str(e)
                if manual:
                    self.root.after(0, lambda: self._set_update_status(f"Couldn't check: {msg}.", T.warn))
                log.info("Update check failed: %s", msg)
        import threading
        threading.Thread(target=work, daemon=True).start()

    def _update_result(self, info, manual):
        if info.newer:
            self.update_url = info.url
            self._set_update_status(f"\u2b06 Clickwave {info.latest} is available (you have {APP_VERSION}). "
                                    "Click to open the download page.", T.ok)
            self._make_update_link()
            self.notify_inline(f"Update available: Clickwave {info.latest} (Settings > About)")
        elif manual:
            self._set_update_status(f"\u2713 You're up to date ({APP_VERSION}).", T.ok)

    def _set_update_status(self, text, color):
        self.update_text, self.update_color = text, color
        if "update_status" in self.w and self.w["update_status"].winfo_exists():
            self.w["update_status"].config(text=text, fg=color)

    def _make_update_link(self):
        lbl = self.w.get("update_status")
        if lbl is not None and self.update_url:
            lbl.configure(cursor="hand2")
            lbl.bind("<ButtonRelease-1>", lambda e: winutil.open_path(self.update_url))

    def check_outputs(self):
        """Tell the user when a chosen output couldn't be opened."""
        fallback = getattr(self.engine, "fallback_from", None)
        if fallback and fallback != getattr(self, "_fallback_noted", None):
            self._fallback_noted = fallback
            self.notify_inline(f"Pack '{fallback}' is missing - using Default")
            self.pack_status(f"\u26a0 The pack '{fallback}' was moved or deleted, so Default is playing. "
                             "Pick another pack above.", T.warn)
        opened = {d.key for d in self.engine._devices}
        missing = [k for k in self.output_keys if k not in opened]
        if not missing or "output_status" not in self.w:
            return
        name = self.audio.name_of(missing[0]) or "an output device"
        self.w["output_status"].config(
            text=f"\u26a0 Couldn't open {name}. Click here for help.", fg=T.red_hover, cursor="hand2")
        self.w["output_status"].bind("<ButtonRelease-1>", lambda e: ErrorDialog(
            self.root, "Audio device didn't open",
            f"Clickwave couldn't start playing on {name}.",
            "Make sure it's plugged in and not disabled in Windows Sound settings. If another app is using it "
            "in 'exclusive mode', close that app (or untick 'Allow applications to take exclusive control' in "
            "the device's Advanced properties). Then press Refresh devices. Switching Output engine to "
            "Compatibility can also help with unusual drivers."))

    def check_hooks(self):
        """The keyboard hook can be blocked by some security software."""
        kb = self.engine._kb_listener
        if kb is not None and not kb.is_alive() and not getattr(self, "_hook_warned", False):
            self._hook_warned = True
            log.error("Keyboard hook stopped")
            ErrorDialog(self.root, "Clicks stopped working",
                        "Windows stopped sending key presses to Clickwave.",
                        "Restart Clickwave. If it happens again, security or anti-cheat software may be blocking "
                        "keyboard hooks - allow Clickwave in it. Running a game as administrator also hides its "
                        "key presses from apps that aren't; run Clickwave as administrator too in that case.")

    def update_latency(self):
        if "latency" not in self.w:
            return
        lats = [d.latency_ms for d in self.engine._devices if getattr(d, "latency_ms", None)]
        if self.audio.backend == "wasapi":
            text = f"WASAPI · ~{max(lats):.0f} ms" if lats else "WASAPI"
        else:
            text = "waveOut (compatibility)"
        self.w["latency"].config(text=text)

    def output_tooltip(self):
        if not self.output_keys:
            return "Clicks are muted"
        lines = ["Playing to:"]
        for key in self.output_keys:
            lines.append("  • " + (self.audio.name_of(key) or key))
        return "\n".join(lines)

    def on_devices_changed(self):
        self.apply_devices()
        self.engine.rebuild()
        self.refresh_output()
        self.schedule_save()

    def on_monitor(self, value):
        self.settings["monitor_device"] = value
        self.on_devices_changed()

    def on_stream(self, value):
        self.settings["stream_device"] = value
        self.on_devices_changed()

    def on_backend(self, value):
        self.settings["backend"] = value
        self.audio.set_backend(value)
        if value != "legacy" and self.audio.backend != "wasapi":
            self.notify_inline("WASAPI unavailable - using compatibility mode")
        self.on_devices_changed()

    def refresh_devices(self):
        self.audio.refresh()
        self.on_devices_changed()
        self.notify_inline(f"Found {len(self.audio.devices)} output devices")

    # ============================================================
    # Appearance / misc settings
    # ============================================================
    def set_global(self, key, value):
        self.settings[key] = value
        self.schedule_save()

    def on_theme(self, value):
        self.settings["theme"] = value
        self.build_ui()
        self.schedule_save()

    def on_accent(self, value):
        self.settings["accent"] = value
        self.build_ui()
        self.overlay.set_keys([(i, input_label(i)) for i in self.settings.p("inputs")], T.accent)
        self.schedule_save()

    def open_log(self):
        if os.path.exists(LOG_PATH):
            winutil.open_path(LOG_PATH)
        else:
            self.notify_inline("No log yet - nothing has gone wrong")

    def reset_settings(self):
        def do():
            self.settings.reset()
            self.apply_engine_config()
            self.apply_devices()
            self.engine.rebuild()
            self.root.attributes("-topmost", False)
            self.setup_tray()
            self.sync_autostart()
            self.build_ui()
            self.save()
        ConfirmDialog(self.root, "Reset all settings",
                      "Reset every setting and profile to defaults? Your sound packs are kept.",
                      ok_text="Reset", on_ok=do)

    # ============================================================
    # Tray / window / startup
    # ============================================================
    def setup_tray(self):
        want = self.settings["tray"]
        if want and self.tray is None:
            self.tray = TrayIcon(ICON_PATH, lambda cmd, arg: self.events.put(("tray", cmd, arg)))
            self.refresh_state()
        elif not want and self.tray is not None:
            self.tray.stop()
            self.tray = None
            self.show_window()

    def on_tray(self, cmd, arg):
        if cmd == "show":
            self.show_window()
        elif cmd == "toggle":
            self.set_running(not self.engine.running, feedback=False)
        elif cmd == "quit":
            self.quit()
        elif cmd == "profile":
            self.switch_profile(arg, toast=True)

    def logo_image(self):
        """The header logo, scaled for the display DPI (cached)."""
        if getattr(self, "_logo", None) is None:
            path = LOGO_PATH if S(28) <= 32 else LOGO_PATH.replace("logo_28", "logo_64")
            try:
                img = tk.PhotoImage(file=path)
                if path.endswith("logo_64.png") and S(28) <= 40:
                    img = img.subsample(2)
                self._logo = img
            except tk.TclError:
                self._logo = None
        return self._logo

    def sync_autostart(self):
        winutil.remove_legacy_autostart()
        want = self.settings["launch_on_startup"]
        current = winutil.get_autostart()
        if want and current != winutil.launch_command():
            winutil.set_autostart(True)
        elif not want and current:
            winutil.set_autostart(False)

    def show_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(100, lambda: self.root.attributes("-topmost", self.settings["always_on_top"]))
        self.root.focus_force()

    def hide_to_tray(self):
        self.root.withdraw()

    def _on_unmap(self, e):
        if e.widget is not self.root or self._quitting:
            return
        if self.settings["minimize_to_tray"] and self.tray and self.root.state() == "iconic":
            self.root.after(10, self.hide_to_tray)

    def _on_configure(self, e):
        if e.widget is self.root and self.root.state() == "normal":
            self._last_geom = [self.root.winfo_x(), self.root.winfo_y(),
                               self.root.winfo_width(), self.root.winfo_height()]

    def on_close(self):
        if self.settings["close_to_tray"] and self.tray:
            self.hide_to_tray()
        else:
            self.quit()

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        if self._last_geom:
            self.settings["window"] = self._last_geom
        self.save()
        try:
            self.engine.shutdown()
        except Exception:
            log.exception("Engine shutdown failed")
        if self.tray:
            self.tray.stop()
        self.overlay.stop()
        self.toast.destroy()
        self.root.destroy()

    # ============================================================
    # Loops
    # ============================================================
    def _poll(self):
        if self._quitting:
            return
        try:
            for _ in range(200):
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "hit":
                    chip = self.w.get("chip_map", {}).get(event[1])
                    if chip is not None and chip.winfo_exists():
                        chip.set_active(event[2])
                    self.overlay.hit(event[1], event[2])
                    if event[2] and self.cps_test is not None:
                        self.cps_test_hit()
                elif kind == "hotkey":
                    self.do_action(event[1])
                elif kind == "captured":
                    self.on_captured(*event[1:])
                elif kind == "rebuilt":
                    self.update_latency()
                    self.check_outputs()
                elif kind == "injected":
                    self.set_input_hint(
                        f"Ignored simulated {input_label(event[1])} presses from macro software - "
                        "only physical presses make clicks (fair play)", T.warn, temporary=True)
                elif kind == "tray":
                    self.on_tray(event[1], event[2])
        except queue.Empty:
            pass
        except Exception:
            log.exception("Event handling failed")
        self.root.after(15, self._poll)

    def _tick(self):
        if self._quitting:
            return
        try:
            self._tick_n += 1
            e = self.engine
            cps = e.cps()
            self.peak_cps = max(self.peak_cps, cps)
            values = {"cps": str(cps), "peak": str(self.peak_cps), "session": f"{e.total_presses:,}",
                      "lifetime": self._compact(self.lifetime_base + e.lifetime)}
            for key, value in values.items():
                lbl = self.w["stats"][key]
                if lbl.cget("text") != value:
                    lbl.config(text=value)
            if cps > self.settings["best_cps"]:
                self.settings["best_cps"] = cps
            self.overlay.stats(cps, self.peak_cps, e.total_presses)
            if self.cps_test is not None:
                self.update_hint()
            if self._tick_n % 5 == 0:
                self.w["spark"].push(cps)
                self.refresh_state()
                self.update_hint()
        except (tk.TclError, KeyError):
            pass
        self.root.after(100, self._tick)

    @staticmethod
    def _compact(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 10_000:
            return f"{n / 1000:.1f}k"
        return f"{n:,}"

    def _slow_tick(self):
        if self._quitting:
            return
        try:
            if self.show_requests.pending():
                self.show_window()
            n = getattr(self, "_slow_n", 0) + 1
            self._slow_n = n
            if n % 4 == 0:
                self.engine.tidy()
                self.check_hooks()
                if self.settings["stats_files"]:
                    e = self.engine
                    write_stat_files(e.cps(), self.peak_cps, e.total_presses, self.lifetime_base + e.lifetime,
                                     self.settings["best_cps"])
            if n % 8 == 0 and self.audio.changed():
                log.info("Audio devices changed - reconnecting")
                self.audio.refresh()
                self.apply_devices()
                self.engine.rebuild()
                self.refresh_output()
            if n % 120 == 0:
                self.save()
        except Exception:
            log.exception("Background tick failed")
        self.root.after(250, self._slow_tick)


def main():
    setup_logging()
    if sys.platform != "win32":
        print("Clickwave only runs on Windows.")
        return
    mutex = winutil.acquire_single_instance()
    if mutex is None:
        return
    if winutil.old_version_running():
        winutil.ask_old_version_to_show()
        winutil.message_box(
            "GD Click (the old version of this app) is still running.\n\n"
            "Quit it first - right-click its tray icon and choose Quit - then open Clickwave again. "
            "Your settings and sound packs will come along automatically.")
        return
    migrate_from_gd_click()
    winutil.set_dpi_awareness()
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Clickwave.App")
    except Exception:
        pass
    startup = "--startup" in sys.argv

    root = tk.Tk()
    root.withdraw()
    set_scale(max(1.0, root.winfo_fpixels("1i") / 96.0))
    init_fonts(root)
    if os.path.exists(ICON_PATH):
        try:
            root.iconbitmap(default=ICON_PATH)
        except tk.TclError:
            pass
    try:
        app = App(root, startup=startup)
    except Exception:
        log.exception("Startup failed")
        raise

    # Window size/position: restore, else fit the Main tab to the screen.
    root.minsize(S(380), S(480))
    left, top, right, bottom = winutil.work_area()
    geom = app.settings["window"]
    root.update_idletasks()
    natural = (app.w["pages"][0].inner.winfo_reqheight() + app.w["tabs"].winfo_reqheight()
               + S(150))
    width, height = S(430), min(natural, bottom - top - S(40))
    x = y = None
    if isinstance(geom, list) and len(geom) == 4:
        gx, gy, gw, gh = geom
        vx, vy, vw, vh = winutil.virtual_screen()
        if isinstance(gw, int) and isinstance(gh, int) and gw >= S(380) and gh >= S(480):
            width, height = gw, gh
        if isinstance(gx, int) and isinstance(gy, int) and vx <= gx <= vx + vw - S(80) and vy <= gy <= vy + vh - S(80):
            x, y = gx, gy
    root.geometry(f"{width}x{height}" + (f"+{x}+{y}" if x is not None else ""))
    root.attributes("-topmost", app.settings["always_on_top"])

    if app.settings["start_minimized"]:
        if app.tray:
            pass  # stay hidden in the tray
        else:
            root.deiconify()
            root.iconify()
    else:
        root.deiconify()
        winutil.style_title_bar(root, T.dark, T.bg, T.text)
    root.mainloop()

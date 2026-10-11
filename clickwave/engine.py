"""Click engine: global input hooks + playback. No UI code.

Hook callbacks run on pynput's threads and must stay fast: everything
they play is pre-rendered, per output device, by a background worker
whenever the configuration changes. Anything the UI needs to react to
is posted to `events` (polled by the Tk loop):
    ("hotkey", action)
    ("captured", token, target, ident_or_None, pos)
    ("hit", ident, pressed)
    ("rebuilt",)
"""

import collections
import ctypes
import queue
import random
import threading
import time

from pynput import keyboard, mouse

from .common import log
from .inputs import MODIFIERS, key_to_id, make_combo
from .sounds import (
    DEFAULT_PACK, FALLBACK, HUMANIZE_GAINS, TIERS, SoundBank, get_pack, render_buffer, synth_cue,
)
from .decode import to_format
from .winutil import OWN_PID, ForegroundTracker


LLKHF_INJECTED, LLKHF_LOWER_IL_INJECTED = 0x10, 0x02
LLMHF_INJECTED, LLMHF_LOWER_IL_INJECTED = 0x01, 0x02

_MOD_VKS = {"ctrl": (0x11,), "shift": (0x10,), "alt": (0x12,), "win": (0x5B, 0x5C)}
_user32 = ctypes.WinDLL("user32")
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short


def _physically_down(mod):
    return any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MOD_VKS.get(mod, ()))


class Voice:
    """Ready-to-play buffers for one (pack, level, pan) combination."""

    def __init__(self):
        self.tables = {}     # (kind, tier) -> {device key: [ctypes buffers]}
        self.counts = {}     # (kind, tier) -> number of variants
        self._last = {}

    def resolve(self, kind, tier):
        """First available tier in the fallback chain, or None."""
        for t in FALLBACK.get(tier, ("normal",)):
            if self.counts.get((kind, t)):
                return t
        return None

    def pick(self, kind, tier):
        """Random variant index, never the same one twice in a row."""
        key = (kind, tier)
        n = self.counts.get(key, 0)
        if n <= 1:
            return 0
        last = self._last.get(key, -1)
        i = random.randrange(n - 1)
        if i >= last:
            i += 1
        i %= n
        self._last[key] = i
        return i


class ClickEngine:
    def __init__(self, events, audio):
        self.events = events
        self.audio = audio

        # --- configuration (set by the app, then call rebuild()) ---
        self.volume = 0.75
        self.release_volume = 0.0
        self.pack = DEFAULT_PACK
        self.pitch = True
        self.humanize = False
        self.overrides = {}
        self.watched = frozenset()
        self.hotkeys = {}            # combo -> action
        self.app_filter = None       # None or frozenset of exe names
        self.output_keys = []        # device keys to play on
        self.device_levels = {}      # device key -> level multiplier
        self.cue_key = None          # device key for start/stop cues
        self.dynamics = True         # fast taps play soft clicks
        self.soft_s = 0.12           # taps closer than this are "soft"
        self.micro_s = 0.05
        self.hard_s = 1.0

        # --- runtime state ---
        self.running = False
        self.capture_target = None   # None | "input" | "hotkey"
        self.capture_token = 0
        self.total_presses = 0
        self.lifetime = 0             # never reset (persisted by the app)
        self.counts = collections.Counter()
        self.banks = {}
        self.last_errors = {}
        self._held = {}              # ident -> (press was played, tier)
        self._kb_injected = False    # set per event by the hook filters
        self._mouse_injected = False
        self.injected_ignored = 0
        self._last_injected_note = 0.0
        self._last_device_test = 0.0
        self._last_press = 0.0
        self._hotkey_held = set()
        self._mods = {}
        self._press_times = collections.deque(maxlen=1024)
        self._fg = ForegroundTracker()

        # --- render products (swapped atomically by the worker) ---
        self._devices = []
        self._voices = {}
        self._ident_voice = {}
        self._default_voice = None
        self._cue = (None, {})

        # --- worker ---
        self._want = threading.Event()
        self._reload = False
        self._stopping = False
        self._worker = threading.Thread(target=self._work, name="ClickwaveRender", daemon=True)
        self._worker.start()
        # UI-triggered sounds (previews, cues) play from this thread, never
        # from the Tk thread: once a file dialog has run, Tk's thread is a
        # single-threaded COM apartment and must not touch the audio objects.
        self._play_jobs = queue.Queue()
        self._player = threading.Thread(target=self._play_loop, name="ClickwavePlayer", daemon=True)
        self._player.start()
        self._kb_listener = None
        self._mouse_listener = None

    # ============================================================
    # Rendering
    # ============================================================
    def rebuild(self, reload_packs=False):
        if reload_packs:
            self._reload = True
        self._want.set()

    def _work(self):
        while True:
            self._want.wait()
            self._want.clear()
            if self._stopping:
                return
            try:
                self._do_rebuild()
            except Exception:
                log.exception("Rebuild failed")
            self.events.put(("rebuilt",))

    def _load_bank(self, name, pitch, reload):
        bank = self.banks.get(name)
        if bank is not None and not reload and bank.pitch == pitch:
            return bank
        pack = get_pack(name)
        if pack is None:
            return None
        bank = SoundBank(pack)
        bank.load(pitch)
        self.banks[name] = bank
        self.last_errors[name] = bank.errors
        return bank

    def _do_rebuild(self):
        reload, self._reload = self._reload, False
        vol, rel = self.volume, self.release_volume
        pack, pitch, humanize = self.pack, self.pitch, self.humanize
        overrides, watched = dict(self.overrides), self.watched
        keys, cue_key = list(self.output_keys), self.cue_key

        devices = []
        for key in keys:
            dev = self.audio.open(key)
            if dev is not None and dev.ok:
                devices.append(dev)
        cue_dev = self.audio.open(cue_key) if cue_key else None
        self.audio.close_unused(set(keys) | ({cue_key} if cue_key else set()))

        self.fallback_from = None
        if reload:
            self.banks = {}
        if self._load_bank(pack, pitch, reload) is None:
            log.warning("Pack %r missing, falling back to Default", pack)
            self.fallback_from = pack
            pack = DEFAULT_PACK
            self._load_bank(pack, pitch, reload)
        if pack not in self.banks:
            return

        def voice_key(ident):
            ov = overrides.get(ident) or {}
            p = ov.get("pack") or pack
            if p != pack and self._load_bank(p, pitch, reload) is None:
                p = pack
            level = ov.get("volume")
            return p, (1.0 if level is None else level / 100.0), (ov.get("pan") or 0) / 100.0

        default_key = (pack, 1.0, 0.0)
        ident_voice = {i: voice_key(i) for i in watched if i in overrides}
        gains = HUMANIZE_GAINS if humanize else (1.0,)
        tiers = TIERS if self.dynamics else ("normal",)
        levels = dict(self.device_levels)
        voices = {}
        for vk in {default_key, *ident_voice.values()}:
            name, level, pan = vk
            bank = self.banks[name]
            v = Voice()
            for kind_name, kvol in (("press", vol * level), ("release", vol * level * rel)):
                if kvol <= 0:
                    continue
                for tier in tiers:
                    if not bank.has(kind_name, tier):
                        continue
                    table = {}
                    for dev in devices:
                        nch, rate, fmt = dev.fmt
                        dvol = kvol * levels.get(dev.key, 1.0)
                        if dvol > 0:
                            table[dev.key] = [render_buffer(s, dvol * g, fmt, nch, pan)
                                              for s in bank.converted(kind_name, tier, nch, rate) for g in gains]
                    v.tables[(kind_name, tier)] = table
                    v.counts[(kind_name, tier)] = len(bank.clips[(kind_name, tier)]) * len(gains)
            voices[vk] = v

        cue = {}
        if cue_dev is not None and cue_dev.ok:
            nch, rate, kind = cue_dev.fmt
            cue = {True: synth_cue(True, nch, rate, kind), False: synth_cue(False, nch, rate, kind)}

        # Publish (each assignment is atomic; play() tolerates mixing).
        self._voices = voices
        self._ident_voice = ident_voice
        self._default_voice = default_key
        self._devices = devices
        self._cue = (cue_dev, cue)

    # ============================================================
    # Playback
    # ============================================================
    def _play(self, ident, kind, tier="normal"):
        voice = self._voices.get(self._ident_voice.get(ident, self._default_voice))
        if voice is None:
            return
        tier = voice.resolve(kind, tier)
        if tier is None:
            return
        idx = voice.pick(kind, tier)
        table = voice.tables.get((kind, tier), {})
        for dev in self._devices:
            bufs = table.get(dev.key)
            if bufs and idx < len(bufs):
                dev.play(bufs[idx])

    def tier_for(self, gap):
        """Which click tier a press gets, from the time since the last one."""
        if not self.dynamics:
            return "normal"
        if gap < self.micro_s:
            return "micro"
        if gap < self.soft_s:
            return "soft"
        if gap > self.hard_s:
            return "hard"
        return "normal"

    def _play_loop(self):
        while True:
            job = self._play_jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:
                log.exception("Preview playback failed")

    # ---- previews: speakers only (fair play) ----
    # Anything not triggered by a physical press - Test buttons, pack and
    # clip previews - plays on your own speakers only, never on the stream
    # output, so it can't be used to fake clicks on a recording.
    def _preview_targets(self):
        dev = self._cue[0]
        return [dev] if dev is not None and dev.ok else []

    def preview(self, ident=None, kind="press"):
        def job():
            voice = self._voices.get(self._ident_voice.get(ident, self._default_voice))
            if voice is None:
                return
            tier = voice.resolve(kind, "normal")
            if tier is None:
                return
            idx = voice.pick(kind, tier)
            table = voice.tables.get((kind, tier), {})
            targets = self._preview_targets()
            for dev in targets:
                bufs = table.get(dev.key)
                if bufs and idx < len(bufs):
                    dev.play(bufs[idx])
                    return
            # Speakers aren't one of the click outputs: render on the fly.
            bank = self.banks.get(ident and (self.overrides.get(ident) or {}).get("pack") or self.pack)
            clips = bank.clips.get((kind, tier)) if bank else None
            if clips and targets:
                dev = targets[0]
                s_, n_, r_ = clips[idx % len(clips)]
                dn, dr, fmt = dev.fmt
                level = self.volume * (self.release_volume if kind == "release" else 1.0)
                dev.play(render_buffer(to_format(s_, n_, r_, dn, dr), level, fmt))
        self._play_jobs.put(job)

    def play_cue(self, on):
        def job():
            dev, bufs = self._cue
            if dev is not None and on in bufs:
                dev.play(bufs[on])
        self._play_jobs.put(job)

    def play_samples(self, samples, nch, rate, volume=None):
        """Preview arbitrary int16 audio on your speakers (never the stream)."""
        vol = self.volume if volume is None else volume

        def job():
            for dev in self._preview_targets():
                dn, dr, kind = dev.fmt
                dev.play(render_buffer(to_format(samples, nch, rate, dn, dr), vol, kind))
        self._play_jobs.put(job)

    def preview_device(self, key, level=1.0):
        """Play one click on a single output device (routing check).
        Limited to one per second so it can't be tapped out in rhythm."""
        now = time.perf_counter()
        if now - self._last_device_test < 1.0:
            return False
        self._last_device_test = now

        def job():
            dev = self.audio.open(key)
            bank = self.banks.get(self.pack) or self._load_bank(self.pack, self.pitch, False)
            if dev is None or not dev.ok or bank is None:
                return
            clips = bank.clips.get(("press", "normal"))
            if not clips:
                return
            s, n, r = random.choice(clips)
            dn, dr, kind = dev.fmt
            dev.play(render_buffer(to_format(s, n, r, dn, dr), max(0.3, self.volume) * level, kind))
        self._play_jobs.put(job)
        return True

    def preview_pack(self, name):
        """Load (if needed) and play a press + release from any pack."""
        def work():
            try:
                bank = self._load_bank(name, self.pitch, False)
                if bank is None:
                    return
                for kind, delay in (("press", 0.0), ("release", 0.11)):
                    clips = bank.clips.get((kind, "normal"))
                    if not clips:
                        continue
                    time.sleep(delay)
                    s, n, r = random.choice(clips)
                    level = self.release_volume if kind == "release" else 1.0
                    if level > 0:
                        self.play_samples(s, n, r, self.volume * level if kind == "release" else None)
            except Exception:
                log.exception("Pack preview failed")
        threading.Thread(target=work, daemon=True).start()

    def tidy(self):
        self.audio.tidy()

    # ============================================================
    # Stats
    # ============================================================
    def cps(self):
        now = time.perf_counter()
        return sum(1 for t in list(self._press_times) if now - t <= 1.0)

    def reset_stats(self):
        self._press_times.clear()
        self.total_presses = 0
        self.counts.clear()

    # ============================================================
    # Inputs / hooks
    # ============================================================
    def set_inputs(self, ids):
        self.watched = frozenset(ids)
        self.update_mouse_listener()

    def begin_capture(self, target):
        self.capture_token += 1
        self.capture_target = target
        self.update_mouse_listener()
        return self.capture_token

    def cancel_capture(self):
        self.capture_token += 1
        self.capture_target = None
        self.update_mouse_listener()

    # ---- fair play: only physical input makes clicks ----
    # Windows marks input created by software (bots, macro tools,
    # AutoHotkey, SendInput) as "injected". Those events never produce a
    # click, a chip highlight, a CPS count or an overlay key - so the sound
    # always comes from a real key moving, at the moment it moved.
    def _kb_filter(self, msg, data):
        self._kb_injected = bool(data.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
        return True

    def _mouse_filter(self, msg, data):
        self._mouse_injected = bool(data.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED))
        return True

    def start_listeners(self):
        if self._kb_listener is None:
            self._kb_listener = keyboard.Listener(
                on_press=lambda k: self._handle(key_to_id(k), True),
                on_release=lambda k: self._handle(key_to_id(k), False),
                win32_event_filter=self._kb_filter,
            )
            self._kb_listener.start()
        self.update_mouse_listener()

    def update_mouse_listener(self):
        """The mouse hook only runs while a mouse button is watched (or
        being captured) - no hook on every mouse move otherwise."""
        needed = (any(i.startswith("mouse:") for i in self.watched)
                  or self.capture_target == "input")
        if needed and self._mouse_listener is None:
            self._mouse_listener = mouse.Listener(on_click=self._on_click, win32_event_filter=self._mouse_filter)
            self._mouse_listener.start()
        elif not needed and self._mouse_listener is not None:
            self._mouse_listener.stop()
            self._mouse_listener = None

    def _on_click(self, x, y, button, pressed):
        self._handle("mouse:" + button.name, pressed, (x, y))

    def _active_mods(self):
        """Modifiers currently held. If Windows swallowed a key-up (UAC
        prompt, Ctrl+Alt+Del...), a modifier would look stuck forever, so
        anything held over 2 s is re-checked against the real key state."""
        now = time.perf_counter()
        active = set()
        for mod, held in self._mods.items():
            for ident, since in list(held.items()):
                if now - since < 2.0 or _physically_down(mod):
                    active.add(mod)
                else:
                    held.pop(ident, None)
        return active

    def _filter_ok(self):
        allowed = self.app_filter
        if allowed is None:
            return True
        pid, exe = self._fg.current()
        return pid == OWN_PID or exe in allowed

    def _handle(self, ident, pressed, pos=None):
        # An exception escaping a pynput callback kills the listener.
        try:
            if ident is None:
                return
            mod = MODIFIERS.get(ident)
            if mod:
                held = self._mods.setdefault(mod, {})
                if pressed:
                    held.setdefault(ident, time.perf_counter())
                else:
                    held.pop(ident, None)

            target = self.capture_target
            if target is not None:
                if not pressed:
                    return
                if ident == "key:esc":
                    self.capture_target = None
                    self.events.put(("captured", self.capture_token, target, None, None))
                elif target == "hotkey":
                    if ident.startswith("mouse:") or mod:
                        return  # wait for the main key of the combo
                    self.capture_target = None
                    combo = make_combo(self._active_mods(), ident)
                    self.events.put(("captured", self.capture_token, target, combo, None))
                else:
                    self.capture_target = None
                    self.events.put(("captured", self.capture_token, target, ident, pos))
                return

            if self.hotkeys and not mod:
                if pressed:
                    combo = make_combo(self._active_mods(), ident)
                    action = self.hotkeys.get(combo) or (self.hotkeys.get(ident) if combo != ident else None)
                    if action:
                        if ident not in self._hotkey_held:
                            self._hotkey_held.add(ident)
                            self.events.put(("hotkey", action))
                        return
                elif ident in self._hotkey_held:
                    self._hotkey_held.discard(ident)
                    return

            if ident not in self.watched:
                return
            if (self._mouse_injected if ident.startswith("mouse:") else self._kb_injected):
                # Simulated by software: never a click (see _kb_filter).
                if pressed:
                    self.injected_ignored += 1
                    now = time.perf_counter()
                    if now - self._last_injected_note > 2.0:
                        self._last_injected_note = now
                        self.events.put(("injected", ident))
                return
            if pressed:
                if ident in self._held:
                    return  # OS key-repeat while held
                now = time.perf_counter()
                play = self.running and self._filter_ok()
                tier = self.tier_for(now - self._last_press)
                self._held[ident] = (play, tier)
                if play:
                    self._last_press = now
                    self._play(ident, "press", tier)
                    self.total_presses += 1
                    self.lifetime += 1
                    self.counts[ident] += 1
                    self._press_times.append(now)
                self.events.put(("hit", ident, True))
            else:
                if ident not in self._held:
                    return
                played, tier = self._held.pop(ident)
                if played and self.running:
                    self._play(ident, "release", tier)
                self.events.put(("hit", ident, False))
        except Exception:
            log.exception("Input handler error")

    def shutdown(self):
        self._stopping = True
        self._want.set()
        self._play_jobs.put(None)
        for listener in (self._kb_listener, self._mouse_listener):
            if listener is not None:
                listener.stop()
        self._kb_listener = self._mouse_listener = None
        self.audio.close_all()

"""Output device management across the WASAPI and legacy backends.

Device keys are stable strings:
  "wasapi:<endpoint id>"   a WASAPI endpoint
  "winmm:<index>"          a legacy waveOut device
  "winmm:mapper"           legacy system default
"""

import threading

from . import wasapi, winmm
from .com import com_thread
from .common import log

CABLE_HINTS = ("Voicemeeter Input", "CABLE Input", "VoiceMeeter", "CABLE", "VB-Audio", "VAIO")


class AudioManager:
    def __init__(self, preference="auto"):
        self.com = com_thread()
        self.backend = None
        self.devices = []          # [(key, name)]
        self.default_key = None    # key of the current system default
        self._open = {}            # key -> device object
        self._lock = threading.Lock()
        self.set_backend(preference)

    # --- backend ---
    def set_backend(self, preference):
        self.close_all()
        self.backend = None
        if preference in ("auto", "wasapi"):
            try:
                self.backend = "wasapi"
                self.refresh()
                if not self.devices:
                    raise OSError("no WASAPI devices")
            except Exception as e:
                log.warning("WASAPI unavailable, using legacy output: %s", e)
                self.backend = None
        if self.backend is None:
            self.backend = "legacy"
            self.refresh()
        log.info("Audio backend: %s (%d devices)", self.backend, len(self.devices))

    def snapshot(self):
        """(devices, default) as currently reported by Windows."""
        if self.backend == "wasapi":
            def work():
                return wasapi.list_devices(), wasapi.default_device_id()
            devs, default = self.com.call(work)
            return [(f"wasapi:{i}", n) for i, n in devs], (f"wasapi:{default}" if default else None)
        devs = [(f"winmm:{i}", n) for i, n in winmm.list_devices()]
        return devs, "winmm:mapper"

    def refresh(self):
        self.devices, self.default_key = self.snapshot()
        # Drop pools for devices that vanished or died.
        with self._lock:
            live = {k for k, _ in self.devices} | {"winmm:mapper"}
            stale = [k for k, d in self._open.items() if k not in live or d.dead]
        for key in stale:
            self._close_key(key)

    def changed(self):
        """True if the device list or default device changed since refresh()."""
        try:
            devs, default = self.snapshot()
        except Exception as e:
            log.warning("Device poll failed: %s", e)
            return False
        with self._lock:
            dead = any(d.dead for d in self._open.values())
        return dead or devs != self.devices or default != self.default_key

    # --- lookup ---
    def name_of(self, key):
        if key == "winmm:mapper":
            return "System default"
        for k, n in self.devices:
            if k == key:
                return n
        return None

    def find(self, stored_name):
        """Key for a saved device name. Also matches the 31-char names v2
        saved from winmm against full WASAPI names (and vice versa)."""
        if not stored_name:
            return None
        for k, n in self.devices:
            if n == stored_name:
                return k
        stem = stored_name.rstrip("…")
        for k, n in self.devices:
            other = n.rstrip("…")
            if other.startswith(stem) or stem.startswith(other):
                return k
        return None

    def find_cable(self):
        for hint in CABLE_HINTS:
            for k, n in self.devices:
                if hint.lower() in n.lower():
                    return k
        return None

    def default(self):
        return self.default_key if self.backend == "wasapi" else "winmm:mapper"

    # --- devices ---
    def open(self, key):
        with self._lock:
            dev = self._open.get(key)
            if dev is not None and not dev.dead:
                return dev
        if dev is not None:
            self._close_key(key)
        name = self.name_of(key) or key
        try:
            if key.startswith("wasapi:"):
                endpoint = key.split(":", 1)[1]
                dev = self.com.call(lambda: wasapi.WasapiDevice(endpoint, name))
            else:
                index = winmm.WAVE_MAPPER if key == "winmm:mapper" else int(key.split(":", 1)[1])
                dev = winmm.WinmmDevice(index, name)
        except Exception as e:
            log.warning("Could not open %s: %s", name, e)
            return None
        dev.key = key
        if not dev.ok:
            log.warning("Device %s opened with no usable voices", name)
        with self._lock:
            self._open[key] = dev
        return dev

    def close_unused(self, keep):
        with self._lock:
            unused = [k for k in self._open if k not in keep]
        for key in unused:
            self._close_key(key)

    def _close_key(self, key):
        with self._lock:
            dev = self._open.pop(key, None)
        if dev is not None:
            try:
                self.com.call(dev.close)
            except Exception as e:
                log.warning("Closing %s failed: %s", key, e)

    def tidy(self):
        with self._lock:
            devs = list(self._open.values())
        if devs:
            try:
                self.com.call(lambda: [d.tidy() for d in devs])
            except Exception as e:
                log.warning("tidy failed: %s", e)

    def close_all(self):
        with self._lock:
            keys = list(self._open)
        for key in keys:
            self._close_key(key)

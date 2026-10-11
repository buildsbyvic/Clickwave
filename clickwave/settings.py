"""Persistent settings with named profiles.

Profile-scoped values (what you click with and how it sounds) live in
settings["profiles"][name]; everything else is global. Settings files
from v2 (flat keys) are migrated automatically.
"""

import copy
import json
import os
import shutil

from .common import APPDATA_DIR, SETTINGS_PATH, log

DEFAULT_INPUTS = ["vk:87", "vk:82", "key:up"]   # W, R, Up Arrow

PROFILE_DEFAULTS = {
    "volume": 75,               # percent, 0-200
    "release_volume": 0,        # percent of press volume, 0-100
    "inputs": list(DEFAULT_INPUTS),
    "overrides": {},            # ident -> {"pack": name|None, "volume": pct|None}
    "pack": "Default",
    "pitch_variation": True,
    "humanize": False,
    "app_filter": False,        # only click while listed apps are focused
    "apps": [],                 # exe names, lowercase
    "dynamics": True,           # fast taps play soft clicks
    "soft_ms": 120,             # taps closer together than this are "soft"
}

HOTKEY_ACTIONS = [
    ("toggle", "Start / stop listening"),
    ("vol_up", "Volume up"),
    ("vol_down", "Volume down"),
    ("next_profile", "Next profile"),
    ("mute_self", "Mute / unmute myself"),
]

GLOBAL_DEFAULTS = {
    "version": 3,
    "profiles": {"Default": copy.deepcopy(PROFILE_DEFAULTS)},
    "active_profile": "Default",
    "hotkeys": {"toggle": "key:f8", "vol_up": None, "vol_down": None,
                "next_profile": None, "mute_self": None},
    # audio output
    "backend": "auto",          # auto | wasapi | legacy
    "hear_myself": True,
    "send_to_stream": True,
    "monitor_device": None,     # None = system default, else device name
    "stream_device": "auto",    # "auto" | None | device name
    "monitor_level": 100,       # percent, applied on top of the volume
    "stream_level": 100,
    # feedback
    "toast": True,
    "toast_position": "top",    # top | bottom | top-right | bottom-right
    "cue_sound": False,
    # window / startup
    "always_on_top": False,
    "start_on_launch": False,
    "tray": True,
    "minimize_to_tray": False,
    "close_to_tray": False,
    "start_minimized": False,
    "launch_on_startup": False,
    # stream overlay
    "overlay": False,
    "overlay_port": 24730,
    "stats_files": False,
    # appearance
    "theme": "dark",
    "accent": "violet",
    "snap": True,
    # state
    "window": None,             # [x, y, w, h]
    "tab": 0,
    "lifetime_clicks": 0,
    "onboarded": False,
    "best_cps": 0,
    "best_test": 0.0,
    "check_updates": True,      # ask GitHub for the latest version number on launch
}


def _same_type(value, default):
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def clean_profile(data):
    out = copy.deepcopy(PROFILE_DEFAULTS)
    if not isinstance(data, dict):
        return out
    for key, default in PROFILE_DEFAULTS.items():
        if key in data and _same_type(data[key], default):
            out[key] = copy.deepcopy(data[key])
    out["volume"] = max(0, min(200, out["volume"]))
    out["soft_ms"] = max(40, min(250, out["soft_ms"]))
    out["release_volume"] = max(0, min(100, out["release_volume"]))
    out["inputs"] = list(dict.fromkeys(i for i in out["inputs"] if isinstance(i, str) and ":" in i))
    out["apps"] = list(dict.fromkeys(a.lower() for a in out["apps"] if isinstance(a, str) and a))
    overrides = {}
    for ident, ov in out["overrides"].items():
        if not isinstance(ov, dict):
            continue
        pack = ov.get("pack") if isinstance(ov.get("pack"), str) else None
        vol = ov.get("volume") if isinstance(ov.get("volume"), int) else None
        pan = ov.get("pan") if isinstance(ov.get("pan"), int) else 0
        if pack or vol is not None or pan:
            overrides[ident] = {"pack": pack, "volume": None if vol is None else max(0, min(200, vol)),
                                "pan": max(-100, min(100, pan))}
    out["overrides"] = overrides
    return out


def migrate_v2(data):
    """v2 stored a single flat profile; fold it into the v3 layout."""
    profile = {k: data[k] for k in ("volume", "release_volume", "inputs", "pitch_variation") if k in data}
    new = {k: v for k, v in data.items() if k in GLOBAL_DEFAULTS}
    new["profiles"] = {"Default": profile}
    new["active_profile"] = "Default"
    new["hotkeys"] = {"toggle": data.get("toggle_hotkey", "key:f8")}
    pos = data.get("window_pos")
    if isinstance(pos, list) and len(pos) == 2:
        new["window"] = [pos[0], pos[1], None, None]
    new["version"] = 3
    log.info("Migrated v2 settings")
    return new


class Settings:
    def __init__(self, path=SETTINGS_PATH):
        self.path = path
        self.data = copy.deepcopy(GLOBAL_DEFAULTS)
        self._load()

    # --- global ---
    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    # --- profiles ---
    @property
    def profile_name(self):
        return self.data["active_profile"]

    @property
    def profile(self):
        return self.data["profiles"][self.data["active_profile"]]

    def p(self, key):
        return self.profile[key]

    def pset(self, key, value):
        self.profile[key] = value

    def profile_names(self):
        return list(self.data["profiles"].keys())

    def create_profile(self, name, copy_from=None):
        src = self.data["profiles"].get(copy_from) if copy_from else None
        self.data["profiles"][name] = copy.deepcopy(src) if src else copy.deepcopy(PROFILE_DEFAULTS)
        self.data["active_profile"] = name

    def rename_profile(self, old, new):
        profiles = self.data["profiles"]
        self.data["profiles"] = {(new if k == old else k): v for k, v in profiles.items()}
        if self.data["active_profile"] == old:
            self.data["active_profile"] = new

    def delete_profile(self, name):
        if len(self.data["profiles"]) <= 1:
            return False
        del self.data["profiles"][name]
        if self.data["active_profile"] == name:
            self.data["active_profile"] = next(iter(self.data["profiles"]))
        return True

    def switch_profile(self, name):
        if name in self.data["profiles"]:
            self.data["active_profile"] = name

    def rename_pack_everywhere(self, old, new):
        for prof in self.data["profiles"].values():
            if prof["pack"] == old:
                prof["pack"] = new
            for ov in prof["overrides"].values():
                if ov.get("pack") == old:
                    ov["pack"] = new

    # --- io ---
    def _load(self):
        try:
            # utf-8-sig: tolerate a BOM (Notepad's "UTF-8 with BOM", PowerShell)
            with open(self.path, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            # Keep the broken file so a later save can't destroy someone's setup.
            try:
                shutil.copy2(self.path, self.path + ".bad")
            except OSError:
                pass
            log.warning("Settings unreadable, using defaults (copy kept as settings.json.bad): %s", e)
            return
        if not isinstance(loaded, dict):
            return
        if "version" not in loaded and ("volume" in loaded or "inputs" in loaded):
            loaded = migrate_v2(loaded)
        # Existing users (settings from an earlier version) skip the welcome card.
        loaded.setdefault("onboarded", True)

        for key, default in GLOBAL_DEFAULTS.items():
            if key in ("profiles", "hotkeys"):
                continue
            if key in loaded and (loaded[key] is None or _same_type(loaded[key], default)):
                if loaded[key] is None and default is not None and key not in (
                        "monitor_device", "stream_device", "window"):
                    continue
                self.data[key] = loaded[key]

        profiles = loaded.get("profiles")
        if isinstance(profiles, dict) and profiles:
            self.data["profiles"] = {str(name): clean_profile(p) for name, p in profiles.items()}
        if self.data["active_profile"] not in self.data["profiles"]:
            self.data["active_profile"] = next(iter(self.data["profiles"]))

        hotkeys = dict(GLOBAL_DEFAULTS["hotkeys"])
        if isinstance(loaded.get("hotkeys"), dict):
            for action in hotkeys:
                value = loaded["hotkeys"].get(action, hotkeys[action])
                hotkeys[action] = value if (value is None or isinstance(value, str)) else None
        self.data["hotkeys"] = hotkeys
        self.data["lifetime_clicks"] = max(0, int(self.data["lifetime_clicks"]))
        self.data["tab"] = max(0, min(2, int(self.data["tab"])))
        for key in ("monitor_level", "stream_level"):
            self.data[key] = max(0, min(150, int(self.data[key])))
        if isinstance(self.data["best_test"], int):
            self.data["best_test"] = float(self.data["best_test"])

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or APPDATA_DIR, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            log.warning("Could not save settings: %s", e)
            self.last_error = e
            return False

    def reset(self):
        keep = {"window": self.data.get("window"), "lifetime_clicks": self.data.get("lifetime_clicks", 0)}
        self.data = copy.deepcopy(GLOBAL_DEFAULTS)
        self.data.update(keep)

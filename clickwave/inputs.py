"""Input identities, labels and hotkey combos.

Inputs are stored as strings so they can live in the settings file:
  "vk:87"       a normal key by Windows virtual-key code (W). Using the
                vk instead of the character means Shift/Ctrl held down
                doesn't change which key it is.
  "key:up"      a pynput special key (arrows, F-keys, Space, ...)
  "mouse:left"  a mouse button
Hotkeys may add modifiers: "ctrl+shift+key:f8".
"""

import ctypes

from pynput import keyboard

_KEY_LABELS = {
    "up": "↑ Up", "down": "↓ Down", "left": "← Left",
    "right": "→ Right", "space": "Space", "enter": "Enter", "esc": "Esc",
    "tab": "Tab", "backspace": "Backspace", "caps_lock": "Caps Lock",
    "shift": "Shift", "shift_l": "Shift", "shift_r": "Right Shift",
    "ctrl": "Ctrl", "ctrl_l": "Ctrl", "ctrl_r": "Right Ctrl",
    "alt": "Alt", "alt_l": "Alt", "alt_r": "Right Alt", "alt_gr": "Alt Gr",
    "cmd": "Win", "cmd_l": "Win", "cmd_r": "Right Win",
    "page_up": "Page Up", "page_down": "Page Down", "num_lock": "Num Lock",
    "print_screen": "Print Screen", "scroll_lock": "Scroll Lock",
    "media_play_pause": "Play/Pause", "media_next": "Next Track",
    "media_previous": "Prev Track", "media_volume_up": "Vol Up",
    "media_volume_down": "Vol Down", "media_volume_mute": "Mute",
}
_VK_LABELS = {
    0x6A: "Num *", 0x6B: "Num +", 0x6D: "Num -", 0x6E: "Num .", 0x6F: "Num /",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}
_MOUSE_LABELS = {
    "left": "Left Click", "right": "Right Click", "middle": "Middle Click",
    "x1": "Mouse 4", "x2": "Mouse 5",
}

# Physical modifier keys -> the logical modifier they represent.
MODIFIERS = {
    "key:ctrl": "ctrl", "key:ctrl_l": "ctrl", "key:ctrl_r": "ctrl",
    "key:shift": "shift", "key:shift_l": "shift", "key:shift_r": "shift",
    "key:alt": "alt", "key:alt_l": "alt", "key:alt_r": "alt", "key:alt_gr": "alt",
    "key:cmd": "win", "key:cmd_l": "win", "key:cmd_r": "win",
}
MOD_ORDER = ("ctrl", "alt", "shift", "win")
_MOD_LABELS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}


def key_to_id(key):
    if isinstance(key, keyboard.Key):
        return "key:" + key.name
    vk = getattr(key, "vk", None)
    if vk is not None:
        return f"vk:{vk}"
    char = getattr(key, "char", None)
    if char:
        return "char:" + char.lower()
    return None


def make_combo(mods, ident):
    ordered = [m for m in MOD_ORDER if m in mods]
    return "+".join(ordered + [ident])


def split_combo(combo):
    """'ctrl+shift+key:f8' -> ({'ctrl','shift'}, 'key:f8')."""
    if not combo:
        return set(), None
    parts = combo.split("+")
    mods = {p for p in parts[:-1] if p in MOD_ORDER}
    return mods, parts[-1]


def input_label(ident):
    if not ident:
        return "None"
    if "+" in ident and ident.split("+")[0] in MOD_ORDER:
        mods, base = split_combo(ident)
        return " + ".join([_MOD_LABELS[m] for m in MOD_ORDER if m in mods] + [input_label(base)])
    kind, _, value = ident.partition(":")
    if kind == "key":
        if value.startswith("f") and value[1:].isdigit():
            return value.upper()
        return _KEY_LABELS.get(value, value.replace("_", " ").title())
    if kind == "mouse":
        return _MOUSE_LABELS.get(value, value.title())
    if kind == "char":
        return value.upper()
    if kind == "vk":
        try:
            vk = int(value)
        except ValueError:
            return value
        if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            return chr(vk)
        if 0x60 <= vk <= 0x69:
            return f"Num {vk - 0x60}"
        if vk in _VK_LABELS:
            return _VK_LABELS[vk]
        try:
            ch = ctypes.windll.user32.MapVirtualKeyW(vk, 2) & 0xFFFF
            if ch:
                return chr(ch).upper()
        except Exception:
            pass
        return f"Key {vk}"
    return ident

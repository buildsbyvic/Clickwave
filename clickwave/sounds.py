"""Sound packs, rendering, and importers.

A pack is a folder of audio clips. Each file's name says what it is:
    press_01.wav, release_01.wav            normal clicks
    soft_press_01.wav, soft_release_01.wav  played during fast spam
    hard_press_01.wav ...                   played after a long pause
    micro_press_01.wav ...                  very fast spam
(Names containing "release"/"_up" are releases; "soft"/"hard"/"micro"
anywhere in the name picks the tier, so GD clickpack names work too.)

The bundled clips form the read-only "Default" pack; user packs live in
%APPDATA%\\Clickwave\\packs\\<name>.
"""

import array
import ctypes
import json
import os
import re
import shutil
import tempfile
import zipfile

from .common import APP_VERSION, BUNDLED_SOUNDS_DIR, LEGACY_SOUNDS_DIR, PACKS_DIR, log
from .decode import (
    AUDIO_EXTENSIONS, DecodeError, decode_any, fade, resample, to_format, write_wav,
)

DEFAULT_PACK = "Default"
PITCH_FACTORS = (0.96, 0.98, 1.0, 1.02, 1.04)
HUMANIZE_GAINS = (0.84, 1.0, 1.14)
TIERS = ("normal", "soft", "hard", "micro")
FALLBACK = {
    "normal": ("normal",),
    "soft": ("soft", "normal"),
    "hard": ("hard", "normal"),
    "micro": ("micro", "soft", "normal"),
}
MAX_VARIANTS = 40          # per (kind, tier), after pitch expansion (memory cap)
MAX_IMPORT = 24            # clips kept per (kind, tier) when importing big packs
MAX_CLIP_SECONDS = 1.0     # longer files get trimmed + faded
MAX_ZIP_BYTES = 400 * 1024 * 1024
PACK_META = "clickwave-pack.json"
LEGACY_PACK_META = "gdclick-pack.json"


# ============================================================
# Naming
# ============================================================

def classify(filename):
    """'softreleases_3.wav' -> ('release', 'soft')."""
    n = os.path.splitext(os.path.basename(filename).lower())[0]
    kind = "release" if ("release" in n or n.startswith("up_") or "_up" in n or "keyup" in n
                         or "key_up" in n) else "press"
    tier = "normal"
    for t in ("soft", "hard", "micro"):
        if t in n:
            tier = t
            break
    return kind, tier


def clip_name(kind, tier, index):
    return f"{'' if tier == 'normal' else tier + '_'}{kind}_{index:02d}.wav"


def _natural_key(text):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def _spread(items, limit):
    """Evenly pick `limit` items from a long list (keeps variety)."""
    if len(items) <= limit:
        return list(items)
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


# ============================================================
# Packs on disk
# ============================================================

class Pack:
    def __init__(self, name, path, builtin=False):
        self.name, self.path, self.builtin = name, path, builtin

    def files(self):
        """{(kind, tier): [paths]}, naturally sorted."""
        out = {}
        try:
            names = os.listdir(self.path)
        except OSError:
            return out
        for name in sorted(names, key=_natural_key):
            if name.lower().endswith(AUDIO_EXTENSIONS):
                out.setdefault(classify(name), []).append(os.path.join(self.path, name))
        return out

    def summary(self):
        files = self.files()
        n_press = sum(len(v) for (k, _), v in files.items() if k == "press")
        n_release = sum(len(v) for (k, _), v in files.items() if k == "release")
        tiers = sorted({t for (_, t) in files if t != "normal"}, key=TIERS.index)
        return n_press, n_release, tiers


def list_packs():
    packs = [Pack(DEFAULT_PACK, BUNDLED_SOUNDS_DIR, builtin=True)]
    try:
        for name in sorted(os.listdir(PACKS_DIR), key=_natural_key):
            path = os.path.join(PACKS_DIR, name)
            if os.path.isdir(path) and name != DEFAULT_PACK:
                packs.append(Pack(name, path))
    except OSError:
        pass
    return packs


def get_pack(name):
    for pack in list_packs():
        if pack.name == name:
            return pack
    return None


def safe_pack_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    return name[:48]


def unique_pack_name(base):
    base = safe_pack_name(base) or "My pack"
    existing = {p.name.lower() for p in list_packs()}
    if base.lower() not in existing:
        return base
    n = 2
    while f"{base} {n}".lower() in existing:
        n += 1
    return f"{base} {n}"


def new_pack_dir(name):
    path = os.path.join(PACKS_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path


def rename_pack(old, new):
    os.replace(os.path.join(PACKS_DIR, old), os.path.join(PACKS_DIR, new))


def delete_pack(name):
    """Move a pack to the Recycle Bin (falls back to deleting)."""
    from .winutil import recycle
    path = os.path.join(PACKS_DIR, name)
    if not recycle(path):
        shutil.rmtree(path, ignore_errors=True)


def retag_clip(path, kind, tier):
    """Rename a clip so it's treated as another kind/tier. Returns the new path."""
    folder = os.path.dirname(path)
    ext = os.path.splitext(path)[1].lower()
    i = 1
    while True:
        candidate = os.path.join(folder, clip_name(kind, tier, i).replace(".wav", ext))
        if not os.path.exists(candidate):
            break
        i += 1
    os.replace(path, candidate)
    return candidate


def add_clips(pack_name, paths):
    """Decode audio files into an existing pack (names decide kind/tier)."""
    pack = get_pack(pack_name)
    errors, added = [], 0
    for path in paths:
        kind, tier = classify(path)
        try:
            s, n, r = decode_any(path, max_seconds=MAX_CLIP_SECONDS + 1)
        except (DecodeError, OSError, EOFError, ValueError) as e:
            errors.append(f"{os.path.basename(path)}: {e}")
            continue
        s, n, r = _limit_length(trim_silence(s, n, r), n, r)
        i = 1
        while os.path.exists(os.path.join(pack.path, clip_name(kind, tier, i))):
            i += 1
        write_wav(os.path.join(pack.path, clip_name(kind, tier, i)), s, n, r)
        added += 1
    return added, errors


def duplicate_pack(name):
    src = get_pack(name)
    new = unique_pack_name(f"{name} copy")
    dst = new_pack_dir(new)
    for paths in src.files().values():
        for path in paths:
            shutil.copy2(path, dst)
    return new


def migrate_legacy_sounds():
    """v2 kept custom sounds in %APPDATA%\\Clickwave\\sounds; make it a pack."""
    try:
        files = [f for f in os.listdir(LEGACY_SOUNDS_DIR) if f.lower().endswith(AUDIO_EXTENSIONS)]
    except OSError:
        return None
    if not files:
        return None
    name = unique_pack_name("My sounds")
    dst = new_pack_dir(name)
    for f in files:
        shutil.move(os.path.join(LEGACY_SOUNDS_DIR, f), os.path.join(dst, f))
    log.info("Moved v2 custom sounds into pack %r", name)
    return name


# ============================================================
# Sound bank: decoded variants of one pack
# ============================================================

def soften(samples, nch, amount=0.62):
    """Make a 'soft click' from a normal one: quieter and a little duller
    (one-pole low-pass), like a key that isn't bottomed out hard."""
    out = array.array("h", bytes(len(samples) * 2))
    a = 0.42
    for c in range(nch):
        y = 0.0
        ch = samples[c::nch]
        res = []
        append = res.append
        for x in ch:
            y += a * (x - y)
            append(int(y * amount))
        out[c::nch] = array.array("h", res)
    return out


class SoundBank:
    def __init__(self, pack):
        self.pack = pack
        self.clips = {}        # (kind, tier) -> [(array('h'), nch, rate)] after pitch variation
        self.file_counts = {}  # (kind, tier) -> number of source files
        self.auto_soft = False
        self.errors = []
        self.pitch = None
        self._converted = {}

    def load(self, pitch_variation):
        errors = []
        decoded = {}
        for key, paths in self.pack.files().items():
            clips = []
            for path in _spread(paths, MAX_VARIANTS):
                try:
                    samples, nch, rate = decode_any(path, max_seconds=MAX_CLIP_SECONDS + 0.5)
                    clips.append(_limit_length(samples, nch, rate))
                except (DecodeError, OSError, EOFError, ValueError) as e:
                    errors.append(f"{os.path.basename(path)}: {e}")
            if clips:
                decoded[key] = clips
        # Packs without soft clicks get generated ones for spam dynamics.
        self.auto_soft = False
        for kind in ("press", "release"):
            if (kind, "soft") not in decoded and (kind, "normal") in decoded:
                decoded[(kind, "soft")] = [(soften(s, n), n, r) for s, n, r in decoded[(kind, "normal")]]
                self.auto_soft = True
        factors = PITCH_FACTORS if pitch_variation else (1.0,)
        clips_out = {}
        for key, clips in decoded.items():
            f = factors
            while len(clips) * len(f) > MAX_VARIANTS and len(f) > 1:
                f = (0.97, 1.0, 1.03) if len(f) > 3 else (1.0,)
            clips_out[key] = [(resample(s, n, fac), n, r) for s, n, r in clips for fac in f]
        self.clips = clips_out
        self.file_counts = {k: len(v) for k, v in decoded.items()}
        self.errors = errors
        self.pitch = pitch_variation
        self._converted = {}
        if errors:
            log.warning("Pack %r: %s", self.pack.name, "; ".join(errors))

    def has(self, kind, tier="normal"):
        return bool(self.clips.get((kind, tier)))

    def converted(self, kind, tier, nch, rate):
        key = (kind, tier, nch, rate)
        out = self._converted.get(key)
        if out is None:
            out = [to_format(s, n, r, nch, rate) for s, n, r in self.clips.get((kind, tier), [])]
            self._converted[key] = out
        return out


LEAD_IN_SECONDS = 0.002


def align_start(samples, nch, rate):
    """Fair play: drop silence before a clip's attack so the click always
    sounds within ~2 ms of the physical press - a pack can't delay it."""
    from .slicer import onset_frame
    start = max(0, onset_frame(samples, nch, rate) - int(LEAD_IN_SECONDS * rate))
    return samples[start * nch:] if start else samples


def _limit_length(samples, nch, rate):
    samples = align_start(samples, nch, rate)
    max_frames = int(MAX_CLIP_SECONDS * rate)
    if len(samples) // nch > max_frames:
        samples = fade(samples[: max_frames * nch], nch, 0, int(0.05 * rate))
    return samples, nch, rate


def pan_gains(pan, nch):
    """Per-channel gains for pan in [-1, 1] (only the front L/R move)."""
    gains = [1.0] * nch
    if nch >= 2 and pan:
        gains[0] = min(1.0, 1.0 - pan)
        gains[1] = min(1.0, 1.0 + pan)
    return gains


def render_buffer(samples, volume, kind, nch=1, pan=0.0):
    """int16 samples -> ctypes buffer in the device sample format."""
    if pan and nch >= 2:
        g = pan_gains(pan, nch)
        samples = array.array("h", [int(s * g[i % nch]) for i, s in enumerate(samples)])
    if kind == "f32":
        k = volume / 32768.0
        if volume <= 1.0:
            data = array.array("f", [s * k for s in samples])
        else:
            data = array.array("f", [
                1.0 if v > 1.0 else (-1.0 if v < -1.0 else v) for v in (s * k for s in samples)
            ])
    elif kind == "s32":
        data = array.array("i", [
            2147483647 if v > 2147483647 else (-2147483648 if v < -2147483648 else v)
            for v in (int(s * volume * 65536) for s in samples)
        ])
    else:
        if volume == 1.0:
            data = samples
        elif volume < 1.0:
            data = array.array("h", [int(s * volume) for s in samples])
        else:
            data = array.array("h", [
                32767 if v > 32767 else (-32768 if v < -32768 else v)
                for v in (int(s * volume) for s in samples)
            ])
    raw = data.tobytes()
    return ctypes.create_string_buffer(raw, len(raw))


def synth_cue(on, nch, rate, kind):
    """Two short soft blips: rising for 'on', falling for 'off'."""
    import math
    notes = (660, 990) if on else (990, 660)
    samples = array.array("h")
    for freq in notes:
        n = int(0.055 * rate)
        for i in range(n):
            env = min(1.0, i / (0.004 * rate)) * min(1.0, (n - i) / (0.02 * rate))
            v = int(9000 * env * math.sin(2 * math.pi * freq * i / rate))
            samples.extend([v] * nch)
    return render_buffer(samples, 1.0, kind)


def save_pack(name, press, release, nch, rate):
    path = new_pack_dir(name)
    for i, clip in enumerate(press, 1):
        write_wav(os.path.join(path, clip_name("press", "normal", i)), clip, nch, rate)
    for i, clip in enumerate(release, 1):
        write_wav(os.path.join(path, clip_name("release", "normal", i)), clip, nch, rate)
    return path


# ============================================================
# Importers
# ============================================================

def trim_silence(samples, nch, rate, floor=0.012):
    """Strip leading/trailing near-silence (keeps a 2 ms lead-in)."""
    level = int(32767 * floor)
    frames = len(samples) // nch
    first, last = None, None
    mono = samples[0::nch] if nch > 1 else samples
    for i in range(frames):
        if abs(mono[i]) > level:
            first = i
            break
    if first is None:
        return samples
    for i in range(frames - 1, first, -1):
        if abs(mono[i]) > level:
            last = i
            break
    first = max(0, first - int(0.002 * rate))
    last = min(frames, (last or frames) + int(0.02 * rate))
    return fade(samples[first * nch:last * nch], nch, int(0.001 * rate), int(0.004 * rate))


class ImportResult:
    def __init__(self):
        self.packs = []        # [(name, n_press, n_release, tiers)]
        self.errors = []
        self.kind = ""         # "clickpack" | "mechvibes" | "files"

    @property
    def message(self):
        parts = []
        for name, n_press, n_release, tiers in self.packs:
            extra = f" + {', '.join(tiers)} clicks" if tiers else ""
            parts.append(f"'{name}' ({n_press} press · {n_release} release{extra})")
        return ", ".join(parts)


def _write_grouped(name, grouped, trim=False):
    """grouped: {(kind, tier): [paths]} -> decode + write a pack."""
    dest = new_pack_dir(name)
    errors, counts = [], {}
    for (kind, tier), paths in grouped.items():
        index = 0
        for path in _spread(sorted(paths, key=_natural_key), MAX_IMPORT):
            try:
                s, n, r = decode_any(path, max_seconds=MAX_CLIP_SECONDS + 1)
            except (DecodeError, OSError, EOFError, ValueError) as e:
                errors.append(f"{os.path.basename(path)}: {e}")
                continue
            if trim:
                s = trim_silence(s, n, r)
            s, n, r = _limit_length(s, n, r)
            index += 1
            write_wav(os.path.join(dest, clip_name(kind, tier, index)), s, n, r)
        if index:
            counts[(kind, tier)] = index
    if not counts:
        shutil.rmtree(dest, ignore_errors=True)
        return None, errors
    n_press = sum(v for (k, _), v in counts.items() if k == "press")
    n_release = sum(v for (k, _), v in counts.items() if k == "release")
    tiers = sorted({t for (_, t) in counts if t != "normal"}, key=TIERS.index)
    return (name, n_press, n_release, tiers), errors


def import_files(paths, name):
    """Decode chosen audio files into a new pack. File names decide
    press/release and tier. Returns ImportResult."""
    result = ImportResult()
    result.kind = "files"
    grouped = {}
    for path in paths:
        grouped.setdefault(classify(path), []).append(path)
    info, errors = _write_grouped(unique_pack_name(name), grouped, trim=True)
    result.errors = errors
    if info:
        result.packs.append(info)
    return result


# GD clickpack layout (ZCB and friends): player1/clicks, releases, ...
CLICKPACK_DIRS = {
    "clicks": ("press", "normal"), "releases": ("release", "normal"),
    "softclicks": ("press", "soft"), "softreleases": ("release", "soft"),
    "hardclicks": ("press", "hard"), "hardreleases": ("release", "hard"),
    "microclicks": ("press", "micro"), "microreleases": ("release", "micro"),
}


def _walk(root, depth=4):
    root = os.path.abspath(root)
    base = root.count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base >= depth:
            dirnames[:] = []
        dirnames.sort(key=_natural_key)
        yield dirpath, dirnames, filenames


def find_clickpack_players(root):
    """[(label, folder)] for every folder holding clicks/releases/... dirs."""
    players = []
    for dirpath, dirnames, _ in _walk(root):
        if any(d.lower() in CLICKPACK_DIRS for d in dirnames):
            players.append((os.path.basename(dirpath), dirpath))
    return players


def find_mechvibes(root):
    for dirpath, _, filenames in _walk(root, depth=3):
        if "config.json" in filenames:
            try:
                with open(os.path.join(dirpath, "config.json"), "r", encoding="utf-8-sig") as f:
                    cfg = json.load(f)
                if isinstance(cfg, dict) and "defines" in cfg:
                    return dirpath
            except (OSError, ValueError):
                continue
    return None


def import_clickpack(root, name):
    result = ImportResult()
    result.kind = "clickpack"
    players = find_clickpack_players(root)
    for i, (label, folder) in enumerate(players):
        grouped = {}
        for sub in os.listdir(folder):
            key = CLICKPACK_DIRS.get(sub.lower())
            path = os.path.join(folder, sub)
            if key and os.path.isdir(path):
                files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(AUDIO_EXTENSIONS)]
                if files:
                    grouped.setdefault(key, []).extend(files)
        if not grouped:
            continue
        suffix = ""
        if len(players) > 1:
            m = re.search(r"(\d+)$", label)
            suffix = f" P{m.group(1)}" if m else f" {label}"
            if i == 0 and m and m.group(1) == "1":
                suffix = ""
        info, errors = _write_grouped(unique_pack_name(name + suffix), grouped)
        result.errors += errors
        if info:
            result.packs.append(info)
    return result


def read_mechvibes_config(folder):
    path = os.path.join(folder, "config.json")
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def import_mechvibes(folder, max_clips=MAX_IMPORT, name=None):
    """Import a Mechvibes sound pack folder (config.json + audio).
    Supports 'single' (one file + [start_ms, duration_ms] slices) and
    'multi' (one file per key) packs, plus key-up sounds where present."""
    cfg = read_mechvibes_config(folder)
    name = unique_pack_name(name or cfg.get("name") or os.path.basename(folder.rstrip("\\/")))
    errors = []
    results = {"press": [], "release": []}

    def take_slices(sound_file, defines, kind):
        segments = sorted({tuple(v) for v in defines.values()
                           if isinstance(v, (list, tuple)) and len(v) == 2})
        if not segments:
            return
        samples, nch, rate = decode_any(os.path.join(folder, sound_file))
        for start_ms, dur_ms in _spread(segments, max_clips):
            a = int(start_ms * rate / 1000) * nch
            b = int((start_ms + dur_ms) * rate / 1000) * nch
            clip = samples[a:b]
            if len(clip) >= nch * 8:
                results[kind].append((fade(clip, nch, int(0.001 * rate), int(0.005 * rate)), nch, rate))

    def take_files(defines, kind):
        files = sorted({v for v in defines.values() if isinstance(v, str) and v})
        for fname in _spread(files, max_clips):
            try:
                s, n, r = decode_any(os.path.join(folder, fname), max_seconds=MAX_CLIP_SECONDS + 1)
                results[kind].append(_limit_length(trim_silence(s, n, r), n, r))
            except (DecodeError, OSError, EOFError, ValueError) as e:
                errors.append(f"{fname}: {e}")

    single = cfg.get("key_define_type", "single") == "single"
    for kind, sound_key, defines_key in (("press", "sound", "defines"), ("release", "sound_up", "defines_up")):
        defines = cfg.get(defines_key)
        if not isinstance(defines, dict):
            continue
        if single:
            sound = cfg.get(sound_key) or cfg.get("sound")
            if sound:
                take_slices(sound, defines, kind)
        else:
            take_files(defines, kind)

    if not results["press"]:
        raise DecodeError("no usable sounds found in this Mechvibes pack" +
                          (f" ({errors[0]})" if errors else ""))
    dest = new_pack_dir(name)
    for kind in ("press", "release"):
        for i, (clip, nch, rate) in enumerate(results[kind], 1):
            write_wav(os.path.join(dest, clip_name(kind, "normal", i)), clip, nch, rate)
    result = ImportResult()
    result.kind = "mechvibes"
    result.errors = errors
    result.packs.append((name, len(results["press"]), len(results["release"]), []))
    return result


def import_folder(root, name=None):
    """Auto-detect what a folder is (GD clickpack, Mechvibes pack, a GD
    Click export, or loose audio files) and import it."""
    name = name or os.path.basename(os.path.abspath(root).rstrip("\\/")) or "Imported pack"
    for meta_name in (PACK_META, LEGACY_PACK_META):   # packs exported by GD Click work too
        meta = os.path.join(root, meta_name)
        if os.path.exists(meta):
            try:
                with open(meta, "r", encoding="utf-8-sig") as f:
                    name = json.load(f).get("name") or name
            except (OSError, ValueError):
                pass
            break
    if find_clickpack_players(root):
        return import_clickpack(root, name)
    mv = find_mechvibes(root)
    if mv:
        return import_mechvibes(mv, name=None)
    files = [os.path.join(d, f) for d, _, fs in _walk(root, depth=2) for f in fs
             if f.lower().endswith(AUDIO_EXTENSIONS)]
    if not files:
        raise DecodeError("no audio files found")
    result = import_files(files, name)
    return result


def import_zip(path):
    """Import a .zip: GD clickpack, Mechvibes pack, Clickwave export or
    plain audio files."""
    with zipfile.ZipFile(path) as zf:
        total = sum(i.file_size for i in zf.infolist())
        if total > MAX_ZIP_BYTES:
            raise DecodeError("zip is too large (over 400 MB uncompressed)")
        with tempfile.TemporaryDirectory(prefix="clickwave-") as tmp:
            zf.extractall(tmp)   # zipfile strips absolute paths and '..'
            entries = [e for e in os.listdir(tmp) if not e.startswith("__MACOSX")]
            root = os.path.join(tmp, entries[0]) if len(entries) == 1 and os.path.isdir(
                os.path.join(tmp, entries[0])) else tmp
            name = os.path.splitext(os.path.basename(path))[0]
            if root != tmp:
                name = os.path.basename(root) or name
            return import_folder(root, name)


def export_pack(name, zip_path):
    pack = get_pack(name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(PACK_META, json.dumps({"name": name, "app": "Clickwave", "version": APP_VERSION}))
        for paths in pack.files().values():
            for p in paths:
                zf.write(p, os.path.basename(p))

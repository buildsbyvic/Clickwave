"""Click detection for the 'Import recording' feature.

How it works (all in decibels, 1 ms frames):

1. Analysis (once per file): short-time energy in dB, a peak envelope
   for drawing, and the recording's noise floor (a low percentile of
   the energy - the level of the room between clicks).
2. Onsets: a click starts where the level jumps sharply compared with
   the quietest point of the previous few milliseconds. Comparing with
   the local past (instead of a fixed threshold) is what lets a
   key-release be found even while the press is still ringing, and
   what keeps loud and quiet clicks equally detectable.
3. Ends: a clip runs until the sound has really decayed back to the
   noise floor (plus a small margin), or until just before the next
   click - so clicks are never chopped off mid-ring.
4. Press/release: in typing-style recordings each keystroke is a
   press followed by a release a few dozen ms later; 'auto' spots that
   rhythm, or it can be forced.

Every threshold is exposed as a DetectOptions field so the UI can offer
fine control, and detection re-runs in milliseconds on the cached
analysis when a slider moves.
"""

import array
import math
import operator
import statistics

from .decode import fade, peak

FRAME_MS = 1.0


class DetectOptions:
    def __init__(self, sensitivity=60, min_gap_ms=12, max_len_ms=350, tail_db=6.0,
                 preroll_ms=3, pair_window_ms=180, mode="auto", decay_db_per_ms=0.6, group_ms=30):
        self.sensitivity = sensitivity      # 0..100
        self.group_ms = group_ms            # sounds closer than this are one click
        self.decay_db_per_ms = decay_db_per_ms  # how fast the memory of the last click fades
        self.min_gap_ms = min_gap_ms        # two clicks can't start closer than this
        self.max_len_ms = max_len_ms        # longest a clip may be
        self.tail_db = tail_db              # clip ends this many dB above the noise floor
        self.preroll_ms = preroll_ms        # kept before the attack
        self.pair_window_ms = pair_window_ms  # release must follow its press within this
        self.mode = mode                    # auto | pairs | press

    def thresholds(self):
        s = max(0.0, min(1.0, self.sensitivity / 100.0))
        rise_db = 20.0 - 14.0 * s           # strict 20 dB jump ... sensitive 6 dB
        margin_db = 16.0 - 11.0 * s         # level above noise needed: 16 ... 5 dB
        return rise_db, margin_db


class Hit:
    __slots__ = ("start", "end", "peak", "kind", "enabled", "manual")

    def __init__(self, start, end, pk, kind="press", manual=False):
        self.start, self.end, self.peak = start, end, pk   # frames, frames, dB
        self.kind = kind
        self.enabled = True
        self.manual = manual


class Analysis:
    """Per-recording measurements, computed once."""

    def __init__(self, samples, nch, rate):
        self.samples, self.nch, self.rate = samples, nch, rate
        self.win = max(1, int(rate * FRAME_MS / 1000))
        win = self.win
        # Detection runs on a high-passed mono copy: hum, rumble and DC
        # offset live below ~250 Hz, click energy lives well above it.
        mono = samples if nch == 1 else array.array("h", (
            sum(samples[i:i + nch]) // nch for i in range(0, len(samples) - nch + 1, nch)))
        a = math.exp(-2 * math.pi * 250.0 / rate)
        hp = array.array("f", bytes(4 * len(mono)))
        y = 0.0
        x_prev = 0
        for i, x in enumerate(mono):
            y = a * (y + x - x_prev)
            x_prev = x
            hp[i] = y
        db, env = [], []
        mul = operator.mul
        log10 = math.log10
        step = win * nch
        for f in range(len(mono) // win):
            block = hp[f * win:(f + 1) * win]
            energy = sum(map(mul, block, block)) / win
            db.append(10 * log10(energy / 1073741824.0 + 1e-12))   # dBFS (32768^2)
            raw = samples[f * step:(f + 1) * step]
            env.append(max(max(raw), -min(raw)))
        self.db = db
        self.env = env
        ordered = sorted(db) if db else [-120.0]
        self.noise_db = ordered[int(len(ordered) * 0.10)]
        self.top_db = ordered[min(len(ordered) - 1, int(len(ordered) * 0.999))]

    @property
    def frames(self):
        return len(self.db)

    @property
    def seconds(self):
        return len(self.samples) / self.nch / self.rate

    def to_frames(self, frame_index):
        return frame_index * self.win

    # ---------------- detection ----------------
    def detect(self, opts):
        db = self.db
        n = len(db)
        if n < 10:
            return []
        rise_db, margin_db = opts.thresholds()
        floor = self.noise_db + margin_db
        decay = opts.decay_db_per_ms * FRAME_MS
        min_gap = max(3, int(opts.min_gap_ms / FRAME_MS))
        onsets = []
        # `follow` remembers the last click and fades at `decay` dB/ms. A
        # new click must rise `rise_db` above that fading memory as it was
        # a few ms ago (before this attack began - attacks take 1-5 ms):
        # rattles inside a click's own ringing tail don't, a release does.
        lag = 5
        follow = [db[0]] * n
        for i in range(1, n):
            d, prev = db[i], follow[i - 1]
            follow[i] = d if d > prev - decay else prev - decay
        for i in range(lag, n):
            d = db[i]
            base = follow[i - lag]
            if d < floor or d - base < rise_db:
                continue
            j = i
            while j > i - lag + 1 and db[j - 1] > base + 3.0:
                j -= 1                                   # back to the start of the attack
            if onsets and j - onsets[-1] < min_gap:
                continue
            onsets.append(j)

        # A keystroke is often several sounds in quick succession (finger
        # tapping the keycap, then the bottom-out; or a little rattle).
        # Sounds closer than `group_ms` belong to one click.
        group = int(opts.group_ms / FRAME_MS)
        if group > 0 and onsets:
            grouped = [onsets[0]]
            last = onsets[0]
            for on in onsets[1:]:
                if on - last > group or on - grouped[-1] > 3 * group:
                    grouped.append(on)
                last = on
            onsets = grouped

        hits = []
        preroll = int(opts.preroll_ms / FRAME_MS)
        max_len = int(opts.max_len_ms / FRAME_MS)
        tail_level = self.noise_db + opts.tail_db
        for k, on in enumerate(onsets):
            nxt = onsets[k + 1] if k + 1 < len(onsets) else n
            limit = min(n, on + max_len, nxt - preroll - 1)
            seg_end = min(n, on + max(15, group + 15), limit)
            pk_i = max(range(on, max(on + 1, seg_end)), key=db.__getitem__)
            end = limit
            t = pk_i
            while t < limit - 4:
                if (db[t] <= tail_level and db[t + 1] <= tail_level and db[t + 2] <= tail_level
                        and db[t + 3] <= tail_level):
                    end = t + 3
                    break
                t += 1
            end = max(min(end, limit), on + 4)
            hits.append(Hit(max(0, on - preroll) * self.win, end * self.win, db[pk_i]))
        classify(hits, self.rate, opts)
        return hits


def classify(hits, rate, opts):
    """Label hits as press/release (manual labels are kept)."""
    auto = [h for h in hits if not h.manual]
    for h in auto:
        h.kind = "press"
    mode = opts.mode
    if mode == "press" or len(auto) < 2:
        return "press"
    window = opts.pair_window_ms / 1000.0
    if mode == "auto":
        gaps = [(b.start - a.start) / rate for a, b in zip(auto, auto[1:])]
        short = sum(1 for g in gaps if g <= window)
        alternating = sum(1 for a, b in zip(gaps, gaps[1:]) if (a <= window) != (b <= window))
        if not (short >= 0.3 * len(gaps) and alternating >= 0.45 * max(1, len(gaps) - 1)):
            return "press"
    i = 0
    while i < len(auto):
        a = auto[i]
        if i + 1 < len(auto) and (auto[i + 1].start - a.start) / rate <= window:
            auto[i + 1].kind = "release"
            i += 2
        else:
            i += 1
    return "pairs"


def extract(analysis, hits, normalize=True, target=0.6, fade_out_ms=20):
    """Cut enabled hits into clips -> (press, release) lists of array('h').
    Clip ends sit at the noise floor, so a short fade-out is inaudible and
    removes any click from a non-zero cut point."""
    a = analysis
    nch, rate, samples = a.nch, a.rate, a.samples
    press, release = [], []
    for h in sorted(hits, key=lambda h: h.start):
        if not h.enabled:
            continue
        clip = samples[h.start * nch:h.end * nch]
        frames = len(clip) // nch
        if frames < int(0.004 * rate):
            continue
        fo = min(int(fade_out_ms / 1000 * rate), frames // 3)
        clip = fade(clip, nch, int(0.0008 * rate), fo)
        (release if h.kind == "release" else press).append(clip)
    if normalize and (press or release):
        goal = int(32767 * target)
        p_peaks = [peak(c) or 1 for c in press]
        r_peaks = [peak(c) or 1 for c in release]
        ratio = (statistics.mean(r_peaks) / statistics.mean(p_peaks)) if (p_peaks and r_peaks) else 1.0
        press = [_scale(c, goal / pk) for c, pk in zip(press, p_peaks)]
        release = [_scale(c, min(1.0, ratio) * goal / pk) for c, pk in zip(release, r_peaks)]
    return press, release


def _scale(clip, factor):
    return array.array("h", [
        32767 if v > 32767 else (-32768 if v < -32768 else v) for v in (int(s * factor) for s in clip)
    ])


def onset_frame(samples, nch, rate, floor=0.02):
    """Index (in frames) where a clip's sound actually starts - used to
    strip leading silence so a click always lines up with the press."""
    level = int(32767 * floor)
    pk = peak(samples)
    if pk == 0:
        return 0
    level = min(level, pk // 4)       # quiet clips: a quarter of their own peak
    frames = len(samples) // nch
    for i in range(frames):
        base = i * nch
        for c in range(nch):
            if abs(samples[base + c]) > level:
                return i
    return 0

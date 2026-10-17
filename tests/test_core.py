"""Unit tests for Clickwave's pure logic. Run:  py -m unittest discover -s tests -v"""

import array
import json
import math
import os
import random
import tempfile
import unittest
import wave
import zipfile

from tests import _env  # must come before any clickwave import

assert _env.ROOT

import queue
import types

from clickwave import engine as engine_mod
from clickwave import errors, inputs, overlay, settings, slicer, sounds, updates
from clickwave.decode import DecodeError

RATE = 44100


def click_track(onsets_s, seconds=2.0, seed=1):
    """Quiet room noise with a short decaying burst at each onset (mono, 16-bit)."""
    rnd = random.Random(seed)
    n = int(RATE * seconds)
    data = array.array("h", (int(rnd.gauss(0, 20)) for _ in range(n)))
    for t in onsets_s:
        start = int(t * RATE)
        for i in range(int(0.03 * RATE)):
            if start + i < n:
                env = math.exp(-i / (0.004 * RATE))
                data[start + i] += int(rnd.uniform(-1, 1) * 18000 * env)
    return data


class SlicerTests(unittest.TestCase):
    def test_detects_each_click(self):
        onsets = [0.3, 0.8, 1.3]
        analysis = slicer.Analysis(click_track(onsets), 1, RATE)
        hits = analysis.detect(slicer.DetectOptions(mode="press"))
        self.assertEqual(len(hits), 3)
        for hit, t in zip(hits, onsets):
            self.assertAlmostEqual(hit.start / RATE, t, delta=0.02)
        self.assertTrue(all(h.kind == "press" for h in hits))

    def test_silence_has_no_hits(self):
        analysis = slicer.Analysis(click_track([]), 1, RATE)
        self.assertEqual(analysis.detect(slicer.DetectOptions()), [])

    def test_classify_pairs(self):
        # press+release 60 ms apart, keystrokes 0.5 s apart
        starts = [0.0, 0.06, 0.5, 0.56, 1.0, 1.06]
        hits = [slicer.Hit(int(t * RATE), int(t * RATE) + 500, -10.0) for t in starts]
        mode = slicer.classify(hits, RATE, slicer.DetectOptions(mode="auto"))
        self.assertEqual(mode, "pairs")
        self.assertEqual([h.kind for h in hits], ["press", "release"] * 3)

    def test_classify_press_only(self):
        hits = [slicer.Hit(int(t * RATE), int(t * RATE) + 500, -10.0) for t in (0.0, 0.5, 1.0, 1.5)]
        self.assertEqual(slicer.classify(hits, RATE, slicer.DetectOptions(mode="auto")), "press")
        self.assertTrue(all(h.kind == "press" for h in hits))

    def test_extract_splits_press_and_release(self):
        analysis = slicer.Analysis(click_track([0.3, 0.36, 0.9, 0.96]), 1, RATE)
        hits = analysis.detect(slicer.DetectOptions(mode="pairs"))
        press, release = slicer.extract(analysis, hits)
        self.assertEqual((len(press), len(release)), (2, 2))


class SettingsTests(unittest.TestCase):
    def test_clean_profile_clamps_and_drops_junk(self):
        p = settings.clean_profile({"volume": 9999, "soft_ms": 1, "inputs": ["key:a", "key:a", "nocolon", 5],
                                    "apps": ["Game.EXE", "game.exe", ""], "bogus": 1})
        self.assertEqual(p["volume"], 200)
        self.assertEqual(p["soft_ms"], 40)
        self.assertEqual(p["inputs"], ["key:a"])
        self.assertEqual(p["apps"], ["game.exe"])
        self.assertNotIn("bogus", p)

    def test_clean_profile_not_a_dict(self):
        self.assertEqual(settings.clean_profile("junk"), settings.clean_profile({}))

    def test_overrides_are_sanitised(self):
        p = settings.clean_profile({"overrides": {"key:a": {"pan": 500, "volume": -4}, "key:b": "x",
                                                  "key:c": {"pack": None, "volume": None, "pan": 0}}})
        self.assertEqual(p["overrides"], {"key:a": {"pack": None, "volume": 0, "pan": 100}})

    def test_migrate_v2(self):
        new = settings.migrate_v2({"volume": 70, "inputs": ["key:a"], "toggle_hotkey": "key:f9"})
        self.assertEqual(new["version"], 3)
        self.assertEqual(new["profiles"]["Default"]["volume"], 70)
        self.assertEqual(new["hotkeys"]["toggle"], "key:f9")

    def test_settings_roundtrip_and_bom(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w", encoding="utf-8-sig") as f:
                json.dump({"version": 3, "overlay_port": 24800, "volume_bogus": 1}, f)
            s = settings.Settings(path)
            self.assertEqual(s["overlay_port"], 24800)
            s["overlay_token"] = "abc"
            self.assertTrue(s.save())
            self.assertEqual(settings.Settings(path)["overlay_token"], "abc")

    def test_stream_mode_default_and_migration(self):
        with tempfile.TemporaryDirectory() as d:
            fresh = settings.Settings(os.path.join(d, "none.json"))
            self.assertEqual(fresh["stream_mode"], "app")          # new installs: OBS app capture
            path = os.path.join(d, "old.json")
            with open(path, "w") as f:
                json.dump({"version": 3, "onboarded": True, "stream_device": "auto"}, f)
            self.assertEqual(settings.Settings(path)["stream_mode"], "cable")   # existing setups keep the cable
            with open(path, "w") as f:
                json.dump({"version": 3, "stream_mode": "bogus"}, f)
            self.assertEqual(settings.Settings(path)["stream_mode"], "app")

    def test_unreadable_file_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                f.write("{not json")
            s = settings.Settings(path)
            self.assertEqual(s["overlay_port"], settings.GLOBAL_DEFAULTS["overlay_port"])
            self.assertTrue(os.path.exists(path + ".bad"))


class OverlayTests(unittest.TestCase):
    keys = [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"), ("e", "E")]

    def test_merge_style_defaults_and_clamps(self):
        st = overlay.merge_style({"key_size": 9999, "gap": -5, "layout": "grid", "junk": 1, "border": "yes"})
        self.assertEqual(st["key_size"], 200)
        self.assertEqual(st["gap"], 0)
        self.assertEqual(st["layout"], "grid")
        self.assertTrue(st["border"] is True)       # wrong type -> default
        self.assertNotIn("junk", st)
        self.assertEqual(overlay.merge_style(None), overlay.merge_style({}))

    def test_row_and_column(self):
        row = overlay.compute_layout(self.keys[:3], overlay.merge_style({"layout": "row"}))
        self.assertEqual([(k["x"], k["y"]) for k in row], [(0, 0), (1, 0), (2, 0)])
        col = overlay.compute_layout(self.keys[:3], overlay.merge_style({"layout": "column"}))
        self.assertEqual([(k["x"], k["y"]) for k in col], [(0, 0), (0, 1), (0, 2)])

    def test_grid_alignment_of_short_row(self):
        for align, xs in (("left", [0, 1]), ("center", [0.5, 1.5]), ("right", [1, 2])):
            st = overlay.merge_style({"layout": "grid", "columns": 3, "align": align})
            last = overlay.compute_layout(self.keys, st)[3:]
            self.assertEqual([k["x"] for k in last], xs, align)

    def test_custom_is_normalised_but_not_realigned(self):
        st = overlay.merge_style({"layout": "custom", "align": "right",
                                  "positions": {"a": [3, 2], "b": [4, 2], "c": [3, 3]}})
        out = {k["id"]: (k["x"], k["y"]) for k in overlay.compute_layout(self.keys[:3], st)}
        self.assertEqual(out, {"a": (0, 0), "b": (1, 0), "c": (0, 1)})

    def test_custom_drag_without_normalising_keeps_others_put(self):
        st = overlay.merge_style({"layout": "custom", "positions": {"a": [3, 0], "b": [1, 0], "c": [2, 0]}})
        out = {k["id"]: k["x"] for k in overlay.compute_layout(self.keys[:3], st, normalize=False)}
        self.assertEqual(out, {"a": 3, "b": 1, "c": 2})

    def test_unplaced_keys_go_under_placed_ones(self):
        st = overlay.merge_style({"layout": "custom", "positions": {"a": [0, 0]}})
        out = {k["id"]: k["y"] for k in overlay.compute_layout(self.keys[:2], st)}
        self.assertEqual(out, {"a": 0, "b": 1})

    def test_server_requires_token_and_host(self):
        import http.client
        srv = overlay.OverlayServer()
        self.assertTrue(srv.start(24960, "secret-token"))
        try:
            def status(path, host=None):
                c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=3)
                c.putrequest("GET", path, skip_host=True)
                c.putheader("Host", host or f"127.0.0.1:{srv.port}")
                c.endheaders()
                r = c.getresponse()
                r.read()
                return r.status, r.getheader("Access-Control-Allow-Origin")

            self.assertEqual(status("/secret-token/state"), (200, None))
            self.assertEqual(status("/state")[0], 404)
            self.assertEqual(status("/wrong/state")[0], 404)
            self.assertEqual(status("/%C3%A9%E2%82%AC/state")[0], 404)     # non-ASCII token must not crash
            self.assertEqual(status("/secret-token/state", host="evil.example")[0], 404)
            self.assertTrue(srv.url.endswith("/secret-token/"))
        finally:
            srv.stop()


class UpdateAndInputTests(unittest.TestCase):
    def test_is_newer(self):
        self.assertTrue(updates.is_newer("v4.2.1", "4.2"))
        self.assertTrue(updates.is_newer("4.10", "4.9"))
        self.assertFalse(updates.is_newer("4.2.0", "4.2"))
        self.assertFalse(updates.is_newer("4.1.9", "4.2"))
        self.assertFalse(updates.is_newer("", "4.2"))

    def test_combo_roundtrip(self):
        combo = inputs.make_combo({"shift", "ctrl"}, "key:f8")
        self.assertEqual(combo, "ctrl+shift+key:f8")
        self.assertEqual(inputs.split_combo(combo), ({"ctrl", "shift"}, "key:f8"))
        self.assertEqual(inputs.split_combo(None), (set(), None))
        self.assertEqual(inputs.split_combo("key:a"), (set(), "key:a"))

    def test_labels(self):
        self.assertEqual(inputs.input_label("ctrl+shift+key:f8"), "Ctrl + Shift + F8")
        self.assertEqual(inputs.input_label("char:a"), "A")
        self.assertEqual(inputs.input_label(None), "None")


class LegacyAutostartTests(unittest.TestCase):
    KEY = r"Software\ClickwaveUnitTest\Run"

    def tearDown(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "GD Click")
        except OSError:
            pass
        for path in (self.KEY, r"Software\ClickwaveUnitTest"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except OSError:
                pass

    def put(self, command):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.KEY) as k:
            winreg.SetValueEx(k, "GD Click", 0, winreg.REG_SZ, command)

    def test_removes_versioned_and_plain_old_exe(self):
        from clickwave import winutil
        for cmd in (r'"C:\x\backup_v3\GD Click v3.1.exe" --startup', r'"C:\x\GD Click.exe" --startup',
                    r'"C:\py\pythonw.exe" "C:\x\gd_click_gui.py" --startup'):
            self.put(cmd)
            self.assertTrue(winutil.remove_legacy_autostart(self.KEY), cmd)
            self.assertIsNone(winutil.get_autostart("GD Click", self.KEY))

    def test_leaves_unrelated_entry_alone(self):
        from clickwave import winutil
        self.put(r'"C:\Tools\SomethingElse.exe"')
        self.assertFalse(winutil.remove_legacy_autostart(self.KEY))
        self.assertIsNotNone(winutil.get_autostart("GD Click", self.KEY))


class ErrorTests(unittest.TestCase):
    def test_known_message(self):
        title, summary, fix = errors.friendly(OSError("[WinError 112] There is not enough space on the disk"))
        self.assertEqual(title, "Disk is full")
        self.assertTrue(summary and fix)

    def test_bad_zip(self):
        self.assertEqual(errors.friendly(zipfile.BadZipFile("x"))[0], "Not a valid zip")

    def test_unknown_falls_back(self):
        title, summary, _ = errors.friendly(ValueError("weird"), title="Import failed")
        self.assertEqual((title, summary), ("Import failed", "weird"))


class FairPlayTests(unittest.TestCase):
    """Software-injected input must never produce a click, a hit event or a count."""

    def setUp(self):
        audio = types.SimpleNamespace(close_all=lambda: None, tidy=lambda: None)
        self.events = queue.Queue()
        self.eng = engine_mod.ClickEngine(self.events, audio)
        self.eng.watched = frozenset({"key:a", "mouse:left"})
        self.eng.running = True
        self.played = []
        self.eng._play = lambda ident, kind, tier: self.played.append((ident, kind))

    def tearDown(self):
        self.eng.shutdown()

    def kinds(self):
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait()[0])
        return out

    def test_real_press_plays(self):
        self.eng._handle("key:a", True)
        self.eng._handle("key:a", False)
        self.assertEqual(self.played, [("key:a", "press"), ("key:a", "release")])
        self.assertEqual(self.eng.total_presses, 1)

    def test_injected_keyboard_is_ignored(self):
        self.eng._kb_injected = True
        self.eng._handle("key:a", True)
        self.eng._handle("key:a", False)
        self.assertEqual(self.played, [])
        self.assertEqual(self.eng.total_presses, 0)
        self.assertEqual(self.eng.injected_ignored, 1)
        self.assertNotIn("hit", self.kinds())

    def test_injected_mouse_is_ignored_but_keyboard_is_not_affected(self):
        self.eng._mouse_injected = True
        self.eng._handle("mouse:left", True)
        self.assertEqual(self.played, [])
        self.eng._handle("key:a", True)         # the keyboard flag is separate
        self.assertEqual(self.played, [("key:a", "press")])

    def test_filter_reads_windows_injected_flag(self):
        self.eng._kb_filter(0, types.SimpleNamespace(flags=engine_mod.LLKHF_INJECTED))
        self.assertTrue(self.eng._kb_injected)
        self.eng._kb_filter(0, types.SimpleNamespace(flags=0))
        self.assertFalse(self.eng._kb_injected)

    def test_not_listening_is_silent(self):
        self.eng.running = False
        self.eng._handle("key:a", True)
        self.assertEqual(self.played, [])


def write_wav(path, samples, rate=RATE):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())


class MechvibesTests(unittest.TestCase):
    def test_import_single_file_pack(self):
        with tempfile.TemporaryDirectory() as d:
            write_wav(os.path.join(d, "all.wav"), click_track([0.1, 0.4, 0.7], seconds=1.0))
            cfg = {"name": "Unit Test Pack", "key_define_type": "single", "sound": "all.wav",
                   "defines": {"30": [100, 60], "31": [400, 60], "32": [700, 60]}}
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump(cfg, f)
            result = sounds.import_mechvibes(d)
        self.assertEqual(result.kind, "mechvibes")
        name, presses, releases, _ = result.packs[0]
        self.assertEqual(name, "Unit Test Pack")
        self.assertEqual((presses, releases), (3, 0))
        self.assertTrue(os.path.isdir(os.path.join(sounds.PACKS_DIR, name)))

    def test_missing_audio_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "config.json"), "w") as f:
                json.dump({"name": "Broken", "key_define_type": "multi", "defines": {"1": "nope.wav"}}, f)
            with self.assertRaises(DecodeError):
                sounds.import_mechvibes(d)


if __name__ == "__main__":
    unittest.main()

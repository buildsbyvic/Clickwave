"""Pack editor: see, hear, retag and remove the clips inside a pack."""

import os
import threading
import tkinter as tk
from tkinter import filedialog

from .. import sounds, winutil
from ..common import log
from ..decode import AUDIO_EXTENSIONS, DecodeError, decode_any
from .theme import F, I, S, T
from .widgets import Dialog, Dropdown, RoundedButton, ScrollPage, ellipsize, label

TAGS = [
    (("press", "normal"), "Press"),
    (("release", "normal"), "Release"),
    (("press", "soft"), "Soft press"),
    (("release", "soft"), "Soft release"),
    (("press", "hard"), "Hard press"),
    (("release", "hard"), "Hard release"),
    (("press", "micro"), "Micro press"),
    (("release", "micro"), "Micro release"),
]
TAG_LABEL = dict(TAGS)


class PackEditor(Dialog):
    def __init__(self, app, pack_name):
        super().__init__(app.root, f"Edit pack - {pack_name}", width=480)
        self.app = app
        self.pack = sounds.get_pack(pack_name)
        self.changed = False
        self._durations = {}
        b = self.body

        head = tk.Frame(b, bg=T.bg)
        head.pack(fill="x")
        label(head, self.pack.name, font="h2").pack(side="left")
        self.count = label(head, "", font="small", fg=T.muted)
        self.count.pack(side="right")
        hint = ("The built-in pack is read-only - duplicate it to edit."
                if self.pack.builtin else
                "Play clips, change what each one is used for, or remove it (goes to the Recycle Bin).")
        label(b, hint, font="tiny", fg=T.faint, justify="left", wraplength=S(440)).pack(anchor="w", pady=(S(4), S(8)))

        frame = tk.Frame(b, bg=T.border, padx=1, pady=1)
        frame.pack(fill="both", expand=True)
        self.page = ScrollPage(frame)
        self.page.configure(bg=T.card, height=S(330))
        self.page.canvas.configure(bg=T.card, height=S(330))
        self.page.inner.configure(bg=T.card)
        self.page.bar.configure(bg=T.card)
        self.page.pack(fill="both", expand=True)
        self.bind("<MouseWheel>", lambda e: self.page.scroll(e.delta))

        row = tk.Frame(b, bg=T.bg)
        row.pack(fill="x", pady=(S(12), 0))
        if not self.pack.builtin:
            RoundedButton(row, "Add files…", self.add_files, height=32, font=F["small"], icon=I.add).pack(side="left")
        RoundedButton(row, "Open folder", lambda: winutil.open_path(self.pack.path), height=32, font=F["small"],
                      icon=I.folder).pack(side="left", padx=(S(8), 0))
        self.status = label(b, "", font="tiny", fg=T.ok)
        self.status.pack(anchor="w", pady=(S(6), 0))
        self.buttons("Done", self.cancel, cancel_text=None)
        self.refresh()
        self.present()

    # ---------------- list ----------------
    def refresh(self):
        inner = self.page.inner
        for child in inner.winfo_children():
            child.destroy()
        files = self.pack.files()
        total = sum(len(v) for v in files.values())
        self.count.config(text=f"{total} clip{'s' if total != 1 else ''}")
        if not total:
            label(inner, "No clips yet - add some files.", font="small", fg=T.faint).pack(pady=S(20))
        for key, title in TAGS:
            paths = files.get(key)
            if not paths:
                continue
            label(inner, f"{title.upper()}  ·  {len(paths)}", font="tiny", fg=T.faint).pack(
                anchor="w", padx=S(12), pady=(S(10), S(2)))
            for path in paths:
                self._row(inner, path, key)
        self.page.scroll_to(0)

    def _duration(self, path):
        if path not in self._durations:
            try:
                s, n, r = decode_any(path, max_seconds=2)
                self._durations[path] = len(s) / n / r
            except (DecodeError, OSError, EOFError, ValueError):
                self._durations[path] = None
        return self._durations[path]

    def _row(self, parent, path, key):
        row = tk.Frame(parent, bg=T.card)
        row.pack(fill="x", padx=S(8), pady=S(1))
        RoundedButton(row, "", lambda: self.play(path), height=28, icon=I.play).pack(side="left")
        name = os.path.basename(path)
        dur = self._duration(path)
        label(row, ellipsize(F["small"], name, S(170)), font="small").pack(side="left", padx=(S(8), 0))
        label(row, "unreadable" if dur is None else f"{dur:.2f} s", font="tiny",
              fg=T.warn if dur is None else T.faint).pack(side="left", padx=(S(8), 0))
        if self.pack.builtin:
            return
        RoundedButton(row, "", lambda: self.remove(path), height=28, icon=I.close).pack(side="right")
        dd = Dropdown(row, TAGS, key, lambda new, p=path: self.retag(p, new), height=28)
        dd.configure(width=S(128))
        dd.pack(side="right", padx=(0, S(6)))

    # ---------------- actions ----------------
    def play(self, path):
        def work():
            try:
                s, n, r = decode_any(path, max_seconds=sounds.MAX_CLIP_SECONDS)
                self.app.engine.play_samples(s, n, r, volume=max(0.3, self.app.engine.volume))
            except (DecodeError, OSError, EOFError, ValueError) as e:
                log.warning("Preview failed: %s", e)
        threading.Thread(target=work, daemon=True).start()

    def retag(self, path, new_key):
        kind, tier = new_key
        if sounds.classify(path) == (kind, tier):
            return
        try:
            new_path = sounds.retag_clip(path, kind, tier)
            self._durations[new_path] = self._durations.pop(path, None)
        except OSError as e:
            self.status.config(text=f"Couldn't rename: {e}", fg=T.red_hover)
            return
        self._changed(f"Moved to {TAG_LABEL[new_key]}")

    def remove(self, path):
        if winutil.recycle(path):
            self._changed(f"Removed {os.path.basename(path)} (it's in the Recycle Bin)")
        else:
            self.status.config(text="Couldn't remove that file", fg=T.red_hover)

    def add_files(self):
        exts = " ".join("*" + e for e in AUDIO_EXTENSIONS)
        paths = filedialog.askopenfilenames(parent=self, title="Add clips",
                                            filetypes=[("Audio", exts), ("All files", "*.*")])
        if not paths:
            return
        self.status.config(text="Adding…", fg=T.muted)

        def work():
            added, errors = sounds.add_clips(self.pack.name, list(paths))
            msg = f"Added {added} clip{'s' if added != 1 else ''}" + (f", {len(errors)} unreadable" if errors else "")
            self.after(0, lambda: self._changed(msg))
        threading.Thread(target=work, daemon=True).start()

    def _changed(self, message):
        self.changed = True
        self.status.config(text="✓ " + message, fg=T.ok)
        self.refresh()
        self.app.on_pack_edited(self.pack.name)

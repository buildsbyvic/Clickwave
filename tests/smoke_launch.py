"""Launch smoke test: builds the whole app with a hidden window, visits every tab, switches theme, quits.
Nothing is shown or played, and %APPDATA% is a throwaway folder.

Run:  py tests/smoke_launch.py        (exit code 0 = OK)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _env  # noqa: E402

assert _env.ROOT

import tkinter as tk  # noqa: E402

import clickwave.ui.app as m  # noqa: E402
from clickwave.ui.theme import init_fonts, set_scale  # noqa: E402


def main():
    m.winutil.old_version_running = lambda: False
    root = tk.Tk()
    root.withdraw()
    set_scale(1.0)
    init_fonts(root)
    problems = []
    root.report_callback_exception = lambda *exc: problems.append(exc[1])
    app = m.App(root)
    for tab in (0, 1, 2, 0):
        app.show_tab(tab)
        root.update()
    app.on_theme("light")
    root.update()
    app.on_theme("dark")
    root.update()
    app.quit()
    if problems:
        print("SMOKE FAILED:", problems)
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

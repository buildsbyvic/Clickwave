"""App-wide constants, paths and logging."""

import logging
import logging.handlers
import os
import sys
import threading

APP_NAME = "Clickwave"
APP_VERSION = "4.1.1"
APP_TAGLINE = "Click sounds for your inputs"

# When bundled into a standalone .exe with PyInstaller, bundled files
# get extracted to a temp folder at runtime (sys._MEIPASS).
if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
else:
    BUNDLE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED_SOUNDS_DIR = os.path.join(BUNDLE_DIR, "sounds")
ASSETS_DIR = os.path.join(BUNDLE_DIR, "assets")
ICON_PATH = os.path.join(ASSETS_DIR, "clickwave.ico")
LOGO_PATH = os.path.join(ASSETS_DIR, "logo_28.png")

# User data lives in %APPDATA% so it survives rebuilds.
_ROAMING = os.environ.get("APPDATA") or os.path.expanduser("~")
APPDATA_DIR = os.path.join(_ROAMING, APP_NAME)
SETTINGS_PATH = os.path.join(APPDATA_DIR, "settings.json")
PACKS_DIR = os.path.join(APPDATA_DIR, "packs")
LEGACY_SOUNDS_DIR = os.path.join(APPDATA_DIR, "sounds")   # v2 custom sounds
LOG_PATH = os.path.join(APPDATA_DIR, "clickwave.log")
OLD_APPDATA_DIR = os.path.join(_ROAMING, "GD Click")      # before the rename

REPO_URL = "https://github.com/buildsbyvic/Clickwave"
RELEASES_URL = REPO_URL + "/releases"
ISSUES_URL = REPO_URL + "/issues"
API_LATEST_URL = "https://api.github.com/repos/buildsbyvic/Clickwave/releases/latest"

log = logging.getLogger("clickwave")


def migrate_from_gd_click():
    """First launch after the rename: copy settings, packs and overlay
    files from %APPDATA%\\GD Click. The old folder is left untouched."""
    import shutil
    if os.path.exists(SETTINGS_PATH) or not os.path.isdir(OLD_APPDATA_DIR):
        return False
    os.makedirs(APPDATA_DIR, exist_ok=True)
    copied = []
    for name in ("settings.json", "packs", "overlay", "sounds"):
        src, dst = os.path.join(OLD_APPDATA_DIR, name), os.path.join(APPDATA_DIR, name)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
                copied.append(name)
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
                copied.append(name)
        except OSError as e:
            log.warning("Migration: couldn't copy %s: %s", name, e)
    log.info("Migrated from GD Click: %s", ", ".join(copied) or "nothing")
    return bool(copied)


def setup_logging():
    """Log to %APPDATA%\\Clickwave\\clickwave.log (the windowed .exe has no
    console, so this is the only place errors would ever show up)."""
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=256 * 1024, backupCount=1, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    def excepthook(exc_type, exc, tb):
        log.error("Unhandled exception", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        log.error("Unhandled exception in thread %s", args.thread.name if args.thread else "?",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    log.info("%s %s starting (python %s, frozen=%s)", APP_NAME, APP_VERSION,
             sys.version.split()[0], getattr(sys, "frozen", False))

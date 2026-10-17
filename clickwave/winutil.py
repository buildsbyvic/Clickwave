"""Small Windows helpers (ctypes / winreg)."""

import ctypes
import os
import re
import sys
import time
from ctypes import wintypes

from .common import APP_NAME, log

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
OWN_PID = os.getpid()


# ------------------------------------------------------------
# Process / foreground window
# ------------------------------------------------------------

def exe_of_pid(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(520)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


class ForegroundTracker:
    """Cheap 'which exe is focused?' for the input hook (cached per pid)."""

    def __init__(self):
        self._cache = {}
        self._last = (None, 0.0, None)  # hwnd, time, result

    def current(self):
        """(pid, exe_basename_lower)."""
        hwnd = user32.GetForegroundWindow()
        now = time.perf_counter()
        last_hwnd, last_time, last_result = self._last
        if hwnd == last_hwnd and now - last_time < 1.0:
            return last_result
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = self._cache.get(pid.value)
        if name is None:
            path = exe_of_pid(pid.value)
            name = os.path.basename(path).lower() if path else ""
            if len(self._cache) > 256:
                self._cache.clear()
            self._cache[pid.value] = name
        result = (pid.value, name)
        self._last = (hwnd, now, result)
        return result


def running_apps():
    """[(exe_basename, window_title)] for visible top-level windows,
    one entry per exe (for the 'Add app' picker)."""
    found = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == OWN_PID:
            return True
        path = exe_of_pid(pid.value)
        if path:
            exe = os.path.basename(path).lower()
            if exe not in found and exe not in ("explorer.exe", "textinputhost.exe",
                                                "applicationframehost.exe", "systemsettings.exe"):
                found[exe] = title.value
        return True

    user32.EnumWindows(callback, 0)
    return sorted(found.items(), key=lambda kv: kv[1].lower())


# ------------------------------------------------------------
# Launch with Windows (HKCU Run key)
# ------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    script = os.path.abspath(sys.argv[0])
    return f'"{exe}" "{script}" --startup'


def get_autostart(value_name=APP_NAME, key_path=RUN_KEY):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            return winreg.QueryValueEx(key, value_name)[0]
    except OSError:
        return None


def set_autostart(enabled, value_name=APP_NAME, key_path=RUN_KEY):
    import winreg
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            if enabled:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        log.warning("Autostart update failed: %s", e)
        return False


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle(*paths):
    """Send files/folders to the Recycle Bin (recoverable) instead of
    deleting them outright. Returns True on success."""
    paths = [os.path.abspath(p) for p in paths if os.path.exists(p)]
    if not paths:
        return True
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3                                   # FO_DELETE
    op.pFrom = "\0".join(paths) + "\0\0"           # double-NUL terminated list
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400          # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
    ok = shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not op.fAnyOperationsAborted
    if not ok:
        log.warning("Recycle failed for %s", paths)
    return ok


_LEGACY_COMMAND = re.compile(r"GD[ _]?Click[^\/\"]*\.exe|gd_click_gui\.py", re.I)


def remove_legacy_autostart(key_path=RUN_KEY):
    """Drop the pre-rename "GD Click" startup entry (only if it really is
    ours), so Windows doesn't launch the old version alongside Clickwave.
    Matches any exe named like "GD Click.exe" or "GD Click v3.1.exe"."""
    command = get_autostart("GD Click", key_path)
    if command and _LEGACY_COMMAND.search(command):
        set_autostart(False, "GD Click", key_path)
        log.info("Removed old GD Click startup entry")
        return True
    return False


# ------------------------------------------------------------
# Window helpers
# ------------------------------------------------------------

def set_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def style_title_bar(root, dark, bg, fg):
    """Title bar colors to match the theme (Windows 10/11)."""
    try:
        hwnd = ctypes.c_void_p(int(root.wm_frame(), 16))
        dwm = ctypes.windll.dwmapi

        def setattr_(attr, value):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        def colorref(hex_color):
            r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
            return b << 16 | g << 8 | r

        setattr_(20, 1 if dark else 0)          # DWMWA_USE_IMMERSIVE_DARK_MODE
        setattr_(35, colorref(bg))              # DWMWA_CAPTION_COLOR
        setattr_(36, colorref(fg))              # DWMWA_TEXT_COLOR
    except Exception:
        pass


def make_overlay(window):
    """Make a Tk toplevel a click-through, non-activating overlay."""
    try:
        hwnd = int(window.wm_frame(), 16)
        GWL_EXSTYLE = -20
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        style |= 0x00000080 | 0x08000000 | 0x00000020 | 0x00080000 | 0x00000008
        #        TOOLWINDOW   NOACTIVATE   TRANSPARENT  LAYERED      TOPMOST
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


def work_area():
    """(left, top, right, bottom) of the primary monitor minus taskbar."""
    rect = wintypes.RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def virtual_screen():
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

_SHOW_EVENT = "Local\\ClickwaveShowRequest"


def acquire_single_instance():
    """Return a mutex handle, or None if Clickwave is already running -
    in which case the running copy is asked to show itself."""
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, "Local\\ClickwaveSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        event = kernel32.CreateEventW(None, False, False, _SHOW_EVENT)
        if event:
            kernel32.SetEvent(event)
            kernel32.CloseHandle(event)
        return None
    return handle


kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]


def old_version_running():
    """True if the pre-rename GD Click is still running (both hooking the
    keyboard would double every click)."""
    handle = kernel32.OpenMutexW(0x00100000, False, "Local\\GDClickSingleInstance")  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def ask_old_version_to_show():
    event = kernel32.CreateEventW(None, False, False, "Local\\GDClickShowRequest")
    if event:
        kernel32.SetEvent(event)
        kernel32.CloseHandle(event)


def message_box(text, title=APP_NAME, icon=0x30):
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    return user32.MessageBoxW(None, text, title, icon | 0x00010000)  # MB_SETFOREGROUND


class ShowRequests:
    """Lets a second launch ask the running instance to come to front."""

    def __init__(self):
        self._event = kernel32.CreateEventW(None, False, False, _SHOW_EVENT)

    def pending(self):
        return bool(self._event) and kernel32.WaitForSingleObject(self._event, 0) == 0


def open_path(path):
    try:
        os.startfile(path)
    except OSError as e:
        log.warning("Could not open %s: %s", path, e)

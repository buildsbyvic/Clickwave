"""System tray icon (Shell_NotifyIcon over ctypes, no dependencies).

Runs its own Win32 message loop on a background thread. Menu picks and
clicks are delivered through `on_command(name, arg)`, which the app
forwards into its Tk event queue.
"""

import ctypes
import os
import threading
from ctypes import wintypes

from .common import APP_NAME, log

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")
kernel32 = ctypes.WinDLL("kernel32")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY, WM_CLOSE, WM_NULL = 0x0002, 0x0010, 0x0000
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_REFRESH = WM_APP + 2
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x80
MF_STRING, MF_GRAYED, MF_CHECKED, MF_POPUP, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x10, 0x800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x2, 0x80, 0x100


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                              wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

CMD_SHOW, CMD_TOGGLE, CMD_QUIT, CMD_PROFILE_BASE = 1, 2, 3, 100


class TrayIcon:
    def __init__(self, icon_path, on_command):
        self.icon_path = icon_path
        self.on_command = on_command
        self.running = False
        self.profiles = []
        self.active_profile = None
        self.hwnd = None
        self._wndproc = WNDPROC(self._proc)   # keep a reference
        self._ready = threading.Event()
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self._thread = threading.Thread(target=self._run, name="ClickwaveTray", daemon=True)
        self._thread.start()
        self._ready.wait(3)

    # --- public (any thread) ---
    def update(self, running, profiles, active_profile):
        self.running = running
        self.profiles = list(profiles)
        self.active_profile = active_profile
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self._thread.join(2)

    # --- tray thread ---
    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            cls = WNDCLASSW()
            cls.lpfnWndProc = self._wndproc
            cls.hInstance = hinst
            cls.lpszClassName = "ClickwaveTrayWindow"
            user32.RegisterClassW(ctypes.byref(cls))
            self.hwnd = user32.CreateWindowExW(0, cls.lpszClassName, APP_NAME, 0, 0, 0, 0, 0,
                                               None, None, hinst, None)
            size = user32.GetSystemMetrics(49)  # SM_CXSMICON
            self._icon = None
            if os.path.exists(self.icon_path):
                self._icon = user32.LoadImageW(None, self.icon_path, 1, size, size, 0x10)  # LR_LOADFROMFILE
            if not self._icon:
                self._icon = user32.LoadImageW(None, ctypes.c_wchar_p(32512), 1, 0, 0, 0x8000)  # IDI_APPLICATION
            self._notify(NIM_ADD)
        except Exception as e:
            log.warning("Tray icon failed: %s", e)
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._icon
        state = "Listening" if self.running else "Stopped"
        tip = f"{APP_NAME} · {state}"
        if self.active_profile and len(self.profiles) > 1:
            tip += f" · {self.active_profile}"
        nid.szTip = tip[:127]
        return nid

    def _notify(self, action):
        nid = self._nid()
        shell32.Shell_NotifyIconW(action, ctypes.byref(nid))

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, CMD_SHOW, f"Show {APP_NAME}")
        user32.AppendMenuW(menu, MF_STRING, CMD_TOGGLE, "Stop listening" if self.running else "Start listening")
        sub = None
        if len(self.profiles) > 1:
            sub = user32.CreatePopupMenu()
            for i, name in enumerate(self.profiles):
                flags = MF_STRING | (MF_CHECKED if name == self.active_profile else 0)
                user32.AppendMenuW(sub, flags, CMD_PROFILE_BASE + i, name.replace("&", "&&"))
            user32.AppendMenuW(menu, MF_POPUP, sub, "Profile")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, "Quit")
        return menu

    def _show_menu(self):
        menu = self._menu()
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)  # also destroys the submenu
        if cmd == CMD_SHOW:
            self.on_command("show", None)
        elif cmd == CMD_TOGGLE:
            self.on_command("toggle", None)
        elif cmd == CMD_QUIT:
            self.on_command("quit", None)
        elif cmd >= CMD_PROFILE_BASE and cmd - CMD_PROFILE_BASE < len(self.profiles):
            self.on_command("profile", self.profiles[cmd - CMD_PROFILE_BASE])

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.on_command("show", None)
                elif event == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if msg == WM_REFRESH:
                self._notify(NIM_MODIFY)
                return 0
            if msg == self._taskbar_created:   # Explorer restarted
                self._notify(NIM_ADD)
                return 0
            if msg == WM_CLOSE:
                self._notify(NIM_DELETE)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            log.warning("Tray message error: %s", e)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

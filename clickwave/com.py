"""Minimal COM plumbing over ctypes (no comtypes dependency).

Interfaces are called by vtable index; every call site documents which
method the index is so it can be checked against the Windows SDK
headers.
"""

import ctypes
import threading
import uuid
from ctypes import wintypes

ole32 = ctypes.WinDLL("ole32")
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None
ole32.CoCreateInstance.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.c_long
ole32.PropVariantClear.argtypes = [ctypes.c_void_p]
ole32.PropVariantClear.restype = ctypes.c_long

COINIT_MULTITHREADED = 0x0
CLSCTX_ALL = 0x17


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, text):
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g

    def __eq__(self, other):
        return isinstance(other, GUID) and bytes(self) == bytes(other)

    def __hash__(self):
        return hash(bytes(self))


class COMError(OSError):
    def __init__(self, hr, where=""):
        self.hr = hr & 0xFFFFFFFF
        super().__init__(f"{where} failed (HRESULT 0x{self.hr:08X})")


_proto_cache = {}
_proto_lock = threading.Lock()


def vcall(obj, index, argtypes, *args, where="COM call"):
    """Call method `index` of COM object `obj` (a c_void_p or int).
    Returns the HRESULT (non-negative) or raises COMError."""
    key = (index, tuple(argtypes))
    with _proto_lock:
        proto = _proto_cache.get(key)
        if proto is None:
            proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
            _proto_cache[key] = proto
    ptr = obj.value if isinstance(obj, ctypes.c_void_p) else obj
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    hr = proto(vtbl[index])(ptr, *args)
    if hr < 0:
        raise COMError(hr, where)
    return hr


_release_proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


def release(obj):
    """IUnknown::Release (vtable index 2). Safe on NULL."""
    ptr = obj.value if isinstance(obj, ctypes.c_void_p) else obj
    if not ptr:
        return
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    _release_proto(vtbl[2])(ptr)


def co_init():
    """Join the multithreaded apartment on the calling thread.
    Returns True if CoUninitialize should be called later."""
    return ole32.CoInitializeEx(None, COINIT_MULTITHREADED) >= 0


class PROPVARIANT(ctypes.Structure):
    # vt + 3 reserved WORDs, then a union whose largest member (BLOB:
    # ULONG + pointer) is 16 bytes on x64 -> 24 bytes total.
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("r1", ctypes.c_ushort),
        ("r2", ctypes.c_ushort),
        ("r3", ctypes.c_ushort),
        ("p1", ctypes.c_void_p),
        ("p2", ctypes.c_void_p),
    ]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class ComThread:
    """A dedicated thread living in the multithreaded apartment. COM
    objects are created and torn down here; it also keeps the MTA alive
    so other (uninitialized) threads can call MTA objects implicitly."""

    def __init__(self):
        self._jobs = []
        self._cv = threading.Condition()
        self._stop = False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ClickwaveCOM", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self):
        co_init()
        self._ready.set()
        while True:
            with self._cv:
                while not self._jobs and not self._stop:
                    self._cv.wait()
                if self._stop and not self._jobs:
                    break
                fn, done, box = self._jobs.pop(0)
            try:
                box["result"] = fn()
            except BaseException as e:  # handed back to the caller
                box["error"] = e
            done.set()
        ole32.CoUninitialize()

    def call(self, fn, timeout=10):
        if threading.current_thread() is self._thread:
            return fn()
        done = threading.Event()
        box = {}
        with self._cv:
            self._jobs.append((fn, done, box))
            self._cv.notify()
        if not done.wait(timeout):
            raise TimeoutError("COM thread did not respond")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(2)


_com_thread = None
_com_thread_lock = threading.Lock()


def com_thread():
    global _com_thread
    with _com_thread_lock:
        if _com_thread is None:
            _com_thread = ComThread()
        return _com_thread

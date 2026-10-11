"""Legacy playback through winmm waveOut (compatibility fallback).

Higher latency than WASAPI, but works everywhere. Each device gets a
fixed pool of waveOut handles opened once and reused round-robin
(opening one per click crashes some virtual drivers like Voicemeeter).
"""

import ctypes
import threading

_winmm = ctypes.WinDLL("winmm")

WAVE_MAPPER = 0xFFFFFFFF
_WHDR_DONE = 0x1
VOICES_PER_DEVICE = 8
FORMAT = (2, 48000, "s16")


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint),
        ("dwBytesRecorded", ctypes.c_uint), ("dwUser", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
        ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p),
    ]


class _WAVEOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort), ("wPid", ctypes.c_ushort),
        ("vDriverVersion", ctypes.c_uint), ("szPname", ctypes.c_wchar * 32),
        ("dwFormats", ctypes.c_uint), ("wChannels", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort), ("dwSupport", ctypes.c_uint),
    ]


# Explicit argtypes: device IDs and handles are pointer-sized on x64.
_winmm.waveOutGetNumDevs.argtypes = []
_winmm.waveOutGetNumDevs.restype = ctypes.c_uint
_winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(_WAVEOUTCAPS), ctypes.c_uint]
_winmm.waveOutGetDevCapsW.restype = ctypes.c_uint
_winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t,
                               ctypes.POINTER(_WAVEFORMATEX), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint]
_winmm.waveOutOpen.restype = ctypes.c_uint
for _name in ("waveOutPrepareHeader", "waveOutWrite", "waveOutUnprepareHeader"):
    getattr(_winmm, _name).argtypes = [ctypes.c_void_p, ctypes.POINTER(_WAVEHDR), ctypes.c_uint]
    getattr(_winmm, _name).restype = ctypes.c_uint
_winmm.waveOutClose.argtypes = [ctypes.c_void_p]
_winmm.waveOutClose.restype = ctypes.c_uint
_winmm.waveOutReset.argtypes = [ctypes.c_void_p]
_winmm.waveOutReset.restype = ctypes.c_uint


def list_devices():
    """[(index, name)] - names are truncated to 31 chars by winmm."""
    devices = []
    caps = _WAVEOUTCAPS()
    for i in range(_winmm.waveOutGetNumDevs()):
        if _winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            name = caps.szPname
            if len(name) >= 31:
                name += "…"
            devices.append((i, name))
    return devices


class WinmmDevice:
    backend = "legacy"
    latency_ms = None

    def __init__(self, device_index, name):
        self.name = name
        self.fmt = FORMAT
        self.dead = False
        self._lock = threading.Lock()
        self._next = 0
        nch, rate, _ = FORMAT
        fmt = _WAVEFORMATEX()
        fmt.wFormatTag = 1
        fmt.nChannels = nch
        fmt.nSamplesPerSec = rate
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = nch * 2
        fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
        self._handles, self._headers, self._buffers = [], [None] * VOICES_PER_DEVICE, [None] * VOICES_PER_DEVICE
        for _ in range(VOICES_PER_DEVICE):
            h = ctypes.c_void_p()
            ok = _winmm.waveOutOpen(ctypes.byref(h), device_index, ctypes.byref(fmt), 0, 0, 0) == 0
            self._handles.append(h if ok else None)
        self.ok = any(h is not None for h in self._handles)

    def play(self, buf):
        with self._lock:
            for _ in range(VOICES_PER_DEVICE):
                slot = self._next
                self._next = (self._next + 1) % VOICES_PER_DEVICE
                handle = self._handles[slot]
                if handle is not None:
                    break
            else:
                return
            old = self._headers[slot]
            if old is not None:
                if not (old.dwFlags & _WHDR_DONE):
                    _winmm.waveOutReset(handle)
                _winmm.waveOutUnprepareHeader(handle, ctypes.byref(old), ctypes.sizeof(old))
            hdr = _WAVEHDR()
            hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
            hdr.dwBufferLength = ctypes.sizeof(buf)
            _winmm.waveOutPrepareHeader(handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
            _winmm.waveOutWrite(handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
            self._buffers[slot] = buf
            self._headers[slot] = hdr

    def tidy(self):
        pass

    def close(self):
        with self._lock:
            for slot, handle in enumerate(self._handles):
                if handle is None:
                    continue
                _winmm.waveOutReset(handle)
                hdr = self._headers[slot]
                if hdr is not None:
                    _winmm.waveOutUnprepareHeader(handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
                _winmm.waveOutClose(handle)
            self._handles = []
            self.ok = False

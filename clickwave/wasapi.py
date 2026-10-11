"""Low-latency playback through WASAPI shared mode (ctypes COM).

Each output device gets a small pool of independent IAudioClient
streams ("voices"). A click is written straight into an idle voice's
buffer and the Windows audio engine mixes the voices together, so no
per-sample mixing ever happens in Python. Latency is roughly one engine
period (~10 ms) versus 40-100 ms for the legacy waveOut API.

Idle voices are stopped by `tidy()` so the app never holds the audio
device awake (which would also block system sleep).
"""

import ctypes
import threading
import time
from ctypes import wintypes

from .com import (
    CLSCTX_ALL, GUID, PROPERTYKEY, PROPVARIANT, COMError, ole32, release, vcall,
)

_P = ctypes.POINTER
_VP = ctypes.c_void_p

CLSID_MMDeviceEnumerator = GUID.from_str("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID.from_str("A95664D2-9614-4F35-A746-DE8DB63617E6")
IID_IAudioClient = GUID.from_str("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
IID_IAudioRenderClient = GUID.from_str("F294ACFC-3146-4483-A7BF-ADDCA7C260E2")
KSDATAFORMAT_SUBTYPE_PCM = GUID.from_str("00000001-0000-0010-8000-00aa00389b71")
KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = GUID.from_str("00000003-0000-0010-8000-00aa00389b71")
PKEY_Device_FriendlyName = PROPERTYKEY(GUID.from_str("a45c254e-df1c-4efd-8020-67d146a850e0"), 14)

eRender = 0
eConsole = 0
DEVICE_STATE_ACTIVE = 0x1
STGM_READ = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_E_DEVICE_INVALIDATED = 0x88890004
VT_LPWSTR = 31

VOICES_PER_DEVICE = 8
BUFFER_HNS = 6_000_000  # 600 ms of room per voice - longer than any click


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEFORMATEXTENSIBLE(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("Format", WAVEFORMATEX),
        ("wValidBitsPerSample", wintypes.WORD),
        ("dwChannelMask", wintypes.DWORD),
        ("SubFormat", GUID),
    ]


def _enumerator():
    enum = _VP()
    hr = ole32.CoCreateInstance(
        ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
        ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum),
    )
    if hr < 0:
        raise COMError(hr, "CoCreateInstance(MMDeviceEnumerator)")
    return enum


def _device_id(device):
    # IMMDevice::GetId (5)
    pstr = ctypes.c_wchar_p()
    vcall(device, 5, [_P(ctypes.c_wchar_p)], ctypes.byref(pstr))
    try:
        return pstr.value
    finally:
        ole32.CoTaskMemFree(ctypes.cast(pstr, _VP))


def _device_name(device):
    # IMMDevice::OpenPropertyStore (4) -> IPropertyStore::GetValue (5)
    store = _VP()
    vcall(device, 4, [wintypes.DWORD, _P(_VP)], STGM_READ, ctypes.byref(store))
    pv = PROPVARIANT()
    try:
        vcall(store, 5, [_P(PROPERTYKEY), _P(PROPVARIANT)], ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(pv))
        if pv.vt == VT_LPWSTR and pv.p1:
            return ctypes.wstring_at(pv.p1)
        return "Unknown device"
    finally:
        ole32.PropVariantClear(ctypes.byref(pv))
        release(store)


def list_devices():
    """[(endpoint_id, friendly_name)] for active playback devices.
    Must run on an MTA thread."""
    enum = _enumerator()
    coll = _VP()
    out = []
    try:
        # IMMDeviceEnumerator::EnumAudioEndpoints (3)
        vcall(enum, 3, [ctypes.c_int, wintypes.DWORD, _P(_VP)], eRender, DEVICE_STATE_ACTIVE, ctypes.byref(coll))
        count = wintypes.UINT()
        vcall(coll, 3, [_P(wintypes.UINT)], ctypes.byref(count))  # IMMDeviceCollection::GetCount
        for i in range(count.value):
            dev = _VP()
            vcall(coll, 4, [wintypes.UINT, _P(_VP)], i, ctypes.byref(dev))  # ::Item
            try:
                out.append((_device_id(dev), _device_name(dev)))
            finally:
                release(dev)
    finally:
        release(coll)
        release(enum)
    return out


def default_device_id():
    enum = _enumerator()
    dev = _VP()
    try:
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint (4)
        vcall(enum, 4, [ctypes.c_int, ctypes.c_int, _P(_VP)], eRender, eConsole, ctypes.byref(dev))
        return _device_id(dev)
    except COMError:
        return None
    finally:
        release(dev)
        release(enum)


class _Voice:
    __slots__ = ("client", "render", "frames", "running", "started_at")

    def __init__(self, client, render, frames):
        self.client = client
        self.render = render
        self.frames = frames
        self.running = False
        self.started_at = 0.0


class WasapiDevice:
    """A pool of shared-mode streams on one endpoint. Create on the COM
    thread; play() may be called from any thread."""

    backend = "wasapi"

    def __init__(self, endpoint_id, name):
        self.endpoint_id = endpoint_id
        self.name = name
        self.ok = False
        self.dead = False
        self.fmt = None          # (channels, rate, "f32" | "s16" | "s32")
        self.block = 0
        self.latency_ms = None
        self._voices = []
        self._lock = threading.Lock()
        self._open()

    def _open(self):
        enum = _enumerator()
        dev = _VP()
        mix = _P(WAVEFORMATEX)()
        try:
            # IMMDeviceEnumerator::GetDevice (5)
            vcall(enum, 5, [wintypes.LPCWSTR, _P(_VP)], self.endpoint_id, ctypes.byref(dev))
            for _ in range(VOICES_PER_DEVICE):
                client = _VP()
                # IMMDevice::Activate (3)
                vcall(dev, 3, [_P(GUID), wintypes.DWORD, _VP, _P(_VP)],
                      ctypes.byref(IID_IAudioClient), CLSCTX_ALL, None, ctypes.byref(client))
                try:
                    if not mix:
                        # IAudioClient::GetMixFormat (8)
                        vcall(client, 8, [_P(_P(WAVEFORMATEX))], ctypes.byref(mix))
                        self._parse_format(mix)
                    # IAudioClient::Initialize (3)
                    vcall(client, 3, [ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                                      _P(WAVEFORMATEX), _VP],
                          AUDCLNT_SHAREMODE_SHARED, 0, BUFFER_HNS, 0, mix, None, where="IAudioClient::Initialize")
                    frames = wintypes.UINT()
                    vcall(client, 4, [_P(wintypes.UINT)], ctypes.byref(frames))  # GetBufferSize
                    render = _VP()
                    vcall(client, 14, [_P(GUID), _P(_VP)], ctypes.byref(IID_IAudioRenderClient),
                          ctypes.byref(render))  # GetService
                    if self.latency_ms is None:
                        default_period, min_period = ctypes.c_longlong(), ctypes.c_longlong()
                        vcall(client, 9, [_P(ctypes.c_longlong), _P(ctypes.c_longlong)],
                              ctypes.byref(default_period), ctypes.byref(min_period))  # GetDevicePeriod
                        lat = ctypes.c_longlong()
                        vcall(client, 5, [_P(ctypes.c_longlong)], ctypes.byref(lat))  # GetStreamLatency
                        self.latency_ms = (default_period.value + lat.value) / 10_000
                    self._voices.append(_Voice(client, render, frames.value))
                except Exception:
                    release(client)
                    raise
            self.ok = bool(self._voices) and self.fmt is not None
        finally:
            if mix:
                ole32.CoTaskMemFree(ctypes.cast(mix, _VP))
            release(dev)
            release(enum)

    def _parse_format(self, mix):
        f = mix.contents
        kind = None
        if f.wFormatTag == 3:
            kind = "f32" if f.wBitsPerSample == 32 else None
        elif f.wFormatTag == 1:
            kind = {16: "s16", 32: "s32"}.get(f.wBitsPerSample)
        elif f.wFormatTag == 0xFFFE:
            ext = ctypes.cast(mix, _P(WAVEFORMATEXTENSIBLE)).contents
            if ext.SubFormat == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT and f.wBitsPerSample == 32:
                kind = "f32"
            elif ext.SubFormat == KSDATAFORMAT_SUBTYPE_PCM:
                kind = {16: "s16", 32: "s32"}.get(f.wBitsPerSample)
        if kind is None:
            raise COMError(0x80004005, f"unsupported mix format (tag {f.wFormatTag}, {f.wBitsPerSample}-bit)")
        self.fmt = (f.nChannels, f.nSamplesPerSec, kind)
        self.block = f.nBlockAlign

    def play(self, buf):
        """buf: ctypes buffer already in this device's mix format."""
        if not self.ok or self.dead:
            return
        nframes = ctypes.sizeof(buf) // self.block
        with self._lock:
            try:
                voice = self._pick_voice()
                frames = min(nframes, voice.frames)
                data = _VP()
                vcall(voice.render, 3, [wintypes.UINT, _P(_VP)], frames, ctypes.byref(data))  # GetBuffer
                ctypes.memmove(data, buf, frames * self.block)
                vcall(voice.render, 4, [wintypes.UINT, wintypes.DWORD], frames, 0)  # ReleaseBuffer
                if not voice.running:
                    vcall(voice.client, 10, [])  # Start
                    voice.running = True
                voice.started_at = time.perf_counter()
            except COMError as e:
                if e.hr == AUDCLNT_E_DEVICE_INVALIDATED:
                    self.dead = True

    def _padding(self, voice):
        pad = wintypes.UINT()
        vcall(voice.client, 6, [_P(wintypes.UINT)], ctypes.byref(pad))  # GetCurrentPadding
        return pad.value

    def _pick_voice(self):
        oldest = None
        for voice in self._voices:
            if self._padding(voice) == 0:
                return voice
            if oldest is None or voice.started_at < oldest.started_at:
                oldest = voice
        # Everything busy: cut off the oldest click.
        vcall(oldest.client, 11, [])  # Stop
        vcall(oldest.client, 12, [])  # Reset
        oldest.running = False
        return oldest

    def tidy(self):
        """Stop voices that finished playing (frees the device to sleep)."""
        if not self.ok or self.dead:
            return
        with self._lock:
            try:
                now = time.perf_counter()
                for voice in self._voices:
                    if voice.running and now - voice.started_at > 0.5 and self._padding(voice) == 0:
                        vcall(voice.client, 11, [])
                        voice.running = False
            except COMError as e:
                if e.hr == AUDCLNT_E_DEVICE_INVALIDATED:
                    self.dead = True

    def close(self):
        with self._lock:
            for voice in self._voices:
                try:
                    if voice.running:
                        vcall(voice.client, 11, [])
                except COMError:
                    pass
                release(voice.render)
                release(voice.client)
            self._voices = []
            self.ok = False

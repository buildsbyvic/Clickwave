"""Audio file decoding + basic DSP, standard library only.

WAV is read with the `wave` module. Everything else (MP3, OGG, FLAC,
M4A/AAC, WMA, even the audio track of MP4/MKV recordings) goes through
Windows Media Foundation, which ships with Windows.

All decoders return (samples, nchannels, rate) where samples is an
array('h') of interleaved 16-bit PCM.
"""

import array
import ctypes
import os
import threading
import wave
from ctypes import wintypes

from .com import GUID, COMError, co_init, ole32, release, vcall

AUDIO_EXTENSIONS = (
    ".wav", ".mp3", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".wma", ".opus",
    ".mp4", ".mkv", ".mov", ".webm", ".aiff", ".aif",
)


class DecodeError(Exception):
    pass


# ============================================================
# WAV
# ============================================================

def decode_wav(path):
    """PCM WAV (8/16/24/32-bit) -> (array('h'), nchannels, rate)."""
    with wave.open(path, "rb") as wf:
        nch = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width == 2:
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) // 2 * 2])
    elif width == 1:
        samples = array.array("h", ((b - 128) << 8 for b in raw))
    elif width == 3:
        samples = array.array("h", (
            int.from_bytes(raw[i + 1:i + 3], "little", signed=True)
            for i in range(0, len(raw) - 2, 3)
        ))
    elif width == 4:
        wide = array.array("i")
        wide.frombytes(raw[: len(raw) // 4 * 4])
        samples = array.array("h", (s >> 16 for s in wide))
    else:
        raise DecodeError(f"unsupported sample width ({width * 8}-bit)")
    if not samples:
        raise DecodeError("file is empty")
    return samples, nch, rate


def write_wav(path, samples, nch, rate):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())


# ============================================================
# Media Foundation
# ============================================================

_mf_lock = threading.Lock()
_mf_started = False

MF_VERSION = 0x00020070
MF_SOURCE_READER_ALL_STREAMS = 0xFFFFFFFE
MF_SOURCE_READER_FIRST_AUDIO_STREAM = 0xFFFFFFFD
MF_SOURCE_READERF_ENDOFSTREAM = 0x2

MF_MT_MAJOR_TYPE = GUID.from_str("48eba18e-f8c9-4687-bf11-0a74c9f96a8f")
MF_MT_SUBTYPE = GUID.from_str("f7e34c9a-42e8-4714-b74b-cb29d72c35e5")
MFMediaType_Audio = GUID.from_str("73647561-0000-0010-8000-00AA00389B71")
MFAudioFormat_PCM = GUID.from_str("00000001-0000-0010-8000-00AA00389B71")
MF_MT_AUDIO_BITS_PER_SAMPLE = GUID.from_str("f2deb57f-40fa-4764-aa33-ed4f2d1ff669")
MF_MT_AUDIO_NUM_CHANNELS = GUID.from_str("37e48bf5-645e-4c5b-89de-ada9e29b696a")
MF_MT_AUDIO_SAMPLES_PER_SECOND = GUID.from_str("5faeeae7-0290-4c31-9e8a-c534f68d9dba")

_P = ctypes.POINTER
_VP = ctypes.c_void_p


def _mf():
    global _mf_started
    mfplat = ctypes.WinDLL("mfplat")
    mfrw = ctypes.WinDLL("mfreadwrite")
    with _mf_lock:
        if not _mf_started:
            mfplat.MFStartup.argtypes = [wintypes.ULONG, wintypes.DWORD]
            mfplat.MFStartup.restype = ctypes.c_long
            hr = mfplat.MFStartup(MF_VERSION, 0)
            if hr < 0:
                raise COMError(hr, "MFStartup")
            _mf_started = True
    mfplat.MFCreateMediaType.argtypes = [_P(_VP)]
    mfplat.MFCreateMediaType.restype = ctypes.c_long
    mfrw.MFCreateSourceReaderFromURL.argtypes = [wintypes.LPCWSTR, _VP, _P(_VP)]
    mfrw.MFCreateSourceReaderFromURL.restype = ctypes.c_long
    return mfplat, mfrw


def decode_mf(path, max_seconds=None):
    """Decode any format Media Foundation understands to 16-bit PCM."""
    uninit = co_init()
    mfplat, mfrw = _mf()
    reader, mtype, out_type = _VP(), _VP(), _VP()
    chunks = []
    try:
        hr = mfrw.MFCreateSourceReaderFromURL(os.path.abspath(path), None, ctypes.byref(reader))
        if hr < 0:
            raise DecodeError(f"Windows can't open this file (0x{hr & 0xFFFFFFFF:08X})")
        # IMFSourceReader::SetStreamSelection (4): only the first audio stream
        vcall(reader, 4, [wintypes.DWORD, wintypes.BOOL], MF_SOURCE_READER_ALL_STREAMS, False)
        try:
            vcall(reader, 4, [wintypes.DWORD, wintypes.BOOL], MF_SOURCE_READER_FIRST_AUDIO_STREAM, True)
        except COMError:
            raise DecodeError("no audio track found")

        hr = mfplat.MFCreateMediaType(ctypes.byref(mtype))
        if hr < 0:
            raise COMError(hr, "MFCreateMediaType")
        # IMFAttributes::SetGUID (24) / SetUINT32 (21)
        vcall(mtype, 24, [_P(GUID), _P(GUID)], ctypes.byref(MF_MT_MAJOR_TYPE), ctypes.byref(MFMediaType_Audio))
        vcall(mtype, 24, [_P(GUID), _P(GUID)], ctypes.byref(MF_MT_SUBTYPE), ctypes.byref(MFAudioFormat_PCM))
        vcall(mtype, 21, [_P(GUID), ctypes.c_uint32], ctypes.byref(MF_MT_AUDIO_BITS_PER_SAMPLE), 16)
        # IMFSourceReader::SetCurrentMediaType (7)
        try:
            vcall(reader, 7, [wintypes.DWORD, _VP, _VP], MF_SOURCE_READER_FIRST_AUDIO_STREAM, None, mtype)
        except COMError:
            raise DecodeError("Windows has no decoder for this audio format")
        # IMFSourceReader::GetCurrentMediaType (6) -> IMFAttributes::GetUINT32 (7)
        vcall(reader, 6, [wintypes.DWORD, _P(_VP)], MF_SOURCE_READER_FIRST_AUDIO_STREAM, ctypes.byref(out_type))
        nch, rate = ctypes.c_uint32(), ctypes.c_uint32()
        vcall(out_type, 7, [_P(GUID), _P(ctypes.c_uint32)], ctypes.byref(MF_MT_AUDIO_NUM_CHANNELS), ctypes.byref(nch))
        vcall(out_type, 7, [_P(GUID), _P(ctypes.c_uint32)], ctypes.byref(MF_MT_AUDIO_SAMPLES_PER_SECOND), ctypes.byref(rate))
        limit = None if max_seconds is None else int(max_seconds * rate.value * nch.value * 2)
        total = 0

        while True:
            idx, flags, ts, sample = wintypes.DWORD(), wintypes.DWORD(), ctypes.c_longlong(), _VP()
            # IMFSourceReader::ReadSample (9)
            vcall(reader, 9, [wintypes.DWORD, wintypes.DWORD, _P(wintypes.DWORD), _P(wintypes.DWORD),
                              _P(ctypes.c_longlong), _P(_VP)],
                  MF_SOURCE_READER_FIRST_AUDIO_STREAM, 0, ctypes.byref(idx), ctypes.byref(flags),
                  ctypes.byref(ts), ctypes.byref(sample))
            if sample.value:
                buf = _VP()
                try:
                    # IMFSample::ConvertToContiguousBuffer (41)
                    vcall(sample, 41, [_P(_VP)], ctypes.byref(buf))
                    ptr, maxlen, curlen = _VP(), wintypes.DWORD(), wintypes.DWORD()
                    # IMFMediaBuffer::Lock (3) / Unlock (4)
                    vcall(buf, 3, [_P(_VP), _P(wintypes.DWORD), _P(wintypes.DWORD)],
                          ctypes.byref(ptr), ctypes.byref(maxlen), ctypes.byref(curlen))
                    chunks.append(ctypes.string_at(ptr, curlen.value))
                    total += curlen.value
                    vcall(buf, 4, [])
                finally:
                    release(buf)
                    release(sample)
            if flags.value & MF_SOURCE_READERF_ENDOFSTREAM:
                break
            if limit is not None and total >= limit:
                break
    finally:
        release(out_type)
        release(mtype)
        release(reader)
        if uninit:
            ole32.CoUninitialize()

    raw = b"".join(chunks)
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if not samples:
        raise DecodeError("no audio decoded")
    return samples, nch.value, rate.value


def decode_any(path, max_seconds=None):
    """WAV via the stdlib (falling back to Media Foundation for exotic
    WAV encodings like float or ADPCM), everything else via MF."""
    try:
        if os.path.getsize(path) == 0:
            raise DecodeError("file is empty")
    except OSError as e:
        raise DecodeError(f"couldn't open the file ({e.strerror or e})")
    if path.lower().endswith(".wav"):
        try:
            return decode_wav(path)
        except (wave.Error, DecodeError, EOFError):
            pass
    if _is_ogg(path):
        # Windows has no OGG container reader for desktop apps; parse it
        # ourselves and use the Vorbis decoder directly.
        from .ogg import MissingDecoder, OggError, decode_ogg
        try:
            return decode_ogg(path, max_seconds)
        except MissingDecoder:
            raise DecodeError(NEEDS_WEB_MEDIA_EXTENSIONS)
        except OggError as e:
            raise DecodeError(str(e))
        except OSError as e:   # COM failures inside the decoder
            raise DecodeError(f"couldn't decode this OGG file ({e})")
    return decode_mf(path, max_seconds)


NEEDS_WEB_MEDIA_EXTENSIONS = "OGG audio needs Microsoft's free Web Media Extensions"


def _is_ogg(path):
    if path.lower().endswith((".ogg", ".oga")):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"OggS"
    except OSError:
        return False


# ============================================================
# DSP helpers
# ============================================================

def convert_channels(samples, src, dst):
    if src == dst:
        return samples
    frames = len(samples) // src
    if dst == 1:
        return array.array("h", (
            sum(samples[i * src:(i + 1) * src]) // src for i in range(frames)
        ))
    if src == 1:
        out = array.array("h", bytes(frames * 2 * dst))
        for c in range(dst):
            out[c::dst] = samples[:frames]
        return out
    out = array.array("h", bytes(frames * 2 * dst))
    for c in range(dst):
        out[c::dst] = samples[min(c, src - 1)::src][:frames]
    return out


def resample(samples, nch, step):
    """Linear-interpolation resample. step > 1 = shorter / higher pitch."""
    if abs(step - 1.0) < 1e-9:
        return samples
    frames = len(samples) // nch
    out_frames = max(1, int(frames / step))
    out = array.array("h", bytes(2 * out_frames * nch))
    last = frames - 1
    for c in range(nch):
        ch = samples[c::nch]
        res = []
        append = res.append
        for i in range(out_frames):
            p = i * step
            j = int(p)
            if j >= last:
                append(ch[last])
            else:
                a = ch[j]
                append(int(a + (ch[j + 1] - a) * (p - j)))
        out[c::nch] = array.array("h", res)
    return out


def to_format(samples, nch, rate, dst_nch, dst_rate):
    """Convert channel count and sample rate in one call."""
    if nch != dst_nch:
        samples = convert_channels(samples, nch, min(dst_nch, 2) if dst_nch > 2 else dst_nch)
        if dst_nch > 2:
            samples = expand_channels(samples, 2, dst_nch)
    if rate != dst_rate:
        samples = resample(samples, dst_nch, rate / dst_rate)
    return samples


def expand_channels(samples, src, dst):
    """Put src channels in the first slots of a wider layout, rest silent
    (e.g. stereo clicks into a 7.1 Voicemeeter input)."""
    frames = len(samples) // src
    out = array.array("h", bytes(frames * 2 * dst))
    for c in range(src):
        out[c::dst] = samples[c::src][:frames]
    return out


def peak(samples):
    if not samples:
        return 0
    return max(max(samples), -min(samples))


def fade(samples, nch, fade_in_frames, fade_out_frames):
    """Linear fade in/out (in place on a copy)."""
    out = array.array("h", samples)
    frames = len(out) // nch
    fi = min(fade_in_frames, frames)
    fo = min(fade_out_frames, frames)
    for i in range(fi):
        g = i / fi
        for c in range(nch):
            out[i * nch + c] = int(out[i * nch + c] * g)
    for i in range(fo):
        g = i / fo
        idx = (frames - 1 - i) * nch
        for c in range(nch):
            out[idx + c] = int(out[idx + c] * g)
    return out


def gain(samples, factor):
    if factor == 1.0:
        return array.array("h", samples)
    return array.array("h", [
        32767 if v > 32767 else (-32768 if v < -32768 else v)
        for v in (int(s * factor) for s in samples)
    ])

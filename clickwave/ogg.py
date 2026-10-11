"""OGG Vorbis decoding for desktop apps.

Windows' Web Media Extensions ships a Vorbis *decoder* that desktop apps
can use, but no OGG *container* reader - so Media Foundation refuses
.ogg files outright. Here the container is parsed in Python (it's a
simple page format) and the Vorbis packets are fed straight into the
decoder MFT.
"""

import array
import ctypes
import struct
from ctypes import wintypes

from .com import GUID, COMError, co_init, ole32, release, vcall

_P = ctypes.POINTER
_VP = ctypes.c_void_p

MFT_CATEGORY_AUDIO_DECODER = GUID.from_str("9ea73fb4-ef7a-4559-8d5d-719d8f0426c7")
MFMediaType_Audio = GUID.from_str("73647561-0000-0010-8000-00AA00389B71")
MFAudioFormat_Vorbis = GUID.from_str("8D2FD10B-5841-4a6b-8905-588FEC1ADED9")
MFAudioFormat_PCM = GUID.from_str("00000001-0000-0010-8000-00AA00389B71")
MFAudioFormat_Float = GUID.from_str("00000003-0000-0010-8000-00AA00389B71")
MF_MT_MAJOR_TYPE = GUID.from_str("48eba18e-f8c9-4687-bf11-0a74c9f96a8f")
MF_MT_SUBTYPE = GUID.from_str("f7e34c9a-42e8-4714-b74b-cb29d72c35e5")
MF_MT_AUDIO_NUM_CHANNELS = GUID.from_str("37e48bf5-645e-4c5b-89de-ada9e29b696a")
MF_MT_AUDIO_SAMPLES_PER_SECOND = GUID.from_str("5faeeae7-0290-4c31-9e8a-c534f68d9dba")
MF_MT_AUDIO_BITS_PER_SAMPLE = GUID.from_str("f2deb57f-40fa-4764-aa33-ed4f2d1ff669")
MF_MT_USER_DATA = GUID.from_str("b6bc765f-4c3b-40a4-bd51-2535b66fe09d")
IID_IMFTransform = GUID.from_str("bf94c121-5b05-4e6f-8000-ba598961414d")

MF_E_TRANSFORM_NEED_MORE_INPUT = 0xC00D6D72
MF_E_TRANSFORM_STREAM_CHANGE = 0xC00D6D61
MFT_OUTPUT_STREAM_PROVIDES_SAMPLES = 0x100
MFT_MESSAGE_COMMAND_DRAIN = 0x1
MFT_MESSAGE_NOTIFY_BEGIN_STREAMING = 0x10000000
MFT_MESSAGE_NOTIFY_START_OF_STREAM = 0x10000003
MFT_MESSAGE_NOTIFY_END_OF_STREAM = 0x10000002
MFT_ENUM_FLAG_ALL = 0x3F
MFT_ENUM_FLAG_UNTRUSTED_STOREMFT = 0x400


class OggError(Exception):
    pass


class MissingDecoder(OggError):
    """Vorbis needs Microsoft's Web Media Extensions on this PC."""


# ============================================================
# Container
# ============================================================

def read_packets(data):
    """Yield (serial, packet bytes) from an OGG bitstream."""
    pos = 0
    pending = {}
    n = len(data)
    while pos + 27 <= n:
        if data[pos:pos + 4] != b"OggS":
            nxt = data.find(b"OggS", pos + 1)   # resync after garbage
            if nxt < 0:
                break
            pos = nxt
            continue
        serial = struct.unpack_from("<I", data, pos + 14)[0]
        nsegs = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nsegs]
        body = pos + 27 + nsegs
        buf = pending.get(serial, b"")
        for size in lacing:
            buf += data[body:body + size]
            body += size
            if size < 255:
                yield serial, buf
                buf = b""
        pending[serial] = buf
        pos = body


def vorbis_stream(data):
    """(channels, rate, [3 header packets], [audio packets]) for the
    first Vorbis stream in the file."""
    serial = None
    headers, audio = [], []
    for s, packet in read_packets(data):
        if serial is None:
            if packet[:7] == b"\x01vorbis":
                serial = s
            elif packet[:8] == b"OpusHead":
                raise OggError("this is Opus audio inside OGG, which isn't supported yet")
            else:
                continue
        if s != serial:
            continue
        if len(headers) < 3:
            headers.append(packet)
        elif packet:
            audio.append(packet)
    if len(headers) < 3:
        raise OggError("no Vorbis audio found in this OGG file")
    ident = headers[0]
    channels = ident[11]
    rate = struct.unpack_from("<I", ident, 12)[0]
    return channels, rate, headers, audio


def _xiph_lace(headers):
    """Codec private data in Xiph lacing (what FFmpeg-based decoders expect)."""
    out = bytearray([len(headers) - 1])
    for h in headers[:-1]:
        size = len(h)
        while size >= 255:
            out.append(255)
            size -= 255
        out.append(size)
    for h in headers:
        out += h
    return bytes(out)


# ============================================================
# Media Foundation plumbing
# ============================================================

class MFT_REGISTER_TYPE_INFO(ctypes.Structure):
    _fields_ = [("guidMajorType", GUID), ("guidSubtype", GUID)]


class MFT_OUTPUT_STREAM_INFO(ctypes.Structure):
    _fields_ = [("dwFlags", wintypes.DWORD), ("cbSize", wintypes.DWORD), ("cbAlignment", wintypes.DWORD)]


class MFT_OUTPUT_DATA_BUFFER(ctypes.Structure):
    _fields_ = [("dwStreamID", wintypes.DWORD), ("pSample", _VP), ("dwStatus", wintypes.DWORD),
                ("pEvents", _VP)]


def _mfplat():
    from .decode import _mf
    mfplat, _ = _mf()
    mfplat.MFTEnumEx.argtypes = [GUID, ctypes.c_uint32, _VP, _VP, _P(_P(_VP)), _P(ctypes.c_uint32)]
    mfplat.MFTEnumEx.restype = ctypes.c_long
    mfplat.MFCreateSample.argtypes = [_P(_VP)]
    mfplat.MFCreateSample.restype = ctypes.c_long
    mfplat.MFCreateMemoryBuffer.argtypes = [wintypes.DWORD, _P(_VP)]
    mfplat.MFCreateMemoryBuffer.restype = ctypes.c_long
    mfplat.MFCreateMediaType.argtypes = [_P(_VP)]
    mfplat.MFCreateMediaType.restype = ctypes.c_long
    return mfplat


def _find_decoder(mfplat):
    info = MFT_REGISTER_TYPE_INFO(MFMediaType_Audio, MFAudioFormat_Vorbis)
    arr = _P(_VP)()
    count = ctypes.c_uint32()
    hr = mfplat.MFTEnumEx(MFT_CATEGORY_AUDIO_DECODER, MFT_ENUM_FLAG_ALL | MFT_ENUM_FLAG_UNTRUSTED_STOREMFT,
                          ctypes.cast(ctypes.byref(info), _VP), None, ctypes.byref(arr), ctypes.byref(count))
    if hr < 0 or count.value == 0:
        raise MissingDecoder("no Vorbis decoder installed")
    transform = _VP()
    try:
        # IMFActivate::ActivateObject (33)
        vcall(arr[0], 33, [_P(GUID), _P(_VP)], ctypes.byref(IID_IMFTransform), ctypes.byref(transform))
    finally:
        for i in range(count.value):
            release(arr[i])
        ole32.CoTaskMemFree(ctypes.cast(arr, _VP))
    return transform


def _media_type(mfplat):
    mt = _VP()
    hr = mfplat.MFCreateMediaType(ctypes.byref(mt))
    if hr < 0:
        raise COMError(hr, "MFCreateMediaType")
    return mt


def _set_guid(obj, key, value):
    vcall(obj, 24, [_P(GUID), _P(GUID)], ctypes.byref(key), ctypes.byref(value))      # SetGUID


def _set_u32(obj, key, value):
    vcall(obj, 21, [_P(GUID), ctypes.c_uint32], ctypes.byref(key), value)              # SetUINT32


def _get_u32(obj, key):
    v = ctypes.c_uint32()
    vcall(obj, 7, [_P(GUID), _P(ctypes.c_uint32)], ctypes.byref(key), ctypes.byref(v))  # GetUINT32
    return v.value


def _get_guid(obj, key):
    g = GUID()
    vcall(obj, 10, [_P(GUID), _P(GUID)], ctypes.byref(key), ctypes.byref(g))           # GetGUID
    return g


def _sample_from(mfplat, payload, time_100ns):
    buf, sample = _VP(), _VP()
    mfplat.MFCreateMemoryBuffer(max(1, len(payload)), ctypes.byref(buf))
    ptr = _VP()
    vcall(buf, 3, [_P(_VP), _VP, _VP], ctypes.byref(ptr), None, None)                    # Lock
    ctypes.memmove(ptr, payload, len(payload))
    vcall(buf, 4, [])                                                                     # Unlock
    vcall(buf, 6, [wintypes.DWORD], len(payload))                                         # SetCurrentLength
    mfplat.MFCreateSample(ctypes.byref(sample))
    vcall(sample, 42, [_VP], buf)                                                         # AddBuffer
    vcall(sample, 36, [ctypes.c_longlong], time_100ns)                                    # SetSampleTime
    vcall(sample, 38, [ctypes.c_longlong], 100_000)                                       # SetSampleDuration
    release(buf)
    return sample


def _read_sample(sample):
    buf = _VP()
    vcall(sample, 41, [_P(_VP)], ctypes.byref(buf))                                       # ConvertToContiguousBuffer
    try:
        ptr, maxlen, cur = _VP(), wintypes.DWORD(), wintypes.DWORD()
        vcall(buf, 3, [_P(_VP), _P(wintypes.DWORD), _P(wintypes.DWORD)],
              ctypes.byref(ptr), ctypes.byref(maxlen), ctypes.byref(cur))
        data = ctypes.string_at(ptr, cur.value)
        vcall(buf, 4, [])
        return data
    finally:
        release(buf)


def decode_ogg(path, max_seconds=None):
    """OGG Vorbis -> (array('h'), channels, rate). Raises MissingDecoder
    if this PC has no Vorbis decoder (Web Media Extensions)."""
    with open(path, "rb") as f:
        data = f.read()
    channels, rate, headers, packets = vorbis_stream(data)
    uninit = co_init()
    mfplat = _mfplat()
    mft = _find_decoder(mfplat)
    in_type = out_type = None
    chunks = []
    total = 0
    limit = None if max_seconds is None else int(max_seconds * rate) * channels
    out_is_float = False
    try:
        # --- input type: Vorbis with the 3 header packets as codec data ---
        in_type = _media_type(mfplat)
        _set_guid(in_type, MF_MT_MAJOR_TYPE, MFMediaType_Audio)
        _set_guid(in_type, MF_MT_SUBTYPE, MFAudioFormat_Vorbis)
        _set_u32(in_type, MF_MT_AUDIO_NUM_CHANNELS, channels)
        _set_u32(in_type, MF_MT_AUDIO_SAMPLES_PER_SECOND, rate)
        blob = _xiph_lace(headers)
        vcall(in_type, 26, [_P(GUID), ctypes.c_char_p, ctypes.c_uint32],                 # SetBlob
              ctypes.byref(MF_MT_USER_DATA), blob, len(blob))
        vcall(mft, 15, [wintypes.DWORD, _VP, wintypes.DWORD], 0, in_type, 0, where="SetInputType")

        # --- output type: prefer 16-bit PCM, else float ---
        chosen = None
        for i in range(16):
            t = _VP()
            try:
                vcall(mft, 14, [wintypes.DWORD, wintypes.DWORD, _P(_VP)], 0, i, ctypes.byref(t))  # GetOutputAvailableType
            except COMError:
                break
            sub = _get_guid(t, MF_MT_SUBTYPE)
            if sub == MFAudioFormat_PCM and chosen is None:
                chosen, out_is_float = t, False
                continue
            if sub == MFAudioFormat_Float and chosen is None:
                chosen, out_is_float = t, True
                continue
            release(t)
        if chosen is None:
            raise OggError("the Vorbis decoder offered no usable output format")
        out_type = chosen
        vcall(mft, 16, [wintypes.DWORD, _VP, wintypes.DWORD], 0, out_type, 0, where="SetOutputType")
        out_channels = _get_u32(out_type, MF_MT_AUDIO_NUM_CHANNELS)
        out_rate = _get_u32(out_type, MF_MT_AUDIO_SAMPLES_PER_SECOND)

        info = MFT_OUTPUT_STREAM_INFO()
        vcall(mft, 7, [wintypes.DWORD, _P(MFT_OUTPUT_STREAM_INFO)], 0, ctypes.byref(info))   # GetOutputStreamInfo
        provides = bool(info.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES)
        out_size = max(info.cbSize, 1 << 16)

        vcall(mft, 23, [ctypes.c_uint, ctypes.c_size_t], MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0)
        vcall(mft, 23, [ctypes.c_uint, ctypes.c_size_t], MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0)

        def drain_outputs():
            nonlocal total
            while True:
                odb = MFT_OUTPUT_DATA_BUFFER()
                odb.dwStreamID = 0
                own = None
                if not provides:
                    own = _sample_from(mfplat, b"", 0)
                    # replace the 1-byte buffer with a big empty one
                    vcall(own, 44, [])                                                    # RemoveAllBuffers
                    buf = _VP()
                    mfplat.MFCreateMemoryBuffer(out_size, ctypes.byref(buf))
                    vcall(own, 42, [_VP], buf)
                    release(buf)
                    odb.pSample = own
                status = wintypes.DWORD()
                proto = ctypes.WINFUNCTYPE(ctypes.c_long, _VP, wintypes.DWORD, wintypes.DWORD,
                                           _P(MFT_OUTPUT_DATA_BUFFER), _P(wintypes.DWORD))
                vtbl = ctypes.cast(mft.value, _P(_P(_VP)))[0]
                hr = proto(vtbl[25])(mft.value, 0, 1, ctypes.byref(odb), ctypes.byref(status))  # ProcessOutput
                if odb.pEvents:
                    release(odb.pEvents)
                if hr & 0xFFFFFFFF == MF_E_TRANSFORM_NEED_MORE_INPUT:
                    release(own)
                    return
                if hr & 0xFFFFFFFF == MF_E_TRANSFORM_STREAM_CHANGE:
                    release(own)
                    raise OggError("decoder changed format mid-stream")
                if hr < 0:
                    release(own)
                    raise COMError(hr, "ProcessOutput")
                if odb.pSample:
                    chunks.append(_read_sample(odb.pSample))
                    total += len(chunks[-1])
                    if provides:
                        release(odb.pSample)
                release(own)

        t = 0
        for packet in packets:
            sample = _sample_from(mfplat, packet, t * 100_000)
            t += 1
            try:
                vcall(mft, 24, [wintypes.DWORD, _VP, wintypes.DWORD], 0, sample, 0, where="ProcessInput")
            finally:
                release(sample)
            drain_outputs()
            if limit is not None and total >= limit * (4 if out_is_float else 2):
                break
        vcall(mft, 23, [ctypes.c_uint, ctypes.c_size_t], MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0)
        vcall(mft, 23, [ctypes.c_uint, ctypes.c_size_t], MFT_MESSAGE_COMMAND_DRAIN, 0)
        drain_outputs()
    finally:
        release(out_type)
        release(in_type)
        release(mft)
        if uninit:
            ole32.CoUninitialize()

    raw = b"".join(chunks)
    if out_is_float:
        fl = array.array("f")
        fl.frombytes(raw[: len(raw) // 4 * 4])
        samples = array.array("h", [
            32767 if v >= 1.0 else (-32768 if v <= -1.0 else int(v * 32767)) for v in fl
        ])
    else:
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) // 2 * 2])
    if not samples:
        raise OggError("the Vorbis decoder produced no audio")
    # The last page's granule position is the true length; decoders emit a
    # little encoder padding past it.
    end = data.rfind(b"OggS")
    if end >= 0 and end + 14 <= len(data):
        granule = struct.unpack_from("<q", data, end + 6)[0]
        if 0 < granule * out_channels < len(samples):
            samples = samples[:granule * out_channels]
    if limit is not None:
        samples = samples[:limit]
    return samples, out_channels or channels, out_rate or rate

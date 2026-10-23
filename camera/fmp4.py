"""Fragmented MP4 helpers for the network camera stream (CAM2 Phase 1b) - stdlib only, pure functions.

ffmpeg remuxes the camera's H.264 (`-c copy`) into fragmented MP4 on a pipe:
one init segment (ftyp + moov) followed by moof+mdat fragments. This module
splits that byte stream into complete boxes/fragments (the pipe delivers
arbitrary chunks), reads the codec, resolution and frame rate out of the
init segment / fragment headers (so no second RTSP session and no ffprobe run
is needed just to learn what the stream is), and tells whether a fragment
starts with a sync sample (a keyframe), which decides where a newly joined
viewer may start.
"""
from __future__ import annotations

from dataclasses import dataclass

# A box larger than this is treated as corrupt (a 4K fragment is well under 1 MB).
MAX_BOX_BYTES = 16 * 1024 * 1024

H264_FAMILY = ("avc1", "avc3")
H265_FAMILY = ("hvc1", "hev1")


@dataclass
class Fragment:
    data: bytes          # moof + mdat, exactly as received
    sync: bool           # first sample is a sync sample (keyframe)
    samples: int         # number of samples in the fragment
    duration_ticks: int  # total sample duration in the track timescale


class Fmp4Error(ValueError):
    pass


class Fmp4Splitter:
    """Feed arbitrary byte chunks; get back the init segment once, then fragments.

    ``feed()`` returns a list of ("init", bytes) / ("fragment", Fragment) items
    in order. A corrupt box header raises Fmp4Error (the caller restarts the
    upstream); nothing here buffers more than MAX_BOX_BYTES.
    """

    def __init__(self):
        self._buf = bytearray()
        self._init = bytearray()
        self._have_init = False
        self._pending = bytearray()
        self._pending_sync = True
        self._pending_samples = 0
        self._pending_ticks = 0

    def feed(self, chunk: bytes) -> list:
        self._buf.extend(chunk)
        out = []
        while len(self._buf) >= 8:
            size = int.from_bytes(self._buf[0:4], "big")
            kind = bytes(self._buf[4:8])
            header = 8
            if size == 1:
                if len(self._buf) < 16:
                    break
                size = int.from_bytes(self._buf[8:16], "big")
                header = 16
            if size < header or size > MAX_BOX_BYTES:
                raise Fmp4Error("corrupt box header")
            if len(self._buf) < size:
                break
            box = bytes(self._buf[:size])
            del self._buf[:size]
            if not self._have_init:
                self._init.extend(box)
                if kind == b"moov":
                    self._have_init = True
                    out.append(("init", bytes(self._init)))
                continue
            if kind == b"moof":
                info = parse_moof(box)
                self._pending_sync = info["sync"]
                self._pending_samples = info["samples"]
                self._pending_ticks = info["duration_ticks"]
            self._pending.extend(box)
            if kind == b"mdat":
                out.append(("fragment", Fragment(bytes(self._pending), self._pending_sync,
                                                 self._pending_samples, self._pending_ticks)))
                self._pending = bytearray()
        return out


def _find(data: bytes, path: tuple, start: int = 0, end: int | None = None):
    """Payload range (start, end) of the first box reached via the type path, or None."""
    end = len(data) if end is None else end
    first, rest = path[0], path[1:]
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = bytes(data[pos + 4:pos + 8])
        header = 8
        if size == 1:
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        if size < header or pos + size > end:
            return None
        if kind == first:
            if not rest:
                return pos + header, pos + size
            found = _find(data, rest, pos + header, pos + size)
            if found:
                return found
        pos += size
    return None


def parse_init(init: bytes) -> dict:
    """{"entry": "avc1", "codec": "avc1.640033", "width": 640, "height": 360, "timescale": 90000}.

    ``codec`` is the MSE codec string for H.264 and "" for anything else. Raises Fmp4Error
    when the init segment has no video sample entry.
    """
    stsd = _find(init, (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stsd"))
    if not stsd:
        raise Fmp4Error("no sample description")
    start, end = stsd
    entry_pos = start + 8  # version/flags + entry_count
    if entry_pos + 8 > end:
        raise Fmp4Error("empty sample description")
    entry_size = int.from_bytes(init[entry_pos:entry_pos + 4], "big")
    entry = init[entry_pos + 4:entry_pos + 8].decode("ascii", "replace")
    payload = entry_pos + 8
    if entry_pos + entry_size > end or payload + 28 > end:
        raise Fmp4Error("truncated sample entry")
    width = int.from_bytes(init[payload + 24:payload + 26], "big")
    height = int.from_bytes(init[payload + 26:payload + 28], "big")
    codec = ""
    if entry in H264_FAMILY:
        avcc = _find(init, (b"avcC",), payload + 78, entry_pos + entry_size)
        if avcc and avcc[1] - avcc[0] >= 4:
            profile, compat, level = init[avcc[0] + 1], init[avcc[0] + 2], init[avcc[0] + 3]
            codec = "%s.%02x%02x%02x" % (entry, profile, compat, level)
    mdhd = _find(init, (b"moov", b"trak", b"mdia", b"mdhd"))
    timescale = 90000
    if mdhd:
        version = init[mdhd[0]]
        offset = mdhd[0] + (20 if version == 1 else 12)
        value = int.from_bytes(init[offset:offset + 4], "big")
        timescale = value or timescale
    return {"entry": entry, "codec": codec, "width": width, "height": height, "timescale": timescale}


def is_h264(info: dict) -> bool:
    return info.get("entry") in H264_FAMILY and bool(info.get("codec"))


def is_h265(info: dict) -> bool:
    return info.get("entry") in H265_FAMILY


def parse_moof(moof: bytes) -> dict:
    """{"sync": bool, "samples": int, "duration_ticks": int} from the first traf's tfhd/trun."""
    traf = _find(moof, (b"moof", b"traf"))
    result = {"sync": True, "samples": 0, "duration_ticks": 0}
    if not traf:
        return result
    tfhd = _find(moof, (b"tfhd",), traf[0], traf[1])
    default_duration = default_flags = None
    if tfhd:
        flags = int.from_bytes(moof[tfhd[0] + 1:tfhd[0] + 4], "big")
        pos = tfhd[0] + 8  # version/flags + track id
        if flags & 0x1:
            pos += 8
        if flags & 0x2:
            pos += 4
        if flags & 0x8:
            default_duration = int.from_bytes(moof[pos:pos + 4], "big")
            pos += 4
        if flags & 0x10:
            pos += 4
        if flags & 0x20:
            default_flags = int.from_bytes(moof[pos:pos + 4], "big")
    trun = _find(moof, (b"trun",), traf[0], traf[1])
    if not trun:
        return result
    flags = int.from_bytes(moof[trun[0] + 1:trun[0] + 4], "big")
    count = int.from_bytes(moof[trun[0] + 4:trun[0] + 8], "big")
    pos = trun[0] + 8
    if flags & 0x1:
        pos += 4
    first_flags = None
    if flags & 0x4:
        first_flags = int.from_bytes(moof[pos:pos + 4], "big")
        pos += 4
    per_sample = 0
    for bit in (0x100, 0x200, 0x400, 0x800):
        if flags & bit:
            per_sample += 4
    total = 0
    first_sample_flags = None
    for index in range(count):
        base = pos + index * per_sample
        if base + per_sample > trun[1]:
            break
        field = base
        duration = default_duration or 0
        sample_flags = None
        if flags & 0x100:
            duration = int.from_bytes(moof[field:field + 4], "big")
            field += 4
        if flags & 0x200:
            field += 4
        if flags & 0x400:
            sample_flags = int.from_bytes(moof[field:field + 4], "big")
        total += duration
        if index == 0:
            first_sample_flags = sample_flags
    chosen = first_flags if first_flags is not None else (first_sample_flags if first_sample_flags is not None else default_flags)
    # sample_is_non_sync_sample is bit 16 of the sample flags; unknown flags -> assume sync
    result["sync"] = True if chosen is None else (chosen & 0x10000) == 0
    result["samples"] = count
    result["duration_ticks"] = total
    return result

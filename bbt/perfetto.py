"""Frame times on Android from Perfetto's SurfaceFlinger frame timeline.

The device's own `perfetto` records `android.surfaceflinger.frametimeline`
(Android 12+, no root). Every buffer an app queues becomes a "surface frame"
with its layer name, owning pid, present type (on time, late, dropped, ...)
and a start and end timestamp; the end is when it reached the screen. The
trace is decoded here with a minimal protobuf reader (standard library only),
so nothing has to be installed on this machine.

Timestamps are CLOCK_BOOTTIME in ns: the same clock as /proc/uptime on the
device, which the sampler uses, so frames and samples line up directly.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

# FrameTimelineEvent.PresentType
PRESENT = {0: "unspecified", 1: "on_time", 2: "late", 3: "early", 4: "dropped", 5: "unknown"}


def config(duration_s: float, buffer_mib: int = 32) -> str:
    """perfetto --txt config: frame timeline only (about 30 KiB/s at 60 fps)."""
    return (f"buffers {{ size_kb: {buffer_mib * 1024} fill_policy: DISCARD }}\n"
            'data_sources { config { name: "android.surfaceflinger.frametimeline" } }\n'
            f"duration_ms: {int(duration_s * 1000)}\n")


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def _fields(b: bytes):
    """(field number, value) for one protobuf message: ints for varints, bytes otherwise."""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        wt = key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")
        yield key >> 3, v


def surface_frames(trace: Path) -> list[dict]:
    """Every finished surface frame: {layer, pid, present, start_ns, end_ns}, by end time.

    Trace = repeated TracePacket packet = 1; TracePacket.timestamp = 8,
    .frame_timeline_event = 76; FrameTimelineEvent.actual_surface_frame_start = 4
    (cookie 1, pid 4, layer_name 5, present_type 6), .frame_end = 5 (cookie 1)."""
    data = Path(trace).read_bytes()
    starts: dict[int, tuple] = {}
    ends: dict[int, int] = {}
    for f, pkt in _fields(data):
        if f != 1 or not isinstance(pkt, bytes):
            continue
        ts = ev = None
        for pf, pv in _fields(pkt):
            if pf == 8:
                ts = pv
            elif pf == 76:
                ev = pv
        if ev is None or ts is None:
            continue
        for ef, body in _fields(ev):
            if ef not in (4, 5):
                continue
            d = dict(_fields(body))
            if ef == 4:
                starts[d.get(1)] = (ts, d.get(5, b"").decode(errors="replace"), d.get(4), d.get(6, 0))
            else:
                ends[d.get(1)] = ts
    out = []
    for cookie, (ts, layer, pid, pt) in starts.items():
        end = ends.get(cookie)
        if end is not None and ts <= end < 2**62:  # unfinished frames end at INT64_MAX
            out.append({"layer": layer, "pid": pid, "present": PRESENT.get(pt, str(pt)),
                        "start_ns": ts, "end_ns": end})
    out.sort(key=lambda x: x["end_ns"])
    return out


def boot_minus_mono(trace: Path) -> int | None:
    """CLOCK_BOOTTIME - CLOCK_MONOTONIC in ns (the time the device slept since boot),
    from the trace's first clock snapshot (TracePacket.clock_snapshot = 6: repeated
    Clock clocks = 1 {clock_id 1, timestamp 2}; MONOTONIC = 3, BOOTTIME = 6).
    SurfaceFlinger's own timestamps (dumpsys --latency) are MONOTONIC."""
    for f, pkt in _fields(Path(trace).read_bytes()):
        if f != 1 or not isinstance(pkt, bytes):
            continue
        for pf, pv in _fields(pkt):
            if pf == 6:
                clocks = {c.get(1): c.get(2) for c in (dict(_fields(cv)) for cf, cv in _fields(pv) if cf == 1)}
                if 3 in clocks and 6 in clocks:
                    return clocks[6] - clocks[3]
    return None


def by_layer(frames: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for fr in frames:
        out[fr["layer"]].append(fr)
    return dict(out)

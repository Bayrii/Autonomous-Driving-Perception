"""Wire protocol shared by the phone client (phone_client/app.js) and the PC.

Two kinds of WebSocket messages travel phone -> PC:

* Binary: one video frame = 32-byte little-endian header + JPEG bytes.
* Text: JSON objects with a ``type`` field (hello, imu, gnss, pong, status).

PC -> phone messages are JSON (welcome, ping, state).

All phone timestamps are integer nanoseconds of the phone's *monotonic* page
clock (``performance.now()``), which starts at page load. One page load = one
``session_id`` = one phone clock domain. See docs/protocol.md.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

PROTOCOL_VERSION = 1

FRAME_MAGIC = b"VFR1"
# magic, frame_index, t_capture_ns, t_send_ns, width, height, ts_source, orientation/90, reserved
FRAME_HEADER = struct.Struct("<4sIqqHHBBH")
assert FRAME_HEADER.size == 32

# How the capture timestamp of a frame was obtained on the phone (best -> worst).
TS_SOURCES = {
    0: "unknown",
    1: "rvfc_capture_time",   # requestVideoFrameCallback metadata.captureTime (camera capture)
    2: "rvfc_callback_time",  # requestVideoFrameCallback 'now' (frame presented to page)
    3: "poll_time",           # performance.now() when the frame was grabbed by a timer
    4: "simulated",           # tools/fake_phone.py
}

SENSOR_UNITS = {
    "gyro": "rad/s",
    "accel": "m/s^2",
}
SENSOR_FRAME = "phone_body"  # x right, y up (top of screen), z out of the screen


@dataclass(frozen=True)
class FrameHeader:
    frame_index: int
    t_capture_ns: int
    t_send_ns: int
    width: int
    height: int
    ts_source: int
    orientation_deg: int

    @property
    def ts_source_name(self) -> str:
        return TS_SOURCES.get(self.ts_source, "unknown")


def pack_frame(h: FrameHeader, jpeg: bytes) -> bytes:
    return FRAME_HEADER.pack(
        FRAME_MAGIC, h.frame_index, h.t_capture_ns, h.t_send_ns, h.width, h.height,
        h.ts_source, (h.orientation_deg // 90) % 4, 0,
    ) + jpeg


def unpack_frame(buf: bytes) -> tuple[FrameHeader, bytes]:
    if len(buf) < FRAME_HEADER.size:
        raise ValueError(f"frame message too short ({len(buf)} bytes)")
    magic, idx, t_cap, t_send, w, h, src, orient, _ = FRAME_HEADER.unpack_from(buf)
    if magic != FRAME_MAGIC:
        raise ValueError(f"bad frame magic {magic!r}")
    return FrameHeader(idx, t_cap, t_send, w, h, src, orient * 90), buf[FRAME_HEADER.size:]


@dataclass(frozen=True)
class ImuSample:
    sensor_type: str  # "gyro" | "accel"
    seq: int          # per-sensor sequence id, increments by 1 for every sample produced on the phone
    t_ns: int         # phone monotonic clock
    x: float
    y: float
    z: float


def parse_imu_samples(msg: dict) -> list[ImuSample]:
    """``msg["samples"]`` is a list of ``[sensor_type, seq, t_ns, x, y, z]``."""
    return [ImuSample(s[0], int(s[1]), int(s[2]), float(s[3]), float(s[4]), float(s[5]))
            for s in msg.get("samples", [])]

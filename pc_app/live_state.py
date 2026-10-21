"""Thread-safe live state shared by the network thread and the viewer (main) thread."""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .clock import LiveClockSync, SyncSample, pc_now_ns
from .protocol import FrameHeader, ImuSample


def _pct(values, q) -> float | None:
    return float(np.percentile(values, q)) if len(values) else None


@dataclass
class SeqTracker:
    """Counts sequence-id gaps (= messages lost between producer and PC)."""
    last: int | None = None
    received: int = 0
    missing: int = 0
    out_of_order: int = 0

    def add(self, seq: int) -> None:
        self.received += 1
        if self.last is not None:
            if seq > self.last + 1:
                self.missing += seq - self.last - 1
            elif seq <= self.last:
                self.out_of_order += 1
                return
        self.last = seq


@dataclass
class LiveState:
    window_s: float = 5.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    connected: bool = False
    session_id: str = ""
    hello: dict = field(default_factory=dict)
    phone_status: dict = field(default_factory=dict)
    clock: LiveClockSync = field(default_factory=LiveClockSync)
    latest_frame: tuple[FrameHeader, bytes, int] | None = None
    frame_seq: SeqTracker = field(default_factory=SeqTracker)
    imu_seq: dict[str, SeqTracker] = field(default_factory=dict)
    frame_times: deque = field(default_factory=lambda: deque(maxlen=300))     # (t_recv_pc, t_cap_phone)
    lat_cap_recv: deque = field(default_factory=lambda: deque(maxlen=300))    # ns
    lat_cap_display: deque = field(default_factory=lambda: deque(maxlen=300))  # ns
    imu_buf: dict[str, deque] = field(default_factory=dict)                   # (t_phone, x, y, z)
    bytes_recv: deque = field(default_factory=lambda: deque(maxlen=300))      # (t_recv_pc, nbytes)

    # ---------------------------------------------------------------- producers (network thread)
    def on_connect(self, session_id: str, hello: dict) -> None:
        with self.lock:
            self.connected, self.session_id, self.hello = True, session_id, hello
            self.clock = LiveClockSync()
            self.frame_seq, self.imu_seq = SeqTracker(), {}
            self.frame_times.clear(); self.lat_cap_recv.clear(); self.lat_cap_display.clear()
            self.imu_buf.clear(); self.bytes_recv.clear()
            self.latest_frame = None

    def on_disconnect(self) -> None:
        with self.lock:
            self.connected = False

    def on_frame(self, h: FrameHeader, jpeg: bytes, t_recv: int) -> None:
        with self.lock:
            self.latest_frame = (h, jpeg, t_recv)
            self.frame_seq.add(h.frame_index)
            self.frame_times.append((t_recv, h.t_capture_ns))
            self.bytes_recv.append((t_recv, len(jpeg)))
            t_cap_pc = self.clock.phone_to_pc(h.t_capture_ns)
            if t_cap_pc is not None:
                self.lat_cap_recv.append(t_recv - t_cap_pc)

    def on_imu(self, samples: list[ImuSample]) -> None:
        with self.lock:
            for s in samples:
                self.imu_seq.setdefault(s.sensor_type, SeqTracker()).add(s.seq)
                buf = self.imu_buf.setdefault(s.sensor_type, deque(maxlen=4000))
                buf.append((s.t_ns, s.x, s.y, s.z))
            for buf in self.imu_buf.values():
                if buf:
                    t_last = buf[-1][0]
                    while buf and t_last - buf[0][0] > self.window_s * 1e9:
                        buf.popleft()

    def on_sync(self, s: SyncSample) -> None:
        with self.lock:
            self.clock.add(s)

    def on_status(self, msg: dict) -> None:
        with self.lock:
            self.phone_status = msg

    # ---------------------------------------------------------------- consumers (viewer thread)
    def on_display(self, h: FrameHeader, t_display: int) -> None:
        with self.lock:
            t_cap_pc = self.clock.phone_to_pc(h.t_capture_ns)
            if t_cap_pc is not None:
                self.lat_cap_display.append(t_display - t_cap_pc)

    def imu_arrays(self, sensor: str) -> np.ndarray:
        with self.lock:
            buf = self.imu_buf.get(sensor)
            return np.array(buf, dtype=np.float64) if buf else np.zeros((0, 4))

    def snapshot(self) -> dict:
        now = pc_now_ns()
        with self.lock:
            ft = [t for t in self.frame_times if now - t[0] < 2e9]
            fps = None
            if len(ft) > 2 and ft[-1][1] > ft[0][1]:
                fps = (len(ft) - 1) / ((ft[-1][1] - ft[0][1]) / 1e9)
            rates = {}
            for name, buf in self.imu_buf.items():
                ts = [b[0] for b in buf if buf[-1][0] - b[0] < 2e9]
                if len(ts) > 2 and ts[-1] > ts[0]:
                    rates[name] = (len(ts) - 1) / ((ts[-1] - ts[0]) / 1e9)
            br = [b for b in self.bytes_recv if now - b[0] < 2e9]
            best = self.clock.best()
            return {
                "connected": self.connected,
                "session_id": self.session_id,
                "ua": self.hello.get("user_agent", ""),
                "imu_api": self.hello.get("imu_api", ""),
                "fps": fps,
                "imu_hz": rates,
                "mbps": sum(b[1] for b in br) * 8 / 2e6 if br else 0.0,
                "frame_missing": self.frame_seq.missing,
                "frames_received": self.frame_seq.received,
                "imu_missing": {k: v.missing for k, v in self.imu_seq.items()},
                "phone_dropped": (self.phone_status.get("frames_dropped_busy", 0) or 0)
                                 + (self.phone_status.get("frames_dropped_backpressure", 0) or 0),
                "lat_recv_p50": _pct(self.lat_cap_recv, 50), "lat_recv_p95": _pct(self.lat_cap_recv, 95),
                "lat_disp_p50": _pct(self.lat_cap_display, 50), "lat_disp_p95": _pct(self.lat_cap_display, 95),
                "rtt_min": best.rtt_ns if best else None,
                "offset": best.offset_ns if best else None,
                "stale_ms": (now - self.latest_frame[2]) / 1e6 if self.latest_frame else None,
            }

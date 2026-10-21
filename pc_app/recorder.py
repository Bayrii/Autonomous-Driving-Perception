"""Session recorder: raw JPEG frames + CSV logs + manifest.json.

All disk IO happens on a dedicated writer thread so the network loop never
blocks. JPEG bytes are stored exactly as received (no re-encoding), so a replay
sees the same pixels the live pipeline saw.

Layout (see docs/protocol.md for every column):

    sessions/<rec_id>/
        manifest.json      session metadata, clock basis, axes/units, counters
        frames/<idx>.jpg   raw frames, file name = phone frame_index
        frames.csv         one row per received frame
        imu.csv            gyro + accel samples (long format)
        gnss.csv           optional location fixes
        clock_sync.csv     ping/pong samples for the phone<->PC clock model
        display.csv        live viewer display times (capture-to-display latency)
        status.csv         phone-side counters reported once per second
        events.csv         connect / disconnect / record start / stop
"""
from __future__ import annotations

import csv
import json
import platform
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .clock import SyncSample, pc_now_ns
from .protocol import PROTOCOL_VERSION, SENSOR_FRAME, SENSOR_UNITS, FrameHeader, ImuSample, TS_SOURCES

CSV_COLUMNS = {
    "frames": ["session_id", "frame_index", "t_capture_phone_ns", "t_send_phone_ns", "t_recv_pc_ns",
               "width", "height", "ts_source", "orientation_deg", "jpeg_bytes", "file"],
    "imu": ["session_id", "sensor_type", "seq", "t_phone_ns", "t_recv_pc_ns", "x", "y", "z",
            "unit", "frame", "accuracy"],
    "gnss": ["session_id", "seq", "t_phone_ns", "t_recv_pc_ns", "fix_time_unix_ms", "lat_deg", "lon_deg",
             "alt_m", "horiz_acc_m", "vert_acc_m", "speed_mps", "heading_deg"],
    "clock_sync": ["session_id", "ping_id", "t_pc_send_ns", "t_phone_ns", "t_pc_recv_ns", "rtt_ns", "offset_ns"],
    "display": ["session_id", "frame_index", "t_recv_pc_ns", "t_display_pc_ns"],
    "status": ["session_id", "t_pc_ns", "t_phone_ns", "frames_captured", "frames_sent",
               "frames_dropped_busy", "frames_dropped_backpressure", "imu_samples", "ws_buffered_bytes",
               "orientation_deg", "imu_api"],
    "events": ["t_pc_ns", "event", "detail"],
}


class Recorder:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.dir: Path | None = None
        self.session_id = ""
        self.counts: dict[str, int] = {}
        self.t_start_pc_ns = 0
        self._manifest: dict = {}

    @property
    def active(self) -> bool:
        return self._thread is not None

    # ------------------------------------------------------------------ control
    def start(self, session_id: str, hello: dict | None, config: dict | None = None) -> Path:
        if self.active:
            return self.dir
        self.session_id = session_id or "nosession"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = self.root / f"{stamp}_{self.session_id}"
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        self.counts = {k: 0 for k in CSV_COLUMNS}
        self.t_start_pc_ns = pc_now_ns()
        self._manifest = {
            "rec_id": self.dir.name,
            "session_id": self.session_id,
            "protocol_version": PROTOCOL_VERSION,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "pc": {"host": socket.gethostname(), "platform": platform.platform(),
                   "python": platform.python_version()},
            "clock_basis": {
                "phone": "performance.now() of the phone web page (monotonic, starts at page load), "
                         "integer ns; one session_id = one phone clock domain",
                "pc": "time.perf_counter_ns() on the PC (monotonic)",
                "phone_to_pc": "fit from clock_sync.csv, see pc_app/clock.py (offset = phone - pc)",
            },
            "imu": {"frame": SENSOR_FRAME, "axes": "x right, y up (toward top of screen), z out of screen",
                    "units": SENSOR_UNITS},
            "frame_ts_sources": TS_SOURCES,
            "phone_hello": hello or {},
            "config": config or {},
            "calibration": None,
            "t_start_pc_ns": self.t_start_pc_ns,
        }
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()
        self.event("record_start", self.dir.name)
        return self.dir

    def stop(self) -> Path | None:
        if not self.active:
            return None
        self.event("record_stop", "")
        self._q.put(None)
        self._thread.join()
        self._thread = None
        self._manifest["t_stop_pc_ns"] = pc_now_ns()
        self._manifest["duration_s"] = (self._manifest["t_stop_pc_ns"] - self.t_start_pc_ns) / 1e9
        self._manifest["row_counts"] = dict(self.counts)
        (self.dir / "manifest.json").write_text(json.dumps(self._manifest, indent=2))
        return self.dir

    def update_hello(self, hello: dict) -> None:
        self._manifest["phone_hello"] = hello

    # ------------------------------------------------------------------ writes
    def frame(self, h: FrameHeader, jpeg: bytes, t_recv: int) -> None:
        if self.active:
            self._q.put(("frame", h, jpeg, t_recv))

    def imu(self, samples: list[ImuSample], t_recv: int) -> None:
        if self.active:
            self._q.put(("rows", "imu", [[self.session_id, s.sensor_type, s.seq, s.t_ns, t_recv, s.x, s.y, s.z,
                                          SENSOR_UNITS.get(s.sensor_type, ""), SENSOR_FRAME, ""]
                                         for s in samples]))

    def gnss(self, samples: list[dict], t_recv: int) -> None:
        if self.active:
            self._q.put(("rows", "gnss", [[self.session_id, s.get("seq"), s.get("t"), t_recv, s.get("fix_ms"),
                                           s.get("lat"), s.get("lon"), s.get("alt"), s.get("acc"),
                                           s.get("alt_acc"), s.get("speed"), s.get("heading")]
                                          for s in samples]))

    def sync(self, s: SyncSample) -> None:
        if self.active:
            self._q.put(("rows", "clock_sync", [[self.session_id, s.ping_id, s.t_pc_send_ns, s.t_phone_ns,
                                                 s.t_pc_recv_ns, s.rtt_ns, round(s.offset_ns)]]))

    def display(self, frame_index: int, t_recv: int, t_display: int) -> None:
        if self.active:
            self._q.put(("rows", "display", [[self.session_id, frame_index, t_recv, t_display]]))

    def status(self, msg: dict, t_pc: int) -> None:
        if self.active:
            self._q.put(("rows", "status", [[self.session_id, t_pc] + [msg.get(k) for k in CSV_COLUMNS["status"][2:]]]))

    def event(self, name: str, detail: str = "") -> None:
        if self.active:
            self._q.put(("rows", "events", [[pc_now_ns(), name, detail]]))

    # ------------------------------------------------------------------ thread
    def _run(self) -> None:
        files, writers = {}, {}
        for name, cols in CSV_COLUMNS.items():
            f = open(self.dir / f"{name}.csv", "w", newline="", buffering=1 << 16)
            w = csv.writer(f)
            w.writerow(cols)
            files[name], writers[name] = f, w
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    item = self._q.get(timeout=0.5)
                except queue.Empty:
                    item = ()
                if item is None:
                    break
                if item and item[0] == "frame":
                    _, h, jpeg, t_recv = item
                    fname = f"frames/{h.frame_index:08d}.jpg"
                    (self.dir / fname).write_bytes(jpeg)
                    writers["frames"].writerow([self.session_id, h.frame_index, h.t_capture_ns, h.t_send_ns, t_recv,
                                                h.width, h.height, h.ts_source_name, h.orientation_deg,
                                                len(jpeg), fname])
                    self.counts["frames"] += 1
                elif item and item[0] == "rows":
                    _, name, rows = item
                    writers[name].writerows(rows)
                    self.counts[name] += len(rows)
                if time.monotonic() - last_flush > 1.0:
                    for f in files.values():
                        f.flush()
                    last_flush = time.monotonic()
        finally:
            for f in files.values():
                f.close()

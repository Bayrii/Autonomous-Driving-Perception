"""On-disk session store.

    <DATA_DIR>/<session_id>/
        metadata.json   configuration, time-basis definitions, units, availability, counters, status
        frames/<seq>.jpg
        frames.csv      one row per accepted frame
        imu.jsonl       one JSON object per IMU sample (browser field names and units)

The data files are the source of truth: after a process restart the in-memory
state (accepted sequence numbers, byte counts) is rebuilt from them. On Render's
free tier the disk itself is ephemeral, so a restart usually loses everything.
"""
from __future__ import annotations

import csv
import io
import json
import os
import secrets
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import FrameMeta, ImuBatch, SessionCreate, StopRequest
from .settings import Settings

FRAME_COLUMNS = ["session_id", "seq", "file", "width", "height", "bytes", "t_capture_mono_ms",
                 "t_encoded_mono_ms", "rvfc_capture_time_ms", "rvfc_media_time_s", "client_attempt",
                 "server_receipt_unix_ms"]

TIME_BASIS = {
    "t_mono_ms": "performance.now() on the phone page, in milliseconds since the page's time origin. "
                 "Monotonic. The common phone-side time basis for frames and IMU samples.",
    "time_origin_unix_ms": "performance.timeOrigin reported by the phone: approximate wall-clock time of "
                           "t_mono_ms = 0 (client.time_origin_unix_ms).",
    "t_capture_mono_ms": "performance.now() immediately before the current video frame was drawn to the "
                         "canvas. A capture-time ESTIMATE: the real exposure happened earlier by an unknown "
                         "camera/browser pipeline delay (typically tens of milliseconds).",
    "t_encoded_mono_ms": "performance.now() when JPEG encoding of the frame finished.",
    "rvfc_capture_time_ms": "requestVideoFrameCallback metadata.captureTime of the most recently presented "
                            "video frame, same time origin, only when the browser provides it. Closer to the "
                            "camera capture time, but still not a guaranteed exposure timestamp.",
    "event_ts_ms": "Event.timeStamp of the DeviceMotion/DeviceOrientation event (same time origin in current "
                   "browsers). This is the browser's event dispatch time, NOT a hardware sensor timestamp.",
    "server_receipt_unix_ms": "Server wall-clock time when the upload arrived. Diagnostics only: never use it "
                              "to align camera and IMU data (it includes network and retry delays).",
}

UNITS = {
    "acceleration": "m/s^2, device frame, gravity excluded (DeviceMotionEvent.acceleration)",
    "accelerationIncludingGravity": "m/s^2, device frame, gravity included",
    "rotationRate": "deg/s (NOT rad/s): alpha about device z, beta about x, gamma about y",
    "orientation": "deg: alpha (about z, 0..360), beta (about x, -180..180), gamma (about y, -90..90); "
                   "absolute = whether alpha is relative to magnetic north",
    "interval": "ms, browser-reported motion event interval",
    "device_frame": "x to the right of the screen, y toward the top of the screen, z out of the screen",
}

LIMITATIONS = [
    "Browser motion events carry dispatch times, not raw hardware IMU timestamps.",
    "Frame timestamps are taken around drawing/encoding and are capture-time estimates, not exposure times.",
    "Camera-IMU synchronisation is therefore approximate (expect tens of milliseconds of offset).",
    "Frames are re-encoded JPEGs from a browser canvas, not the camera's raw output.",
    "Phones pause or stop camera and sensors when the page is hidden or the screen turns off.",
    "Null sensor values mean the browser/device did not provide them; they were not replaced by zeros.",
]


class StoreError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _utc(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Session:
    id: str
    dir: Path
    meta: dict
    created_unix: float
    status: str = "active"                  # active | completed | interrupted | time_limit
    ended_unix: float | None = None
    last_activity_unix: float = 0.0
    frame_seqs: set[int] = field(default_factory=set)
    batch_seqs: set[int] = field(default_factory=set)
    bytes: int = 0
    counters: dict = field(default_factory=lambda: {
        "frames_accepted": 0, "frames_duplicate": 0, "frames_rejected": 0,
        "imu_batches_accepted": 0, "imu_batches_duplicate": 0, "imu_batches_rejected": 0,
        "imu_motion_samples": 0, "imu_orientation_samples": 0})
    frame_sizes: dict = field(default_factory=dict)   # "WxH" -> count (actual encoded dimensions)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def active(self) -> bool:
        return self.status == "active"

    def summary(self, ttl_s: int) -> dict:
        end = self.ended_unix
        return {
            "session_id": self.id, "status": self.status,
            "created_utc": _utc(self.created_unix), "ended_utc": _utc(end),
            "duration_s": round((end or time.time()) - self.created_unix, 1),
            "frames": self.counters["frames_accepted"],
            "imu_samples": self.counters["imu_motion_samples"] + self.counters["imu_orientation_samples"],
            "bytes": self.bytes,
            "expires_utc": _utc(end + ttl_s) if end else None,
            "downloadable": not self.active,
        }


class SessionStore:
    def __init__(self, settings: Settings):
        self.s = settings
        self.root = Path(settings.data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.exports = self.root / "_exports"
        self.exports.mkdir(exist_ok=True)
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self._load_existing()

    # ------------------------------------------------------------------ helpers
    @property
    def total_bytes(self) -> int:
        return sum(s.bytes for s in list(self.sessions.values()))

    def get(self, sid: str) -> Session:
        sess = self.sessions.get(sid)
        if sess is None:
            raise StoreError(404, "session_not_found",
                             "Session not found on the server. It may have expired, been deleted, or been lost "
                             "when the server restarted (Render's free tier does not keep files).")
        return sess

    def _require_active(self, sess: Session, now: float) -> None:
        if sess.active and now - sess.created_unix > self.s.max_session_seconds:
            self._finalize(sess, "time_limit", now)
        if not sess.active:
            raise StoreError(409, "session_not_active",
                             f"Session is no longer recording (status: {sess.status}). It can be downloaded.")

    def _write_meta(self, sess: Session) -> None:
        m = sess.meta
        m.update({
            "status": sess.status, "ended_utc": _utc(sess.ended_unix),
            "server_counters": dict(sess.counters), "frame_dimensions_actual": dict(sess.frame_sizes),
            "bytes": sess.bytes,
        })
        _atomic_write(sess.dir / "metadata.json", json.dumps(m, indent=2))

    def _finalize(self, sess: Session, status: str, now: float) -> None:
        sess.status, sess.ended_unix = status, now
        self._write_meta(sess)

    # ------------------------------------------------------------------ create
    def create(self, req: SessionCreate) -> Session:
        now = time.time()
        with self.lock:
            active = sum(1 for s in self.sessions.values() if s.active)
            if active >= self.s.max_active_sessions:
                raise StoreError(429, "too_many_active_sessions",
                                 f"{active} recordings are already active on the server. Stop them first.")
            if self.total_bytes + self.s.max_frame_bytes > self.s.max_total_bytes:  # room for at least a frame
                raise StoreError(507, "storage_full", "Server storage is full. Download and delete old recordings.")
            sid = secrets.token_hex(16)
            d = self.root / sid
            (d / "frames").mkdir(parents=True)
            meta = {
                "session_id": sid, "format_version": 1, "created_utc": _utc(now), "status": "active",
                "client": req.client.model_dump(), "requested_capture": req.requested.model_dump(),
                "camera": req.camera.model_dump(), "sensor_availability": req.sensors.model_dump(),
                "time_basis": TIME_BASIS, "units": UNITS, "limitations": LIMITATIONS,
                "files": {"frames/": "JPEG images named <seq>.jpg", "frames.csv": "frame metadata",
                          "imu.jsonl": "one IMU sample per line", "metadata.json": "this file"},
                "server_limits": {"max_session_seconds": self.s.max_session_seconds,
                                  "max_session_bytes": self.s.max_session_bytes,
                                  "max_frame_bytes": self.s.max_frame_bytes},
            }
            sess = Session(sid, d, meta, created_unix=now, last_activity_unix=now)
            try:
                with open(d / "frames.csv", "w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(FRAME_COLUMNS)
                (d / "imu.jsonl").touch()
                self._write_meta(sess)
            except OSError:
                shutil.rmtree(d, ignore_errors=True)   # no half-created session left behind
                raise
            self.sessions[sid] = sess
            return sess

    # ------------------------------------------------------------------ uploads
    def _check_space(self, sess: Session, incoming: int) -> None:
        if sess.bytes + incoming > self.s.max_session_bytes:
            raise StoreError(507, "session_storage_limit",
                             "This recording reached its storage limit. Stop it and start a new one.")
        if self.total_bytes + incoming > self.s.max_total_bytes:
            raise StoreError(507, "storage_full", "Server storage is full. Download and delete old recordings.")

    def add_frame(self, sid: str, meta: FrameMeta, jpeg: bytes, width: int, height: int) -> str:
        now = time.time()
        sess = self.get(sid)
        with sess.lock:
            self._require_active(sess, now)
            if meta.seq in sess.frame_seqs:           # retried upload: already stored
                sess.counters["frames_duplicate"] += 1
                sess.last_activity_unix = now
                return "duplicate"
            self._check_space(sess, len(jpeg))
            name = f"frames/{meta.seq:06d}.jpg"
            tmp = sess.dir / (name + ".tmp")
            tmp.write_bytes(jpeg)
            os.replace(tmp, sess.dir / name)
            row = [sid, meta.seq, name, width, height, len(jpeg), meta.t_capture_mono_ms,
                   _blank(meta.t_encoded_mono_ms), _blank(meta.rvfc_capture_time_ms),
                   _blank(meta.rvfc_media_time_s), meta.attempt, round(now * 1000, 1)]
            with open(sess.dir / "frames.csv", "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow(row)
            sess.frame_seqs.add(meta.seq)
            sess.bytes += len(jpeg)
            sess.counters["frames_accepted"] += 1
            key = f"{width}x{height}"
            sess.frame_sizes[key] = sess.frame_sizes.get(key, 0) + 1
            sess.last_activity_unix = now
            return "stored"

    def add_imu(self, sid: str, batch: ImuBatch) -> tuple[str, int]:
        now = time.time()
        sess = self.get(sid)
        with sess.lock:
            self._require_active(sess, now)
            if batch.batch_seq in sess.batch_seqs:
                sess.counters["imu_batches_duplicate"] += 1
                sess.last_activity_unix = now
                return "duplicate", 0
            receipt = round(now * 1000, 1)
            buf = io.StringIO()
            for smp in batch.samples:
                row = {"session_id": sid, "batch_seq": batch.batch_seq, **smp.model_dump(),
                       "server_receipt_unix_ms": receipt}
                buf.write(json.dumps(row, separators=(",", ":")) + "\n")
            data = buf.getvalue().encode("utf-8")
            self._check_space(sess, len(data))
            with open(sess.dir / "imu.jsonl", "ab") as fh:
                fh.write(data)
            sess.batch_seqs.add(batch.batch_seq)
            sess.bytes += len(data)
            sess.counters["imu_batches_accepted"] += 1
            for smp in batch.samples:
                sess.counters[f"imu_{smp.kind}_samples"] += 1
            sess.last_activity_unix = now
            return "stored", len(batch.samples)

    def count_rejected(self, sid: str, kind: str) -> None:
        sess = self.sessions.get(sid)
        if sess is not None:
            with sess.lock:
                sess.counters[f"{kind}_rejected"] += 1

    # ------------------------------------------------------------------ stop / export / delete
    def stop(self, sid: str, req: StopRequest) -> dict:
        now = time.time()
        sess = self.get(sid)
        with sess.lock:
            if sess.active:
                sess.status, sess.ended_unix = "completed", now
            sess.meta["stop"] = {"reason": req.reason, "received_utc": _utc(now)}
            sess.meta["client_counters"] = req.counters.model_dump()
            sess.meta["observed"] = req.observed.model_dump()
            sess.meta["client_events"] = [e.model_dump() for e in req.events]
            self._write_meta(sess)
            out = sess.summary(self.s.session_ttl_s)
            out["server_counters"] = dict(sess.counters)
            return out

    def export_zip(self, sid: str) -> tuple[Path, str]:
        sess = self.get(sid)
        if sess.active:
            raise StoreError(409, "session_active", "Stop the recording before downloading it.")
        with sess.lock:
            self._write_meta(sess)
            path = self.exports / f"{sid}-{secrets.token_hex(4)}.zip"
            # streamed file by file: memory stays bounded regardless of recording size
            with zipfile.ZipFile(path, "w", allowZip64=True) as zf:
                for name in ("metadata.json", "frames.csv", "imu.jsonl"):
                    zf.write(sess.dir / name, f"{sid}/{name}", compress_type=zipfile.ZIP_DEFLATED)
                for p in sorted((sess.dir / "frames").glob("*.jpg")):
                    zf.write(p, f"{sid}/frames/{p.name}", compress_type=zipfile.ZIP_STORED)
        return path, f"recording-{sid}.zip"

    def delete(self, sid: str) -> None:
        sess = self.get(sid)
        if sess.active:
            raise StoreError(409, "session_active", "Stop the recording before deleting it.")
        with self.lock:
            self.sessions.pop(sid, None)
        shutil.rmtree(sess.dir, ignore_errors=True)

    def list(self) -> list[dict]:
        return sorted((s.summary(self.s.session_ttl_s) for s in self.sessions.values()),
                      key=lambda d: d["created_utc"], reverse=True)

    # ------------------------------------------------------------------ cleanup
    def cleanup(self, now: float | None = None) -> dict:
        """Finalize idle/over-time sessions, delete expired finished ones and stale exports.
        Active sessions are never deleted."""
        now = now or time.time()
        report = {"interrupted": 0, "time_limit": 0, "deleted": 0, "exports_removed": 0}
        for sess in list(self.sessions.values()):
            with sess.lock:
                if sess.active and now - sess.created_unix > self.s.max_session_seconds:
                    self._finalize(sess, "time_limit", now)
                    report["time_limit"] += 1
                elif sess.active and now - sess.last_activity_unix > self.s.session_idle_timeout_s:
                    self._finalize(sess, "interrupted", sess.last_activity_unix)
                    report["interrupted"] += 1
            if not sess.active and sess.ended_unix and now - sess.ended_unix > self.s.session_ttl_s:
                with self.lock:
                    self.sessions.pop(sess.id, None)
                shutil.rmtree(sess.dir, ignore_errors=True)
                report["deleted"] += 1
        for p in self.exports.glob("*.zip"):
            try:
                if now - p.stat().st_mtime > 3600:
                    p.unlink()
                    report["exports_removed"] += 1
            except OSError:
                pass
        return report

    # ------------------------------------------------------------------ restart recovery
    def _load_existing(self) -> None:
        for d in self.root.iterdir():
            if not d.is_dir() or d.name.startswith("_") or not (d / "metadata.json").exists():
                continue
            try:
                meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
                created = datetime.fromisoformat(meta["created_utc"]).timestamp()
                ended = datetime.fromisoformat(meta["ended_utc"]).timestamp() if meta.get("ended_utc") else None
                sess = Session(d.name, d, meta, created_unix=created, status=meta.get("status", "interrupted"),
                               ended_unix=ended, last_activity_unix=time.time())
                with open(d / "frames.csv", newline="", encoding="utf-8") as fh:
                    for r in csv.DictReader(fh):
                        sess.frame_seqs.add(int(r["seq"]))
                        key = f"{r['width']}x{r['height']}"
                        sess.frame_sizes[key] = sess.frame_sizes.get(key, 0) + 1
                with open(d / "imu.jsonl", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            r = json.loads(line)
                            sess.batch_seqs.add(int(r["batch_seq"]))
                            sess.counters[f"imu_{r['kind']}_samples"] += 1
                sess.counters.update({k: v for k, v in meta.get("server_counters", {}).items()
                                      if k.endswith(("_duplicate", "_rejected"))})
                sess.counters["frames_accepted"] = len(sess.frame_seqs)
                sess.counters["imu_batches_accepted"] = len(sess.batch_seqs)
                sess.bytes = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
                self.sessions[sess.id] = sess
            except (OSError, ValueError, KeyError):
                continue  # unreadable leftovers are ignored, never deleted automatically


def _blank(v):
    return "" if v is None else v

"""Configuration from environment variables (Render dashboard → Environment)."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    access_token: str = ""                 # APP_ACCESS_TOKEN: shared secret for all recording endpoints
    auth_disabled: bool = False            # APP_AUTH_DISABLED=1 only for local development
    data_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "phone-recordings")

    max_frame_bytes: int = 1_500_000       # one JPEG
    max_frame_side: int = 1920             # pixels, each side
    min_frame_side: int = 16
    validate_jpeg_decode: bool = True      # OpenCV reduced-size decode of every frame
    max_json_bytes: int = 1_000_000        # IMU batch / session / stop bodies
    max_imu_batch: int = 2000              # samples per batch
    max_session_seconds: int = 1800        # recording duration limit
    max_session_bytes: int = 400_000_000
    max_total_bytes: int = 1_500_000_000   # all sessions on disk
    max_active_sessions: int = 3
    session_idle_timeout_s: int = 600      # active session without uploads -> "interrupted"
    session_ttl_s: int = 6 * 3600          # finished sessions are deleted this long after they end
    cleanup_interval_s: int = 300

    # Phone-side defaults (requested targets, not guarantees)
    client_width: int = 640
    client_height: int = 480
    client_fps: float = 5.0
    client_jpeg_quality: float = 0.7
    client_imu_batch_ms: int = 300
    client_max_pending_frames: int = 10
    client_upload_concurrency: int = 2
    client_request_timeout_ms: int = 10_000
    client_max_retries: int = 3
    client_stop_flush_timeout_ms: int = 15_000

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.access_token = os.environ.get("APP_ACCESS_TOKEN", "").strip()
        s.auth_disabled = _bool("APP_AUTH_DISABLED")
        if os.environ.get("DATA_DIR", "").strip():
            s.data_dir = Path(os.environ["DATA_DIR"].strip())
        s.max_frame_bytes = _int("MAX_FRAME_BYTES", s.max_frame_bytes)
        s.max_session_seconds = _int("MAX_SESSION_SECONDS", s.max_session_seconds)
        s.max_session_bytes = _int("MAX_SESSION_BYTES", s.max_session_bytes)
        s.max_total_bytes = _int("MAX_TOTAL_BYTES", s.max_total_bytes)
        s.max_active_sessions = _int("MAX_ACTIVE_SESSIONS", s.max_active_sessions)
        s.session_idle_timeout_s = _int("SESSION_IDLE_TIMEOUT_S", s.session_idle_timeout_s)
        s.session_ttl_s = _int("SESSION_TTL_S", s.session_ttl_s)
        s.validate_jpeg_decode = _bool("VALIDATE_JPEG_DECODE", s.validate_jpeg_decode)
        return s

    def client_config(self) -> dict:
        return {
            "width": self.client_width, "height": self.client_height, "fps": self.client_fps,
            "jpeg_quality": self.client_jpeg_quality, "imu_batch_ms": self.client_imu_batch_ms,
            "max_pending_frames": self.client_max_pending_frames,
            "upload_concurrency": self.client_upload_concurrency,
            "request_timeout_ms": self.client_request_timeout_ms, "max_retries": self.client_max_retries,
            "stop_flush_timeout_ms": self.client_stop_flush_timeout_ms,
            "max_imu_batch": self.max_imu_batch, "max_frame_bytes": self.max_frame_bytes,
            "max_session_seconds": self.max_session_seconds,
            "auth_required": not self.auth_disabled,
        }

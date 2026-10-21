"""Request schemas. Field names of sensor data follow the browser APIs exactly
(DeviceMotionEvent / DeviceOrientationEvent), units unchanged, nulls preserved."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Num = Optional[float]   # null = the browser did not provide the value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientInfo(_Strict):
    user_agent: str = Field("", max_length=512)
    platform: str = Field("", max_length=64)
    language: str = Field("", max_length=32)
    screen_width: Optional[int] = Field(None, ge=0, le=20000)
    screen_height: Optional[int] = Field(None, ge=0, le=20000)
    device_pixel_ratio: Num = None
    time_origin_unix_ms: Num = None        # performance.timeOrigin: wall clock at t_mono_ms = 0
    secure_context: Optional[bool] = None


class RequestedCapture(_Strict):
    width: int = Field(640, ge=16, le=1920)
    height: int = Field(480, ge=16, le=1920)
    fps: float = Field(5.0, gt=0, le=30)
    jpeg_quality: float = Field(0.7, ge=0.1, le=1.0)
    imu_batch_ms: int = Field(300, ge=50, le=5000)


class CameraInfo(_Strict):
    available: bool = False
    label: str = Field("", max_length=256)
    facing_mode_requested: str = Field("environment", max_length=32)
    # MediaTrackSettings as reported by the browser (actual, not requested)
    track_settings: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=40)
    video_width: Optional[int] = Field(None, ge=0, le=10000)
    video_height: Optional[int] = Field(None, ge=0, le=10000)
    error: str = Field("", max_length=300)


class SensorInfo(_Strict):
    device_motion_api: bool = False         # 'DeviceMotionEvent' in window
    device_orientation_api: bool = False
    motion_permission: str = Field("not-required", max_length=32)       # granted | denied | not-required | error
    orientation_permission: str = Field("not-required", max_length=32)
    motion_events_seen: bool = False        # an event actually arrived before recording started
    orientation_events_seen: bool = False
    acceleration_available: Optional[bool] = None
    acceleration_including_gravity_available: Optional[bool] = None
    rotation_rate_available: Optional[bool] = None
    orientation_available: Optional[bool] = None
    note: str = Field("", max_length=500)


class SessionCreate(_Strict):
    client: ClientInfo = Field(default_factory=ClientInfo)
    requested: RequestedCapture = Field(default_factory=RequestedCapture)
    camera: CameraInfo = Field(default_factory=CameraInfo)
    sensors: SensorInfo = Field(default_factory=SensorInfo)


class FrameMeta(_Strict):
    """Query parameters of a frame upload (the body is the raw JPEG)."""
    seq: int = Field(ge=0, le=10_000_000)
    t_capture_mono_ms: float = Field(ge=0)
    t_encoded_mono_ms: Optional[float] = Field(None, ge=0)
    rvfc_capture_time_ms: Optional[float] = Field(None, ge=0)
    rvfc_media_time_s: Optional[float] = Field(None, ge=0)
    width: int = Field(ge=1, le=10000)
    height: int = Field(ge=1, le=10000)
    attempt: int = Field(1, ge=1, le=20)


class Vec3(_Strict):
    x: Num = None
    y: Num = None
    z: Num = None


class RotationRate(_Strict):
    alpha: Num = None   # deg/s about device z
    beta: Num = None    # deg/s about device x
    gamma: Num = None   # deg/s about device y


class Orientation(_Strict):
    alpha: Num = None   # deg
    beta: Num = None
    gamma: Num = None
    absolute: Optional[bool] = None


class ImuSample(_Strict):
    seq: int = Field(ge=0, le=100_000_000)
    kind: Literal["motion", "orientation"]
    t_mono_ms: float = Field(ge=0)          # performance.now() when the handler ran
    event_ts_ms: Num = None                 # Event.timeStamp (browser dispatch time, same origin)
    interval: Num = None                    # DeviceMotionEvent.interval, ms
    acceleration: Optional[Vec3] = None
    accelerationIncludingGravity: Optional[Vec3] = None
    rotationRate: Optional[RotationRate] = None
    orientation: Optional[Orientation] = None


class ImuBatch(_Strict):
    batch_seq: int = Field(ge=0, le=10_000_000)
    samples: list[ImuSample] = Field(min_length=1)


class ClientEvent(_Strict):
    t_mono_ms: Num = None
    type: str = Field(max_length=40)
    detail: str = Field("", max_length=300)


class ClientCounters(_Strict):
    frames_captured: int = Field(0, ge=0)
    frames_dropped_busy: int = Field(0, ge=0)        # encoder still busy with the previous frame
    frames_dropped_queue: int = Field(0, ge=0)       # upload queue full
    frames_dropped_not_ready: int = Field(0, ge=0)   # video had no frame yet / camera stopped
    frames_encode_failed: int = Field(0, ge=0)
    frames_accepted: int = Field(0, ge=0)
    frames_failed: int = Field(0, ge=0)              # gave up after retries / rejected
    frames_unsent_at_stop: int = Field(0, ge=0)
    imu_motion_events: int = Field(0, ge=0)
    imu_orientation_events: int = Field(0, ge=0)
    imu_samples_accepted: int = Field(0, ge=0)
    imu_samples_dropped: int = Field(0, ge=0)        # buffer overflow while offline
    imu_samples_failed: int = Field(0, ge=0)
    imu_samples_unsent_at_stop: int = Field(0, ge=0)
    upload_retries: int = Field(0, ge=0)
    page_hidden_count: int = Field(0, ge=0)


class Observed(_Strict):
    frame_fps: Num = None
    motion_hz: Num = None
    orientation_hz: Num = None
    video_width: Optional[int] = None
    video_height: Optional[int] = None
    encoded_width: Optional[int] = None
    encoded_height: Optional[int] = None


class StopRequest(_Strict):
    reason: str = Field("user", max_length=40)
    counters: ClientCounters = Field(default_factory=ClientCounters)
    observed: Observed = Field(default_factory=Observed)
    events: list[ClientEvent] = Field(default_factory=list, max_length=200)

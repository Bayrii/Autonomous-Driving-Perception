# Design decision: phone acquisition and transport stack (Week 1)

**Decision.** The PC serves a **browser-based acquisition page** to the phone. The page streams everything over
**one WebSocket**: IMU as batched JSON, and video as JPEG frames with a 32-byte timestamp header ("MJPEG over
WebSocket"). A **Python aiohttp** server ingests, records and replays the stream. Wi-Fi with HTTPS is the default
link. Android phones can also use USB via `adb reverse`.

**Main reason.** Gate W1 asks for *every frame to carry its capture time and the IMU to be alignable*. In the
browser, camera frames (`requestVideoFrameCallback` `captureTime`) and motion samples (Generic Sensor / DeviceMotion
timestamps) are all stamped on the **same monotonic phone clock** (`performance.now`). Frame↔IMU alignment therefore
does not depend on network delay. The PC↔phone clock relation is only needed for latency metrics, and it is
estimated separately with ping/pong.

## Options considered

| Option | For | Against | Verdict |
|---|---|---|---|
| **Native Android** (Camera2 + SensorManager) | Best timestamps: `SENSOR_TIMESTAMP` and `SensorEvent.timestamp` share `elapsedRealtimeNanos` when the timestamp source is REALTIME. IMU at 200–500 Hz. Exposure time and rolling-shutter skew are available. Hardware H.264. | Android only. Needs a Kotlin app, Android Studio and signing; 1–2 weeks to do properly. | Month-2 upgrade. Same protocol, so the PC side is unchanged. |
| **Native iOS** (AVFoundation + Core Motion) | Camera presentation timestamps and `CMDeviceMotion` share the host clock; 100 Hz | Needs a Mac, Xcode and a developer account | Rejected for Week 1 |
| **Off-the-shelf apps**: IP Webcam / DroidCam (video); Sensor Logger / phyphox / HyperIMU (IMU) | No coding | Video apps expose no per-frame capture timestamps. Two apps means two unrelated clocks with no common reference, so Gate W1 fails. Most IMU apps record locally rather than stream. | Rejected. Useful as a cross-check of sensor rates. |
| **Browser web client** (chosen) | Android and iOS, zero install, one phone clock for all streams, all code in this repo | IMU rate capped by the browser (often ~60 Hz). JPEG encoding uses phone CPU. The page must stay in the foreground. Needs a secure context (self-signed certificate). Frame timestamp quality varies by browser. | **Chosen.** Rate and timestamp source are logged per session, so the limits are measured, not assumed. |

## Transport

| Choice | Alternatives | Why |
|---|---|---|
| **Wi-Fi + HTTPS (default), USB via `adb reverse` (Android)** | USB only / Wi-Fi only | Wi-Fi works with any phone and mount. USB gives lower, steadier latency, charges the phone, and `http://localhost` counts as a secure context, so there is no certificate warning. Both are supported with no code change. |
| **Video: JPEG per WebSocket message** | RTSP (H.264/RTP), WebRTC | Capture timestamp and frame index travel *inside* each frame message. Drops are explicit (index gaps). Each frame is independently decodable, which gives raw storage and exact replay. RTSP/WebRTC carry RTP timestamps that are hard to map back to the sensor clock, add decoder buffering, and WebRTC silently changes resolution and bitrate under congestion, which hurts reproducibility. Cost: 3–5× the bitrate of H.264. At 720p, 30 fps and quality 0.8, expect about 10–30 Mb/s depending on the scene; H.264 would need about 4–8 Mb/s. That fits 5 GHz Wi-Fi or USB. On congested 2.4 GHz, use 640×480 or 15 fps. |
| **Telemetry: same WebSocket (TCP)** | UDP, raw TCP | Browsers cannot open raw UDP or TCP sockets. TCP gives ordered, lossless delivery with sequence ids preserved. Batching every 40 ms keeps overhead low. Head-of-line blocking under Wi-Fi loss shows up as latency, which is measured, not as data loss. |
| **Backpressure: drop on the phone** | Queue everything | If `ws.bufferedAmount` exceeds 1.5 MB or the encoder is busy, the phone drops the frame and counts it. Latency stays bounded, and every drop is visible as a `frame_index` gap. |

## Consequences, risks and mitigations

* **Frame timestamp quality depends on the browser.** The source is logged per frame (`ts_source`). The residual
  camera–IMU offset `td` is *measured* by `evaluation/sync_check.py` and applied downstream (`align_frames(td_ns=…)`,
  `replay --td-ms`).
* **The IMU rate may be capped near 60 Hz.** That is inside the 50–100 Hz target. If a device delivers less than
  50 Hz, switch to the native Android client.
* **Phone CPU spent on JPEG encoding.** Default 720p at 30 fps, quality 0.8. Drop to 640×480 or quality 0.7 if
  `frames_dropped_busy` grows.
* **Self-signed certificate warning on Wi-Fi.** Accept it once per device, or use USB mode.
* **Screen-off or backgrounded page stops capture.** The page requests a Screen Wake Lock. Gaps are visible in the
  report.

**Upgrade path (Month 2).** A native Android client that implements this protocol unchanged (the 32-byte frame
header and the JSON schema). It adds hardware timestamps, exposure time, rolling-shutter skew and IMU rates of
200 Hz or more.

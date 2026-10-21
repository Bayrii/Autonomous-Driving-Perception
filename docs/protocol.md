# Wire protocol and session format (v1)

Source of truth in code: [pc_app/protocol.py](../pc_app/protocol.py) (PC) and
[phone_client/app.js](../phone_client/app.js) (phone).

## Clocks

| Name | Where | Definition |
|---|---|---|
| phone clock | every `t_*phone*` field, `t` in IMU samples | `performance.now()` of the phone page in **integer ns**. Monotonic, starts at page load. Shared by camera frames, IMU samples and pongs. |
| PC clock | every `t_*pc*` field | `time.perf_counter_ns()` on the PC. Monotonic and system-wide. |
| UTC wall clock | `fix_time_unix_ms` (GNSS), `created_utc` | Not used for alignment. |

`session_id` is generated once per page load, so **one session_id = one phone clock domain**. Reloading the page
starts a new clock domain, and the PC starts a new recording.

## Connection

The phone opens `wss://<pc>:8443/ws/phone` over Wi-Fi, or `ws://localhost:8080/ws/phone` over USB with `adb reverse`.
Everything travels on that single WebSocket.

### Phone → PC, text (JSON)

| `type` | Fields | When |
|---|---|---|
| `hello` | `protocol`, `session_id`, `user_agent`, `platform`, `time_origin_ms`, `cross_origin_isolated`, `imu_api` (`generic_sensor` \| `devicemotion`), `imu_hz_requested`, `frame_timestamp`, `video{requested, jpeg_quality, track_settings, label}`, `orientation_deg`, `screen` | On connect, and again if the IMU API changes |
| `imu` | `session_id`, `samples: [[sensor_type, seq, t_ns, x, y, z], ...]` | Batched every 40 ms |
| `gnss` | `session_id`, `samples: [{seq, t, fix_ms, lat, lon, alt, acc, alt_acc, speed, heading}]` | When a fix arrives (optional) |
| `pong` | `id`, `t` (phone clock read when the ping arrived) | Immediately after each `ping` |
| `status` | cumulative `frames_captured`, `frames_sent`, `frames_dropped_busy`, `frames_dropped_backpressure`, `imu_samples`, plus `ws_buffered_bytes`, `orientation_deg`, `imu_api`, `t_phone_ns` | Every 1 s |

IMU sample schema:

| Field | Meaning |
|---|---|
| `sensor_type` | `gyro` (rad/s) or `accel` (m/s², **including gravity**, i.e. specific force) |
| `seq` | Per-sensor counter. It increments for every sample the phone produced, so a gap means a lost sample. |
| `t_ns` | Phone-clock time of the reading (Generic Sensor `reading.timestamp` or `DeviceMotionEvent.timeStamp`) |
| `x, y, z` | Phone body frame: x right, y toward the top of the screen, z out of the screen. Android and W3C use the same convention. |

### Phone → PC, binary (one video frame)

All fields are little-endian, followed by the JPEG bytes:

| Offset | Type | Field |
|---|---|---|
| 0 | 4 bytes | magic `VFR1` |
| 4 | u32 | `frame_index`. Increments for every frame selected for sending, so gaps = dropped frames. |
| 8 | i64 | `t_capture_ns`, phone clock |
| 16 | i64 | `t_send_ns`, phone clock just before `ws.send` (after JPEG encoding) |
| 24 | u16 | width |
| 26 | u16 | height |
| 28 | u8 | `ts_source`: 1 = camera `captureTime`, 2 = video-frame-callback time, 3 = poll time, 4 = simulated |
| 29 | u8 | screen orientation / 90 |
| 30 | u16 | reserved |

### PC → phone

| `type` | Fields | Purpose |
|---|---|---|
| `welcome` | `protocol_version`, `recording` | Reply to `hello` |
| `ping` | `id` | Clock sync, every 0.5 s. The PC stores its own send time. |
| `state` | `recording`, `rec_id` | Drives the REC indicator on the phone |

## Session directory

One directory per recording: `sessions/<YYYYmmdd-HHMMSS>_<session_id>/`.

| File | Columns / content |
|---|---|
| `manifest.json` | `rec_id`, `session_id`, protocol version, `created_utc`, PC info, **clock basis**, IMU axes and units, the phone `hello` (device, API, camera settings; the simulator adds `sim_truth`), config, `duration_s`, row counts |
| `frames/<frame_index:08d>.jpg` | JPEG bytes exactly as received (not re-encoded) |
| `frames.csv` | `session_id, frame_index, t_capture_phone_ns, t_send_phone_ns, t_recv_pc_ns, width, height, ts_source, orientation_deg, jpeg_bytes, file` |
| `imu.csv` | `session_id, sensor_type, seq, t_phone_ns, t_recv_pc_ns, x, y, z, unit, frame, accuracy` (long format: one row per sample) |
| `gnss.csv` | `session_id, seq, t_phone_ns, t_recv_pc_ns, fix_time_unix_ms, lat_deg, lon_deg, alt_m, horiz_acc_m, vert_acc_m, speed_mps, heading_deg` |
| `clock_sync.csv` | `session_id, ping_id, t_pc_send_ns, t_phone_ns, t_pc_recv_ns, rtt_ns, offset_ns` (offset = phone − PC) |
| `display.csv` | `session_id, frame_index, t_recv_pc_ns, t_display_pc_ns`. Live viewer only. |
| `status.csv` | Phone counters, one row per second |
| `events.csv` | `t_pc_ns, event, detail`: `record_start`, `record_stop`, `disconnect`, `reconnect` |
| `report/` | Written by `evaluation.*`: `report.md`, `metrics.json`, PNG plots, `sync_check.json`, `imu_yaw.json` |
| `calibration.json` | Optional, written by `python -m pc_app calibrate` |

To convert phone time to PC time, fit the clock model with
`pc_app.session.Session(path).clock_model.phone_to_pc(t)`; see
[pc_app/clock.py](../pc_app/clock.py).

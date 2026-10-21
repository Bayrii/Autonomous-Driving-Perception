# Synchronisation and calibration (Day 4)

## 1. Clock domains

| Clock | Stamps | Properties |
|---|---|---|
| Phone `performance.now()` | Camera frames, IMU samples, pongs, phone status | Monotonic, starts at page load, 5 µs resolution when cross-origin isolated. One per `session_id`. |
| Phone camera pipeline | (Hidden.) Exposure start and readout | Browsers expose only `captureTime`, or the time the frame reached the page. Exposure time and rolling-shutter skew are not exposed. |
| Phone sensor hub | (Hidden.) IMU hardware time | Generic Sensor `timestamp` follows it closely. `devicemotion` uses the event dispatch time, which adds a few ms of jitter. |
| PC `perf_counter_ns()` | Receive time, display time, ping send/receive | Monotonic and system-wide. `time.monotonic` is avoided because it ticks at 15.6 ms on Windows. |
| UTC | GNSS fix time | Wall clock; can jump (NTP). Not used for alignment. |

**Key design point.** Frames and IMU share the phone clock, so **frame↔IMU pairing never involves the network**.
The phone↔PC mapping is needed only for latency metrics and for PC-side events.

### Phone ↔ PC offset and drift

The PC sends `ping` at `t0`. The phone answers with its clock reading `tp`, and the PC receives the answer at `t3`.

```
offset = tp − (t0 + t3)/2           (phone − PC),   |error| ≤ RTT/2,   RTT = t3 − t0
```

Queueing only ever *adds* delay, so the lowest-RTT samples are the most trustworthy. The model keeps the
minimum-RTT ping per 2 s bin and fits `offset(t) = a + b·t`, where `b` is the relative drift
([pc_app/clock.py](../../pc_app/clock.py)). The remaining error is the unknown asymmetry of the fastest ping,
bounded by `RTT_min/2` and reported as `clock_offset_uncertainty_ms`.

* Drift matters for long sessions: 50 ppm × 10 min = 30 ms. A constant offset is not enough.
* Drift is only observable over long sessions. With ±0.3 ms per point, a 30 s session pins drift to about
  ±10 ppm; a 2 min session to about ±1–3 ppm.
* Validation on the simulator (two 30 s reference sessions), against injected truth: offset error **0.19 / 0.05 ms**,
  drift error **8.1 / 1.4 ppm**
  ([benchmarks.csv](../../evaluation/benchmarks.csv); test `test_clock_model_recovers_offset_and_drift`).

## 2. Pairing frames with IMU samples

[pc_app/sync.py](../../pc_app/sync.py) `align_frames()` returns, for every frame:
* gyro and accel interpolated at the frame time;
* the distance to the nearest IMU sample;
* the integrated rotation since the previous frame;
* a `valid` flag (IMU covers the frame with no gap over 50 ms).

| Method | Timing error | Value error for ω(t) | 1 rad/s, 2 Hz rotation @100 Hz | @60 Hz |
|---|---|---|---|---|
| Nearest neighbour | ≤ T/2 | ≤ \|ω′\|·T/2 | ≤ 63 mrad/s | ≤ 105 mrad/s |
| Linear interpolation | none (exact at samples) | ≤ \|ω″\|·T²/8 | ≤ 2 mrad/s | ≤ 5.5 mrad/s |

We use **linear interpolation** for instantaneous values. For motion *between* frames we use **integration**:
composing exponential-map increments of the gyro (trapezoid rule). That is what image motion between two frames
depends on. Interpolation never extrapolates past the IMU span, and frames over a gap are flagged invalid rather
than filled.

## 3. Camera–IMU time offset `td`

Even on one clock, the frame timestamp is not the mid-exposure instant. Pipeline latency, exposure, rolling-shutter
readout and the browser's choice of timestamp (`ts_source`) all shift it. We use the Kalibr convention:
`t_imu = t_cam + td`.

**Measuring it.** [evaluation/sync_check.py](../../evaluation/sync_check.py) works as follows:
1. Measure global image motion between consecutive frames with phase correlation.
2. Predict the same motion from the gyro for a distant scene (`u̇ = −f·ω_Cy`, `v̇ = f·ω_Cx`, with
   `ω_C = R_CB (ω_B − b_g)`).
3. Search `td` in ±200 ms for maximum correlation, with parabolic sub-ms refinement.

The script also fits the focal length `f` in pixels, which should match the calibration, and checks the per-axis
signs, which validates `R_CB` for that screen orientation.

* Recording for it: handheld, a textured static scene 2 m or more away, 20–30 s of panning and tilting, with
  2 s of stillness at the start (for the gyro bias). Driving footage does not work: translation and moving objects
  dominate.
* Simulator validation: the injected `td` is −25 ms. The estimate is **−25.3 ms** (portrait, 640×480) and
  **−25.7 ms** (landscape, 1280×720, 3 % frame and 2 % IMU drops), with focal length 501 / 500 px against a true
  500 px. Phase correlation runs on frames downscaled to 640 px wide; at 320 px, fine texture biased `td` by about
  3 ms.
* Applying it: `align_frames(..., td_ns=…)` in code, or `python -m pc_app replay <session> --td-ms <td>`.
* Expected on real phones: `td` of roughly −10 to −60 ms when `ts_source` is the callback time; smaller with
  `captureTime`.

**Exposure time and rolling shutter.** The web API does not expose either. Treat the estimated `td` as
"mid-exposure, mid-readout on average". Row-dependent rolling-shutter error (about 9 px at 0.5 rad/s with 20 ms
readout) stays uncorrected in Month 1. The native Camera2 client (`SENSOR_EXPOSURE_TIME`,
`SENSOR_ROLLING_SHUTTER_SKEW`) is the fix.

## 4. Camera intrinsics (checkerboard)

Model: pinhole `u = fx·X/Z + cx`, `v = fy·Y/Z + cy`, plus OpenCV radial and tangential distortion
`(k1, k2, p1, p2, k3)`.

```
python -m pc_app calibrate --make-board board.png          # 10x7 squares = 9x6 inner corners
# show board.png full-screen on a monitor (or print it); measure one square in mm
# record ~40 s at the SAME resolution/orientation as your road sessions, covering centre, edges, corners, tilts
python -m pc_app calibrate --session sessions/<rec_id> --square-mm <measured> --inner 9x6
```

The output is `calibration.json` in the session directory: K, distortion, RMS and per-view reprojection error,
and field of view.

* **Quality.** RMS below 0.5 px is good; above 1 px, re-capture with sharper frames and more coverage. Compare `fx`
  with `focal_px_est` from `sync_check`; they should agree within a few percent.
* **Validity.** Calibration holds for **one resolution and orientation only**. The browser may crop or scale, so
  calibrate exactly what you stream.
* **Pitfalls.**
  * Autofocus changes focal length slightly. Point at distant objects, as on the road.
  * Electronic image stabilisation warps frames per frame and breaks the pinhole model. Disable it in the camera
    app if the phone exposes that.
  * Motion blur ruins corner detection. Move slowly.
* Tested on synthetic views rendered with a known K: `tests/test_calibrate.py` recovers fx and fy within 15 px
  of 900.

## 5. Extrinsics

* **Camera from body, `R_CB`.** Known per screen orientation, with a lever arm of about 1 cm (negligible). The
  orientation is logged per frame.
* **Vehicle from body, `R_VB`** (mount). Roll and pitch come from mean gravity at standstill (the report prints
  "gravity at rest"). Yaw comes from straight-line acceleration or GNSS course. A level, forward-looking camera
  gives `R_VC = R_VEHICLE_FROM_CAMERA_NOMINAL`.
* **Camera height** above the road: measure it with a tape (in metres). It is needed for the ground-plane
  homography and distance in Weeks 2–3.
* **Calibration metadata to record per session:** phone model (in the manifest `user_agent`), resolution and
  orientation (`frames.csv`), mount description and camera height (add to `manifest.json` → `calibration` or a
  note next to the session), and the `calibration.json` path.

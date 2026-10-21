# Stress test: protocol and results (Day 5)

**Status.** The PC pipeline has been validated end to end with the phone simulator, against injected ground truth
(section 3). **The real-phone runs in section 2 still need to be recorded.** They require the physical phone,
and their table is left empty on purpose rather than filled with guesses.

## 1. Protocol

Every run follows the same steps:

```
python -m pc_app live                     # window opens; phone opens the printed URL and taps Start
# press r to record, r again to stop
python -m evaluation.session_report sessions/<rec_id> --scenario <name>
python -m evaluation.imu_plots sessions/<rec_id>
```

Each report run appends one row to [evaluation/benchmarks.csv](../../evaluation/benchmarks.csv) and writes
`sessions/<rec_id>/report/report.md`.

| # | Scenario | How | Duration | What it validates |
|---|---|---|---|---|
| S1 | **stationary** | Phone flat, face up, on a table, camera on, not touched | 60 s | Gravity sign and magnitude: expect accel ≈ (0, 0, **+9.81**) with the IMU API in use; otherwise note the platform sign convention. Gyro bias and noise. IMU rate and jitter at rest. Baseline latency. |
| S2 | **indoor handheld rotation** | Textured scene 2 m or more away. Hold still 2 s, then pan and tilt by hand. | 30 s | `python -m evaluation.sync_check` gives the camera–IMU `td`, the focal-length sanity check, and the axis-convention check. Use it once in portrait and once in landscape. |
| S3 | **walking / handheld** | Walk around the building with the stream on, including weak-Wi-Fi spots | 2 min | Wi-Fi jitter, drops, reconnects (`events.csv`), backpressure |
| S4 | **road-facing, passenger** | Phone in a rigid windshield mount, landscape, operated **by a passenger** (or recorded only while parked). The driver never touches the phone. | ≥ 2 min | Real road footage for Week 2, vibration, mount orientation (gravity at rest = mount roll and pitch), and **the 2-minute replay deliverable** |
| S5 | **USB vs Wi-Fi** (Android) | Repeat S1 with `adb reverse tcp:8080 tcp:8080` and `http://localhost:8080` | 60 s | Transport comparison: latency p95, drops |

Metric definitions (all in [evaluation/metrics.py](../../evaluation/metrics.py)):

| Metric | Definition |
|---|---|
| achieved FPS | (frames − 1) / capture-time span (phone clock) |
| jitter | Standard deviation of capture intervals, excluding gaps (gaps are counted separately) |
| dropped frames | Missing `frame_index` values (PC view), plus the phone's busy and backpressure counters |
| IMU Hz | Samples / timestamp span, per sensor |
| data gaps | IMU intervals > 2× median, and missing `seq` |
| latency | (PC receive time or PC display time) − capture time mapped to the PC clock (clock model ± RTT_min/2), as p50/p95/p99 |
| Gate W1 | Every frame has a positive, monotonic capture time, **and** ≥ 99 % of frames inside the IMU span have valid IMU alignment |

## 2. Real-phone results (to be recorded)

| Scenario | Phone / browser / IMU API | Link | Res | FPS | Jitter ms | Missing / phone-dropped | Gyro Hz | Accel Hz | IMU gaps | Cap→PC p50/p95 ms | Cap→display p50/p95 ms | `td` ms | Gate W1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 stationary | | Wi-Fi | | | | | | | | | | – | |
| S2 rotation (portrait) | | Wi-Fi | | | | | | | | | | | |
| S2 rotation (landscape) | | Wi-Fi | | | | | | | | | | | |
| S3 walking | | Wi-Fi | | | | | | | | | | – | |
| S4 road (passenger) | | Wi-Fi | | | | | | | | | | – | |
| S5 stationary | | USB | | | | | | | | | | – | |

Copy the values from each session's `report.md`, or straight from `evaluation/benchmarks.csv`.

## 3. Pipeline validation with the simulator (done)

Commands: `python tools/sim_demo.py --duration 30 --seed 1` and
`python tools/sim_demo.py --duration 30 --seed 2 --orientation 90 --frame-drop 0.03 --imu-drop 0.02 --width 1280 --height 720`.
The simulator runs on the same PC, so latency here is the PC pipeline's own cost: JPEG encoding plus localhost
transport.

| | Sim A: 640×480 portrait, clean | Sim B: 1280×720 landscape, 3 % frame and 2 % IMU drops |
|---|---|---|
| achieved FPS | 30.00 | 29.13 (drops) |
| frames / missing index / phone-reported drops | 901 / 0 / 0 | 874 / 26 / 25 (last second not yet reported) |
| gyro Hz / jitter ms / missing seq | 100.0 / 0.43 / 0 | 98.3 / 0.97 / 51 (accel 68); about 60 injected per sensor |
| cap→PC latency p50 / p95 ms | 11.6 / 18.9 | 17.6 / 27.9 |
| clock RTT min ms | 0.54 | 0.55 |
| clock offset error vs truth | 0.19 ms | 0.05 ms |
| `td` estimate vs truth (−25 ms) | −25.3 ms | −25.7 ms |
| focal estimate vs truth (500 px) | 501 px | 500 px |
| frames with valid IMU alignment | 100 % | 100 % |
| Gate W1 | PASS | PASS |

Separately, the live OpenCV viewer was run against the simulator. It measured capture→display latency of 32 ms at
p50 and 42 ms at p95 (versus 10 ms at p50 for capture→PC). Rendering and the 30 Hz display loop add about 20 ms.

**Conclusion so far:**
* Every injected loss is visible on the PC (index and sequence gaps).
* The clock model recovers the injected offset to under 0.3 ms.
* The data-driven `td` estimate is within 1 ms of truth.
* Replay is byte-identical across runs (`replay --export` hashes match).

What the simulator **cannot** tell us, and S1–S5 must measure:
* real browser IMU rate caps;
* frame timestamp quality (`ts_source`);
* phone JPEG encode time at 720p;
* Wi-Fi jitter;
* thermal throttling over long sessions;
* the iOS / Android accelerometer sign convention.

## 4. Known limitations to watch in the real runs

* **Browser IMU rate cap** (often about 60 Hz). This is acceptable for the 50–100 Hz target. If the measured rate
  falls below 50 Hz, the native Android client becomes a priority.
* **`devicemotion` fallback.** Timestamps are event dispatch times, adding a few ms of jitter. Check `imu_api` in
  the manifest.
* **No exposure time or rolling-shutter skew from the web API.** Only the average `td` is corrected.
* **Screen off or page backgrounded.** Capture stops; this appears as gaps and in `events.csv`.
* **Thermal throttling** on long 720p sessions. Watch FPS and `frames_dropped_busy` over time.

# Smartphone-Based Autonomous Driving Perception & Sensor Fusion

This is a research prototype. A smartphone streams camera frames, gyroscope and accelerometer data (and,
optionally, GNSS) to a PC. The PC timestamps, records and replays the stream, and will later run all perception
models. **Week 1 is done:** acquisition, streaming, synchronisation, calibration tooling, and stress-test tooling.

> **Safety.** This is for passenger-only or offline/recorded data collection. Never operate the phone while
> driving. Nothing here controls a vehicle or is roadworthy.

## Two ways to collect data

| | Cloud recorder ([cloud_app/](cloud_app/README.md)) | Live PC streaming (`pc_app`) |
|---|---|---|
| Network | Phone and PC on **any** networks; phone uploads to Render over HTTPS | Phone must reach the PC: same Wi-Fi, USB or a tunnel |
| Live view on the PC | No; download the ZIP after stopping | Yes, with latency measurement |
| Rates | 5 fps JPEG, browser motion events (deg/s, nulls kept) | up to 30 fps, IMU 60–100 Hz |
| Use when | Office/corporate networks, mobile data | You control the network |

For the cloud recorder (deploy, use, data format, phone checklist) see **[cloud_app/README.md](cloud_app/README.md)**.

## Quick start (live PC streaming)

```bash
pip install -r requirements.txt          # Python >= 3.11
python -m pc_app live                     # starts the server + viewer, prints the phone URL
```

**Connect the phone.** Choose one of two ways:

* **Wi-Fi (Android or iPhone).** Put the phone on the same network and open the printed
  `https://<pc-ip>:8443/` URL.
  * Accept the self-signed certificate warning once.
  * Allow Python through the Windows firewall when prompted.
* **USB (Android).** Run `adb reverse tcp:8080 tcp:8080`, then open `http://localhost:8080/` on the phone. There
  is no certificate warning, and latency is lower.

Until the phone connects, the viewer window (and the terminal) show a **QR code** of the address. Scan it with the
phone camera instead of typing it.

**Phone cannot open the page?** Windows Firewall blocks Python on "Public" networks, which includes most office
Wi-Fi. In an **administrator** PowerShell, run:

```powershell
# remove Python's inbound block rules (block rules beat allow rules), then allow only the stream port
Get-NetFirewallApplicationFilter | Where-Object Program -like "*\python.exe" | Get-NetFirewallRule |
    Where-Object { $_.Direction -eq "Inbound" -and $_.Action -eq "Block" } | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Phone streamer 8443" -Direction Inbound -Protocol TCP -LocalPort 8443 `
    -RemoteAddress LocalSubnet -Action Allow -Profile Any
```

If it still times out, the Wi-Fi isolates devices from each other (common on corporate and guest networks). Use the
next section.

### No shared Wi-Fi: public HTTPS link via VS Code port forwarding

The phone can be on **any** network, including mobile data. VS Code (already installed) relays port 8080 through
Microsoft dev tunnels over outgoing HTTPS (port 443). Nothing is installed, and the firewall is not involved.

1. Start the PC side: `python -m pc_app live`
2. In VS Code, open the **Ports** tab next to Terminal (or `Ctrl+Shift+P` → *Ports: Focus on Ports View*).
   * Click **Forward a Port** and type `8080`.
   * Sign in with GitHub or Microsoft when asked (once).
3. Right-click the new row → **Port Visibility** → **Public**. Copy the **Forwarded Address**, e.g.
   `https://abc123x-8080.euw.devtunnels.ms/`.
4. Create `configs/local.toml` (it stays out of git):
   ```toml
   [tunnel]
   public_url = "https://abc123x-8080.euw.devtunnels.ms/"
   ```
5. Restart `python -m pc_app live`. The window now shows a QR code for
   `https://abc123x-8080.euw.devtunnels.ms/?k=<key>`.
   * The `k` access key is required, because the address is public.
   * The key is saved in `certs/access_key.txt`, so the link stays the same. Bookmark it on the phone.
6. On the phone (Chrome), scan the QR code. Dev tunnels show a one-time **"You are about to connect to a developer
   tunnel"** page: tap **Continue**. Then tap **Start streaming** and allow camera and motion.

Over a tunnel, the page defaults to 640×480 at 15 fps, quality 0.7 (about 2–4 Mb/s; `[tunnel.client]` in the config).
The capture timestamps and IMU alignment are unaffected by the extra internet delay; only the latency numbers grow.

* If VS Code reports the forward failing, forward `127.0.0.1:8080` instead of `8080`.
* If your organisation blocks dev tunnels, use USB (above).
* `python -m pc_app live --tunnel` is a zero-setup alternative (SSH to localhost.run), but measured at only about
  0.3 Mb/s: fine for IMU, about 1 fps of video.
* Privacy: all tunnels decrypt traffic at the provider, so camera frames pass through Microsoft's (or
  localhost.run's) relay.

Then tap **Start streaming** on the phone and allow camera and motion access (and location, if GNSS is enabled).
Recording is controlled in the PC viewer: press **r** to start or stop, **q** to quit. Sessions are saved to
`sessions/<date-time>_<session_id>/`.

```bash
python -m pc_app replay sessions/<rec_id>                 # deterministic replay (space pause, n step, q quit)
python -m pc_app replay sessions/<rec_id> --export out.mp4 --headless
python -m evaluation.session_report sessions/<rec_id> --scenario stationary   # report + benchmark CSV row
python -m evaluation.sync_check sessions/<rec_id>        # camera<->IMU time offset from data
python -m evaluation.imu_plots sessions/<rec_id>         # gyro/accel/yaw exercise plot
python -m pc_app calibrate --make-board board.png        # then: --session sessions/<rec_id> --square-mm 25
```

**No phone at hand?** `python tools/sim_demo.py --duration 20` runs the whole pipeline (server, simulated phone,
recording, report) against known ground truth. `python -m pytest` runs the tests (22 tests, about 20 s).

## How it works

```
 PHONE (browser page, phone_client/)          PC (pc_app/)                                   OFFLINE (evaluation/)
 camera  --rVFC captureTime--+                 aiohttp server --> LiveState --> OpenCV viewer  session_report -> report.md, plots,
 gyro    --sensor timestamp--+-> 1 WebSocket ->   |  ping/pong clock sync                         benchmarks.csv
 accel   --sensor timestamp--+   JPEG+32B hdr     +--> Recorder (thread) --> sessions/<id>/   sync_check -> camera-IMU td
 GNSS    --(optional)--------+   JSON IMU batches                          Session --> replay   imu_plots  -> yaw / stationary
```

* **One phone clock.** Frames and IMU samples are stamped on the phone's monotonic `performance.now()`, so
  aligning them does not depend on network delay. The PC↔phone offset and drift come from ping/pong
  ([pc_app/clock.py](pc_app/clock.py)) and are used only for latency metrics.
* **Nothing is hidden.**
  * Every frame carries an index, so gaps show drops.
  * Every IMU sample carries a per-sensor sequence id.
  * The frame timestamp source is logged per frame.
  * The camera–IMU offset is estimated from data, not assumed.
* **Raw and replayable.** JPEGs are stored exactly as received. Replay order comes only from recorded data, and
  two exports of the same session are byte-identical.

## Repository layout

| Path | Contents |
|---|---|
| [phone_client/](phone_client/) | Acquisition web page (camera, IMU, GNSS, clock-sync replies, backpressure) |
| [pc_app/](pc_app/) | `server` (ingest), `recorder`, `session` (reader API for later weeks), `clock`, `sync` (frame↔IMU alignment, frames), `viewer`/`live`/`replay`, `calibrate` |
| [evaluation/](evaluation/) | `metrics`, `session_report`, `sync_check`, `imu_plots`, [benchmarks.csv](evaluation/benchmarks.csv) |
| [tools/](tools/) | `fake_phone.py` (simulated phone with ground truth), `sim_demo.py` |
| [configs/default.toml](configs/default.toml) | Ports, stream defaults, viewer settings |
| [tests/](tests/) | Unit and end-to-end tests |
| [docs/](docs/) | [protocol and session format](docs/protocol.md), Week 1 docs below |

The `perception/`, `localization/` and `fusion/` packages from the plan start in Weeks 2–3. They will consume
`pc_app.session.Session` and `pc_app.sync.align_frames`.

## Week 1 deliverables

| Plan item | Where |
|---|---|
| Day 1: sensor/API survey, transport comparison, chosen stack | [docs/week1/research_notes.md](docs/week1/research_notes.md) §1, [docs/week1/design_decision.md](docs/week1/design_decision.md) (one-page note) |
| Day 2: telemetry schema, PC receiver, live plots, drop counters, CSV logging | [docs/protocol.md](docs/protocol.md), `pc_app/server.py`, `pc_app/live_state.py`, viewer IMU plots, `imu.csv` |
| Day 3: video with frame index and capture time, latency, viewer with status and recording | `phone_client/app.js`, frame header, `pc_app/live.py` (capture→PC and capture→display p50/p95 on screen) |
| Day 4: clock domains, offset/drift, frame↔IMU pairing and error, intrinsics/extrinsics, checkerboard | [docs/week1/sync_and_calibration.md](docs/week1/sync_and_calibration.md), `pc_app/clock.py`, `pc_app/sync.py`, `evaluation/sync_check.py`, `pc_app/calibrate.py` |
| Day 5: stress test (FPS, IMU Hz, latency percentiles, jitter, drops, gaps), replay | [docs/week1/stress_test.md](docs/week1/stress_test.md), `evaluation/session_report.py`, `python -m pc_app replay` |
| Research questions + addendum A (frames, sensor physics, bias exercise, yaw plot) | [docs/week1/research_notes.md](docs/week1/research_notes.md) §2–3, `evaluation/imu_plots.py` |
| **Still to do with the physical phone** | Record S1–S5 from the stress-test doc, including the 2-minute road-facing (passenger) replay; run the checkerboard calibration; fill the results table |

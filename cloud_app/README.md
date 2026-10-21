# Phone camera + IMU recorder (Render)

The phone opens the deployed **HTTPS** site and records camera frames plus the motion sensors its browser exposes.
It uploads them with `fetch()` POST requests to the FastAPI backend on the same origin. The phone and your PC can
be on **any networks**. Recordings are stored **on the Render server**; your PC downloads finished recordings as
ZIP files from the same site.

```
phone (any network) --HTTPS POST--> Render: FastAPI + temporary disk <--HTTPS GET (ZIP)-- your PC
```

> **Storage is temporary.** Render's free web services have an ephemeral filesystem. Every restart, redeploy or
> idle spin-down deletes all recordings. Download each recording right after you stop it. This is not durable
> cloud storage.

## 1. Run locally

```bash
pip install -r requirements.txt     # from the repository root: includes FastAPI/uvicorn and OpenCV
cd cloud_app
# PowerShell: $env:APP_ACCESS_TOKEN="dev-secret"   bash: export APP_ACCESS_TOKEN=dev-secret
uvicorn main:app --host 127.0.0.1 --port 8000
```

`cloud_app/requirements.txt` is what Render installs. On a PC that already has `opencv-python`, do not also install
`opencv-python-headless`, because both provide `cv2` and conflict.

Open http://127.0.0.1:8000 on the PC; `localhost` counts as a secure context, so the camera works. A phone needs
the HTTPS deployment, because browsers block the camera and motion sensors on plain `http://` addresses other than
localhost.

Tests (from the repository root): `python -m pytest tests/test_cloud_api.py`.

## 2. Deploy on Render (free)

1. Put the repository on GitHub (Render deploys from a Git repository):
   ```bash
   git add -A && git commit -m "Phone recorder for Render"
   git remote add origin https://github.com/<you>/<repo>.git && git push -u origin master
   ```
2. In the Render dashboard, choose **New → Blueprint**, pick the repository, and Render reads
   [`render.yaml`](../render.yaml). When asked for **APP_ACCESS_TOKEN**, enter a long random secret, e.g. the
   output of `python -c "import secrets; print(secrets.token_urlsafe(24))"`.

   Without Blueprints, use **New → Web Service** with these settings:

   | Setting | Value |
   |---|---|
   | Root Directory | `cloud_app` |
   | Runtime | Python 3 |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1` |
   | Instance Type | Free |
   | Health Check Path | `/health` |
   | Environment | `APP_ACCESS_TOKEN` = your secret · `PYTHON_VERSION` = `3.12.11` · `DATA_DIR` = `/tmp/phone-recordings` |

3. Wait for "Live", then check `https://<name>.onrender.com/health` → `{"status":"ok"}`.
4. On the phone, open `https://<name>.onrender.com/`, enter the token, tap **Enable camera & sensors**, then
   **Start recording**.
5. On the PC, open the same address, enter the token, tap **Refresh list** → **Download**. Or run:
   `python tools/download_recordings.py https://<name>.onrender.com` (reads `APP_ACCESS_TOKEN` or asks for it).

**Free-tier behaviour**
* **Cold starts:** the service sleeps after about 15 minutes without requests. The first request then takes up to
  about a minute; the page shows "Server is waking up" and retries.
* **Small instance:** about 512 MB RAM and a fraction of a CPU. That is why the defaults are 640×480 at 5 fps,
  JPEG quality 0.7, and the server validates JPEGs with a cheap header parse plus a reduced-size OpenCV decode.
* **Single worker:** session state is in one process (`--workers 1`). Do not scale it to several instances.
* **Ephemeral disk:** see the warning above. Sessions are also deleted automatically 6 hours after they end
  (`SESSION_TTL_S`), and uploads are refused once `MAX_TOTAL_BYTES` (1.5 GB) is used.

## 3. Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `APP_ACCESS_TOKEN` | *(none)* | Required secret. If unset, recording endpoints answer 503. |
| `DATA_DIR` | `<tmp>/phone-recordings` | Where sessions are written |
| `MAX_SESSION_SECONDS` | 1800 | Recording length limit |
| `MAX_SESSION_BYTES` | 400 MB | Per recording |
| `MAX_TOTAL_BYTES` | 1.5 GB | All recordings; new data is refused with 507 beyond this |
| `MAX_ACTIVE_SESSIONS` | 3 | Concurrent recordings |
| `SESSION_IDLE_TIMEOUT_S` | 600 | An active recording with no uploads for this long is finalized as `interrupted` |
| `SESSION_TTL_S` | 21600 | Finished recordings are deleted this long after they end |
| `MAX_FRAME_BYTES` | 1.5 MB | One JPEG |
| `APP_AUTH_DISABLED` | off | `1` disables the token check. **Local development only.** |

The token travels only in the `Authorization: Bearer …` header. It never appears in a URL, the JavaScript, or the
repository. The page keeps it in memory, so reloading the page forgets it. `/health` is public and returns only
`{"status":"ok"}`.

## 4. API

| Method and path | Purpose |
|---|---|
| `GET /health` | Liveness, public |
| `GET /api/config` | Capture defaults and limits, public |
| `GET /api/auth/check` | Token check |
| `POST /api/sessions` | Create a session (JSON: client, requested capture, camera, sensor availability) → `201 {session_id}`. The server generates a 32-hex id. |
| `POST /api/sessions/{id}/frames?seq=&t_capture_mono_ms=&t_encoded_mono_ms=&width=&height=[&rvfc_capture_time_ms=&rvfc_media_time_s=&attempt=]` | Body = raw JPEG (`Content-Type: image/jpeg`). A repeated `seq` is acknowledged as `duplicate` and not stored twice. |
| `POST /api/sessions/{id}/imu` | JSON `{batch_seq, samples:[…]}`, up to 2000 samples. A repeated `batch_seq` is a `duplicate`. |
| `POST /api/sessions/{id}/stop` | JSON with client counters, observed rates and events. Finalizes the session. |
| `GET /api/sessions` / `GET /api/sessions/{id}` | List / one summary |
| `GET /api/sessions/{id}/download` | ZIP of a finished session, streamed from a temporary file that is deleted afterwards |
| `DELETE /api/sessions/{id}` | Delete a finished session |

Errors are JSON `{"detail": "...", "code": "..."}`:

| Status | Meaning |
|---|---|
| 401 | Wrong or missing token |
| 404 | Unknown session; also what you get after a server restart |
| 409 | The session is not active (stopped, time limit, inactivity) |
| 413 | Too large |
| 415 | Wrong content type |
| 422 | Invalid data |
| 429 | Too many active sessions |
| 503 | No token configured |
| 507 | Storage limit |

## 5. What a recording contains

Each ZIP holds a folder `<session_id>/`:

| File | Contents |
|---|---|
| `metadata.json` | Requested vs actual capture settings (camera track settings, encoded sizes); sensor availability and permission results; time-basis definitions; units; limitations; server counters (accepted / duplicate / rejected); client counters (captured / dropped / failed / not uploaded at stop); observed rates; page events (hidden/visible, offline, camera ended). |
| `frames/<seq>.jpg` | JPEG frames, named by sequence number |
| `frames.csv` | `session_id, seq, file, width, height, bytes, t_capture_mono_ms, t_encoded_mono_ms, rvfc_capture_time_ms, rvfc_media_time_s, client_attempt, server_receipt_unix_ms` |
| `imu.jsonl` | One sample per line: `session_id, batch_seq, seq, kind (motion/orientation), t_mono_ms, event_ts_ms, interval, acceleration{x,y,z}, accelerationIncludingGravity{x,y,z}, rotationRate{alpha,beta,gamma}, orientation{alpha,beta,gamma,absolute}, server_receipt_unix_ms` |

**Time basis.** All phone-side times are `performance.now()` in milliseconds, one monotonic clock for frames and
IMU. `metadata.client.time_origin_unix_ms` maps it to wall-clock time.

* `t_capture_mono_ms` is taken just before the frame is drawn. It is a capture-time *estimate*; the exposure
  happened earlier by an unknown pipeline delay.
* `rvfc_capture_time_ms` is a better hint where the browser provides it.
* `event_ts_ms` is the browser's event dispatch time, not a hardware IMU timestamp.
* `server_receipt_unix_ms` is for diagnostics only. **Never** align camera and IMU with it.

**Units.**
* Acceleration is in m/s².
* Rotation rate is in **degrees per second**, as the browser reports it; it is not converted to rad/s.
* Orientation is in degrees.
* A missing value is `null`, never 0.

## 6. Manual test checklist (real phones)

Automated tests cover the server and a desktop browser with a fake camera. They cannot prove real camera or IMU
behaviour, so do these on the phones.

**Android (Chrome)**
- [ ] Open the `https://…onrender.com` address over **mobile data**, with the PC on another network.
- [ ] Wrong token → "Access token rejected"; correct token → "accepted".
- [ ] Tap **Enable**: the camera prompt appears; the rear camera previews; the actual size shows under "Camera".
- [ ] Sensor table shows changing acceleration and rotation-rate values when the phone moves. Motion rate is
      usually 50–200 /s.
- [ ] Record 30 s: frame rate about 5 /s; queues stay near 0; captured ≈ accepted.
- [ ] Deny the camera (site settings), reload, enable → a clear "Camera permission denied" message.
- [ ] Airplane mode for 5 s during recording → retries; then drops are counted; after reconnecting, uploads
      resume; Stop reports what was not uploaded.
- [ ] Switch apps for 5 s during recording → on return, the warning appears, `page_hidden` events appear in
      `metadata.json`, and a gap shows in `frames.csv`.
- [ ] Stop → summary message → **Download recording** saves the ZIP; on the PC, the list shows it and the
      download matches.

**iPhone (Safari)**
- [ ] Tap **Enable**: iOS asks for **Motion & Orientation** access first, then the camera. Allow both.
- [ ] Deny motion access → the note says to reload and allow; camera-only recording still works.
- [ ] Rear camera preview plays inline (not full screen).
- [ ] Check `accelerationIncludingGravity` signs while the phone lies flat, face up; iOS has historically used
      the opposite sign to Android. The value is recorded as delivered, never altered.
- [ ] Lock the screen during recording → the camera stops; on return a warning appears. Expected: phones do
      not record in the background.
- [ ] Download works (Safari shows the download or share sheet).

**Server behaviour**
- [ ] Leave the service idle for more than 15 minutes, open the page → "Server is waking up", then it works.
- [ ] Redeploy during a recording → uploads get 404 → the page says the server no longer has the recording.

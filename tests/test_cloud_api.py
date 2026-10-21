"""Integration tests of the Render recorder backend (cloud_app/). They exercise the HTTP API with
real JPEGs and IMU batches; they do not (and cannot) prove phone camera or sensor behaviour."""
import csv
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cloud_app"))
from phonerec.api import create_app  # noqa: E402
from phonerec.settings import Settings  # noqa: E402

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_jpeg(w=640, h=480, seed=0) -> bytes:
    rng = np.random.default_rng(seed)
    img = cv2.GaussianBlur(rng.integers(0, 255, (h, w, 3), np.uint8), (0, 0), 2)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()


def frame_params(seq, w=640, h=480, **kw):
    p = {"seq": seq, "t_capture_mono_ms": 1000.0 + 200 * seq, "t_encoded_mono_ms": 1012.5 + 200 * seq,
         "width": w, "height": h}
    p.update(kw)
    return p


def motion(seq, t, with_gyro=True):
    return {"seq": seq, "kind": "motion", "t_mono_ms": t, "event_ts_ms": t - 0.4, "interval": 16.7,
            "acceleration": {"x": 0.1, "y": -0.2, "z": 0.05},
            "accelerationIncludingGravity": {"x": 0.3, "y": 9.7, "z": 0.4},
            "rotationRate": {"alpha": 12.5, "beta": -3.0, "gamma": 0.7} if with_gyro else None}


def orientation(seq, t):
    return {"seq": seq, "kind": "orientation", "t_mono_ms": t, "event_ts_ms": None,
            "orientation": {"alpha": 180.0, "beta": 45.0, "gamma": None, "absolute": False}}


@pytest.fixture
def settings(tmp_path):
    return Settings(access_token=TOKEN, data_dir=tmp_path / "data", cleanup_interval_s=3600)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def new_session(client) -> str:
    r = client.post("/api/sessions", headers=AUTH, json={
        "client": {"user_agent": "pytest", "time_origin_unix_ms": 1.7e12},
        "requested": {"width": 640, "height": 480, "fps": 5, "jpeg_quality": 0.7},
        "camera": {"available": True, "track_settings": {"width": 640, "height": 480, "frameRate": 30}},
        "sensors": {"device_motion_api": True, "motion_events_seen": True, "rotation_rate_available": False}})
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    assert len(sid) == 32 and all(ch in "0123456789abcdef" for ch in sid)
    return sid


def put_frame(client, sid, seq, jpeg=None, **kw):
    return client.post(f"/api/sessions/{sid}/frames", params=frame_params(seq, **kw), content=jpeg or make_jpeg(seed=seq),
                       headers={**AUTH, "Content-Type": "image/jpeg"})


# --------------------------------------------------------------------------- public endpoints
def test_health_and_frontend(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200 and "Phone Sensor Recorder" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "Authorization" in js.text
    assert "localhost" not in js.text and "http://" not in js.text      # relative, same-origin API only
    assert client.get("/api/config").json()["auth_required"] is True


# --------------------------------------------------------------------------- auth
@pytest.mark.parametrize("method,path", [
    ("post", "/api/sessions"), ("get", "/api/sessions"), ("get", "/api/auth/check"),
    ("post", "/api/sessions/" + "a" * 32 + "/frames"), ("post", "/api/sessions/" + "a" * 32 + "/imu"),
    ("post", "/api/sessions/" + "a" * 32 + "/stop"), ("get", "/api/sessions/" + "a" * 32 + "/download"),
    ("delete", "/api/sessions/" + "a" * 32)])
def test_protected_endpoints_reject_missing_or_wrong_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    # a token in the URL is never accepted
    assert getattr(client, method)(path + "?token=" + TOKEN).status_code == 401


def test_server_without_token_refuses(tmp_path):
    with TestClient(create_app(Settings(access_token="", data_dir=tmp_path))) as c:
        r = c.post("/api/sessions", json={}, headers={"Authorization": "Bearer anything"})
        assert r.status_code == 503 and "APP_ACCESS_TOKEN" in r.json()["detail"]
        assert c.get("/health").status_code == 200


# --------------------------------------------------------------------------- full recording flow
def test_full_recording_flow_with_retries(client, settings):
    sid = new_session(client)
    for seq in range(3):
        r = put_frame(client, sid, seq)
        assert r.status_code == 200 and r.json()["status"] == "stored"
    dup = put_frame(client, sid, 1, attempt=2)                        # retried upload
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"

    batch = {"batch_seq": 0, "samples": [motion(0, 1000.0), orientation(1, 1001.0), motion(2, 1016.7, with_gyro=False)]}
    r = client.post(f"/api/sessions/{sid}/imu", headers=AUTH, json=batch)
    assert r.json() == {"status": "stored", "batch_seq": 0, "samples_stored": 3}
    r = client.post(f"/api/sessions/{sid}/imu", headers=AUTH, json=batch)     # retried batch
    assert r.json()["status"] == "duplicate" and r.json()["samples_stored"] == 0
    client.post(f"/api/sessions/{sid}/imu", headers=AUTH, json={"batch_seq": 1, "samples": [motion(3, 1033.4)]})

    assert client.get(f"/api/sessions/{sid}/download", headers=AUTH).status_code == 409   # still active
    stop = client.post(f"/api/sessions/{sid}/stop", headers=AUTH, json={
        "reason": "user", "counters": {"frames_captured": 4, "frames_accepted": 3, "frames_dropped_busy": 1},
        "observed": {"frame_fps": 4.9, "motion_hz": 60.0, "encoded_width": 640, "encoded_height": 480},
        "events": [{"t_mono_ms": 1500.0, "type": "page_hidden"}]})
    assert stop.status_code == 200, stop.text
    s = stop.json()
    assert s["status"] == "completed" and s["frames"] == 3 and s["imu_samples"] == 4
    assert s["server_counters"]["frames_duplicate"] == 1 and s["server_counters"]["imu_batches_duplicate"] == 1
    assert put_frame(client, sid, 9).status_code == 409                 # no uploads after stop

    r = client.get(f"/api/sessions/{sid}/download", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {f"{sid}/metadata.json", f"{sid}/frames.csv", f"{sid}/imu.jsonl"} <= names
    jpgs = sorted(n for n in names if n.endswith(".jpg"))
    assert jpgs == [f"{sid}/frames/{i:06d}.jpg" for i in range(3)]
    rows = list(csv.DictReader(io.StringIO(zf.read(f"{sid}/frames.csv").decode())))
    assert [int(r["seq"]) for r in rows] == [0, 1, 2]                  # duplicate not recorded twice
    assert all(r["session_id"] == sid and r["width"] == "640" and float(r["server_receipt_unix_ms"]) > 1e12
               for r in rows)
    assert all(r["file"] in {n.split("/", 1)[1] for n in jpgs} for r in rows)
    imu = [json.loads(line) for line in zf.read(f"{sid}/imu.jsonl").decode().splitlines()]
    assert [x["seq"] for x in imu] == [0, 1, 2, 3] and {x["session_id"] for x in imu} == {sid}
    assert imu[2]["rotationRate"] is None and imu[1]["orientation"]["gamma"] is None   # nulls preserved
    assert imu[1]["event_ts_ms"] is None and imu[0]["rotationRate"]["alpha"] == 12.5   # deg/s, unconverted
    meta = json.loads(zf.read(f"{sid}/metadata.json"))
    assert meta["session_id"] == sid and meta["status"] == "completed"
    assert "deg/s" in meta["units"]["rotationRate"] and "ESTIMATE" in meta["time_basis"]["t_capture_mono_ms"]
    assert meta["server_counters"]["frames_accepted"] == 3 and meta["client_counters"]["frames_dropped_busy"] == 1
    assert meta["frame_dimensions_actual"] == {"640x480": 3}
    assert meta["sensor_availability"]["rotation_rate_available"] is False
    assert list((settings.data_dir / "_exports").glob("*.zip")) == []          # temporary archive cleaned up

    assert client.delete(f"/api/sessions/{sid}", headers=AUTH).status_code == 200
    assert client.get(f"/api/sessions/{sid}", headers=AUTH).status_code == 404


# --------------------------------------------------------------------------- invalid payloads
def test_invalid_frames_rejected(client, settings):
    sid = new_session(client)
    jpg = make_jpeg()
    bad = [
        (dict(headers={**AUTH, "Content-Type": "application/octet-stream"}), 415),
        (dict(content=b"\x89PNG not a jpeg" * 20), 422),
        (dict(content=jpg[: len(jpg) // 2]), 422),                                  # truncated
        (dict(params=frame_params(0, w=320, h=240)), 422),                          # metadata/JPEG mismatch
        (dict(content=b"\xff\xd8" + bytes(settings.max_frame_bytes) + b"\xff\xd9"), 413),
        (dict(params={k: v for k, v in frame_params(0).items() if k != "seq"}), 422),
        (dict(params={**frame_params(0), "unexpected": 1}), 422),
        (dict(content=make_jpeg(8, 8)), 422),                                       # too small
    ]
    for kw, status in bad:
        args = dict(params=frame_params(0), content=jpg, headers={**AUTH, "Content-Type": "image/jpeg"})
        args.update(kw)
        r = client.post(f"/api/sessions/{sid}/frames", **args)
        assert r.status_code == status, (kw.keys(), r.status_code, r.text[:200])
    assert client.post("/api/sessions/NOT-HEX/frames", params=frame_params(0), content=jpg,
                       headers={**AUTH, "Content-Type": "image/jpeg"}).status_code == 422
    assert put_frame(client, "f" * 32, 0).status_code == 404
    assert client.get(f"/api/sessions/{sid}", headers=AUTH).json()["frames"] == 0


def test_invalid_imu_rejected(client, settings):
    sid = new_session(client)
    url = f"/api/sessions/{sid}/imu"
    assert client.post(url, headers=AUTH, json={"batch_seq": 0, "samples": [{**motion(0, 1.0), "kind": "gps"}]}).status_code == 422
    assert client.post(url, headers=AUTH, json={"batch_seq": 0, "samples": [{**motion(0, 1.0), "extra": 1}]}).status_code == 422
    assert client.post(url, headers=AUTH, json={"batch_seq": 0, "samples": []}).status_code == 422
    assert client.post(url, headers={**AUTH, "Content-Type": "application/json"}, content=b"{not json").status_code == 422
    big = {"batch_seq": 0, "samples": [motion(i, float(i)) for i in range(settings.max_imu_batch + 1)]}
    assert client.post(url, headers=AUTH, json=big).status_code in (413,)
    assert client.post(url, headers={**AUTH, "Content-Length": str(settings.max_json_bytes + 1)},
                       content=b"x" * (settings.max_json_bytes + 1)).status_code == 413
    assert client.get(f"/api/sessions/{sid}", headers=AUTH).json()["imu_samples"] == 0


# --------------------------------------------------------------------------- limits
def test_active_session_limit(client, settings):
    for _ in range(settings.max_active_sessions):
        new_session(client)
    r = client.post("/api/sessions", headers=AUTH, json={})
    assert r.status_code == 429 and r.json()["code"] == "too_many_active_sessions"


def test_storage_limits(tmp_path):
    jpg = make_jpeg()
    s = Settings(access_token=TOKEN, data_dir=tmp_path, max_session_bytes=len(jpg) * 2 + 10)
    with TestClient(create_app(s)) as c:
        sid = new_session(c)
        assert put_frame(c, sid, 0, jpg).status_code == 200
        assert put_frame(c, sid, 1, jpg).status_code == 200
        r = put_frame(c, sid, 2, jpg)
        assert r.status_code == 507 and r.json()["code"] == "session_storage_limit"
    s2 = Settings(access_token=TOKEN, data_dir=tmp_path / "b", max_frame_bytes=len(jpg) + 100,
                  max_total_bytes=len(jpg) + 150)
    with TestClient(create_app(s2)) as c:
        sid = new_session(c)
        assert put_frame(c, sid, 0, jpg).status_code == 200
        assert put_frame(c, sid, 1, jpg).json()["code"] == "storage_full"
        r = c.post("/api/sessions", headers=AUTH, json={})
        assert r.status_code == 507


def test_session_time_limit(client):
    sid = new_session(client)
    store = client.app.state.store
    store.sessions[sid].created_unix -= client.app.state.settings.max_session_seconds + 1
    r = put_frame(client, sid, 0)
    assert r.status_code == 409 and r.json()["code"] == "session_not_active"
    assert client.get(f"/api/sessions/{sid}", headers=AUTH).json()["status"] == "time_limit"
    assert client.get(f"/api/sessions/{sid}/download", headers=AUTH).status_code == 200


def test_cleanup_never_deletes_active_sessions(client):
    store, cfg = client.app.state.store, client.app.state.settings
    active, idle, old = new_session(client), new_session(client), new_session(client)
    client.post(f"/api/sessions/{old}/stop", headers=AUTH, json={})
    now = time.time()
    store.sessions[idle].last_activity_unix = now - cfg.session_idle_timeout_s - 1
    store.sessions[old].ended_unix = now - cfg.session_ttl_s - 1
    rep = store.cleanup(now)
    assert rep["interrupted"] == 1 and rep["deleted"] == 1
    assert store.sessions[active].status == "active" and store.sessions[idle].status == "interrupted"
    assert old not in store.sessions and not (cfg.data_dir / old).exists()
    assert client.delete(f"/api/sessions/{active}", headers=AUTH).status_code == 409   # active: refused


def test_restart_recovers_sessions_and_idempotency(settings):
    with TestClient(create_app(settings)) as c:
        sid = new_session(c)
        put_frame(c, sid, 0)
        c.post(f"/api/sessions/{sid}/imu", headers=AUTH, json={"batch_seq": 0, "samples": [motion(0, 1.0)]})
    with TestClient(create_app(settings)) as c:                        # new process, same disk
        assert c.get(f"/api/sessions/{sid}", headers=AUTH).json()["frames"] == 1
        assert put_frame(c, sid, 0).json()["status"] == "duplicate"
        r = c.post(f"/api/sessions/{sid}/imu", headers=AUTH, json={"batch_seq": 0, "samples": [motion(0, 1.0)]})
        assert r.json()["status"] == "duplicate"
        listed = c.get("/api/sessions", headers=AUTH).json()
        assert [x["session_id"] for x in listed["sessions"]] == [sid] and "Temporary" in listed["storage_note"]

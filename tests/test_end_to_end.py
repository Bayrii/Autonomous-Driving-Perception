"""Server + simulated phone + recorder + session reader + metrics, against known truth."""
import json

import numpy as np
import pytest

from evaluation.metrics import compute
from evaluation.sync_check import estimate
from pc_app.session import Session
from tools import sim_demo


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    root = tmp_path_factory.mktemp("sessions")
    path = sim_demo.run(12.0, sessions_dir=root, report=False,
                        sim_args=["--frame-drop=0.03", "--imu-drop=0.02", "--seed=7"])
    return Session(path)


def test_session_files_and_manifest(session):
    for name in ("manifest.json", "frames.csv", "imu.csv", "clock_sync.csv", "status.csv", "events.csv"):
        assert (session.path / name).exists(), name
    mf = json.loads((session.path / "manifest.json").read_text())
    assert mf["session_id"] == "sim0007" and mf["imu"]["units"]["gyro"] == "rad/s"
    assert mf["row_counts"]["frames"] == len(session.frames)
    assert all((session.path / f).exists() for f in session.frames.file)


def test_gate_w1_and_counters(session):
    m = compute(session)
    assert m["gate_w1_capture_ts"] and m["gate_w1_imu_aligned"]
    assert 25 < m["video_fps"] < 31
    assert 90 < m["gyro_hz"] < 101
    # simulated drops are visible on the PC as missing indices / sequence ids; the phone's own
    # counter is sampled once per second, so it can lag the index gaps by a few frames
    assert m["frames_missing_idx"] > 0
    assert abs(m["frames_missing_idx"] - m["phone_dropped_backpressure"]) <= 3
    assert m["gyro_seq_missing"] > 0
    assert abs(m["sim_clock_offset_err_ms"]) < 1.0
    assert m["lat_cap_to_pc_p50_ms"] > 0
    assert np.isclose(m["gravity_y"], 9.81, atol=0.1)


def test_sync_check_recovers_camera_delay(session):
    r = estimate(session)
    assert r["ok"] and r["axis_convention_ok"]
    assert abs(r["td_ms"] - r["sim_true_td_ms"]) < 5.0
    assert abs(r["focal_px_est"] - 500) < 25


def test_replay_is_deterministic(session):
    a = [(f.frame_index, f.t_capture_phone_ns) for f in session.iter_frames()]
    b = [(f.frame_index, f.t_capture_phone_ns) for f in Session(session.path).iter_frames()]
    assert a == b and a == sorted(a)

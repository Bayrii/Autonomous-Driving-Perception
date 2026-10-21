import numpy as np
import pytest

from pc_app.clock import SyncSample, fit_clock_model
from pc_app.live_state import SeqTracker
from pc_app.protocol import FRAME_HEADER, FrameHeader, pack_frame, unpack_frame
from pc_app.sync import (R_camera_from_body, R_VEHICLE_FROM_CAMERA_NOMINAL, align_frames, integrate_gyro,
                         interpolate, nearest_distance_ns, rotation_angle, rotvec, so3_exp)


# ----------------------------------------------------------------------------- protocol
def test_frame_header_roundtrip():
    h = FrameHeader(123456, 9_876_543_210_123, 9_876_543_299_999, 1280, 720, 1, 270)
    jpeg = b"\xff\xd8fakejpeg\xff\xd9"
    h2, j2 = unpack_frame(pack_frame(h, jpeg))
    assert h2 == h and j2 == jpeg
    assert FRAME_HEADER.size == 32  # phone_client/app.js writes exactly 32 bytes


def test_frame_header_rejects_garbage():
    with pytest.raises(ValueError):
        unpack_frame(b"XXXX" + bytes(40))
    with pytest.raises(ValueError):
        unpack_frame(b"VFR1")


def test_seq_tracker_counts_gaps():
    t = SeqTracker()
    for s in [0, 1, 2, 5, 6, 6, 9]:
        t.add(s)
    assert t.missing == 2 + 2 and t.out_of_order == 1 and t.received == 7


# ----------------------------------------------------------------------------- clock
def test_sync_sample_offset():
    s = SyncSample(1, t_pc_send_ns=1000, t_phone_ns=5_000_600, t_pc_recv_ns=2000)
    assert s.rtt_ns == 1000 and s.offset_ns == 5_000_600 - 1500


def test_clock_model_recovers_offset_and_drift():
    rng = np.random.default_rng(0)
    true_off, drift = 7.5e12, 50e-6
    t0 = np.arange(0, 300e9, 0.5e9) + 1e12            # 5 min of pings at 2 Hz
    up = rng.exponential(2e6, len(t0)) + 0.3e6       # asymmetric, heavy-tailed queueing
    down = rng.exponential(1e6, len(t0)) + 0.3e6
    t_phone_read = t0 + up
    tp = t_phone_read + true_off + drift * (t_phone_read - 1e12)
    t3 = t0 + up + down
    m = fit_clock_model(t0, tp, t3)
    t_mid = 1e12 + 150e9
    est = m.pc_to_phone(t_mid) - t_mid
    truth = true_off + drift * (t_mid - 1e12)
    assert abs(est - truth) < 0.5e6                  # < 0.5 ms
    assert abs(m.drift_ppm - 50) < 2
    assert abs(m.phone_to_pc(m.pc_to_phone(t_mid)) - t_mid) < 1


# ----------------------------------------------------------------------------- frames / rotations
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_camera_from_body_is_rotation(angle):
    R = R_camera_from_body(angle)
    assert np.allclose(R @ R.T, np.eye(3)) and np.isclose(np.linalg.det(R), 1.0)
    assert np.allclose(R @ [0, 0, 1], [0, 0, -1])     # rear camera looks out of the back


def test_vehicle_from_camera_nominal():
    R = R_VEHICLE_FROM_CAMERA_NOMINAL
    assert np.isclose(np.linalg.det(R), 1)
    assert np.allclose(R @ [0, 0, 1], [1, 0, 0])      # optical axis -> forward
    assert np.allclose(R @ [0, 1, 0], [0, 0, -1])     # image down -> vehicle down


def test_portrait_pan_left_moves_scene_right():
    # positive rotation about body +y (phone upright) turns the rear camera to the left,
    # so a static scene moves right in the image: u' = -f * w_Cy > 0
    w_c = R_camera_from_body(0) @ np.array([0, 1.0, 0])
    assert -w_c[1] > 0


def test_so3_exp_log_roundtrip():
    phi = np.array([0.3, -0.2, 0.9])
    R = so3_exp(phi)
    assert np.allclose(R @ R.T, np.eye(3))
    assert np.allclose(rotvec(R), phi)


# ----------------------------------------------------------------------------- interpolation
def test_interpolation_linear_exact_and_bounds():
    t = np.arange(0, 1_000_000_000, 10_000_000, dtype=np.int64)    # 100 Hz
    v = np.stack([2.0 * t / 1e9, -t / 1e9, np.ones(len(t))], -1)
    q = np.array([-1, 5_000_000, 123_456_789, t[-1], t[-1] + 1])
    out, ok = interpolate(q, t, v, "linear")
    assert ok.tolist() == [False, True, True, True, False]
    assert np.allclose(out[ok, 0], 2.0 * q[ok] / 1e9)
    nn, _ = interpolate(q, t, v, "nearest")
    assert np.all(np.abs(nn[ok, 0] - 2.0 * q[ok] / 1e9) <= 2.0 * 0.005 + 1e-12)  # <= |w'| T/2
    assert nearest_distance_ns([123_456_789], t)[0] == 3_456_789


def test_interpolation_rejects_gaps():
    t = np.array([0, 10, 20, 100, 110], dtype=np.int64) * 1_000_000
    v = np.arange(5.0)
    _, ok = interpolate([15_000_000, 50_000_000], t, v, "linear", max_gap_ns=30e6)
    assert ok.tolist() == [True, False]


def test_gyro_integration_constant_rate():
    t = np.arange(0, 2_000_000_001, 10_000_000, dtype=np.int64)
    w = np.tile([0.0, 0.0, 0.5], (len(t), 1))
    R = integrate_gyro(t, w, 250_000_000, 1_250_000_000)
    assert np.isclose(rotation_angle(R), 0.5, atol=1e-9)


def test_yaw_bias_exercise():
    """Addendum exercise A: 0.11 rad/s measured, 0.01 rad/s bias, 10 s."""
    t = np.arange(0, 10_000_000_001, 10_000_000, dtype=np.int64)
    w = np.tile([0.0, 0.0, 0.11], (len(t), 1))
    raw = rotation_angle(integrate_gyro(t, w, 0, 10_000_000_000))
    corrected = rotation_angle(integrate_gyro(t, w, 0, 10_000_000_000, bias=(0, 0, 0.01)))
    assert np.isclose(np.degrees(raw), 63.025, atol=1e-2)
    assert np.isclose(np.degrees(corrected), 57.296, atol=1e-2)


def test_align_frames_flags_gaps_and_shifts():
    t = np.arange(0, 1_000_000_001, 10_000_000, dtype=np.int64)
    t = np.delete(t, range(40, 50))                   # 100 ms hole
    g = np.stack([t / 1e9, np.zeros(len(t)), np.zeros(len(t))], -1)
    frames = np.array([100, 445, 700], dtype=np.int64) * 1_000_000
    al = align_frames(frames, t, g, t, g, td_ns=5_000_000)
    assert al["valid"].tolist() == [True, False, True]
    assert np.isclose(al["gyro_lin"][0, 0], 0.105)    # sampled at capture + td

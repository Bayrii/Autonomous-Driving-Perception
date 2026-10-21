"""Frame <-> IMU time alignment and basic frame conventions (Day 4).

Frames and IMU samples carry timestamps from the *same* phone clock, so pairing
them needs no network-delay correction; only an optional camera-IMU time shift
``td_ns`` (Kalibr convention: t_imu = t_cam + td) remains, which
evaluation/sync_check.py estimates from data.

Interpolation error (sample period T, signal w(t)):
  nearest-neighbour : timing error <= T/2       -> value error <= |w'| T/2
  linear            : value error   <= |w''| T^2 / 8
At T = 10 ms (100 Hz) and a 2 Hz, 1 rad/s rotation: NN <= 63 mrad/s, linear <= 2 mrad/s.
"""
from __future__ import annotations

import numpy as np

# --------------------------------------------------------------------------- frames
# Phone body frame B (Android / W3C device frame): x right, y up, z out of the screen.
# Rear-camera frame C (OpenCV convention): x right in image, y down in image, z forward (optical axis).
# Browsers deliver frames already rotated to the screen orientation, so R_CB depends on
# screen.orientation.angle (0 portrait, 90 = device turned counter-clockwise, ...).
_R_CB = {
    0:   np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]], float),   # x_C=+x_B, y_C=-y_B, z_C=-z_B
    90:  np.array([[0, -1, 0], [-1, 0, 0], [0, 0, -1]], float),  # x_C=-y_B, y_C=-x_B
    180: np.array([[-1, 0, 0], [0, 1, 0], [0, 0, -1]], float),   # x_C=-x_B, y_C=+y_B
    270: np.array([[0, 1, 0], [1, 0, 0], [0, 0, -1]], float),    # x_C=+y_B, y_C=+x_B
}


def R_camera_from_body(orientation_deg: int) -> np.ndarray:
    """Rotation taking phone-body vectors into rear-camera coordinates: v_C = R_CB @ v_B."""
    return _R_CB[int(orientation_deg) % 360].copy()


# Vehicle frame V (ISO 8855): x forward, y left, z up. For a phone mounted so the camera looks
# straight ahead with an upright, level image: x_V = z_C, y_V = -x_C, z_V = -y_C. The real
# mount adds pitch/roll/yaw that must be calibrated (gravity at standstill gives pitch/roll).
R_VEHICLE_FROM_CAMERA_NOMINAL = np.array([[0, 0, 1], [-1, 0, 0], [0, -1, 0]], float)


# --------------------------------------------------------------------------- interpolation
def interpolate(t_query, t, v, method: str = "linear", max_gap_ns: float | None = None):
    """Sample a time series at ``t_query``.

    Returns (values [M, D], valid [M]). A query is invalid if it lies outside the
    sample range or (when ``max_gap_ns`` is set) if the bracketing samples are
    further apart than max_gap_ns (a data gap). Never extrapolates.
    """
    tq = np.asarray(t_query, dtype=np.int64)
    t = np.asarray(t, dtype=np.int64)
    v = np.asarray(v, dtype=np.float64)
    if v.ndim == 1:
        v = v[:, None]
    out = np.full((len(tq), v.shape[1]), np.nan)
    if len(t) < 2:
        return out, np.zeros(len(tq), bool)
    hi = np.clip(np.searchsorted(t, tq, side="left"), 1, len(t) - 1)
    lo = hi - 1
    inside = (tq >= t[0]) & (tq <= t[-1])
    gap = (t[hi] - t[lo]).astype(np.float64)
    if max_gap_ns is not None:
        inside &= gap <= max_gap_ns
    if method == "nearest":
        idx = np.where(tq - t[lo] <= t[hi] - tq, lo, hi)
        out = v[idx]
    elif method == "linear":
        w = np.where(gap > 0, (tq - t[lo]) / np.where(gap > 0, gap, 1), 0.0)[:, None]
        out = v[lo] * (1 - w) + v[hi] * w
    else:
        raise ValueError(method)
    out = out.astype(np.float64)
    out[~inside] = np.nan
    return out, inside


def nearest_distance_ns(t_query, t) -> np.ndarray:
    """|t_query - nearest sample time|: the timing error of nearest-neighbour pairing."""
    tq = np.asarray(t_query, np.int64)
    t = np.asarray(t, np.int64)
    hi = np.clip(np.searchsorted(t, tq), 1, len(t) - 1)
    return np.minimum(np.abs(tq - t[hi - 1]), np.abs(t[hi] - tq))


# --------------------------------------------------------------------------- gyro integration
def so3_exp(phi: np.ndarray) -> np.ndarray:
    """Rodrigues: rotation vector (rad) -> rotation matrix."""
    theta = float(np.linalg.norm(phi))
    K = np.array([[0, -phi[2], phi[1]], [phi[2], 0, -phi[0]], [-phi[1], phi[0], 0]])
    if theta < 1e-9:
        return np.eye(3) + K
    return np.eye(3) + np.sin(theta) / theta * K + (1 - np.cos(theta)) / theta**2 * K @ K


def integrate_gyro(t_ns, w, t0_ns: int, t1_ns: int, bias=(0.0, 0.0, 0.0)) -> np.ndarray:
    """Relative body rotation between t0 and t1: R with v_B(t0) = R @ v_B(t1).

    Integrates body angular velocity from t0 to t1 with linear interpolation at
    the interval ends and midpoint (trapezoid) increments composed on SO(3).
    """
    t_ns = np.asarray(t_ns, np.int64)
    w = np.asarray(w, np.float64) - np.asarray(bias, np.float64)
    inner = t_ns[(t_ns > t0_ns) & (t_ns < t1_ns)]
    knots = np.concatenate([[t0_ns], inner, [t1_ns]])
    wk, ok = interpolate(knots, t_ns, w, "linear")
    if not ok.all():
        raise ValueError("gyro data does not cover the requested interval")
    R = np.eye(3)
    for i in range(len(knots) - 1):
        dt = (knots[i + 1] - knots[i]) * 1e-9
        R = R @ so3_exp(0.5 * (wk[i] + wk[i + 1]) * dt)
    return R


def rotation_angle(R: np.ndarray) -> float:
    return float(np.arccos(np.clip((np.trace(R) - 1) / 2, -1.0, 1.0)))


def rotvec(R: np.ndarray) -> np.ndarray:
    """Inverse of so3_exp (rotation vector, rad)."""
    th = rotation_angle(R)
    v = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    if th < 1e-9:
        return 0.5 * v
    return th / (2 * np.sin(th)) * v


# --------------------------------------------------------------------------- per-frame alignment
def align_frames(frame_t_ns, gyro_t, gyro_xyz, accel_t, accel_xyz, td_ns: int = 0,
                 max_gap_ns: float = 50e6):
    """Per-frame IMU context, the input every later fusion stage consumes.

    Returns dict of arrays (one row per frame):
      t_imu_ns           frame time on the IMU clock (capture + td)
      gyro_nn, gyro_lin  [N,3] rad/s nearest / linear
      gyro_nn_dt_ns      timing error of the nearest pairing
      accel_lin          [N,3] m/s^2
      drot_prev          [N,3] rotation vector (rad) from previous frame to this one (body frame)
      valid              bool, IMU covers the frame without gaps > max_gap_ns
    """
    tf = np.asarray(frame_t_ns, np.int64) + int(td_ns)
    g_nn, ok_nn = interpolate(tf, gyro_t, gyro_xyz, "nearest", max_gap_ns)
    g_lin, ok_g = interpolate(tf, gyro_t, gyro_xyz, "linear", max_gap_ns)
    a_lin, ok_a = interpolate(tf, accel_t, accel_xyz, "linear", max_gap_ns)
    drot = np.full((len(tf), 3), np.nan)
    for i in range(1, len(tf)):
        if ok_g[i] and ok_g[i - 1]:
            drot[i] = rotvec(integrate_gyro(gyro_t, gyro_xyz, tf[i - 1], tf[i]))
    return {
        "t_imu_ns": tf, "gyro_nn": g_nn, "gyro_lin": g_lin,
        "gyro_nn_dt_ns": nearest_distance_ns(tf, gyro_t) if len(gyro_t) > 1 else np.full(len(tf), -1),
        "accel_lin": a_lin, "drot_prev": drot, "valid": ok_nn & ok_g & ok_a,
    }

"""Stream-quality metrics for one recorded session (Day 5 stress test)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from pc_app.session import Session
from pc_app.sync import align_frames

G = 9.81


def _pct(x, q):
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    return float(np.percentile(x, q)) if len(x) else float("nan")


def _ms(ns):
    return np.asarray(ns, dtype=np.float64) / 1e6


def stationary_mask(t_ns, gyro, accel_t, accel, win_s=0.5, gyro_thr=0.05, acc_thr=0.3):
    """Per-gyro-sample flag: |w| and |a|-g stay small over a +-win/2 window."""
    if len(t_ns) < 3 or len(accel_t) < 3:
        return np.zeros(len(t_ns), bool)
    gn = pd.Series(np.linalg.norm(gyro, axis=1))
    an = np.interp(t_ns, accel_t, np.linalg.norm(accel, axis=1))
    dt = np.median(np.diff(t_ns)) / 1e9
    w = max(3, int(win_s / dt))
    g_max = gn.rolling(w, center=True, min_periods=1).max().to_numpy()
    a_dev = pd.Series(np.abs(an - G)).rolling(w, center=True, min_periods=1).max().to_numpy()
    return (g_max < gyro_thr) & (a_dev < acc_thr)


def compute(ses: Session) -> dict:
    m: dict = {"rec_id": ses.path.name, "session_id": ses.manifest.get("session_id", "")}
    hello = ses.manifest.get("phone_hello", {})
    m["client"] = hello.get("user_agent", "")[:60]
    m["imu_api"] = hello.get("imu_api", "")
    m["duration_s"] = ses.manifest.get("duration_s", float("nan"))
    cm = ses.clock_model
    f = ses.frames

    # ------------------------------------------------------------ video
    t_cap = f.t_capture_phone_ns.to_numpy(np.int64)
    m["frames"] = len(f)
    m["frames_with_capture_ts"] = int((f.t_capture_phone_ns > 0).sum())
    span = int(f.frame_index.max() - f.frame_index.min() + 1) if len(f) else 0
    m["frames_missing_idx"] = span - len(f)
    st = ses.status
    if len(st) >= 2:
        num = st[["frames_dropped_busy", "frames_dropped_backpressure"]].fillna(0)
        d = num.iloc[-1] - num.iloc[0]  # counters are cumulative since page load
        m["phone_dropped_busy"] = int(d.frames_dropped_busy)
        m["phone_dropped_backpressure"] = int(d.frames_dropped_backpressure)
    if len(f) > 1:
        dur = (t_cap[-1] - t_cap[0]) / 1e9
        iv = _ms(np.diff(t_cap))
        m["video_fps"] = (len(f) - 1) / dur if dur > 0 else float("nan")
        m["video_w"], m["video_h"] = int(f.width.iloc[0]), int(f.height.iloc[0])
        m["frame_dt_p50_ms"] = _pct(iv, 50)
        m["frame_dt_p95_ms"] = _pct(iv, 95)
        m["frame_dt_max_ms"] = float(iv.max())
        m["frame_jitter_std_ms"] = float(iv[iv <= 1.5 * np.median(iv)].std())  # gaps reported separately
        m["frame_gaps_gt_1p5x"] = int((iv > 1.5 * np.median(iv)).sum())
        m["frame_ts_nonmonotonic"] = int((np.diff(t_cap) <= 0).sum())
        m["frame_ts_source"] = f.ts_source.mode().iloc[0]
        m["jpeg_kb_mean"] = float(f.jpeg_bytes.mean() / 1024)
        m["video_mbps"] = float(f.jpeg_bytes.sum() * 8 / 1e6 / dur) if dur > 0 else float("nan")

    # ------------------------------------------------------------ latency (needs the clock model)
    if cm is not None and len(f):
        cap_pc = cm.phone_to_pc(t_cap)
        lat = _ms(f.t_recv_pc_ns.to_numpy(np.float64) - cap_pc)
        m["lat_cap_to_pc_p50_ms"], m["lat_cap_to_pc_p95_ms"] = _pct(lat, 50), _pct(lat, 95)
        m["lat_cap_to_pc_p99_ms"] = _pct(lat, 99)
        enc = _ms(f.t_send_phone_ns.to_numpy(np.float64) - t_cap)
        m["lat_phone_encode_p50_ms"], m["lat_phone_encode_p95_ms"] = _pct(enc, 50), _pct(enc, 95)
        net = _ms(f.t_recv_pc_ns.to_numpy(np.float64) - cm.phone_to_pc(f.t_send_phone_ns.to_numpy(np.float64)))
        m["lat_network_p50_ms"], m["lat_network_p95_ms"] = _pct(net, 50), _pct(net, 95)
        disp = ses.display
        if not disp.empty:
            j = disp.merge(f[["frame_index", "t_capture_phone_ns"]], on="frame_index")
            dl = _ms(j.t_display_pc_ns.to_numpy(np.float64) - cm.phone_to_pc(j.t_capture_phone_ns.to_numpy(np.float64)))
            m["lat_cap_to_display_p50_ms"], m["lat_cap_to_display_p95_ms"] = _pct(dl, 50), _pct(dl, 95)
            m["frames_displayed_pct"] = 100.0 * len(j) / len(f)

    # ------------------------------------------------------------ clock
    cs = ses.clock_sync
    if cm is not None:
        m["clock_samples"] = len(cs)
        m["clock_rtt_min_ms"] = float(cs.rtt_ns.min() / 1e6)
        m["clock_rtt_p50_ms"] = _pct(_ms(cs.rtt_ns), 50)
        m["clock_rtt_p95_ms"] = _pct(_ms(cs.rtt_ns), 95)
        m["clock_drift_ppm"] = cm.drift_ppm
        m["clock_fit_resid_us"] = cm.residual_std_ns / 1e3
        m["clock_offset_uncertainty_ms"] = cm.uncertainty_ns / 1e6
        truth = hello.get("sim_truth")
        if truth:
            t_mid = float(cs.t_pc_recv_ns.median())
            true_off = truth["offset_at_pc0_ns"] + truth["drift_ppm"] * 1e-6 * (t_mid - truth["pc0_ns"])
            m["sim_clock_offset_err_ms"] = float((cm.pc_to_phone(t_mid) - t_mid - true_off) / 1e6)
            m["sim_drift_err_ppm"] = cm.drift_ppm - truth["drift_ppm"]

    # ------------------------------------------------------------ IMU
    for s in ("gyro", "accel"):
        df = ses.imu[ses.imu.sensor_type == s] if not ses.imu.empty else pd.DataFrame()
        if len(df) < 2:
            m[f"{s}_samples"] = len(df)
            continue
        t = np.sort(df.t_phone_ns.to_numpy(np.int64))
        iv = _ms(np.diff(t))
        seqs = df.seq.to_numpy()
        m[f"{s}_samples"] = len(df)
        m[f"{s}_hz"] = (len(t) - 1) / ((t[-1] - t[0]) / 1e9)
        m[f"{s}_dt_p50_ms"] = _pct(iv, 50)
        m[f"{s}_jitter_std_ms"] = float(iv[iv <= 2 * np.median(iv)].std())  # gaps reported separately
        m[f"{s}_dt_max_ms"] = float(iv.max())
        m[f"{s}_gaps_gt_2x"] = int((iv > 2 * np.median(iv)).sum())
        m[f"{s}_seq_missing"] = int(seqs.max() - seqs.min() + 1 - len(np.unique(seqs)))
        m[f"{s}_ts_nonmonotonic"] = int((np.diff(df.t_phone_ns.to_numpy(np.int64)) <= 0).sum())
        if cm is not None:
            il = _ms(df.t_recv_pc_ns.to_numpy(np.float64) - cm.phone_to_pc(df.t_phone_ns.to_numpy(np.float64)))
            m[f"{s}_lat_to_pc_p50_ms"], m[f"{s}_lat_to_pc_p95_ms"] = _pct(il, 50), _pct(il, 95)

    # ------------------------------------------------------------ frame <-> IMU alignment (Gate W1)
    g_t, g = ses.sensor("gyro")
    a_t, a = ses.sensor("accel")
    if len(g_t) > 1 and len(a_t) > 1 and len(f):
        al = align_frames(t_cap, g_t, g, a_t, a)
        # frames before the first / after the last IMU sample (recording edges) are reported separately
        interior = (t_cap >= max(g_t[0], a_t[0])) & (t_cap <= min(g_t[-1], a_t[-1]))
        m["frames_outside_imu_span"] = int((~interior).sum())
        m["frames_imu_aligned_pct"] = 100.0 * float(al["valid"][interior].mean()) if interior.any() else 0.0
        nn = _ms(al["gyro_nn_dt_ns"][interior])
        m["frame_imu_nn_dt_p50_ms"] = _pct(nn, 50)
        m["frame_imu_nn_dt_max_ms"] = float(nn.max()) if len(nn) else float("nan")
        st_mask = stationary_mask(g_t, g, a_t, a)
        m["stationary_pct"] = 100.0 * float(st_mask.mean())
        if st_mask.sum() > 10:
            gb = g[st_mask].mean(0)
            ga = np.array([np.interp(g_t[st_mask], a_t, a[:, k]).mean() for k in range(3)])
            m["gyro_bias_x"], m["gyro_bias_y"], m["gyro_bias_z"] = map(float, gb)
            m["gravity_x"], m["gravity_y"], m["gravity_z"] = map(float, ga)
    else:
        m["frames_imu_aligned_pct"] = 0.0

    m["gate_w1_capture_ts"] = bool(len(f) and m["frames_with_capture_ts"] == len(f)
                                   and m.get("frame_ts_nonmonotonic", 0) == 0)
    m["gate_w1_imu_aligned"] = bool(m["frames_imu_aligned_pct"] >= 99.0)
    return m

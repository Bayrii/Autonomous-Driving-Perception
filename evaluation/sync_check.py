"""Validate camera<->IMU synchronisation from data (Day 4 / Gate W1).

Under (mostly) rotational motion, global image motion between consecutive frames
must match the gyro-predicted motion of a distant scene:

    u' = -f * w_Cy,  v' = f * w_Cx,   w_C = R_CB(orientation) @ (w_B - bias)

The script measures image motion with phase correlation, then searches the time
shift td (Kalibr convention t_imu = t_cam + td) that maximises the correlation
between measured and predicted motion. It also fits f (px/rad), which should be
close to the calibrated focal length, and checks the per-axis signs, which
validates the phone-body -> camera axis convention.

Record for it: hold the phone, point it at a textured static scene a few metres
away, and rotate it (pan/tilt) by hand for 20-30 s. Driving footage is dominated
by translation and is not suitable.

    python -m evaluation.sync_check sessions/<rec_id>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from pc_app.session import Session
from pc_app.sync import R_camera_from_body
from evaluation.metrics import stationary_mask


def image_motion(ses: Session, work_width: int = 640):
    """Per consecutive frame pair: (t0, t1, du/dt, dv/dt [full-res px/s], response).

    Frames are downscaled to ``work_width`` (never upscaled). Downscaling much further
    blurs fine texture toward Nyquist and biases phase correlation (seen as a ~3 ms td
    error at 320 px on 1280-wide simulated frames).
    """
    rows, prev, prev_t, prev_idx = [], None, None, None
    win = None
    for fr in ses.iter_frames():
        img = cv2.imread(str(fr.path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            prev = None
            continue
        s = min(1.0, work_width / img.shape[1])
        g = cv2.resize(img, (int(round(img.shape[1] * s)), int(round(img.shape[0] * s))),
                       interpolation=cv2.INTER_AREA).astype(np.float32)
        if win is None or win.shape != g.shape[::-1]:
            win = cv2.createHanningWindow(g.shape[::-1], cv2.CV_32F)
        if prev is not None and fr.frame_index == prev_idx + 1 and prev.shape == g.shape:
            (dx, dy), resp = cv2.phaseCorrelate(prev, g, win)
            dt = (fr.t_capture_phone_ns - prev_t) / 1e9
            if dt > 0:
                rows.append((prev_t, fr.t_capture_phone_ns, dx / s / dt, dy / s / dt, resp))
        prev, prev_t, prev_idx = g, fr.t_capture_phone_ns, fr.frame_index
    return np.array(rows, dtype=np.float64).reshape(-1, 5)


def _interval_mean(t_ns, w, t0, t1):
    """Mean of w over [t0, t1] via the cumulative trapezoid integral."""
    tt = (t_ns - t_ns[0]) / 1e9
    c = np.vstack([np.zeros((1, 3)), np.cumsum(0.5 * (w[1:] + w[:-1]) * np.diff(tt)[:, None], axis=0)])
    a, b = (t0 - t_ns[0]) / 1e9, (t1 - t_ns[0]) / 1e9
    ca = np.stack([np.interp(a, tt, c[:, k]) for k in range(3)], -1)
    cb = np.stack([np.interp(b, tt, c[:, k]) for k in range(3)], -1)
    return (cb - ca) / (b - a)[:, None]


def estimate(ses: Session, search_ms: float = 200.0, step_ms: float = 1.0, min_response: float = 0.05) -> dict:
    g_t, g = ses.sensor("gyro")
    a_t, a = ses.sensor("accel")
    if len(g_t) < 10:
        return {"ok": False, "reason": "no gyro data"}
    st = stationary_mask(g_t, g, a_t, a)
    bias = g[st].mean(0) if st.sum() > 20 else np.zeros(3)
    orient = int(ses.frames.orientation_deg.mode().iloc[0])
    R = R_camera_from_body(orient)
    mo = image_motion(ses)
    mo = mo[mo[:, 4] >= min_response]
    if len(mo) < 20:
        return {"ok": False, "reason": f"only {len(mo)} usable frame pairs"}
    meas = np.concatenate([mo[:, 2], mo[:, 3]])

    lags = np.arange(-search_ms, search_ms + step_ms / 2, step_ms)
    corr = np.full(len(lags), np.nan)
    for i, lag in enumerate(lags):
        t0 = mo[:, 0] + lag * 1e6
        t1 = mo[:, 1] + lag * 1e6
        ok = (t0 >= g_t[0]) & (t1 <= g_t[-1])
        if ok.mean() < 0.8:
            continue
        wc = _interval_mean(g_t, g - bias, t0[ok], t1[ok]) @ R.T
        pred = np.concatenate([-wc[:, 1], wc[:, 0]])
        mm = np.concatenate([mo[ok, 2], mo[ok, 3]])
        if pred.std() > 0:
            corr[i] = np.corrcoef(mm, pred)[0, 1]
    if np.all(np.isnan(corr)):
        return {"ok": False, "reason": "IMU does not cover the video"}
    k = int(np.nanargmax(corr))
    td = lags[k]
    if 0 < k < len(lags) - 1 and np.isfinite(corr[k - 1]) and np.isfinite(corr[k + 1]):  # parabolic refinement
        den = corr[k - 1] - 2 * corr[k] + corr[k + 1]
        if den < 0:
            td += 0.5 * (corr[k - 1] - corr[k + 1]) / den * step_ms

    t0, t1 = mo[:, 0] + td * 1e6, mo[:, 1] + td * 1e6
    ok = (t0 >= g_t[0]) & (t1 <= g_t[-1])
    wc = _interval_mean(g_t, g - bias, t0[ok], t1[ok]) @ R.T
    pu, pv = -wc[:, 1], wc[:, 0]
    mu, mv = mo[ok, 2], mo[ok, 3]
    pred = np.concatenate([pu, pv])
    meas = np.concatenate([mu, mv])
    f_est = float(pred @ meas / (pred @ pred))
    excitation = float(np.degrees(np.sqrt(np.mean(pred ** 2))))
    corr_u = float(np.corrcoef(mu, pu)[0, 1]) if pu.std() > 0 else float("nan")
    corr_v = float(np.corrcoef(mv, pv)[0, 1]) if pv.std() > 0 else float("nan")
    peak = float(np.nanmax(corr))
    reliable = peak > 0.7 and excitation > 10 and f_est > 0
    result = {
        "ok": True, "reliable": bool(reliable),
        "td_ms": float(td), "convention": "t_imu = t_cam + td",
        "peak_corr": peak, "corr_u": corr_u, "corr_v": corr_v,
        "focal_px_est": f_est, "orientation_deg": orient,
        "gyro_rms_deg_s": excitation, "gyro_bias_used": bias.tolist(),
        "pairs_used": int(ok.sum()),
        "axis_convention_ok": bool(corr_u > 0 and corr_v > 0),
        "interpretation": ("frame timestamps are late; shift them by td" if td < 0 else
                           "frame timestamps are early; shift them by td"),
    }
    truth = ses.manifest.get("phone_hello", {}).get("sim_truth")
    if truth:
        result["sim_true_td_ms"] = truth["td_ms"]
        result["sim_td_err_ms"] = float(td - truth["td_ms"])
        result["sim_true_focal_px"] = truth["focal_px"]
    result["_curve"] = (lags.tolist(), corr.tolist())
    result["_series"] = ((mo[ok, 0] - mo[0, 0]) / 1e9, mu, mv, f_est * pu, f_est * pv)
    return result


def plot(res: dict, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from evaluation.plot_style import apply_style, legend_above, C1, C2, INK2
    apply_style()
    lags, corr = res["_curve"]
    t, mu, mv, pu, pv = res["_series"]
    fig, ax = plt.subplots(3, 1, figsize=(10, 8.5), gridspec_kw={"height_ratios": [1, 1.2, 1.2]})
    ax[0].plot(lags, corr, color=C1, lw=1.5)
    ax[0].axvline(res["td_ms"], color=INK2, lw=1, ls="--")
    ax[0].annotate(f"td = {res['td_ms']:+.1f} ms (corr {res['peak_corr']:.2f})", (res["td_ms"], res["peak_corr"]),
                   xytext=(8, -14), textcoords="offset points", color=INK2)
    ax[0].set(xlabel="time shift td [ms]  (t_imu = t_cam + td)", ylabel="correlation",
              title="Image motion vs gyro-predicted motion")
    for a_, m_, p_, name in ((ax[1], mu, pu, "horizontal u"), (ax[2], mv, pv, "vertical v")):
        a_.plot(t, m_, color=C1, lw=1.2, label="image motion (phase correlation)")
        a_.plot(t, p_, color=C2, lw=1.2, label=f"gyro x focal ({res['focal_px_est']:.0f} px), shifted by td")
        a_.set(ylabel=f"{name} [px/s]")
        legend_above(a_, 2)
    ax[2].set_xlabel("time [s]")
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--search-ms", type=float, default=200.0)
    a = ap.parse_args(argv)
    ses = Session(a.session)
    res = estimate(ses, a.search_ms)
    out_dir = ses.path / "report"
    out_dir.mkdir(exist_ok=True)
    if res.get("ok"):
        plot(res, out_dir / "sync_check.png")
    clean = {k: v for k, v in res.items() if not k.startswith("_")}
    (out_dir / "sync_check.json").write_text(json.dumps(clean, indent=2))
    print(json.dumps(clean, indent=2))


if __name__ == "__main__":
    main()

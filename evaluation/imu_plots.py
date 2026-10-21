"""Addendum exercise A: gyro / accel / estimated yaw with stationary segments.

Two yaw estimates are plotted:

* naive   : integral of gyro z (rotation about the phone's own z axis)
* heading : integral of omega . up_B, where up_B is the unit "up" direction in the
            phone frame measured from gravity while stationary. This is rotation
            about the vertical, i.e. the vehicle's heading change, as long as the
            mount does not move.

For a phone lying flat the two agree; for an upright (windshield) mount, phone z
is horizontal, so the naive yaw is NOT the car heading - the required "phone
orientation differs from car heading" case. Gyro bias (mean at rest) is removed.

    python -m evaluation.imu_plots sessions/<rec_id>
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from evaluation.metrics import stationary_mask
from evaluation.plot_style import AXES3, C1, C2, INK2, SHADE, apply_style, legend_above
from pc_app.session import Session


def yaw_estimates(t_ns, gyro, accel_t, accel):
    st = stationary_mask(t_ns, gyro, accel_t, accel)
    bias = gyro[st].mean(0) if st.sum() > 20 else np.zeros(3)
    if st.sum() > 20:
        g_rest = np.array([np.interp(t_ns[st], accel_t, accel[:, k]).mean() for k in range(3)])
    else:  # no rest period: fall back to the session mean (biased by vehicle acceleration)
        g_rest = accel.mean(0)
    up = g_rest / np.linalg.norm(g_rest)  # specific force at rest points UP
    w = gyro - bias
    dt = np.diff(t_ns) / 1e9
    integ = lambda r: np.concatenate([[0.0], np.cumsum(0.5 * (r[1:] + r[:-1]) * dt)])
    return {"stationary": st, "bias": bias, "up_body": up,
            "yaw_naive": integ(w[:, 2]), "yaw_heading": integ(w @ up),
            "yaw_naive_raw": integ(gyro[:, 2])}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    a = ap.parse_args(argv)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    apply_style()

    ses = Session(a.session)
    g_t, g = ses.sensor("gyro")
    a_t, acc = ses.sensor("accel")
    if len(g_t) < 10 or len(a_t) < 10:
        raise SystemExit("not enough IMU data")
    y = yaw_estimates(g_t, g, a_t, acc)
    t = (g_t - g_t[0]) / 1e9
    ta = (a_t - g_t[0]) / 1e9

    fig, ax = plt.subplots(3, 1, figsize=(11, 8.5), sharex=True)
    for axx in ax:
        axx.fill_between(t, 0, 1, where=y["stationary"], color=SHADE, alpha=0.6, lw=0,
                         transform=axx.get_xaxis_transform(), label="stationary")
    for k, n in enumerate("xyz"):
        ax[0].plot(t, g[:, k], color=AXES3[k], lw=1.0, label=n)
        ax[1].plot(ta, acc[:, k], color=AXES3[k], lw=1.0, label=n)
    ax[0].set(ylabel="rad/s", title="Gyroscope (phone body frame)")
    ax[1].set(ylabel="m/s²", title="Accelerometer incl. gravity (phone body frame)")
    ax[2].plot(t, np.degrees(y["yaw_heading"]), color=C1, lw=1.5, label="heading change (about gravity axis)")
    ax[2].plot(t, np.degrees(y["yaw_naive"]), color=C2, lw=1.5, label="naive yaw (about phone z)")
    up = y["up_body"]
    ax[2].set(ylabel="deg", xlabel="time [s]",
              title=f"Integrated yaw, bias removed (up in phone frame = [{up[0]:+.2f}, {up[1]:+.2f}, {up[2]:+.2f}])")
    for axx, n in zip(ax, (4, 4, 3)):
        legend_above(axx, n)
    fig.tight_layout()
    out_dir = ses.path / "report"
    out_dir.mkdir(exist_ok=True)
    fig.savefig(out_dir / "imu_yaw.png", dpi=110)
    plt.close(fig)
    summary = {"gyro_bias_rad_s": y["bias"].tolist(), "up_in_phone_frame": up.tolist(),
               "tilt_of_phone_z_from_vertical_deg": float(np.degrees(np.arccos(abs(up[2])))),
               "final_heading_change_deg": float(np.degrees(y["yaw_heading"][-1])),
               "final_naive_yaw_deg": float(np.degrees(y["yaw_naive"][-1])),
               "final_naive_yaw_without_bias_correction_deg": float(np.degrees(y["yaw_naive_raw"][-1])),
               "stationary_pct": float(100 * y["stationary"].mean())}
    (out_dir / "imu_yaw.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"plot -> {out_dir / 'imu_yaw.png'}")


if __name__ == "__main__":
    main()

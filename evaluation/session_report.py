"""Stress-test report for one or more recorded sessions (Day 5).

    python -m evaluation.session_report sessions/<rec_id> [more sessions] [--scenario indoor]

Writes <session>/report/{report.md, metrics.json, *.png} and appends one row per
session to evaluation/benchmarks.csv (the benchmark CSV deliverable).
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

from evaluation.metrics import compute, stationary_mask
from evaluation.plot_style import AXES3, C1, C2, INK2, SHADE, SURFACE, apply_style, legend_above
from pc_app.config import ROOT
from pc_app.session import Session

BENCH_CSV = ROOT / "evaluation" / "benchmarks.csv"
BENCH_COLS = [
    "rec_id", "scenario", "client", "imu_api", "duration_s", "video_w", "video_h", "video_fps",
    "frame_dt_p50_ms", "frame_dt_p95_ms", "frame_jitter_std_ms", "frames", "frames_missing_idx",
    "phone_dropped_busy", "phone_dropped_backpressure", "frame_gaps_gt_1p5x", "frame_ts_source",
    "gyro_hz", "gyro_jitter_std_ms", "gyro_gaps_gt_2x", "gyro_seq_missing",
    "accel_hz", "accel_jitter_std_ms", "accel_gaps_gt_2x", "accel_seq_missing",
    "lat_cap_to_pc_p50_ms", "lat_cap_to_pc_p95_ms", "lat_cap_to_display_p50_ms", "lat_cap_to_display_p95_ms",
    "lat_phone_encode_p50_ms", "lat_network_p50_ms", "video_mbps",
    "clock_rtt_min_ms", "clock_drift_ppm", "clock_offset_uncertainty_ms",
    "frames_imu_aligned_pct", "stationary_pct", "gate_w1_capture_ts", "gate_w1_imu_aligned",
]


def _fmt(v, nd=1):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    if isinstance(v, bool):
        return "PASS" if v else "FAIL"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


# --------------------------------------------------------------------------- plots
def plot_timeline(ses: Session, out: Path) -> None:
    import matplotlib.pyplot as plt
    g_t, g = ses.sensor("gyro")
    a_t, a = ses.sensor("accel")
    f = ses.frames
    t0 = min([x[0] for x in (g_t, a_t) if len(x)] + [int(f.t_capture_phone_ns.iloc[0])])
    fig, ax = plt.subplots(3, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [1.3, 1.3, 1]})
    st = stationary_mask(g_t, g, a_t, a) if len(g_t) > 2 else np.zeros(0, bool)
    for arr_t, arr, axx, unit, title in ((g_t, g, ax[0], "rad/s", "Gyroscope (phone body frame)"),
                                         (a_t, a, ax[1], "m/s²", "Accelerometer incl. gravity (phone body frame)")):
        ts = (arr_t - t0) / 1e9
        if len(st) and len(g_t):
            stg = np.interp(arr_t, g_t, st.astype(float)) > 0.5 if len(arr_t) else []
            axx.fill_between(ts, 0, 1, where=stg, color=SHADE, alpha=0.6, lw=0,
                             transform=axx.get_xaxis_transform(), label="stationary")
        for k, n in enumerate("xyz"):
            if len(arr):
                axx.plot(ts, arr[:, k], color=AXES3[k], lw=1.0, label=n)
        axx.set(ylabel=unit, title=title)
        legend_above(axx, 4)
    tf = (f.t_capture_phone_ns.to_numpy(np.int64) - t0) / 1e9
    if len(tf) > 1:
        ax[2].plot(tf[1:], np.diff(tf) * 1e3, color=C1, lw=1.0, marker="o", ms=2, label="frame interval")
        gaps = np.flatnonzero(np.diff(f.frame_index.to_numpy()) > 1)
        if len(gaps):
            ax[2].scatter(tf[gaps + 1], np.diff(tf)[gaps] * 1e3, s=36, facecolor="none", edgecolor=C2, lw=1.5,
                          label="missing frame index", zorder=3)
    ax[2].set(ylabel="ms", xlabel="time on the phone clock [s]", title="Video frame intervals (capture timestamps)")
    legend_above(ax[2], 2)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


def plot_latency(ses: Session, out: Path) -> None:
    import matplotlib.pyplot as plt
    cm, f = ses.clock_model, ses.frames
    if cm is None or f.empty:
        return
    lat = (f.t_recv_pc_ns.to_numpy(float) - cm.phone_to_pc(f.t_capture_phone_ns.to_numpy(float))) / 1e6
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.6))
    ax[0].hist(lat, bins=50, color=C1, edgecolor="white", linewidth=0.5)
    for q, ls in ((50, "-"), (95, "--")):
        v = np.percentile(lat, q)
        ax[0].axvline(v, color=INK2, lw=1, ls=ls)
        ax[0].annotate(f"p{q} {v:.0f} ms", (v, 0.95), xycoords=("data", "axes fraction"), xytext=(4, 0),
                       textcoords="offset points", color=INK2, fontsize=8, va="top",
                           bbox=dict(fc=SURFACE, ec="none", pad=1.5))
    ax[0].set(title="Capture → PC receive latency", xlabel="ms", ylabel="frames")
    disp = ses.display
    if not disp.empty:
        j = disp.merge(f[["frame_index", "t_capture_phone_ns"]], on="frame_index")
        dl = (j.t_display_pc_ns.to_numpy(float) - cm.phone_to_pc(j.t_capture_phone_ns.to_numpy(float))) / 1e6
        ax[1].hist(dl, bins=50, color=C1, edgecolor="white", linewidth=0.5)
        for q, ls in ((50, "-"), (95, "--")):
            v = np.percentile(dl, q)
            ax[1].axvline(v, color=INK2, lw=1, ls=ls)
            ax[1].annotate(f"p{q} {v:.0f} ms", (v, 0.95), xycoords=("data", "axes fraction"), xytext=(4, 0),
                           textcoords="offset points", color=INK2, fontsize=8, va="top",
                           bbox=dict(fc=SURFACE, ec="none", pad=1.5))
        ax[1].set(title="Capture → display latency (live viewer)", xlabel="ms", ylabel="frames")
    else:
        ax[1].text(0.5, 0.5, "no display log\n(headless recording)", ha="center", va="center", color=INK2,
                   transform=ax[1].transAxes)
        ax[1].set_axis_off()
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


def plot_clock(ses: Session, out: Path) -> None:
    import matplotlib.pyplot as plt
    cs, cm = ses.clock_sync, ses.clock_model
    if cm is None:
        return
    mid = (cs.t_pc_send_ns.to_numpy(float) + cs.t_pc_recv_ns.to_numpy(float)) / 2
    off = cs.t_phone_ns.to_numpy(float) - mid
    model = cm.pc_to_phone(mid) - mid
    ts = (mid - mid[0]) / 1e9
    fig, ax = plt.subplots(2, 1, figsize=(11, 5.5), sharex=True)
    ax[0].scatter(ts, (off - model) / 1e6, s=8, color=C1, label="ping sample (offset − model)")
    ax[0].axhline(0, color=INK2, lw=1)
    ax[0].set(ylabel="ms", title=f"Clock offset residuals (model drift {cm.drift_ppm:+.1f} ppm, "
                                 f"uncertainty ±{cm.uncertainty_ns / 1e6:.1f} ms)")
    legend_above(ax[0], 1)
    ax[1].plot(ts, cs.rtt_ns / 1e6, color=C1, lw=1.0, marker="o", ms=2)
    ax[1].set(ylabel="RTT [ms]", xlabel="time [s]", title="Ping round-trip time")
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)


# --------------------------------------------------------------------------- report
def write_report(ses: Session, m: dict, out_dir: Path, scenario: str) -> Path:
    L = [f"# Stream report — {ses.path.name}", "",
         f"Scenario: **{scenario or 'unspecified'}** · client: `{m.get('client', '')}` · IMU API: `{m.get('imu_api')}` "
         f"· duration {_fmt(m.get('duration_s'))} s", ""]
    if ses.manifest.get("phone_hello", {}).get("sim_truth"):
        L += ["> **Simulated session** (tools/fake_phone.py). Numbers validate the PC pipeline, not a phone.", ""]
    L += ["## Gate W1", "",
          "| check | result |", "|---|---|",
          f"| every saved frame has a capture timestamp, monotonic | {_fmt(m['gate_w1_capture_ts'])} |",
          f"| IMU can be aligned to frames (≥ 99 % of frames inside the IMU span) | {_fmt(m['gate_w1_imu_aligned'])} "
          f"({_fmt(m.get('frames_imu_aligned_pct'), 2)} %) |", ""]
    rows = [
        ("Video", [
            ("resolution", f"{m.get('video_w')}x{m.get('video_h')}"),
            ("achieved FPS (capture clock)", _fmt(m.get("video_fps"), 2)),
            ("frame interval p50 / p95 / max [ms]",
             f"{_fmt(m.get('frame_dt_p50_ms'))} / {_fmt(m.get('frame_dt_p95_ms'))} / {_fmt(m.get('frame_dt_max_ms'))}"),
            ("frame jitter (std of interval) [ms]", _fmt(m.get("frame_jitter_std_ms"), 2)),
            ("frames received / missing frame indices", f"{m.get('frames')} / {m.get('frames_missing_idx')}"),
            ("dropped on phone: encoder busy / backpressure",
             f"{m.get('phone_dropped_busy', 'n/a')} / {m.get('phone_dropped_backpressure', 'n/a')}"),
            ("intervals > 1.5x median", m.get("frame_gaps_gt_1p5x")),
            ("frame timestamp source", m.get("frame_ts_source")),
            ("mean JPEG size [KB] / bitrate [Mb/s]", f"{_fmt(m.get('jpeg_kb_mean'))} / {_fmt(m.get('video_mbps'), 2)}"),
        ]),
        ("IMU", [(f"{s}: rate [Hz] / dt p50 [ms] / jitter std [ms]",
                  f"{_fmt(m.get(f'{s}_hz'))} / {_fmt(m.get(f'{s}_dt_p50_ms'), 2)} / {_fmt(m.get(f'{s}_jitter_std_ms'), 2)}")
                 for s in ("gyro", "accel")] +
                [(f"{s}: max gap [ms] / gaps > 2x / missing seq",
                  f"{_fmt(m.get(f'{s}_dt_max_ms'))} / {m.get(f'{s}_gaps_gt_2x')} / {m.get(f'{s}_seq_missing')}")
                 for s in ("gyro", "accel")] +
                [("stationary time [%]", _fmt(m.get("stationary_pct"))),
                 ("gravity at rest (body x, y, z) [m/s²]",
                  f"{_fmt(m.get('gravity_x'), 2)}, {_fmt(m.get('gravity_y'), 2)}, {_fmt(m.get('gravity_z'), 2)}"),
                 ("gyro bias at rest (x, y, z) [rad/s]",
                  f"{_fmt(m.get('gyro_bias_x'), 4)}, {_fmt(m.get('gyro_bias_y'), 4)}, {_fmt(m.get('gyro_bias_z'), 4)}")]),
        ("Latency (PC clock via clock model)", [
            ("capture → PC receive p50 / p95 / p99 [ms]",
             f"{_fmt(m.get('lat_cap_to_pc_p50_ms'))} / {_fmt(m.get('lat_cap_to_pc_p95_ms'))} / {_fmt(m.get('lat_cap_to_pc_p99_ms'))}"),
            ("of which phone encode p50 / p95 [ms]",
             f"{_fmt(m.get('lat_phone_encode_p50_ms'))} / {_fmt(m.get('lat_phone_encode_p95_ms'))}"),
            ("of which network+queue p50 / p95 [ms]",
             f"{_fmt(m.get('lat_network_p50_ms'))} / {_fmt(m.get('lat_network_p95_ms'))}"),
            ("capture → display p50 / p95 [ms]",
             f"{_fmt(m.get('lat_cap_to_display_p50_ms'))} / {_fmt(m.get('lat_cap_to_display_p95_ms'))}"),
            ("IMU sample → PC p50 / p95 [ms] (includes 40 ms batching)",
             f"{_fmt(m.get('gyro_lat_to_pc_p50_ms'))} / {_fmt(m.get('gyro_lat_to_pc_p95_ms'))}"),
        ]),
        ("Clock sync", [
            ("ping samples / RTT min / p50 / p95 [ms]",
             f"{m.get('clock_samples')} / {_fmt(m.get('clock_rtt_min_ms'), 2)} / {_fmt(m.get('clock_rtt_p50_ms'), 2)} / "
             f"{_fmt(m.get('clock_rtt_p95_ms'), 2)}"),
            ("drift phone vs PC [ppm]", _fmt(m.get("clock_drift_ppm"), 2)),
            ("offset uncertainty (min RTT / 2) [ms]", _fmt(m.get("clock_offset_uncertainty_ms"), 2)),
            ("frame ↔ nearest gyro sample p50 / max [ms]",
             f"{_fmt(m.get('frame_imu_nn_dt_p50_ms'), 2)} / {_fmt(m.get('frame_imu_nn_dt_max_ms'), 2)}"),
        ] + ([("SIM: offset error [ms] / drift error [ppm]",
               f"{_fmt(m.get('sim_clock_offset_err_ms'), 3)} / {_fmt(m.get('sim_drift_err_ppm'), 2)}")]
             if "sim_clock_offset_err_ms" in m else [])),
    ]
    for title, items in rows:
        L += [f"## {title}", "", "| metric | value |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in items]
        L.append("")
    sc = out_dir / "sync_check.json"
    if sc.exists():
        s = json.loads(sc.read_text())
        L += ["## Camera ↔ IMU time offset (evaluation/sync_check.py)", ""]
        if s.get("ok"):
            L += [f"td = **{s['td_ms']:+.1f} ms** ({s['convention']}), peak correlation {s['peak_corr']:.2f}, "
                  f"focal estimate {s['focal_px_est']:.0f} px, axis convention "
                  f"{'consistent' if s['axis_convention_ok'] else 'INCONSISTENT'}, "
                  f"{'reliable' if s['reliable'] else 'NOT reliable (needs more rotation / texture)'}."
                  + (f" Simulation truth {s['sim_true_td_ms']:+.1f} ms." if "sim_true_td_ms" in s else ""), "",
                  "![sync check](sync_check.png)", ""]
        else:
            L += [f"not available: {s.get('reason')}", ""]
    L += ["## Plots", "", "![timeline](timeline.png)", "", "![latency](latency.png)", "", "![clock](clock_sync.png)", ""]
    p = out_dir / "report.md"
    p.write_text("\n".join(L), encoding="utf-8")
    return p


def append_benchmark(m: dict, scenario: str, path: Path = BENCH_CSV) -> None:
    row = {**m, "scenario": scenario}
    rows = []
    if path.exists():
        with open(path, newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("rec_id") != m["rec_id"]]  # re-running replaces
    rows.append({k: row.get(k, "") for k in BENCH_COLS})
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=BENCH_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{v:.3f}" if isinstance(v, float) else v) for k, v in r.items()})


def report(session: str | Path, scenario: str = "", sync_check: bool = True, bench: Path = BENCH_CSV) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    apply_style()
    ses = Session(session)
    out_dir = ses.path / "report"
    out_dir.mkdir(exist_ok=True)
    m = compute(ses)
    if sync_check:
        from evaluation import sync_check as sc
        res = sc.estimate(ses)
        if res.get("ok"):
            sc.plot(res, out_dir / "sync_check.png")
            m["sync_td_ms"], m["sync_peak_corr"] = res["td_ms"], res["peak_corr"]
        (out_dir / "sync_check.json").write_text(
            json.dumps({k: v for k, v in res.items() if not k.startswith("_")}, indent=2))
    plot_timeline(ses, out_dir / "timeline.png")
    plot_latency(ses, out_dir / "latency.png")
    plot_clock(ses, out_dir / "clock_sync.png")
    (out_dir / "metrics.json").write_text(json.dumps(m, indent=2, default=str))
    p = write_report(ses, m, out_dir, scenario)
    append_benchmark(m, scenario, bench)
    print(f"{ses.path.name}: report -> {p}")
    return m


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--scenario", default="", help="indoor | stationary | handheld | road (passenger) | sim")
    ap.add_argument("--no-sync-check", action="store_true")
    a = ap.parse_args(argv)
    for s in a.sessions:
        report(s, a.scenario, not a.no_sync_check)


if __name__ == "__main__":
    main()

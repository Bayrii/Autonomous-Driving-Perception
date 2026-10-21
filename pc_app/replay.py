"""Deterministic replay of a recorded session.

Frame order and IMU pairing come only from recorded data. Pacing (real time,
--speed, --step) changes *when* a frame is shown, never *what* is shown, so
``--export`` produces the same video on every run.
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from .session import Session
from .sync import align_frames
from .viewer import GREEN, GREY, RED, YELLOW, compose


def run_replay(path: str | Path, cfg: dict, speed: float = 1.0, step: bool = False, loop: bool = False,
               export: str | None = None, headless: bool = False, td_ms: float = 0.0) -> int:
    ses = Session(path)
    frames = ses.frames
    if frames.empty:
        raise SystemExit(f"{path}: no frames recorded")
    g_t, g = ses.sensor("gyro")
    a_t, a = ses.sensor("accel")
    gyro_arr = np.column_stack([g_t, g]) if len(g_t) else np.zeros((0, 4))
    accel_arr = np.column_stack([a_t, a]) if len(a_t) else np.zeros((0, 4))
    td_ns = int(td_ms * 1e6)
    t_cap = frames.t_capture_phone_ns.to_numpy(np.int64)
    al = align_frames(t_cap, g_t, g, a_t, a, td_ns) if len(g_t) > 1 and len(a_t) > 1 else None
    marks = t_cap + td_ns
    n = len(frames)
    disp_h = cfg["viewer"]["display_height"]
    win = f"replay - {ses.path.name}"
    writer, out_size = None, None
    shown = 0

    while True:
        wall0, t0 = time.perf_counter(), t_cap[0]
        paused = step
        prev_idx = None
        for i, fr in enumerate(ses.iter_frames()):
            img = fr.image()
            gap = prev_idx is not None and fr.frame_index != prev_idx + 1
            dt_ms = (t_cap[i] - t_cap[i - 1]) / 1e6 if i else 0.0
            lines = [
                (f"REPLAY {ses.path.name}", GREEN),
                (f"frame {i + 1}/{n}  idx {fr.frame_index}  t = {(t_cap[i] - t_cap[0]) / 1e9:7.3f} s", GREY),
                (f"frame dt {dt_ms:5.1f} ms" + ("   GAP (missing frame index)" if gap else ""),
                 RED if gap else GREY),
            ]
            if al is not None and al["valid"][i]:
                gl, ac = al["gyro_lin"][i], al["accel_lin"][i]
                lines += [
                    (f"gyro@frame [rad/s] {gl[0]:+.3f} {gl[1]:+.3f} {gl[2]:+.3f}", GREY),
                    (f"accel@frame [m/s2] {ac[0]:+.2f} {ac[1]:+.2f} {ac[2]:+.2f}", GREY),
                    (f"nearest IMU sample {al['gyro_nn_dt_ns'][i] / 1e6:.1f} ms away (linear interp used)", GREY),
                ]
                if not np.isnan(al["drot_prev"][i, 0]):
                    lines.append((f"rotation since prev frame {np.degrees(np.linalg.norm(al['drot_prev'][i])):.2f} deg",
                                  GREY))
            else:
                lines.append(("no IMU coverage for this frame", YELLOW))
            lines.append((f"td (cam->imu shift) {td_ms:+.1f} ms   orientation {fr.orientation_deg} deg", GREY))
            lines.append(("keys: space pause, n step, q quit", GREY))

            canvas = compose(img, gyro_arr, accel_arr, t_cap[i] + td_ns, lines, disp_h,
                             cfg["viewer"]["plot_window_s"], marks_ns=marks)
            if export:
                if writer is None:
                    out_size = (canvas.shape[1], canvas.shape[0])
                    fps = n / max(1e-9, (t_cap[-1] - t_cap[0]) / 1e9) if n > 1 else 30.0
                    writer = cv2.VideoWriter(str(export), cv2.VideoWriter_fourcc(*"mp4v"), fps, out_size)
                if (canvas.shape[1], canvas.shape[0]) != out_size:
                    canvas = cv2.resize(canvas, out_size)
                writer.write(canvas)
            shown += 1
            prev_idx = fr.frame_index
            if headless:
                continue

            cv2.imshow(win, canvas)
            if paused:
                key = cv2.waitKey(0) & 0xFF
            else:
                due = wall0 + (t_cap[i] - t0) / 1e9 / speed
                key = cv2.waitKey(max(1, int((due - time.perf_counter()) * 1000))) & 0xFF
            if key in (ord("q"), 27):
                loop = False
                break
            if key == ord(" "):
                paused = not paused
                if not paused:  # re-anchor pacing after a pause
                    wall0, t0 = time.perf_counter(), t_cap[i]
            elif key == ord("n"):
                paused = True
        if not loop:
            break
    if writer is not None:
        writer.release()
        print(f"exported {shown} frames -> {export}")
    if not headless:
        cv2.destroyAllWindows()
    return shown

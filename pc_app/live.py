"""Live mode: ingest server + viewer (or headless console stats)."""
from __future__ import annotations

import logging
import time

import cv2
import numpy as np

from .clock import pc_now_ns
from .live_state import LiveState
from .server import PhoneServer
from .viewer import GREEN, GREY, RED, YELLOW, compose, fmt_ms, waiting_screen

log = logging.getLogger("pc_app.live")


def status_lines(snap: dict, server: PhoneServer) -> list:
    rec = server.recorder
    lines = []
    if snap["connected"]:
        lines.append((f"PHONE CONNECTED  session {snap['session_id']}  imu api: {snap['imu_api']}", GREEN))
    else:
        lines.append(("waiting for phone... open one of the URLs printed in the console", YELLOW))
    fps = "--" if snap["fps"] is None else f"{snap['fps']:.1f}"
    lines.append((f"video {fps} fps  {snap['mbps']:.1f} Mb/s  rx {snap['frames_received']}  "
                  f"missing idx {snap['frame_missing']}  phone-dropped {snap['phone_dropped']}",
                  GREEN if snap["frame_missing"] == 0 else YELLOW))
    hz = "  ".join(f"{k} {v:.0f} Hz" for k, v in sorted(snap["imu_hz"].items())) or "no IMU"
    miss = sum(snap["imu_missing"].values())
    lines.append((f"IMU {hz}  missing seq {miss}", GREEN if snap["imu_hz"] and miss == 0 else YELLOW))
    lines.append((f"latency capture->PC p50/p95 {fmt_ms(snap['lat_recv_p50'])}/{fmt_ms(snap['lat_recv_p95'])} ms",
                  GREY))
    lines.append((f"latency capture->display p50/p95 {fmt_ms(snap['lat_disp_p50'])}/"
                  f"{fmt_ms(snap['lat_disp_p95'])} ms", GREY))
    lines.append((f"clock: min RTT {fmt_ms(snap['rtt_min'], 1)} ms (offset error <= RTT/2)", GREY))
    stale = snap["stale_ms"]
    lines.append((f"newest frame age {'--' if stale is None else f'{stale:.0f}'} ms",
                  RED if stale is not None and stale > 500 else GREY))
    if rec.active:
        lines.append((f"REC -> {rec.dir.name}", RED))
    else:
        lines.append(("not recording", GREY))
    lines.append(("keys: r = start/stop recording   q = quit", GREY))
    return lines


def run_live(cfg: dict, record: bool = False, headless: bool = False, duration: float | None = None,
             https: bool = True, tunnel: bool = False, public_url: str | None = None) -> None:
    state = LiveState(window_s=cfg["viewer"]["plot_window_s"])
    server = PhoneServer(cfg, state, enable_https=https)
    server.start()
    tun = None
    public_url = public_url or cfg["tunnel"].get("public_url") or None
    if tunnel:
        from .tunnel import SshTunnel
        tun = SshTunnel(server.http_port, cfg["tunnel"]["ssh_host"])
        try:
            public_url = tun.start()
        except RuntimeError as e:
            server.stop()
            raise SystemExit(f"Could not open the tunnel: {e}")
    if public_url:
        server.set_public_url(public_url, cfg["tunnel"]["client"])
    print("\nOpen on the phone:")
    for u in server.urls:
        print("  ", u)
    _print_qr(server.phone_url)
    print()
    if record:
        server.set_recording(True)
    t0 = time.monotonic()
    try:
        if headless:
            _headless_loop(state, server, duration, t0)
        else:
            _viewer_loop(cfg, state, server, duration, t0)
    except KeyboardInterrupt:
        pass
    finally:
        if tun is not None:
            tun.stop()
        saved = server.stop()
        if saved:
            print(f"\nSaved session: {saved}")
            print(f"  replay : python -m pc_app replay \"{saved}\"")
            print(f"  report : python -m evaluation.session_report \"{saved}\"")


def _print_qr(url: str | None) -> None:
    """QR code of the phone address in the terminal (optional: needs `segno`)."""
    if not url:
        return
    try:
        import segno
        print(f"\nScan with the phone camera -> {url}")
        segno.make(url, error="m").terminal(compact=True)
    except Exception:  # segno missing, or the console cannot draw block characters
        pass


def _headless_loop(state, server, duration, t0):
    last = 0.0
    while duration is None or time.monotonic() - t0 < duration:
        time.sleep(0.1)
        if time.monotonic() - last > 2.0:
            last = time.monotonic()
            print(" | ".join(txt for txt, _ in status_lines(state.snapshot(), server)[:6]), flush=True)


def _viewer_loop(cfg, state: LiveState, server: PhoneServer, duration, t0):
    win = "phone perception - live"
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    last_idx, img, last_render = None, None, 0.0
    placeholder = waiting_screen(server.phone_url, cfg["viewer"]["display_height"],
                                 public=server.access_key is not None)
    while duration is None or time.monotonic() - t0 < duration:
        with state.lock:
            lf = state.latest_frame
        new = lf is not None and lf[0].frame_index != last_idx
        if not new and time.monotonic() - last_render < 1 / 30:
            if _handle_key(cv2.waitKey(5), server, win):
                break
            continue
        if new:
            h, jpeg, t_recv = lf
            dec = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if dec is not None:
                img = dec
            last_idx = h.frame_index
        gyro, accel = state.imu_arrays("gyro"), state.imu_arrays("accel")
        ends = [a[-1, 0] for a in (gyro, accel) if len(a)]
        t_end = max(ends) if ends else (lf[0].t_capture_ns if lf else 0)
        canvas = compose(img if img is not None else placeholder, gyro, accel, t_end,
                         status_lines(state.snapshot(), server),
                         cfg["viewer"]["display_height"], cfg["viewer"]["plot_window_s"],
                         recording=server.recorder.active)
        cv2.imshow(win, canvas)
        key = cv2.waitKey(1)
        last_render = time.monotonic()
        if new:
            t_disp = pc_now_ns()
            state.on_display(h, t_disp)
            server.recorder.display(h.frame_index, t_recv, t_disp)
        if _handle_key(key, server, win):
            break
    cv2.destroyAllWindows()


def _handle_key(key: int, server: PhoneServer, win: str) -> bool:
    key &= 0xFF
    if key in (ord("q"), 27):
        return True
    if key == ord("r"):
        server.set_recording(not server.recorder.active)
    try:
        return cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1
    except cv2.error:
        return True

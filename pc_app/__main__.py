"""python -m pc_app {live, replay, calibrate} ..."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import load_config


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m pc_app")
    ap.add_argument("--config", help="TOML file overriding configs/default.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lv = sub.add_parser("live", help="receive, display and record the phone stream")
    lv.add_argument("--record", action="store_true", help="start recording as soon as a phone connects")
    lv.add_argument("--headless", action="store_true", help="no window; print stats to the console")
    lv.add_argument("--duration", type=float, help="stop after N seconds")
    lv.add_argument("--no-https", action="store_true", help="only serve plain HTTP on localhost")
    lv.add_argument("--tunnel", action="store_true",
                    help="public https link via an SSH tunnel (localhost.run): phone on any network, no Wi-Fi needed")
    lv.add_argument("--public-url", help="public https address of a tunnel you started yourself "
                                         "(e.g. VS Code port forwarding of port 8080)")

    rp = sub.add_parser("replay", help="deterministic replay of a recorded session")
    rp.add_argument("session")
    rp.add_argument("--speed", type=float, default=1.0)
    rp.add_argument("--step", action="store_true", help="start paused; any key advances one frame")
    rp.add_argument("--loop", action="store_true")
    rp.add_argument("--export", help="write the rendered replay to an .mp4")
    rp.add_argument("--headless", action="store_true", help="no window (use with --export)")
    rp.add_argument("--td-ms", type=float, default=0.0, help="camera->IMU time shift (t_imu = t_cam + td)")

    cb = sub.add_parser("calibrate", help="checkerboard intrinsics calibration")
    cb.add_argument("--make-board", metavar="PNG", help="write a printable/displayable checkerboard and exit")
    cb.add_argument("--session", type=Path)
    cb.add_argument("--images", type=Path)
    cb.add_argument("--inner", default="9x6", help="inner corners COLSxROWS (board from --make-board: 9x6)")
    cb.add_argument("--square-mm", type=float, default=25.0, help="measured square size")
    cb.add_argument("--every", type=int, default=10, help="use every N-th frame of a session")
    cb.add_argument("--out", type=Path)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("numexpr").setLevel(logging.WARNING)
    cfg = load_config(a.config)

    if a.cmd == "live":
        from .live import run_live
        run_live(cfg, record=a.record, headless=a.headless, duration=a.duration, https=not a.no_https,
                 tunnel=a.tunnel, public_url=a.public_url)
    elif a.cmd == "replay":
        from .replay import run_replay
        run_replay(a.session, cfg, a.speed, a.step, a.loop, a.export, a.headless, a.td_ms)
    elif a.cmd == "calibrate":
        from .calibrate import calibrate, make_board
        if a.make_board:
            make_board(Path(a.make_board))
            return
        if not (a.session or a.images):
            ap.error("calibrate needs --session or --images (or --make-board)")
        c, r = (int(v) for v in a.inner.lower().split("x"))
        calibrate(a.session, a.images, c, r, a.square_mm, a.every, a.out)


if __name__ == "__main__":
    main()

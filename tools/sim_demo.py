"""One-command pipeline check without a phone: server + simulated phone + record + report.

    python tools/sim_demo.py --duration 20 [--imu-drop 0.01 --frame-drop 0.02]

Prints the recorded session path; the report lands in <session>/report/.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pc_app.config import load_config  # noqa: E402
from pc_app.recorder import Recorder  # noqa: E402
from pc_app.server import PhoneServer  # noqa: E402
from tools.fake_phone import FakePhone, build_parser  # noqa: E402


def run(duration: float, sessions_dir: Path | None = None, report: bool = True, sim_args: list[str] = (),
        scenario: str = "sim", bench: Path | None = None) -> Path:
    cfg = copy.deepcopy(load_config())
    cfg["server"]["http_port"] = 0  # any free port
    rec = Recorder(sessions_dir) if sessions_dir else None
    server = PhoneServer(cfg, recorder=rec, enable_https=False)
    server.start()
    server.set_recording(True)  # starts as soon as the simulated phone says hello
    try:
        a = build_parser().parse_args([f"--url=ws://127.0.0.1:{server.http_port}/ws/phone",
                                       f"--duration={duration}", *sim_args])
        asyncio.run(FakePhone(a).run())
        time.sleep(0.3)
    finally:
        path = server.stop()
    if path is None:
        raise RuntimeError("nothing was recorded")
    print(f"recorded {path}")
    if report:
        from evaluation.session_report import BENCH_CSV, report as make_report
        make_report(path, scenario, bench=bench or BENCH_CSV)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--no-report", action="store_true")
    a, rest = ap.parse_known_args()
    run(a.duration, report=not a.no_report, sim_args=rest)

"""Simulated phone: speaks the exact phone_client protocol with KNOWN ground truth.

Used to test the PC pipeline end to end without a device and to check that the
estimators recover what was injected:

* phone clock = PC clock + offset + drift  (truth for pc_app/clock.py)
* camera timestamps late by --cam-delay-ms (truth td = -delay for sync_check)
* rotation-only handheld motion: gyro = omega + bias + noise; the image is a
  textured plane shifted by the matching pixel motion (focal length --focal-px)
* optional random IMU / frame drops (truth for the drop counters)

The truth is sent in the hello message (``sim_truth``) and lands in manifest.json.
Offset truth assumes the simulator and server share the PC clock
(perf_counter_ns is system-wide on Windows and Linux).

    python tools/fake_phone.py --duration 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pc_app.protocol import FrameHeader, pack_frame  # noqa: E402
from pc_app.sync import R_camera_from_body, so3_exp  # noqa: E402

G = 9.81


class Motion:
    """Precomputed 1 kHz rotation-only trajectory."""

    def __init__(self, duration_s: float, focal_px: float, orientation: int, seed: int, amp: float = 1.0):
        rng = np.random.default_rng(seed)
        self.dt = 1e-3
        n = int((duration_s + 2) / self.dt)
        t = np.arange(n) * self.dt
        ph = rng.uniform(0, 2 * np.pi, 6)
        ramp = np.clip((t - 2.0) / 1.0, 0, 1)  # still for 2 s (stationary segment), then 1 s ramp-up
        w = amp * ramp[:, None] * np.stack([
            0.5 * np.sin(2 * np.pi * 0.40 * t + ph[0]) + 0.25 * np.sin(2 * np.pi * 1.1 * t + ph[1]),
            0.6 * np.sin(2 * np.pi * 0.30 * t + ph[2]) + 0.30 * np.sin(2 * np.pi * 0.9 * t + ph[3]),
            0.08 * np.sin(2 * np.pi * 0.50 * t + ph[4]),
        ], axis=1)
        self.t, self.w = t, w
        # image velocity of a far scene under pure rotation (camera frame): u' = -f w_Cy, v' = f w_Cx
        wc = w @ R_camera_from_body(orientation).T
        integ = lambda r: np.concatenate([[0.0], np.cumsum(0.5 * (r[1:] + r[:-1]))]) * self.dt  # value at t[i]
        self.u = integ(-focal_px * wc[:, 1])
        self.v = integ(focal_px * wc[:, 0])
        # orientation for gravity: world z up; phone upright portrait at start (body y = up)
        R = np.array([[1.0, 0, 0], [0, 0, -1], [0, 1, 0]])
        self.grav_b = np.empty((n, 3))
        for i in range(n):
            self.grav_b[i] = R.T @ np.array([0, 0, G])  # specific force at rest = +g up, in body frame
            R = R @ so3_exp(w[i] * self.dt)

    def at(self, t_s: float) -> int:
        return min(int(round(t_s / self.dt)), len(self.t) - 1)


class FakePhone:
    def __init__(self, a):
        self.a = a
        self.rng = np.random.default_rng(a.seed)
        self.pc0 = time.perf_counter_ns()
        self.phone_epoch = int(5_000e6 + self.rng.uniform(0, 1e9))  # phone clock reading at pc0
        self.drift = a.drift_ppm * 1e-6
        self.motion = Motion(a.duration, a.focal_px, a.orientation, a.seed, a.motion_amp)
        tex = self.rng.integers(0, 255, (a.height + 1400, a.width + 1400), np.uint8)
        tex = cv2.GaussianBlur(tex, (0, 0), 3)
        tex = cv2.normalize(tex, None, 0, 255, cv2.NORM_MINMAX)
        self.texture = cv2.cvtColor(tex, cv2.COLOR_GRAY2BGR)
        self.bias = np.array([0.002, -0.003, 0.01])
        self.sid = f"sim{a.seed:04d}"
        self.counters = dict(frames_captured=0, frames_sent=0, frames_dropped_busy=0,
                             frames_dropped_backpressure=0, imu_samples=0)

    def phone_ns(self, pc_ns: int) -> int:
        return self.phone_epoch + int((pc_ns - self.pc0) * (1 + self.drift))

    def true_s(self, pc_ns: int) -> float:
        return (pc_ns - self.t_start) / 1e9

    def render(self, t_s: float) -> np.ndarray:
        i = self.motion.at(t_s)
        x0 = int(700 - self.motion.u[i])
        y0 = int(700 - self.motion.v[i])
        x0 = int(np.clip(x0, 0, self.texture.shape[1] - self.a.width))
        y0 = int(np.clip(y0, 0, self.texture.shape[0] - self.a.height))
        img = self.texture[y0:y0 + self.a.height, x0:x0 + self.a.width].copy()
        cv2.putText(img, f"SIMULATED t={t_s:6.2f}s", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        return img

    def hello(self) -> dict:
        a = self.a
        return {
            "type": "hello", "protocol": 1, "session_id": self.sid, "user_agent": "fake_phone.py simulator",
            "platform": "sim", "clock": "simulated", "imu_api": "simulated", "imu_hz_requested": a.imu_hz,
            "frame_timestamp": "simulated", "orientation_deg": a.orientation,
            "video": {"requested": {"width": a.width, "height": a.height, "fps": a.fps}, "jpeg_quality": 0.8},
            "sim_truth": {
                "pc0_ns": self.pc0, "phone_at_pc0_ns": self.phone_epoch, "drift_ppm": a.drift_ppm,
                "offset_at_pc0_ns": self.phone_epoch - self.pc0, "td_ms": -a.cam_delay_ms,
                "focal_px": a.focal_px, "gyro_bias": self.bias.tolist(), "imu_drop": a.imu_drop,
                "frame_drop": a.frame_drop,
            },
        }

    async def run(self) -> None:
        import aiohttp
        a = self.a
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(a.url, max_msg_size=32 << 20) as ws:
                await ws.send_json(self.hello())
                self.t_start = time.perf_counter_ns()
                stop = asyncio.Event()
                tasks = [asyncio.create_task(c) for c in
                         (self._imu(ws, stop), self._frames(ws, stop), self._recv(ws, stop), self._status(ws, stop))]
                await asyncio.sleep(a.duration)
                stop.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _sleep_until(self, pc_ns: int) -> None:
        d = (pc_ns - time.perf_counter_ns()) / 1e9
        if d > 0:
            await asyncio.sleep(d)

    async def _imu(self, ws, stop) -> None:
        a, k, seq_g, seq_a, batch = self.a, 0, 0, 0, []
        period = 1e9 / a.imu_hz
        last_send = time.perf_counter_ns()
        while not stop.is_set():
            t_pc = self.t_start + int(k * period + self.rng.normal(0, 0.3e6))  # small sampling jitter
            k += 1
            await self._sleep_until(t_pc)
            i = self.motion.at(self.true_s(t_pc))
            gyr = self.motion.w[i] + self.bias + self.rng.normal(0, 0.005, 3)
            acc = self.motion.grav_b[i] + self.rng.normal(0, 0.05, 3)
            tp = self.phone_ns(t_pc)
            if self.rng.random() >= a.imu_drop:
                batch.append(["gyro", seq_g, tp, *map(float, gyr)])
            if self.rng.random() >= a.imu_drop:
                batch.append(["accel", seq_a, tp, *map(float, acc)])
            seq_g += 1
            seq_a += 1
            self.counters["imu_samples"] += 2
            if time.perf_counter_ns() - last_send > 40e6 and batch:
                await ws.send_str(json.dumps({"type": "imu", "session_id": self.sid, "samples": batch}))
                batch, last_send = [], time.perf_counter_ns()

    async def _frames(self, ws, stop) -> None:
        a, idx = self.a, 0
        period = 1e9 / a.fps
        while not stop.is_set():
            t_pc = self.t_start + int(idx * period)       # true capture instant
            t_avail = t_pc + int(a.cam_delay_ms * 1e6)     # frame reaches the page late ...
            await self._sleep_until(t_avail)
            img = self.render(self.true_s(t_pc))
            self.counters["frames_captured"] += 1
            if self.rng.random() < a.frame_drop:
                self.counters["frames_dropped_backpressure"] += 1
                idx += 1
                continue
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            t_cap = self.phone_ns(t_avail)                 # ... and is stamped when it arrives (late)
            h = FrameHeader(idx, t_cap, self.phone_ns(time.perf_counter_ns()), a.width, a.height, 4, a.orientation)
            await ws.send_bytes(pack_frame(h, jpg.tobytes()))
            self.counters["frames_sent"] += 1
            idx += 1

    async def _recv(self, ws, stop) -> None:
        while not stop.is_set():
            try:
                msg = await asyncio.wait_for(ws.receive(), 0.2)
            except asyncio.TimeoutError:
                continue
            if msg.type.name != "TEXT":
                if msg.type.name in ("CLOSED", "CLOSING", "ERROR"):
                    return
                continue
            t = self.phone_ns(time.perf_counter_ns())
            m = json.loads(msg.data)
            if m.get("type") == "ping":
                await ws.send_str(json.dumps({"type": "pong", "id": m["id"], "t": t}))

    async def _status(self, ws, stop) -> None:
        while not stop.is_set():
            await asyncio.sleep(1.0)
            await ws.send_str(json.dumps({"type": "status", "session_id": self.sid,
                                          "t_phone_ns": self.phone_ns(time.perf_counter_ns()), **self.counters,
                                          "ws_buffered_bytes": 0, "orientation_deg": self.a.orientation,
                                          "imu_api": "simulated"}))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:8080/ws/phone")
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--imu-hz", type=float, default=100.0)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=480)
    p.add_argument("--orientation", type=int, default=0, choices=[0, 90, 180, 270])
    p.add_argument("--focal-px", type=float, default=500.0)
    p.add_argument("--drift-ppm", type=float, default=40.0)
    p.add_argument("--cam-delay-ms", type=float, default=25.0)
    p.add_argument("--imu-drop", type=float, default=0.0, help="probability of losing an IMU sample")
    p.add_argument("--frame-drop", type=float, default=0.0, help="probability of dropping a frame")
    p.add_argument("--motion-amp", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=1)
    return p


def main(argv=None) -> None:
    a = build_parser().parse_args(argv)
    asyncio.run(FakePhone(a).run())


if __name__ == "__main__":
    main()

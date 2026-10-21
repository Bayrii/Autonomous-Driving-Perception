"""Read a recorded session for deterministic replay and offline analysis.

Replay order is defined only by recorded data (frame_index / phone capture
time), never by wall-clock or thread timing, so two replays of the same session
produce identical frame/IMU pairings.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
import pandas as pd

from .clock import ClockModel, fit_clock_model


@dataclass
class ReplayFrame:
    frame_index: int
    t_capture_phone_ns: int
    t_recv_pc_ns: int
    width: int
    height: int
    orientation_deg: int
    path: Path

    def image(self) -> np.ndarray:
        img = cv2.imread(str(self.path), cv2.IMREAD_COLOR)
        if img is None:
            raise IOError(f"cannot decode {self.path}")
        return img


class Session:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not (self.path / "frames.csv").exists():
            raise FileNotFoundError(f"{self.path} is not a session directory (no frames.csv)")
        mf = self.path / "manifest.json"
        self.manifest: dict = json.loads(mf.read_text()) if mf.exists() else {}

    def _csv(self, name: str) -> pd.DataFrame:
        p = self.path / f"{name}.csv"
        if not p.exists() or p.stat().st_size == 0:
            return pd.DataFrame()
        return pd.read_csv(p)

    @cached_property
    def frames(self) -> pd.DataFrame:
        df = self._csv("frames")
        return df.sort_values("frame_index", kind="stable").drop_duplicates("frame_index").reset_index(drop=True)

    @cached_property
    def imu(self) -> pd.DataFrame:
        return self._csv("imu")

    @cached_property
    def gnss(self) -> pd.DataFrame:
        return self._csv("gnss")

    @cached_property
    def clock_sync(self) -> pd.DataFrame:
        return self._csv("clock_sync")

    @cached_property
    def display(self) -> pd.DataFrame:
        return self._csv("display")

    @cached_property
    def status(self) -> pd.DataFrame:
        return self._csv("status")

    @cached_property
    def clock_model(self) -> ClockModel | None:
        cs = self.clock_sync
        if cs.empty:
            return None
        return fit_clock_model(cs.t_pc_send_ns, cs.t_phone_ns, cs.t_pc_recv_ns)

    def sensor(self, sensor_type: str) -> tuple[np.ndarray, np.ndarray]:
        """(t_phone_ns int64 [N], xyz float64 [N,3]) sorted by time, duplicates removed."""
        df = self.imu
        if df.empty:
            return np.zeros(0, np.int64), np.zeros((0, 3))
        df = df[df.sensor_type == sensor_type].sort_values("t_phone_ns", kind="stable")
        df = df.drop_duplicates("seq")
        return df.t_phone_ns.to_numpy(np.int64), df[["x", "y", "z"]].to_numpy(np.float64)

    def iter_frames(self) -> Iterator[ReplayFrame]:
        for r in self.frames.itertuples(index=False):
            yield ReplayFrame(int(r.frame_index), int(r.t_capture_phone_ns), int(r.t_recv_pc_ns),
                              int(r.width), int(r.height), int(r.orientation_deg), self.path / r.file)

"""PC clock and phone<->PC clock-offset estimation.

PC clock: ``time.perf_counter_ns()``. On Windows ``time.monotonic()`` ticks at
~15.6 ms, while perf_counter uses QueryPerformanceCounter (<1 us), so every PC
timestamp in this project comes from :func:`pc_now_ns`.

Offset estimation is NTP-style. The PC sends ``ping`` at t0 (PC clock), the
phone answers with its own clock reading tp, the PC receives the answer at t3.
Assuming symmetric transit, the phone read its clock at PC time (t0+t3)/2, so

    offset = tp - (t0 + t3) / 2       (phone_clock - pc_clock)
    |error| <= rtt / 2,  rtt = t3 - t0

Samples with small RTT are the most trustworthy (no queueing), so the live
estimate uses the minimum-RTT sample of a sliding window, and the offline model
fits offset = a + b * t_pc through the minimum-RTT sample of each time bin
(b = relative drift of the two oscillators).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np


def pc_now_ns() -> int:
    return time.perf_counter_ns()


@dataclass(frozen=True)
class SyncSample:
    ping_id: int
    t_pc_send_ns: int
    t_phone_ns: int
    t_pc_recv_ns: int

    @property
    def rtt_ns(self) -> int:
        return self.t_pc_recv_ns - self.t_pc_send_ns

    @property
    def t_pc_mid_ns(self) -> float:
        return (self.t_pc_send_ns + self.t_pc_recv_ns) / 2

    @property
    def offset_ns(self) -> float:
        return self.t_phone_ns - self.t_pc_mid_ns


class LiveClockSync:
    """Running offset estimate for live latency display (min RTT in a window)."""

    def __init__(self, window_s: float = 10.0):
        self.window_ns = int(window_s * 1e9)
        self.samples: deque[SyncSample] = deque()

    def add(self, s: SyncSample) -> None:
        self.samples.append(s)
        while self.samples and s.t_pc_recv_ns - self.samples[0].t_pc_recv_ns > self.window_ns:
            self.samples.popleft()

    def best(self) -> SyncSample | None:
        return min(self.samples, key=lambda s: s.rtt_ns) if self.samples else None

    def phone_to_pc(self, t_phone_ns: int) -> float | None:
        b = self.best()
        return None if b is None else t_phone_ns - b.offset_ns


@dataclass(frozen=True)
class ClockModel:
    """offset(t_pc) = a + b * (t_pc - t_ref); phone = pc + offset."""
    a_ns: float
    b: float
    t_ref_ns: float
    residual_std_ns: float
    min_rtt_ns: float
    n_used: int

    @property
    def drift_ppm(self) -> float:
        return self.b * 1e6

    @property
    def uncertainty_ns(self) -> float:
        """Bound on the absolute offset error from transit asymmetry."""
        return self.min_rtt_ns / 2

    def phone_to_pc(self, t_phone_ns):
        t_phone = np.asarray(t_phone_ns, dtype=np.float64)
        # phone = pc + a + b (pc - ref)  =>  pc = (phone - a + b ref) / (1 + b)
        return (t_phone - self.a_ns + self.b * self.t_ref_ns) / (1.0 + self.b)

    def pc_to_phone(self, t_pc_ns):
        t_pc = np.asarray(t_pc_ns, dtype=np.float64)
        return t_pc + self.a_ns + self.b * (t_pc - self.t_ref_ns)

    def to_dict(self) -> dict:
        return {
            "offset_ns_at_ref": self.a_ns, "drift_ppm": self.drift_ppm, "t_ref_pc_ns": self.t_ref_ns,
            "residual_std_us": self.residual_std_ns / 1e3, "min_rtt_ms": self.min_rtt_ns / 1e6,
            "uncertainty_ms": self.uncertainty_ns / 1e6, "n_used": self.n_used,
        }


def fit_clock_model(t_pc_send, t_phone, t_pc_recv, bin_s: float = 2.0,
                    min_bins_for_drift: int = 3) -> ClockModel:
    """Fit offset(t_pc) from ping/pong samples using the min-RTT sample per bin."""
    t0 = np.asarray(t_pc_send, dtype=np.float64)
    tp = np.asarray(t_phone, dtype=np.float64)
    t3 = np.asarray(t_pc_recv, dtype=np.float64)
    if len(t0) == 0:
        raise ValueError("no clock-sync samples")
    rtt = t3 - t0
    mid = (t0 + t3) / 2
    off = tp - mid

    bins = np.floor((mid - mid.min()) / (bin_s * 1e9)).astype(int)
    keep = [np.flatnonzero(bins == b)[np.argmin(rtt[bins == b])] for b in np.unique(bins)]
    keep = np.asarray(keep)
    t_ref = float(mid[keep].mean())

    if len(keep) >= min_bins_for_drift:
        x = mid[keep] - t_ref
        b, a = np.polyfit(x, off[keep], 1)
        resid = off[keep] - (a + b * x)
    else:  # too short to see drift: constant offset from the single best sample
        best = keep[np.argmin(rtt[keep])]
        a, b = float(off[best]), 0.0
        resid = off[keep] - a
    return ClockModel(float(a), float(b), t_ref, float(np.std(resid)), float(rtt.min()), len(keep))

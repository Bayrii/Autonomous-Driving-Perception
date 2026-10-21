"""OpenCV viewer shared by live mode and replay: video | IMU plots | status bar."""
from __future__ import annotations

import time

import cv2
import numpy as np

AXIS_COLORS = [(229, 135, 57), (38, 89, 217), (112, 158, 25)]  # x blue, y orange, z aqua (BGR, dark-surface steps)
PLOT_W = 480
STATUS_H = 120
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _plot(arr: np.ndarray, t_end_ns: float, window_s: float, size: tuple[int, int], title: str,
          unit: str, min_range: float, marks_ns=()) -> np.ndarray:
    """arr: [N, 4] rows (t_ns, x, y, z). Draws the last window_s seconds ending at t_end_ns."""
    w, h = size
    img = np.full((h, w, 3), 24, np.uint8)
    m_l, m_r, m_t, m_b = 46, 8, 22, 18
    pw, ph = w - m_l - m_r, h - m_t - m_b
    sel = arr[(arr[:, 0] > t_end_ns - window_s * 1e9) & (arr[:, 0] <= t_end_ns)] if len(arr) else arr
    rng = max(min_range, float(np.abs(sel[:, 1:]).max()) * 1.1) if len(sel) else min_range

    def to_px(t, v):
        x = m_l + (1 - (t_end_ns - t) / (window_s * 1e9)) * pw
        y = m_t + (0.5 - v / (2 * rng)) * ph
        return np.stack([x, y], -1)

    cv2.rectangle(img, (m_l, m_t), (m_l + pw, m_t + ph), (70, 70, 70), 1)
    cv2.line(img, (m_l, m_t + ph // 2), (m_l + pw, m_t + ph // 2), (70, 70, 70), 1)
    for tm in marks_ns:  # frame times (shows frame <-> IMU alignment)
        if t_end_ns - window_s * 1e9 < tm <= t_end_ns:
            x = int(to_px(np.array(tm), np.array(0.0))[0])
            cv2.line(img, (x, m_t + ph - 6), (x, m_t + ph), (120, 120, 120), 1)
    for k in range(3):
        if len(sel) > 1:
            pts = to_px(sel[:, 0], sel[:, k + 1]).astype(np.int32)
            cv2.polylines(img, [pts], False, AXIS_COLORS[k], 1, cv2.LINE_AA)
    cv2.putText(img, f"{title} [{unit}]", (m_l, 15), FONT, 0.45, (220, 220, 220), 1, cv2.LINE_AA)
    for k, n in enumerate("xyz"):
        val = f"{n}={sel[-1, k + 1]:+.2f}" if len(sel) else f"{n}=--"
        cv2.putText(img, val, (w - 230 + 75 * k, 15), FONT, 0.42, AXIS_COLORS[k], 1, cv2.LINE_AA)
    cv2.putText(img, f"{rng:.1f}", (4, m_t + 10), FONT, 0.38, (160, 160, 160), 1)
    cv2.putText(img, f"{-rng:.1f}", (4, m_t + ph), FONT, 0.38, (160, 160, 160), 1)
    cv2.putText(img, f"-{window_s:.0f}s", (m_l, h - 4), FONT, 0.38, (160, 160, 160), 1)
    return img


def compose(frame: np.ndarray | None, gyro: np.ndarray, accel: np.ndarray, t_end_ns: float,
            status_lines: list[tuple[str, tuple[int, int, int]]], display_h: int = 540,
            window_s: float = 5.0, recording: bool = False, banner: str = "", marks_ns=()) -> np.ndarray:
    if frame is None:
        vid = np.zeros((display_h, int(display_h * 16 / 9), 3), np.uint8)
        cv2.putText(vid, "waiting for video...", (30, display_h // 2), FONT, 1.0, (200, 200, 200), 2)
    else:
        s = display_h / frame.shape[0]
        vid = cv2.resize(frame, (max(1, int(frame.shape[1] * s)), display_h), interpolation=cv2.INTER_AREA)
    ph = display_h // 2
    plots = np.vstack([
        _plot(gyro, t_end_ns, window_s, (PLOT_W, ph), "gyroscope", "rad/s", 1.0, marks_ns),
        _plot(accel, t_end_ns, window_s, (PLOT_W, display_h - ph), "accelerometer", "m/s^2", 12.0, marks_ns),
    ])
    top = np.hstack([vid, plots])
    status = np.full((STATUS_H, top.shape[1], 3), 16, np.uint8)
    col_w = top.shape[1] // 2
    for i, (txt, color) in enumerate(status_lines):
        x = 10 + (i // 5) * col_w
        y = 22 + (i % 5) * 21
        cv2.putText(status, txt, (x, y), FONT, 0.5, color, 1, cv2.LINE_AA)
    out = np.vstack([top, status])
    if recording and int(time.monotonic() * 2) % 2 == 0:
        cv2.circle(out, (24, 24), 10, (0, 0, 255), -1)
        cv2.putText(out, "REC", (40, 31), FONT, 0.7, (0, 0, 255), 2)
    if banner:
        cv2.putText(out, banner, (10, display_h - 12), FONT, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(out, banner, (10, display_h - 12), FONT, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
    return out


def qr_image(text: str, module_px: int = 8) -> np.ndarray | None:
    """QR code as a BGR image (white quiet zone), or None if `segno` is not installed."""
    try:
        import segno
    except ImportError:
        return None
    m = np.array([list(r) for r in segno.make(text, error="m").matrix], np.uint8)
    m = np.pad(m, 4)  # quiet zone
    img = np.where(m[..., None] == 1, 0, 255).astype(np.uint8).repeat(3, axis=2)
    return cv2.resize(img, None, fx=module_px, fy=module_px, interpolation=cv2.INTER_NEAREST)


def waiting_screen(url: str | None, height: int = 540, public: bool = False) -> np.ndarray:
    """Placeholder shown until the first frame arrives: QR code + address for the phone."""
    w = int(height * 16 / 9)
    img = np.full((height, w, 3), 24, np.uint8)
    if not url:
        cv2.putText(img, "waiting for video...", (30, height // 2), FONT, 1.0, (200, 200, 200), 2)
        return img
    qr = qr_image(url)
    x_text = 30
    if qr is not None:
        s = min(1.0, (height - 60) / qr.shape[0])
        qr = cv2.resize(qr, None, fx=s, fy=s, interpolation=cv2.INTER_NEAREST)
        y0 = (height - qr.shape[0]) // 2
        img[y0:y0 + qr.shape[0], 30:30 + qr.shape[1]] = qr
        x_text = 30 + qr.shape[1] + 30
    addr = url.split("?", 1)
    addr_lines = [addr[0]] + (["?" + addr[1]] if len(addr) > 1 else [])  # long links on two lines
    if public:
        hint = ["Public link: works on any network,", "including mobile data.", "Tap Start streaming."]
    else:
        hint = ["Same Wi-Fi as this PC.", "Accept the certificate warning,", "then tap Start streaming."]
    lines = ["Scan with the phone camera,", "or type this address:", "", *addr_lines, "", *hint]
    for i, t in enumerate(lines):
        bold = t in addr_lines
        cv2.putText(img, t, (x_text, 150 + 36 * i), FONT, 0.75 if bold else 0.6,
                    (255, 255, 255) if bold else (200, 200, 200), 2 if bold else 1, cv2.LINE_AA)
    return img


def fmt_ms(ns, digits=0) -> str:
    return "--" if ns is None else f"{ns / 1e6:.{digits}f}"


GREEN, YELLOW, RED, GREY = (120, 220, 120), (60, 210, 230), (80, 80, 240), (200, 200, 200)

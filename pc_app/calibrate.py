"""Checkerboard camera calibration (intrinsics K + distortion) from a recorded
session or a folder of images.

Procedure (docs/week1/sync_and_calibration.md):
  1. python -m pc_app calibrate --make-board board.png    (show full-screen on a monitor, or print)
  2. record a session with the SAME resolution/orientation you will use on the road,
     moving the phone so the board covers centre, corners and edges at several tilts
  3. python -m pc_app calibrate --session sessions/<id> --square-mm <measured size>
Intrinsics are only valid for that resolution and orientation (and focus distance).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np


def make_board(out: Path, cols: int = 10, rows: int = 7, square_px: int = 120) -> None:
    """Board with cols x rows *squares* -> (cols-1) x (rows-1) inner corners."""
    m = square_px
    img = np.full((rows * m + 2 * m, cols * m + 2 * m), 255, np.uint8)
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                img[m + r * m: m + (r + 1) * m, m + c * m: m + (c + 1) * m] = 0
    cv2.imwrite(str(out), img)
    print(f"wrote {out}: {cols}x{rows} squares = {cols - 1}x{rows - 1} inner corners. "
          f"Measure the printed/displayed square size in mm.")


def _images(session: Path | None, images: Path | None, every: int):
    if session is not None:
        from .session import Session
        ses = Session(session)
        for i, fr in enumerate(ses.iter_frames()):
            if i % every == 0:
                yield fr.path.name, fr.image()
    else:
        for p in sorted(images.iterdir()):
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"):
                img = cv2.imread(str(p))
                if img is not None:
                    yield p.name, img


def calibrate(session: Path | None, images: Path | None, inner_cols: int, inner_rows: int, square_mm: float,
              every: int = 10, out: Path | None = None, max_views: int = 60) -> dict:
    pattern = (inner_cols, inner_rows)
    objp = np.zeros((inner_cols * inner_rows, 3), np.float32)
    objp[:, :2] = np.mgrid[0:inner_cols, 0:inner_rows].T.reshape(-1, 2) * (square_mm / 1000.0)
    obj_pts, img_pts, names, size = [], [], [], None
    for name, img in _images(session, images, every):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if size is None:
            size = gray.shape[::-1]
        elif gray.shape[::-1] != size:
            continue  # orientation/resolution change -> different intrinsics
        ok, corners = cv2.findChessboardCornersSB(gray, pattern, flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
        if ok:
            obj_pts.append(objp)
            img_pts.append(corners.astype(np.float32))
            names.append(name)
    print(f"board found in {len(img_pts)} images")
    if len(img_pts) < 8:
        raise SystemExit("need >= 8 views with a detected board (move the board across the whole image)")
    if len(img_pts) > max_views:  # evenly subsample to keep calibration fast
        sel = np.linspace(0, len(img_pts) - 1, max_views).round().astype(int)
        obj_pts, img_pts, names = [obj_pts[i] for i in sel], [img_pts[i] for i in sel], [names[i] for i in sel]

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(obj_pts, img_pts, size, None, None)
    per_view = []
    for o, ip, rv, tv in zip(obj_pts, img_pts, rvecs, tvecs):
        proj, _ = cv2.projectPoints(o, rv, tv, K, dist)
        per_view.append(float(np.sqrt(np.mean(np.sum((proj - ip) ** 2, axis=-1)))))
    fov_x = 2 * np.degrees(np.arctan(size[0] / (2 * K[0, 0])))
    fov_y = 2 * np.degrees(np.arctan(size[1] / (2 * K[1, 1])))
    result = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(session or images),
        "image_size": list(size),
        "K": K.tolist(), "dist": dist.ravel().tolist(),
        "dist_model": "OpenCV (k1, k2, p1, p2, k3)",
        "rms_reprojection_px": float(rms),
        "per_view_rms_px": dict(zip(names, per_view)),
        "n_views": len(img_pts),
        "board": {"inner_corners": list(pattern), "square_mm": square_mm},
        "fov_deg": [float(fov_x), float(fov_y)],
        "note": "valid only for this resolution/orientation; RMS < 0.5 px is good, > 1 px means re-capture",
    }
    out = out or ((session or images) / "calibration.json")
    Path(out).write_text(json.dumps(result, indent=2))
    print(f"RMS reprojection error {rms:.3f} px, fx={K[0, 0]:.1f} fy={K[1, 1]:.1f} "
          f"cx={K[0, 2]:.1f} cy={K[1, 2]:.1f}, FOV {fov_x:.1f}x{fov_y:.1f} deg -> {out}")
    return result

"""Calibration on synthetic views rendered with a known pinhole camera."""
import cv2
import numpy as np

from pc_app.calibrate import calibrate, make_board
from pc_app.sync import so3_exp


def test_calibration_recovers_known_intrinsics(tmp_path):
    sq_px, sq_m = 120, 0.025
    make_board(tmp_path / "board.png", cols=10, rows=7, square_px=sq_px)
    board = cv2.imread(str(tmp_path / "board.png"), cv2.IMREAD_GRAYSCALE)
    K = np.array([[900.0, 0, 640], [0, 900.0, 360], [0, 0, 1]])
    # board pixel -> metres on the plane, origin at the first inner corner (2 squares in incl. margin)
    S = np.array([[sq_m / sq_px, 0, -2 * sq_m], [0, sq_m / sq_px, -2 * sq_m], [0, 0, 1]])
    rng = np.random.default_rng(3)
    views = tmp_path / "views"
    views.mkdir()
    for i in range(14):
        R = so3_exp(rng.uniform(-0.45, 0.45, 3))
        t = np.array([rng.uniform(-0.12, 0.0), rng.uniform(-0.08, 0.0), rng.uniform(0.35, 0.55)])
        H = K @ np.column_stack([R[:, 0], R[:, 1], t]) @ S
        img = cv2.warpPerspective(board, H, (1280, 720), borderValue=128)
        cv2.imwrite(str(views / f"{i:02d}.png"), img)
    res = calibrate(None, views, 9, 6, sq_m * 1000, out=tmp_path / "calib.json")
    Kest = np.array(res["K"])
    assert res["rms_reprojection_px"] < 0.5
    assert abs(Kest[0, 0] - 900) < 15 and abs(Kest[1, 1] - 900) < 15
    assert abs(Kest[0, 2] - 640) < 15 and abs(Kest[1, 2] - 360) < 15

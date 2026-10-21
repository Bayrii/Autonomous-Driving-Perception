"""JPEG validation: header parsing for exact dimensions (cheap), plus an optional
OpenCV reduced-size decode to reject corrupt data. Render's free tier has ~0.1 CPU,
so a full-resolution decode of every frame is avoided."""
from __future__ import annotations

import cv2
import numpy as np

_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


class JpegError(ValueError):
    pass


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """(width, height) from the first SOF segment. Raises JpegError."""
    n = len(data)
    if n < 128 or data[:2] != b"\xff\xd8":
        raise JpegError("not a JPEG (missing start-of-image marker)")
    if data[-2:] != b"\xff\xd9":
        raise JpegError("truncated JPEG (missing end-of-image marker)")
    i = 2
    while i + 4 <= n:
        if data[i] != 0xFF:
            raise JpegError("corrupt JPEG marker structure")
        marker = data[i + 1]
        if marker == 0xFF:          # fill byte
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xD9, 0xDA):  # end of image / start of scan before any frame header
            break
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if seg < 2:
            raise JpegError("corrupt JPEG segment length")
        if marker in _SOF and i + 9 <= n:
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            if w == 0 or h == 0:
                raise JpegError("JPEG with zero dimension")
            return w, h
        i += 2 + seg
    raise JpegError("JPEG frame header not found")


def decodes(data: bytes) -> bool:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_8)
    return img is not None and img.size > 0

"""Blur check for photos, replacing the WhatsApp group's "blur kion hay
itne" moment. Laplacian variance is a standard cheap blur proxy: a sharp
image has a lot of high-frequency edge content, so the variance of its
Laplacian is high; a blurry one is low. Implemented with Pillow + numpy
instead of OpenCV to keep the dependency footprint small on Windows.
"""
import io

import numpy as np
from PIL import Image, ImageFilter

from config import CONFIG

# Discrete Laplacian kernel (4-neighbor), same shape OpenCV's cv2.Laplacian
# produces with default settings.
_LAPLACIAN_KERNEL = ImageFilter.Kernel(
    (3, 3),
    [0, 1, 0, 1, -4, 1, 0, 1, 0],
    scale=1,
)


def laplacian_variance(image_bytes: bytes) -> float:
    with Image.open(io.BytesIO(image_bytes)) as img:
        gray = img.convert("L")
        edges = gray.filter(_LAPLACIAN_KERNEL)
        arr = np.asarray(edges, dtype=np.float64)
        return float(arr.var())


def is_blurry(image_bytes: bytes, threshold: float | None = None) -> tuple[bool, float]:
    threshold = CONFIG.blur_variance_threshold if threshold is None else threshold
    try:
        variance = laplacian_variance(image_bytes)
    except Exception:
        # Not a decodable image (or corrupt) — don't block the upload on it.
        return False, -1.0
    return variance < threshold, variance

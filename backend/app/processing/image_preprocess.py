"""Image loading, resource-limit checks and OpenCV preprocessing for OCR."""
import logging
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
from PIL import Image, ImageSequence

from app.core.config import settings
from app.processing.errors import DocumentError, ErrorCode

logger = logging.getLogger(__name__)

def check_image_limits(file_path: Path) -> int:
    """
    Validate an image file against MAX_IMAGE_PIXELS / MAX_PAGES using only the header
    (nothing is decoded), so a decompression bomb is rejected cheaply. Returns the
    frame count. Raises DocumentError(FILE_TOO_LARGE) or CORRUPT_FILE.
    """
    try:
        with Image.open(file_path) as pil:
            frames = getattr(pil, "n_frames", 1)
            if frames > settings.MAX_PAGES:
                raise DocumentError(
                    ErrorCode.FILE_TOO_LARGE,
                    f"image has {frames} frames, limit is {settings.MAX_PAGES}",
                )
            for i in range(frames):
                pil.seek(i)
                w, h = pil.size
                if w * h > settings.MAX_IMAGE_PIXELS:
                    raise DocumentError(
                        ErrorCode.FILE_TOO_LARGE,
                        f"frame {i + 1} is {w}x{h} px, limit is {settings.MAX_IMAGE_PIXELS} px",
                    )
            return frames
    except DocumentError:
        raise
    except Image.DecompressionBombError as e:
        raise DocumentError(ErrorCode.FILE_TOO_LARGE, f"decompression bomb: {e}") from e
    except Exception as e:
        raise DocumentError(ErrorCode.CORRUPT_FILE, f"Cannot read image: {e!r}") from e


def _load_image_from_bytes(img_bytes: bytes) -> np.ndarray:
    """Load image bytes into an OpenCV ndarray."""
    nparr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Failed to decode image bytes")
    return img


def _load_image_from_path(file_path: Path) -> np.ndarray:
    img = cv2.imread(str(file_path))
    if img is None:
        raise ValueError(f"Failed to read image: {file_path}")
    return img


def _to_grayscale(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _deskew(gray: np.ndarray) -> np.ndarray:
    """
    Correct rotation/skew using Hough line detection.
    Returns the deskewed grayscale image.
    """
    # Edge detection
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    # Hough line transform
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=100, minLineLength=100, maxLineGap=10)
    if lines is None or len(lines) == 0:
        return gray

    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if x2 - x1 != 0:
            angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            if abs(angle) < 45:
                angles.append(angle)

    if not angles:
        return gray

    median_angle = float(np.median(angles))
    if abs(median_angle) < 0.3:
        return gray  # negligible skew

    logger.debug(f"Deskewing by {median_angle:.2f}°")
    h, w = gray.shape
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
    rotated = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=255)
    return rotated


def _denoise_and_threshold(gray: np.ndarray) -> np.ndarray:
    """
    Apply Gaussian blur + adaptive threshold to clean up scan noise.
    """
    # Mild Gaussian blur to reduce scanner noise
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    # Adaptive threshold: local areas handled independently (good for uneven lighting)
    binary = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        blockSize=15,
        C=8,
    )
    return binary


def _upscale_if_needed(img: np.ndarray) -> np.ndarray:
    """
    Upscale small images. For scanned docs < ~1600px wide, upscale 2x for OCR.
    """
    h, w = img.shape[:2]
    if w < 1600:
        scale = 2.0
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
        logger.debug(f"Upscaled image from {w}px to {int(w * scale)}px width")
    return img


def iter_image_frames(file_path: Path) -> Iterator[np.ndarray]:
    """
    Yield each frame of an image as a BGR ndarray, one at a time.
    Multi-page TIFFs yield one frame per page; other formats yield one frame.
    (cv2.imread only reads the first TIFF page, so Pillow is used for frames.)
    """
    check_image_limits(file_path)
    with Image.open(file_path) as pil:
        for frame in ImageSequence.Iterator(pil):
            rgb = np.array(frame.convert("RGB"))
            yield cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def count_image_frames(file_path: Path) -> int:
    return check_image_limits(file_path)


def preprocess(img: np.ndarray) -> np.ndarray:
    """Standard variant: grayscale -> deskew -> denoise/threshold -> upscale."""
    gray = _deskew(_to_grayscale(img))
    return _upscale_if_needed(_denoise_and_threshold(gray))

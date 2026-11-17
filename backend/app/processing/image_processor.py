import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageSequence

from app.core.config import settings

logger = logging.getLogger(__name__)

OCR_LANGS = settings.OCR_LANGS


@dataclass
class OCRResult:
    text: str
    confidence: float   # 0.0 - 1.0
    duration_ms: int


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


def _run_tesseract(img: np.ndarray, langs: str = OCR_LANGS) -> tuple[str, float]:
    """
    Run Tesseract OCR once on a preprocessed image.
    Returns (text, confidence) where confidence is 0.0-1.0.
    Text is rebuilt from the word-level data so Tesseract only runs a single pass.
    """
    pil_img = Image.fromarray(img)
    data = pytesseract.image_to_data(pil_img, lang=langs, output_type=pytesseract.Output.DICT)

    lines: dict[tuple, list[str]] = {}
    confidences: list[float] = []
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < 0 or not word:  # -1 marks layout (non-word) rows
            continue
        confidences.append(conf)
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)

    text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    avg_conf = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
    return text.strip(), round(avg_conf, 4)


def preprocess_and_ocr(img: np.ndarray) -> OCRResult:
    """
    Full preprocessing pipeline: grayscale → deskew → denoise → upscale → OCR.
    """
    t_start = time.time()
    gray = _to_grayscale(img)
    gray = _deskew(gray)
    gray = _denoise_and_threshold(gray)
    gray = _upscale_if_needed(gray)
    text, confidence = _run_tesseract(gray)
    duration_ms = int((time.time() - t_start) * 1000)
    return OCRResult(text=text, confidence=confidence, duration_ms=duration_ms)


def ocr_from_path(file_path: Path) -> OCRResult:
    """Entry point for single-frame image files (JPEG, PNG, TIFF first frame)."""
    img = _load_image_from_path(file_path)
    return preprocess_and_ocr(img)


def iter_image_frames(file_path: Path) -> Iterator[np.ndarray]:
    """
    Yield each frame of an image as a BGR ndarray, one at a time.
    Multi-page TIFFs yield one frame per page; other formats yield one frame.
    (cv2.imread only reads the first TIFF page, so Pillow is used for frames.)
    """
    with Image.open(file_path) as pil:
        for frame in ImageSequence.Iterator(pil):
            rgb = np.array(frame.convert("RGB"))
            yield cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def count_image_frames(file_path: Path) -> int:
    with Image.open(file_path) as pil:
        return getattr(pil, "n_frames", 1)


def ocr_from_bytes(img_bytes: bytes) -> tuple[str, float]:
    """
    Entry point called by the PDF processor for scanned PDF pages.
    Returns (text, confidence).
    """
    img = _load_image_from_bytes(img_bytes)
    result = preprocess_and_ocr(img)
    return result.text, result.confidence

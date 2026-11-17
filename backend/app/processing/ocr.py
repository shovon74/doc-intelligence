"""Tesseract OCR on top of the image_preprocess helpers."""
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytesseract
from PIL import Image

from app.core.config import settings
from app.processing import image_preprocess
from app.processing.errors import DocumentError, ErrorCode

logger = logging.getLogger(__name__)

OCR_LANGS = settings.OCR_LANGS


@dataclass
class OCRResult:
    text: str
    confidence: float   # 0.0 - 1.0
    duration_ms: int


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
    """Full pipeline: preprocess (grayscale, deskew, denoise, upscale) -> OCR."""
    t_start = time.time()
    text, confidence = _run_tesseract(image_preprocess.preprocess(img))
    duration_ms = int((time.time() - t_start) * 1000)
    return OCRResult(text=text, confidence=confidence, duration_ms=duration_ms)


def ocr_from_path(file_path: Path) -> OCRResult:
    """Entry point for single-frame image files (JPEG, PNG, TIFF first frame)."""
    image_preprocess.check_image_limits(file_path)
    img = image_preprocess._load_image_from_path(file_path)
    return preprocess_and_ocr(img)


def ocr_from_bytes(img_bytes: bytes) -> tuple[str, float]:
    """
    Entry point called by the PDF processor for scanned PDF pages.
    Returns (text, confidence).
    """
    img = image_preprocess._load_image_from_bytes(img_bytes)
    h, w = img.shape[:2]
    if w * h > settings.MAX_IMAGE_PIXELS:
        raise DocumentError(ErrorCode.FILE_TOO_LARGE, f"{w}x{h} px exceeds pixel limit")
    result = preprocess_and_ocr(img)
    return result.text, result.confidence

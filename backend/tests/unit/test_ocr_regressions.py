import numpy as np
import pytest

from app.processing import image_preprocess, ocr


@pytest.mark.unit
def test_deskew_accepts_opencv5_line_shape(monkeypatch):
    """OpenCV 5 returns HoughLinesP lines as (N, 4); OpenCV 4 returned (N, 1, 4)."""
    lines = np.array([[0, 0, 200, 10], [0, 5, 200, 16]], dtype=np.int32)
    for shape in (lines, lines.reshape(-1, 1, 4)):
        monkeypatch.setattr(image_preprocess.cv2, "HoughLinesP", lambda *a, _s=shape, **k: _s)
        out = image_preprocess._deskew(np.full((300, 300), 255, dtype=np.uint8))
        assert out.shape == (300, 300)


@pytest.mark.unit
def test_ocr_retries_without_threshold_when_result_is_weak(monkeypatch):
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setattr(image_preprocess, "preprocess", lambda i: "thresholded")
    monkeypatch.setattr(image_preprocess, "preprocess_gray", lambda i: "gray")
    reads = {"thresholded": ("", 0.0), "gray": ("Invoice Date: 2025-04-22 " * 10, 0.4)}
    monkeypatch.setattr(ocr, "_run_tesseract", lambda x: reads[x])
    r = ocr.preprocess_and_ocr(img)
    assert r.text.startswith("Invoice Date") and r.confidence == 0.4


@pytest.mark.unit
def test_ocr_keeps_good_first_read(monkeypatch):
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    calls = []
    monkeypatch.setattr(image_preprocess, "preprocess", lambda i: "thresholded")
    monkeypatch.setattr(image_preprocess, "preprocess_gray", lambda i: calls.append(1) or "gray")
    monkeypatch.setattr(ocr, "_run_tesseract", lambda x: ("good text " * 30, 0.9))
    assert ocr.preprocess_and_ocr(img).confidence == 0.9 and not calls

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import fitz  # PyMuPDF

from app.core.config import settings
from app.processing.errors import DocumentError, ErrorCode

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS = settings.PDF_MIN_TEXT_CHARS_PER_PAGE


@dataclass
class PageResult:
    page_number: int          # 1-indexed
    text: str
    source_type: str          # "digital" or "ocr"
    ocr_confidence: float | None = None
    char_count: int = 0
    duration_ms: int = 0
    error: str | None = None

    def __post_init__(self):
        self.char_count = len(self.text)


def _extract_page_text(page: fitz.Page) -> str:
    """Extract text from a digital PDF page."""
    return page.get_text("text", sort=True).strip()


def _open_pdf(file_path: Path) -> fitz.Document:
    try:
        doc = fitz.open(str(file_path))
    except Exception as e:  # fitz raises several distinct error types
        raise DocumentError(ErrorCode.CORRUPT_FILE, f"Cannot open PDF: {e}") from e
    if doc.needs_pass:
        # Try an empty password (many "encrypted" PDFs only restrict editing)
        if not doc.authenticate(""):
            doc.close()
            raise DocumentError(ErrorCode.ENCRYPTED_PDF, "PDF requires a password")
    if doc.page_count == 0:
        # e.g. a truncated file that PyMuPDF "repaired" down to nothing
        doc.close()
        raise DocumentError(ErrorCode.CORRUPT_FILE, "PDF has no readable pages")
    if doc.page_count > settings.MAX_PAGES:
        count = doc.page_count
        doc.close()
        raise DocumentError(
            ErrorCode.FILE_TOO_LARGE, f"PDF has {count} pages, limit is {settings.MAX_PAGES}"
        )
    return doc


def _ocr_page(page: fitz.Page, ocr_processor) -> tuple[str, float]:
    mat = fitz.Matrix(settings.OCR_DPI / 72, settings.OCR_DPI / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    try:
        return ocr_processor.ocr_from_bytes(pix.tobytes("png"))
    finally:
        pix = None  # free the (large) pixmap immediately


def stream_pdf_pages(
    file_path: Path,
    ocr_processor=None,
    first_page: int = 1,
    last_page: int | None = None,
) -> Generator[PageResult, None, None]:
    """
    Stream PDF pages one at a time (bounded memory).

    Pages with fewer than PDF_MIN_TEXT_CHARS_PER_PAGE characters of embedded text
    fall back to OCR via ``ocr_processor`` (any object/module exposing
    ``ocr_from_bytes(bytes) -> (text, confidence)``). A failure on one page is
    reported on that PageResult and does not abort the document.

    ``first_page``/``last_page`` (1-indexed, inclusive) select a batch of pages.

    Raises DocumentError (CORRUPT_FILE / ENCRYPTED_PDF) if the file cannot be opened.
    """
    doc = _open_pdf(file_path)
    try:
        total_pages = doc.page_count
        logger.info(f"PDF opened: {file_path.name}, pages={total_pages}")

        last = min(last_page or total_pages, total_pages)
        for page_num in range(max(first_page, 1) - 1, last):
            t_start = time.time()
            page_number = page_num + 1  # 1-indexed
            source_type = "digital"
            confidence = None
            text = ""
            error = None

            try:
                page = doc.load_page(page_num)
                text = _extract_page_text(page)

                if len(text) < MIN_TEXT_CHARS and ocr_processor is not None:
                    logger.debug(
                        f"Page {page_number}: only {len(text)} chars, triggering OCR fallback"
                    )
                    ocr_text, confidence = _ocr_page(page, ocr_processor)
                    # Keep the digital text if OCR found nothing better
                    if len(ocr_text) > len(text):
                        text, source_type = ocr_text, "ocr"
                    else:
                        confidence = None
            except Exception as e:
                logger.warning(f"Failed to process page {page_number}: {e}")
                error = str(e)
            finally:
                page = None  # release page memory

            yield PageResult(
                page_number=page_number,
                text=text,
                source_type=source_type,
                ocr_confidence=confidence,
                duration_ms=int((time.time() - t_start) * 1000),
                error=error,
            )
    finally:
        doc.close()  # also runs if the consumer stops iterating early


def get_page_count(file_path: Path) -> int:
    """Quick page count without loading full content."""
    doc = _open_pdf(file_path)
    try:
        return doc.page_count
    finally:
        doc.close()

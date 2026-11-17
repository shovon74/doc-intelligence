"""
Processing stage orchestrator: turns a stored upload into DocumentPage rows.

    PDF          -> PyMuPDF text, OCR fallback per page
    JPG/PNG/TIFF -> OpenCV preprocessing + Tesseract (one page per TIFF frame)
    XLSX/XLS/CSV -> one page per sheet

Pages are flushed to the database in batches so memory stays bounded for
1,000-page documents, and progress is reported as pages complete. The function
is idempotent: re-running it (Celery retry) replaces previously stored pages.
"""
import logging
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy.orm import Session

from app.db.models import Document, DocumentPage, ProcessingEvent
from app.processing.errors import DocumentError, ErrorCode, get_error_info, to_document_error
from app.processing import image_processor, pdf_processor, spreadsheet_processor

logger = logging.getLogger(__name__)

BATCH_SIZE = 20
PROCESSING_PROGRESS_SHARE = 50.0   # processing = first half of overall progress

PDF_EXTS = {"pdf"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "tiff", "tif"}
SHEET_EXTS = {"xlsx", "xls", "csv"}


def _image_pages(path: Path) -> Iterator[dict]:
    for n, frame in enumerate(image_processor.iter_image_frames(path), start=1):
        try:
            r = image_processor.preprocess_and_ocr(frame)
            yield dict(page_number=n, text=r.text, source_type="ocr",
                       ocr_confidence=r.confidence, error=None)
        except Exception as e:
            logger.warning(f"OCR failed on frame {n} of {path.name}: {e}")
            yield dict(page_number=n, text="", source_type="ocr",
                       ocr_confidence=None, error=str(e))


def _pdf_pages(path: Path) -> Iterator[dict]:
    for r in pdf_processor.stream_pdf_pages(path, ocr_processor=image_processor):
        yield dict(page_number=r.page_number, text=r.text, source_type=r.source_type,
                   ocr_confidence=r.ocr_confidence, error=r.error)


def _sheet_pages(path: Path, ext: str) -> Iterator[dict]:
    for r in spreadsheet_processor.stream_sheets(path, ext):
        yield dict(page_number=r.page_number, text=r.text, source_type="spreadsheet",
                   ocr_confidence=None, error=r.error)


def _plan(path: Path, ext: str) -> tuple[int, Iterator[dict]]:
    """Return (total_page_count, page iterator) for the file type."""
    try:
        if ext in PDF_EXTS:
            return pdf_processor.get_page_count(path), _pdf_pages(path)
        if ext in IMAGE_EXTS:
            return image_processor.count_image_frames(path), _image_pages(path)
        if ext in SHEET_EXTS:
            return spreadsheet_processor.count_sheets(path, ext), _sheet_pages(path, ext)
    except DocumentError:
        raise
    except Exception as e:
        raise DocumentError(ErrorCode.CORRUPT_FILE, f"Cannot open file: {e!r}") from e
    raise DocumentError(ErrorCode.UNSUPPORTED_TYPE, f"No processor for '.{ext}' files")


def _page_error_message(source_type: str) -> str:
    """User-facing text for a single failed page (raw errors are only logged)."""
    code = ErrorCode.OCR_FAILED if source_type == "ocr" else ErrorCode.CORRUPT_FILE
    return get_error_info(code).message


def _event(db: Session, doc_id: uuid.UUID, status: str, message: str | None = None,
           duration_ms: int | None = None, details: dict | None = None) -> None:
    db.add(ProcessingEvent(document_id=doc_id, stage="processing", status=status,
                           message=message, duration_ms=duration_ms, details=details))


def process_document(
    db: Session,
    document_id: uuid.UUID | str,
    on_progress: Callable[[float], None] | None = None,
) -> dict:
    """
    Extract text for every page of a stored document and persist it.

    On success the document is left in status "extracting" (next stage's input).
    On failure the document is marked "failed" with error_code/error_message and
    the DocumentError is re-raised. Returns a small summary dict.

    The stored error_message is always the friendly text for the error code;
    technical detail goes only to the log and the processing-event details.
    """
    doc_id = uuid.UUID(str(document_id))
    doc = db.get(Document, doc_id)
    if doc is None:
        raise DocumentError(ErrorCode.INTERNAL_ERROR, f"Document {doc_id} does not exist")

    t_start = time.time()
    doc.status, doc.progress_percent = "processing", 0.0
    doc.error_code = doc.error_message = None
    # idempotent re-run: drop pages from any earlier attempt
    db.query(DocumentPage).filter(DocumentPage.document_id == doc_id).delete()
    _event(db, doc_id, "started", f"Processing {doc.original_filename}")
    db.commit()

    try:
        path = Path(doc.stored_path)
        if not path.is_file():
            raise DocumentError(ErrorCode.INTERNAL_ERROR, f"Stored file not found: {doc.stored_path}")

        ext = doc.file_ext.lower().lstrip(".")
        total, pages = _plan(path, ext)
        doc.page_count = total
        db.commit()

        done = failed = 0
        batch: list[DocumentPage] = []

        def flush() -> None:
            db.add_all(list(batch))
            batch.clear()
            doc.progress_percent = round(PROCESSING_PROGRESS_SHARE * done / max(total, 1), 1)
            db.commit()
            if on_progress:
                on_progress(doc.progress_percent)

        for p in pages:
            done += 1
            failed += bool(p["error"])
            batch.append(DocumentPage(
                document_id=doc_id,
                page_number=p["page_number"],
                source_type=p["source_type"],
                text=p["text"].replace("\x00", ""),   # Postgres text rejects NUL bytes
                ocr_confidence=p["ocr_confidence"],
                char_count=len(p["text"]),
                status="failed" if p["error"] else "completed",
                error_message=_page_error_message(p["source_type"]) if p["error"] else None,
            ))
            if len(batch) >= BATCH_SIZE:
                flush()
        flush()

        if total and failed == total:
            code = ErrorCode.CORRUPT_FILE if ext in SHEET_EXTS else ErrorCode.OCR_FAILED
            raise DocumentError(code, f"All {total} pages failed to process")

        ms = int((time.time() - t_start) * 1000)
        doc.status = "extracting"
        doc.progress_percent = PROCESSING_PROGRESS_SHARE
        _event(db, doc_id, "succeeded", f"{done - failed}/{done} pages processed",
               ms, {"pages": done, "failed_pages": failed})
        db.commit()
        return {"document_id": str(doc_id), "pages": done, "failed_pages": failed,
                "duration_ms": ms}

    except Exception as e:
        db.rollback()
        err = to_document_error(e)
        logger.exception(f"Processing failed for document {doc_id} [{err.code.value}]")
        doc = db.get(Document, doc_id)
        doc.status = "failed"
        doc.error_code, doc.error_message = err.code.value, err.message
        _event(db, doc_id, "failed", err.message, int((time.time() - t_start) * 1000),
               {"code": err.code.value, "retryable": err.retryable, "detail": err.detail})
        db.commit()
        raise err from e

"""
Processing stage orchestrator: turns a stored upload into DocumentPage rows.

    PDF          -> PyMuPDF text, OCR fallback per page
    JPG/PNG/TIFF -> OpenCV preprocessing + Tesseract (one page per TIFF frame)
    XLSX/XLS/CSV -> one page per sheet

Pages are flushed to the database in batches so memory stays bounded for
1,000-page documents, and progress is reported as pages complete. The work is
split into three steps so large PDFs can run as parallel page batches:

    start_document  -> validate file, reset state, count pages
    process_pages   -> extract one page range (one Celery task per batch)
    finish_document -> join the batch results and set the final status

``process_document`` runs the three in sequence for the in-process path. Every step is
idempotent: re-running it replaces previously stored pages.
"""
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Document, DocumentPage, ProcessingEvent
from app.processing.errors import DocumentError, ErrorCode, get_error_info, to_document_error
from app.processing import image_preprocess, ocr, pdf, spreadsheet

logger = logging.getLogger(__name__)

PROCESSING_PROGRESS_SHARE = 50.0   # processing = first half of overall progress

PDF_EXTS = {"pdf"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "tiff", "tif"}
SHEET_EXTS = {"xlsx", "xls", "csv"}


def _image_pages(path: Path) -> Iterator[dict]:
    for n, frame in enumerate(image_preprocess.iter_image_frames(path), start=1):
        try:
            r = ocr.preprocess_and_ocr(frame)
            yield dict(page_number=n, text=r.text, source_type="ocr",
                       ocr_confidence=r.confidence, error=None)
        except Exception as e:
            logger.warning(f"OCR failed on frame {n} of {path.name}: {e}")
            yield dict(page_number=n, text="", source_type="ocr",
                       ocr_confidence=None, error=str(e))


def _pdf_pages(path: Path, first: int, last: int | None) -> Iterator[dict]:
    for r in pdf.stream_pdf_pages(path, ocr_processor=ocr, first_page=first, last_page=last):
        yield dict(page_number=r.page_number, text=r.text, source_type=r.source_type,
                   ocr_confidence=r.ocr_confidence, error=r.error)


def _sheet_pages(path: Path, ext: str) -> Iterator[dict]:
    for r in spreadsheet.stream_sheets(path, ext):
        yield dict(page_number=r.page_number, text=r.text, source_type="spreadsheet",
                   ocr_confidence=None, error=r.error,
                   mapping=r.extraction.details() if r.extraction else None)


def _count_pages(path: Path, ext: str) -> int:
    try:
        if ext in PDF_EXTS:
            return pdf.get_page_count(path)
        if ext in IMAGE_EXTS:
            return image_preprocess.count_image_frames(path)
        if ext in SHEET_EXTS:
            return spreadsheet.count_sheets(path, ext)
    except DocumentError:
        raise
    except Exception as e:
        raise DocumentError(ErrorCode.CORRUPT_FILE, f"Cannot open file: {e!r}") from e
    raise DocumentError(ErrorCode.UNSUPPORTED_TYPE, f"No processor for '.{ext}' files")


def _iter_pages(path: Path, ext: str, first: int, last: int | None) -> Iterator[dict]:
    if ext in PDF_EXTS:
        return _pdf_pages(path, first, last)
    if ext in IMAGE_EXTS:
        return _image_pages(path)
    return _sheet_pages(path, ext)


def _page_error_message(source_type: str) -> str:
    """User-facing text for a single failed page (raw errors are only logged)."""
    code = ErrorCode.OCR_FAILED if source_type == "ocr" else ErrorCode.CORRUPT_FILE
    return get_error_info(code).message


def add_event(db: Session, doc_id: uuid.UUID, status: str, message: str | None = None,
           duration_ms: int | None = None, details: dict | None = None,
           stage: str = "processing") -> None:
    db.add(ProcessingEvent(document_id=doc_id, stage=stage, status=status,
                           message=message, duration_ms=duration_ms, details=details))


def batch_ranges(total: int, size: int | None = None) -> list[tuple[int, int]]:
    """Split pages 1..total into inclusive (first, last) ranges of ``size`` pages."""
    size = size or settings.PAGE_BATCH_SIZE
    return [(a, min(a + size - 1, total)) for a in range(1, total + 1, size)]


def is_batchable(ext: str, total: int) -> bool:
    """Only PDFs larger than one batch are split; images and sheets run as one unit."""
    return ext in PDF_EXTS and total > settings.PAGE_BATCH_SIZE


@dataclass
class Plan:
    total: int
    ext: str


def run_stage(db: Session, doc_id: uuid.UUID, will_retry: bool, fn, stage: str = "processing"):
    """
    Run one pipeline step. On failure the document is marked failed with the friendly
    message for the error code and the DocumentError is re-raised. If the error is
    retryable and a retry is coming (``will_retry``), the document stays in "processing"
    and a "retried" event is recorded instead.
    """
    t0 = time.time()
    try:
        return fn()
    except Exception as e:
        db.rollback()
        err = to_document_error(e)
        logger.exception(f"Processing failed for document {doc_id} [{err.code.value}]")
        ms = int((time.time() - t0) * 1000)
        details = {"code": err.code.value, "retryable": err.retryable, "detail": err.detail}
        doc = db.get(Document, doc_id)
        if will_retry and err.retryable:
            doc.status = "extracting" if stage == "extraction" else "processing"
            doc.error_code = doc.error_message = None
            add_event(db, doc_id, "retried", f"Will retry: {err.code.value}", ms, details, stage)
        else:
            doc.status = "failed"
            doc.error_code, doc.error_message = err.code.value, err.message
            add_event(db, doc_id, "failed", err.message, ms, details, stage)
        db.commit()
        raise err from e


def start_document(db: Session, document_id: uuid.UUID | str, will_retry: bool = False) -> Plan:
    """Reset the document, validate the stored file and count its pages."""
    doc_id = uuid.UUID(str(document_id))
    doc = db.get(Document, doc_id)
    if doc is None:
        raise DocumentError(ErrorCode.INTERNAL_ERROR, f"Document {doc_id} does not exist")

    doc.status, doc.progress_percent = "processing", 0.0
    doc.error_code = doc.error_message = None
    # idempotent re-run: drop pages from any earlier attempt
    db.query(DocumentPage).filter(DocumentPage.document_id == doc_id).delete()
    add_event(db, doc_id, "started", f"Processing {doc.original_filename}")
    db.commit()

    def work() -> Plan:
        path = Path(doc.stored_path)
        if not path.is_file():
            raise DocumentError(ErrorCode.INTERNAL_ERROR, f"Stored file not found: {doc.stored_path}")
        if path.stat().st_size == 0:
            raise DocumentError(ErrorCode.EMPTY_FILE, f"Stored file is zero bytes: {path.name}")
        ext = doc.file_ext.lower().lstrip(".")
        doc.page_count = _count_pages(path, ext)
        db.commit()
        return Plan(total=doc.page_count, ext=ext)

    return run_stage(db, doc_id, will_retry, work)


def process_pages(
    db: Session,
    document_id: uuid.UUID | str,
    first: int,
    last: int | None,
    total: int,
    shared_progress: bool = False,
    on_progress: Callable[[float], None] | None = None,
) -> dict:
    """
    Extract and store pages ``first..last`` (PDFs; other types ignore the range).

    ``shared_progress`` is for parallel batches: progress is added atomically in SQL
    instead of being assigned. Returns counts for ``finish_document``. A page that
    fails is counted and stored as failed; it does not raise.
    """
    doc_id = uuid.UUID(str(document_id))
    doc = db.get(Document, doc_id)
    path = Path(doc.stored_path)
    ext = doc.file_ext.lower().lstrip(".")
    t0 = time.time()

    if shared_progress:   # batch re-run: replace this range only
        db.query(DocumentPage).filter(
            DocumentPage.document_id == doc_id,
            DocumentPage.page_number.between(first, last or total),
        ).delete(synchronize_session=False)

    done = failed = flushed = 0
    has_text = False
    mappings: list[dict] = []
    batch: list[DocumentPage] = []

    def flush() -> None:
        nonlocal flushed
        n = len(batch)
        db.add_all(list(batch))
        batch.clear()
        flushed += n
        if shared_progress:
            share = PROCESSING_PROGRESS_SHARE * n / max(total, 1)
            db.execute(update(Document).where(Document.id == doc_id).values(
                progress_percent=func.least(PROCESSING_PROGRESS_SHARE,
                                            Document.progress_percent + share)))
        else:
            doc.progress_percent = round(PROCESSING_PROGRESS_SHARE * flushed / max(total, 1), 1)
        db.commit()
        if on_progress:
            on_progress(doc.progress_percent)

    for p in _iter_pages(path, ext, first, last):
        done += 1
        failed += bool(p["error"])
        has_text = has_text or bool(p["text"].strip())
        if p.get("mapping"):
            mappings.append(p["mapping"])
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
        if len(batch) >= settings.PAGE_BATCH_SIZE:
            flush()
    if batch:
        flush()
    return {"first": first, "last": last, "pages": done, "failed": failed,
            "has_text": has_text, "mappings": mappings,
            "duration_ms": int((time.time() - t0) * 1000)}


def failed_batch_result(first: int, last: int) -> dict:
    """Result for a batch that crashed as a whole: all its pages count as failed."""
    n = last - first + 1
    return {"first": first, "last": last, "pages": n, "failed": n, "has_text": False,
            "mappings": [], "duration_ms": 0}


def finish_document(db: Session, document_id: uuid.UUID | str, results: list[dict]) -> dict:
    """
    Join page results. The document fails only if every page failed or no page has
    text; a few failed pages are recorded on the pages and in the event details.
    """
    doc_id = uuid.UUID(str(document_id))

    def work() -> dict:
        doc = db.get(Document, doc_id)
        ext = doc.file_ext.lower().lstrip(".")
        total = doc.page_count
        done = sum(r["pages"] for r in results)
        failed = sum(r["failed"] for r in results) + max(total - done, 0)   # lost batches
        done = max(done, total)
        ms = sum(r["duration_ms"] for r in results)

        if total and failed >= total:
            code = ErrorCode.CORRUPT_FILE if ext in SHEET_EXTS else ErrorCode.OCR_FAILED
            raise DocumentError(code, f"All {total} pages failed to process")
        if not any(r["has_text"] for r in results):
            # Pages stay stored for inspection; the document itself is marked failed.
            raise DocumentError(ErrorCode.NO_TEXT_FOUND, f"{done} page(s) processed, no text")

        doc.status = "extracting"
        doc.progress_percent = PROCESSING_PROGRESS_SHARE
        details = {"pages": done, "failed_pages": failed, "batches": len(results)}
        mappings = [m for r in results for m in r["mappings"]]
        if mappings:
            details["sheet_mappings"] = mappings
        add_event(db, doc_id, "succeeded", f"{done - failed}/{done} pages processed", ms, details)
        db.commit()
        return {"document_id": str(doc_id), "pages": done, "failed_pages": failed,
                "duration_ms": ms}

    return run_stage(db, doc_id, False, work)


def process_planned(db: Session, document_id: uuid.UUID | str, plan: Plan,
                    on_progress: Callable[[float], None] | None = None,
                    will_retry: bool = False) -> dict:
    """Process every page of an already started document in this process, then finish it."""
    doc_id = uuid.UUID(str(document_id))
    result = run_stage(db, doc_id, will_retry, lambda: process_pages(
        db, doc_id, 1, plan.total, plan.total, on_progress=on_progress))
    return finish_document(db, doc_id, [result])


def process_document(
    db: Session,
    document_id: uuid.UUID | str,
    on_progress: Callable[[float], None] | None = None,
    will_retry: bool = False,
) -> dict:
    """
    Extract text for every page of a stored document and persist it (in-process path).

    On success the document is left in status "extracting" (next stage's input).
    On failure the document is marked "failed" with error_code/error_message and
    the DocumentError is re-raised, unless the error is retryable and ``will_retry``
    is set (see ``run_stage``). Returns a small summary dict.

    The stored error_message is always the friendly text for the error code;
    technical detail goes only to the log and the processing-event details.
    """
    plan = start_document(db, document_id, will_retry)
    return process_planned(db, document_id, plan, on_progress, will_retry)

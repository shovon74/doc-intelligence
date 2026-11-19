import logging
from datetime import date
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, Page, get_current_user, pagination
from app.core.config import settings
from app.db.models import Document, DocumentPage, Invoice, ProcessingEvent
from app.db.session import get_db
from app.llm.factory import resolve_llm
from app.processing.errors import DocumentError, ErrorCode, get_error_info
from app.schemas.api import (DocumentDetail, DocumentOut, DocumentStatusOut, EventOut,
                             PageOut, PageTextOut, RejectedOut, ReprocessFailedOut,
                             ReprocessIn, ResultOut, UploadedOut, UploadResult)
from app.storage import file_handler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["Documents"])

MAX_FILES_PER_UPLOAD = 20
IN_FLIGHT = ("queued", "processing", "extracting", "validating")
STATUSES = ("uploaded",) + IN_FLIGHT + ("completed", "needs_review", "failed")


def owned(db: Session, user: CurrentUser, document_id: uuid.UUID) -> Document:
    """The user's document, or 404 (never reveals that another user's exists)."""
    doc = db.scalar(select(Document).where(Document.id == document_id,
                                           Document.user_id == user.id))
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    return doc


def _counts(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    if not ids:
        return {}
    rows = db.execute(
        select(Invoice.document_id, func.count(Invoice.id),
               func.sum(case((Invoice.needs_review.is_(True), 1), else_=0)))
        .where(Invoice.document_id.in_(ids)).group_by(Invoice.document_id)).all()
    return {r[0]: (r[1], int(r[2] or 0)) for r in rows}


def document_out(db: Session, docs: list[Document]) -> list[DocumentOut]:
    counts = _counts(db, [d.id for d in docs])
    out = []
    for d in docs:
        o = DocumentOut.model_validate(d)
        o.invoice_count, o.needs_review_count = counts.get(d.id, (0, 0))
        out.append(o)
    return out


def enqueue(db: Session, doc: Document) -> None:
    """Hand the document to the worker. A broker outage fails the document visibly."""
    from app.workers.tasks import process_document_task
    try:
        process_document_task.delay(str(doc.id))
    except Exception:
        logger.exception(f"Could not enqueue document {doc.id}")
        info = get_error_info(ErrorCode.INTERNAL_ERROR)
        doc.status, doc.error_code, doc.error_message = "failed", info.code.value, info.message
        db.commit()


def _check_provider(requested: str | None) -> str | None:
    requested = (requested or "").strip().lower() or None
    if requested == "fake" and settings.ENVIRONMENT.lower() == "production":
        raise DocumentError(ErrorCode.LLM_NOT_CONFIGURED, "the test provider is disabled")
    resolve_llm(requested)                  # raises LLM_NOT_CONFIGURED when it cannot work
    return requested


@router.post("", response_model=UploadResult, status_code=status.HTTP_202_ACCEPTED,
             summary="Upload one or more documents (processing starts via POST /{id}/reprocess)")
def upload(files: list[UploadFile] = File(...), llm_provider: str | None = Form(None),
           db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Upload at most {MAX_FILES_PER_UPLOAD} files at a time.")
    provider = _check_provider(llm_provider)

    accepted: list[UploadedOut] = []
    rejected: list[tuple[str, DocumentError]] = []
    for f in files:
        name = Path(f.filename or "upload").name or "upload"
        try:
            saved = file_handler.save_upload(f.file, name, db, user_id=user.id)
            doc = file_handler.register_document(db, saved, name, user.id, provider)
        except DocumentError as e:
            rejected.append((name, e))
            continue
        db.refresh(doc)
        accepted.append(UploadedOut(id=doc.id, filename=name, status=doc.status,
                                    duplicate=saved["is_duplicate"]))
    if rejected and not accepted:
        raise rejected[0][1]                # nothing was accepted: report the first reason
    return UploadResult(
        documents=accepted,
        rejected=[RejectedOut(filename=n, code=e.code.value, message=e.message)
                  for n, e in rejected])


SORTS = {"created_at": Document.created_at, "updated_at": Document.updated_at,
         "filename": Document.original_filename, "status": Document.status,
         "size": Document.size_bytes}


@router.get("", response_model=PageOut[DocumentOut], summary="List your documents")
def list_documents(status_: str | None = Query(None, alias="status"),
                   type_: str | None = Query(None, alias="type", max_length=10,
                                             description="File type, e.g. pdf, xlsx, csv"),
                   q: str | None = Query(None, max_length=200, description="Filename contains"),
                   needs_review: bool | None = Query(None, description="Has flagged invoices"),
                   date_from: date | None = None, date_to: date | None = None,
                   sort: str = Query("created_at"), order: str = Query("desc", pattern="^(asc|desc)$"),
                   page: Page = Depends(pagination), db: Session = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    if status_ is not None and status_ not in STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"status must be one of: {', '.join(STATUSES)}.")
    if sort not in SORTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"sort must be one of: {', '.join(SORTS)}.")
    query = select(Document).where(Document.user_id == user.id)
    if status_:
        query = query.where(Document.status == status_)
    if type_:
        query = query.where(Document.file_ext == type_.lower().lstrip("."))
    if date_from:
        query = query.where(func.date(Document.created_at) >= date_from)
    if date_to:
        query = query.where(func.date(Document.created_at) <= date_to)
    if needs_review is not None:
        flagged = select(Invoice.id).where(Invoice.document_id == Document.id,
                                           Invoice.needs_review.is_(True)).exists()
        query = query.where(flagged if needs_review else ~flagged)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(Document.original_filename.ilike(f"%{escaped}%", escape="\\"))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    column = SORTS[sort]
    docs = db.scalars(query.order_by(column.asc() if order == "asc" else column.desc(), Document.id)
                      .offset(page.offset).limit(page.page_size)).all()
    return PageOut(items=document_out(db, list(docs)), total=total, page=page.page,
                   page_size=page.page_size)


@router.post("/reprocess-failed", response_model=ReprocessFailedOut,
             status_code=status.HTTP_202_ACCEPTED, summary="Requeue all your failed documents")
def reprocess_failed(db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    docs = db.scalars(select(Document).where(Document.user_id == user.id,
                                             Document.status == "failed")).all()
    for d in docs:
        d.status, d.progress_percent = "queued", 0.0
        d.error_code = d.error_message = d.completed_at = None
    db.commit()
    for d in docs:
        enqueue(db, d)
    return ReprocessFailedOut(requeued=[d.id for d in docs])


@router.get("/{document_id}", response_model=DocumentDetail,
            summary="One document with its invoices, issues and processing history")
def get_document(document_id: uuid.UUID, db: Session = Depends(get_db),
                 user: CurrentUser = Depends(get_current_user)):
    from app.api.v1.invoices import invoice_out
    doc = owned(db, user, document_id)
    invoices = db.scalars(select(Invoice).where(Invoice.document_id == doc.id)
                          .options(selectinload(Invoice.line_items),
                                   selectinload(Invoice.validation_issues),
                                   selectinload(Invoice.corrections))
                          .order_by(Invoice.segment_index)).all()
    events = db.scalars(select(ProcessingEvent).where(ProcessingEvent.document_id == doc.id)
                        .order_by(ProcessingEvent.created_at)).all()
    out = DocumentDetail.model_validate(document_out(db, [doc])[0].model_dump())
    out.invoices = [invoice_out(i) for i in invoices]
    out.events = events
    return out


@router.get("/{document_id}/status", response_model=DocumentStatusOut,
            summary="Lightweight status for progress polling")
def document_status(document_id: uuid.UUID, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    doc = owned(db, user, document_id)
    stage = db.scalar(select(ProcessingEvent.stage).where(ProcessingEvent.document_id == doc.id)
                      .order_by(ProcessingEvent.created_at.desc()).limit(1))
    return DocumentStatusOut(id=doc.id, status=doc.status, progress_percent=doc.progress_percent,
                             error_code=doc.error_code, error_message=doc.error_message,
                             stage=stage)


@router.get("/{document_id}/result", response_model=ResultOut,
            summary="Extracted invoices, line items and issues (optionally the page texts)")
def document_result(document_id: uuid.UUID, include_text: bool = False,
                    db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    from app.api.v1.invoices import invoice_out
    doc = owned(db, user, document_id)
    invoices = db.scalars(select(Invoice).where(Invoice.document_id == doc.id)
                          .options(selectinload(Invoice.line_items),
                                   selectinload(Invoice.validation_issues),
                                   selectinload(Invoice.corrections))
                          .order_by(Invoice.segment_index)).all()
    pages = None
    if include_text:
        rows = db.scalars(select(DocumentPage).where(DocumentPage.document_id == doc.id)
                          .order_by(DocumentPage.page_number)).all()
        pages = [PageTextOut.model_validate(r) for r in rows]
    return ResultOut(document=document_out(db, [doc])[0],
                     invoices=[invoice_out(i) for i in invoices], pages=pages)


@router.get("/{document_id}/history", response_model=list[EventOut],
            summary="Processing events timeline")
def document_history(document_id: uuid.UUID, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    doc = owned(db, user, document_id)
    return db.scalars(select(ProcessingEvent).where(ProcessingEvent.document_id == doc.id)
                      .order_by(ProcessingEvent.created_at)).all()


@router.get("/{document_id}/file", summary="Download or preview the original file")
def document_file(document_id: uuid.UUID, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(get_current_user)):
    doc = owned(db, user, document_id)
    path = Path(doc.stored_path).resolve()
    root = Path(settings.UPLOAD_DIR).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The original file is no longer available.")
    return FileResponse(path, media_type=doc.mime_type, filename=doc.original_filename,
                        content_disposition_type="inline")


@router.post("/{document_id}/reprocess", response_model=DocumentOut,
             status_code=status.HTTP_202_ACCEPTED, summary="Run a document through the pipeline again")
def reprocess(document_id: uuid.UUID, body: ReprocessIn | None = None,
              db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    doc = owned(db, user, document_id)
    if doc.status in IN_FLIGHT:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This document is still being processed. Wait for it to finish.")
    if body and body.llm_provider is not None:
        doc.llm_provider_requested = _check_provider(body.llm_provider)
    else:
        _check_provider(doc.llm_provider_requested)
    doc.status, doc.progress_percent = "queued", 0.0
    doc.error_code = doc.error_message = doc.completed_at = None
    db.commit()
    enqueue(db, doc)
    db.refresh(doc)
    return document_out(db, [doc])[0]


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a document, its extracted data and the stored file")
def delete_document(document_id: uuid.UUID, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    doc = owned(db, user, document_id)
    if doc.status in IN_FLIGHT:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This document is still being processed. Wait for it to finish.")
    folder = Path(doc.stored_path).resolve().parent
    root = Path(settings.UPLOAD_DIR).resolve()
    db.delete(doc)
    db.commit()
    if root in folder.parents:
        shutil.rmtree(folder, ignore_errors=True)

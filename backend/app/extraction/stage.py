"""
Extraction stage: stored pages -> Invoice / LineItem rows.

    spreadsheets      -> mapped directly (heuristic columns, LLM column-mapping fallback)
    PDFs, images      -> heuristic segmentation, then one LLM call per invoice segment

On success the document moves to "validating" (the validation stage is next). Per-segment
failures do not fail the document unless every segment fails. The primary provider is
tried first; the fallback provider (auto mode, both keys present) takes over when the
primary is unavailable, rate limited, or returns an unparseable reply.
"""
import logging
import time
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import Document, DocumentPage, Invoice, LineItem
from app.extraction import prompts
from app.extraction.extractor import ExtractedInvoice, extract_invoice, llm_column_mapper
from app.extraction.normalize import norm_invoice_number, norm_supplier
from app.extraction.segmentation import segment_pages
from app.llm.factory import Resolution, resolve_llm
from app.processing import pipeline, spreadsheet
from app.processing.errors import DocumentError, ErrorCode

logger = logging.getLogger(__name__)

MAX_SEGMENT_CHARS = 24_000
EXTRACTION_PROGRESS = 90.0
_FALLBACK_ON = {ErrorCode.LLM_PROVIDER_UNAVAILABLE, ErrorCode.LLM_RATE_LIMITED,
                ErrorCode.EXTRACTION_PARSE_ERROR}


def _extract_with_fallback(res: Resolution, text: str, start: int, end: int) -> ExtractedInvoice:
    try:
        return extract_invoice(res.primary, text, start, end)
    except DocumentError as e:
        if res.fallback is None or e.code not in _FALLBACK_ON:
            raise
        logger.warning(f"{res.primary.name} failed ({e.code.value}); using {res.fallback.name}")
        return extract_invoice(res.fallback, text, start, end)


def _from_text(db: Session, doc: Document, will_retry: bool) -> tuple[list[ExtractedInvoice], dict]:
    pages = (db.query(DocumentPage)
             .filter(DocumentPage.document_id == doc.id, DocumentPage.status == "completed")
             .order_by(DocumentPage.page_number).all())
    segments = segment_pages([(p.page_number, p.text) for p in pages])
    if not segments:
        raise DocumentError(ErrorCode.NO_TEXT_FOUND, "no page text to extract from")
    res = resolve_llm(doc.llm_provider_requested)       # LLM_NOT_CONFIGURED fails the document

    items: list[ExtractedInvoice] = []
    failures: list[DocumentError] = []
    truncated = 0
    for i, seg in enumerate(segments, start=1):
        text = seg.text
        if len(text) > MAX_SEGMENT_CHARS:
            text, truncated = text[:MAX_SEGMENT_CHARS], truncated + 1
        try:
            items.append(_extract_with_fallback(res, text, seg.page_start, seg.page_end))
        except DocumentError as e:
            if will_retry and e.retryable:
                raise                       # retry the whole stage rather than accept a gap
            logger.warning(f"Segment pages {seg.page_start}-{seg.page_end} failed: {e.code.value}")
            failures.append(e)
        doc.progress_percent = round(50 + (EXTRACTION_PROGRESS - 50) * i / len(segments), 1)
        db.commit()
    if not items:
        raise failures[-1]
    info = {"segments": len(segments), "failed_segments": len(failures), "truncated": truncated,
            "provider": res.primary.name, "fallback": res.fallback.name if res.fallback else None}
    return items, info


def _from_sheets(db: Session, doc: Document) -> tuple[list[ExtractedInvoice], dict]:
    try:
        provider = resolve_llm(doc.llm_provider_requested).primary
        mapper = llm_column_mapper(provider)
    except DocumentError:
        provider, mapper = None, None        # heuristics only; the LLM is just a fallback here
    items: list[ExtractedInvoice] = []
    mappings: list[dict] = []
    for sheet in spreadsheet.stream_sheets(Path(doc.stored_path), doc.file_ext, mapper):
        ex = sheet.extraction
        if ex is None:
            continue
        mappings.append(ex.details())
        for inv in ex.invoices:
            items.append(ExtractedInvoice(
                extraction=inv, page_start=sheet.page_number, page_end=sheet.page_number,
                provider="spreadsheet", model=ex.mapping_source,
                prompt_version=prompts.COLUMN_MAPPING_PROMPT_VERSION
                if ex.mapping_source == "llm" else None,
                raw=inv.model_dump(mode="json")))
    if not items:
        raise DocumentError(ErrorCode.EXTRACTION_PARSE_ERROR,
                            "no spreadsheet columns could be mapped to invoice fields")
    return items, {"sheet_mappings": mappings,
                   "provider": provider.name if provider else None}


def _persist(db: Session, doc: Document, items: list[ExtractedInvoice]) -> None:
    db.query(Invoice).filter(Invoice.document_id == doc.id).delete()   # idempotent re-run
    for idx, it in enumerate(items):
        ex = it.extraction
        inv = Invoice(
            document_id=doc.id, segment_index=idx, page_start=it.page_start, page_end=it.page_end,
            invoice_number=ex.invoice_number, invoice_date=ex.invoice_date,
            supplier_name=ex.supplier_name, customer_name=ex.customer_name, subtotal=ex.subtotal,
            tax=ex.tax, total=ex.total, currency=ex.currency,
            supplier_norm=norm_supplier(ex.supplier_name),
            invoice_number_norm=norm_invoice_number(ex.invoice_number),
            field_confidence=it.field_confidence or None,
            field_evidence=it.field_evidence or None,
            raw_extraction={**it.raw, "ungrounded": it.ungrounded},
            provider=it.provider, model=it.model, prompt_version=it.prompt_version,
            tokens_in=it.tokens_in, tokens_out=it.tokens_out, latency_ms=it.latency_ms,
            needs_review=bool(it.ungrounded),
            review_reason=[f"UNGROUNDED:{f}" for f in it.ungrounded] or None,
        )
        inv.line_items = [
            LineItem(position=i, description=li.description, quantity=li.quantity,
                     unit_price=li.unit_price, line_total=li.line_total, tax_rate=li.tax_rate,
                     raw=li.model_dump(mode="json"))
            for i, li in enumerate(ex.line_items)]
        db.add(inv)


def extract_document(db: Session, document_id: uuid.UUID | str, will_retry: bool = False) -> dict:
    """Run the extraction stage. Marks the document failed (friendly message) on error."""
    doc_id = uuid.UUID(str(document_id))

    def work() -> dict:
        t0 = time.time()
        doc = db.get(Document, doc_id)
        if doc is None:
            raise DocumentError(ErrorCode.INTERNAL_ERROR, f"Document {doc_id} does not exist")
        doc.status, doc.error_code, doc.error_message = "extracting", None, None
        pipeline.add_event(db, doc_id, "started", "Extracting invoices", stage="extraction")
        db.commit()

        ext = doc.file_ext.lower().lstrip(".")
        items, info = (_from_sheets(db, doc) if ext in pipeline.SHEET_EXTS
                       else _from_text(db, doc, will_retry))
        _persist(db, doc, items)

        ms = int((time.time() - t0) * 1000)
        doc.status, doc.progress_percent = "validating", EXTRACTION_PROGRESS
        info = {**info, "invoices": len(items), "ungrounded_invoices":
                sum(1 for i in items if i.ungrounded),
                "tokens_in": sum(i.tokens_in for i in items),
                "tokens_out": sum(i.tokens_out for i in items)}
        pipeline.add_event(db, doc_id, "succeeded", f"{len(items)} invoice(s) extracted", ms,
                        info, stage="extraction")
        db.commit()
        return {"document_id": str(doc_id), "invoices": len(items), "duration_ms": ms}

    return pipeline.run_stage(db, doc_id, will_retry, work, stage="extraction")

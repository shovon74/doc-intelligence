"""Keyword and semantic search over the indexed chunks of the user's own documents.

Snippets mark matches with [[ ]] (never HTML), so the client can render them safely.
"""
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, Page, get_current_user, pagination
from app.db.models import Document, DocumentChunk, Invoice
from app.db.session import get_db
from app.llm.factory import resolve_embedder
from app.schemas.api import PageOut

router = APIRouter(prefix="/search", tags=["Search"])

HEADLINE = "StartSel=[[, StopSel=]], MaxFragments=1, MinWords=8, MaxWords=35"
SNIPPET_CHARS = 300


class Filters(BaseModel):
    supplier: str | None = Field(None, max_length=200)
    document_id: uuid.UUID | None = None
    needs_review: bool | None = None
    date_from: date | None = None
    date_to: date | None = None
    min_total: Decimal | None = None
    max_total: Decimal | None = None


class SemanticIn(Filters):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(10, ge=1, le=50)


class Hit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    invoice_id: uuid.UUID | None
    invoice_number: str | None
    supplier_name: str | None
    total: Decimal | None
    kind: str                       # "invoice" summary or "page" text
    snippet: str
    score: float


def _scoped(user: CurrentUser, f: Filters, *columns):
    stmt = (select(DocumentChunk, Document, Invoice, *columns)
            .join(Document, Document.id == DocumentChunk.document_id)
            .outerjoin(Invoice, Invoice.id == DocumentChunk.invoice_id)
            .where(Document.user_id == user.id))
    if f.document_id:
        stmt = stmt.where(Document.id == f.document_id)
    if f.supplier:
        esc = f.supplier.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Invoice.supplier_name.ilike(f"%{esc}%", escape="\\"))
    if f.needs_review is not None:
        stmt = stmt.where(Invoice.needs_review.is_(f.needs_review))
    if f.date_from:
        stmt = stmt.where(Invoice.invoice_date >= f.date_from)
    if f.date_to:
        stmt = stmt.where(Invoice.invoice_date <= f.date_to)
    if f.min_total is not None:
        stmt = stmt.where(Invoice.total >= f.min_total)
    if f.max_total is not None:
        stmt = stmt.where(Invoice.total <= f.max_total)
    return stmt


def _hit(chunk: DocumentChunk, doc: Document, inv: Invoice | None, snippet: str,
         score: float) -> Hit:
    return Hit(chunk_id=chunk.id, document_id=doc.id, filename=doc.original_filename,
               invoice_id=inv.id if inv else None, invoice_number=inv.invoice_number if inv else None,
               supplier_name=inv.supplier_name if inv else None, total=inv.total if inv else None,
               kind="invoice" if chunk.chunk_index == 0 else "page", snippet=snippet,
               score=round(float(score), 4))


@router.get("", response_model=PageOut[Hit], summary="Keyword search with filters")
def keyword_search(q: str = Query(min_length=1, max_length=200),
                   supplier: str | None = Query(None, max_length=200),
                   document_id: uuid.UUID | None = None, needs_review: bool | None = None,
                   date_from: date | None = None, date_to: date | None = None,
                   min_total: Decimal | None = None, max_total: Decimal | None = None,
                   page: Page = Depends(pagination), db: Session = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    f = Filters(supplier=supplier, document_id=document_id, needs_review=needs_review,
                date_from=date_from, date_to=date_to, min_total=min_total, max_total=max_total)
    tsq = func.websearch_to_tsquery("english", q)
    base = _scoped(user, f).where(DocumentChunk.tsv.op("@@")(tsq))
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rank = func.ts_rank(DocumentChunk.tsv, tsq)
    rows = db.execute(
        base.add_columns(func.ts_headline("english", DocumentChunk.content, tsq, HEADLINE),
                         rank.label("rank"))
        .order_by(rank.desc(), DocumentChunk.id).offset(page.offset).limit(page.page_size)).all()
    return PageOut(items=[_hit(c, d, i, snip, r) for c, d, i, snip, r in rows], total=total,
                   page=page.page, page_size=page.page_size)


@router.post("/semantic", response_model=list[Hit],
             summary="Semantic search (needs an embedding provider)")
def semantic_search(body: SemanticIn, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    try:
        embedder = resolve_embedder()
    except Exception:
        embedder = None
    if embedder is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Semantic search needs an embedding provider. Set an OpenAI or "
                            "Gemini API key, or use keyword search.")
    try:
        (vector,) = embedder.embed([body.query])
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The embedding provider is unavailable right now. Try again shortly.")
    distance = DocumentChunk.embedding.cosine_distance(vector)
    rows = db.execute(
        _scoped(user, body, distance.label("distance"))
        .where(DocumentChunk.embedding.is_not(None),
               DocumentChunk.embedding_provider == embedder.name,
               DocumentChunk.embedding_model == embedder.embedding_model)
        .order_by(distance, DocumentChunk.id).limit(body.limit)).all()
    return [_hit(c, d, i, c.content[:SNIPPET_CHARS], 1 - dist) for c, d, i, dist in rows]

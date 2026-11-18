"""
Search index: document_chunks rows for keyword (tsvector) and semantic (pgvector) search.

Per document: one summary chunk per invoice (chunk_index 0, linked to the invoice) and the
page text split into chunks (chunk_index >= 1, linked to the invoice covering that page).
Embeddings are optional: without a provider (or when it fails) keyword search still works.
"""
import logging
import uuid

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.db.models import DocumentChunk, DocumentPage, Invoice
from app.llm.factory import resolve_embedder

logger = logging.getLogger(__name__)

CHUNK_CHARS = 1200
EMBED_BATCH = 64
MAX_EMBED_CHARS = 6000


def invoice_summary(inv: Invoice) -> str:
    parts = [f"Invoice {inv.invoice_number or '(no number)'}"]
    for label, value in (("Supplier", inv.supplier_name), ("Customer", inv.customer_name),
                         ("Date", inv.invoice_date), ("Subtotal", inv.subtotal), ("Tax", inv.tax),
                         ("Total", inv.total), ("Currency", inv.currency)):
        if value is not None:
            parts.append(f"{label}: {value}")
    items = [li.description for li in inv.line_items if li.description]
    if items:
        parts.append("Items: " + "; ".join(items[:20]))
    return "\n".join(parts)


def split_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Pack paragraphs into chunks of at most `size` characters (long ones are cut)."""
    chunks, current = [], ""
    for para in (p.strip() for p in text.split("\n\n")):
        while len(para) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:size])
            para = para[size:]
        if para and len(current) + len(para) + 2 > size and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def _embed(texts: list[str]):
    """(vectors, provider, model) or (None, None, None) when unavailable."""
    if not texts:
        return [], None, None
    try:
        embedder = resolve_embedder()
        if embedder is None:
            return None, None, None
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            vectors += embedder.embed([t[:MAX_EMBED_CHARS] for t in texts[i:i + EMBED_BATCH]])
        return vectors, embedder.name, embedder.embedding_model
    except Exception as e:      # keyword search must survive any embedding failure
        logger.warning(f"Embedding skipped: {e!r}")
        return None, None, None


def _add(db: Session, document_id, invoice_id, index: int, texts: list[str]) -> None:
    vectors, provider, model = _embed(texts)
    for i, text in enumerate(texts):
        db.add(DocumentChunk(
            document_id=document_id, invoice_id=invoice_id, chunk_index=index + i, content=text,
            embedding=vectors[i] if vectors else None, embedding_provider=provider,
            embedding_model=model))


def _fill_tsv(db: Session, **where) -> None:
    db.flush()
    stmt = update(DocumentChunk).values(tsv=func.to_tsvector("english", DocumentChunk.content))
    for col, val in where.items():
        stmt = stmt.where(getattr(DocumentChunk, col) == val)
    db.execute(stmt.where(DocumentChunk.tsv.is_(None)))


def index_document(db: Session, document_id: uuid.UUID | str) -> int:
    """Rebuild every chunk of a document. Returns the number of chunks."""
    doc_id = uuid.UUID(str(document_id))
    db.query(DocumentChunk).filter(DocumentChunk.document_id == doc_id).delete()
    invoices = db.query(Invoice).filter(Invoice.document_id == doc_id).order_by(
        Invoice.segment_index).all()
    pages = db.query(DocumentPage).filter(DocumentPage.document_id == doc_id,
                                          DocumentPage.status == "completed").order_by(
        DocumentPage.page_number).all()

    rows: list[tuple[uuid.UUID | None, int, str]] = [(inv.id, 0, invoice_summary(inv))
                                                     for inv in invoices]
    n = 1
    for page in pages:
        owner = next((i.id for i in invoices if i.page_start <= page.page_number <= i.page_end), None)
        for piece in split_text(page.text or ""):
            rows.append((owner, n, piece))
            n += 1

    vectors, provider, model = _embed([r[2] for r in rows])
    for i, (invoice_id, index, text) in enumerate(rows):
        db.add(DocumentChunk(
            document_id=doc_id, invoice_id=invoice_id, chunk_index=index, content=text,
            embedding=vectors[i] if vectors else None, embedding_provider=provider,
            embedding_model=model))
    _fill_tsv(db, document_id=doc_id)
    db.commit()
    return len(rows)


def reindex_invoice(db: Session, inv: Invoice) -> None:
    """Replace one invoice's summary chunk after a correction."""
    db.query(DocumentChunk).filter(DocumentChunk.invoice_id == inv.id,
                                   DocumentChunk.chunk_index == 0).delete()
    _add(db, inv.document_id, inv.id, 0, [invoice_summary(inv)])
    _fill_tsv(db, invoice_id=inv.id, chunk_index=0)
    db.commit()


def safe_reindex_invoice(db: Session, inv: Invoice) -> None:
    try:
        reindex_invoice(db, inv)
    except Exception:
        db.rollback()
        logger.exception(f"Re-index failed for invoice {inv.id}")

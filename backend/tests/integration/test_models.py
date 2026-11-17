"""Phase 1 model tests: every table inserts and reads back, constraints and indexes exist."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Correction, Document, DocumentChunk, DocumentPage, Invoice, LineItem, ProcessingEvent, User,
    ValidationIssue,
)

pytestmark = pytest.mark.integration


def make_user(db, email=None):
    u = User(email=email or f"{uuid.uuid4()}@example.com", password_hash="x")
    db.add(u)
    db.flush()
    return u


def make_doc(db, user, **kw):
    d = Document(user_id=user.id, original_filename="a.pdf", stored_path="/data/a.pdf",
                 mime_type="application/pdf", file_ext="pdf", size_bytes=10, sha256="0" * 64, **kw)
    db.add(d)
    db.flush()
    return d


def make_invoice(db, doc, **kw):
    inv = Invoice(document_id=doc.id, **kw)
    db.add(inv)
    db.flush()
    return inv


def test_user_insert_read_defaults_and_unique_email(db):
    u = make_user(db, "dup@example.com")
    db.refresh(u)
    assert u.role == "user" and u.created_at is not None and isinstance(u.id, uuid.UUID)
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(User(email="dup@example.com", password_hash="y"))
            db.flush()


def test_document_defaults(db):
    d = make_doc(db, make_user(db))
    db.refresh(d)
    assert d.status == "queued" and d.progress_percent == 0.0 and d.page_count == 0
    assert d.error_code is None and d.updated_at is not None


def test_document_page_roundtrip_and_unique_page_number(db):
    d = make_doc(db, make_user(db))
    db.add(DocumentPage(document_id=d.id, page_number=1, text="hello", ocr_confidence=0.91,
                        char_count=5, source_type="ocr"))
    db.flush()
    page = db.scalar(select(DocumentPage).where(DocumentPage.document_id == d.id))
    assert page.text == "hello" and page.ocr_confidence == pytest.approx(0.91)
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(DocumentPage(document_id=d.id, page_number=1, text="dup"))
            db.flush()


def test_invoice_exact_decimals_jsonb_and_array(db):
    d = make_doc(db, make_user(db))
    inv = make_invoice(
        db, d, invoice_number="INV-1", invoice_date=date(2024, 5, 1), supplier_name="ACME",
        total=Decimal("1234.5678"), tax=Decimal("0.0001"), currency="EUR",
        field_confidence={"total": 0.98}, field_evidence={"total": {"page": 1, "text": "1,234.57"}},
        raw_extraction={"line_items": [{"description": "x"}]},
        review_reason=["MISSING_FIELD", "TOTAL_MISMATCH"], tokens_in=10, tokens_out=5)
    db.expire(inv)
    got = db.get(Invoice, inv.id)
    assert got.total == Decimal("1234.5678") and got.tax == Decimal("0.0001")
    assert got.invoice_date == date(2024, 5, 1)
    assert got.field_confidence == {"total": 0.98}
    assert got.field_evidence["total"]["page"] == 1
    assert got.review_reason == ["MISSING_FIELD", "TOTAL_MISMATCH"]
    assert got.needs_review is False and got.is_corrected is False


def test_line_items_ordered_by_position(db):
    d = make_doc(db, make_user(db))
    inv = make_invoice(db, d)
    for pos in (2, 0, 1):
        db.add(LineItem(invoice_id=inv.id, position=pos, description=f"item{pos}",
                        quantity=Decimal("2"), unit_price=Decimal("1.5"),
                        line_total=Decimal("3"), raw={"p": pos}))
    db.flush()
    db.refresh(inv)
    assert [li.description for li in inv.line_items] == ["item0", "item1", "item2"]
    assert inv.line_items[0].line_total == Decimal("3.0000")


def test_validation_issue_correction_and_event(db):
    u = make_user(db)
    d = make_doc(db, u)
    inv = make_invoice(db, d)
    db.add_all([
        ValidationIssue(document_id=d.id, invoice_id=inv.id, code="TOTAL_MISMATCH",
                        severity="error", field="total", message="m", details={"diff": "1.00"}),
        ValidationIssue(document_id=d.id, code="FILE_LEVEL", severity="info", message="doc-level"),
        Correction(invoice_id=inv.id, user_id=u.id, field="total", old_value="1", new_value="2"),
        ProcessingEvent(document_id=d.id, stage="processing", status="succeeded",
                        duration_ms=12, details={"pages": 3}),
    ])
    db.flush()
    db.refresh(d)
    db.refresh(inv)
    assert len(d.validation_issues) == 2 and inv.validation_issues[0].resolved is False
    assert inv.corrections[0].new_value == "2" and inv.corrections[0].user.id == u.id
    assert d.events[0].details == {"pages": 3}
    nullable = [i for i in d.validation_issues if i.invoice_id is None]
    assert len(nullable) == 1                                  # document-level issue allowed


def test_chunk_fulltext_and_vector_roundtrip(db):
    d = make_doc(db, make_user(db))
    inv = make_invoice(db, d)
    vec = [0.0] * 768
    vec[0] = 1.0
    chunk = DocumentChunk(document_id=d.id, invoice_id=inv.id, content="Invoice from ACME Corp",
                          embedding=vec, embedding_provider="fake", embedding_model="fake-1",
                          tsv=func.to_tsvector("english", "Invoice from ACME Corp"))
    db.add(chunk)
    db.flush()
    db.expire(chunk)
    got = db.get(DocumentChunk, chunk.id)
    assert list(got.embedding)[:2] == [1.0, 0.0] and len(got.embedding) == 768
    hit = db.scalar(select(DocumentChunk.id).where(
        DocumentChunk.tsv.op("@@")(func.plainto_tsquery("english", "acme"))))
    assert hit == chunk.id
    other = [0.0] * 768
    other[1] = 1.0
    db.add(DocumentChunk(document_id=d.id, chunk_index=1, content="far", embedding=other))
    db.flush()
    nearest = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == d.id)
                         .order_by(DocumentChunk.embedding.cosine_distance(vec)).limit(1)).one()
    assert nearest.id == chunk.id


def test_cascade_delete_document_removes_children(db):
    u = make_user(db)
    d = make_doc(db, u)
    inv = make_invoice(db, d)
    db.add_all([
        DocumentPage(document_id=d.id, page_number=1, text="t"),
        LineItem(invoice_id=inv.id, description="x"),
        ValidationIssue(document_id=d.id, invoice_id=inv.id, code="C", severity="info", message="m"),
        Correction(invoice_id=inv.id, user_id=u.id, field="f"),
        ProcessingEvent(document_id=d.id, stage="s", status="started"),
        DocumentChunk(document_id=d.id, invoice_id=inv.id, content="c"),
    ])
    db.flush()
    db.delete(d)
    db.flush()
    for model in (DocumentPage, Invoice, LineItem, ValidationIssue, Correction,
                  ProcessingEvent, DocumentChunk):
        assert db.scalar(select(func.count()).select_from(model)
                         .where(model.id.isnot(None))) is not None
    assert db.get(Invoice, inv.id) is None
    assert db.scalar(select(func.count()).select_from(DocumentChunk)
                     .where(DocumentChunk.document_id == d.id)) == 0
    assert db.scalar(select(func.count()).select_from(LineItem)
                     .where(LineItem.invoice_id == inv.id)) == 0
    assert db.scalar(select(func.count()).select_from(Correction)
                     .where(Correction.invoice_id == inv.id)) == 0


def test_deleting_user_cascades_to_documents(db):
    u = make_user(db)
    d = make_doc(db, u)
    db.flush()
    db.delete(u)
    db.flush()
    db.expire_all()
    assert db.get(Document, d.id) is None


def _indexdefs(db, table):
    rows = db.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
                      {"t": table}).all()
    return {name: definition for name, definition in rows}


def test_search_indexes_exist_gin_and_hnsw(db):
    defs = _indexdefs(db, "document_chunks")
    assert "USING gin" in defs["ix_document_chunks_tsv"]
    hnsw = defs["ix_document_chunks_embedding"]
    assert "USING hnsw" in hnsw and "vector_cosine_ops" in hnsw
    assert "m='16'" in hnsw.replace(" ", "") or "m=16" in hnsw.replace(" ", "")


def test_lookup_indexes_exist(db):
    inv = _indexdefs(db, "invoices")
    assert "ix_invoices_supplier_invoice_norm" in inv
    assert "(supplier_norm, invoice_number_norm)" in inv["ix_invoices_supplier_invoice_norm"]
    assert "ix_documents_user_status" in _indexdefs(db, "documents")
    assert "uq_document_pages_doc_page" in _indexdefs(db, "document_pages")

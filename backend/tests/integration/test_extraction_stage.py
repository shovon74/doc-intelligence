"""Extraction stage against real Postgres: rows, relations, idempotent re-run."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import Document, DocumentPage, Invoice, ProcessingEvent, User
from app.extraction import stage
from app.llm.factory import Resolution
from app.llm.fake import FakeProvider

pytestmark = pytest.mark.integration

PAGE = ("Invoice Number: INV-7\nInvoice Date: 2024-03-05\nSupplier: ACME Ltd\n"
        "Subtotal: 100.00\nTax: 20.00\nTotal: 120.00\nCurrency: EUR")


def _setup(db):
    u = User(email="stage@example.com", password_hash="x")
    db.add(u)
    db.flush()
    d = Document(user_id=u.id, original_filename="a.pdf", stored_path="/x", mime_type="a/b",
                 file_ext="pdf", size_bytes=1, sha256="1" * 64, status="extracting",
                 llm_provider_requested="fake")
    db.add(d)
    db.flush()
    db.add(DocumentPage(document_id=d.id, page_number=1, text=PAGE))
    db.flush()
    return d


def test_stage_writes_invoice_rows_and_is_idempotent(db, monkeypatch):
    monkeypatch.setattr(stage, "resolve_llm",
                        lambda requested=None: Resolution(FakeProvider()))
    d = _setup(db)
    for _ in range(2):                                   # re-run replaces, never duplicates
        stage.extract_document(db, d.id)
    invoices = db.scalars(select(Invoice).where(Invoice.document_id == d.id)).all()
    assert len(invoices) == 1
    inv = invoices[0]
    assert inv.total == Decimal("120.0000") and inv.supplier_norm == "acme"
    assert inv.invoice_number_norm == "INV7" and inv.provider == "fake"
    assert inv.prompt_version and inv.tokens_in > 0 and inv.needs_review is False
    db.refresh(d)
    assert d.status == "validating" and d.progress_percent == stage.EXTRACTION_PROGRESS
    events = db.scalars(select(ProcessingEvent).where(
        ProcessingEvent.document_id == d.id, ProcessingEvent.stage == "extraction",
        ProcessingEvent.status == "succeeded")).all()
    assert len(events) == 2

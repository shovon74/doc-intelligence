"""Validation stage and cross-check against real Postgres."""
import uuid
from datetime import date
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import CurrentUser, get_current_user
from app.db.models import Document, Invoice, LineItem, ProcessingEvent, User, ValidationIssue
from app.db.session import get_db
from app.main import app
from app.processing.errors import DocumentError, ErrorCode
from app.validation import engine
from app.validation.crosscheck import DocumentNotFound, DocumentNotReady, run_crosscheck

pytestmark = pytest.mark.integration
TODAY = date(2026, 6, 1)


def _user(db, email):
    u = User(email=email, password_hash="x")
    db.add(u)
    db.flush()
    return u


def _doc(db, user, name="a.pdf", status="validating"):
    d = Document(user_id=user.id, original_filename=name, stored_path="/x", mime_type="a/b",
                 file_ext="pdf", size_bytes=1, sha256="1" * 64, status=status)
    db.add(d)
    db.flush()
    return d


def _inv(db, doc, idx=0, number="INV-1", supplier="ACME Ltd", **kw):
    f = dict(invoice_number=number, invoice_date=date(2026, 5, 1), supplier_name=supplier,
             subtotal=D("100"), tax=D("20"), total=D("120"), currency="EUR",
             supplier_norm="acme", invoice_number_norm=number.replace("-", "").upper())
    inv = Invoice(document_id=doc.id, segment_index=idx, **{**f, **kw})
    db.add(inv)
    db.flush()
    return inv


def _issues(db, inv):
    return {i.code for i in db.scalars(select(ValidationIssue).where(
        ValidationIssue.invoice_id == inv.id))}


def test_clean_document_completes_and_is_idempotent(db):
    d = _doc(db, _user(db, "v1@example.com"))
    inv = _inv(db, d)
    for _ in range(2):
        out = engine.validate_document(db, d.id, today=TODAY)
    assert out["status"] == "completed" and out["issues"] == 0
    db.refresh(d)
    db.refresh(inv)
    assert d.status == "completed" and d.progress_percent == 100 and d.completed_at
    assert inv.needs_review is False and inv.review_reason is None
    events = db.scalars(select(ProcessingEvent).where(
        ProcessingEvent.document_id == d.id, ProcessingEvent.stage == "validation",
        ProcessingEvent.status == "succeeded")).all()
    assert len(events) == 2


def test_flagged_invoice_sets_needs_review_and_keeps_ungrounded_reasons(db):
    d = _doc(db, _user(db, "v2@example.com"))
    inv = _inv(db, d, total=D("999"), review_reason=["UNGROUNDED:total"], needs_review=True)
    out = engine.validate_document(db, d.id, today=TODAY)
    db.refresh(d)
    db.refresh(inv)
    assert out["status"] == "needs_review" and d.status == "needs_review"
    assert inv.needs_review and inv.review_reason == ["UNGROUNDED:total", "TOTAL_MISMATCH"]
    assert _issues(db, inv) == {"TOTAL_MISMATCH"}
    engine.validate_document(db, d.id, today=TODAY)              # re-run: no stale or doubled rows
    assert db.query(ValidationIssue).filter_by(invoice_id=inv.id).count() == 1


def test_revalidation_after_a_fix_clears_the_flag(db):
    d = _doc(db, _user(db, "v3@example.com"))
    inv = _inv(db, d, total=D("999"))
    engine.validate_document(db, d.id, today=TODAY)
    inv.total = D("120")
    engine.validate_invoice(db, inv, TODAY)
    assert engine.settle_document(db, d) == "completed"
    assert inv.needs_review is False and _issues(db, inv) == set()


def test_duplicates_across_documents_reupload_and_within_a_document(db):
    u = _user(db, "v4@example.com")
    first, second, third = (_doc(db, u, f"f{i}.pdf") for i in range(3))
    original = _inv(db, first, supplier="ACME Ltd")
    reupload = _inv(db, second, supplier="Acme Limited")           # same file uploaded again
    row2 = _inv(db, third, idx=0)
    row3 = _inv(db, third, idx=1)                                  # repeated row in one document
    for d in (first, second, third):
        engine.validate_document(db, d.id, today=TODAY)
    assert "DUPLICATE_INVOICE" not in _issues(db, original)        # the original is not flagged
    for dup in (reupload, row2, row3):
        assert "DUPLICATE_INVOICE" in _issues(db, dup)
        assert dup.needs_review and "DUPLICATE_INVOICE" in dup.review_reason
    issue = db.scalars(select(ValidationIssue).where(
        ValidationIssue.invoice_id == reupload.id,
        ValidationIssue.code == "DUPLICATE_INVOICE")).one()
    assert issue.details["original_invoice_id"] == str(original.id)
    db.refresh(second)
    assert second.status == "needs_review"


def test_duplicates_never_cross_users_or_match_different_numbers(db):
    a, b = _user(db, "v5a@example.com"), _user(db, "v5b@example.com")
    da, db_, dc = _doc(db, a), _doc(db, b), _doc(db, a, "other.pdf")
    _inv(db, da)
    other_user = _inv(db, db_)
    other_number = _inv(db, dc, number="INV-2")
    for d in (da, db_, dc):
        engine.validate_document(db, d.id, today=TODAY)
    assert "DUPLICATE_INVOICE" not in _issues(db, other_user) | _issues(db, other_number)


def test_missing_document_fails_with_internal_error(db):
    with pytest.raises(DocumentError) as e:
        engine.validate_document(db, uuid.uuid4())
    assert e.value.code is ErrorCode.INTERNAL_ERROR


# ------------------------------------------------------------------ cross-check API
def _line(inv, i, total, qty=1):
    inv.line_items.append(LineItem(position=i, quantity=D(qty), unit_price=D(total),
                                   line_total=D(total)))


def test_crosscheck_pdf_vs_xlsx_through_the_api(db):
    u = _user(db, "c1@example.com")
    pdf, xlsx = _doc(db, u, "i.pdf", "completed"), _doc(db, u, "i.xlsx", "needs_review")
    xlsx.file_ext = "xlsx"
    p1 = _inv(db, pdf, 0)
    p2 = _inv(db, pdf, 1, number="INV-2", total=D("50"), subtotal=D("50"), tax=D(0))
    x1 = _inv(db, xlsx, 0, total=D("121.5"), tax=D("21.5"), invoice_date=date(2026, 5, 3))
    x2 = _inv(db, xlsx, 1, number="INV-2", total=D("50"), subtotal=D("50"), tax=D(0))
    for inv, rows in ((p1, (60, 40)), (x1, (60, 40)), (p2, (50,)), (x2, (50,))):
        for i, t in enumerate(rows):
            _line(inv, i, t)
    db.flush()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(u.id)
    try:
        client = TestClient(app)
        r = client.post("/api/v1/crosscheck", json={"document_a_id": str(pdf.id),
                                                    "document_b_id": str(xlsx.id)})
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "mismatch"
        by_number = {p["a"]["invoice_number"]: p for p in body["result"]["pairs"]}
        assert {d["field"] for d in by_number["INV-1"]["differences"]} == {
            "invoice_date", "tax", "total"}
        assert by_number["INV-2"]["differences"] == []
        assert body["result"]["only_in_a"] == [] and body["result"]["only_in_b"] == []

        got = client.get(f"/api/v1/crosscheck/{body['id']}")
        assert got.status_code == 200 and got.json()["result"] == body["result"]

        same = client.post("/api/v1/crosscheck", json={"document_a_id": str(pdf.id),
                                                       "document_b_id": str(pdf.id)})
        assert same.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_crosscheck_access_rules(db):
    owner, stranger = _user(db, "c2@example.com"), _user(db, "c3@example.com")
    done, queued = _doc(db, owner, status="completed"), _doc(db, owner, status="processing")
    other = _doc(db, stranger, status="completed")
    with pytest.raises(DocumentNotFound):
        run_crosscheck(db, owner.id, done.id, other.id)            # not the user's document
    with pytest.raises(DocumentNotReady):
        run_crosscheck(db, owner.id, done.id, queued.id)
    row = run_crosscheck(db, owner.id, done.id, _doc(db, owner, "b.pdf", "completed").id)
    assert row.status == "match" and row.result["summary"]["paired"] == 0

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(stranger.id)
    try:
        client = TestClient(app)
        assert client.get(f"/api/v1/crosscheck/{row.id}").status_code == 404   # other user's result
        assert client.post("/api/v1/crosscheck", json={
            "document_a_id": str(done.id), "document_b_id": str(queued.id)}).status_code == 404
    finally:
        app.dependency_overrides.clear()
    assert client.get(f"/api/v1/crosscheck/{row.id}").status_code == 401   # no auth wired yet

"""REST API against real Postgres: auth, upload, documents, invoices, isolation, envelope."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import Document, Invoice, LineItem, ValidationIssue
from app.db.session import get_db
from app.main import app
from app.storage import file_handler
from app.workers import tasks

pytestmark = pytest.mark.integration
API = "/api/v1"
CSV = b"Invoice No,Total\nA-1,10\n"


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    monkeypatch.setattr(file_handler, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    delay = MagicMock()
    monkeypatch.setattr(tasks.process_document_task, "delay", delay)
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app, raise_server_exceptions=False)
    c.delay = delay
    yield c
    app.dependency_overrides.clear()


def signup(client, email, password="correct horse"):
    assert client.post(f"{API}/auth/register", json={"email": email, "password": password}
                       ).status_code == 201
    token = client.post(f"{API}/auth/login", json={"email": email, "password": password}
                        ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def upload(client, headers, name="a.csv", data=CSV, **form):
    return client.post(f"{API}/documents", headers=headers, data=form,
                       files=[("files", (name, data, "text/csv"))])


def make_invoice(db, doc_id, **kw):
    f = dict(invoice_number="INV-1", invoice_date=date(2026, 5, 1), supplier_name="ACME Ltd", customer_name="Client",
             subtotal=D("100"), tax=D("20"), total=D("120"), currency="EUR", supplier_norm="acme",
             invoice_number_norm="INV1")
    inv = Invoice(document_id=doc_id, **{**f, **kw})
    db.add(inv)
    db.flush()
    return inv


# ------------------------------------------------------------------ auth
def test_register_login_me_and_bad_credentials(client):
    h = signup(client, "Auth@Example.com")
    me = client.get(f"{API}/auth/me", headers=h).json()
    assert me["email"] == "auth@example.com" and "password" not in str(me)
    dup = client.post(f"{API}/auth/register",
                      json={"email": "auth@example.com", "password": "another pass"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "CONFLICT"
    for bad in ({"email": "nope", "password": "longenough"}, {"email": "a@b.co", "password": "short"}):
        r = client.post(f"{API}/auth/register", json=bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
        assert "longenough" not in r.text                      # input is never echoed back
    for creds in ({"email": "auth@example.com", "password": "wrong password"},
                  {"email": "ghost@example.com", "password": "correct horse"}):
        r = client.post(f"{API}/auth/login", json=creds)
        assert r.status_code == 401 and r.json()["error"]["message"] == (
            "Email or password is incorrect.")                 # same answer for both


def test_tokens_expire_and_cannot_be_forged(client):
    h = signup(client, "tok@example.com")
    uid = client.get(f"{API}/auth/me", headers=h).json()["id"]
    import uuid
    expired, _ = create_access_token(uuid.UUID(uid), now=datetime.now(timezone.utc) - timedelta(days=3))
    forged = jwt.encode({"sub": uid, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                        "some-other-secret-that-is-long-enough!", algorithm="HS256")
    unsigned = jwt.encode({"sub": uid, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                          None, algorithm="none")
    for token in (expired, forged, unsigned, "garbage"):
        r = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, token
    assert client.get(f"{API}/auth/me").status_code == 401
    assert client.get(f"{API}/documents").status_code == 401


# ------------------------------------------------------------------ upload and documents
def test_upload_returns_202_and_waits_for_explicit_start(client):
    h = signup(client, "up@example.com")
    r = upload(client, h)
    assert r.status_code == 202
    (doc,) = r.json()["documents"]
    assert doc["status"] == "uploaded" and doc["duplicate"] is False and r.json()["rejected"] == []
    client.delay.assert_not_called()                            # no automatic processing

    started = client.post(f"{API}/documents/{doc['id']}/reprocess", headers=h)
    assert started.status_code == 202 and started.json()["status"] == "queued"
    client.delay.assert_called_once_with(doc["id"])

    again = upload(client, h).json()["documents"][0]            # same bytes again
    assert again["duplicate"] is True and again["id"] == doc["id"]
    assert client.delay.call_count == 1                         # never enqueued twice


def test_upload_rejections_use_the_error_envelope(client):
    h = signup(client, "bad@example.com")
    r = upload(client, h, name="x.exe", data=b"MZ\x90\x00" + b"\x00" * 64)
    assert r.status_code == 415 and r.json()["error"]["code"] == "UNSUPPORTED_TYPE"
    assert r.json()["error"]["correlation_id"] == r.headers["X-Correlation-ID"]
    assert upload(client, h, name="e.csv", data=b"").json()["error"]["code"] == "EMPTY_FILE"

    mixed = client.post(f"{API}/documents", headers=h, files=[
        ("files", ("ok.csv", CSV, "text/csv")),
        ("files", ("bad.exe", b"MZ\x90\x00" + b"\x00" * 64, "application/octet-stream"))])
    body = mixed.json()
    assert mixed.status_code == 202 and len(body["documents"]) == 1
    assert [x["filename"] for x in body["rejected"]] == ["bad.exe"]

    assert upload(client, h, name="p.csv", llm_provider="openai").status_code == 422   # no key set
    assert upload(client, h, name="q.csv", llm_provider="nonsense").status_code == 422
    too_many = client.post(f"{API}/documents", headers=h,
                           files=[("files", (f"{i}.csv", CSV, "text/csv")) for i in range(21)])
    assert too_many.status_code == 422


def test_unreachable_broker_fails_the_document_instead_of_hiding_it(client):
    client.delay.side_effect = RuntimeError("redis down")
    h = signup(client, "broker@example.com")
    doc = upload(client, h).json()["documents"][0]
    assert doc["status"] == "uploaded"
    started = client.post(f"{API}/documents/{doc['id']}/reprocess", headers=h)
    assert started.status_code == 202 and started.json()["status"] == "failed"
    got = client.get(f"{API}/documents/{doc['id']}", headers=h).json()
    assert got["error_code"] == "INTERNAL_ERROR" and got["error_message"]


def test_list_detail_status_file_reprocess_and_delete(client, db):
    h = signup(client, "docs@example.com")
    ids = []
    for i in range(3):
        ids.append(upload(client, h, name=f"f{i}.csv",
                          data=CSV + f"B-{i},{i}\n".encode()).json()["documents"][0]["id"])
    first = db.get(Document, __import__("uuid").UUID(ids[0]))
    first.status = "failed"
    first.error_code, first.error_message = "OCR_FAILED", "Could not read the scan."
    make_invoice(db, first.id, needs_review=True)
    db.flush()

    page = client.get(f"{API}/documents?page_size=2&page=1", headers=h).json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["page_size"] == 2
    assert client.get(f"{API}/documents?page_size=2&page=2", headers=h).json()["items"][0]["id"] == ids[0]
    failed = client.get(f"{API}/documents?status=failed", headers=h).json()
    assert [d["id"] for d in failed["items"]] == [ids[0]]
    assert failed["items"][0]["invoice_count"] == 1 and failed["items"][0]["needs_review_count"] == 1
    assert client.get(f"{API}/documents?q=f1", headers=h).json()["total"] == 1
    assert client.get(f"{API}/documents?q=%25", headers=h).json()["total"] == 0     # no wildcard abuse
    assert client.get(f"{API}/documents?status=bogus", headers=h).status_code == 422
    assert client.get(f"{API}/documents?page_size=1000", headers=h).status_code == 422

    detail = client.get(f"{API}/documents/{ids[0]}", headers=h).json()
    assert detail["status"] == "failed" and len(detail["invoices"]) == 1 and "stored_path" not in detail
    st = client.get(f"{API}/documents/{ids[0]}/status", headers=h).json()
    assert st["status"] == "failed" and st["error_code"] == "OCR_FAILED"

    f = client.get(f"{API}/documents/{ids[0]}/file", headers=h)
    assert f.status_code == 200 and f.content.startswith(b"Invoice No")

    client.delay.reset_mock()
    r = client.post(f"{API}/documents/{ids[0]}/reprocess", headers=h)       # failed -> queued again
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["error_code"] is None
    client.delay.assert_called_once_with(ids[0])
    assert client.post(f"{API}/documents/{ids[0]}/reprocess", headers=h).status_code == 409
    assert client.delete(f"{API}/documents/{ids[0]}", headers=h).status_code == 409

    first.status = "completed"
    db.flush()
    path = file_handler.UPLOAD_DIR / ids[0]
    assert path.exists()
    assert client.delete(f"{API}/documents/{ids[0]}", headers=h).status_code == 204
    assert not path.exists()
    assert client.get(f"{API}/documents/{ids[0]}", headers=h).status_code == 404


# ------------------------------------------------------------------ isolation
def test_another_users_data_is_invisible_everywhere(client, db):
    a, b = signup(client, "own@example.com"), signup(client, "intruder@example.com")
    doc_id = upload(client, a).json()["documents"][0]["id"]
    inv = make_invoice(db, db.get(Document, __import__("uuid").UUID(doc_id)).id)

    assert client.get(f"{API}/documents", headers=b).json()["total"] == 0
    assert client.get(f"{API}/invoices", headers=b).json()["total"] == 0
    for method, url, kw in [
            ("get", f"/documents/{doc_id}", {}), ("get", f"/documents/{doc_id}/status", {}),
            ("get", f"/documents/{doc_id}/file", {}), ("post", f"/documents/{doc_id}/reprocess", {}),
            ("delete", f"/documents/{doc_id}", {}), ("get", f"/invoices/{inv.id}", {}),
            ("patch", f"/invoices/{inv.id}", {"json": {"total": "1"}})]:
        r = getattr(client, method)(API + url, headers=b, **kw)
        assert r.status_code == 404, (method, url)
        assert r.json()["error"]["code"] == "NOT_FOUND"
    assert client.get(f"{API}/documents/{doc_id}", headers=a).status_code == 200   # owner unaffected
    # same bytes uploaded by the other user is their own document, not the first user's
    other = upload(client, b).json()["documents"][0]
    assert other["id"] != doc_id and other["duplicate"] is False


# ------------------------------------------------------------------ invoices
def test_invoice_filters_pagination_and_detail(client, db):
    import uuid
    h = signup(client, "inv@example.com")
    doc = db.get(Document, uuid.UUID(upload(client, h).json()["documents"][0]["id"]))
    make_invoice(db, doc.id, invoice_number="A-1", supplier_name="ACME Ltd", total=D("50"),
                 subtotal=D("50"), tax=D(0), invoice_date=date(2026, 1, 5))
    make_invoice(db, doc.id, invoice_number="B-2", supplier_name="Beta GmbH", total=D("500"),
                 subtotal=D("500"), tax=D(0), invoice_date=date(2026, 3, 5), currency="USD",
                 needs_review=True, review_reason=["TOTAL_MISMATCH"])
    inv_c = make_invoice(db, doc.id, invoice_number="C_3", supplier_name="100% Co", total=D("5"),
                         subtotal=D("5"), tax=D(0), invoice_date=None)
    inv_c.line_items = [LineItem(position=0, description="Widget", quantity=D(1),
                                 unit_price=D(5), line_total=D(5))]
    db.flush()

    def ids(qs):
        r = client.get(f"{API}/invoices?{qs}", headers=h)
        assert r.status_code == 200, r.text
        return [i["invoice_number"] for i in r.json()["items"]]

    assert ids("") == ["B-2", "A-1", "C_3"]                        # newest date first, null last
    assert ids("supplier=acme") == ["A-1"] and ids("supplier=%25") == ["C_3"]
    assert ids("invoice_number=_") == ["C_3"]                      # underscore is literal
    assert ids("needs_review=true") == ["B-2"] and ids("currency=usd") == ["B-2"]
    assert ids("date_from=2026-02-01") == ["B-2"] and ids("date_to=2026-02-01") == ["A-1"]
    assert ids("min_total=10&max_total=100") == ["A-1"]
    assert client.get(f"{API}/invoices?page_size=2&page=2", headers=h).json()["total"] == 3
    assert ids("page_size=2&page=2") == ["C_3"]
    assert client.get(f"{API}/invoices?min_total=abc", headers=h).status_code == 422

    detail = client.get(f"{API}/invoices/{inv_c.id}", headers=h).json()
    assert detail["line_items"][0]["description"] == "Widget" and "raw_extraction" not in detail


def test_correcting_an_invoice_revalidates_it_and_settles_the_document(client, db):
    import uuid
    h = signup(client, "fix@example.com")
    doc = db.get(Document, uuid.UUID(upload(client, h).json()["documents"][0]["id"]))
    doc.status = "needs_review"
    inv = make_invoice(db, doc.id, total=D("999"), needs_review=True,
                       review_reason=["UNGROUNDED:total", "TOTAL_MISMATCH"])
    db.add(ValidationIssue(document_id=doc.id, invoice_id=inv.id, code="TOTAL_MISMATCH",
                           severity="error", field="total", message="m"))
    db.flush()

    r = client.patch(f"{API}/invoices/{inv.id}", headers=h, json={"total": "120.00"})
    assert r.status_code == 200
    body = r.json()
    assert D(body["total"]) == D("120") and body["is_corrected"] is True
    assert body["needs_review"] is False and body["review_reason"] is None and body["issues"] == []
    assert [(c["field"], D(c["old_value"]), D(c["new_value"])) for c in body["corrections"]] == [
        ("total", D("999"), D("120"))]
    db.refresh(doc)
    assert doc.status == "completed"

    r = client.patch(f"{API}/invoices/{inv.id}", headers=h,
                     json={"supplier_name": "Beta GmbH", "currency": "gbp", "tax": "50"})
    body = r.json()
    assert body["supplier_name"] == "Beta GmbH" and body["currency"] == "GBP"
    assert [i["code"] for i in body["issues"]] == ["TOTAL_MISMATCH", "TAX_ANOMALY"]   # errors first
    assert body["needs_review"] is True
    db.refresh(inv), db.refresh(doc)
    assert inv.supplier_norm == "beta" and doc.status == "needs_review"

    unchanged = client.patch(f"{API}/invoices/{inv.id}", headers=h, json={"currency": "GBP"})
    assert unchanged.status_code == 200 and len(unchanged.json()["corrections"]) == 4   # no new row
    for bad in ({}, {"total": "abc"}, {"currency": "EURO"}, {"unknown": 1}, {"total": "1e30"}):
        assert client.patch(f"{API}/invoices/{inv.id}", headers=h, json=bad).status_code == 422, bad


def test_correction_blocked_while_document_is_processing(client, db):
    import uuid
    h = signup(client, "busy@example.com")
    doc = db.get(Document, uuid.UUID(upload(client, h).json()["documents"][0]["id"]))
    doc.status = "queued"
    inv = make_invoice(db, doc.id)
    db.flush()
    assert client.patch(f"{API}/invoices/{inv.id}", headers=h,
                        json={"total": "1"}).status_code == 409           # still queued


# ------------------------------------------------------------------ providers, headers
def test_providers_endpoint_and_response_headers(client, monkeypatch):
    h = signup(client, "prov@example.com")
    assert client.get(f"{API}/providers").status_code == 401
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "auto")
    r = client.get(f"{API}/providers", headers=h)
    body = r.json()
    assert body["llm"]["configured"] == ["openai"] and "openai" in body["llm"]["options"]
    assert "sk-test" not in r.text                                    # keys are never exposed
    assert body["embedding"]["available"] is True and body["embedding"]["provider"] == "openai"
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    assert client.get(f"{API}/providers", headers=h).json()["embedding"]["available"] is False

    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in r.headers["Content-Security-Policy"]
    assert "Content-Security-Policy" not in client.get("/docs").headers


def test_envelope_for_unknown_routes_bad_ids_and_server_errors(client, monkeypatch):
    h = signup(client, "env@example.com")
    r = client.get(f"{API}/nothing-here", headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = client.get(f"{API}/documents/not-a-uuid", headers=h)
    assert r.status_code == 422 and r.json()["error"]["details"][0]["field"] == "document_id"
    mine = client.get(f"{API}/auth/me", headers={**h, "X-Correlation-ID": "trace-123"})
    assert mine.headers["X-Correlation-ID"] == "trace-123"
    bad = client.get(f"{API}/auth/me", headers={**h, "X-Correlation-ID": "evil\r\nheader: x"})
    assert bad.headers["X-Correlation-ID"] != "evil\r\nheader: x"

    monkeypatch.setattr("app.api.v1.providers.resolve_embedder",
                        MagicMock(side_effect=RuntimeError("secret internals")))
    r = client.get(f"{API}/providers", headers=h)
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internals" not in r.text


def test_cors_only_allows_listed_origins(client):
    ok = client.options(f"{API}/documents", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    bad = client.options(f"{API}/documents", headers={
        "Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers

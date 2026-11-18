"""Result, history, reprocess-failed, list filters and issue resolution."""
import uuid
from decimal import Decimal as D

import pytest

from app.db.models import Document, DocumentPage, ProcessingEvent, ValidationIssue
from tests.integration.test_api import API, CSV, client, make_invoice, signup, upload  # noqa: F401

pytestmark = pytest.mark.integration


def _doc(db, resp):
    return db.get(Document, uuid.UUID(resp.json()["documents"][0]["id"]))


def test_result_history_and_page_text(client, db):
    h = signup(client, "res@example.com")
    doc = _doc(db, upload(client, h))
    doc.status = "completed"
    make_invoice(db, doc.id)
    db.add(DocumentPage(document_id=doc.id, page_number=1, text="hello page"))
    db.add(ProcessingEvent(document_id=doc.id, stage="extraction", status="succeeded", message="ok"))
    db.flush()

    r = client.get(f"{API}/documents/{doc.id}/result", headers=h).json()
    assert r["document"]["id"] == str(doc.id) and len(r["invoices"]) == 1 and r["pages"] is None
    r = client.get(f"{API}/documents/{doc.id}/result?include_text=true", headers=h).json()
    assert [p["text"] for p in r["pages"]] == ["hello page"]

    hist = client.get(f"{API}/documents/{doc.id}/history", headers=h).json()
    assert [e["stage"] for e in hist] == ["extraction"]
    other = signup(client, "res2@example.com")
    for path in ("result", "history"):
        assert client.get(f"{API}/documents/{doc.id}/{path}", headers=other).status_code == 404


def test_reprocess_failed_requeues_only_your_failed_documents(client, db):
    h, other = signup(client, "rf@example.com"), signup(client, "rf2@example.com")
    failed = _doc(db, upload(client, h, name="a.csv", data=CSV + b"X,1\n"))
    fine = _doc(db, upload(client, h, name="b.csv", data=CSV + b"Y,2\n"))
    theirs = _doc(db, upload(client, other))
    for d in (failed, theirs):
        d.status, d.error_code = "failed", "OCR_FAILED"
    fine.status = "completed"
    db.flush()
    client.delay.reset_mock()

    r = client.post(f"{API}/documents/reprocess-failed", headers=h)
    assert r.status_code == 202 and r.json()["requeued"] == [str(failed.id)]
    client.delay.assert_called_once_with(str(failed.id))
    db.refresh(failed), db.refresh(theirs)
    assert failed.status == "queued" and failed.error_code is None
    assert theirs.status == "failed"                                  # not touched
    assert client.post(f"{API}/documents/reprocess-failed", headers=h).json()["requeued"] == []


def test_document_list_filters_and_sorting(client, db):
    h = signup(client, "flt@example.com")
    a = _doc(db, upload(client, h, name="alpha.csv", data=CSV + b"A,1\n"))
    b = _doc(db, upload(client, h, name="beta.csv", data=CSV + b"B,2\n"))
    b.file_ext = "xlsx"
    make_invoice(db, a.id, needs_review=True)
    make_invoice(db, b.id)
    db.flush()

    def names(qs):
        r = client.get(f"{API}/documents?{qs}", headers=h)
        assert r.status_code == 200, r.text
        return [d["original_filename"] for d in r.json()["items"]]

    assert names("type=xlsx") == ["beta.csv"] and names("type=.csv") == ["alpha.csv"]
    assert names("needs_review=true") == ["alpha.csv"] and names("needs_review=false") == ["beta.csv"]
    assert names("sort=filename&order=asc") == ["alpha.csv", "beta.csv"]
    assert names("sort=filename&order=desc") == ["beta.csv", "alpha.csv"]
    assert names("date_from=2999-01-01") == [] and len(names("date_to=2999-01-01")) == 2
    assert client.get(f"{API}/documents?sort=password", headers=h).status_code == 422
    assert client.get(f"{API}/documents?order=sideways", headers=h).status_code == 422


def test_resolving_issues_clears_the_flag_when_none_are_open(client, db):
    h, other = signup(client, "rsv@example.com"), signup(client, "rsv2@example.com")
    doc = _doc(db, upload(client, h))
    doc.status = "needs_review"
    inv = make_invoice(db, doc.id, needs_review=True,
                       review_reason=["UNGROUNDED:total", "TAX_ANOMALY", "LOW_CONFIDENCE"])
    issues = [ValidationIssue(document_id=doc.id, invoice_id=inv.id, code=c, severity="warning",
                              message="m") for c in ("TAX_ANOMALY", "LOW_CONFIDENCE")]
    db.add_all(issues)
    db.flush()

    url = lambda i: f"{API}/invoices/{inv.id}/resolve-issue/{i.id}"
    assert client.post(url(issues[0]), headers=other).status_code == 404
    r = client.post(url(issues[0]), headers=h).json()
    assert r["needs_review"] is True and r["review_reason"] == ["UNGROUNDED:total", "LOW_CONFIDENCE"]
    assert [i["resolved"] for i in sorted(r["issues"], key=lambda i: i["code"])] == [False, True]
    r = client.post(url(issues[1]), headers=h).json()                  # only UNGROUNDED left
    assert r["needs_review"] is True and r["review_reason"] == ["UNGROUNDED:total"]
    db.refresh(doc)
    assert doc.status == "needs_review"

    assert client.post(f"{API}/invoices/{inv.id}/resolve-issue/{uuid.uuid4()}",
                       headers=h).status_code == 404
    assert D(r["total"]) == D("120")

import uuid
from decimal import Decimal as D

from app.db.models import Document, LineItem
from tests.integration.test_api import API, client, make_invoice, signup, upload  # noqa: F401

import pytest

pytestmark = pytest.mark.integration


def test_line_items_can_be_edited_added_and_removed(client, db):
    h = signup(client, f"lines{uuid.uuid4().hex[:6]}@example.com")
    doc = db.get(Document, uuid.UUID(upload(client, h).json()["documents"][0]["id"]))
    doc.status = "needs_review"
    inv = make_invoice(db, doc.id, subtotal=D("448"), tax=D("0"), total=D("448"),
                       needs_review=True,
                       review_reason=["LINE_MATH_MISMATCH", "UNGROUNDED:line_items[0].unit_price"])
    db.add(LineItem(invoice_id=inv.id, position=0, description="Toner", quantity=D(7),
                    unit_price=D(6400), line_total=D(448)))
    db.flush()
    base = f"{API}/invoices/{inv.id}"
    line = client.get(base, headers=h).json()["line_items"][0]

    # edit: fixing the unit price clears the math warning and the line's review flag
    r = client.patch(f"{base}/line-items/{line['id']}", headers=h, json={"unit_price": "64"})
    assert r.status_code == 200
    out = r.json()
    assert out["line_items"][0]["unit_price"] == "64.00" and out["is_corrected"] is True
    assert out["issues"] == [] and out["needs_review"] is False and out["review_reason"] is None
    assert out["corrections"][-1]["field"] == "line_items[0].unit_price"

    # add: appended, and the line sum check now sees it (473 vs subtotal 448)
    r = client.post(f"{base}/line-items", headers=h,
                    json={"description": "Delivery", "quantity": "1", "unit_price": "25",
                          "line_total": "25"})
    assert r.status_code == 201
    out = r.json()
    assert [li["description"] for li in out["line_items"]] == ["Toner", "Delivery"]
    assert "LINE_SUM_MISMATCH" in [i["code"] for i in out["issues"]]

    # delete: consistent again, positions stay 0..n-1
    r = client.delete(f"{base}/line-items/{out['line_items'][1]['id']}", headers=h)
    assert r.status_code == 200 and [li["position"] for li in r.json()["line_items"]] == [0]
    assert r.json()["issues"] == []

    # validation and ownership
    assert client.post(f"{base}/line-items", headers=h, json={}).status_code == 422
    assert client.patch(f"{base}/line-items/{line['id']}", headers=h,
                        json={"quantity": "abc"}).status_code == 422
    assert client.patch(f"{base}/line-items/{line['id']}", headers=h, json={}).status_code == 422
    assert client.delete(f"{base}/line-items/{uuid.uuid4()}", headers=h).status_code == 404
    other = signup(client, f"other{uuid.uuid4().hex[:6]}@example.com")
    assert client.patch(f"{base}/line-items/{line['id']}", headers=other,
                        json={"unit_price": "1"}).status_code == 404


def test_line_edit_blocked_while_processing(client, db):
    h = signup(client, f"busy{uuid.uuid4().hex[:6]}@example.com")
    doc = db.get(Document, uuid.UUID(upload(client, h).json()["documents"][0]["id"]))
    inv = make_invoice(db, doc.id)
    doc.status = "queued"
    db.flush()
    r = client.post(f"{API}/invoices/{inv.id}/line-items", headers=h, json={"description": "x"})
    assert r.status_code == 409


def test_resolving_the_last_issue_settles_the_document(client, db):
    """The document must leave needs_review in the same request (sessions do not autoflush)."""
    from app.db.models import ValidationIssue
    h = signup(client, f"resolve{uuid.uuid4().hex[:6]}@example.com")
    doc = db.get(Document, uuid.UUID(upload(client, h).json()["documents"][0]["id"]))
    doc.status = "needs_review"
    inv = make_invoice(db, doc.id, needs_review=True, review_reason=["TAX_ANOMALY"])
    issue = ValidationIssue(document_id=doc.id, invoice_id=inv.id, code="TAX_ANOMALY",
                            severity="warning", field="tax", message="m")
    db.add(issue)
    db.flush()
    r = client.post(f"{API}/invoices/{inv.id}/resolve-issue/{issue.id}", headers=h)
    assert r.status_code == 200 and r.json()["needs_review"] is False
    db.refresh(doc)
    assert doc.status == "completed"


def test_confirm_clears_not_found_flags_and_settles(client, db):
    h = signup(client, f"confirm{uuid.uuid4().hex[:6]}@example.com")
    doc = db.get(Document, uuid.UUID(upload(client, h).json()["documents"][0]["id"]))
    doc.status = "needs_review"
    inv = make_invoice(db, doc.id, needs_review=True,
                       review_reason=["UNGROUNDED:total", "UNGROUNDED:line_items[0].quantity",
                                      "UNGROUNDED:line_items[0].unit_price",
                                      "UNGROUNDED:line_items[1].unit_price"])
    db.flush()
    url = f"{API}/invoices/{inv.id}/confirm"

    out = client.post(url, headers=h, json={"fields": ["line_items[0]"]}).json()     # a whole line
    assert out["review_reason"] == ["UNGROUNDED:total", "UNGROUNDED:line_items[1].unit_price"]
    assert out["needs_review"] is True

    out = client.post(url, headers=h, json={"fields": ["total", "line_items[1]"]}).json()
    assert out["review_reason"] is None and out["needs_review"] is False
    db.refresh(doc)
    assert doc.status == "completed"
    assert out["is_corrected"] is False                      # confirming is not a data change

    assert client.post(url, headers=h, json={"fields": []}).status_code == 422
    assert client.post(url, headers=h, json={}).status_code == 422
    other = signup(client, f"other{uuid.uuid4().hex[:6]}@example.com")
    assert client.post(url, headers=other, json={"fields": ["total"]}).status_code == 404

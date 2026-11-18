"""Search indexing, keyword and semantic search against real Postgres + pgvector."""
import uuid
from datetime import date
from decimal import Decimal as D

import pytest

from app.db.models import Document, DocumentChunk, DocumentPage, LineItem
from app.llm.fake import FakeProvider
from app.search import indexer
from tests.integration.test_api import API, client, make_invoice, signup, upload  # noqa: F401
from tests.integration.test_api import CSV

pytestmark = pytest.mark.integration


@pytest.fixture
def embedder(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(indexer, "resolve_embedder", lambda: fake)
    monkeypatch.setattr("app.api.v1.search.resolve_embedder", lambda: fake)
    return fake


def _doc(db, resp):
    return db.get(Document, uuid.UUID(resp.json()["documents"][0]["id"]))


def seed(client, db, h, name="a.csv", data=CSV, **inv):
    doc = _doc(db, upload(client, h, name=name, data=data))
    doc.status = "completed"
    invoice = make_invoice(db, doc.id, page_start=1, page_end=1, **inv)
    invoice.line_items = [LineItem(position=0, description="Ergonomic keyboard", quantity=D(1),
                                   unit_price=D(80), line_total=D(80))]
    db.add(DocumentPage(document_id=doc.id, page_number=1,
                        text="Delivery note\n\nPlease deliver the pallets to warehouse seven."))
    db.flush()
    indexer.index_document(db, doc.id)
    return doc, invoice


def test_split_text_respects_the_size_limit():
    assert indexer.split_text("") == []
    chunks = indexer.split_text("a" * 50 + "\n\n" + "b" * 50 + "\n\n" + "c" * 130, size=100)
    assert all(len(c) <= 100 for c in chunks) and "".join(chunks).count("c") == 130
    assert indexer.split_text("short") == ["short"]


def test_index_builds_summary_and_page_chunks_and_is_rebuildable(client, db, embedder):
    h = signup(client, "idx@example.com")
    doc, inv = seed(client, db, h)
    chunks = db.query(DocumentChunk).filter_by(document_id=doc.id).order_by(
        DocumentChunk.chunk_index).all()
    assert [c.chunk_index for c in chunks] == [0, 1]
    assert chunks[0].invoice_id == inv.id and "ACME Ltd" in chunks[0].content
    assert "Ergonomic keyboard" in chunks[0].content
    assert chunks[1].invoice_id == inv.id                       # page 1 belongs to the invoice
    assert all(c.tsv is not None and c.embedding is not None for c in chunks)
    assert chunks[0].embedding_provider == "fake" and chunks[0].embedding_model
    assert indexer.index_document(db, doc.id) == 2              # rebuild: no duplicates
    assert db.query(DocumentChunk).filter_by(document_id=doc.id).count() == 2


def test_keyword_search_filters_snippets_and_isolation(client, db, embedder):
    h, other = signup(client, "kw@example.com"), signup(client, "kw2@example.com")
    seed(client, db, h, supplier_name="ACME Ltd", total=D("120"), invoice_date=date(2026, 1, 5))
    seed(client, db, h, name="b.csv", data=CSV + b"B,2\n", supplier_name="Beta GmbH",
         invoice_number="B-2", total=D("900"), invoice_date=date(2026, 6, 5), needs_review=True)
    seed(client, db, other, name="c.csv", data=CSV + b"C,3\n", supplier_name="Gamma")

    def hits(qs):
        r = client.get(f"{API}/search?{qs}", headers=h)
        assert r.status_code == 200, r.text
        return r.json()

    r = hits("q=keyboard")
    assert r["total"] == 2 and {x["kind"] for x in r["items"]} == {"invoice"}
    assert "[[keyboard]]" in r["items"][0]["snippet"] and "<" not in r["items"][0]["snippet"]
    assert {x["supplier_name"] for x in r["items"]} == {"ACME Ltd", "Beta GmbH"}   # not Gamma's
    page = hits("q=warehouse")["items"][0]
    assert page["kind"] == "page" and "[[warehouse]]" in page["snippet"]
    assert hits("q=pallets%20warehouse")["total"] == 2                          # both users' docs excluded
    assert [x["supplier_name"] for x in hits("q=keyboard&supplier=beta")["items"]] == ["Beta GmbH"]
    assert hits("q=keyboard&needs_review=true")["total"] == 1
    assert hits("q=keyboard&date_from=2026-03-01")["total"] == 1
    assert hits("q=keyboard&min_total=500")["items"][0]["invoice_number"] == "B-2"
    assert hits("q=keyboard&max_total=500")["items"][0]["invoice_number"] == "INV-1"
    assert hits("q=nonexistentword")["total"] == 0
    assert hits("q=keyboard&page_size=1")["total"] == 2 and len(hits("q=keyboard&page_size=1")["items"]) == 1
    assert client.get(f"{API}/search", headers=h).status_code == 422
    assert client.get(f"{API}/search?q=x").status_code == 401


def test_semantic_search_ranks_by_meaning_filters_and_isolates(client, db, embedder):
    h, other = signup(client, "sem@example.com"), signup(client, "sem2@example.com")
    seed(client, db, h, supplier_name="ACME Ltd")
    seed(client, db, h, name="b.csv", data=CSV + b"B,2\n", supplier_name="Beta GmbH",
         invoice_number="B-2")
    seed(client, db, other, name="c.csv", data=CSV + b"C,3\n", supplier_name="Gamma")

    r = client.post(f"{API}/search/semantic", headers=h, json={"query": "ergonomic keyboard"})
    assert r.status_code == 200
    items = r.json()
    assert items and all(i["supplier_name"] != "Gamma" for i in items)
    assert items == sorted(items, key=lambda i: -i["score"]) and items[0]["score"] > 0
    only = client.post(f"{API}/search/semantic", headers=h,
                       json={"query": "keyboard", "supplier": "beta", "limit": 5}).json()
    assert {i["supplier_name"] for i in only} == {"Beta GmbH"}
    assert client.post(f"{API}/search/semantic", headers=h, json={"query": ""}).status_code == 422
    assert client.post(f"{API}/search/semantic", json={"query": "x"}).status_code == 401


def test_semantic_search_only_uses_vectors_from_the_active_embedding_model(client, db, embedder):
    h = signup(client, "mdl@example.com")
    doc, _ = seed(client, db, h)
    db.query(DocumentChunk).filter_by(document_id=doc.id).update({"embedding_model": "old-model"})
    db.flush()
    r = client.post(f"{API}/search/semantic", headers=h, json={"query": "keyboard"})
    assert r.status_code == 200 and r.json() == []              # stale vectors never mixed in


def test_semantic_503_without_provider_and_keyword_still_works(client, db, monkeypatch):
    monkeypatch.setattr(indexer, "resolve_embedder", lambda: None)
    monkeypatch.setattr("app.api.v1.search.resolve_embedder", lambda: None)
    h = signup(client, "no503@example.com")
    doc, _ = seed(client, db, h)
    assert all(c.embedding is None for c in db.query(DocumentChunk).filter_by(document_id=doc.id))
    r = client.post(f"{API}/search/semantic", headers=h, json={"query": "keyboard"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    kw = client.get(f"{API}/search?q=keyboard", headers=h)
    assert kw.status_code == 200, kw.text
    assert kw.json()["total"] == 1


def test_embedding_failure_does_not_break_indexing(client, db, monkeypatch):
    class Broken(FakeProvider):
        def embed(self, texts):
            raise RuntimeError("provider down")

    monkeypatch.setattr(indexer, "resolve_embedder", lambda: Broken())
    h = signup(client, "brk@example.com")
    doc, _ = seed(client, db, h)
    assert db.query(DocumentChunk).filter_by(document_id=doc.id).count() == 2
    assert client.get(f"{API}/search?q=keyboard", headers=h).json()["total"] == 1


def test_correcting_an_invoice_reindexes_its_summary(client, db, embedder):
    h = signup(client, "cor@example.com")
    doc, inv = seed(client, db, h)
    doc.status = "needs_review"
    db.flush()
    assert client.get(f"{API}/search?q=Zephyr", headers=h).json()["total"] == 0
    r = client.patch(f"{API}/invoices/{inv.id}", headers=h, json={"supplier_name": "Zephyr Corp"})
    assert r.status_code == 200
    hit = client.get(f"{API}/search?q=Zephyr", headers=h).json()
    assert hit["total"] == 1 and hit["items"][0]["supplier_name"] == "Zephyr Corp"
    assert client.get(f"{API}/search?q=ACME", headers=h).json()["total"] == 0     # old text gone
    assert db.query(DocumentChunk).filter_by(document_id=doc.id).count() == 2     # page chunk kept
    chunk = db.query(DocumentChunk).filter_by(document_id=doc.id, chunk_index=0).one()
    assert chunk.embedding is not None and chunk.tsv is not None

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tenacity import wait_none

from app.db.models import Document, DocumentPage, Invoice
from app.extraction import grounding, normalize, prompts, segmentation, stage
from app.extraction.extractor import extract_invoice, llm_column_mapper
from app.llm.base import LLMProvider
from app.llm.factory import Resolution
from app.llm.fake import FakeProvider
from app.processing.errors import DocumentError, ErrorCode
from app.schemas.extraction import InvoiceExtraction, LLMInvoice

INVOICE_TEXT = """ACME Corp Ltd
Invoice Number: INV-2024-001
Invoice Date: 2024-03-05
Supplier: ACME Corp Ltd
Customer: Zed Industries
Subtotal: 1,000.00
Tax: 200.00
Total: 1,200.00
Currency: EUR
"""


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(LLMProvider, "retry_wait", wait_none())


# ---------------------------------------------------------------- normalize
@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("2024-03-05", date(2024, 3, 5)), ("25/03/2024", date(2024, 3, 25)),
    ("03/25/2024", date(2024, 3, 25)), ("03/04/2024", None),      # ambiguous -> null
    ("31/02/2024", None), ("March 5, 2024", date(2024, 3, 5)), (None, None), ("", None),
])
def test_parse_date_strict(raw, expected):
    assert normalize.parse_date_strict(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("EUR", "EUR"), ("usd", "USD"), ("€", "EUR"), ("£", "GBP"), ("$", None), ("euros", None),
    (None, None),
])
def test_currency(raw, expected):
    assert normalize.normalize_currency(raw) == expected


@pytest.mark.unit
def test_normalisation_keys():
    assert normalize.norm_supplier("ACME Corp., Ltd.") == "acme"
    assert normalize.norm_supplier("Müller & Söhne GmbH") == "muller and sohne"
    assert normalize.norm_supplier("Acme") == normalize.norm_supplier("ACME Inc")
    assert normalize.norm_supplier(None) is None and normalize.norm_supplier("Ltd") is None
    assert normalize.norm_invoice_number("INV-00123") == normalize.norm_invoice_number("inv 00123")
    assert normalize.norm_invoice_number("00123") == "123"
    assert normalize.norm_invoice_number("000") == "0"
    assert normalize.norm_invoice_number(None) is None


@pytest.mark.unit
def test_normalize_invoice_locale_numbers_and_empty_lines():
    raw = LLMInvoice.model_validate({
        "invoice_number": " 77 ", "invoice_date": "25.03.2024", "total": "1.234,50",
        "tax": 12.5, "currency": "eur",
        "line_items": [{"description": "Widget", "quantity": "2", "unit_price": "10,00"},
                       {"description": None}]})
    inv = normalize.normalize_invoice(raw)
    assert inv.invoice_number == "77" and inv.invoice_date == date(2024, 3, 25)
    assert inv.total == Decimal("1234.50") and inv.tax == Decimal("12.5")
    assert inv.currency == "EUR" and len(inv.line_items) == 1
    assert inv.line_items[0].unit_price == Decimal("10.00")


# ---------------------------------------------------------------- grounding
def _inv(**kw):
    return InvoiceExtraction(**kw)


@pytest.mark.unit
def test_grounded_values_pass():
    inv = _inv(invoice_number="INV-2024-001", invoice_date=date(2024, 3, 5),
               supplier_name="acme corp ltd", total=Decimal("1200"), tax=Decimal("200.00"),
               currency="EUR")
    assert grounding.ungrounded_fields(inv, INVOICE_TEXT) == []


@pytest.mark.unit
def test_invented_values_are_flagged():
    inv = _inv(invoice_number="INV-9999", invoice_date=date(2023, 1, 1),
               supplier_name="Globex", total=Decimal("999.99"), currency="GBP")
    assert grounding.ungrounded_fields(inv, INVOICE_TEXT) == [
        "invoice_number", "invoice_date", "supplier_name", "total", "currency"]


@pytest.mark.unit
def test_grounding_accepts_other_date_and_number_formats():
    text = "Invoice 5 dated 5th March 2024. Amount due: 1.200,00 EUR"
    inv = _inv(invoice_date=date(2024, 3, 5), total=Decimal("1200"))
    assert grounding.ungrounded_fields(inv, text) == []


@pytest.mark.unit
def test_grounding_line_items_and_currency_symbols():
    text = "Widget x 2 @ 10.00 = 20.00 €"
    inv = _inv(currency="EUR", line_items=[
        {"description": "Widget", "quantity": 2, "unit_price": 10, "line_total": 20},
        {"description": "Phantom", "line_total": 55}])
    assert grounding.ungrounded_fields(inv, text) == [
        "line_items[1].description", "line_items[1].line_total"]


# ---------------------------------------------------------------- segmentation
@pytest.mark.unit
def test_segmentation_single_multi_page_invoice():
    pages = [(1, "Invoice No: A-1\nitems"), (2, "continued items, total 5"), (3, "")]
    (seg,) = segmentation.segment_pages(pages)
    assert (seg.page_start, seg.page_end) == (1, 2) and "[Page 2]" in seg.text


@pytest.mark.unit
def test_segmentation_splits_on_new_invoice_number():
    pages = [(1, "Invoice No: A-1"), (2, "more of A-1"), (3, "Invoice Number: B-2"),
             (4, "Invoice #: B-2 page two"), (5, "INV: C-3")]
    segs = segmentation.segment_pages(pages)
    assert [(s.page_start, s.page_end) for s in segs] == [(1, 2), (3, 4), (5, 5)]


@pytest.mark.unit
def test_segmentation_same_number_with_different_formatting_is_one_invoice():
    segs = segmentation.segment_pages([(1, "Invoice No: INV-001"), (2, "Invoice No: inv001")])
    assert len(segs) == 1


@pytest.mark.unit
def test_segmentation_without_numbers_and_empty_documents():
    assert len(segmentation.segment_pages([(1, "hello"), (2, "world")])) == 1
    assert segmentation.segment_pages([(1, "  "), (2, "")]) == []


# ---------------------------------------------------------------- extractor
@pytest.mark.unit
def test_extractor_end_to_end_with_fake_provider():
    out = extract_invoice(FakeProvider(), INVOICE_TEXT, 1, 2)
    inv = out.extraction
    assert inv.invoice_number == "INV-2024-001" and inv.invoice_date == date(2024, 3, 5)
    assert inv.total == Decimal("1200.00") and inv.currency == "EUR"
    assert out.ungrounded == [] and out.field_confidence["total"] == 0.9
    assert out.provider == "fake" and out.model == "fake-1"
    assert out.prompt_version == prompts.EXTRACTION_PROMPT_VERSION
    assert out.tokens_in > 0 and out.tokens_out > 0 and (out.page_start, out.page_end) == (1, 2)


@pytest.mark.unit
def test_extractor_absent_fields_stay_none():
    out = extract_invoice(FakeProvider(), "Just a delivery note.\nNothing financial here.")
    assert all(v is None for k, v in out.extraction.model_dump().items() if k != "line_items")
    assert out.ungrounded == []


@pytest.mark.unit
def test_extractor_flags_hallucinated_values_and_caps_confidence():
    canned = {"invoice_number": "INV-2024-001", "total": "9999.00",
              "field_confidence": {"invoice_number": 0.95, "total": 0.99},
              "field_evidence": {"total": "Total: 9999.00"}}
    out = extract_invoice(FakeProvider(canned), INVOICE_TEXT)
    assert out.ungrounded == ["total"]
    assert out.field_confidence["total"] == 0.2 and out.field_confidence["invoice_number"] == 0.95


@pytest.mark.unit
def test_extractor_uses_json_repair_for_invalid_schema_reply():
    p = FakeProvider([{"line_items": "not a list"}, {"invoice_number": "INV-2024-001"}])
    out = extract_invoice(p, INVOICE_TEXT)
    assert p.calls == 2 and out.extraction.invoice_number == "INV-2024-001"


@pytest.mark.unit
def test_prompt_marks_document_text_as_data():
    assert "<document>" in prompts.extraction_user("x") and "prompt injection" in \
        prompts.EXTRACTION_SYSTEM


@pytest.mark.unit
def test_llm_column_mapper_inverts_prompt_output():
    p = FakeProvider({"invoice_number": "Rechnungsnr", "total": "Gesamt", "bogus": "Missing"})
    mapped = llm_column_mapper(p)(["Rechnungsnr", "Gesamt"], [{"Rechnungsnr": "1"}])
    assert mapped == {"Rechnungsnr": "invoice_number", "Gesamt": "total"}


# ---------------------------------------------------------------- stage (mock db)
def _doc(ext="pdf", provider=None, path="/x"):
    return MagicMock(id=uuid.uuid4(), file_ext=ext, stored_path=path, llm_provider_requested=provider,
                     original_filename="d." + ext)


def _db(doc, pages):
    db = MagicMock()
    db.get.return_value = doc
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = pages
    return db


def _page(n, text):
    return MagicMock(page_number=n, text=text)


def _use(monkeypatch, primary, fallback=None):
    monkeypatch.setattr(stage, "resolve_llm", lambda requested=None: Resolution(primary, fallback))


def _saved(db):
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], Invoice)]


@pytest.mark.unit
def test_stage_persists_invoices_with_provider_metadata(monkeypatch):
    doc = _doc()
    pages = [_page(1, INVOICE_TEXT), _page(2, INVOICE_TEXT.replace("INV-2024-001", "INV-2024-002"))]
    db = _db(doc, pages)
    _use(monkeypatch, FakeProvider())
    out = stage.extract_document(db, doc.id)
    inv1, inv2 = _saved(db)
    assert out["invoices"] == 2 and doc.status == "validating"
    assert (inv1.invoice_number, inv1.invoice_number_norm) == ("INV-2024-001", "INV2024001")
    assert inv1.supplier_norm == "acme" and inv1.total == Decimal("1200.00")
    assert (inv1.provider, inv1.model) == ("fake", "fake-1")
    assert inv1.prompt_version == prompts.EXTRACTION_PROMPT_VERSION and inv1.tokens_in > 0
    assert (inv1.page_start, inv1.page_end, inv2.page_start) == (1, 1, 2)
    assert inv1.needs_review is False and inv1.field_evidence["total"].startswith("Total")
    event = db.add.call_args_list[-1].args[0]
    assert event.stage == "extraction" and event.status == "succeeded"
    assert event.details["invoices"] == 2 and event.details["provider"] == "fake"


@pytest.mark.unit
def test_stage_flags_ungrounded_invoice_for_review(monkeypatch):
    doc = _doc()
    db = _db(doc, [_page(1, INVOICE_TEXT)])
    _use(monkeypatch, FakeProvider({"invoice_number": "INV-2024-001", "total": "5.55"}))
    stage.extract_document(db, doc.id)
    (inv,) = _saved(db)
    assert inv.needs_review and inv.review_reason == ["UNGROUNDED:total"]
    assert inv.raw_extraction["ungrounded"] == ["total"]


@pytest.mark.unit
def test_stage_no_provider_fails_document_with_not_configured(monkeypatch):
    def none(requested=None):
        raise DocumentError(ErrorCode.LLM_NOT_CONFIGURED, "no key")

    monkeypatch.setattr(stage, "resolve_llm", none)
    doc = _doc()
    db = _db(doc, [_page(1, INVOICE_TEXT)])
    with pytest.raises(DocumentError) as ei:
        stage.extract_document(db, doc.id)
    assert ei.value.code is ErrorCode.LLM_NOT_CONFIGURED
    assert doc.status == "failed" and doc.error_code == "LLM_NOT_CONFIGURED"
    assert db.add.call_args_list[-1].args[0].stage == "extraction"


class _Down(FakeProvider):
    name = "down"

    def __init__(self, code=ErrorCode.LLM_PROVIDER_UNAVAILABLE):
        super().__init__()
        self.code = code

    def _complete(self, system, user):
        raise DocumentError(self.code)


@pytest.mark.unit
def test_stage_falls_back_to_second_provider(monkeypatch):
    doc = _doc()
    db = _db(doc, [_page(1, INVOICE_TEXT)])
    _use(monkeypatch, _Down(), FakeProvider())
    stage.extract_document(db, doc.id)
    (inv,) = _saved(db)
    assert inv.provider == "fake"


@pytest.mark.unit
def test_stage_without_fallback_fails_friendly(monkeypatch):
    doc = _doc()
    db = _db(doc, [_page(1, INVOICE_TEXT)])
    _use(monkeypatch, _Down(ErrorCode.LLM_RATE_LIMITED))
    with pytest.raises(DocumentError) as ei:
        stage.extract_document(db, doc.id)
    assert ei.value.code is ErrorCode.LLM_RATE_LIMITED and doc.status == "failed"


@pytest.mark.unit
def test_stage_retryable_failure_keeps_document_extracting_when_retry_pending(monkeypatch):
    doc = _doc()
    db = _db(doc, [_page(1, INVOICE_TEXT)])
    _use(monkeypatch, _Down(ErrorCode.LLM_RATE_LIMITED))
    with pytest.raises(DocumentError):
        stage.extract_document(db, doc.id, will_retry=True)
    assert doc.status == "extracting" and db.add.call_args_list[-1].args[0].status == "retried"


@pytest.mark.unit
def test_stage_partial_segment_failure_does_not_fail_document(monkeypatch):
    class Flaky(FakeProvider):
        def _complete(self, system, user):
            if "B-2" in user:
                raise DocumentError(ErrorCode.LLM_PROVIDER_UNAVAILABLE)
            return super()._complete(system, user)

    doc = _doc()
    pages = [_page(1, "Invoice Number: A-1\nTotal: 5"), _page(2, "Invoice Number: B-2\nTotal: 6")]
    db = _db(doc, pages)
    _use(monkeypatch, Flaky())
    out = stage.extract_document(db, doc.id)
    assert out["invoices"] == 1 and doc.status == "validating"
    assert db.add.call_args_list[-1].args[0].details["failed_segments"] == 1


@pytest.mark.unit
def test_stage_no_text_fails_with_no_text_found(monkeypatch):
    doc = _doc()
    db = _db(doc, [])
    _use(monkeypatch, FakeProvider())
    with pytest.raises(DocumentError) as ei:
        stage.extract_document(db, doc.id)
    assert ei.value.code is ErrorCode.NO_TEXT_FOUND


@pytest.mark.unit
def test_stage_long_segment_is_truncated(monkeypatch):
    doc = _doc()
    db = _db(doc, [_page(1, "Invoice Number: A-1\n" + "x" * (stage.MAX_SEGMENT_CHARS + 10))])
    p = FakeProvider()
    _use(monkeypatch, p)
    stage.extract_document(db, doc.id)
    assert len(p.prompts[0]) < stage.MAX_SEGMENT_CHARS + 2000
    assert db.add.call_args_list[-1].args[0].details["truncated"] == 1


@pytest.mark.unit
def test_stage_spreadsheet_maps_directly_without_llm(tmp_path, monkeypatch):
    f = tmp_path / "s.csv"
    f.write_text("Invoice No,Date,Vendor,Total\nI-1,2024-01-05,ACME,10.50\nI-2,2024-01-06,Beta,20\n")
    doc = _doc("csv", path=str(f))
    db = _db(doc, [])
    _use(monkeypatch, FakeProvider())
    p = FakeProvider()
    p.calls = 0
    out = stage.extract_document(db, doc.id)
    first, second = _saved(db)
    assert out["invoices"] == 2 and first.total == Decimal("10.50")
    assert (first.provider, first.model, first.tokens_in) == ("spreadsheet", "heuristic", 0)
    assert first.supplier_norm == "acme" and second.invoice_number_norm == "I2"
    assert db.add.call_args_list[-1].args[0].details["sheet_mappings"][0]["layout"] == \
        "row_per_invoice"


@pytest.mark.unit
def test_stage_spreadsheet_uses_llm_mapping_fallback(tmp_path, monkeypatch):
    f = tmp_path / "s.csv"
    f.write_text("Rechnungsnr,Gesamt\nR-1,10\n")
    doc = _doc("csv", path=str(f))
    db = _db(doc, [])
    _use(monkeypatch, FakeProvider({"invoice_number": "Rechnungsnr", "total": "Gesamt"}))
    stage.extract_document(db, doc.id)
    (inv,) = _saved(db)
    assert inv.invoice_number == "R-1" and inv.total == Decimal("10")
    assert inv.model == "llm" and inv.prompt_version == prompts.COLUMN_MAPPING_PROMPT_VERSION


@pytest.mark.unit
def test_stage_spreadsheet_without_provider_and_unmappable_columns_fails(tmp_path, monkeypatch):
    def none(requested=None):
        raise DocumentError(ErrorCode.LLM_NOT_CONFIGURED)

    monkeypatch.setattr(stage, "resolve_llm", none)
    f = tmp_path / "s.csv"
    f.write_text("foo,bar\n1,2\n")
    doc = _doc("csv", path=str(f))
    with pytest.raises(DocumentError) as ei:
        stage.extract_document(_db(doc, []), doc.id)
    assert ei.value.code is ErrorCode.EXTRACTION_PARSE_ERROR

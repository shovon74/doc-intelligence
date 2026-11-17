import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.processing import sheet_mapping as sm
from app.processing import spreadsheet
from app.processing.pipeline import process_document


def _only(path, ext, **kw):
    (sheet,) = spreadsheet.stream_sheets(path, ext, **kw)
    return sheet


@pytest.mark.unit
def test_parse_decimal():
    cases = [
        ("1,234.50", "1234.50"), ("1.234,50", "1234.50"), ("12,50", "12.50"), ("1,234", "1234"),
        ("1,234,567", "1234567"), ("(45.00)", "-45.00"), ("$ 1,000", "1000"),
        ("EUR 99.9", "99.9"), (12, "12"), (12.5, "12.5"),
    ]
    for raw, expected in cases:
        assert sm.parse_decimal(raw) == Decimal(expected), raw
    for raw in (None, "", "n/a", "abc", True):
        assert sm.parse_decimal(raw) is None, raw


@pytest.mark.unit
def test_parse_date():
    cases = [("2024-03-05", date(2024, 3, 5)), ("05.03.2024", date(2024, 3, 5)),
             ("March 5, 2024", date(2024, 3, 5)), ("garbage", None), (None, None)]
    for raw, expected in cases:
        assert sm.parse_date(raw) == expected, raw


@pytest.mark.unit
def test_synonym_mapping_first_column_wins_and_reports_unmapped():
    mapping, unmapped = sm.map_columns(
        ["Inv No.", "Vendor Name", "Grand Total", "VAT", "Notes", "Total"])
    assert mapping == {"invoice_number": "Inv No.", "supplier_name": "Vendor Name",
                       "total": "Grand Total", "tax": "VAT"}
    assert unmapped == ["Notes", "Total"]


@pytest.mark.unit
def test_header_row_detected_below_title_rows(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("ACME Monthly Report\nGenerated 2024-02-01\n"
                 "Invoice No,Date,Supplier,Total\nINV-1,2024-01-05,ACME,100.00\n"
                 "INV-2,2024-01-09,Beta,50.50\n")
    s = _only(f, "csv")
    assert s.header_row == 2 and s.columns == ["Invoice No", "Date", "Supplier", "Total"]
    assert s.row_count == 2
    assert [i.invoice_number for i in s.extraction.invoices] == ["INV-1", "INV-2"]


@pytest.mark.unit
def test_header_in_first_row_when_nothing_recognisable(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("a,b\n1,2\n3,4\n")
    s = _only(f, "csv")
    assert s.header_row == 0 and s.columns == ["a", "b"] and s.row_count == 2
    assert s.extraction.invoices == []          # nothing recognisable -> no invoices


@pytest.mark.unit
def test_row_per_invoice_drops_trailing_totals(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("Invoice No,Date,Supplier,Customer,Subtotal,Tax,Total,Currency\n"
                 "00123,2024-01-05,ACME,Zed,\"1,000.00\",200.00,\"1,200.00\",eur\n"
                 "00124,2024-01-06,Beta,Zed,50,5,55,USD\n"
                 "Total,,,,1050,205,1255,\n")
    s = _only(f, "csv")
    ex = s.extraction
    assert ex.layout == "row_per_invoice" and ex.dropped_totals_rows == 1
    assert len(ex.invoices) == 2
    first = ex.invoices[0]
    assert first.invoice_number == "00123"                  # leading zeros kept
    assert first.invoice_date == date(2024, 1, 5)
    assert (first.subtotal, first.tax, first.total) == (Decimal("1000.00"), Decimal("200.00"),
                                                        Decimal("1200.00"))
    assert first.currency == "EUR" and first.customer_name == "Zed"


@pytest.mark.unit
def test_interior_total_label_is_not_dropped(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("Invoice No,Total\nTotal Solutions 1,10\nINV-2,20\n")
    ex = _only(f, "csv").extraction
    assert ex.dropped_totals_rows == 0 and len(ex.invoices) == 2


@pytest.mark.unit
def test_grouped_by_invoice_number_collects_line_items(tmp_path):
    f = tmp_path / "g.csv"
    f.write_text("Invoice No,Date,Supplier,Description,Qty,Unit Price,Line Total,Total\n"
                 "A-1,2024-01-05,ACME,Widget,2,10,20,30\n"
                 "A-1,,,Gadget,1,10,10,\n"
                 "A-2,2024-01-07,Beta,Service,1,99,99,99\n"
                 "A-3,2024-01-08,Beta,Fee,1,5,5,5\n"
                 "A-3,,,Fee 2,1,5,5,\n")
    ex = _only(f, "csv").extraction
    assert ex.layout == "grouped_by_invoice"
    assert [i.invoice_number for i in ex.invoices] == ["A-1", "A-2", "A-3"]
    a1 = ex.invoices[0]
    assert a1.total == Decimal("30") and a1.supplier_name == "ACME"
    assert [(i.description, i.quantity, i.line_total) for i in a1.line_items] == [
        ("Widget", Decimal("2"), Decimal("20")), ("Gadget", Decimal("1"), Decimal("10"))]
    assert len(ex.invoices[1].line_items) == 1


@pytest.mark.unit
def test_duplicate_invoice_numbers_without_line_columns_stay_separate_invoices(tmp_path):
    f = tmp_path / "d.csv"
    f.write_text("Invoice No,Supplier,Total\nX-1,ACME,10\nX-1,ACME,10\n")
    ex = _only(f, "csv").extraction
    assert ex.layout == "row_per_invoice" and len(ex.invoices) == 2   # left for the duplicate rule


@pytest.mark.unit
def test_xlsx_dates_and_numbers(tmp_path):
    f = tmp_path / "x.xlsx"
    pd.DataFrame({"Invoice Number": ["I-1"], "Invoice Date": [pd.Timestamp("2024-02-03")],
                  "Vendor": ["ACME"], "Amount": [12.5]}).to_excel(f, index=False)
    s = _only(f, "xlsx")
    inv = s.extraction.invoices[0]
    assert inv.invoice_date == date(2024, 2, 3) and inv.total == Decimal("12.5")
    assert s.rows[0]["Invoice Date"] == "2024-02-03"           # JSON-safe


@pytest.mark.unit
def test_mapping_is_saved_in_event_details(tmp_path):
    f = tmp_path / "m.csv"
    f.write_text("Inv No,Vendor,Grand Total,Notes\nI-1,ACME,10,hi\n")
    doc = MagicMock(id=uuid.uuid4(), stored_path=str(f), file_ext="csv", original_filename="m.csv")
    db = MagicMock()
    db.get.return_value = doc
    process_document(db, doc.id)
    event = db.add.call_args_list[-1].args[0]
    (m,) = event.details["sheet_mappings"]
    assert m["mapping"] == {"invoice_number": "Inv No", "supplier_name": "Vendor",
                            "total": "Grand Total"}
    assert m["unmapped_columns"] == ["Notes"] and m["mapping_source"] == "heuristic"


@pytest.mark.unit
def test_llm_mapper_hook_fills_missing_required_columns():
    calls = []

    def mapper(unmapped, sample):
        calls.append(unmapped)
        return {"Rechnungsnr": "invoice_number", "Gesamt": "total", "Bogus": "not_a_field"}

    ex = sm.extract_from_rows(["Rechnungsnr", "Gesamt", "Bogus"],
                              [{"Rechnungsnr": "R1", "Gesamt": "10", "Bogus": "x"}], mapper)
    assert calls == [["Rechnungsnr", "Gesamt", "Bogus"]]
    assert ex.mapping == {"invoice_number": "Rechnungsnr", "total": "Gesamt"}
    assert ex.mapping_source == "llm" and ex.unmapped_columns == ["Bogus"]
    assert ex.invoices[0].total == Decimal("10")


@pytest.mark.unit
def test_llm_mapper_not_called_when_heuristics_suffice_and_errors_are_ignored():
    mapper = MagicMock(return_value={})
    sm.extract_from_rows(["Invoice No", "Total", "Extra"], [{"Invoice No": "1", "Total": "2"}], mapper)
    mapper.assert_not_called()
    boom = MagicMock(side_effect=RuntimeError("llm down"))
    ex = sm.extract_from_rows(["Foo", "Date"], [{"Foo": "1", "Date": "2024-01-01"}], boom)
    assert boom.called and ex.mapping_source == "heuristic"
    assert ex.invoices[0].invoice_date == date(2024, 1, 1)

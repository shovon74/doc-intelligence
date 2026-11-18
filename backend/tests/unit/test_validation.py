from datetime import date
from decimal import Decimal as D

import pytest

from app.db.models import Invoice, LineItem
from app.validation import engine
from app.validation.crosscheck import compare_invoices

TODAY = date(2026, 6, 1)


def make(lines=(), **kw) -> Invoice:
    base = dict(invoice_number="INV-1", invoice_date=date(2026, 5, 1), supplier_name="ACME",
                subtotal=D("100"), tax=D("20"), total=D("120"), currency="EUR",
                supplier_norm="acme", invoice_number_norm="INV1")
    inv = Invoice(**{**base, **kw})
    inv.line_items = [LineItem(position=i, **l) for i, l in enumerate(lines)]
    return inv


def codes(inv):
    return [i.code for i in engine.check_invoice(inv, TODAY)]


@pytest.mark.unit
def test_clean_invoice_has_no_issues():
    good = make(lines=[dict(quantity=D(2), unit_price=D(30), line_total=D(60)),
                       dict(quantity=D(1), unit_price=D(40), line_total=D(40))])
    assert engine.check_invoice(good, TODAY) == []


@pytest.mark.unit
def test_each_rule_fires_on_its_failure_and_stays_quiet_otherwise():
    def line(q, p, t):
        return [dict(quantity=D(q), unit_price=D(p), line_total=D(t))]

    cases = [
        ("MISSING_FIELD", dict(invoice_number=None), dict()),
        ("MISSING_FIELD", dict(total=None), dict()),
        ("MISSING_FIELD", dict(supplier_name=""), dict()),
        ("MISSING_FIELD", dict(invoice_date=None), dict()),
        ("TOTAL_MISMATCH", dict(total=D("130")), dict(total=D("120.01"))),     # within tolerance
        ("LINE_SUM_MISMATCH", dict(lines=line(1, 50, 50)), dict(lines=line(1, 100, 100))),
        ("LINE_MATH_MISMATCH", dict(lines=line(2, 10, 25), subtotal=None, tax=None, total=None),
         dict(lines=line(2, 10, 20), subtotal=None, tax=None, total=None)),
        ("TAX_ANOMALY", dict(tax=D("50"), total=D("150")), dict(tax=D("30"), total=D("130"))),
        ("TAX_ANOMALY", dict(tax=D("-5"), total=D("95")), dict()),
        ("NEGATIVE_AMOUNT", dict(subtotal=D("-100"), tax=D(0), total=D("-100")), dict()),
        ("FUTURE_DATE", dict(invoice_date=date(2026, 7, 1)), dict(invoice_date=TODAY)),
        ("OLD_DATE", dict(invoice_date=date(2010, 1, 1)), dict(invoice_date=date(2018, 1, 1))),
        ("LOW_CONFIDENCE", dict(field_confidence={"total": 0.3}),
         dict(field_confidence={"total": 0.5})),
    ]
    for code, bad, ok in cases:
        lines = bad.pop("lines", ())
        assert code in codes(make(lines=lines, **bad)), (code, bad)
        lines = ok.pop("lines", ())
        assert code not in codes(make(lines=lines, **ok)), (code, ok)


@pytest.mark.unit
def test_rule_edge_cases():
    # partial data: rules skip rather than guess
    assert codes(make(tax=None)) == []
    assert codes(make(lines=[dict(quantity=None, unit_price=D(5), line_total=D(100))])) == []
    # missing currency is info only
    (issue,) = engine.check_invoice(make(currency=None), TODAY)
    assert (issue.code, issue.severity, issue.field) == ("MISSING_FIELD", "info", "currency")
    # severity of a missing required field
    (issue,) = engine.check_invoice(make(total=None, subtotal=None), TODAY)
    assert issue.severity == "error" and issue.field == "total"
    # reasons: ungrounded kept, stale codes dropped, info ignored, codes de-duplicated
    inv = make(review_reason=["UNGROUNDED:total", "TOTAL_MISMATCH"])
    reasons = engine._review_reasons(inv, [engine.Issue("MISSING_FIELD", "info", "m"),
                                           engine.Issue("TAX_ANOMALY", "warning", "m"),
                                           engine.Issue("TAX_ANOMALY", "warning", "m")])
    assert reasons == ["UNGROUNDED:total", "TAX_ANOMALY"]


# ------------------------------------------------------------------ cross-check
def _line(total, qty=1, price=None):
    return dict(quantity=D(qty), unit_price=D(price if price is not None else total),
                line_total=D(total))


@pytest.mark.unit
def test_crosscheck_identical_documents_match_despite_formatting():
    a = [make(lines=[_line(100)], supplier_name="ACME Ltd.", invoice_number="INV-1")]
    b = [make(lines=[_line(100)], supplier_name="acme limited", invoice_number="inv1",
              total=D("120.01"))]                                      # inside tolerance
    r = compare_invoices(a, b)
    assert r["status"] == "match" and r["summary"]["paired"] == 1
    assert r["summary"]["differences"] == 0


@pytest.mark.unit
def test_crosscheck_reports_exactly_the_planted_differences():
    a = [make(lines=[_line(60), _line(40)]),
         make(invoice_number="INV-2", invoice_number_norm="INV2", total=D("50"),
              subtotal=D("50"), tax=D(0))]
    b = [make(lines=[_line(60), _line(45)], total=D("125"), tax=D("25"),
              invoice_date=date(2026, 5, 2), currency="USD", subtotal=D("100")),
         make(invoice_number="INV-3", invoice_number_norm="INV3"),
         make(invoice_number="INV-5", invoice_number_norm="INV5")]
    r = compare_invoices(a, b)
    assert r["status"] == "mismatch"
    (pair,) = r["pairs"]
    assert {d["field"] for d in pair["differences"]} == {
        "invoice_date", "tax", "total", "currency", "line_items[1].line_total",
        "line_items[1].unit_price"}
    got = {d["field"]: (d["a"], d["b"]) for d in pair["differences"]}
    assert got["total"] == ("120", "125") and got["invoice_date"] == ("2026-05-01", "2026-05-02")
    assert [i["invoice_number"] for i in r["only_in_a"]] == ["INV-2"]
    assert [i["invoice_number"] for i in r["only_in_b"]] == ["INV-3", "INV-5"]


@pytest.mark.unit
def test_crosscheck_pairs_lone_invoices_and_flags_number_and_line_count():
    a = [make(invoice_number="INV-1", invoice_number_norm="INV1", lines=[_line(100)])]
    b = [make(invoice_number="INV-9", invoice_number_norm="INV9",
              lines=[_line(50), _line(50)])]
    r = compare_invoices(a, b)
    assert {d["field"] for d in r["pairs"][0]["differences"]} == {"invoice_number",
                                                                  "line_item_count"}
    assert r["only_in_a"] == [] and r["only_in_b"] == []


@pytest.mark.unit
def test_crosscheck_missing_value_on_one_side_is_a_difference():
    r = compare_invoices([make()], [make(customer_name="Zed", tax=None)])
    assert {d["field"] for d in r["pairs"][0]["differences"]} == {"customer_name", "tax"}

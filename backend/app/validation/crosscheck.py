"""
Cross-check: compare the invoices of two documents (e.g. a PDF and a spreadsheet of the
same invoices) and report exactly which fields differ.

Invoices are paired by (invoice number, supplier), then by invoice number alone. When one
invoice is left on each side they are paired anyway, so a wrong invoice number shows up as a
difference. Everything else is reported as only in A / only in B.
"""
import uuid
from decimal import Decimal

from sqlalchemy.orm import Session, selectinload

from app.db.models import Crosscheck, Document, Invoice
from app.extraction.normalize import norm_supplier
from app.validation.engine import _close

MONEY = ("subtotal", "tax", "total")
TEXT = ("invoice_number", "supplier_name", "customer_name")
LINE_MONEY = ("quantity", "unit_price", "line_total")


def _norm(field: str, value) -> str | None:
    if value in (None, ""):
        return None
    if field in ("supplier_name", "customer_name"):
        return norm_supplier(str(value))
    if field == "invoice_number":
        return "".join(ch for ch in str(value).upper() if ch.isalnum())
    return str(value).strip().upper()


def _money_diff(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None and b is None:
        return False
    return a is None or b is None or not _close(a, b)


def _show(v) -> str | None:
    return None if v is None else str(v)


def _show_money(v) -> str | None:
    return None if v is None else f"{v:.2f}"


def _pair_up(a: list[Invoice], b: list[Invoice]) -> tuple[list[tuple], list[Invoice], list[Invoice]]:
    pairs: list[tuple[Invoice, Invoice]] = []
    left_a, left_b = list(a), list(b)
    for strict in (True, False):
        for x in list(left_a):
            if not x.invoice_number_norm:
                continue
            for y in left_b:
                if (x.invoice_number_norm == y.invoice_number_norm
                        and (not strict or x.supplier_norm == y.supplier_norm)):
                    pairs.append((x, y))
                    left_a.remove(x)
                    left_b.remove(y)
                    break
    if len(left_a) == 1 and len(left_b) == 1:
        pairs.append((left_a.pop(), left_b.pop()))
    return pairs, left_a, left_b


def _differences(x: Invoice, y: Invoice) -> list[dict]:
    diffs = []

    def add(name, a, b):
        diffs.append({"field": name, "a": _show(a), "b": _show(b)})

    for f in TEXT:
        if _norm(f, getattr(x, f)) != _norm(f, getattr(y, f)):
            add(f, getattr(x, f), getattr(y, f))
    if x.invoice_date != y.invoice_date:
        add("invoice_date", x.invoice_date, y.invoice_date)
    for f in MONEY:
        if _money_diff(getattr(x, f), getattr(y, f)):
            add(f, getattr(x, f), getattr(y, f))
    if _norm("currency", x.currency) != _norm("currency", y.currency):
        add("currency", x.currency, y.currency)

    la, lb = x.line_items, y.line_items
    if la and lb and len(la) != len(lb):
        add("line_item_count", len(la), len(lb))
    elif la and lb:
        for i, (p, q) in enumerate(zip(la, lb)):
            for f in LINE_MONEY:
                if _money_diff(getattr(p, f), getattr(q, f)):
                    add(f"line_items[{i}].{f}", getattr(p, f), getattr(q, f))
    return diffs


def _ref(inv: Invoice) -> dict:
    return {"invoice_id": str(inv.id), "invoice_number": inv.invoice_number,
            "supplier_name": inv.supplier_name, "total": _show_money(inv.total)}


def compare_invoices(a: list[Invoice], b: list[Invoice]) -> dict:
    pairs, only_a, only_b = _pair_up(a, b)
    out_pairs = [{"a": _ref(x), "b": _ref(y), "differences": _differences(x, y)}
                 for x, y in pairs]
    n_diff = sum(len(p["differences"]) for p in out_pairs)
    # Invoices only in B (the other rows of a ledger) do not count against the check: the question is
    # whether everything in A is in B and agrees.
    matched = all(not p["differences"] for p in out_pairs) and not only_a
    return {
        "status": "match" if matched else "mismatch",
        "summary": {"invoices_a": len(a), "invoices_b": len(b), "paired": len(pairs),
                    "differences": n_diff, "only_in_a": len(only_a), "only_in_b": len(only_b)},
        "pairs": out_pairs,
        "only_in_a": [_ref(i) for i in only_a],
        "only_in_b": [_ref(i) for i in only_b],
    }


class DocumentNotFound(Exception):
    pass


class DocumentNotReady(Exception):
    pass


def run_crosscheck(db: Session, user_id: uuid.UUID, doc_a: uuid.UUID, doc_b: uuid.UUID) -> Crosscheck:
    sides = []
    for doc_id in (doc_a, doc_b):
        doc = db.get(Document, doc_id)
        if doc is None or doc.user_id != user_id:
            raise DocumentNotFound(str(doc_id))
        if doc.status not in ("completed", "needs_review"):
            raise DocumentNotReady(f"{doc.original_filename} is {doc.status}")
        sides.append(db.query(Invoice).options(selectinload(Invoice.line_items))
                     .filter(Invoice.document_id == doc_id).order_by(Invoice.segment_index).all())
    result = compare_invoices(*sides)
    row = Crosscheck(user_id=user_id, document_a_id=doc_a, document_b_id=doc_b,
                     status=result["status"], result=result)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row

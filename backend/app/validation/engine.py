"""
Validation stage: extracted invoices -> ValidationIssue rows, review flags, final status.

Rules are pure functions over one invoice (`RULES`); duplicate detection needs the database
and runs separately. Errors and warnings flag the invoice for review; info does not.
`review_reason` = existing `UNGROUNDED:*` reasons (set by extraction) + one entry per issue code.
The document ends `completed`, or `needs_review` when any invoice is flagged.
"""
import time
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import Document, Invoice, ValidationIssue
from app.processing import pipeline
from app.processing.errors import DocumentError, ErrorCode

ERROR, WARNING, INFO = "error", "warning", "info"
REQUIRED_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "total")
MIN_TOLERANCE = Decimal("0.02")
REL_TOLERANCE = Decimal("0.001")
MAX_TAX_RATE = Decimal("0.30")
MAX_AGE_DAYS = 365 * 10
LOW_CONFIDENCE = 0.5
UNGROUNDED_PREFIX = "UNGROUNDED:"


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    field: str | None = None
    details: dict = dc_field(default_factory=dict)


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(MIN_TOLERANCE, abs(b) * REL_TOLERANCE)


def missing_field(inv: Invoice, today: date) -> list[Issue]:
    out = [Issue("MISSING_FIELD", ERROR, f"{f.replace('_', ' ').capitalize()} is missing.", f)
           for f in REQUIRED_FIELDS if getattr(inv, f) in (None, "")]
    if not inv.currency:
        out.append(Issue("MISSING_FIELD", INFO, "Currency is missing.", "currency"))
    return out


def total_mismatch(inv: Invoice, today: date) -> list[Issue]:
    if None in (inv.subtotal, inv.tax, inv.total):
        return []
    discount = abs(inv.discount) if inv.discount is not None else Decimal(0)
    expected = inv.subtotal + inv.tax - discount
    if _close(inv.total, expected):
        return []
    formula = f"Subtotal {inv.subtotal} + tax {inv.tax}" + (f" - discount {discount}" if discount else "")
    return [Issue("TOTAL_MISMATCH", ERROR,
                  f"{formula} = {expected}, but total is {inv.total}.",
                  "total", {"expected": str(expected), "actual": str(inv.total)})]


def line_sum_mismatch(inv: Invoice, today: date) -> list[Issue]:
    totals = [li.line_total for li in inv.line_items]
    if not totals or None in totals:
        return []
    if inv.subtotal is not None:
        target, name = inv.subtotal, "subtotal"
    elif inv.tax is None and inv.total is not None:
        target, name = inv.total, "total"
    else:
        return []
    s = sum(totals, Decimal(0))
    if _close(s, target):
        return []
    return [Issue("LINE_SUM_MISMATCH", WARNING,
                  f"Line items add up to {s}, but {name} is {target}.", name,
                  {"line_sum": str(s), "compared_to": name, "value": str(target)})]


def line_math_mismatch(inv: Invoice, today: date) -> list[Issue]:
    out = []
    for i, li in enumerate(inv.line_items):
        if None in (li.quantity, li.unit_price, li.line_total):
            continue
        expected = li.quantity * li.unit_price
        if not _close(li.line_total, expected):
            out.append(Issue("LINE_MATH_MISMATCH", WARNING,
                             f"Line {i + 1}: {li.quantity} x {li.unit_price} = {expected}, "
                             f"but line total is {li.line_total}.", f"line_items[{i}]",
                             {"position": i, "expected": str(expected),
                              "actual": str(li.line_total)}))
    return out


def tax_anomaly(inv: Invoice, today: date) -> list[Issue]:
    if inv.tax is None or inv.subtotal is None or inv.subtotal <= 0:
        return []
    if inv.tax < 0 or inv.tax > inv.subtotal:
        why = "negative" if inv.tax < 0 else "larger than the subtotal"
        return [Issue("TAX_ANOMALY", WARNING, f"Tax {inv.tax} is {why}.", "tax")]
    rate = inv.tax / inv.subtotal
    if rate > MAX_TAX_RATE:
        return [Issue("TAX_ANOMALY", WARNING,
                      f"Tax is {rate:.0%} of the subtotal, above the {MAX_TAX_RATE:.0%} limit.",
                      "tax", {"rate": str(round(rate, 4))})]
    return []


def negative_amount(inv: Invoice, today: date) -> list[Issue]:
    return [Issue("NEGATIVE_AMOUNT", WARNING, f"{f.capitalize()} is negative ({v}); credit note?", f)
            for f in ("subtotal", "total") if (v := getattr(inv, f)) is not None and v < 0]


def date_out_of_range(inv: Invoice, today: date) -> list[Issue]:
    d = inv.invoice_date
    if d is None:
        return []
    if d > today:
        return [Issue("FUTURE_DATE", WARNING, f"Invoice date {d} is in the future.", "invoice_date")]
    if (today - d).days > MAX_AGE_DAYS:
        return [Issue("OLD_DATE", WARNING, f"Invoice date {d} is more than 10 years ago.",
                      "invoice_date")]
    return []


def low_confidence(inv: Invoice, today: date) -> list[Issue]:
    return [Issue("LOW_CONFIDENCE", WARNING, f"The extraction is unsure about {f} ({c:.2f}).", f,
                  {"confidence": c})
            for f, c in sorted((inv.field_confidence or {}).items())
            if isinstance(c, (int, float)) and c < LOW_CONFIDENCE]


RULES: dict[str, Callable[[Invoice, date], list[Issue]]] = {
    "MISSING_FIELD": missing_field, "TOTAL_MISMATCH": total_mismatch,
    "LINE_SUM_MISMATCH": line_sum_mismatch, "LINE_MATH_MISMATCH": line_math_mismatch,
    "TAX_ANOMALY": tax_anomaly, "NEGATIVE_AMOUNT": negative_amount,
    "DATE_OUT_OF_RANGE": date_out_of_range, "LOW_CONFIDENCE": low_confidence,
}


def check_invoice(inv: Invoice, today: date | None = None) -> list[Issue]:
    today = today or datetime.now(timezone.utc).date()
    return [i for rule in RULES.values() for i in rule(inv, today)]


def find_duplicate(db: Session, inv: Invoice) -> Issue | None:
    """Flag `inv` when the same user already holds an earlier invoice with the same
    supplier and invoice number (another document, a re-upload, or an earlier row)."""
    if not inv.supplier_norm or not inv.invoice_number_norm:
        return None
    doc = db.get(Document, inv.document_id)
    mine = (inv.created_at, str(inv.id))
    others = db.scalars(
        select(Invoice).join(Document, Document.id == Invoice.document_id)
        .where(Document.user_id == doc.user_id, Invoice.id != inv.id,
               Invoice.supplier_norm == inv.supplier_norm,
               Invoice.invoice_number_norm == inv.invoice_number_norm)
        .order_by(Invoice.created_at, Invoice.id)).all()
    earlier = [o for o in others if (o.created_at, str(o.id)) < mine]
    if not earlier:
        return None
    first = earlier[0]
    same_doc = first.document_id == inv.document_id
    return Issue("DUPLICATE_INVOICE", WARNING,
                 f"Invoice {inv.invoice_number} from {inv.supplier_name} already exists"
                 f"{' in this document' if same_doc else ''}.", "invoice_number",
                 {"original_invoice_id": str(first.id), "original_document_id": str(first.document_id),
                  "matches": len(earlier)})


def _review_reasons(inv: Invoice, issues: list[Issue]) -> list[str]:
    kept = [r for r in (inv.review_reason or []) if r.startswith(UNGROUNDED_PREFIX)]
    codes = list(dict.fromkeys(i.code for i in issues if i.severity in (ERROR, WARNING)))
    return kept + codes


def validate_invoice(db: Session, inv: Invoice, today: date | None = None) -> list[Issue]:
    """Re-run every rule for one invoice and replace its issues and review flags."""
    issues = check_invoice(inv, today)
    dup = find_duplicate(db, inv)
    if dup:
        issues.append(dup)
    db.query(ValidationIssue).filter(ValidationIssue.invoice_id == inv.id).delete()
    for i in issues:
        db.add(ValidationIssue(document_id=inv.document_id, invoice_id=inv.id, code=i.code,
                               severity=i.severity, field=i.field, message=i.message,
                               details=i.details or None))
    reasons = _review_reasons(inv, issues)
    inv.review_reason = reasons or None
    inv.needs_review = bool(reasons)
    return issues


def settle_document(db: Session, doc: Document) -> str:
    """completed when no invoice needs review, otherwise needs_review."""
    flagged = db.query(Invoice).filter(Invoice.document_id == doc.id,
                                       Invoice.needs_review.is_(True)).count()
    doc.status = "needs_review" if flagged else "completed"
    doc.progress_percent = 100.0
    doc.completed_at = datetime.now(timezone.utc)
    return doc.status


def validate_document(db: Session, document_id: uuid.UUID | str, will_retry: bool = False,
                      today: date | None = None) -> dict:
    doc_id = uuid.UUID(str(document_id))

    def work() -> dict:
        t0 = time.time()
        doc = db.get(Document, doc_id)
        if doc is None:
            raise DocumentError(ErrorCode.INTERNAL_ERROR, f"Document {doc_id} does not exist")
        doc.status = "validating"
        pipeline.add_event(db, doc_id, "started", "Validating invoices", stage="validation")
        db.commit()

        invoices = db.query(Invoice).filter(Invoice.document_id == doc_id).order_by(
            Invoice.segment_index).all()
        counts: dict[str, int] = {}
        for inv in invoices:
            for i in validate_invoice(db, inv, today):
                counts[i.code] = counts.get(i.code, 0) + 1
        status = settle_document(db, doc)
        ms = int((time.time() - t0) * 1000)
        pipeline.add_event(db, doc_id, "succeeded", f"Validation finished: {status}", ms,
                           {"invoices": len(invoices), "issues": counts, "status": status},
                           stage="validation")
        db.commit()
        return {"document_id": str(doc_id), "status": status, "issues": sum(counts.values()),
                "duration_ms": ms}

    return pipeline.run_stage(db, doc_id, will_retry, work, stage="validation")

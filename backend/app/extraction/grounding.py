"""
Grounding: every extracted value must be traceable to the document text.

A value that cannot be found in the text (an LLM invention) is reported as ungrounded
so the caller can lower its confidence and flag the invoice for review.
"""
import re
from datetime import date, datetime
from decimal import Decimal

from app.extraction.normalize import CURRENCY_SYMBOLS, norm_invoice_number
from app.processing.sheet_mapping import parse_decimal
from app.schemas.extraction import InvoiceExtraction

_NUM = re.compile(r"\(?-?[$€£]?\s?\d[\d.,' ]*\d\)?|\d")
_DATE_TOKENS = [
    re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"),
    re.compile(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"),
    re.compile(r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4}"),
    re.compile(r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"),
]
_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
            "%m-%d-%Y", "%d/%m/%y", "%m/%d/%y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%B %d %Y",
            "%b %d, %Y", "%b %d %Y")


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def numbers_in(text: str) -> set[Decimal]:
    out = set()
    for m in _NUM.finditer(text):
        # The pattern lets spaces join digits ("1 234,50"), which also glues table columns
        # ("2   90.00   180.00") into one token. Accept the whole token and each part of it.
        tok = m.group(0)
        for cand in (tok, *tok.split()):
            d = parse_decimal(cand)
            if d is not None:
                out.add(abs(d).normalize())
                out.add(d.normalize())
    return out


def dates_in(text: str) -> set[date]:
    """All dates the text can express (both day-first and month-first readings)."""
    out = set()
    for rx in _DATE_TOKENS:
        for m in rx.finditer(text):
            tok = re.sub(r"(?<=\d)(st|nd|rd|th)", "", m.group(0)).replace(".,", ",")
            for fmt in _FORMATS:
                try:
                    out.add(datetime.strptime(tok, fmt).date())
                except ValueError:
                    continue
    return out


def ungrounded_fields(inv: InvoiceExtraction, text: str) -> list[str]:
    """Names of extracted fields whose value does not appear in ``text``."""
    flat, nums = _squash(text), numbers_in(text)
    alnum_upper = re.sub(r"[^A-Z0-9]", "", text.upper())
    bad: list[str] = []

    def num_ok(v: Decimal | None) -> bool:
        return v is None or v.normalize() in nums or abs(v).normalize() in nums

    if inv.invoice_number and (_squash(inv.invoice_number) not in flat and
                               (norm_invoice_number(inv.invoice_number) or "") not in alnum_upper):
        bad.append("invoice_number")
    if inv.invoice_date and inv.invoice_date not in dates_in(text):
        bad.append("invoice_date")
    for f in ("supplier_name", "customer_name"):
        v = getattr(inv, f)
        if v and _squash(v) not in flat:
            bad.append(f)
    for f in ("subtotal", "tax", "discount", "total"):
        if not num_ok(getattr(inv, f)):
            bad.append(f)
    if inv.currency:
        symbols = [s for s, c in CURRENCY_SYMBOLS.items() if c == inv.currency]
        if inv.currency.lower() not in flat and not any(s in text for s in symbols) \
                and not (inv.currency == "USD" and "$" in text):
            bad.append("currency")
    for i, li in enumerate(inv.line_items):
        if li.description and _squash(li.description) not in flat:
            bad.append(f"line_items[{i}].description")
        for f in ("quantity", "unit_price", "line_total", "tax_rate"):
            if not num_ok(getattr(li, f)):
                bad.append(f"line_items[{i}].{f}")
    return bad

"""Turn raw LLM strings into typed values and build normalised duplicate-detection keys."""
import re
import unicodedata
from datetime import date

from app.processing.sheet_mapping import parse_date as parse_date_loose
from app.processing.sheet_mapping import parse_decimal
from app.schemas.extraction import InvoiceExtraction, LineItemExtraction, LLMInvoice, LLMLineItem

__all__ = ["parse_decimal", "parse_date_strict", "normalize_currency", "norm_supplier",
           "norm_invoice_number", "normalize_invoice", "CURRENCY_SYMBOLS"]

CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}   # "$" is ambiguous
_LEGAL = {"ltd", "limited", "inc", "incorporated", "llc", "llp", "gmbh", "ag", "corp",
          "corporation", "co", "company", "sa", "srl", "bv", "plc", "pty", "pvt", "oy", "ab"}


def parse_date_strict(value) -> date | None:
    """ISO first. Numeric day/month dates parse only when unambiguous (one part > 12)."""
    if value is None or value == "":
        return None
    s = str(value).strip()
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12 and b <= 12:
            day, month = a, b
        elif b > 12 and a <= 12:
            day, month = b, a
        else:
            return None                      # ambiguous (or invalid): never guess
        try:
            return date(y, month, day)
        except ValueError:
            return None
    return parse_date_loose(s)


def normalize_currency(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if s in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[s]
    return s.upper() if re.fullmatch(r"[A-Za-z]{3}", s) else None


def _text(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def norm_supplier(name: str | None) -> str | None:
    if not name:
        return None
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", s).split() if w not in _LEGAL]
    return " ".join(words) or None


def norm_invoice_number(number: str | None) -> str | None:
    if not number:
        return None
    s = re.sub(r"[^A-Z0-9]", "", number.upper())
    return (s.lstrip("0") or s[-1:]) or None


def _line(li: LLMLineItem) -> LineItemExtraction:
    return LineItemExtraction(
        description=_text(li.description), quantity=parse_decimal(li.quantity),
        unit_price=parse_decimal(li.unit_price), line_total=parse_decimal(li.line_total),
        tax_rate=parse_decimal(li.tax_rate))


def _abs(v):
    return abs(v) if v is not None else None


def normalize_invoice(raw: LLMInvoice) -> InvoiceExtraction:
    lines = [_line(li) for li in raw.line_items]
    return InvoiceExtraction(
        invoice_number=_text(raw.invoice_number), invoice_date=parse_date_strict(raw.invoice_date),
        supplier_name=_text(raw.supplier_name), customer_name=_text(raw.customer_name),
        subtotal=parse_decimal(raw.subtotal), tax=parse_decimal(raw.tax),
        discount=_abs(parse_decimal(raw.discount)),
        total=parse_decimal(raw.total), currency=normalize_currency(raw.currency),
        line_items=[li for li in lines if any(v is not None for v in li.model_dump().values())])

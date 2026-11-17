"""
Spreadsheet -> InvoiceExtraction, without an LLM.

Steps: drop trailing totals rows, map column headers to invoice fields with a
synonym table, then build invoices in one of two layouts:

  * "row_per_invoice"      - each row is one invoice
  * "grouped_by_invoice"   - several rows (line items) share an invoice number

If required columns stay unmapped, an optional ``llm_mapper`` hook is asked for a
mapping (filled in by the extraction stage); without it the sheet is returned with
what the heuristics found.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable

from app.schemas.extraction import INVOICE_FIELDS, LINE_FIELDS, InvoiceExtraction, LineItemExtraction

# canonical field -> accepted header names (compared after normalisation)
SYNONYMS: dict[str, tuple[str, ...]] = {
    "invoice_number": ("invoice number", "invoice no", "invoice num", "invoice id", "invoice",
                       "inv no", "inv number", "inv", "bill number", "bill no", "document number",
                       "doc no", "reference", "ref no", "number"),
    "invoice_date": ("invoice date", "date", "inv date", "bill date", "issue date", "issued",
                     "document date", "date issued"),
    "supplier_name": ("supplier", "supplier name", "vendor", "vendor name", "seller", "from",
                      "billed by", "company", "issuer"),
    "customer_name": ("customer", "customer name", "client", "client name", "buyer", "bill to",
                      "billed to", "sold to", "to"),
    "subtotal": ("subtotal", "sub total", "net", "net amount", "amount before tax",
                 "total excl tax", "total ex vat", "net total"),
    "tax": ("tax", "tax amount", "vat", "vat amount", "gst", "sales tax", "total tax"),
    "total": ("total", "total amount", "grand total", "amount due", "invoice total", "gross",
              "gross amount", "total incl tax", "total inc vat", "amount"),
    "currency": ("currency", "curr", "ccy", "currency code"),
    "description": ("description", "item", "item description", "product", "details",
                    "line description", "service"),
    "quantity": ("quantity", "qty", "units", "count"),
    "unit_price": ("unit price", "price", "rate", "unit cost", "price each", "cost"),
    "line_total": ("line total", "line amount", "amount line", "extended", "extended price",
                   "item total", "line net"),
    "tax_rate": ("tax rate", "vat rate", "vat %", "tax %", "gst rate"),
}

REQUIRED_FOR_MAPPING = ("invoice_number", "total")
_TOTALS_LABEL = re.compile(r"^\s*(grand\s+|sub\s*)?totals?\b|^\s*sum\b|^\s*(report\s+)?total\b",
                           re.IGNORECASE)

LlmMapper = Callable[[list[str], list[dict]], dict[str, str]]


def normalize_header(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%]+", " ", str(name).lower())).strip()


_LOOKUP: dict[str, str] = {}
for _canon, _names in SYNONYMS.items():
    for _n in _names:
        _LOOKUP.setdefault(normalize_header(_n), _canon)
for _canon in SYNONYMS:
    _LOOKUP.setdefault(normalize_header(_canon), _canon)


def count_header_matches(cells) -> int:
    """How many cells look like known invoice column names (used for header-row detection)."""
    return len({_LOOKUP[h] for c in cells if c is not None and (h := normalize_header(c)) in _LOOKUP})


def map_columns(columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return ({field: column}, unmapped_columns). First matching column wins per field."""
    mapping: dict[str, str] = {}
    unmapped = []
    for col in columns:
        canon = _LOOKUP.get(normalize_header(col))
        if canon and canon not in mapping:
            mapping[canon] = col
        else:
            unmapped.append(col)
    return mapping, unmapped


# ----------------------------------------------------------------- value parsing
def parse_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    s = str(value).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        # the last separator is the decimal mark
        dec, thou = (",", ".") if s.rfind(",") > s.rfind(".") else (".", ",")
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s:
        # "12,50" is a decimal comma; "1,234" and "1,234,567" are thousands separators
        head, _, tail = s.rpartition(",")
        if s.count(",") == 1 and len(tail) != 3:
            s = head + "." + tail
        else:
            s = s.replace(",", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if neg else d


def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y",
                "%d-%m-%Y", "%B %d, %Y", "%d %B %Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _text(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if isinstance(value, float) and value.is_integer():
        s = str(int(value))          # 1001.0 -> "1001"
    return s or None


# ----------------------------------------------------------------- rows
def drop_totals_rows(rows: list[dict], columns: list[str]) -> tuple[list[dict], int]:
    """Remove trailing summary rows ("Total", "Grand total", ...). Interior rows are kept."""
    keep = list(rows)
    dropped = 0
    while keep:
        first = next((v for v in (keep[-1].get(c) for c in columns) if v not in (None, "")), None)
        if first is not None and _TOTALS_LABEL.match(str(first)):
            keep.pop()
            dropped += 1
        else:
            break
    return keep, dropped


def _line(row: dict, mapping: dict[str, str]) -> LineItemExtraction | None:
    values = {
        "description": _text(row.get(mapping["description"])) if "description" in mapping else None,
        "quantity": parse_decimal(row.get(mapping.get("quantity", ""))),
        "unit_price": parse_decimal(row.get(mapping.get("unit_price", ""))),
        "line_total": parse_decimal(row.get(mapping.get("line_total", ""))),
        "tax_rate": parse_decimal(row.get(mapping.get("tax_rate", ""))),
    }
    return LineItemExtraction(**values) if any(v is not None for v in values.values()) else None


def _header_fields(row: dict, mapping: dict[str, str]) -> dict:
    out = {}
    for f in INVOICE_FIELDS:
        if f not in mapping:
            continue
        raw = row.get(mapping[f])
        if f == "invoice_date":
            out[f] = parse_date(raw)
        elif f in ("subtotal", "tax", "total"):
            out[f] = parse_decimal(raw)
        elif f == "currency":
            t = _text(raw)
            out[f] = t.upper() if t else None
        else:
            out[f] = _text(raw)
    return out


def _is_grouped(rows: list[dict], mapping: dict[str, str]) -> bool:
    if "invoice_number" not in mapping or not any(f in mapping for f in LINE_FIELDS):
        return False
    numbers = [_text(r.get(mapping["invoice_number"])) for r in rows]
    numbers = [n for n in numbers if n]
    return len(numbers) != len(set(numbers))


@dataclass
class SheetExtraction:
    invoices: list[InvoiceExtraction] = field(default_factory=list)
    layout: str = "row_per_invoice"
    mapping: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    dropped_totals_rows: int = 0
    mapping_source: str = "heuristic"      # heuristic | llm
    sheet_name: str | None = None

    def details(self) -> dict:
        """JSON-safe summary stored in the processing-event details."""
        return {"sheet": self.sheet_name, "layout": self.layout, "mapping": self.mapping,
                "unmapped_columns": self.unmapped_columns, "mapping_source": self.mapping_source,
                "dropped_totals_rows": self.dropped_totals_rows, "invoices": len(self.invoices)}


def extract_from_rows(columns: list[str], rows: list[dict],
                      llm_mapper: LlmMapper | None = None,
                      sheet_name: str | None = None) -> SheetExtraction:
    rows, dropped = drop_totals_rows(rows, columns)
    mapping, unmapped = map_columns(columns)
    source = "heuristic"

    if llm_mapper and any(f not in mapping for f in REQUIRED_FOR_MAPPING) and unmapped:
        try:
            suggested = llm_mapper(list(unmapped), rows[:5]) or {}
        except Exception:          # the fallback is best-effort; heuristics stay authoritative
            suggested = {}
        for col, canon in suggested.items():
            if col in unmapped and canon in SYNONYMS and canon not in mapping:
                mapping[canon] = col
                unmapped.remove(col)
                source = "llm"

    result = SheetExtraction(mapping=mapping, unmapped_columns=unmapped,
                             dropped_totals_rows=dropped, mapping_source=source,
                             sheet_name=sheet_name)
    if not any(f in mapping for f in INVOICE_FIELDS):
        return result                                      # nothing recognisable

    if _is_grouped(rows, mapping):
        result.layout = "grouped_by_invoice"
        groups: dict[str, InvoiceExtraction] = {}
        for row in rows:
            key = _text(row.get(mapping["invoice_number"]))
            if key is None:
                continue
            fields = _header_fields(row, mapping)
            inv = groups.setdefault(key, InvoiceExtraction())
            for k, v in fields.items():                    # first non-empty value wins
                if v is not None and getattr(inv, k) is None:
                    setattr(inv, k, v)
            line = _line(row, mapping)
            if line:
                inv.line_items.append(line)
        result.invoices = list(groups.values())
        return result

    for row in rows:
        fields = _header_fields(row, mapping)
        line = _line(row, mapping)
        if not any(v is not None for v in fields.values()) and line is None:
            continue
        inv = InvoiceExtraction(**fields)
        if line:
            inv.line_items.append(line)
        result.invoices.append(inv)
    return result

"""
Structured invoice extraction result (shared by spreadsheet mapping and LLM extraction).

Every field is optional: a value that is absent from the document stays ``None``
and is never guessed.
"""
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class LineItemExtraction(BaseModel):
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    tax_rate: Decimal | None = None


class InvoiceExtraction(BaseModel):
    invoice_number: str | None = None
    invoice_date: date | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    currency: str | None = None
    line_items: list[LineItemExtraction] = Field(default_factory=list)


INVOICE_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                  "subtotal", "tax", "total", "currency")
LINE_FIELDS = ("description", "quantity", "unit_price", "line_total", "tax_rate")

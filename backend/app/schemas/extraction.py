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


# ---- raw LLM reply: values stay exactly as printed; normalize.py converts them ----
Scalar = str | int | float | None


class LLMLineItem(BaseModel):
    model_config = {"extra": "ignore"}
    description: Scalar = None
    quantity: Scalar = None
    unit_price: Scalar = None
    line_total: Scalar = None
    tax_rate: Scalar = None


class LLMInvoice(BaseModel):
    model_config = {"extra": "ignore"}
    invoice_number: Scalar = None
    invoice_date: Scalar = None
    supplier_name: Scalar = None
    customer_name: Scalar = None
    subtotal: Scalar = None
    tax: Scalar = None
    total: Scalar = None
    currency: Scalar = None
    line_items: list[LLMLineItem] = Field(default_factory=list)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    field_evidence: dict[str, str | None] = Field(default_factory=dict)


INVOICE_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
                  "subtotal", "tax", "total", "currency")
LINE_FIELDS = ("description", "quantity", "unit_price", "line_total", "tax_rate")

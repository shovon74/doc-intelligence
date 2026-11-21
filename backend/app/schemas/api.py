"""Request and response models for the REST API."""
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

T = TypeVar("T")


# The database keeps 4 decimals; API output shows money with 2 and quantities without trailing zeros.
Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str, when_used="json")]
Quantity = Annotated[Decimal, PlainSerializer(lambda v: format(v.normalize(), "f"), return_type=str,
                                              when_used="json")]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PageOut(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


# ------------------------------------------------------------------ auth
class Credentials(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", v):
            raise ValueError("Enter a valid email address.")
        return v


class LoginIn(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=200)


class UserOut(ORM):
    id: uuid.UUID
    email: str
    role: str
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ------------------------------------------------------------------ documents
class DocumentOut(ORM):
    id: uuid.UUID
    original_filename: str
    mime_type: str
    file_ext: str
    size_bytes: int
    page_count: int
    status: str
    progress_percent: float
    error_code: str | None
    error_message: str | None
    llm_provider_requested: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    invoice_count: int = 0
    needs_review_count: int = 0


class DocumentStatusOut(BaseModel):
    id: uuid.UUID
    status: str
    progress_percent: float
    error_code: str | None
    error_message: str | None
    stage: str | None = None


class EventOut(ORM):
    stage: str
    status: str
    message: str | None
    duration_ms: int | None
    created_at: datetime


class IssueOut(ORM):
    id: uuid.UUID
    invoice_id: uuid.UUID | None
    code: str
    severity: str
    field: str | None
    message: str
    details: dict[str, Any] | None
    resolved: bool


class LineItemOut(ORM):
    id: uuid.UUID
    position: int
    description: str | None
    quantity: Quantity | None
    unit_price: Money | None
    line_total: Money | None
    tax_rate: Decimal | None


class CorrectionOut(ORM):
    field: str
    old_value: str | None
    new_value: str | None
    created_at: datetime


class InvoiceSummary(ORM):
    id: uuid.UUID
    document_id: uuid.UUID
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    customer_name: str | None
    subtotal: Money | None
    tax: Money | None
    discount: Money | None = None
    total: Money | None
    currency: str | None
    needs_review: bool
    review_reason: list[str] | None
    is_corrected: bool


class InvoiceOut(InvoiceSummary):
    segment_index: int
    page_start: int
    page_end: int
    field_confidence: dict[str, Any] | None
    field_evidence: dict[str, Any] | None
    provider: str | None
    model: str | None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    line_items: list[LineItemOut] = []
    issues: list[IssueOut] = Field([], validation_alias="validation_issues")
    corrections: list[CorrectionOut] = []


class DocumentDetail(DocumentOut):
    invoices: list[InvoiceOut] = []
    events: list[EventOut] = []


class PageTextOut(ORM):
    page_number: int
    source_type: str
    text: str
    ocr_confidence: float | None


class ResultOut(BaseModel):
    document: DocumentOut
    invoices: list[InvoiceOut]
    pages: list[PageTextOut] | None = None


class ReprocessFailedOut(BaseModel):
    requeued: list[uuid.UUID]


class UploadedOut(BaseModel):
    id: uuid.UUID
    filename: str
    status: str
    duplicate: bool


class RejectedOut(BaseModel):
    filename: str
    code: str
    message: str


class UploadResult(BaseModel):
    documents: list[UploadedOut]
    rejected: list[RejectedOut]


class ReprocessIn(BaseModel):
    llm_provider: str | None = None


# ------------------------------------------------------------------ invoice edits
class InvoiceUpdate(BaseModel):
    """Only the fields that are sent are changed; null clears a field."""
    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None = Field(None, max_length=200)
    invoice_date: date | None = None
    supplier_name: str | None = Field(None, max_length=500)
    customer_name: str | None = Field(None, max_length=500)
    subtotal: Decimal | None = Field(None, max_digits=18, decimal_places=4)
    tax: Decimal | None = Field(None, max_digits=18, decimal_places=4)
    discount: Decimal | None = Field(None, max_digits=18, decimal_places=4)
    total: Decimal | None = Field(None, max_digits=18, decimal_places=4)
    currency: str | None = Field(None, min_length=3, max_length=3, pattern=r"^[A-Za-z]{3}$")

    @field_validator("invoice_number", "supplier_name", "customer_name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return (v.strip() or None) if v is not None else None

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v


class ConfirmIn(BaseModel):
    """Fields (or whole lines, e.g. "line_items[1]") the user checked and found correct."""
    model_config = ConfigDict(extra="forbid")

    fields: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=200)


class LineItemUpdate(BaseModel):
    """Only the fields that are sent are changed; null clears a field."""
    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(None, max_length=2000)
    quantity: Decimal | None = Field(None, max_digits=18, decimal_places=4)
    unit_price: Decimal | None = Field(None, max_digits=18, decimal_places=4)
    line_total: Decimal | None = Field(None, max_digits=18, decimal_places=4)

    @field_validator("description")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return (v.strip() or None) if v is not None else None


# ------------------------------------------------------------------ providers
class ProvidersOut(BaseModel):
    llm: dict[str, Any]
    embedding: dict[str, Any]

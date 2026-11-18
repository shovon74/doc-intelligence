import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, Page, get_current_user, pagination
from app.api.v1.documents import IN_FLIGHT
from app.db.models import Correction, Document, Invoice
from app.db.session import get_db
from app.extraction.normalize import norm_invoice_number, norm_supplier
from app.schemas.api import InvoiceOut, InvoiceSummary, InvoiceUpdate, PageOut
from app.validation import engine

router = APIRouter(prefix="/invoices", tags=["Invoices"])

_SEVERITY = {"error": 0, "warning": 1, "info": 2}


def invoice_out(inv: Invoice) -> InvoiceOut:
    out = InvoiceOut.model_validate(inv)
    out.issues = sorted(out.issues, key=lambda i: (_SEVERITY.get(i.severity, 3), i.code))
    return out


def _owned(db: Session, user: CurrentUser, invoice_id: uuid.UUID) -> Invoice:
    inv = db.scalar(
        select(Invoice).join(Document, Document.id == Invoice.document_id)
        .where(Invoice.id == invoice_id, Document.user_id == user.id)
        .options(selectinload(Invoice.line_items), selectinload(Invoice.validation_issues),
                 selectinload(Invoice.corrections)))
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found.")
    return inv


@router.get("", response_model=PageOut[InvoiceSummary], summary="Search and filter your invoices")
def list_invoices(
        supplier: str | None = Query(None, max_length=200, description="Supplier contains"),
        invoice_number: str | None = Query(None, max_length=200),
        document_id: uuid.UUID | None = None, currency: str | None = Query(None, max_length=3),
        needs_review: bool | None = None, date_from: date | None = None, date_to: date | None = None,
        min_total: Decimal | None = None, max_total: Decimal | None = None,
        page: Page = Depends(pagination), db: Session = Depends(get_db),
        user: CurrentUser = Depends(get_current_user)):
    query = (select(Invoice).join(Document, Document.id == Invoice.document_id)
             .where(Document.user_id == user.id))

    def contains(column, text):
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return column.ilike(f"%{escaped}%", escape="\\")

    if supplier:
        query = query.where(contains(Invoice.supplier_name, supplier))
    if invoice_number:
        query = query.where(contains(Invoice.invoice_number, invoice_number))
    if document_id:
        query = query.where(Invoice.document_id == document_id)
    if currency:
        query = query.where(Invoice.currency == currency.upper())
    if needs_review is not None:
        query = query.where(Invoice.needs_review.is_(needs_review))
    if date_from:
        query = query.where(Invoice.invoice_date >= date_from)
    if date_to:
        query = query.where(Invoice.invoice_date <= date_to)
    if min_total is not None:
        query = query.where(Invoice.total >= min_total)
    if max_total is not None:
        query = query.where(Invoice.total <= max_total)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Invoice.invoice_date.desc().nulls_last(),
                                     Invoice.created_at.desc(), Invoice.id)
                      .offset(page.offset).limit(page.page_size)).all()
    return PageOut(items=[InvoiceSummary.model_validate(r) for r in rows], total=total,
                   page=page.page, page_size=page.page_size)


@router.get("/{invoice_id}", response_model=InvoiceOut,
            summary="One invoice with line items, issues and corrections")
def get_invoice(invoice_id: uuid.UUID, db: Session = Depends(get_db),
                user: CurrentUser = Depends(get_current_user)):
    return invoice_out(_owned(db, user, invoice_id))


@router.patch("/{invoice_id}", response_model=InvoiceOut,
              summary="Correct extracted fields; the invoice is re-validated immediately")
def correct_invoice(invoice_id: uuid.UUID, body: InvoiceUpdate, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(get_current_user)):
    sent = body.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Send at least one field to change.")
    inv = _owned(db, user, invoice_id)
    doc = db.get(Document, inv.document_id)
    if doc.status in IN_FLIGHT:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This document is still being processed. Wait for it to finish.")

    changed = [f for f, v in sent.items() if getattr(inv, f) != v]
    for f in changed:
        old, new = getattr(inv, f), sent[f]
        db.add(Correction(invoice_id=inv.id, user_id=user.id, field=f,
                          old_value=None if old is None else str(old),
                          new_value=None if new is None else str(new)))
        setattr(inv, f, new)
    if changed:
        if "invoice_number" in changed:
            inv.invoice_number_norm = norm_invoice_number(inv.invoice_number)
        if "supplier_name" in changed:
            inv.supplier_norm = norm_supplier(inv.supplier_name)
        inv.is_corrected = True
        inv.review_reason = [r for r in (inv.review_reason or [])
                             if r.removeprefix(engine.UNGROUNDED_PREFIX) not in changed] or None
        inv.field_confidence = {**(inv.field_confidence or {}), **{f: 1.0 for f in changed}}
        engine.validate_invoice(db, inv)
        engine.settle_document(db, doc)
        db.commit()
        db.refresh(inv, ["validation_issues", "corrections", "line_items"])
    return invoice_out(inv)

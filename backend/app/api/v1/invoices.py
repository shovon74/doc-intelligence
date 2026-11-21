import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser, Page, get_current_user, pagination
from app.api.v1.documents import IN_FLIGHT
from app.db.models import Correction, Document, Invoice, LineItem, ValidationIssue
from app.db.session import get_db
from app.extraction.normalize import norm_invoice_number, norm_supplier
from app.schemas.api import (ConfirmIn, InvoiceOut, InvoiceSummary, InvoiceUpdate, LineItemUpdate,
                             PageOut)
from app.search.indexer import safe_reindex_invoice
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
        safe_reindex_invoice(db, inv)
        db.refresh(inv, ["validation_issues", "corrections", "line_items"])
    return invoice_out(inv)


# ---------------------------------------------------------------- line items
def _editable(db: Session, user: CurrentUser, invoice_id: uuid.UUID) -> tuple[Invoice, Document]:
    inv = _owned(db, user, invoice_id)
    doc = db.get(Document, inv.document_id)
    if doc.status in IN_FLIGHT:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This document is still being processed. Wait for it to finish.")
    return inv, doc


def _line_or_404(inv: Invoice, line_id: uuid.UUID) -> LineItem:
    line = next((li for li in inv.line_items if li.id == line_id), None)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Line item not found.")
    return line


def _finish_line_change(db: Session, inv: Invoice, doc: Document, drop_ungrounded: str) -> InvoiceOut:
    """After any line edit: mark corrected, forget stale line flags, re-validate, settle."""
    db.flush()
    db.refresh(inv, ["line_items"])
    for i, li in enumerate(sorted(inv.line_items, key=lambda x: x.position)):
        li.position = i                                   # keep positions 0..n-1 after add/delete
    inv.is_corrected = True
    prefix = engine.UNGROUNDED_PREFIX + drop_ungrounded
    inv.review_reason = [r for r in (inv.review_reason or []) if not r.startswith(prefix)] or None
    # confidence keys of lines are index based; once lines change they no longer line up
    inv.field_confidence = {k: v for k, v in (inv.field_confidence or {}).items()
                            if not k.startswith("line_items")}
    db.flush()
    db.refresh(inv, ["line_items"])
    engine.validate_invoice(db, inv)
    engine.settle_document(db, doc)
    db.commit()
    safe_reindex_invoice(db, inv)
    db.refresh(inv, ["validation_issues", "corrections", "line_items"])
    return invoice_out(inv)


@router.post("/{invoice_id}/line-items", response_model=InvoiceOut,
             status_code=status.HTTP_201_CREATED,
             summary="Add a line item the extraction missed; the invoice is re-validated")
def add_line_item(invoice_id: uuid.UUID, body: LineItemUpdate, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(get_current_user)):
    inv, doc = _editable(db, user, invoice_id)
    values = body.model_dump(exclude_unset=True)
    if not any(v not in (None, "") for v in values.values()):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Enter at least one value.")
    position = max((li.position for li in inv.line_items), default=-1) + 1
    inv.line_items.append(LineItem(position=position, **values))
    db.add(Correction(invoice_id=inv.id, user_id=user.id, field=f"line_items[{position}]",
                      old_value=None, new_value=values.get("description") or "(added line)"))
    return _finish_line_change(db, inv, doc, "line_items[")


@router.patch("/{invoice_id}/line-items/{line_id}", response_model=InvoiceOut,
              summary="Correct one line item; the invoice is re-validated immediately")
def correct_line_item(invoice_id: uuid.UUID, line_id: uuid.UUID, body: LineItemUpdate,
                      db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    sent = body.model_dump(exclude_unset=True)
    if not sent:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Send at least one field to change.")
    inv, doc = _editable(db, user, invoice_id)
    line = _line_or_404(inv, line_id)
    changed = [f for f, v in sent.items() if getattr(line, f) != v]
    if not changed:
        return invoice_out(inv)
    for f in changed:
        old, new = getattr(line, f), sent[f]
        db.add(Correction(invoice_id=inv.id, user_id=user.id,
                          field=f"line_items[{line.position}].{f}",
                          old_value=None if old is None else str(old),
                          new_value=None if new is None else str(new)))
        setattr(line, f, new)
    return _finish_line_change(db, inv, doc, f"line_items[{line.position}]")


@router.delete("/{invoice_id}/line-items/{line_id}", response_model=InvoiceOut,
               summary="Remove a line item; the invoice is re-validated immediately")
def delete_line_item(invoice_id: uuid.UUID, line_id: uuid.UUID, db: Session = Depends(get_db),
                     user: CurrentUser = Depends(get_current_user)):
    inv, doc = _editable(db, user, invoice_id)
    line = _line_or_404(inv, line_id)
    db.add(Correction(invoice_id=inv.id, user_id=user.id, field=f"line_items[{line.position}]",
                      old_value=line.description or "(line removed)", new_value=None))
    inv.line_items.remove(line)
    return _finish_line_change(db, inv, doc, "line_items[")


@router.post("/{invoice_id}/confirm", response_model=InvoiceOut,
             summary="Mark values flagged as 'not found in the document text' as checked and correct")
def confirm_fields(invoice_id: uuid.UUID, body: ConfirmIn, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    inv, doc = _editable(db, user, invoice_id)

    def confirmed(reason: str) -> bool:
        if not reason.startswith(engine.UNGROUNDED_PREFIX):
            return False
        name = reason.removeprefix(engine.UNGROUNDED_PREFIX)
        # "line_items[1]" confirms every flagged value of that line
        return any(name == f or name.startswith(f + ".") for f in body.fields)

    before = inv.review_reason or []
    inv.review_reason = [r for r in before if not confirmed(r)] or None
    if inv.review_reason != (before or None):
        engine.validate_invoice(db, inv)
        engine.settle_document(db, doc)
        db.commit()
        db.refresh(inv, ["validation_issues", "corrections", "line_items"])
    return invoice_out(inv)


@router.post("/{invoice_id}/resolve-issue/{issue_id}", response_model=InvoiceOut,
             summary="Acknowledge an issue; the invoice stops being flagged once none are open")
def resolve_issue(invoice_id: uuid.UUID, issue_id: uuid.UUID, db: Session = Depends(get_db),
                  user: CurrentUser = Depends(get_current_user)):
    inv = _owned(db, user, invoice_id)
    issue = db.scalar(select(ValidationIssue).where(ValidationIssue.id == issue_id,
                                                    ValidationIssue.invoice_id == inv.id))
    if issue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Issue not found.")
    doc = db.get(Document, inv.document_id)
    if doc.status in IN_FLIGHT:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This document is still being processed. Wait for it to finish.")
    issue.resolved = True
    db.flush()
    open_issues = [i for i in db.scalars(select(ValidationIssue).where(
        ValidationIssue.invoice_id == inv.id, ValidationIssue.resolved.is_(False)))
        if i.severity in (engine.ERROR, engine.WARNING)]
    # all issues acknowledged: also clear ungrounded flags, which have no issue row to resolve
    reasons = engine._review_reasons(inv, open_issues) if open_issues else []
    inv.review_reason = reasons or None
    inv.needs_review = bool(reasons)
    engine.settle_document(db, doc)
    db.commit()
    db.refresh(inv, ["validation_issues", "corrections", "line_items"])
    return invoice_out(inv)

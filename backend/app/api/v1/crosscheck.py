import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user
from app.db.models import Crosscheck
from app.db.session import get_db
from app.validation.crosscheck import DocumentNotFound, DocumentNotReady, run_crosscheck

router = APIRouter()


class CrosscheckRequest(BaseModel):
    document_a_id: uuid.UUID
    document_b_id: uuid.UUID


class CrosscheckOut(BaseModel):
    id: uuid.UUID
    document_a_id: uuid.UUID
    document_b_id: uuid.UUID
    status: str
    result: dict

    @classmethod
    def of(cls, row: Crosscheck) -> "CrosscheckOut":
        return cls(id=row.id, document_a_id=row.document_a_id, document_b_id=row.document_b_id,
                   status=row.status, result=row.result)


@router.post("/crosscheck", response_model=CrosscheckOut, status_code=status.HTTP_201_CREATED,
             summary="Compare the invoices of two documents")
def create_crosscheck(body: CrosscheckRequest, db: Session = Depends(get_db),
                      user: CurrentUser = Depends(get_current_user)):
    if body.document_a_id == body.document_b_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Choose two different documents.")
    try:
        row = run_crosscheck(db, user.id, body.document_a_id, body.document_b_id)
    except DocumentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
    except DocumentNotReady as e:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Document is not processed yet: {e}.")
    return CrosscheckOut.of(row)


@router.get("/crosscheck/{crosscheck_id}", response_model=CrosscheckOut,
            summary="Get a cross-check result")
def get_crosscheck(crosscheck_id: uuid.UUID, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(get_current_user)):
    row = db.get(Crosscheck, crosscheck_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cross-check not found.")
    return CrosscheckOut.of(row)

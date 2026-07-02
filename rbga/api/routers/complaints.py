"""Anonymous complaints endpoints.

Two asymmetric access levels on purpose:
  * POST /complaints  — PUBLIC. Anyone can submit. We record only what the form
    sends; the server adds NOTHING identifying (no IP, no user agent).
  * GET  /complaints  — RESTRICTED. Requires the reviewer token, so reading the
    complaint log is deliberately separated from mere server/API access.

See CLAUDE.md for why the complaints data ideally lives off personal
infrastructure and in its own DB schema/credentials.
"""
import os
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.database import get_session
from ...db.models import Complaint, ComplaintCategory

router = APIRouter(prefix="/complaints", tags=["complaints"])

_REVIEWER_TOKEN = os.environ.get("COMPLAINTS_API_TOKEN")


def require_reviewer(x_reviewer_token: str | None = Header(default=None)):
    # Fail closed: if no token is configured, nobody can read complaints.
    if not _REVIEWER_TOKEN or x_reviewer_token != _REVIEWER_TOKEN:
        raise HTTPException(403, "Not authorised to read complaints")


class ComplaintIn(BaseModel):
    category: ComplaintCategory
    body: str
    contact: str | None = None  # optional; only if the submitter wants a reply


class ComplaintAck(BaseModel):
    """Minimal acknowledgement — we don't echo the content back."""

    id: int
    created_at: datetime


class ComplaintOut(ComplaintAck):
    model_config = ConfigDict(from_attributes=True)

    category: ComplaintCategory
    body: str
    contact: str | None


@router.post("", response_model=ComplaintAck, status_code=201)
def submit(data: ComplaintIn, db: Session = Depends(get_session)):
    complaint = Complaint(category=data.category, body=data.body, contact=data.contact)
    db.add(complaint)
    db.commit()
    db.refresh(complaint)
    return complaint


@router.get("", response_model=list[ComplaintOut], dependencies=[Depends(require_reviewer)])
def list_complaints(db: Session = Depends(get_session)):
    return db.scalars(select(Complaint).order_by(Complaint.created_at.desc())).all()

"""Anonymous complaints endpoints.

Three access levels on purpose:
  * POST  /complaints        — PUBLIC. Anyone can submit. We record only what the
    form sends; the server adds NOTHING identifying (no IP, no user agent).
  * GET   /complaints        — RESTRICTED. Requires the reviewer token, so reading
    the complaint log is deliberately separated from mere server/API access.
  * PATCH /complaints/{id}    — RESTRICTED (reviewer). Acknowledge / escalate /
    close a complaint per the ladder in docs/complaints-policy.md.

See CLAUDE.md for why the complaints data ideally lives off personal
infrastructure and in its own DB schema/credentials.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_reviewer
from ...db.database import get_session
from ...db.models import Complaint, ComplaintCategory, ComplaintStatus, EscalationTarget

router = APIRouter(prefix="/complaints", tags=["complaints"])


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
    status: ComplaintStatus
    escalated_to: EscalationTarget | None
    closed_at: datetime | None


class ComplaintUpdate(BaseModel):
    """Reviewer action on a complaint. Both fields optional: send whichever you
    want to change. Setting `escalated_to` implies `status = escalated` unless a
    `status` is given explicitly in the same request."""

    status: ComplaintStatus | None = None
    escalated_to: EscalationTarget | None = None


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


@router.patch(
    "/{complaint_id}",
    response_model=ComplaintOut,
    dependencies=[Depends(require_reviewer)],
)
def update_complaint(
    complaint_id: int, data: ComplaintUpdate, db: Session = Depends(get_session)
):
    complaint = db.get(Complaint, complaint_id)
    if not complaint:
        raise HTTPException(404, "No such complaint")

    # Escalating implies the 'escalated' status; an explicit status below wins.
    if data.escalated_to is not None:
        complaint.escalated_to = data.escalated_to
        complaint.status = ComplaintStatus.escalated
    if data.status is not None:
        complaint.status = data.status

    # Keep closed_at in step with the status so retention can purge on it.
    if complaint.status == ComplaintStatus.closed:
        if complaint.closed_at is None:
            complaint.closed_at = datetime.utcnow()
    else:
        complaint.closed_at = None  # reopened

    db.commit()
    db.refresh(complaint)
    return complaint

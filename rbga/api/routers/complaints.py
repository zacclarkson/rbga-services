"""Anonymous complaints endpoints.

Access levels on purpose:
  * POST  /complaints          — PUBLIC. Anyone can submit. We record only what the
    form sends; the server adds NOTHING identifying (no IP, no user agent).
    Complaints *about the president* are rejected here and redirected to RUSU
    (policy §5 — no impartial internal handler exists above the president).
  * GET   /complaints          — RESTRICTED. Requires the reviewer token.
  * GET   /complaints/{id}      — RESTRICTED. Single complaint (the Discord handler
    fetches the body on demand to show it ephemerally).
  * PATCH /complaints/{id}      — RESTRICTED. Acknowledge / escalate / close per
    the ladder in docs/complaints-policy.md.
  * POST  /complaints/{id}/routed — RESTRICTED. Bookkeeping: the Discord handler
    marks a complaint once it has been posted to its handler tier.

See CLAUDE.md for why the complaints data lives in its own DB schema/credentials
and is handled through Discord (metadata only; the body stays here).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_reviewer
from ..ratelimit import complaints_rate_limit
from ...db.database import get_session
from ...db.models import Complaint, ComplaintCategory, ComplaintStatus, EscalationTarget

router = APIRouter(prefix="/complaints", tags=["complaints"])


class ComplaintIn(BaseModel):
    category: ComplaintCategory
    # Capped so the public endpoint can't be used to dump unbounded data.
    body: str = Field(min_length=1, max_length=5000)
    # Optional; only if the submitter wants a reply. Bound matches the DB column.
    contact: str | None = Field(default=None, max_length=256)


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
    routed_at: datetime | None


class ComplaintUpdate(BaseModel):
    """Reviewer action on a complaint. Both fields optional: send whichever you
    want to change. Setting `escalated_to` implies `status = escalated` unless a
    `status` is given explicitly in the same request."""

    status: ComplaintStatus | None = None
    escalated_to: EscalationTarget | None = None


@router.post(
    "",
    response_model=ComplaintAck,
    status_code=201,
    dependencies=[Depends(complaints_rate_limit)],
)
def submit(data: ComplaintIn, db: Session = Depends(get_session)):
    # A complaint *about the president* has no impartial internal handler, so the
    # club does not take or store it — the submitter is directed to RUSU (§5).
    if data.category == ComplaintCategory.president:
        raise HTTPException(
            400,
            "Complaints about the president are handled independently of the club. "
            "Please contact RUSU Student Rights (https://rusu.rmit.edu.au/studentrights/) "
            "or RMIT Safer Community.",
        )
    complaint = Complaint(category=data.category, body=data.body, contact=data.contact)
    db.add(complaint)
    db.commit()
    db.refresh(complaint)
    return complaint


@router.get("", response_model=list[ComplaintOut], dependencies=[Depends(require_reviewer)])
def list_complaints(db: Session = Depends(get_session)):
    return db.scalars(select(Complaint).order_by(Complaint.created_at.desc())).all()


@router.get(
    "/{complaint_id}",
    response_model=ComplaintOut,
    dependencies=[Depends(require_reviewer)],
)
def get_complaint(complaint_id: int, db: Session = Depends(get_session)):
    complaint = db.get(Complaint, complaint_id)
    if not complaint:
        raise HTTPException(404, "No such complaint")
    return complaint


@router.post(
    "/{complaint_id}/routed",
    response_model=ComplaintOut,
    dependencies=[Depends(require_reviewer)],
)
def mark_routed(complaint_id: int, db: Session = Depends(get_session)):
    """Stamp when the Discord handler has posted this complaint to its tier, so
    the poll loop doesn't post it again. Idempotent — re-marking is a no-op."""
    complaint = db.get(Complaint, complaint_id)
    if not complaint:
        raise HTTPException(404, "No such complaint")
    if complaint.routed_at is None:
        complaint.routed_at = datetime.utcnow()
        db.commit()
        db.refresh(complaint)
    return complaint


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

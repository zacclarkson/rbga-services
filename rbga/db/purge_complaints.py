"""Retention purge for complaints (policy §8).

Deletes complaints that have been *closed* for longer than
COMPLAINTS_RETENTION_DAYS. Meant to run on a schedule (cron on the box):

    docker compose run --rm api python -m rbga.db.purge_complaints

Fail-safe: if COMPLAINTS_RETENTION_DAYS is unset/blank, this does NOTHING — we
never want an unconfigured deploy silently deleting complaint records. The
retention period itself is an exec decision, kept in config not code.

Privacy: this prints only a COUNT, never complaint contents or contact details
(see the no-de-anonymising-logs rule in CLAUDE.md).
"""
import os
from datetime import datetime, timedelta

from sqlalchemy import delete

from .database import SessionLocal
from .models import Complaint, ComplaintStatus


def purge(session, retention_days: int) -> int:
    """Delete complaints closed more than `retention_days` ago. Returns the row
    count deleted. Only touches CLOSED complaints — open/escalated ones are never
    purged regardless of age."""
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    result = session.execute(
        delete(Complaint).where(
            Complaint.status == ComplaintStatus.closed,
            Complaint.closed_at.is_not(None),
            Complaint.closed_at < cutoff,
        )
    )
    session.commit()
    return result.rowcount


def main() -> None:
    raw = os.environ.get("COMPLAINTS_RETENTION_DAYS")
    if not raw:
        print("COMPLAINTS_RETENTION_DAYS unset - retention purge skipped.")
        return

    retention_days = int(raw)
    db = SessionLocal()
    try:
        deleted = purge(db, retention_days)
    finally:
        db.close()
    print(f"Purged {deleted} complaint(s) closed more than {retention_days} day(s) ago.")


if __name__ == "__main__":
    main()

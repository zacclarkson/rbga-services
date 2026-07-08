"""Retention purge for complaints (policy §8).

Deletes complaints at the turn of the calendar year: a complaint closed in a
previous (UTC) year is permanently deleted, so the wipe effectively happens on
1 January. Meant to run on a daily schedule (cron on the box) and is idempotent:
most runs delete nothing, and the first run after the year ticks over does the
purge (self-healing if the box was down on Jan 1):

    docker compose run --rm api python -m rbga.db.purge_complaints

COMPLAINTS_RETENTION_YEARS controls how many year boundaries a closed complaint
survives (1 = deleted at the first New Year after it closes). Fail-safe: if it
is unset/blank, this does NOTHING; we never want an unconfigured deploy
silently deleting complaint records. The retention period itself is an exec
decision, kept in config not code.

Privacy: this prints only a COUNT, never complaint contents or contact details
(see the no-de-anonymising rule in docs/complaints-policy.md).
"""
import os
from datetime import datetime

from sqlalchemy import delete

from .database import SessionLocal
from .models import Complaint, ComplaintStatus


def purge(session, retention_years: int) -> int:
    """Delete complaints closed before 1 January of (current year -
    retention_years + 1). With retention_years=1 that is anything closed in a
    previous calendar year. Returns the row count deleted. Only touches CLOSED
    complaints; open ones are never purged regardless of age."""
    cutoff = datetime(datetime.utcnow().year - retention_years + 1, 1, 1)
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
    raw = os.environ.get("COMPLAINTS_RETENTION_YEARS")
    if not raw:
        if os.environ.get("COMPLAINTS_RETENTION_DAYS"):
            print(
                "COMPLAINTS_RETENTION_DAYS was replaced by "
                "COMPLAINTS_RETENTION_YEARS - retention purge skipped."
            )
        else:
            print("COMPLAINTS_RETENTION_YEARS unset - retention purge skipped.")
        return

    retention_years = int(raw)
    db = SessionLocal()
    try:
        deleted = purge(db, retention_years)
    finally:
        db.close()
    print(
        f"Purged {deleted} complaint(s) closed before the last "
        f"{retention_years} calendar year(s)."
    )


if __name__ == "__main__":
    main()

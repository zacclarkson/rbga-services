"""Retention purge: deletes only complaints closed in a previous calendar
year; unconfigured retention is a no-op."""
from datetime import datetime

from sqlalchemy import select

from rbga.db import purge_complaints
from rbga.db.database import SessionLocal
from rbga.db.models import Complaint, ComplaintCategory, ComplaintStatus


def _count(db) -> int:
    return len(db.scalars(select(Complaint)).all())


def test_purge_deletes_only_closed_in_prior_years():
    this_year = datetime.utcnow().year
    db = SessionLocal()
    try:
        db.add_all(
            [
                Complaint(
                    category=ComplaintCategory.member,
                    body="closed last year",
                    status=ComplaintStatus.closed,
                    closed_at=datetime(this_year - 1, 12, 31, 23, 59),
                ),
                Complaint(
                    category=ComplaintCategory.member,
                    body="closed this year",
                    status=ComplaintStatus.closed,
                    closed_at=datetime(this_year, 1, 1),
                ),
                Complaint(
                    category=ComplaintCategory.member,
                    body="still open from last year (never purged)",
                    status=ComplaintStatus.new,
                    created_at=datetime(this_year - 1, 6, 1),
                ),
            ]
        )
        db.commit()

        deleted = purge_complaints.purge(db, retention_years=1)
        assert deleted == 1
        assert _count(db) == 2  # closed-this-year + open survive
    finally:
        db.close()


def test_main_is_noop_when_retention_unset(monkeypatch, capsys):
    monkeypatch.delenv("COMPLAINTS_RETENTION_YEARS", raising=False)
    monkeypatch.delenv("COMPLAINTS_RETENTION_DAYS", raising=False)

    db = SessionLocal()
    try:
        db.add(
            Complaint(
                category=ComplaintCategory.member,
                body="closed years ago",
                status=ComplaintStatus.closed,
                closed_at=datetime(datetime.utcnow().year - 3, 6, 1),
            )
        )
        db.commit()

        purge_complaints.main()
        assert "skipped" in capsys.readouterr().out
        assert _count(db) == 1  # nothing deleted
    finally:
        db.close()


def test_main_hints_when_only_legacy_days_var_set(monkeypatch, capsys):
    """An un-migrated deploy still setting the old days var must not purge,
    and the log must say the var was renamed."""
    monkeypatch.delenv("COMPLAINTS_RETENTION_YEARS", raising=False)
    monkeypatch.setenv("COMPLAINTS_RETENTION_DAYS", "365")

    db = SessionLocal()
    try:
        db.add(
            Complaint(
                category=ComplaintCategory.member,
                body="closed years ago",
                status=ComplaintStatus.closed,
                closed_at=datetime(datetime.utcnow().year - 3, 6, 1),
            )
        )
        db.commit()

        purge_complaints.main()
        out = capsys.readouterr().out
        assert "COMPLAINTS_RETENTION_YEARS" in out
        assert "skipped" in out
        assert _count(db) == 1  # nothing deleted
    finally:
        db.close()

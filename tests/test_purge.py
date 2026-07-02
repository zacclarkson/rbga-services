"""Retention purge: deletes only complaints closed longer than the retention
window; unconfigured retention is a no-op."""
from datetime import datetime, timedelta

from sqlalchemy import select

from rbga.db import purge_complaints
from rbga.db.database import SessionLocal
from rbga.db.models import Complaint, ComplaintCategory, ComplaintStatus


def _count(db) -> int:
    return len(db.scalars(select(Complaint)).all())


def test_purge_deletes_only_old_closed():
    db = SessionLocal()
    try:
        db.add_all(
            [
                Complaint(
                    category=ComplaintCategory.member,
                    body="old closed",
                    status=ComplaintStatus.closed,
                    closed_at=datetime.utcnow() - timedelta(days=100),
                ),
                Complaint(
                    category=ComplaintCategory.member,
                    body="recently closed",
                    status=ComplaintStatus.closed,
                    closed_at=datetime.utcnow() - timedelta(days=1),
                ),
                Complaint(
                    category=ComplaintCategory.member,
                    body="still open (never purged)",
                    status=ComplaintStatus.new,
                ),
            ]
        )
        db.commit()

        deleted = purge_complaints.purge(db, retention_days=30)
        assert deleted == 1
        assert _count(db) == 2  # recent-closed + open survive
    finally:
        db.close()


def test_main_is_noop_when_retention_unset(monkeypatch, capsys):
    monkeypatch.delenv("COMPLAINTS_RETENTION_DAYS", raising=False)

    db = SessionLocal()
    try:
        db.add(
            Complaint(
                category=ComplaintCategory.member,
                body="old closed",
                status=ComplaintStatus.closed,
                closed_at=datetime.utcnow() - timedelta(days=1000),
            )
        )
        db.commit()

        purge_complaints.main()
        assert "skipped" in capsys.readouterr().out
        assert _count(db) == 1  # nothing deleted
    finally:
        db.close()

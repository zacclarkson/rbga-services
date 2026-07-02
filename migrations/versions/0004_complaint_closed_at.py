"""add closed_at to complaints

Records when a complaint was closed, so the retention purge
(rbga/db/purge_complaints.py, policy §8) can delete closed complaints a
configured number of days after closure rather than after creation.

Revision ID: 0004_complaint_closed_at
Revises: 0003_complaint_escalation
Create Date: 2026-07-02
"""
from alembic import op
import sqlalchemy as sa

from rbga.db.database import COMPLAINTS_SCHEMA

revision = "0004_complaint_closed_at"
down_revision = "0003_complaint_escalation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("complaints", schema=COMPLAINTS_SCHEMA) as batch:
        batch.add_column(sa.Column("closed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("complaints", schema=COMPLAINTS_SCHEMA) as batch:
        batch.drop_column("closed_at")

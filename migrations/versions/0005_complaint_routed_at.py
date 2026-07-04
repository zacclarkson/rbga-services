"""add routed_at to complaints

Marks when the Discord handler has posted a complaint to its handler tier, so the
bot's poll loop doesn't route the same complaint twice.

Revision ID: 0005_complaint_routed_at
Revises: 0004_complaint_closed_at
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

from rbga.db.database import COMPLAINTS_SCHEMA

revision = "0005_complaint_routed_at"
down_revision = "0004_complaint_closed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("complaints", schema=COMPLAINTS_SCHEMA) as batch:
        batch.add_column(sa.Column("routed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("complaints", schema=COMPLAINTS_SCHEMA) as batch:
        batch.drop_column("routed_at")

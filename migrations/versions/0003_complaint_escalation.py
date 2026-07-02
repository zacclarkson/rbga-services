"""add status + escalated_to to complaints

Lifecycle/escalation tracking for the complaints module (see
docs/complaints-policy.md). Two new enum-backed columns on the complaints table:
  * status       — new / acknowledged / escalated / closed (default 'new')
  * escalated_to — committee / exec / president / rusu (nullable)

Revision ID: 0003_complaint_escalation
Revises: 0002_add_price
Create Date: 2026-07-02
"""
from alembic import op
import sqlalchemy as sa

from rbga.db.database import COMPLAINTS_SCHEMA

revision = "0003_complaint_escalation"
down_revision = "0002_add_price"
branch_labels = None
depends_on = None

# create_type=False: we create the PG types explicitly below so batch/ALTER DDL
# doesn't try to CREATE TYPE a second time. On SQLite these render as VARCHAR and
# no type is created regardless.
_status = sa.Enum(
    "new", "acknowledged", "escalated", "closed", name="complaintstatus", create_type=False
)
_target = sa.Enum(
    "committee", "exec", "president", "rusu", name="escalationtarget", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # On Postgres the enum types must exist before columns reference them.
    if is_pg:
        _status.create(bind, checkfirst=True)
        _target.create(bind, checkfirst=True)

    with op.batch_alter_table("complaints", schema=COMPLAINTS_SCHEMA) as batch:
        batch.add_column(
            sa.Column("status", _status, nullable=False, server_default="new")
        )
        batch.add_column(sa.Column("escalated_to", _target, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("complaints", schema=COMPLAINTS_SCHEMA) as batch:
        batch.drop_column("escalated_to")
        batch.drop_column("status")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _target.drop(bind, checkfirst=True)
        _status.drop(bind, checkfirst=True)

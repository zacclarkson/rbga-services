"""add complaints_config (Discord routing targets)

Single-row table holding where complaints are routed in Discord (committee/exec
channel ids + president user id), set at runtime by the /complaints-setup wizard.
Not sensitive, so it lives in the default schema.

Revision ID: 0006_complaints_config
Revises: 0005_complaint_routed_at
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_complaints_config"
down_revision = "0005_complaint_routed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("complaints_config"):
        op.create_table(
            "complaints_config",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("committee_channel_id", sa.String(32), nullable=True),
            sa.Column("exec_channel_id", sa.String(32), nullable=True),
            sa.Column("president_user_id", sa.String(32), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("complaints_config")

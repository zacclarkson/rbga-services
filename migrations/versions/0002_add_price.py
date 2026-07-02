"""add price to board_games

Revision ID: 0002_add_price
Revises: 0001_baseline
Create Date: 2026-07-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_add_price"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("board_games") as batch:
        batch.add_column(sa.Column("price", sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("board_games") as batch:
        batch.drop_column("price")

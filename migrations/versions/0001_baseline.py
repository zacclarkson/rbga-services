"""baseline: keys, board_games (no price), complaints

Idempotent on purpose: this repo adopted Alembic *after* the schema was already
live on the server (172 board_games rows), so baseline must no-op against an
existing DB while still building a fresh one. It creates only the tables that
don't already exist.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-02
"""
from alembic import op
import sqlalchemy as sa

from rbga.db.database import COMPLAINTS_SCHEMA

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    is_pg = bind.dialect.name == "postgresql"

    if not insp.has_table("keys"):
        op.create_table(
            "keys",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("colour", sa.String(64), nullable=False),
            sa.Column("campus", sa.String(64), nullable=False),
            sa.Column("holder", sa.String(128), nullable=True),
            sa.Column("prev_holder", sa.String(128), nullable=True),
            sa.Column("transfer_time", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_keys_colour", "keys", ["colour"], unique=True)

    if not insp.has_table("board_games"):
        op.create_table(
            "board_games",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("publisher", sa.String(200), nullable=True),
            sa.Column("min_players", sa.Integer(), nullable=True),
            sa.Column("max_players", sa.Integer(), nullable=True),
            sa.Column("location", sa.String(128), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("owner", sa.String(128), nullable=True),
            sa.Column("condition", sa.String(64), nullable=True),
            sa.Column("bgg_link", sa.String(512), nullable=True),
            sa.Column("image", sa.String(512), nullable=True),
        )
        op.create_index("ix_board_games_title", "board_games", ["title"])

    # Complaints live in their own schema on Postgres (None on SQLite dev).
    if is_pg and COMPLAINTS_SCHEMA:
        op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{COMPLAINTS_SCHEMA}"'))
    if not insp.has_table("complaints", schema=COMPLAINTS_SCHEMA):
        op.create_table(
            "complaints",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column(
                "category",
                sa.Enum("member", "committee", "exec", "president", name="complaintcategory"),
                nullable=False,
            ),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("contact", sa.String(256), nullable=True),
            schema=COMPLAINTS_SCHEMA,
        )
        op.create_index(
            "ix_complaints_created_at", "complaints", ["created_at"], schema=COMPLAINTS_SCHEMA
        )


def downgrade() -> None:
    op.drop_table("complaints", schema=COMPLAINTS_SCHEMA)
    op.drop_table("board_games")
    op.drop_table("keys")

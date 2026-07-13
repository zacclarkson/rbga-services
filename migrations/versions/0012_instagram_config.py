"""add instagram_config (Graph API token storage)

Single-row table holding the Instagram Graph API access token for the
announcement-to-story feature. Long-lived tokens expire every 60 days and the
bot refreshes them in place, so the current token lives here (seeded from
IG_ACCESS_TOKEN) rather than in .env. Default public schema: the bot accesses
it directly, and rbga_bot is granted new public tables automatically by the
default privileges set in migration 0011.

Revision ID: 0012_instagram_config
Revises: 0011_grant_owners_to_bot
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_instagram_config"
down_revision = "0011_grant_owners_to_bot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("instagram_config"):
        op.create_table(
            "instagram_config",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("access_token", sa.Text(), nullable=True),
            sa.Column("token_refreshed_at", sa.DateTime(), nullable=True),
            sa.Column("env_seed", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("instagram_config")

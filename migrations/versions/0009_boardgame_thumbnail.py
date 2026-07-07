"""add thumbnail to board_games

BGG's full image is often a multi-MB original; Discord's media proxy times
out fetching ten of them per gallery page and thumbnails render blank. Store
BGG's small thumbnail variant separately and use it for embed thumbnails.

Revision ID: 0009_boardgame_thumbnail
Revises: 0008_boardgame_tags
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_boardgame_thumbnail"
down_revision = "0008_boardgame_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("board_games") as batch:
        batch.add_column(sa.Column("thumbnail", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("board_games") as batch:
        batch.drop_column("thumbnail")

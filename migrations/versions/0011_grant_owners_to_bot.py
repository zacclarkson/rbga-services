"""grant owners to rbga_bot + default privileges for future tables

Migration 0010 created the `owners` table, but the bot connects as the
least-privilege `rbga_bot` role whose grants were issued before that table
existed — so every /owner command died with "permission denied for table
owners". Grant the table (and its id sequence), and set default privileges
on the public schema so tables added by future migrations are granted
automatically instead of repeating this failure. Complaints isolation is
unaffected: it is schema-based (REVOKE ON SCHEMA complaints), and these
defaults are scoped to public only.

Revision ID: 0011_grant_owners_to_bot
Revises: 0010_owners_stocktake_sell
Create Date: 2026-07-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_grant_owners_to_bot"
down_revision = "0010_owners_stocktake_sell"
branch_labels = None
depends_on = None

BOT_ROLE = "rbga_bot"


def _bot_role_exists(bind) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": BOT_ROLE}
        ).scalar()
    )


def upgrade() -> None:
    bind = op.get_bind()
    # Grants are a Postgres concept; SQLite dev/tests have a single user.
    if bind.dialect.name != "postgresql":
        return
    # A dev Postgres without the bot role shouldn't fail the migration.
    if not _bot_role_exists(bind):
        return
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON owners TO {BOT_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE owners_id_seq TO {BOT_ROLE}")
    # Future tables/sequences in public (created by this migration-running
    # role) are granted automatically; the complaints schema stays revoked.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {BOT_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {BOT_ROLE}"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not _bot_role_exists(bind):
        return
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {BOT_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {BOT_ROLE}"
    )
    op.execute(f"REVOKE USAGE, SELECT ON SEQUENCE owners_id_seq FROM {BOT_ROLE}")
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON owners FROM {BOT_ROLE}")

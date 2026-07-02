"""Alembic environment — wired to the app's own Base/metadata and DATABASE_URL.

Online-only (we always have a live connection in dev and deploy). Uses batch mode
so ALTERs work on SQLite dev, and includes schemas so the isolated `complaints`
schema is handled on Postgres.
"""
from logging.config import fileConfig

from alembic import context

from rbga.db.database import DATABASE_URL, engine
from rbga.db.models import Base  # noqa: F401 — imports all models onto Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
            include_schemas=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if DATABASE_URL:  # always set (defaults to SQLite in database.py)
    run_migrations_online()

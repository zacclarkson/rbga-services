"""Shared database layer — one engine/session used by both the API and the bot.

Reads DATABASE_URL from the environment so the same code runs against local
SQLite in dev and Postgres on the server (see CLAUDE.md). Nothing here is
service-specific: the API modules and the Discord bot both import from here.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# Default to a throwaway local SQLite file so `git clone && run` works with no
# setup. The server / compose stack overrides this with a Postgres URL.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./rbga_dev.db")

# Complaints get their own Postgres schema (and, in production, their own DB
# credentials) so a leak of the bot/board-games creds never exposes them.
# Left unset in SQLite dev (None == default schema) so local runs still work.
COMPLAINTS_SCHEMA = os.environ.get("COMPLAINTS_SCHEMA") or None

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_session():
    """FastAPI dependency — yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

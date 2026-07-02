"""Test fixtures.

The shared engine (rbga/db/database.py) and the auth tokens (rbga/api/auth.py)
are read from the environment at *import* time, so we set DATABASE_URL to a
throwaway SQLite file BEFORE importing anything from rbga. Tokens are injected
per-test by monkeypatching the auth module globals.
"""
import os
import tempfile
from pathlib import Path

import pytest

_DB_FILE = Path(tempfile.gettempdir()) / "rbga_pytest.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE.as_posix()}"
os.environ.pop("COMPLAINTS_SCHEMA", None)  # default schema on SQLite

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import rbga.api.auth as auth  # noqa: E402
from rbga.api.main import app  # noqa: E402
from rbga.db.database import engine  # noqa: E402

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


@pytest.fixture(autouse=True)
def fresh_db():
    """Rebuild the schema from migrations before each test (faithful to prod),
    and dispose/delete after so tests don't leak state into each other."""
    _DB_FILE.unlink(missing_ok=True)
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")
    yield
    engine.dispose()
    _DB_FILE.unlink(missing_ok=True)


@pytest.fixture
def client():
    # No `with` — we don't want the app lifespan (it would re-run migrations);
    # the fresh_db fixture owns schema setup.
    return TestClient(app)


@pytest.fixture
def write_token(monkeypatch):
    monkeypatch.setattr(auth, "_WRITE_TOKEN", "write-secret")
    return "write-secret"


@pytest.fixture
def reviewer_token(monkeypatch):
    monkeypatch.setattr(auth, "_REVIEWER_TOKEN", "review-secret")
    return "review-secret"

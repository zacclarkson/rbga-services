"""FastAPI app — the HTTP half of the monolith.

One process, three feature modules (keys, board games, complaints) mounted as
routers. The Discord bot is a *separate* process (different runtime shape — a
long-running gateway connection) but shares the same image and db layer.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from ..db.database import COMPLAINTS_SCHEMA, Base, engine
from .routers import boardgames, complaints, keys


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev convenience only: auto-create tables on startup. In production this
    # should be replaced by Alembic migrations (not yet added — see CLAUDE.md).
    # Complaints live in their own Postgres schema (CLAUDE.md); create_all won't
    # create the schema itself, so ensure it exists first. None on SQLite dev,
    # where schemas don't apply.
    if COMPLAINTS_SCHEMA and engine.dialect.name != "sqlite":
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{COMPLAINTS_SCHEMA}"'))
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="RBGA Services", lifespan=lifespan)
app.include_router(keys.router)
app.include_router(boardgames.router)
app.include_router(complaints.router)


@app.get("/health")
def health():
    return {"status": "ok"}

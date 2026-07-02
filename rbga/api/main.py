"""FastAPI app — the HTTP half of the monolith.

One process, three feature modules (keys, board games, complaints) mounted as
routers. The Discord bot is a *separate* process (different runtime shape — a
long-running gateway connection) but shares the same image and db layer.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..db.database import Base, engine
from .routers import boardgames, complaints, keys


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev convenience only: auto-create tables on startup. In production this
    # should be replaced by Alembic migrations (not yet added — see CLAUDE.md).
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="RBGA Services", lifespan=lifespan)
app.include_router(keys.router)
app.include_router(boardgames.router)
app.include_router(complaints.router)


@app.get("/health")
def health():
    return {"status": "ok"}

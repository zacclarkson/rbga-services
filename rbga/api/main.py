"""FastAPI app — the HTTP half of the monolith.

One process, three feature modules (keys, board games, complaints) mounted as
routers. The Discord bot is a *separate* process (different runtime shape — a
long-running gateway connection) but shares the same image and db layer.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import boardgames, complaints, keys

# alembic.ini lives at the repo/image root (two levels up from this file).
_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring the schema up to date on startup (Alembic). Keeps local `uvicorn`
    # zero-setup and is idempotent, so it's a no-op once already migrated. The
    # deploy also runs `alembic upgrade head` before services start.
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")
    yield


app = FastAPI(title="RBGA Services", lifespan=lifespan)

# The (future) complaints form on GitHub Pages POSTs cross-origin. Allow only
# the origins named in CORS_ALLOW_ORIGINS (comma-separated); default empty means
# no cross-origin requests are permitted — safe until the Pages origin is known.
_cors_origins = [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(keys.router)
app.include_router(boardgames.router)
app.include_router(complaints.router)


@app.get("/health")
def health():
    return {"status": "ok"}

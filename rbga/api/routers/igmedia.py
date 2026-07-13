"""Transient public hosting for Instagram-story images.

Instagram's publishing API doesn't take an upload; it *fetches* the image from
a public URL. The bot renders a story card (or proxies a Discord attachment),
POSTs the bytes here, and hands the resulting public URL to Instagram. Files
are throwaway: unguessable uuid4 names, deleted on a best-effort sweep after
MAX_AGE (Instagram fetches within seconds of publish). The content is a club
announcement — already public — so an unauthenticated GET on an unguessable
name is fine; only the *upload* is gated (write token), so outsiders can't use
the API as free image hosting.
"""
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..auth import require_api_token

router = APIRouter(prefix="/ig-media", tags=["instagram"])

# Container-local scratch space; losing it on redeploy is harmless (files
# only need to outlive one Instagram fetch).
MEDIA_DIR = Path(os.environ.get("IG_MEDIA_DIR", "data/ig_media"))
MAX_AGE_SECONDS = 3600
MAX_BYTES = 8 * 1024 * 1024  # Instagram's own story-image cap
_TYPES = {"image/png": ".png", "image/jpeg": ".jpg"}


def _sweep() -> None:
    """Best-effort delete of files past MAX_AGE; runs on each upload so the
    dir can't grow unbounded without needing a scheduler."""
    now = time.time()
    if not MEDIA_DIR.is_dir():
        return
    for f in MEDIA_DIR.iterdir():
        try:
            if now - f.stat().st_mtime > MAX_AGE_SECONDS:
                f.unlink()
        except OSError:
            pass  # raced with another delete, or transient fs error


@router.post("", dependencies=[Depends(require_api_token)])
async def upload(request: Request):
    ext = _TYPES.get(request.headers.get("content-type", "").split(";")[0].strip())
    if not ext:
        raise HTTPException(415, "Send image/png or image/jpeg")
    body = await request.body()
    if not body:
        raise HTTPException(400, "Empty body")
    if len(body) > MAX_BYTES:
        raise HTTPException(413, f"Image exceeds {MAX_BYTES} bytes")
    _sweep()
    name = f"{uuid.uuid4().hex}{ext}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(body)
    return {"name": name, "path": f"/ig-media/{name}"}


@router.get("/{name}")
def serve(name: str):
    # Only names we could have generated: 32 hex chars + a known extension.
    # Rejects traversal and probing without touching the filesystem.
    stem, _, ext = name.partition(".")
    if len(stem) != 32 or not all(c in "0123456789abcdef" for c in stem):
        raise HTTPException(404, "No such media")
    media_type = {v.lstrip("."): k for k, v in _TYPES.items()}.get(ext)
    if not media_type:
        raise HTTPException(404, "No such media")
    path = MEDIA_DIR / name
    if not path.is_file():
        raise HTTPException(404, "No such media")
    return FileResponse(path, media_type=media_type)

"""HTTP auth for the API — all guards live here so there's one place to reason
about who can do what.

Two independent, fail-closed token checks (an unset token rejects everything):

  * require_api_token — gates the REST *writes* on keys/board-games
    (X-API-Token / RBGA_API_TOKEN). Reads stay open.
  * require_reviewer  — gates reading and managing complaints
    (X-Reviewer-Token / COMPLAINTS_API_TOKEN). Deliberately a *separate*
    credential and header from the general write token, so a leak of the
    board-games/keys token can't touch complaints.

Both apply via `dependencies=[Depends(...)]` on the routes that need them.
"""
import os

from fastapi import Header, HTTPException

_WRITE_TOKEN = os.environ.get("RBGA_API_TOKEN")
_REVIEWER_TOKEN = os.environ.get("COMPLAINTS_API_TOKEN")


def require_api_token(x_api_token: str | None = Header(default=None)):
    # Fail closed: no token configured -> all writes rejected.
    if not _WRITE_TOKEN or x_api_token != _WRITE_TOKEN:
        raise HTTPException(403, "Not authorised to modify this resource")


def require_reviewer(x_reviewer_token: str | None = Header(default=None)):
    # Fail closed: no token configured -> nobody can read/manage complaints.
    if not _REVIEWER_TOKEN or x_reviewer_token != _REVIEWER_TOKEN:
        raise HTTPException(403, "Not authorised to read complaints")

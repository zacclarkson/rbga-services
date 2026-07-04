"""A small in-memory rate limiter for the public complaints endpoint.

Design constraint: the complaints privacy rules forbid *storing or logging* any
de-anonymising data, including IPs. So we never keep the raw client IP — the
limiter key is a SHA-256 of (per-process random salt + IP). The salt is generated
fresh at process start and never persisted, so the in-memory map can't be
reversed back to IPs, and nothing is ever written to a log or the DB. The IP is
touched only transiently to compute that hash.

Fixed-window counter, keyed per hashed-IP. This assumes a single API worker (one
uvicorn process, as in compose) — a multi-worker/replicated deployment would need
a shared store (e.g. Redis) instead.
"""
import hashlib
import os
import secrets
import time

from fastapi import Header, HTTPException, Request

from . import auth

# Per-process salt — makes the stored hashes non-reversible and unlinkable across
# restarts. Never logged, never persisted.
_SALT = secrets.token_bytes(16)


def _parse_limit(raw: str | None) -> tuple[int, float]:
    """Parse "<count>/<seconds>" (e.g. "5/60"). Defaults to 5 per 60s."""
    if not raw:
        return 5, 60.0
    count, _, window = raw.partition("/")
    return int(count), float(window or 60)


_LIMIT, _WINDOW = _parse_limit(os.environ.get("COMPLAINTS_RATE_LIMIT"))

# key -> (window_start_epoch, count_in_window)
_hits: dict[str, tuple[float, int]] = {}


def _client_ip(request: Request) -> str:
    # Behind cloudflared the real client IP is in CF-Connecting-IP; fall back to
    # the socket peer for direct/local requests.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


def _key(request: Request) -> str:
    ip = _client_ip(request)
    return hashlib.sha256(_SALT + ip.encode("utf-8")).hexdigest()


def _reset() -> None:
    """Clear all counters (used by tests)."""
    _hits.clear()


def _enforce(request: Request) -> None:
    """Fixed-window count; raise 429 over quota."""
    now = time.time()
    key = _key(request)

    window_start, count = _hits.get(key, (now, 0))
    if now - window_start >= _WINDOW:
        window_start, count = now, 0  # window rolled over

    count += 1
    _hits[key] = (window_start, count)

    # Opportunistic prune so the map can't grow without bound.
    if len(_hits) > 10_000:
        for k, (ws, _) in list(_hits.items()):
            if now - ws >= _WINDOW:
                _hits.pop(k, None)

    if count > _LIMIT:
        raise HTTPException(429, "Too many submissions, please try again later.")


def complaints_rate_limit(
    request: Request, x_reviewer_token: str | None = Header(default=None)
) -> None:
    """FastAPI dependency for POST /complaints. Trusted callers holding the
    reviewer token (the bot forwarding Discord submissions) are exempt — the
    public throttle is only for anonymous/web submissions."""
    if auth.is_reviewer(x_reviewer_token):
        return
    _enforce(request)

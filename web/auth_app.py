"""Dashboard login: password hashing, cookie sessions, and the HTTP
auth gate shared by the api and portfolio FastAPI apps.

Distinct from the `web/auth/` package, which handles *Schwab* OAuth.
This module is about logging a human into the dashboard itself.

Design notes
------------
- Passwords hashed with stdlib ``hashlib.pbkdf2_hmac`` (no extra deps).
  Stored as ``pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>``.
- Sessions live in the shared SQLite ``sessions`` table, so a cookie
  minted by the api container validates on the portfolio container too.
- Service-to-service calls (scheduler -> portfolio/api) carry
  ``X-Internal-Token: $INTERNAL_API_TOKEN`` and bypass the cookie check.
- First-run: when the ``users`` table is empty, ``/api/auth/me`` reports
  ``setup_required`` and ``/api/auth/setup`` creates the first admin.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse

from . import db

COOKIE_NAME = "ta_session"
SESSION_TTL_DAYS = 30
_PBKDF2_ITERS = 600_000

# Paths under /api that do NOT require a session cookie.
PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/me",
    "/api/auth/login",
    "/api/auth/setup",
    "/api/auth/schwab/callback",  # Schwab OAuth redirect; guarded by its own state nonce
}


# ---------- password hashing ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


# ---------- sessions ----------

def new_session(username: str) -> tuple[str, str]:
    """Create a session row and return (token, expires_at_iso)."""
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    db.create_session(token, username, expires_at)
    return token, expires_at


def _internal_token() -> str | None:
    return os.environ.get("INTERNAL_API_TOKEN")


def is_authorized(request: Request) -> bool:
    """True if the request carries a valid session cookie OR the internal token."""
    internal = _internal_token()
    if internal:
        hdr = request.headers.get("x-internal-token")
        if hdr and hmac.compare_digest(hdr, internal):
            return True
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return db.get_session(token) is not None


def _is_public(path: str) -> bool:
    if path in PUBLIC_API_PATHS:
        return True
    # Everything not under /api/ is static (served by nginx in prod; in
    # dev it's harmless) and never gated here.
    return not path.startswith("/api/")


async def auth_middleware(request: Request, call_next):
    """ASGI middleware enforcing login on all /api/ routes except the allowlist."""
    if _is_public(request.url.path) or is_authorized(request):
        return await call_next(request)
    return JSONResponse({"detail": "authentication required"}, status_code=401)


def set_session_cookie(response, token: str) -> None:
    # SameSite=strict: the dashboard only ever uses this cookie on same-origin
    # fetch/XHR calls, never on a cross-site top-level navigation, so strict
    # adds CSRF defense with no UX cost here. (The Schwab OAuth return is a
    # separate, public callback that doesn't read this cookie.)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="strict",
        secure=True,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_username(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    sess = db.get_session(token)
    return sess["username"] if sess else None

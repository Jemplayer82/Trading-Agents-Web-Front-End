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
- The gate is FAIL-CLOSED: if ``INTERNAL_API_TOKEN`` is unset, the
  internal-token branch is skipped entirely and a valid session is still
  required. Missing config never grants anonymous access.
- All token/hash comparisons use ``hmac.compare_digest``, never ``==``
  (timing side-channel — see the inline notes in ``is_authorized``).
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

# Brute-force throttling for /api/auth/login. Failures are recorded per
# username AND per client IP; crossing either threshold inside the sliding
# window locks that key out with 429 until old attempts age past the window.
# The per-IP limit is deliberately looser: it exists to slow username
# spraying, not to lock out a whole NAT because one account was targeted.
LOCKOUT_WINDOW_MINUTES = 15
LOCKOUT_MAX_PER_USER = 5
LOCKOUT_MAX_PER_IP = 20

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
    """PBKDF2-HMAC-SHA256 with a fresh 16-byte salt and 600k iterations.

    The output embeds algorithm/iterations/salt, so ``_PBKDF2_ITERS`` can be
    raised later without invalidating existing rows — ``verify_password``
    reads the parameters back from each stored hash.
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2_sha256${_PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash. Malformed rows verify False.

    Iterations and salt come from the stored string, not the module constant,
    so hashes minted under older settings keep verifying. The final compare is
    ``hmac.compare_digest`` — keep it timing-safe.
    """
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
    # Fail-closed: if INTERNAL_API_TOKEN is unset this branch is skipped and the
    # request still needs a valid session. Unset config must never open a hole.
    if internal:
        hdr = request.headers.get("x-internal-token")
        # compare_digest, never ==: string equality short-circuits and leaks a
        # timing side-channel. That exact regression shipped once — don't repeat it.
        if hdr and hmac.compare_digest(hdr, internal):
            return True
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return db.get_session(token) is not None


def client_ip(request: Request) -> str:
    """Client address for login throttling.

    X-Real-IP is written by our nginx from ``$remote_addr`` on every proxied
    request, so a browser can't forge it (unlike the first hop of
    X-Forwarded-For, which the client controls). Direct hits — tests, the
    internal Docker network — fall back to the socket peer.
    """
    return request.headers.get("x-real-ip") or (
        request.client.host if request.client else "unknown"
    )


def is_login_locked(username: str, ip: str) -> bool:
    """True when this username or source address is inside a lockout.

    Checked BEFORE password verification so a locked-out attacker gets a
    cheap 429 instead of burning a PBKDF2 round per guess.
    """
    since = (datetime.utcnow() - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).isoformat()
    if db.count_failed_logins_for_user(username, since) >= LOCKOUT_MAX_PER_USER:
        return True
    return db.count_failed_logins_for_ip(ip, since) >= LOCKOUT_MAX_PER_IP


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

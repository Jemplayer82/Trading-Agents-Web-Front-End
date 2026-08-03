"""Schwab OAuth routes — T2 (Schwab brokerage) only.

Split out of web/main.py so the api app stays byte-identical across every
tier branch; main.py mounts this router only when features.enabled("schwab")
is true. Moved verbatim — the state-cookie CSRF gate and its path/samesite/
secure attributes are load-bearing, do not simplify them in isolation.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import schwab as schwab_auth
from .auth import token_store

log = logging.getLogger(__name__)

router = APIRouter()

# Short-lived cookie carrying the OAuth anti-CSRF state nonce between the
# /api/auth/schwab redirect and the Schwab callback. SameSite=lax (not strict)
# so it survives the top-level cross-site redirect back from Schwab.
_SCHWAB_STATE_COOKIE = "schwab_oauth_state"


@router.get("/api/auth/schwab")
def schwab_login() -> RedirectResponse:
    """Start the Schwab OAuth flow: mint an anti-CSRF state nonce, stash it in
    the short-lived cookie above, and redirect to Schwab's authorize page."""
    state = secrets.token_urlsafe(32)
    resp = RedirectResponse(url=schwab_auth.build_auth_url(state), status_code=302)
    resp.set_cookie(
        _SCHWAB_STATE_COOKIE, state,
        max_age=600, httponly=True, samesite="lax", secure=True, path="/api/auth/schwab",
    )
    return resp


@router.get("/api/auth/schwab/callback")
def schwab_callback(
    request: Request, code: str | None = None, error: str | None = None, state: str | None = None
) -> HTMLResponse:
    """Schwab OAuth redirect target (public — listed in auth_app.PUBLIC_API_PATHS;
    the state nonce is its own gate).

    Every failure branch deliberately returns a generic message to the browser
    and logs the specifics server-side, so upstream error details are never
    reflected to whoever drove the redirect.
    """
    # Verify the anti-CSRF state matches the nonce we set when starting the flow.
    expected_state = request.cookies.get(_SCHWAB_STATE_COOKIE)
    if not expected_state or not state or not hmac.compare_digest(state, expected_state):
        log.warning("Schwab callback rejected: missing or mismatched OAuth state")
        return HTMLResponse("<h1>Invalid or expired authorization request</h1>", status_code=400)
    if error:
        # Don't echo the raw upstream error back to the browser; log it instead.
        log.warning("Schwab auth returned error: %s", error)
        return HTMLResponse("<h1>Schwab authorization failed</h1>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Missing ?code= parameter</h1>", status_code=400)
    try:
        bundle = schwab_auth.exchange_code(code)
        token_store.save(bundle)
    except Exception:
        log.exception("Schwab code exchange failed")
        return HTMLResponse("<h1>Authorization failed. Please try again.</h1>", status_code=500)
    resp = HTMLResponse(
        """
        <html><body style='background:#0b0f14;color:#d6e1ea;font-family:monospace;padding:48px;text-align:center;'>
          <h2 style='color:#7be38c;'>✅ Schwab connected.</h2>
          <p>You can close this tab. The dashboard now has access.</p>
          <script>setTimeout(() => window.close(), 1500);</script>
        </body></html>
        """.strip()
    )
    # One-time nonce; drop it now that the flow is complete.
    resp.delete_cookie(_SCHWAB_STATE_COOKIE, path="/api/auth/schwab")
    return resp


@router.get("/api/auth/schwab/status")
def schwab_status() -> dict[str, Any]:
    """Schwab connectivity via the MCP server. `enabled` is the master switch;
    `connected` reflects whether the MCP's Schwab session currently returns data."""
    from tradingagents.dataflows import schwab_mcp
    if not schwab_mcp.schwab_enabled():
        return {"enabled": False, "connected": False, "source": "mcp"}
    accounts = None
    try:
        accounts = schwab_mcp.get_accounts(fields="positions")
    except Exception:
        log.debug("[schwab_status] MCP read failed", exc_info=True)
    return {
        "enabled": True,
        "connected": bool(accounts),
        "num_accounts": len(accounts) if isinstance(accounts, list) else 0,
        "source": "mcp",
    }


@router.delete("/api/auth/schwab")
def schwab_disconnect() -> dict[str, str]:
    token_store.clear()
    return {"status": "disconnected"}

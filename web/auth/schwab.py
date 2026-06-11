"""Schwab OAuth flow + token endpoint client.

Schwab refresh tokens are valid for 7 days. Access tokens last 30 min.
We encode that lifecycle into TokenBundle and surface a `refresh_days_remaining`
helper that the scheduler uses for the hourly health check.
"""
from __future__ import annotations

import os
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from .token_store import TokenBundle

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

ACCESS_TTL = 1800           # 30 min
REFRESH_TTL = 7 * 24 * 3600 # 7 days


def _basic_auth_header() -> dict[str, str]:
    key = os.environ["SCHWAB_APP_KEY"]
    secret = os.environ["SCHWAB_APP_SECRET"]
    creds = b64encode(f"{key}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def build_auth_url(state: str) -> str:
    """Build the Schwab authorization URL.

    `state` is an opaque anti-CSRF nonce the caller also stores client-side (a
    cookie) and re-checks on the callback, so an attacker can't forge a callback
    that links their Schwab account to a victim's dashboard session.
    """
    params = {
        "response_type": "code",
        "client_id": os.environ["SCHWAB_APP_KEY"],
        "redirect_uri": os.environ["SCHWAB_CALLBACK_URL"],
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def exchange_code(code: str) -> TokenBundle:
    """Trade an authorization code for an initial access + refresh token pair."""
    redirect_uri = os.environ["SCHWAB_CALLBACK_URL"]
    headers = _basic_auth_header() | {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    with httpx.Client(timeout=20) as client:
        r = client.post(TOKEN_URL, headers=headers, data=data)
        r.raise_for_status()
        payload = r.json()
    now = _now()
    return TokenBundle(
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=_iso(now + timedelta(seconds=payload.get("expires_in", ACCESS_TTL))),
        refresh_issued_at=_iso(now),
    )


def refresh(bundle: TokenBundle) -> TokenBundle:
    """Use the refresh token to mint a fresh access token.

    Schwab usually returns just a new access token; if a fresh refresh_token
    comes back too, swap it in and reset refresh_issued_at.
    """
    headers = _basic_auth_header() | {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"grant_type": "refresh_token", "refresh_token": bundle.refresh_token}
    with httpx.Client(timeout=20) as client:
        r = client.post(TOKEN_URL, headers=headers, data=data)
        r.raise_for_status()
        payload = r.json()
    now = _now()
    new_refresh = payload.get("refresh_token") or bundle.refresh_token
    issued = _iso(now) if payload.get("refresh_token") else bundle.refresh_issued_at
    return TokenBundle(
        access_token=payload["access_token"],
        refresh_token=new_refresh,
        expires_at=_iso(now + timedelta(seconds=payload.get("expires_in", ACCESS_TTL))),
        refresh_issued_at=issued,
    )


def refresh_days_remaining(bundle: TokenBundle) -> int:
    try:
        issued = datetime.fromisoformat(bundle.refresh_issued_at)
    except (TypeError, ValueError):
        return 0
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    expires = issued + timedelta(seconds=REFRESH_TTL)
    delta = expires - _now()
    return max(0, int(delta.total_seconds() // 86400))

"""Schwab Trader API client — read positions, auto-refresh access tokens."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from . import schwab, token_store
from .token_store import TokenBundle

log = logging.getLogger(__name__)

BASE_URL = "https://api.schwabapi.com/trader/v1"
REFRESH_LEEWAY_SEC = 60


class SchwabError(Exception):
    pass


@dataclass
class Position:
    symbol: str
    quantity: float
    asset_type: str
    market_value: float = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_fresh(bundle: TokenBundle) -> TokenBundle:
    try:
        expires = datetime.fromisoformat(bundle.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        expires = _now()
    if expires - _now() <= timedelta(seconds=REFRESH_LEEWAY_SEC):
        log.info("Schwab access token near expiry — refreshing")
        new_bundle = schwab.refresh(bundle)
        token_store.save(new_bundle)
        return new_bundle
    return bundle


def _client(bundle: TokenBundle) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={
            "Authorization": f"Bearer {bundle.access_token}",
            "Accept": "application/json",
        },
        timeout=30,
    )


def _loaded(bundle: Optional[TokenBundle]) -> TokenBundle:
    bundle = bundle or token_store.load()
    if not bundle:
        raise SchwabError("not connected — no Schwab tokens on disk")
    return _ensure_fresh(bundle)


def get_account_numbers(bundle: Optional[TokenBundle] = None) -> tuple[list[dict], TokenBundle]:
    bundle = _loaded(bundle)
    with _client(bundle) as c:
        r = c.get("/accounts/accountNumbers")
        r.raise_for_status()
        return r.json(), bundle


def get_positions(account_hash: str, bundle: Optional[TokenBundle] = None) -> tuple[list[Position], TokenBundle]:
    bundle = _loaded(bundle)
    with _client(bundle) as c:
        r = c.get(f"/accounts/{account_hash}", params={"fields": "positions"})
        r.raise_for_status()
        data = r.json()
    sec_acct = data.get("securitiesAccount") or data
    raw = sec_acct.get("positions") if isinstance(sec_acct, dict) else None
    positions: list[Position] = []
    if not raw:
        return positions, bundle
    for p in raw:
        inst = p.get("instrument") or {}
        sym = inst.get("symbol")
        if not sym:
            continue
        qty = float(p.get("longQuantity", 0)) - float(p.get("shortQuantity", 0))
        if qty == 0:
            continue
        positions.append(Position(
            symbol=sym,
            quantity=qty,
            asset_type=inst.get("assetType", "EQUITY"),
            market_value=float(p.get("marketValue", 0)),
        ))
    return positions, bundle

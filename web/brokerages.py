"""Brokerage provider abstraction.

All brokerage-specific account/position parsing lives here, normalized to one
shape the rest of the app consumes. Schwab (via its MCP server) is the only
live provider; adding another brokerage means writing one BrokerageProvider
subclass and appending it to _PROVIDERS.

Normalized position dict:
    symbol           raw brokerage symbol (OCC string for options)
    display_symbol   "AAPL 150C" for options, == symbol otherwise
    shares           contracts for options
    average_price    per-share (per-share premium for options)
    current_price    market_value / (shares * multiplier)
    market_value, cost_basis, gain_dollars, gain_percent
    asset_type       "EQUITY" | "OPTION" | ...
    multiplier       100 for options, 1 otherwise
    expiration_date  "YYYY-MM-DD" or None (option-only)
    strike, put_call, underlying    None for non-options

Normalized account dict:
    id          "<provider>:<account_number>" (namespaced so brokerages can't collide)
    brokerage   provider name, e.g. "schwab"
    label       "SCHWAB ••1234"
    positions, total_value, cash, cost_basis, gain_dollars, gain_percent
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from tradingagents.dataflows import schwab_mcp

log = logging.getLogger(__name__)

OPTION_MULTIPLIER = 100

_OCC_TAIL = re.compile(r"\d{6}[CP]\d{8}")


def parse_occ_symbol(symbol: str) -> dict[str, Any] | None:
    """Parse an OCC option symbol, e.g. 'AAPL  250117C00150000'.

    Fixed-tail parse: the last 15 chars are YYMMDD + C/P + 8-digit strike*1000;
    everything before (stripped of padding) is the underlying root. Returns
    {underlying, expiration_date "YYYY-MM-DD", put_call "CALL"/"PUT", strike}
    or None if the string isn't an OCC symbol.
    """
    s = (symbol or "").rstrip()
    if len(s) < 16:
        return None
    tail = s[-15:]
    if not _OCC_TAIL.fullmatch(tail):
        return None
    root = s[:-15].rstrip()
    if not root:
        return None
    try:
        exp = datetime.strptime(tail[:6], "%y%m%d").date()
    except ValueError:
        return None
    return {
        "underlying": root,
        "expiration_date": exp.isoformat(),
        "put_call": "CALL" if tail[6] == "C" else "PUT",
        "strike": int(tail[7:]) / 1000.0,
    }


class BrokerageProvider(ABC):
    """One brokerage backend. fetch_accounts returns normalized account dicts."""

    name: str = "base"

    @abstractmethod
    def enabled(self) -> bool: ...

    @abstractmethod
    def fetch_accounts(self) -> list[dict[str, Any]]:
        """Normalized accounts, or [] when unreachable / unauthed."""


class SchwabProvider(BrokerageProvider):
    name = "schwab"

    def enabled(self) -> bool:
        return schwab_mcp.schwab_enabled()

    def fetch_accounts(self) -> list[dict[str, Any]]:
        accounts = schwab_mcp.get_accounts(fields="positions")
        if not accounts:
            return []
        out: list[dict[str, Any]] = []
        for a in accounts:
            sec = a.get("securitiesAccount") or a
            account_num = str(sec.get("accountNumber") or "")
            label = f"SCHWAB ••{account_num[-4:]}" if len(account_num) >= 4 else "SCHWAB"

            positions: list[dict[str, Any]] = []
            acct_cost = 0.0
            acct_mv = 0.0

            for p in sec.get("positions") or []:
                instr = p.get("instrument") or {}
                sym = instr.get("symbol")
                shares = float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)
                if not sym or shares == 0:
                    continue
                pos = self._normalize_position(instr, sym, shares, p)
                positions.append(pos)
                acct_cost += pos["cost_basis"]
                acct_mv += pos["market_value"]

            positions.sort(key=lambda x: -x["market_value"])
            cur = sec.get("currentBalances") or {}
            init = sec.get("initialBalances") or {}
            tv = (cur.get("liquidationValue") or cur.get("equity")
                  or init.get("liquidationValue") or init.get("accountValue") or acct_mv)
            cash = cur.get("cashBalance") or init.get("cashBalance") or init.get("totalCash") or 0
            acct_gain = acct_mv - acct_cost
            acct_gain_pct = (acct_gain / acct_cost * 100) if acct_cost else 0.0

            out.append({
                "id": f"{self.name}:{account_num}",
                "brokerage": self.name,
                "label": label,
                "positions": positions,
                "total_value": round(float(tv), 2),
                "cash": round(float(cash), 2),
                "cost_basis": round(acct_cost, 2),
                "gain_dollars": round(acct_gain, 2),
                "gain_percent": round(acct_gain_pct, 4),
            })
        return out

    def _normalize_position(
        self, instr: dict[str, Any], sym: str, shares: float, p: dict[str, Any]
    ) -> dict[str, Any]:
        asset_type = instr.get("assetType") or "EQUITY"
        avg_price = float(p.get("averagePrice") or 0)
        mv = float(p.get("marketValue") or 0)

        multiplier = 1
        display_symbol = sym
        expiration_date = strike = put_call = underlying = None

        if asset_type == "OPTION":
            multiplier = OPTION_MULTIPLIER
            occ = parse_occ_symbol(sym)
            if occ:
                expiration_date = occ["expiration_date"]
                strike = occ["strike"]
                put_call = occ["put_call"]
                underlying = occ["underlying"]
            else:
                put_call = instr.get("putCall")
                underlying = instr.get("underlyingSymbol")
            if underlying and strike is not None and put_call:
                display_symbol = f"{underlying} {strike:g}{put_call[0]}"

        cost = avg_price * shares * multiplier
        cp = mv / (shares * multiplier) if shares else 0.0
        gain = mv - cost
        gain_pct = (gain / cost * 100) if cost else 0.0

        return {
            "symbol": sym,
            "display_symbol": display_symbol,
            "shares": round(shares, 4),
            "average_price": round(avg_price, 4),
            "current_price": round(cp, 4),
            "market_value": round(mv, 2),
            "cost_basis": round(cost, 2),
            "gain_dollars": round(gain, 2),
            "gain_percent": round(gain_pct, 4),
            "asset_type": asset_type,
            "multiplier": multiplier,
            "expiration_date": expiration_date,
            "strike": strike,
            "put_call": put_call,
            "underlying": underlying,
        }


_PROVIDERS: list[BrokerageProvider] = [SchwabProvider()]


def get_providers() -> list[BrokerageProvider]:
    return [p for p in _PROVIDERS if p.enabled()]


def any_enabled() -> bool:
    return bool(get_providers())


def fetch_all_accounts() -> list[dict[str, Any]]:
    """Normalized accounts across all enabled providers.

    A provider that raises is logged and skipped so one broken brokerage
    can't blank the whole holdings panel.
    """
    out: list[dict[str, Any]] = []
    for provider in get_providers():
        try:
            out.extend(provider.fetch_accounts())
        except Exception:
            log.exception("[brokerages] %s fetch_accounts failed", provider.name)
    return out

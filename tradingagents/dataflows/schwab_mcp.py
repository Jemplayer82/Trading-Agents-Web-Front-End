"""Minimal MCP/JSON-RPC client for the user's Schwab MCP server (streamable HTTP).

The app reaches Schwab through the Schwab MCP server (which holds the
authenticated Schwab session) instead of the app's own OAuth. The server is
*stateless*: a single POST of a ``tools/call`` returns one SSE frame whose
``result.content[0].text`` is the tool's JSON payload — no session id or
``initialize`` handshake is required (verified against schwab-mcp 1.0.0).

Market-data + account READS only. No order/trade tools are exposed here, which
keeps the "paper only — no real orders" guarantee enforced in code.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DEFAULT_URL = "http://100.112.40.124:3105/mcp"
_id_lock = threading.Lock()
_req_id = 0


def _next_id() -> int:
    global _req_id
    with _id_lock:
        _req_id += 1
        return _req_id


def mcp_url() -> str:
    return (os.environ.get("SCHWAB_MCP_URL") or _DEFAULT_URL).strip() or _DEFAULT_URL


def market_data_enabled() -> bool:
    """Whether to route market-data (quotes/OHLCV) through Schwab.

    Account-sync endpoints ignore this flag and probe reachability directly.
    """
    return (os.environ.get("SCHWAB_MARKET_DATA", "1").strip().lower()) not in ("0", "false", "no", "")


def _parse_frame(text: str) -> dict[str, Any] | None:
    """Extract the JSON-RPC object from an SSE ('data: {...}') or plain-JSON body."""
    body = (text or "").strip()
    if not body:
        return None
    if body[0] == "{":
        try:
            return json.loads(body)
        except ValueError:
            return None
    obj: dict[str, Any] | None = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
    return obj


def call_tool(name: str, arguments: dict[str, Any], timeout: float = 30.0) -> Any:
    """Call a Schwab MCP tool; return its parsed JSON payload, or None on any error."""
    req = {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(mcp_url(), json=req, headers=headers)
            resp.raise_for_status()
            frame = _parse_frame(resp.text)
    except Exception as exc:  # noqa: BLE001 — callers fall back to yfinance
        log.warning("[schwab_mcp] %s call failed: %s", name, exc)
        return None

    if not frame:
        log.warning("[schwab_mcp] %s: empty/unparseable response", name)
        return None
    if frame.get("error"):
        log.warning("[schwab_mcp] %s error: %s", name, frame["error"])
        return None

    result = frame.get("result") or {}
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            txt = block.get("text") or ""
            try:
                return json.loads(txt)
            except ValueError:
                return txt
    if "structuredContent" in result:
        return result["structuredContent"]
    return None


# ── Typed wrappers (market data + account reads only) ────────────────────────

def get_quotes(symbols: list[str]) -> dict[str, Any] | None:
    """Real-time quotes keyed by symbol. getQuotes takes a comma-separated string."""
    syms = [s for s in (symbols or []) if s]
    if not syms:
        return None
    data = call_tool("getQuotes", {"symbols": ",".join(syms)})
    return data if isinstance(data, dict) else None


def get_price_history(
    symbol: str,
    period_type: str = "year",
    period: int = 5,
    frequency_type: str = "daily",
    frequency: int = 1,
) -> dict[str, Any] | None:
    """OHLCV candles for one symbol. Returns the Schwab priceHistory payload
    ({'candles': [{open,high,low,close,volume,datetime(ms)}], ...}) or None."""
    data = call_tool("getPriceHistory", {
        "symbol": symbol,
        "periodType": period_type,
        "period": period,
        "frequencyType": frequency_type,
        "frequency": frequency,
    })
    return data if isinstance(data, dict) else None


def get_account_numbers() -> list[dict[str, Any]] | None:
    data = call_tool("getAccountNumbers", {})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("accounts") or data.get("accountNumbers")
    return None


def get_accounts(fields: str = "positions") -> list[dict[str, Any]] | None:
    data = call_tool("getAccounts", {"fields": fields})
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("accounts") or [data]
    return None


def quote_price(quote_obj: dict[str, Any]) -> float | None:
    """Pull the best current price from one symbol's quote object."""
    if not isinstance(quote_obj, dict):
        return None
    q = quote_obj.get("quote") or {}
    for key in ("lastPrice", "mark", "closePrice"):
        v = q.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None

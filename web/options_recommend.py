"""On-demand single-ticker options recommendation.

User types a ticker on the Options tab -> POST /api/options-recommend -> this
module produces a specific contract recommendation with confidence:

  1. quick directional read  — spy_scanner._quick_scan_one (one quick-LLM call
     over 1mo momentum features), same scorer the daily scan trusts;
  2. vet BOTH sides          — options_data.fetch_contract for the call AND the
     put (Schwab greeks pick, yfinance fallback, liquidity gates), so the
     recommender chooses between real tradeable contracts, not abstractions;
  3. one deep-LLM call       — sees the quick read, both contracts, this
     ticker's graded history + global calibration (TradingMemoryLog
     .get_past_context) and the account's options lessons
     (options_learning.format_track_record), returns strict JSON;
  4. learning-loop tie-in    — the directional call is stored in the memory
     log (best-effort) so the nightly sweep grades this recommendation like
     any scan decision.

Deterministic fallback: if the deep call fails or returns garbage, degrade to
the quick scan's own signal (conviction >= options_data.MIN_CONVICTION picks
the matching side, else NO_TRADE) — the endpoint never 500s on LLM flakiness.

Advisory only — nothing here opens positions or touches the ledger.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

import yfinance as yf

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.default_config import DEFAULT_CONFIG

from . import db, options_data, options_learning, spy_scanner
from .llm_helpers import llm_for

log = logging.getLogger(__name__)

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

_ACTIONS = ("CALL", "PUT", "NO_TRADE")

_SYSTEM = """You are a disciplined options advisor. You recommend LONG single-leg
calls or puts only (no spreads, no short options), or NO_TRADE — cash is a
position, and long premium must earn its theta.

You will receive: a momentum quick-read on the underlying, up to two pre-vetted
liquid contracts (one call, one put), this ticker's graded decision history
with calibration stats, and lessons distilled from the account's own closed
options trades. Weigh the lessons — they are this system's real track record.

Be honest about confidence: 8-10 needs strong momentum AND clean contract
pricing AND supportive history; disagreement between them caps confidence at 5.
Recommend NO_TRADE freely — most days, most tickers, there is no edge.

Return ONLY valid JSON — no prose, no markdown fences:
{{"action": "CALL" | "PUT" | "NO_TRADE",
  "confidence": 1-10,
  "entry_premium": <float, near the mid>,
  "target_premium": <float>,
  "stop_premium": <float>,
  "horizon_days": <int, expected hold>,
  "thesis": "<2-3 sentences: why this side, why now>",
  "risks": ["<top risk>", "<second risk>"]}}
For NO_TRADE set the premium fields to null and explain why in "thesis"."""


def valid_ticker(ticker: str) -> str | None:
    """Uppercased ticker if it looks plausible, else None."""
    t = (ticker or "").strip().upper()
    return t if _TICKER_RE.match(t) else None


def _price_data(ticker: str) -> dict[str, list]:
    raw = yf.download(ticker, period="1mo", auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise ValueError(f"no price data for {ticker}")
    closes = raw["Close"].dropna()
    volumes = raw["Volume"].dropna()
    # Single-ticker downloads can still come back with MultiIndex columns.
    if hasattr(closes, "columns"):
        closes, volumes = closes.iloc[:, 0], volumes.iloc[:, 0]
    return {"close": closes.tolist(), "volume": volumes.tolist()}


def _contract_line(c: dict[str, Any] | None, side: str) -> str:
    if not c:
        return f"{side}: no tradeable contract passed vetting\n"
    delta = f"{abs(c['delta']):.2f}" if c.get("delta") is not None else "n/a"
    return (
        f"{side}: {c['occ_symbol']} | strike {c['strike']:g} exp {c['expiration_date']} "
        f"({c.get('dte')}d) | delta {delta} | mid ${float(c.get('mid') or 0):.2f} "
        f"(${float(c.get('mid') or 0) * 100:,.0f}/contract) | OI {c.get('open_interest')} "
        f"| spread {float(c.get('spread_pct') or 0):.1%}\n"
    )


def _fallback(quick: dict[str, Any], call: dict | None, put: dict | None) -> dict[str, Any]:
    """Deterministic rec from the quick read when the deep LLM fails."""
    sig = (quick.get("signal") or "HOLD").upper()
    conviction = int(quick.get("conviction") or 0)
    side = None
    if conviction >= options_data.MIN_CONVICTION:
        if sig == "BUY" and call:
            side = ("CALL", call)
        elif sig == "SELL" and put:
            side = ("PUT", put)
    if side is None:
        return {
            "action": "NO_TRADE", "confidence": max(1, min(conviction, 4)),
            "entry_premium": None, "target_premium": None, "stop_premium": None,
            "horizon_days": None,
            "thesis": f"Quick scan reads {sig} conviction {conviction}/10 — below the "
                      f"bar for levered premium (advisor unavailable, deterministic fallback).",
            "risks": ["Recommendation degraded: deep advisor call failed."],
        }
    action, c = side
    mid = float(c.get("mid") or 0)
    return {
        "action": action, "confidence": min(conviction, 6),
        "entry_premium": round(mid, 2),
        "target_premium": round(mid * 1.5, 2),
        "stop_premium": round(mid * 0.6, 2),
        "horizon_days": min(int(c.get("dte") or 21), 14),
        "thesis": f"Quick scan {sig} conviction {conviction}/10; mechanical 1.5x target / "
                  "-40% stop (advisor unavailable, deterministic fallback).",
        "risks": ["Recommendation degraded: deep advisor call failed."],
    }


def _parse_rec(raw: str) -> dict[str, Any]:
    raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw.strip())
    rec = json.loads(raw)
    if not isinstance(rec, dict) or str(rec.get("action", "")).upper() not in _ACTIONS:
        raise ValueError("advisor returned invalid recommendation JSON")
    rec["action"] = str(rec["action"]).upper()
    try:
        rec["confidence"] = max(1, min(10, int(rec.get("confidence") or 1)))
    except (TypeError, ValueError):
        rec["confidence"] = 1
    return rec


def recommend(ticker: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full recommendation flow for one ticker. Raises ValueError on bad input
    or missing price data; degrades (never raises) on LLM failure."""
    t = valid_ticker(ticker)
    if not t:
        raise ValueError(f"invalid ticker: {ticker!r}")
    config = {**DEFAULT_CONFIG, **(config or {})}

    price_data = _price_data(t)
    spot = float(price_data["close"][-1])

    # 1. Quick directional read (never raises — degrades to HOLD/1 + error key).
    quick_llm = llm_for(config, deep=False, temperature=0.0)
    quick = spy_scanner._quick_scan_one(t, price_data, "Unknown", quick_llm)

    # 2. Vet both sides so the advisor picks between real contracts.
    call, call_notes = options_data.fetch_contract(t, "BUY", spot_hint=spot)
    put, put_notes = options_data.fetch_contract(t, "SELL", spot_hint=spot)
    notes = call_notes + put_notes

    # 3. Learning context — the whole point of asking THIS system and not a
    # generic chatbot. Best-effort: an empty section never blocks the rec.
    past_context = ""
    try:
        past_context = TradingMemoryLog(config).get_past_context(t)
    except Exception:
        log.exception("[recommend] past context failed for %s", t)
    lessons = ""
    try:
        accounts = db.list_paper_accounts(kind="options")
        if accounts:
            aid = int(accounts[0]["id"])
            closed = db.list_options_positions(aid, status="settled")
            stats = options_learning.compute_options_stats(
                closed, min_closed=int(config.get("options_lessons_min_closed", 10)))
            lesson_row = db.latest_options_lesson(aid)
            lessons = options_learning.format_track_record(
                stats, (lesson_row or {}).get("lessons_md"),
                max_chars=int(config.get("options_lessons_max_chars", 1200)))
    except Exception:
        log.exception("[recommend] lessons context failed")

    user = (
        f"Ticker: {t} | spot ${spot:,.2f} | date {datetime.utcnow().date().isoformat()}\n\n"
        f"=== MOMENTUM QUICK-READ ===\n"
        f"signal {quick.get('signal')} conviction {quick.get('conviction')}/10 — "
        f"{(quick.get('reasoning') or '').strip()}\n\n"
        f"=== VETTED CONTRACTS ===\n"
        f"{_contract_line(call, 'CALL')}{_contract_line(put, 'PUT')}\n"
    )
    if past_context.strip():
        user += f"=== GRADED DECISION HISTORY ({t} + calibration) ===\n{past_context.strip()}\n\n"
    if lessons.strip():
        user += f"{lessons.strip()}\n\n"
    user += "Recommend."

    if not call and not put:
        rec = {
            "action": "NO_TRADE", "confidence": 2,
            "entry_premium": None, "target_premium": None, "stop_premium": None,
            "horizon_days": None,
            "thesis": f"No {t} contract passed liquidity/delta/DTE vetting on either side — "
                      "nothing tradeable to recommend.",
            "risks": ["Illiquid or unavailable option chain."],
        }
    else:
        # Degradation ladder: deep advisor -> quick-model advisor -> mechanical
        # fallback. The deep role can route through the switchboard bus to a
        # claude-CLI agent with a hard 150s budget that a long prompt can blow
        # (observed live, twice in a row) — the quick model (direct provider)
        # answers this JSON-picking task fast and reliably, and a model-graded
        # rec beats the mechanical formula every time.
        rec = None
        for deep in (True, False):
            try:
                llm = llm_for(config, deep=deep, temperature=0.2)
                resp = llm.invoke([
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ])
                rec = _parse_rec(resp.content if hasattr(resp, "content") else str(resp))
                break
            except Exception:
                log.exception("[recommend] %s advisor failed for %s",
                              "deep" if deep else "quick", t)
        if rec is None:
            log.warning("[recommend] advisor unavailable for %s — deterministic fallback", t)
            rec = _fallback(quick, call, put)

    # Pin the recommendation to the actually-vetted contract for its side; an
    # advisor-hallucinated occ_symbol must never reach the user.
    chosen = call if rec["action"] == "CALL" else put if rec["action"] == "PUT" else None
    if rec["action"] != "NO_TRADE" and chosen is None:
        rec = _fallback(quick, call, put)
        chosen = call if rec["action"] == "CALL" else put if rec["action"] == "PUT" else None
    if chosen:
        rec["occ_symbol"] = chosen["occ_symbol"]

    # 4. Feed the learning loop: this rec is a directional decision like any
    # scan dive. Rated Buy/Sell/Hold so the nightly sweep can grade it.
    try:
        rating = {"CALL": "Buy", "PUT": "Sell"}.get(rec["action"], "Hold")
        TradingMemoryLog(config).store_decision(
            ticker=t,
            trade_date=datetime.utcnow().date().isoformat(),
            final_trade_decision=(
                f"Rating: {rating}\n\nOn-demand options recommendation "
                f"(confidence {rec['confidence']}/10): {rec.get('thesis', '')}"
            ),
        )
    except Exception:
        log.exception("[recommend] memory-log store failed for %s", t)

    return {
        "ticker": t,
        "spot": spot,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "quick": {k: quick.get(k) for k in ("signal", "conviction", "reasoning")},
        "recommendation": rec,
        "contract": chosen,
        "candidates": {"call": call, "put": put},
        "vetting_notes": notes[:8],
        "context_used": {"history": bool(past_context.strip()), "lessons": bool(lessons.strip())},
    }

"""LLM allocator: 50 deep-dive results → $100k paper portfolio (or weekly rebalance)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_openai import ChatOpenAI
from tradingagents.default_config import DEFAULT_CONFIG

from .llm_helpers import llm_for

log = logging.getLogger(__name__)

# ─── Fresh allocation (week 1 or first ever run) ──────────────────────────────

FRESH_SYSTEM_PROMPT = """You are a quantitative portfolio manager. Given a set of
deep-dive analyses for the top S&P 500 candidates, produce a $100,000 paper
portfolio allocation.

Rules:
- Total must equal EXACTLY $100,000.
- Minimum position: $500. Maximum position: $15,000 (15%).
- Weight positions by signal strength and conviction score.
- Only BUY-rated tickers should receive meaningful allocation (>1%).
- HOLD tickers may receive small allocations (0.5–3%) as speculative.
- SELL-rated tickers get $0.
- Set "action" to "NEW" for every position (this is a fresh portfolio).
- Return ONLY valid JSON — no prose, no markdown fences.

Output format (array of objects):
[
  {"ticker": "NVDA", "action": "NEW", "allocation_pct": 8.5, "dollar_amount": 8500,
   "entry_price": 145.23, "rationale": "...one sentence..."},
  ...
]
"""

# ─── Weekly rebalance (week 2+) ───────────────────────────────────────────────

REBALANCE_SYSTEM_PROMPT = """You are a quantitative portfolio manager running a
weekly rebalance of a paper portfolio.

You will receive:
1. The total capital available for this week (starting value).
2. Current holdings with this week's updated signals.
3. New high-conviction candidates not yet in the portfolio.

Rebalancing rules:
- Total allocations must equal EXACTLY the starting capital (rounded to nearest $1).
- Minimum position: $500. Maximum: 15% of starting capital.
- EXITED positions (SELL signal or dropped out of top candidates) free up capital.
- Kept positions maintain their original entry_price for P&L tracking.
- New positions use the current entry_price provided.
- Weight by conviction; BUY > HOLD in allocation size.
- Trim HOLD positions to make room for high-conviction BUYs.
- Return ONLY valid JSON — no prose, no markdown fences.

Action values:
  "NEW"     — new position added this week
  "HOLD"    — existing position kept at similar weight
  "ADDED"   — existing position, allocation increased
  "TRIMMED" — existing position, allocation decreased
  "EXITED"  — position closed (include with dollar_amount: 0 for the record)

Output format (array of objects, include EXITED positions with dollar_amount 0):
[
  {"ticker": "NVDA", "action": "HOLD", "allocation_pct": 8.5, "dollar_amount": 8500,
   "entry_price": 145.23, "rationale": "...one sentence..."},
  {"ticker": "META", "action": "EXITED", "allocation_pct": 0, "dollar_amount": 0,
   "entry_price": 520.00, "rationale": "Signal flipped to SELL."},
  ...
]
"""

FRESH_USER_HEADER = "Date: {trade_date}\nCandidates ({n} tickers):\n"

REBALANCE_USER_HEADER = (
    "Date: {trade_date}\n"
    "Starting capital: ${capital:,.0f}\n\n"
    "=== CURRENT HOLDINGS ({n_held} positions) ===\n"
)

REBALANCE_NEW_HEADER = "\n=== NEW CANDIDATES ({n_new} tickers not currently held) ===\n"

PER_TICKER_TEMPLATE = (
    "{ticker} | signal: {signal} | conviction: {conviction}/10 | "
    "entry_price: ${entry_price:.2f} | {excerpt}\n"
)


def _llm(config: dict[str, Any]) -> ChatOpenAI:
    return llm_for(config, deep=True, temperature=0.1)


# ─── Fallbacks ────────────────────────────────────────────────────────────────

def _fallback_fresh(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buys = [c for c in candidates if (c.get("signal") or "").upper() == "BUY"]
    if not buys:
        buys = sorted(candidates, key=lambda c: -(c.get("conviction") or 0))[:20]
    total = 100_000
    per = round(total / len(buys), 2)
    return [
        {
            "ticker": c["ticker"],
            "action": "NEW",
            "allocation_pct": round(per / 1000, 2),
            "dollar_amount": per,
            "entry_price": c.get("entry_price", 0),
            "rationale": f"Fallback equal-weight (conviction {c.get('conviction','?')}/10).",
        }
        for c in buys
    ]


def _fallback_rebalance(
    candidates: list[dict[str, Any]],
    previous_portfolio: list[dict[str, Any]],
    starting_value: float,
) -> list[dict[str, Any]]:
    """Equal-weight BUY candidates using available capital."""
    new_tickers = {c["ticker"] for c in candidates}
    prev_map = {p["ticker"]: p for p in previous_portfolio}
    result: list[dict[str, Any]] = []

    buys = [c for c in candidates if (c.get("signal") or "").upper() == "BUY"]
    if not buys:
        buys = sorted(candidates, key=lambda c: -(c.get("conviction") or 0))[:20]

    per = round(starting_value / max(len(buys), 1), 2)
    alloc_pct = round(per / starting_value * 100, 2) if starting_value else 0

    active_tickers = {c["ticker"] for c in buys}

    # Mark exits
    for prev in previous_portfolio:
        if prev["ticker"] not in new_tickers or prev["ticker"] not in active_tickers:
            result.append({
                "ticker": prev["ticker"],
                "action": "EXITED",
                "allocation_pct": 0,
                "dollar_amount": 0,
                "entry_price": prev.get("entry_price", 0),
                "rationale": "Dropped from top candidates or SELL signal.",
            })

    # Allocate to buys
    for c in buys:
        prev = prev_map.get(c["ticker"])
        action = "HOLD" if prev else "NEW"
        entry = prev["entry_price"] if prev else c.get("entry_price", 0)
        result.append({
            "ticker": c["ticker"],
            "action": action,
            "allocation_pct": alloc_pct,
            "dollar_amount": per,
            "entry_price": entry,
            "rationale": f"Fallback equal-weight (conviction {c.get('conviction','?')}/10).",
        })

    return result


# ─── Public API ───────────────────────────────────────────────────────────────

def run(
    candidates: list[dict[str, Any]],
    trade_date: str,
    config: dict[str, Any],
    previous_portfolio: list[dict[str, Any]] | None = None,
    starting_value: float | None = None,
) -> dict[str, Any]:
    """Return {allocations, total, report_md, starting_value}.

    If previous_portfolio is provided (week 2+), performs a rebalance.
    Otherwise allocates a fresh $100k portfolio (week 1).
    """
    if not candidates:
        return {"allocations": [], "total": 0, "report_md": "No candidates provided.",
                "starting_value": starting_value or 100_000}

    is_rebalance = bool(previous_portfolio)
    capital = starting_value if (is_rebalance and starting_value) else 100_000.0

    # ── Build the user message ────────────────────────────────────────────────
    if not is_rebalance:
        user_msg = FRESH_USER_HEADER.format(trade_date=trade_date, n=len(candidates))
        for c in candidates:
            excerpt = (c.get("final_decision") or c.get("reasoning") or "")[:300]
            user_msg += PER_TICKER_TEMPLATE.format(
                ticker=c["ticker"],
                signal=(c.get("signal") or "—").upper(),
                conviction=c.get("conviction") or 0,
                entry_price=c.get("entry_price") or 0.0,
                excerpt=excerpt,
            )
        system = FRESH_SYSTEM_PROMPT
        fallback_fn = lambda: _fallback_fresh(candidates)
    else:
        prev_map = {p["ticker"]: p for p in (previous_portfolio or [])}
        cand_map = {c["ticker"]: c for c in candidates}

        # Split candidates into held vs new
        held = [c for c in candidates if c["ticker"] in prev_map]
        new_cands = [c for c in candidates if c["ticker"] not in prev_map]
        # Mark previous holdings not in new candidates as exits
        exits = [p for p in (previous_portfolio or []) if p["ticker"] not in cand_map]

        user_msg = REBALANCE_USER_HEADER.format(
            trade_date=trade_date, capital=capital, n_held=len(held) + len(exits)
        )
        # Show current holdings with updated signals
        for prev in (previous_portfolio or []):
            cand = cand_map.get(prev["ticker"])
            if cand:
                sig = (cand.get("signal") or "—").upper()
                conv = cand.get("conviction") or 0
                excerpt = (cand.get("final_decision") or cand.get("reasoning") or "")[:200]
            else:
                sig = "SELL"
                conv = 0
                excerpt = "No longer in top candidates — consider exiting."
            user_msg += PER_TICKER_TEMPLATE.format(
                ticker=prev["ticker"],
                signal=sig,
                conviction=conv,
                entry_price=prev.get("entry_price") or 0.0,
                excerpt=excerpt,
            )
        # Show new candidates
        if new_cands:
            user_msg += REBALANCE_NEW_HEADER.format(n_new=len(new_cands))
            for c in new_cands:
                excerpt = (c.get("final_decision") or c.get("reasoning") or "")[:200]
                user_msg += PER_TICKER_TEMPLATE.format(
                    ticker=c["ticker"],
                    signal=(c.get("signal") or "—").upper(),
                    conviction=c.get("conviction") or 0,
                    entry_price=c.get("entry_price") or 0.0,
                    excerpt=excerpt,
                )

        system = REBALANCE_SYSTEM_PROMPT
        fallback_fn = lambda: _fallback_rebalance(candidates, previous_portfolio or [], capital)

    # ── Call the LLM ─────────────────────────────────────────────────────────
    llm = _llm({**DEFAULT_CONFIG, **config})
    try:
        resp = llm.invoke([
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        allocations: list[dict[str, Any]] = json.loads(raw)
    except Exception as exc:
        log.exception("Allocator LLM call failed: %s", exc)
        allocations = fallback_fn()

    # ── Patch entry_price for HOLD/ADDED/TRIMMED from previous portfolio ──────
    if is_rebalance and previous_portfolio:
        prev_map = {p["ticker"]: p for p in previous_portfolio}
        for a in allocations:
            if a.get("action") in ("HOLD", "ADDED", "TRIMMED"):
                prev = prev_map.get(a["ticker"])
                if prev and prev.get("entry_price"):
                    a["entry_price"] = prev["entry_price"]

    total = sum(a.get("dollar_amount", 0) for a in allocations if a.get("action") != "EXITED")

    # ── Build markdown report ─────────────────────────────────────────────────
    mode_label = "Rebalance" if is_rebalance else "Initial Portfolio"
    lines = [
        f"# S&P 500 Paper Portfolio — {trade_date} ({mode_label})",
        f"**Starting capital:** ${capital:,.0f}  ",
        f"**Total deployed:** ${total:,.0f}  ",
        f"**Positions:** {len([a for a in allocations if a.get('action') != 'EXITED'])}",
        "",
    ]
    if is_rebalance:
        exited = [a for a in allocations if a.get("action") == "EXITED"]
        new = [a for a in allocations if a.get("action") == "NEW"]
        lines += [
            f"**New positions:** {len(new)}  ",
            f"**Exited positions:** {len(exited)}  ",
            "",
        ]

    lines += [
        "| Ticker | Action | Signal | Conv. | $ Amount | % | Rationale |",
        "|--------|--------|--------|-------|----------|---|-----------|",
    ]
    for a in sorted(allocations, key=lambda x: -(x.get("dollar_amount") or 0)):
        lines.append(
            f"| {a['ticker']} | {a.get('action','—')} | {a.get('signal','').upper() or '—'} "
            f"| {a.get('conviction','—')} "
            f"| ${a.get('dollar_amount', 0):,.0f} "
            f"| {a.get('allocation_pct', 0):.1f}% "
            f"| {(a.get('rationale') or '')[:80]} |"
        )

    return {
        "allocations": allocations,
        "total": total,
        "report_md": "\n".join(lines),
        "starting_value": capital,
    }

"""LLM allocator: 50 deep-dive results → $100k paper portfolio (or weekly rebalance).

Phase 3 of the S&P pipeline (web/portfolio_main._run_spy_scan). One deep-LLM
call turns the enriched candidates into a JSON array of allocations; if the
call or its JSON parse fails, a deterministic equal-weight fallback runs so a
scan never finishes without a portfolio.

Allocation dict (persisted as spy_scans.portfolio_json; consumed by
spy_scanner.refresh_portfolio_prices and /api/spy-account/compare):
    ticker, action, allocation_pct, dollar_amount, entry_price, rationale
    shares, cost_basis              added here post-LLM (whole-share conversion)
    current_price, current_value    added later by refresh_portfolio_prices

`action` is "NEW" | "HOLD" | "ADDED" | "TRIMMED" | "EXITED". EXITED rows are
kept (shares=0) as a paper trail; downstream code filters action != "EXITED"
to find live positions.

Two modes: fresh (week 1, $100k) vs rebalance (week 2+, capital = the previous
scan's refreshed value; kept positions retain their original entry_price so
P&L stays anchored to actual cost).
"""
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

_FRESH_SYSTEM_TEMPLATE = """You are a quantitative portfolio manager. You have up to
${capital:,.0f} of paper capital and a shortlist of deep-dive analyses for the top S&P
500 candidates.
{bias_context}
Capital discipline (IMPORTANT):
- The candidate list is a SHORTLIST for review — NOT a mandate to buy all of them.
- You do NOT have to deploy the full ${capital:,.0f}. Hold the remainder as cash whenever
  there aren't enough genuinely compelling, high-conviction opportunities.
- Only deploy capital to names you would actually buy with real money. It is
  perfectly fine (and often correct) to invest well under ${capital:,.0f} in just a
  handful of positions and leave the rest in cash.

Rules:
- Total invested must be ≤ ${capital:,.0f} (anything not invested is cash).
- Minimum position: $500. Maximum position: ${max_pos:,.0f} ({max_pct}% of capital).
- At least {min_cash_pct}% of capital must remain as cash (uninvested buffer).
- Weight positions by signal strength and conviction score.
- Only BUY-rated tickers should receive meaningful allocation (>1%).
- HOLD tickers may receive small allocations (0.5–3%) as speculative, or none.
- SELL-rated and low-conviction tickers get $0 (just omit them).
- Set "action" to "NEW" for every position (this is a fresh portfolio).
- Return ONLY valid JSON — no prose, no markdown fences.

Output format (array of objects):
[
  {{"ticker": "NVDA", "action": "NEW", "allocation_pct": 8.5, "dollar_amount": 8500,
   "entry_price": 145.23, "rationale": "...one sentence..."}},
  ...
]
"""

_REBALANCE_SYSTEM_TEMPLATE = """You are a quantitative portfolio manager running a
weekly rebalance of a paper portfolio.
{bias_context}
You will receive:
1. The total capital available for this week (starting value).
2. Current holdings with this week's updated signals.
3. New high-conviction candidates not yet in the portfolio.

Capital discipline (IMPORTANT):
- The candidate list is a SHORTLIST for review — NOT a mandate to hold all of them.
- You do NOT have to stay fully invested. Total invested may be LESS than the
  starting capital; hold the remainder as cash when conviction is thin.
- Raise cash by trimming or exiting when there aren't enough compelling ideas.

Rebalancing rules:
- Total invested must be ≤ the starting capital (the rest is cash).
- Minimum position: $500. Maximum: {max_pct}% of starting capital.
- At least {min_cash_pct}% of capital must remain as cash (uninvested buffer).
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
  {{"ticker": "NVDA", "action": "HOLD", "allocation_pct": 8.5, "dollar_amount": 8500,
   "entry_price": 145.23, "rationale": "...one sentence..."}},
  {{"ticker": "META", "action": "EXITED", "allocation_pct": 0, "dollar_amount": 0,
   "entry_price": 520.00, "rationale": "Signal flipped to SELL."}},
  ...
]
"""

_BIAS_CONTEXT = {
    "bullish": "\nMarket stance: BULLISH — prefer larger positions on high-conviction BUYs. "
               "Deploy more capital when signals are strong. In borderline Buy/Hold cases, lean Buy.\n",
    "bearish": "\nMarket stance: BEARISH — prefer smaller positions and higher cash buffers. "
               "Be selective; only deploy to the highest-conviction BUYs. In borderline Hold/Sell cases, lean Sell.\n",
    "neutral": "",
}


def _position_limits(aggressiveness: int, capital: float) -> tuple[float, int, int]:
    """Return (max_position_dollars, max_pct, min_cash_pct) from aggressiveness 1–10."""
    if aggressiveness <= 3:
        max_pct, min_cash_pct = 7, 20
    elif aggressiveness <= 7:
        max_pct, min_cash_pct = 12, 10
    else:
        max_pct, min_cash_pct = 20, 5
    return capital * max_pct / 100, max_pct, min_cash_pct

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


def _llm(config: dict[str, Any]):
    return llm_for(config, deep=True, temperature=0.1)


# ─── Fallbacks ────────────────────────────────────────────────────────────────

def _fallback_fresh(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Equal-weight the BUYs (or top 20 by conviction if none) across $100k."""
    buys = [c for c in candidates if (c.get("signal") or "").upper() == "BUY"]
    if not buys:
        buys = sorted(candidates, key=lambda c: -(c.get("conviction") or 0))[:20]
    total = 100_000
    per = round(total / len(buys), 2)
    return [
        {
            "ticker": c["ticker"],
            "action": "NEW",
            # per/1000 == per / $100k * 100, i.e. pct of the fixed fresh capital.
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
    aggressiveness: int = 5,
    bias: str = "neutral",
) -> dict[str, Any]:
    """Return {allocations, total, cash, report_md, starting_value}.

    If previous_portfolio is provided (week 2+), performs a rebalance;
    otherwise allocates a fresh portfolio (week 1). aggressiveness (1–10)
    controls position sizing limits; bias (bullish/neutral/bearish) shifts
    the LLM prompt toward more or less aggressive stance.
    """
    if not candidates:
        return {"allocations": [], "total": 0, "report_md": "No candidates provided.",
                "starting_value": starting_value or 100_000}

    is_rebalance = bool(previous_portfolio)
    capital = starting_value if (is_rebalance and starting_value) else 100_000.0
    max_pos, max_pct, min_cash_pct = _position_limits(aggressiveness, capital)
    bias_context = _BIAS_CONTEXT.get(bias, "")

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
        system = _FRESH_SYSTEM_TEMPLATE.format(
            capital=capital, max_pos=max_pos, max_pct=max_pct,
            min_cash_pct=min_cash_pct, bias_context=bias_context,
        )
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

        system = _REBALANCE_SYSTEM_TEMPLATE.format(
            max_pct=max_pct, min_cash_pct=min_cash_pct, bias_context=bias_context,
        )
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

    # ── Convert dollar targets into WHOLE shares (real paper-trade fills) ─────
    # shares = floor(target $ / entry price); the rounding remainder stays cash.
    kept: list[dict[str, Any]] = []
    for a in allocations:
        if a.get("action") == "EXITED":
            a["shares"] = 0
            a["cost_basis"] = 0.0
            kept.append(a)
            continue
        ep = float(a.get("entry_price") or 0)
        target = float(a.get("dollar_amount") or 0)
        shares = int(target // ep) if ep > 0 else 0
        if shares <= 0:
            # Can't afford a single whole share — that money stays in cash.
            continue
        a["shares"] = shares
        a["cost_basis"] = round(shares * ep, 2)
        a["allocation_pct"] = round(a["cost_basis"] / capital * 100, 2) if capital else 0.0
        kept.append(a)
    allocations = kept

    total = sum(a.get("cost_basis", 0) for a in allocations if a.get("action") != "EXITED")
    cash = max(0.0, capital - total)
    cash_pct = (cash / capital * 100) if capital else 0.0

    # ── Build markdown report ─────────────────────────────────────────────────
    mode_label = "Rebalance" if is_rebalance else "Initial Portfolio"
    bias_label = bias.capitalize() if bias != "neutral" else "Neutral"
    lines = [
        f"# S&P 500 Paper Portfolio — {trade_date} ({mode_label})",
        f"**Starting capital:** ${capital:,.0f}  ",
        f"**Aggressiveness:** {aggressiveness}/10 | **Bias:** {bias_label}  ",
        f"**Total deployed:** ${total:,.0f} ({(total / capital * 100) if capital else 0:.1f}%)  ",
        f"**Cash (uninvested):** ${cash:,.0f} ({cash_pct:.1f}%)  ",
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
        "| Ticker | Action | Signal | Shares | Entry $ | Cost | % | Rationale |",
        "|--------|--------|--------|--------|---------|------|---|-----------|",
    ]
    for a in sorted(allocations, key=lambda x: -(x.get("cost_basis") or 0)):
        is_exit = a.get("action") == "EXITED"
        lines.append(
            f"| {a['ticker']} | {a.get('action','—')} | {a.get('signal','').upper() or '—'} "
            f"| {'—' if is_exit else a.get('shares', 0)} "
            f"| ${a.get('entry_price', 0):,.2f} "
            f"| {'—' if is_exit else '$' + format(a.get('cost_basis', 0), ',.0f')} "
            f"| {a.get('allocation_pct', 0):.1f}% "
            f"| {(a.get('rationale') or '')[:80]} |"
        )

    return {
        "allocations": allocations,
        "total": total,
        "cash": round(cash, 2),
        "report_md": "\n".join(lines),
        "starting_value": capital,
    }

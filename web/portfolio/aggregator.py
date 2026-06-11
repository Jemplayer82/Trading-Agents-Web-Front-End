"""LLM aggregator: per-ticker analyses → portfolio-wide briefing.

Uses the same ChatOpenAI-compatible pattern as the rest of the codebase. The
four markdown sections it produces (Concentration / Correlation / Rebalance /
Watch List) are also the four blocks the newsletter template renders.
"""
from __future__ import annotations

import logging
from typing import Any

from tradingagents.constants import SIGNALS
from tradingagents.default_config import DEFAULT_CONFIG

from ..llm_helpers import llm_for

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a portfolio risk officer reviewing a multi-agent
analysis of every ticker the user currently holds at Schwab. Your job is to look
at the portfolio as a WHOLE and produce a short, actionable briefing.

You ALWAYS return Markdown with EXACTLY these four `##` sections, in order:

## Concentration Risk
Are any single positions, sectors, or themes outsized? Quantify when obvious.
Two to four sentences.

## Correlation Themes
Group positions by what's actually driving them (rates, AI capex, US consumer,
oil, etc.). Call out clusters that move together. Two to four sentences.

## Rebalance Suggestions
Three to five concrete, numbered ideas. Each one references the ticker(s) by
symbol and a clear action ("trim", "add", "hold", "set stop at $X"). No hedging.

## Watch List
Three to five tickers the user should pay extra attention to today, each with a
one-sentence reason."""

USER_PROMPT_HEADER = """Date: {trade_date}
Total positions: {n}
Signal breakdown: {sig}

Per-ticker analyses follow. Use them — do not invent fresh ones."""

PER_TICKER_TEMPLATE = """
---
### {ticker}  (signal: {signal}, qty: {qty}, mkt_val: ${mv:,.0f})

**Trader plan:**
{trader_plan}

**Final risk verdict:**
{final_decision}
"""


def run(per_ticker: list[dict[str, Any]], trade_date: str, config: dict[str, Any]) -> str:
    if not per_ticker:
        return (
            "## Concentration Risk\nNo positions on file.\n\n"
            "## Correlation Themes\n—\n\n"
            "## Rebalance Suggestions\n—\n\n"
            "## Watch List\n—\n"
        )
    counts = {sig: 0 for sig in SIGNALS}
    for p in per_ticker:
        s = (p.get("signal") or "").upper()
        if s in counts:
            counts[s] += 1
    sig = f"{counts['BUY']} BUY · {counts['HOLD']} HOLD · {counts['SELL']} SELL"
    user_msg = USER_PROMPT_HEADER.format(trade_date=trade_date, n=len(per_ticker), sig=sig)
    for p in per_ticker:
        user_msg += PER_TICKER_TEMPLATE.format(
            ticker=p.get("ticker", "?"),
            signal=(p.get("signal") or "—").upper(),
            qty=p.get("quantity", 0),
            mv=p.get("market_value", 0),
            trader_plan=(p.get("trader_plan") or "(no trader plan)")[:2000],
            final_decision=(p.get("final_decision") or "(no decision)")[:2000],
        )
    llm = llm_for({**DEFAULT_CONFIG, **config}, deep=True, temperature=0.2)
    try:
        resp = llm.invoke([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ])
        return resp.content if hasattr(resp, "content") else str(resp)
    except Exception as exc:
        log.exception("Aggregator LLM call failed")
        return f"## Aggregator Error\n\nFailed to run aggregator: `{exc}`. Per-ticker reports are still available below.\n"

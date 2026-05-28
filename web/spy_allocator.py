"""LLM allocator: 50 deep-dive results → $100k paper portfolio."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_openai import ChatOpenAI
from tradingagents.default_config import DEFAULT_CONFIG

from .llm_helpers import llm_for

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a quantitative portfolio manager. Given a set of
deep-dive analyses for the top S&P 500 candidates, produce a $100,000 paper
portfolio allocation.

Rules:
- Total must equal EXACTLY $100,000.
- Minimum position: $500. Maximum position: $15,000 (15%).
- Weight positions by signal strength and conviction score.
- Only BUY-rated tickers should receive meaningful allocation (>1%).
- HOLD tickers may receive small allocations (0.5–3%) as speculative.
- SELL-rated tickers get $0.
- Return ONLY valid JSON — no prose, no markdown fences.

Output format (array of objects):
[
  {"ticker": "NVDA", "allocation_pct": 8.5, "dollar_amount": 8500, "entry_price": 145.23, "rationale": "...one sentence..."},
  ...
]
"""

USER_PROMPT_HEADER = """Date: {trade_date}
Candidates ({n} tickers, select best allocation):
"""

PER_TICKER_TEMPLATE = (
    "{ticker} | signal: {signal} | conviction: {conviction}/10 | "
    "entry_price: ${entry_price:.2f} | {excerpt}\n"
)


def _llm_for(config: dict[str, Any]) -> ChatOpenAI:
    return llm_for(config, deep=True, temperature=0.1)


def _fallback_allocation(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Equal-weight BUY candidates by conviction when LLM fails."""
    buys = [c for c in candidates if (c.get("signal") or "").upper() == "BUY"]
    if not buys:
        buys = sorted(candidates, key=lambda c: -(c.get("conviction") or 0))[:20]
    total = 100_000
    per = round(total / len(buys), 2)
    return [
        {
            "ticker": c["ticker"],
            "allocation_pct": round(per / 1000, 2),
            "dollar_amount": per,
            "entry_price": c.get("entry_price", 0),
            "rationale": f"Fallback equal-weight allocation (conviction {c.get('conviction', '?')}/10).",
        }
        for c in buys
    ]


def run(
    candidates: list[dict[str, Any]],
    trade_date: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Return {allocations, total, report_md}."""
    if not candidates:
        return {"allocations": [], "total": 0, "report_md": "No candidates provided."}

    user_msg = USER_PROMPT_HEADER.format(trade_date=trade_date, n=len(candidates))
    for c in candidates:
        excerpt = (c.get("final_decision") or c.get("reasoning") or "")[:300]
        user_msg += PER_TICKER_TEMPLATE.format(
            ticker=c["ticker"],
            signal=(c.get("signal") or "—").upper(),
            conviction=c.get("conviction") or 0,
            entry_price=c.get("entry_price") or 0.0,
            excerpt=excerpt,
        )

    llm = _llm_for({**DEFAULT_CONFIG, **config})
    try:
        resp = llm.invoke([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ])
        raw = resp.content if hasattr(resp, "content") else str(resp)
        # Strip markdown fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        allocations: list[dict[str, Any]] = json.loads(raw)
    except Exception as exc:
        log.exception("Allocator LLM call failed: %s", exc)
        allocations = _fallback_allocation(candidates)

    total = sum(a.get("dollar_amount", 0) for a in allocations)
    # Build a short markdown report
    lines = [
        f"# S&P 500 Paper Portfolio — {trade_date}",
        f"**Total deployed:** ${total:,.0f}  ",
        f"**Positions:** {len(allocations)}",
        "",
        "| Ticker | Signal | Conv. | $ Amount | % | Rationale |",
        "|--------|--------|-------|----------|---|-----------|",
    ]
    for a in sorted(allocations, key=lambda x: -(x.get("dollar_amount") or 0)):
        lines.append(
            f"| {a['ticker']} | {a.get('signal', '').upper() or '—'} "
            f"| {a.get('conviction', '—')} "
            f"| ${a.get('dollar_amount', 0):,.0f} "
            f"| {a.get('allocation_pct', 0):.1f}% "
            f"| {a.get('rationale', '')[:80]} |"
        )
    return {"allocations": allocations, "total": total, "report_md": "\n".join(lines)}

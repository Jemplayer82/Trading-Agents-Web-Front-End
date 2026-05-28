"""Portfolio scan orchestrator.

Wraps the existing per-ticker TradingAgentsGraph: loops over the user's Schwab
positions, runs each ticker through the multi-agent pipeline, then hands the
collected outputs back to the caller (web/portfolio_main.py), which persists
them and invokes the aggregator separately so an aggregator failure doesn't
lose per-ticker work.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

log = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[dict[str, Any]], None]]


def _signal_from_decision(text: str) -> str:
    if not text:
        return ""
    upper = text.upper()
    for tag in ("BUY", "SELL", "HOLD"):
        if tag in upper:
            return tag
    return ""


def _final_state_from_stream(graph: TradingAgentsGraph, init_state: dict[str, Any]) -> dict[str, Any]:
    args = graph.propagator.get_graph_args()
    final: dict[str, Any] = {}
    for chunk in graph.graph.stream(init_state, **args):
        if isinstance(chunk, dict):
            final.update(chunk)
    return final


def run_single_ticker(
    ticker: str,
    trade_date: str,
    config: dict[str, Any],
    selected_analysts: list[str],
) -> dict[str, Any]:
    full_config = {**DEFAULT_CONFIG, **config}
    graph = TradingAgentsGraph(selected_analysts=selected_analysts, config=full_config)
    init_state = graph.propagator.create_initial_state(
        company_name=ticker,
        trade_date=trade_date,
    )
    final = _final_state_from_stream(graph, init_state)
    final_decision = final.get("final_trade_decision") or ""
    signal = _signal_from_decision(final_decision)
    try:
        processed = graph.process_signal(final_decision)
        if isinstance(processed, str) and processed.strip():
            signal = processed.strip().upper()
    except Exception:
        log.exception("process_signal failed for %s", ticker)
    return {
        "ticker": ticker,
        "trade_date": trade_date,
        "final_state": final,
        "signal": signal,
    }


def run_portfolio_scan(
    positions: list[dict[str, Any]],
    trade_date: str,
    config: dict[str, Any],
    selected_analysts: list[str],
    on_progress: ProgressCb = None,
) -> dict[str, Any]:
    per_ticker: list[dict[str, Any]] = []
    n = len(positions)
    for i, pos in enumerate(positions, start=1):
        ticker = pos["symbol"]
        log.info("[portfolio] %d/%d analyzing %s", i, n, ticker)
        if on_progress:
            on_progress({"type": "ticker_start", "i": i, "n": n, "ticker": ticker})
        try:
            result = run_single_ticker(ticker, trade_date, config, selected_analysts)
            result["quantity"] = pos.get("quantity", 0)
            result["market_value"] = pos.get("market_value", 0)
            per_ticker.append(result)
            if on_progress:
                on_progress({
                    "type": "ticker_done",
                    "i": i, "n": n, "ticker": ticker,
                    "signal": result["signal"],
                })
        except Exception as exc:
            log.exception("Per-ticker analysis failed for %s", ticker)
            per_ticker.append({
                "ticker": ticker,
                "quantity": pos.get("quantity", 0),
                "market_value": pos.get("market_value", 0),
                "error": str(exc),
                "signal": "",
                "final_state": {},
            })
            if on_progress:
                on_progress({
                    "type": "ticker_error",
                    "i": i, "n": n, "ticker": ticker, "error": str(exc),
                })
    return {"per_ticker": per_ticker, "trade_date": trade_date}

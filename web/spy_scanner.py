"""S&P 500 scanner: quick screen (all ~500) + deep dive (top 50) + price refresh."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import yfinance as yf
from langchain_openai import ChatOpenAI
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.portfolio_graph import run_single_ticker

from . import db
from .llm_helpers import llm_for

log = logging.getLogger(__name__)

QUICK_SCAN_SYSTEM = (
    "You are a momentum-based equity screener. Given recent price "
    "data for a ticker, output a trading signal and conviction score. "
    "Be brief and decisive.\n"
    "Format your response EXACTLY as:\n"
    "SIGNAL: BUY|HOLD|SELL\n"
    "CONVICTION: 1-10\n"
    "REASONING: one sentence"
)

QUICK_SCAN_USER = (
    "Ticker: {ticker}\n"
    "Current price: {price:.2f}\n"
    "5-day return: {ret5:.1f}%\n"
    "20-day return: {ret20:.1f}%\n"
    "Volume vs 20-day avg: {vol_ratio:.1f}x\n"
    "Sector: {sector}\n"
)

_SIGNAL_RE = re.compile(r"SIGNAL\s*:\s*(BUY|HOLD|SELL)", re.IGNORECASE)
_CONV_RE = re.compile(r"CONVICTION\s*:\s*([1-9]|10)", re.IGNORECASE)
_REASON_RE = re.compile(r"REASONING\s*:\s*(.+)", re.IGNORECASE)


def _llm_quick(config: dict[str, Any]) -> ChatOpenAI:
    return llm_for(config, deep=False, temperature=0.0)


def _parse_quick_response(text: str) -> tuple[str, int, str]:
    signal = "HOLD"
    conviction = 5
    reasoning = ""
    m = _SIGNAL_RE.search(text)
    if m:
        signal = m.group(1).upper()
    m = _CONV_RE.search(text)
    if m:
        conviction = int(m.group(1))
    m = _REASON_RE.search(text)
    if m:
        reasoning = m.group(1).strip()[:500]
    return signal, conviction, reasoning


def _quick_scan_one(
    ticker: str,
    price_data: dict[str, Any],
    sector: str,
    llm: ChatOpenAI,
) -> dict[str, Any]:
    try:
        closes = price_data.get("close", [])
        if len(closes) < 5:
            return {"ticker": ticker, "signal": "HOLD", "conviction": 1,
                    "reasoning": "Insufficient price data.", "entry_price": 0.0}
        price = float(closes[-1])
        ret5 = ((closes[-1] / closes[-5]) - 1) * 100 if len(closes) >= 5 else 0
        ret20 = ((closes[-1] / closes[0]) - 1) * 100 if len(closes) >= 20 else 0
        volumes = price_data.get("volume", [])
        vol_ratio = 1.0
        if len(volumes) >= 20 and volumes[-1] and sum(volumes[-20:]) > 0:
            avg_vol = sum(volumes[-20:-1]) / 19
            vol_ratio = float(volumes[-1]) / avg_vol if avg_vol else 1.0

        prompt = QUICK_SCAN_USER.format(
            ticker=ticker, price=price, ret5=ret5, ret20=ret20,
            vol_ratio=vol_ratio, sector=sector,
        )
        # Retry up to 3 times on 429 rate-limit responses.
        for attempt in range(4):
            try:
                resp = llm.invoke([
                    {"role": "system", "content": QUICK_SCAN_SYSTEM},
                    {"role": "user", "content": prompt},
                ])
                break
            except Exception as e:
                msg = str(e).lower()
                if "429" in msg or "too many" in msg or "rate" in msg:
                    if attempt < 3:
                        import time as _time
                        wait = 5 * (attempt + 1)
                        log.warning("Quick scan 429 for %s, retrying in %ss", ticker, wait)
                        _time.sleep(wait)
                        continue
                raise
        raw = resp.content if hasattr(resp, "content") else str(resp)
        signal, conviction, reasoning = _parse_quick_response(raw)
        return {"ticker": ticker, "signal": signal, "conviction": conviction,
                "reasoning": reasoning, "entry_price": price}
    except Exception as exc:
        log.warning("Quick scan failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "signal": "HOLD", "conviction": 1,
                "reasoning": "scan error: {}".format(exc), "entry_price": 0.0, "error": str(exc)}


def run_quick_scan(
    scan_id: int,
    tickers: list[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fetch bulk price data then score each ticker with a lightweight LLM call."""
    log.info("[spy %s] quick scan: fetching price data for %d tickers", scan_id, len(tickers))
    db.update_spy_scan(scan_id, status="running_quick", quick_total=len(tickers))

    try:
        raw = yf.download(tickers, period="1mo", auto_adjust=True, progress=False, threads=True)
    except Exception as exc:
        log.exception("[spy %s] yfinance bulk download failed: %s", scan_id, exc)
        raw = None

    price_data_map: dict[str, dict[str, list]] = {}
    if raw is not None and not raw.empty:
        if hasattr(raw.columns, "levels"):
            for t in tickers:
                try:
                    closes = raw["Close"][t].dropna().tolist()
                    volumes = raw["Volume"][t].dropna().tolist()
                    price_data_map[t] = {"close": closes, "volume": volumes}
                except (KeyError, TypeError):
                    pass
        else:
            closes = raw["Close"].dropna().tolist()
            volumes = raw["Volume"].dropna().tolist()
            if tickers:
                price_data_map[tickers[0]] = {"close": closes, "volume": volumes}

    llm = _llm_quick(config)
    results: list[dict[str, Any]] = []
    completed = 0

    def _scan_one(t: str) -> dict[str, Any]:
        return _quick_scan_one(t, price_data_map.get(t, {"close": [], "volume": []}), "Unknown", llm)

    # 5 concurrent LLM calls — stays well within Ollama Cloud's rate limit
    # even when a single-ticker analysis is also running in the api container.
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_scan_one, t): t for t in tickers}
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            db.upsert_spy_quick_result(
                scan_id=scan_id,
                ticker=result["ticker"],
                signal=result.get("signal"),
                conviction=result.get("conviction"),
                reasoning=result.get("reasoning"),
                error=result.get("error"),
            )
            completed += 1
            if completed % 50 == 0 or completed == len(tickers):
                db.update_spy_scan(scan_id, quick_count=completed)
                log.info("[spy %s] quick scan %d/%d done", scan_id, completed, len(tickers))

    return results


def run_deep_dives(
    scan_id: int,
    candidates: list[dict[str, Any]],
    trade_date: str,
    config: dict[str, Any],
    selected_analysts: list[str],
) -> list[dict[str, Any]]:
    """Run full TradingAgentsGraph on each candidate."""
    log.info("[spy %s] deep dive on %d tickers", scan_id, len(candidates))
    db.update_spy_scan(scan_id, status="running_deep", deep_total=len(candidates))

    enriched: list[dict[str, Any]] = []
    completed = 0

    def _dive(c: dict[str, Any]) -> dict[str, Any]:
        ticker = c["ticker"]
        analysis_id = db.create_analysis({
            "ticker": ticker,
            "trade_date": trade_date,
            "provider": config.get("llm_provider"),
            "deep_model": config.get("deep_think_llm"),
            "quick_model": config.get("quick_think_llm"),
            "analysts": selected_analysts,
            "research_depth": config.get("max_debate_rounds", 1),
            "language": config.get("output_language", "English"),
        })
        try:
            result = run_single_ticker(ticker, trade_date, config, selected_analysts)
            final_state = result["final_state"]
            signal = result.get("signal") or c.get("signal") or "HOLD"
            db.complete_analysis(analysis_id, final_state, signal)
            db.upsert_spy_quick_result(
                scan_id=scan_id, ticker=ticker, signal=signal,
                conviction=c.get("conviction"), reasoning=c.get("reasoning"),
                analysis_id=analysis_id,
            )
            return {
                **c,
                "signal": signal,
                "analysis_id": analysis_id,
                "final_decision": final_state.get("final_trade_decision", ""),
            }
        except Exception as exc:
            log.exception("[spy %s] deep dive failed for %s", scan_id, ticker)
            db.fail_analysis(analysis_id, str(exc))
            return {**c, "error": str(exc), "analysis_id": analysis_id}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_dive, c): c["ticker"] for c in candidates}
        for fut in as_completed(futures):
            result = fut.result()
            enriched.append(result)
            completed += 1
            db.update_spy_scan(scan_id, deep_count=completed)
            log.info("[spy %s] deep dive %d/%d: %s", scan_id, completed, len(candidates), result["ticker"])

    return enriched


def refresh_portfolio_prices(scan_id: int) -> dict[str, Any]:
    """Fetch current prices for the scan portfolio and compute P&L vs entry prices."""
    scan = db.get_spy_scan(scan_id)
    if not scan:
        return {"error": "scan not found"}
    portfolio = scan.get("portfolio_json")
    if not portfolio:
        return {"error": "no portfolio yet"}

    tickers = [a["ticker"] for a in portfolio]
    try:
        prices_df = yf.download(tickers, period="1d", auto_adjust=True, progress=False)
        if hasattr(prices_df.columns, "levels"):
            current_prices = {
                t: float(prices_df["Close"][t].dropna().iloc[-1])
                for t in tickers
                if t in prices_df["Close"] and not prices_df["Close"][t].dropna().empty
            }
        else:
            current_prices = (
                {tickers[0]: float(prices_df["Close"].dropna().iloc[-1])}
                if tickers and not prices_df.empty else {}
            )
    except Exception as exc:
        log.exception("Price refresh failed for scan %s: %s", scan_id, exc)
        return {"error": str(exc)}

    current_value = 0.0
    signal_flips: list[str] = []
    for a in portfolio:
        t = a["ticker"]
        cp = current_prices.get(t) or a.get("entry_price") or 0
        ep = a.get("entry_price") or cp
        pos_value = (cp / ep) * a.get("dollar_amount", 0) if ep and ep > 0 else a.get("dollar_amount", 0)
        current_value += pos_value

        quick = db.get_spy_quick_result(scan_id, t)
        if quick and quick.get("signal") and a.get("signal"):
            if quick["signal"].upper() != a["signal"].upper():
                signal_flips.append("{}: was {} at entry, now {}".format(t, a["signal"], quick["signal"]))

    return_pct = ((current_value - 100_000) / 100_000) * 100
    rebalance_notes = (
        "Signal flips detected:\n" + "\n".join("- " + f for f in signal_flips)
    ) if signal_flips else ""

    db.update_spy_scan_prices(scan_id=scan_id, current_value=current_value, rebalance_notes=rebalance_notes)
    return {
        "current_value": round(current_value, 2),
        "return_pct": round(return_pct, 2),
        "rebalance_notes": rebalance_notes,
    }

"""S&P 500 scanner: quick screen (all ~500) + deep dive (top 50) + price refresh.

Runs inside the portfolio container, driven by web/portfolio_main._run_spy_scan.

Concurrency is shared CROSS-CONTAINER with the api app's single-ticker
analyses: the api container registers each in-flight analysis as a row in the
SQLite llm_activity table (heartbeat-stamped); _GateMonitor polls that count
every ~3s and resizes a DynamicGate to max(1, TOTAL - active_singles), so
interactive runs always get slots first and the scan floors at one worker
instead of starving. TOTAL comes from OLLAMA_MAX_CONCURRENCY (default 5),
re-read per scan so a value saved in dashboard Settings applies without a
redeploy. Stale activity rows (crashed api runs) age out via the heartbeat TTL
in db.count_active_single, so they can't permanently throttle the scanner.

Cancellation is cooperative: the cancel endpoint sets
spy_scans.cancel_requested=1; workers check it between tickers and raise
ScanCancelled, which the caller records as status 'cancelled' (not 'failed').

Progress: run_quick_scan writes quick_count/quick_total and run_deep_dives
writes deep_count/deep_total on the spy_scans row; the frontend polls those
every 5s for its progress bar.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import yfinance as yf
from langchain_openai import ChatOpenAI

from tradingagents.dataflows import schwab_mcp
from tradingagents.graph.portfolio_graph import run_single_ticker

from . import db
from .llm_helpers import llm_for

log = logging.getLogger(__name__)


class ScanCancelled(Exception):
    """Raised inside a scan worker loop when the user requests cancellation."""


def _total_budget() -> int:
    """Max concurrent LLM calls shared with single-ticker analyses.

    Read at call time so a value set via the Settings UI (OLLAMA_MAX_CONCURRENCY)
    takes effect on the next scan without a redeploy.
    """
    try:
        return max(1, int(os.environ.get("OLLAMA_MAX_CONCURRENCY", "5")))
    except (TypeError, ValueError):
        return 5


class DynamicGate:
    """A resizable concurrency limiter.

    Workers wrap their LLM call in `with gate:`. A monitor thread calls
    set_limit() to shrink/grow the number of permitted concurrent calls.
    Shrinking below the in-flight count is allowed — in-flight calls finish,
    and no new ones are admitted until usage drops below the new limit.
    """

    def __init__(self, limit: int) -> None:
        self._cv = threading.Condition()
        self._limit = max(1, limit)
        self._in_use = 0

    def set_limit(self, n: int) -> None:
        with self._cv:
            self._limit = max(1, n)
            self._cv.notify_all()

    @property
    def limit(self) -> int:
        return self._limit

    def __enter__(self) -> DynamicGate:
        with self._cv:
            while self._in_use >= self._limit:
                self._cv.wait(timeout=1.0)
            self._in_use += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        with self._cv:
            self._in_use -= 1
            self._cv.notify_all()


class _GateMonitor:
    """Background thread that keeps a DynamicGate sized to the live budget.

    scan_limit = max(1, TOTAL - active_single_ticker_analyses)
    so single-ticker analyses always get priority and the scan floors at 1.
    The active count comes from the shared llm_activity table (written by the
    api container, heartbeat TTL pruning), which is what makes the throttling
    work across containers.
    """

    def __init__(self, gate: DynamicGate, poll_seconds: float = 3.0) -> None:
        self._gate = gate
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _recompute(self) -> int:
        total = _total_budget()
        active = 0
        try:
            active = db.count_active_single()
        except Exception:
            log.debug("[gate] count_active_single failed", exc_info=True)
        return max(1, total - active)

    def _run(self) -> None:
        while not self._stop.is_set():
            limit = self._recompute()
            if limit != self._gate.limit:
                log.info("[gate] resizing scan concurrency to %d", limit)
            self._gate.set_limit(limit)
            self._stop.wait(self._poll)

    def __enter__(self) -> DynamicGate:
        # Apply an initial limit synchronously before any work starts.
        self._gate.set_limit(self._recompute())
        self._thread.start()
        return self._gate

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()

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
    """Pull SIGNAL / CONVICTION / REASONING out of the LLM reply.

    Lenient by design: anything off-format degrades to HOLD / 5 / "" instead
    of failing the ticker.
    """
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
    gate: DynamicGate | None = None,
) -> dict[str, Any]:
    """Score one ticker: momentum features -> one cheap LLM call -> parsed signal.

    Never raises — any failure comes back as a HOLD/conviction-1 row with an
    "error" key, so one bad ticker can't sink the scan.
    """
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
        # Retry up to 3 times on 429 rate-limit responses. The dynamic gate
        # caps how many of these LLM calls run at once (shared budget with
        # single-ticker analyses).
        for attempt in range(4):
            try:
                if gate is not None:
                    with gate:
                        resp = llm.invoke([
                            {"role": "system", "content": QUICK_SCAN_SYSTEM},
                            {"role": "user", "content": prompt},
                        ])
                else:
                    resp = llm.invoke([
                        {"role": "system", "content": QUICK_SCAN_SYSTEM},
                        {"role": "user", "content": prompt},
                    ])
                break
            except Exception as e:
                msg = str(e).lower()
                if "429" in msg or "too many" in msg or "rate" in msg:
                    if attempt < 3:
                        wait = 5 * (attempt + 1)
                        log.warning("Quick scan 429 for %s, retrying in %ss", ticker, wait)
                        time.sleep(wait)
                        continue
                raise
        raw = resp.content if hasattr(resp, "content") else str(resp)
        signal, conviction, reasoning = _parse_quick_response(raw)
        return {"ticker": ticker, "signal": signal, "conviction": conviction,
                "reasoning": reasoning, "entry_price": price}
    except Exception as exc:
        log.warning("Quick scan failed for %s: %s", ticker, exc)
        return {"ticker": ticker, "signal": "HOLD", "conviction": 1,
                "reasoning": f"scan error: {exc}", "entry_price": 0.0, "error": str(exc)}


def run_quick_scan(
    scan_id: int,
    tickers: list[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fetch bulk price data then score each ticker with a lightweight LLM call.

    One yf.download covers all ~500 tickers (per-ticker downloads get rate
    limited); tickers missing from the result are scored on empty data and
    come back HOLD/1. quick_count is flushed to the DB every 50 completions,
    and the cancel flag is checked per completed future.
    """
    log.info("[spy %s] quick scan: fetching price data for %d tickers", scan_id, len(tickers))
    db.update_spy_scan(scan_id, status="running_quick", quick_total=len(tickers))

    try:
        raw = yf.download(tickers, period="1mo", auto_adjust=True, progress=False, threads=True)
    except Exception as exc:
        log.exception("[spy %s] yfinance bulk download failed: %s", scan_id, exc)
        raw = None

    price_data_map: dict[str, dict[str, list]] = {}
    if raw is not None and not raw.empty:
        # yfinance returns MultiIndex columns ("Close", ticker) for multi-ticker
        # downloads but flat columns for a single ticker — handle both.
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

    # The thread pool is sized to the max budget; the DynamicGate (resized by
    # the monitor thread) is what actually throttles concurrent LLM calls so
    # single-ticker analyses keep priority. The scan floors at 1 worker.
    budget = _total_budget()

    with _GateMonitor(DynamicGate(budget)) as gate:
        def _scan_one(t: str) -> dict[str, Any]:
            if db.is_spy_scan_cancelled(scan_id):
                return {"ticker": t, "signal": "HOLD", "conviction": 0,
                        "reasoning": "cancelled", "entry_price": 0.0, "skipped": True}
            return _quick_scan_one(
                t, price_data_map.get(t, {"close": [], "volume": []}), "Unknown", llm, gate
            )

        with ThreadPoolExecutor(max_workers=budget) as pool:
            futures = {pool.submit(_scan_one, t): t for t in tickers}
            for fut in as_completed(futures):
                result = fut.result()
                if result.get("skipped"):
                    continue
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

                if db.is_spy_scan_cancelled(scan_id):
                    log.info("[spy %s] cancellation requested — stopping quick scan", scan_id)
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise ScanCancelled()

    return results


def run_deep_dives(
    scan_id: int,
    candidates: list[dict[str, Any]],
    trade_date: str,
    config: dict[str, Any],
    selected_analysts: list[str],
) -> list[dict[str, Any]]:
    """Run the full multi-agent graph on each candidate, under the shared gate.

    Each dive gets its own analyses row (so it shows up in dashboard history);
    success backfills analysis_id + final signal onto the spy quick result.
    A failed dive is returned with an "error" key rather than raised — the
    allocator just sees fewer usable candidates.
    """
    log.info("[spy %s] deep dive on %d tickers", scan_id, len(candidates))
    db.update_spy_scan(scan_id, status="running_deep", deep_total=len(candidates))

    enriched: list[dict[str, Any]] = []
    completed = 0
    budget = _total_budget()

    def _dive(c: dict[str, Any], gate: DynamicGate) -> dict[str, Any]:
        ticker = c["ticker"]
        if db.is_spy_scan_cancelled(scan_id):
            return {**c, "skipped": True}
        with gate:
            return _dive_inner(c, ticker)

    def _dive_inner(c: dict[str, Any], ticker: str) -> dict[str, Any]:
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

    with _GateMonitor(DynamicGate(budget)) as gate:
        with ThreadPoolExecutor(max_workers=budget) as pool:
            futures = {pool.submit(_dive, c, gate): c["ticker"] for c in candidates}
            for fut in as_completed(futures):
                result = fut.result()
                if result.get("skipped"):
                    continue
                enriched.append(result)
                completed += 1
                db.update_spy_scan(scan_id, deep_count=completed)
                log.info("[spy %s] deep dive %d/%d: %s", scan_id, completed, len(candidates), result["ticker"])

                if db.is_spy_scan_cancelled(scan_id):
                    log.info("[spy %s] cancellation requested — stopping deep dives", scan_id)
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise ScanCancelled()

    return enriched


def refresh_portfolio_prices(scan_id: int) -> dict[str, Any]:
    """Mark the scan's paper portfolio to market and persist per-position P&L.

    Prefers one bulk Schwab quote call (real-time); falls back to yfinance
    closes. Also diffs each position's entry signal against its latest
    quick-scan signal and records flips in rebalance_notes — that's the
    signal-flip surface the dashboard and the weekly rebalance read. Called
    hourly on weekdays by the scheduler and once right after a scan completes.
    """
    scan = db.get_spy_scan(scan_id)
    if not scan:
        return {"error": "scan not found"}
    portfolio = scan.get("portfolio_json")
    if not portfolio:
        return {"error": "no portfolio yet"}

    tickers = [a["ticker"] for a in portfolio]

    # Prefer real-time Schwab quotes (one bulk call); fall back to yfinance.
    current_prices: dict[str, float] = {}
    if schwab_mcp.market_data_enabled():
        try:
            quotes = schwab_mcp.get_quotes(tickers)
            if quotes:
                for t in tickers:
                    p = schwab_mcp.quote_price(quotes.get(t, {}))
                    if p:
                        current_prices[t] = p
                if current_prices:
                    log.info("[spy %s] priced %d/%d via Schwab", scan_id, len(current_prices), len(tickers))
        except Exception:
            log.exception("[spy %s] Schwab quotes failed; using yfinance", scan_id)

    if not current_prices:
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

    # Basis = the capital this scan started with (100k for week 1, the prior
    # week's value for a rebalance). Anything not deployed is held as cash.
    basis = float(scan.get("starting_value") or 100_000)

    positions_value = 0.0
    deployed = 0.0
    signal_flips: list[str] = []
    for a in portfolio:
        # Skip closed positions — they hold no capital.
        if a.get("action") == "EXITED":
            continue
        t = a["ticker"]
        ep = float(a.get("entry_price") or 0)
        # Whole shares purchased at entry. Legacy scans (pre whole-share) have
        # no `shares` field — derive it from the dollar target / entry price.
        shares = a.get("shares")
        if shares is None:
            shares = int(float(a.get("dollar_amount") or 0) // ep) if ep > 0 else 0
            a["shares"] = shares
        if shares <= 0:
            continue
        cost = a.get("cost_basis")
        if cost is None:
            cost = round(shares * ep, 2)
            a["cost_basis"] = cost
        deployed += cost

        cp = current_prices.get(t) or ep
        a["current_price"] = round(cp, 2)
        a["current_value"] = round(shares * cp, 2)
        positions_value += a["current_value"]

        quick = db.get_spy_quick_result(scan_id, t)
        if quick and quick.get("signal") and a.get("signal"):
            if quick["signal"].upper() != a["signal"].upper():
                signal_flips.append("{}: was {} at entry, now {}".format(t, a["signal"], quick["signal"]))

    cash = max(0.0, basis - deployed)
    current_value = positions_value + cash
    return_pct = ((current_value - basis) / basis) * 100 if basis else 0.0
    rebalance_notes = (
        "Signal flips detected:\n" + "\n".join("- " + f for f in signal_flips)
    ) if signal_flips else ""

    # Persist the mutated portfolio so per-position current_price/current_value
    # are saved alongside the scan-level value.
    db.update_spy_scan_prices(
        scan_id=scan_id,
        current_value=current_value,
        rebalance_notes=rebalance_notes,
        portfolio_json=portfolio,
    )
    return {
        "current_value": round(current_value, 2),
        "positions_value": round(positions_value, 2),
        "cash": round(cash, 2),
        "deployed": round(deployed, 2),
        "return_pct": round(return_pct, 2),
        "rebalance_notes": rebalance_notes,
    }

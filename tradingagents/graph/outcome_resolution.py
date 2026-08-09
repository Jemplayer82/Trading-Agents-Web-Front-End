"""Standalone outcome-resolution machinery for the trading memory log.

Extracted from ``TradingAgentsGraph`` so the nightly sweep (web/scheduler.py)
can resolve pending decisions for *every* ticker without constructing the full
agent graph. ``TradingAgentsGraph._resolve_pending_entries`` delegates here to
preserve the original same-ticker-on-propagate behaviour.

Correctness rules enforced here (resolutions are irreversible once written):

- **Maturity guard** — an entry only resolves once the full ``holding_days``
  trading-day window has price data. Early resolution would freeze a 1-day
  return into the log as if it were the 5-day outcome.
- **Censoring** — entries older than ``sweep_censor_after_days`` whose price
  series ended early (delisting/halt) resolve with whatever data exists and a
  canned CENSORED reflection, so the worst blowups aren't silently dropped
  from the record (survivorship bias).
- **Date-aligned alpha** — benchmark closes are taken as-of the stock's actual
  start/end dates rather than by positional index, so holiday/calendar
  mismatches can't shift the comparison window.
- **Crypto** — ``-USD`` tickers trade 7 days/week; comparing them to an equity
  index over mismatched calendars produces noise, so they resolve with
  ``alpha == raw`` under the ``absolute`` benchmark sentinel.
- **Noise short-circuit** — outcomes inside the noise band get a canned NOISE
  reflection with no LLM call; single-window moves that small carry no lesson.
  The band is volatility-scaled per ticker when enough price history exists.
- **Shared history slicing** — shared price-history frames are always sliced
  to the exact per-entry window requested by ``fetch_returns``, so batching
  downloads changes only the number of network calls, never a resolved outcome.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Benchmark sentinel for assets with no meaningful equity benchmark (crypto).
ABSOLUTE_BENCHMARK = "absolute"

# Calendar-day lookback fetched *before* the trade date to estimate the
# ticker's 5-day sigma for the volatility-scaled noise band.
_VOL_LOOKBACK_CALENDAR_DAYS = 120
# Minimum daily-return observations required to trust the sigma estimate.
_MIN_VOL_OBSERVATIONS = 20


def resolve_benchmark(ticker: str, config: dict) -> str:
    """Pick the benchmark ticker for alpha calculation against ``ticker``.

    Crypto (``-USD`` suffix) always resolves to the ``absolute`` sentinel —
    even past an explicit ``benchmark_ticker`` override — because a 7-day
    trading calendar cannot be date-aligned with any equity index.
    Otherwise ``config["benchmark_ticker"]`` wins when set, then the suffix
    map, then the empty-suffix default (SPY).

    A ticker that resolves to ITSELF (SPY vs SPY — the options scan deep-dives
    SPY every day) also gets the ``absolute`` sentinel: self-benchmarked alpha
    is identically zero, which would resolve every entry as free NOISE and
    count any Hold rating as a guaranteed calibration hit.
    """
    ticker_upper = ticker.upper()
    if ticker_upper.endswith("-USD"):
        return ABSOLUTE_BENCHMARK
    explicit = config.get("benchmark_ticker")
    if explicit:
        benchmark = explicit
    else:
        benchmark_map = config.get("benchmark_map", {})
        benchmark = None
        for suffix, mapped in benchmark_map.items():
            if suffix and ticker_upper.endswith(suffix.upper()):
                benchmark = mapped
                break
        if benchmark is None:
            benchmark = benchmark_map.get("", "SPY")
    if str(benchmark).upper() == ticker_upper:
        return ABSOLUTE_BENCHMARK
    return benchmark


def _normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    """Strip timezone and time-of-day so rows join cleanly on calendar date."""
    if df.empty:
        df = df.copy()
        df.index = pd.DatetimeIndex([], dtype="datetime64[ns]")
        return df
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df = df.copy()
    df.index = idx.normalize()
    return df


def _stock_window(trade_date: str, holding_days: int) -> tuple[str, str]:
    """Return the (start, end) calendar window for a stock price fetch."""
    start_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start = (start_dt - timedelta(days=_VOL_LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    end = (start_dt + timedelta(days=holding_days + 7)).strftime("%Y-%m-%d")
    return start, end


def _benchmark_window(trade_date: str, holding_days: int) -> tuple[str, str]:
    """Return the (start, end) calendar window for a benchmark price fetch."""
    start_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start = (start_dt - timedelta(days=7)).strftime("%Y-%m-%d")
    end = (start_dt + timedelta(days=holding_days + 7)).strftime("%Y-%m-%d")
    return start, end


class PriceHistoryCache:
    """Shared, lazy price-history cache.

    ``plan()`` unions the windows a sweep will need for each symbol so the
    sweep can download one wide frame per symbol instead of one per entry.
    Downloads are lazy: nothing happens until ``fetch_returns`` actually calls
    ``get()``, so callers that mock ``fetch_returns`` wholesale (e.g.
    TestSweep) keep making zero network calls with no edits at all.

    Frames are always sliced back to the exact per-entry window requested by
    ``get()`` (start-inclusive, end-exclusive, matching yfinance semantics).
    This is correctness-critical: ``fetch_returns`` derives ``sigma_5d``
    from every row before the trade date, uses benchmark ``asof`` lookups,
    and detects halted-then-resumed tickers via the post-window rows. Handing
    it a wider shared frame would change those values and silently
    reclassify entries. Batching therefore changes only the number of network
    calls, never a resolved outcome.
    """

    def __init__(self) -> None:
        self._planned: dict[str, tuple[str, str] | None] = {}
        self._frames: dict[str, pd.DataFrame] = {}
        self._downloads = 0

    def plan(self, symbol: str, start: str, end: str) -> None:
        """Union the future download window for ``symbol``; performs no I/O."""
        existing = self._planned.get(symbol)
        if existing is None:
            self._planned[symbol] = (start, end)
        else:
            p_start, p_end = existing
            # ISO date strings compare correctly lexicographically.
            self._planned[symbol] = (min(p_start, start), max(p_end, end))

    def get(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Return the exact ``[start, end)`` slice, downloading lazily if needed."""
        if symbol not in self._frames:
            # If a window was planned, download the unioned frame; otherwise
            # fetch exactly the requested window.
            dl_start, dl_end = self._planned.get(symbol) or (start, end)
            df = _normalize_history(yf.Ticker(symbol).history(start=dl_start, end=dl_end))
            self._frames[symbol] = df
            self._downloads += 1
        frame = self._frames[symbol]
        # Exact slice: start-inclusive, end-exclusive, matching yfinance.
        return frame[(frame.index >= pd.Timestamp(start)) & (frame.index < pd.Timestamp(end))]

    @property
    def download_count(self) -> int:
        """Number of symbol downloads actually performed."""
        return self._downloads


def fetch_returns(
    ticker: str,
    trade_date: str,
    holding_days: int = 5,
    benchmark: str = "SPY",
    *,
    allow_partial: bool = False,
    history: PriceHistoryCache | None = None,
) -> tuple[float | None, float | None, int | None, float | None]:
    """Forward return, alpha, actual holding days, and 5-day sigma for ``ticker``.

    Returns ``(raw_return, alpha_return, actual_days, sigma_5d)`` or all-``None``
    when the entry cannot be resolved yet (immature window, missing data, or
    network error). ``sigma_5d`` is the trailing 5-day volatility estimate used
    for the noise band; ``None`` when there isn't enough pre-window history.

    ``allow_partial=True`` (censor path) resolves with fewer than
    ``holding_days`` of data when the price series ended early — the caller is
    responsible for gating this on entry age.

    ``history`` may be a shared ``PriceHistoryCache`` so a sweep can collapse
    per-entry downloads into one download per symbol. Slices are exact, so
    per-entry results are identical whether the frame came from the cache or a
    fresh fetch.
    """
    try:
        if history is None:
            history = PriceHistoryCache()

        start_dt = datetime.strptime(trade_date, "%Y-%m-%d")
        stock_start, stock_end = _stock_window(trade_date, holding_days)
        stock = history.get(ticker, stock_start, stock_end)
        if stock.empty:
            return None, None, None, None

        trade_ts = pd.Timestamp(start_dt)
        pre = stock[stock.index < trade_ts]
        post = stock[stock.index >= trade_ts]
        if len(post) < 2:
            return None, None, None, None

        # Maturity guard: require the full window unless the caller explicitly
        # allows a censored partial resolution.
        if len(post) - 1 < holding_days and not allow_partial:
            return None, None, None, None
        actual_days = min(holding_days, len(post) - 1)

        end_ts = post.index[actual_days]
        raw = float(
            (post["Close"].iloc[actual_days] - post["Close"].iloc[0])
            / post["Close"].iloc[0]
        )

        # Trailing 5-day sigma from pre-window daily returns (vol-scaled noise band).
        sigma_5d: float | None = None
        if len(pre) >= _MIN_VOL_OBSERVATIONS + 1:
            daily = pre["Close"].pct_change().dropna()
            if len(daily) >= _MIN_VOL_OBSERVATIONS:
                sigma_5d = float(daily.std() * math.sqrt(5))

        if benchmark == ABSOLUTE_BENCHMARK:
            return raw, raw, actual_days, sigma_5d

        bench_start, bench_end = _benchmark_window(trade_date, holding_days)
        bench = history.get(benchmark, bench_start, bench_end)
        if bench.empty or bench.index[-1] < end_ts:
            # Benchmark data hasn't caught up to the stock's end date yet.
            return None, None, None, None

        # Date-aligned closes: as-of lookups tolerate benchmark holidays by
        # falling back to the most recent prior close.
        bench_start_close = bench["Close"].asof(post.index[0])
        bench_end_close = bench["Close"].asof(end_ts)
        if pd.isna(bench_start_close) or pd.isna(bench_end_close):
            return None, None, None, None

        bench_ret = float((bench_end_close - bench_start_close) / bench_start_close)
        alpha = raw - bench_ret
        return raw, alpha, actual_days, sigma_5d
    except Exception as e:
        logger.warning(
            "Could not resolve outcome for %s on %s vs %s (will retry next run): %s",
            ticker, trade_date, benchmark, e,
        )
        return None, None, None, None


def noise_band(sigma_5d: float | None, config: dict) -> float:
    """Volatility-scaled noise band, clamped; fixed fallback without sigma."""
    fallback = config.get("noise_alpha_threshold", 0.02)
    if sigma_5d is None or sigma_5d <= 0:
        return fallback
    frac = config.get("noise_band_sigma_frac", 0.5)
    lo = config.get("noise_band_min", 0.015)
    hi = config.get("noise_band_max", 0.06)
    return min(max(sigma_5d * frac, lo), hi)


def _noise_reflection(alpha: float, band: float, actual_days: int) -> str:
    return (
        f"CLASS: NOISE | HORIZON: {actual_days}d | EVIDENCE: none\n"
        f"CALL: alpha {alpha:+.1%} inside the +/-{band:.1%} noise band — uninformative\n"
        "THESIS: not evaluated — move within noise\n"
        "LESSON: none — outcome uninformative\n"
        "CONFIDENCE: low"
    )


def _censored_reflection(actual_days: int, holding_days: int, age_days: int) -> str:
    return (
        f"CLASS: CENSORED | HORIZON: {actual_days}d of {holding_days}d | EVIDENCE: none\n"
        f"CALL: price series ended after {actual_days}d (entry {age_days}d old) — probable delisting/halt\n"
        "THESIS: not evaluated — window censored\n"
        "LESSON: none — outcome censored\n"
        "CONFIDENCE: low"
    )


def _fetch_news_context(ticker: str, start_date: str, end_date: str, limit: int = 8) -> str:
    """Best-effort holding-window news pull for the reflector.

    Uses the vendor router (yfinance by default — free/unlimited). yfinance
    only serves recent articles, so stale windows legitimately come back
    empty; the reflection prompt treats empty context as "no evidence" and
    forbids exogenous-surprise claims on it.
    """
    try:
        from tradingagents.dataflows.interface import route_to_vendor

        news = route_to_vendor("get_news", ticker, start_date, end_date)
        if isinstance(news, dict):
            import json

            news = json.dumps(news)
        news = (news or "").strip()
        if not news or "No news found" in news:
            return ""
        # Keep the prompt bounded — a handful of headlines is enough evidence.
        lines = news.splitlines()
        if len(lines) > limit * 6:
            news = "\n".join(lines[: limit * 6])
        return news
    except Exception as e:
        logger.warning("News context fetch failed for %s (%s..%s): %s", ticker, start_date, end_date, e)
        return ""


def resolve_all_pending(
    memory_log,
    reflector,
    config: dict,
    ticker: str | None = None,
    max_reflections: int | None = None,
) -> dict:
    """Resolve matured pending memory-log entries, optionally for one ticker.

    Groups pending entries by ticker, isolates failures per entry, and writes
    all updates in a single atomic batch. Price histories are downloaded in
    bulk: one download per distinct ticker and one per distinct benchmark per
    sweep. ``PriceHistoryCache`` always slices the shared frame back to the
    exact per-entry window requested by ``fetch_returns``, so batching changes
    only the number of network calls, never a resolved outcome. Entries
    younger than ``holding_days`` calendar days are counted as ``immature``
    with no network call at all (unless they have already entered the censor
    path, i.e. are older than ``sweep_censor_after_days``).

    LLM reflections are capped by ``max_reflections`` (canned NOISE/CENSORED
    reflections are free and never capped); entries skipped for budget stay
    pending for the next run. ``reflector=None`` is treated as a zero LLM
    budget — NOISE/CENSORED entries still resolve, so a missing/misconfigured
    LLM key degrades the sweep instead of killing it.

    Returns summary counts for logging:
    ``{"resolved", "noise", "censored", "immature", "budget_deferred", "errors",
    "llm_reflections"}``.
    """
    holding_days = config.get("reflection_holding_days", 5)
    censor_after = config.get("sweep_censor_after_days", 30)
    summary = {
        "resolved": 0, "noise": 0, "censored": 0, "immature": 0,
        "budget_deferred": 0, "errors": 0, "llm_reflections": 0,
    }

    pending = memory_log.get_pending_entries()
    if ticker is not None:
        pending = [e for e in pending if e["ticker"] == ticker]
    if not pending:
        return summary

    by_ticker: dict[str, list[dict]] = {}
    for entry in pending:
        by_ticker.setdefault(entry["ticker"], []).append(entry)

    today = datetime.now()
    updates: list[dict] = []
    survivors: list[tuple] = []

    # -----------------------------------------------------------------------
    # PHASE 1 -- AGE GUARD (before any I/O)
    # -----------------------------------------------------------------------
    for tkr, entries in by_ticker.items():
        benchmark = resolve_benchmark(tkr, config)
        for entry in entries:
            try:
                trade_date = entry["date"]
                trade_dt = datetime.strptime(trade_date, "%Y-%m-%d")
                age_days = (today - trade_dt).days
                allow_partial = age_days > censor_after
            except Exception:
                summary["errors"] += 1
                logger.exception("Sweep failed for %s on %s -- continuing", tkr, entry.get("date"))
                continue

            # Age guard: fetch_returns requires len(post) - 1 >= holding_days,
            # i.e. at least holding_days + 1 distinct trading-day bars
            # starting at the trade date. Bars fall on distinct calendar
            # days, so the (holding_days+1)-th bar is never earlier than
            # trade_date + holding_days CALENDAR days. Skipping here is
            # therefore strictly conservative -- it can never skip an entry
            # that would actually have resolved -- and it saves two network
            # calls each. The censor path (allow_partial) resolves on a short
            # series and is exempt.
            if not allow_partial and age_days < holding_days:
                summary["immature"] += 1
                continue

            survivors.append((tkr, entry, trade_date, trade_dt, age_days, allow_partial, benchmark))

    # -----------------------------------------------------------------------
    # PHASE 2 -- PLAN THE DOWNLOADS (still no I/O)
    # -----------------------------------------------------------------------
    history = PriceHistoryCache()
    for tkr, _entry, trade_date, _trade_dt, _age_days, _allow_partial, benchmark in survivors:
        history.plan(tkr, *_stock_window(trade_date, holding_days))
        if benchmark != ABSOLUTE_BENCHMARK:
            history.plan(benchmark, *_benchmark_window(trade_date, holding_days))

    # -----------------------------------------------------------------------
    # PHASE 3 -- RESOLVE
    # -----------------------------------------------------------------------
    for tkr, entry, trade_date, trade_dt, age_days, allow_partial, benchmark in survivors:
        try:
            raw, alpha, actual_days, sigma_5d = fetch_returns(
                tkr, trade_date, holding_days, benchmark,
                allow_partial=allow_partial, history=history,
            )
            if raw is None:
                # Immature window, benchmark lag, or (for old entries) a
                # series with <2 rows — nothing safe to write yet.
                summary["immature"] += 1
                continue

            band = noise_band(sigma_5d, config)
            censored = allow_partial and actual_days < holding_days
            benchmark_label = "none (absolute return)" if benchmark == ABSOLUTE_BENCHMARK else benchmark

            if censored:
                reflection = _censored_reflection(actual_days, holding_days, age_days)
                summary["censored"] += 1
            elif abs(alpha) < band:
                reflection = _noise_reflection(alpha, band, actual_days)
                summary["noise"] += 1
            else:
                if reflector is None or (
                    max_reflections is not None
                    and summary["llm_reflections"] >= max_reflections
                ):
                    summary["budget_deferred"] += 1
                    continue
                window_end = (
                    trade_dt + timedelta(days=holding_days + 7)
                ).strftime("%Y-%m-%d")
                news_context = _fetch_news_context(tkr, trade_date, window_end)
                reflection = reflector.reflect_on_final_decision(
                    final_decision=entry.get("decision", ""),
                    raw_return=raw,
                    alpha_return=alpha,
                    benchmark_name=benchmark_label,
                    rating=entry.get("rating", "Unrated"),
                    holding_days=actual_days,
                    noise_pct=band,
                    news_context=news_context,
                )
                summary["llm_reflections"] += 1

            updates.append({
                "ticker": tkr,
                "trade_date": trade_date,
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": actual_days,
                "reflection": reflection,
            })
            summary["resolved"] += 1
        except Exception:
            summary["errors"] += 1
            logger.exception("Sweep failed for %s on %s -- continuing", tkr, entry.get("date"))

    logger.info("Sweep fetched %d symbol histories for %d entries", history.download_count, len(survivors))

    if updates:
        memory_log.batch_update_with_outcomes(updates)
    return summary

"""Tests for the outcome-resolution sweep, mechanical scoring, and calibration stats.

Follows the patterns in test_memory_log.py: tmp_path-backed TradingMemoryLog,
patched yfinance.Ticker returning DatetimeIndex DataFrames, MagicMock LLM.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.agents.utils.calibration import compute_calibration, format_calibration
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.scoring import HIT, MISS, UNINFORMATIVE, score_outcome
from tradingagents.graph.outcome_resolution import (
    ABSOLUTE_BENCHMARK,
    fetch_returns,
    noise_band,
    resolve_all_pending,
    resolve_benchmark,
)

DECISION_BUY = "Rating: Buy\n\nStrong momentum and expanding margins."
DECISION_SELL = "Rating: Sell\n\nDeteriorating fundamentals. Exit position immediately."


def make_log(tmp_path, filename="trading_memory.md"):
    return TradingMemoryLog({"memory_log_path": str(tmp_path / filename)})


def _price_df(prices, start="2026-01-05"):
    idx = pd.bdate_range(start=start, periods=len(prices))
    return pd.DataFrame({"Close": prices}, index=idx)


def _recent_date(days_back: int) -> str:
    return (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")


def _patch_tickers(price_map):
    """patch yfinance.Ticker; price_map maps symbol -> DataFrame."""
    def _make(sym):
        m = MagicMock()
        m.history.return_value = price_map.get(sym, pd.DataFrame({"Close": []}))
        return m
    p = patch("yfinance.Ticker")
    return p, _make


# ---------------------------------------------------------------------------
# fetch_returns: maturity guard, date alignment, crypto
# ---------------------------------------------------------------------------

class TestFetchReturns:

    def test_maturity_guard_defers_short_window(self):
        """4 post-window rows with holding_days=5 → all None (retry later)."""
        p, make = _patch_tickers({
            "NVDA": _price_df([100.0, 102.0, 104.0, 103.0]),
            "SPY": _price_df([400.0, 402.0, 404.0, 403.0]),
        })
        with p as cls:
            cls.side_effect = make
            raw, alpha, days, sigma = fetch_returns("NVDA", "2026-01-05", 5, "SPY")
        assert (raw, alpha, days, sigma) == (None, None, None, None)

    def test_full_window_resolves_at_holding_days(self):
        p, make = _patch_tickers({
            "NVDA": _price_df([100.0, 102.0, 104.0, 103.0, 105.0, 106.0]),
            "SPY": _price_df([400.0, 402.0, 404.0, 403.0, 405.0, 406.0]),
        })
        with p as cls:
            cls.side_effect = make
            raw, alpha, days, sigma = fetch_returns("NVDA", "2026-01-05", 5, "SPY")
        assert days == 5
        assert raw == pytest.approx(0.06)
        assert alpha == pytest.approx(0.06 - 0.015)

    def test_allow_partial_resolves_censored_window(self):
        """allow_partial (censor path) resolves with fewer than holding_days."""
        p, make = _patch_tickers({
            "NVDA": _price_df([100.0, 102.0, 104.0]),
            "SPY": _price_df([400.0, 402.0, 404.0]),
        })
        with p as cls:
            cls.side_effect = make
            raw, alpha, days, sigma = fetch_returns(
                "NVDA", "2026-01-05", 5, "SPY", allow_partial=True,
            )
        assert days == 2
        assert raw == pytest.approx(0.04)

    def test_date_aligned_alpha_with_stock_holiday(self):
        """Stock missing a day the benchmark trades: closes align by date, not position."""
        stock_idx = pd.to_datetime([
            "2026-01-05", "2026-01-06", "2026-01-08", "2026-01-09",
            "2026-01-12", "2026-01-13",
        ])
        stock = pd.DataFrame({"Close": [100.0, 101.0, 102.0, 103.0, 104.0, 110.0]}, index=stock_idx)
        spy = _price_df([400.0, 402.0, 404.0, 403.0, 405.0, 406.0, 408.0])  # trades Jan 7 too
        p, make = _patch_tickers({"NVDA": stock, "SPY": spy})
        with p as cls:
            cls.side_effect = make
            raw, alpha, days, sigma = fetch_returns("NVDA", "2026-01-05", 5, "SPY")
        assert days == 5
        assert raw == pytest.approx(0.10)
        # Stock's 5th trading day is Jan 13; SPY close asof Jan 13 is 408.
        assert alpha == pytest.approx(0.10 - (408.0 - 400.0) / 400.0)

    def test_crypto_absolute_alpha_equals_raw(self):
        p, make = _patch_tickers({
            "BTC-USD": _price_df([50000.0, 51000.0, 52000.0, 51500.0, 53000.0, 54000.0]),
        })
        with p as cls:
            cls.side_effect = make
            raw, alpha, days, sigma = fetch_returns(
                "BTC-USD", "2026-01-05", 5, ABSOLUTE_BENCHMARK,
            )
        assert raw == pytest.approx(0.08)
        assert alpha == raw

    def test_sigma_computed_from_pre_window_history(self):
        """With >=20 pre-window rows, sigma_5d is a positive float."""
        pre = [100.0 + (i % 3) for i in range(30)]  # oscillating history
        post = [103.0, 104.0, 105.0, 106.0, 107.0, 108.0]
        df = _price_df(pre + post, start="2025-11-20")
        trade_date = df.index[30].strftime("%Y-%m-%d")
        spy = _price_df([400.0] * 36, start="2025-11-20")
        p, make = _patch_tickers({"NVDA": df, "SPY": spy})
        with p as cls:
            cls.side_effect = make
            raw, alpha, days, sigma = fetch_returns("NVDA", trade_date, 5, "SPY")
        assert sigma is not None and sigma > 0


class TestResolveBenchmark:

    def test_crypto_overrides_explicit_benchmark(self):
        cfg = {"benchmark_ticker": "QQQ", "benchmark_map": {"": "SPY"}}
        assert resolve_benchmark("BTC-USD", cfg) == ABSOLUTE_BENCHMARK

    def test_equity_paths_unchanged(self):
        cfg = {"benchmark_ticker": None, "benchmark_map": {".T": "^N225", "": "SPY"}}
        assert resolve_benchmark("7203.T", cfg) == "^N225"
        assert resolve_benchmark("NVDA", cfg) == "SPY"


class TestNoiseBand:

    def test_fallback_without_sigma(self):
        assert noise_band(None, {"noise_alpha_threshold": 0.02}) == 0.02

    def test_vol_scaled_and_clamped(self):
        cfg = {"noise_band_sigma_frac": 0.5, "noise_band_min": 0.015, "noise_band_max": 0.06}
        assert noise_band(0.04, cfg) == pytest.approx(0.02)   # 0.5 * 4%
        assert noise_band(0.20, cfg) == pytest.approx(0.06)   # clamped high (crypto-vol)
        assert noise_band(0.01, cfg) == pytest.approx(0.015)  # clamped low


# ---------------------------------------------------------------------------
# resolve_all_pending: the sweep
# ---------------------------------------------------------------------------

class TestSweep:

    def test_multi_ticker_all_resolve(self, tmp_path):
        """Entries across tickers resolve in one sweep call (no ticker filter)."""
        log = make_log(tmp_path)
        d1, d2 = _recent_date(20), _recent_date(15)
        log.store_decision("NVDA", d1, DECISION_BUY)
        log.store_decision("AAPL", d2, DECISION_SELL)
        reflector = MagicMock()
        reflector.reflect_on_final_decision.return_value = "CLASS: CONFIRMED-THESIS\nCALL: ok"
        with patch(
            "tradingagents.graph.outcome_resolution.fetch_returns",
            return_value=(0.08, 0.06, 5, None),
        ), patch(
            "tradingagents.graph.outcome_resolution._fetch_news_context",
            return_value="",
        ):
            summary = resolve_all_pending(log, reflector, {})
        assert summary["resolved"] == 2
        assert log.get_pending_entries() == []

    def test_one_bad_ticker_does_not_kill_sweep(self, tmp_path):
        log = make_log(tmp_path)
        d = _recent_date(20)
        log.store_decision("BAD", d, DECISION_BUY)
        log.store_decision("GOOD", d, DECISION_BUY)
        reflector = MagicMock()
        reflector.reflect_on_final_decision.return_value = "CLASS: CONFIRMED-THESIS"

        def _fetch(ticker, *a, **kw):
            if ticker == "BAD":
                raise RuntimeError("boom")
            return (0.08, 0.06, 5, None)

        with patch(
            "tradingagents.graph.outcome_resolution.fetch_returns", side_effect=_fetch,
        ), patch(
            "tradingagents.graph.outcome_resolution._fetch_news_context",
            return_value="",
        ):
            summary = resolve_all_pending(log, reflector, {})
        assert summary["resolved"] == 1
        assert summary["errors"] == 1
        assert len(log.get_pending_entries()) == 1  # BAD stays pending

    def test_noise_short_circuit_skips_llm(self, tmp_path):
        """|alpha| inside the band → canned NOISE reflection, no LLM call."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", _recent_date(20), DECISION_BUY)
        reflector = MagicMock()
        with patch(
            "tradingagents.graph.outcome_resolution.fetch_returns",
            return_value=(0.01, 0.005, 5, None),
        ):
            summary = resolve_all_pending(log, reflector, {})
        reflector.reflect_on_final_decision.assert_not_called()
        assert summary["noise"] == 1
        entry = log.load_entries()[0]
        assert entry["pending"] is False
        assert entry["reflection"].startswith("CLASS: NOISE")

    def test_llm_budget_defers_excess(self, tmp_path):
        """max_reflections=1: second non-noise entry stays pending for next run."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", _recent_date(20), DECISION_BUY)
        log.store_decision("AAPL", _recent_date(15), DECISION_SELL)
        reflector = MagicMock()
        reflector.reflect_on_final_decision.return_value = "CLASS: CONFIRMED-THESIS"
        with patch(
            "tradingagents.graph.outcome_resolution.fetch_returns",
            return_value=(0.08, 0.06, 5, None),
        ), patch(
            "tradingagents.graph.outcome_resolution._fetch_news_context",
            return_value="",
        ):
            summary = resolve_all_pending(log, reflector, {}, max_reflections=1)
        assert summary["llm_reflections"] == 1
        assert summary["budget_deferred"] == 1
        assert len(log.get_pending_entries()) == 1

    def test_censored_old_entry_no_llm(self, tmp_path):
        """Entry older than censor window with a short series → CENSORED, no LLM."""
        log = make_log(tmp_path)
        log.store_decision("GONE", _recent_date(60), DECISION_BUY)
        reflector = MagicMock()
        with patch(
            "tradingagents.graph.outcome_resolution.fetch_returns",
            return_value=(-0.40, -0.42, 2, None),  # series ended after 2 days
        ):
            summary = resolve_all_pending(log, reflector, {"sweep_censor_after_days": 30})
        reflector.reflect_on_final_decision.assert_not_called()
        assert summary["censored"] == 1
        entry = log.load_entries()[0]
        assert entry["reflection"].startswith("CLASS: CENSORED")

    def test_none_reflector_defers_llm_entries_resolves_noise(self, tmp_path):
        """reflector=None (LLM key missing): noise resolves, LLM-graded defers."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", _recent_date(20), DECISION_BUY)   # big alpha → LLM path
        log.store_decision("AAPL", _recent_date(15), DECISION_SELL)  # noise path

        def _fetch(ticker, *a, **kw):
            return (0.08, 0.06, 5, None) if ticker == "NVDA" else (0.01, 0.005, 5, None)

        with patch(
            "tradingagents.graph.outcome_resolution.fetch_returns", side_effect=_fetch,
        ):
            summary = resolve_all_pending(log, None, {})
        assert summary["noise"] == 1
        assert summary["budget_deferred"] == 1
        assert len(log.get_pending_entries()) == 1

    def test_immature_entry_stays_pending(self, tmp_path):
        log = make_log(tmp_path)
        log.store_decision("NVDA", _recent_date(2), DECISION_BUY)
        reflector = MagicMock()
        with patch(
            "tradingagents.graph.outcome_resolution.fetch_returns",
            return_value=(None, None, None, None),
        ):
            summary = resolve_all_pending(log, reflector, {})
        assert summary["immature"] == 1
        assert len(log.get_pending_entries()) == 1


# ---------------------------------------------------------------------------
# store_decision idempotency across resolution
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_no_duplicate_after_resolution(self, tmp_path):
        """Re-running a (ticker, date) whose entry already resolved must not re-append."""
        log = make_log(tmp_path)
        d = _recent_date(20)
        log.store_decision("NVDA", d, DECISION_BUY)
        log.update_with_outcome("NVDA", d, 0.05, 0.03, 5, "CLASS: CONFIRMED-THESIS")
        log.store_decision("NVDA", d, DECISION_BUY)  # would previously duplicate
        assert len(log.load_entries()) == 1


# ---------------------------------------------------------------------------
# Mechanical scoring
# ---------------------------------------------------------------------------

class TestScoring:

    BAND = 0.02

    @pytest.mark.parametrize("rating", ["Buy", "Overweight"])
    def test_bullish(self, rating):
        assert score_outcome(rating, 0.05, self.BAND) == HIT
        assert score_outcome(rating, -0.05, self.BAND) == MISS
        assert score_outcome(rating, 0.01, self.BAND) == UNINFORMATIVE

    @pytest.mark.parametrize("rating", ["Sell", "Underweight"])
    def test_bearish(self, rating):
        assert score_outcome(rating, -0.05, self.BAND) == HIT
        assert score_outcome(rating, 0.05, self.BAND) == MISS
        assert score_outcome(rating, -0.01, self.BAND) == UNINFORMATIVE

    def test_hold(self):
        assert score_outcome("Hold", 0.01, self.BAND) == HIT
        assert score_outcome("Hold", -0.01, self.BAND) == HIT
        assert score_outcome("Hold", 0.05, self.BAND) == MISS

    def test_unrated_excluded(self):
        assert score_outcome("Unrated", 0.05, self.BAND) is None


# ---------------------------------------------------------------------------
# Calibration stats + context injection
# ---------------------------------------------------------------------------

def _resolved_entry(ticker, date, rating, alpha_pct, reflection="CLASS: CONFIRMED-THESIS\nLESSON: x"):
    return {
        "date": date, "ticker": ticker, "rating": rating, "pending": False,
        "raw": alpha_pct, "alpha": alpha_pct, "holding": "5d",
        "decision": "Rating: %s" % rating, "reflection": reflection,
    }


class TestCalibration:

    def test_counts_and_template(self):
        entries = [
            _resolved_entry("NVDA", "2026-06-01", "Buy", "+5.0%"),
            _resolved_entry("AAPL", "2026-06-02", "Buy", "-4.0%",
                            reflection="CLASS: FORESEEABLE-MISS\nLESSON: y"),
            _resolved_entry("MSFT", "2026-06-03", "Sell", "-6.0%"),
        ]
        stats = compute_calibration(entries, {})
        assert stats["n_total"] == 3
        assert stats["per_rating"]["Buy"]["hit"] == 1
        assert stats["per_rating"]["Buy"]["miss"] == 1
        assert stats["per_rating"]["Sell"]["hit"] == 1
        text = format_calibration(stats)
        assert "CALIBRATION — 3 resolved decisions" in text
        assert "[SMALL SAMPLE — ignore]" in text  # n < 10 everywhere
        assert "CAUTION" in text

    def test_exogenous_skew_warning(self):
        entries = [
            _resolved_entry("T%d" % i, "2026-06-01", "Buy", "-5.0%",
                            reflection="CLASS: EXOGENOUS-SURPRISE\nLESSON: excuse")
            for i in range(5)
        ]
        text = format_calibration(compute_calibration(entries, {}))
        assert "self-excuse bias" in text

    def test_crypto_and_unrated_excluded(self):
        entries = [
            _resolved_entry("BTC-USD", "2026-06-01", "Buy", "+9.0%"),
            _resolved_entry("NVDA", "2026-06-02", "Unrated", "+5.0%"),
            _resolved_entry("AAPL", "2026-06-03", "Buy", "+5.0%"),
        ]
        stats = compute_calibration(entries, {})
        assert stats["n_total"] == 1
        assert stats["n_crypto"] == 1
        assert stats["n_unrated"] == 1

    def test_censored_counted_not_scored(self):
        entries = [
            _resolved_entry("GONE", "2026-06-01", "Buy", "-40.0%",
                            reflection="CLASS: CENSORED\nLESSON: none"),
            _resolved_entry("AAPL", "2026-06-03", "Buy", "+5.0%"),
        ]
        stats = compute_calibration(entries, {})
        assert stats["n_total"] == 1
        assert stats["n_censored"] == 1

    def test_empty_log_renders_nothing(self):
        assert format_calibration(compute_calibration([], {})) == ""


class TestContextInjection:

    def test_calibration_block_precedes_anecdotes(self, tmp_path):
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "m.md"),
            "noise_alpha_threshold": 0.02,
        })
        d = _recent_date(20)
        log.store_decision("NVDA", d, DECISION_BUY)
        log.update_with_outcome("NVDA", d, 0.05, 0.04, 5, "CLASS: CONFIRMED-THESIS\nLESSON: watch")
        ctx = log.get_past_context("NVDA")
        assert ctx.index("CALIBRATION") < ctx.index("Past analyses of NVDA")

    def test_cross_ticker_slots_unique_tickers(self, tmp_path):
        """Three resolved entries of one other ticker fill only one cross slot."""
        log = make_log(tmp_path)
        for i, d in enumerate([_recent_date(30), _recent_date(25), _recent_date(20)]):
            log.store_decision("AAPL", d, DECISION_BUY)
            log.update_with_outcome("AAPL", d, 0.05, 0.04, 5, "CLASS: CONFIRMED-THESIS\nLESSON: l%d" % i)
        d2 = _recent_date(15)
        log.store_decision("MSFT", d2, DECISION_BUY)
        log.update_with_outcome("MSFT", d2, 0.05, 0.04, 5, "CLASS: CONFIRMED-THESIS\nLESSON: m")
        ctx = log.get_past_context("NVDA")
        assert ctx.count("| AAPL |") == 1
        assert ctx.count("| MSFT |") == 1

    def test_recency_cutoff_expires_old_lessons(self, tmp_path):
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "m.md"),
            "memory_context_max_age_days": 180,
        })
        log.store_decision("OLD", "2020-01-05", DECISION_BUY)
        log.update_with_outcome("OLD", "2020-01-05", 0.05, 0.04, 5, "CLASS: CONFIRMED-THESIS\nLESSON: stale")
        d = _recent_date(20)
        log.store_decision("NEW", d, DECISION_BUY)
        log.update_with_outcome("NEW", d, 0.05, 0.04, 5, "CLASS: CONFIRMED-THESIS\nLESSON: fresh")
        ctx = log.get_past_context("NVDA")
        assert "| NEW |" in ctx
        assert "| OLD |" not in ctx

    def test_unreflected_entry_not_leaked_into_cross_slots(self, tmp_path):
        """Resolved entry with empty reflection is skipped, not shown as decision prose."""
        log = make_log(tmp_path)
        d = _recent_date(20)
        log.store_decision("AAPL", d, DECISION_BUY)
        log.update_with_outcome("AAPL", d, 0.05, 0.04, 5, "")
        ctx = log.get_past_context("NVDA")
        assert "Strong momentum" not in ctx


# ---------------------------------------------------------------------------
# Reflector prompt contract
# ---------------------------------------------------------------------------

class TestReflectorPrompt:

    def test_prompt_contains_classification_and_evidence_rules(self):
        from tradingagents.graph.reflection import Reflector

        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "CLASS: NOISE"
        r = Reflector(mock_llm)
        r.reflect_on_final_decision(
            final_decision=DECISION_BUY, raw_return=0.08, alpha_return=0.06,
            benchmark_name="SPY", rating="Buy", holding_days=5,
            noise_pct=0.02, news_context="",
        )
        messages = mock_llm.invoke.call_args[0][0]
        system = next(c for role, c in messages if role == "system")
        human = next(c for role, c in messages if role == "human")
        assert "EXOGENOUS-SURPRISE requires citing a specific headline" in system
        assert "CONFIRMED-THESIS" in system
        assert "watch for" in system
        assert "(no news retrieved for this window)" in human
        assert "+8.0%" in human
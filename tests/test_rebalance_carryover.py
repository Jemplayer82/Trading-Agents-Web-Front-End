"""Unit tests for stop-loss / weekly-rebalance EXITED row carryover.

These tests exercise the allocator's canonical live/stopped filters and the
fallback rebalance path. They intentionally do NOT hit the database or any
LLM; they prove that a ticker stopped out mid-week is treated as a fresh
candidate in the next rebalance, while still-live positions keep their
original cost basis.
"""

import pytest

from web import spy_allocator

pytestmark = pytest.mark.unit


def test_live_positions_filters_exited_and_preserves_order():
    portfolio = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 100.0},
        {"ticker": "MSFT", "action": "EXITED", "entry_price": 200.0},
        {"ticker": "GOOGL", "action": "NEW", "entry_price": 300.0},
        {"ticker": "AMZN", "action": "EXITED", "entry_price": 400.0},
    ]
    live = spy_allocator.live_positions(portfolio)
    assert live == [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 100.0},
        {"ticker": "GOOGL", "action": "NEW", "entry_price": 300.0},
    ]
    assert spy_allocator.live_positions(None) == []
    assert spy_allocator.live_positions([]) == []


def test_stopped_positions_selects_only_stop_reasons():
    portfolio = [
        {"ticker": "AAPL", "action": "EXITED", "exit_reason": "stop_loss"},
        {"ticker": "MSFT", "action": "EXITED", "exit_reason": "trail_stop"},
        {"ticker": "GOOGL", "action": "EXITED", "exit_reason": "stop_limit"},
        {"ticker": "AMZN", "action": "EXITED"},  # weekly rebalance exit — no reason
        {"ticker": "META", "action": "HOLD"},  # still live
    ]
    stopped = spy_allocator.stopped_positions(portfolio)
    assert [p["ticker"] for p in stopped] == ["AAPL", "MSFT", "GOOGL"]


def test_fallback_rebalance_stopped_out_then_recandidated_is_new():
    previous = [
        {
            "ticker": "AAPL",
            "action": "EXITED",
            "exit_reason": "stop_loss",
            "entry_price": 100.0,
            "exit_price": 90.0,
            "dollar_amount": 0,
        },
    ]
    candidates = [
        {"ticker": "AAPL", "entry_price": 95.0, "signal": "BUY", "conviction": 8},
    ]
    result = spy_allocator._fallback_rebalance(candidates, previous, 100_000.0)

    aapl_rows = [r for r in result if r["ticker"] == "AAPL"]
    assert len(aapl_rows) == 1
    assert aapl_rows[0]["action"] == "NEW"
    assert aapl_rows[0]["entry_price"] == 95.0


def test_fallback_rebalance_no_duplicate_exited_for_already_exited():
    previous = [
        {"ticker": "MSFT", "action": "HOLD", "entry_price": 200.0, "dollar_amount": 5000},
        {"ticker": "TSLA", "action": "EXITED", "entry_price": 300.0, "dollar_amount": 0},
    ]
    candidates = [
        {"ticker": "MSFT", "entry_price": 220.0, "signal": "BUY", "conviction": 7},
    ]
    result = spy_allocator._fallback_rebalance(candidates, previous, 100_000.0)

    msft_rows = [r for r in result if r["ticker"] == "MSFT"]
    tsla_rows = [r for r in result if r["ticker"] == "TSLA"]
    assert len(msft_rows) == 1
    assert msft_rows[0]["action"] == "HOLD"
    assert tsla_rows == []


def test_fallback_rebalance_live_position_still_hold_at_original_entry():
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 100.0, "dollar_amount": 5000},
    ]
    candidates = [
        {"ticker": "AAPL", "entry_price": 110.0, "signal": "BUY", "conviction": 8},
    ]
    result = spy_allocator._fallback_rebalance(candidates, previous, 100_000.0)

    aapl_rows = [r for r in result if r["ticker"] == "AAPL"]
    assert len(aapl_rows) == 1
    assert aapl_rows[0]["action"] == "HOLD"
    assert aapl_rows[0]["entry_price"] == 100.0
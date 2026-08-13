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


def test_carry_forward_state_keeps_peak_price_for_hold():
    previous = [
        {"ticker": "MSFT", "action": "HOLD", "entry_price": 200.0, "peak_price": 260.0},
    ]
    allocations = [
        {
            "ticker": "MSFT",
            "action": "HOLD",
            "allocation_pct": 5.0,
            "dollar_amount": 5000.0,
            "entry_price": 0.0,
            "rationale": "keep",
        },
    ]
    spy_allocator.carry_forward_state(allocations, previous)
    assert allocations[0]["peak_price"] == 260.0
    assert allocations[0]["entry_price"] == 200.0


def test_carry_forward_state_new_allocation_gets_no_peak():
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 150.0, "peak_price": 170.0},
    ]
    allocations = [{"ticker": "AAPL", "action": "NEW", "entry_price": 160.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert "peak_price" not in allocations[0]
    assert allocations[0]["entry_price"] == 160.0


def test_carry_forward_state_exited_previous_row_ignored():
    previous = [
        {"ticker": "TSLA", "action": "EXITED", "entry_price": 300.0, "peak_price": 350.0},
    ]
    allocations = [{"ticker": "TSLA", "action": "HOLD", "entry_price": 310.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert "peak_price" not in allocations[0]
    assert allocations[0]["entry_price"] == 310.0


def test_carry_forward_state_carries_stop_limit_state():
    previous = [
        {
            "ticker": "NVDA",
            "action": "HOLD",
            "entry_price": 100.0,
            "peak_price": 120.0,
            "pending_stop_limit": True,
            "stop_limit_price": 95.0,
        },
    ]
    allocations = [{"ticker": "NVDA", "action": "HOLD", "entry_price": 0.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert allocations[0]["pending_stop_limit"] is True
    assert allocations[0]["stop_limit_price"] == 95.0
    assert allocations[0]["peak_price"] == 120.0


def test_carry_forward_state_missing_peak_price_no_key_error():
    previous = [{"ticker": "META", "action": "HOLD", "entry_price": 180.0}]
    allocations = [{"ticker": "META", "action": "HOLD", "entry_price": 0.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert "peak_price" not in allocations[0]
    assert allocations[0]["entry_price"] == 180.0


def test_carry_forward_state_clamps_peak_below_entry():
    previous = [
        {"ticker": "AMZN", "action": "HOLD", "entry_price": 200.0, "peak_price": 190.0},
    ]
    allocations = [{"ticker": "AMZN", "action": "HOLD", "entry_price": 0.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert allocations[0]["peak_price"] == 200.0
    assert allocations[0]["entry_price"] == 200.0


def test_carry_forward_state_entry_price_patch_for_added_and_trimmed():
    previous = [
        {"ticker": "GOOGL", "action": "HOLD", "entry_price": 100.0, "peak_price": 110.0},
    ]
    for action in ("ADDED", "TRIMMED"):
        allocations = [{"ticker": "GOOGL", "action": action, "entry_price": 0.0}]
        spy_allocator.carry_forward_state(allocations, previous)
        assert allocations[0]["entry_price"] == 100.0
        assert allocations[0]["peak_price"] == 110.0


def test_carry_forward_state_does_not_overwrite_existing_stop_state():
    previous = [
        {
            "ticker": "X",
            "action": "HOLD",
            "entry_price": 50.0,
            "peak_price": 60.0,
            "pending_stop_limit": True,
            "stop_limit_price": 45.0,
        },
    ]
    allocations = [
        {
            "ticker": "X",
            "action": "HOLD",
            "entry_price": 55.0,
            "peak_price": 70.0,
            "pending_stop_limit": False,
            "stop_limit_price": 40.0,
        },
    ]
    spy_allocator.carry_forward_state(allocations, previous)
    assert allocations[0]["entry_price"] == 50.0  # entry_price is always overwritten
    assert allocations[0]["peak_price"] == 70.0
    assert allocations[0]["pending_stop_limit"] is False
    assert allocations[0]["stop_limit_price"] == 40.0


def test_carry_forward_state_none_values_not_copied():
    previous = [
        {"ticker": "Y", "action": "HOLD", "entry_price": 100.0, "peak_price": None},
    ]
    allocations = [{"ticker": "Y", "action": "HOLD", "entry_price": 0.0}]
    spy_allocator.carry_forward_state(allocations, previous)
    assert "peak_price" not in allocations[0]
    assert allocations[0]["entry_price"] == 100.0


def test_build_rebalance_message_no_stopped_section_when_none():
    candidates = [
        {
            "ticker": "AAPL",
            "signal": "BUY",
            "conviction": 8,
            "entry_price": 150.0,
            "final_decision": "Strong momentum.",
        },
    ]
    previous = [
        {"ticker": "AAPL", "action": "HOLD", "entry_price": 140.0},
    ]
    msg = spy_allocator.build_rebalance_user_message(
        candidates, previous, "2024-01-01", 100_000.0
    )
    assert "STOPPED OUT" not in msg
    assert "=== CURRENT HOLDINGS (1 positions) ===" in msg
    assert "AAPL | signal: BUY | conviction: 8/10 | entry_price: $140.00" in msg


def test_build_rebalance_message_shows_stopped_row():
    candidates = [
        {
            "ticker": "NVDA",
            "signal": "BUY",
            "conviction": 9,
            "entry_price": 700.0,
            "final_decision": "Great setup.",
        },
    ]
    previous = [
        {
            "ticker": "AAPL",
            "action": "EXITED",
            "exit_reason": "trail_stop",
            "entry_price": 100.0,
            "exit_price": 117.5,
        },
    ]
    msg = spy_allocator.build_rebalance_user_message(
        candidates, previous, "2024-01-02", 95_000.0
    )
    assert "=== STOPPED OUT SINCE LAST REBALANCE (1 positions" in msg
    assert "AAPL | closed: trail_stop | entry_price: $100.00 | exit_price: $117.50" in msg


def test_build_rebalance_message_weekly_exited_not_in_stopped_section():
    previous = [
        {"ticker": "TSLA", "action": "EXITED", "entry_price": 200.0, "exit_price": 180.0},
    ]
    msg = spy_allocator.build_rebalance_user_message(
        [], previous, "2024-01-03", 90_000.0
    )
    assert "STOPPED OUT" not in msg
    assert "TSLA" not in msg


def test_build_rebalance_message_stopped_not_in_current_holdings():
    previous = [
        {
            "ticker": "AMD",
            "action": "EXITED",
            "exit_reason": "stop_loss",
            "entry_price": 150.0,
            "exit_price": 140.0,
        },
    ]
    msg = spy_allocator.build_rebalance_user_message(
        [], previous, "2024-01-04", 100_000.0
    )
    holdings_part, _, stopped_part = msg.partition("=== STOPPED OUT")
    assert "AMD" not in holdings_part
    assert "AMD" in stopped_part


def test_build_rebalance_message_missing_exit_price_formats_zero():
    previous = [
        {
            "ticker": "META",
            "action": "EXITED",
            "exit_reason": "stop_limit",
            "entry_price": 300.0,
        },
    ]
    msg = spy_allocator.build_rebalance_user_message(
        [], previous, "2024-01-05", 100_000.0
    )
    assert "META | closed: stop_limit | entry_price: $300.00 | exit_price: $0.00" in msg


def test_build_rebalance_message_missing_entry_price_formats_zero():
    previous = [
        {"ticker": "XOM", "action": "EXITED", "exit_reason": "stop_loss", "exit_price": 50.0},
    ]
    msg = spy_allocator.build_rebalance_user_message(
        [], previous, "2024-01-06", 100_000.0
    )
    assert "XOM | closed: stop_loss | entry_price: $0.00 | exit_price: $50.00" in msg

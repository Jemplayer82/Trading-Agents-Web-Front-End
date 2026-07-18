"""Unit tests for web/options_data.py — OCC symbols, chain normalization,
liquidity gates, and deterministic contract selection."""

from datetime import date, timedelta

import pandas as pd
import pytest

from web import options_data
from web.brokerages import parse_occ_symbol
from web.options_data import (
    build_occ_symbol,
    normalize_schwab_chain,
    normalize_yf_chain,
    passes_liquidity_gates,
    pick_expiry,
    select_contract,
)

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 17)


def _exp(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# ── OCC symbol round-trip ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("underlying", "exp", "put_call", "strike"),
    [
        ("AAPL", "2026-08-21", "CALL", 230.0),
        ("GOOGL", "2026-08-21", "PUT", 182.5),
        ("F", "2026-12-18", "CALL", 12.0),
        ("BRK-B", "2026-09-18", "PUT", 460.0),
    ],
)
def test_occ_symbol_round_trips_with_brokerages_parser(underlying, exp, put_call, strike):
    occ = build_occ_symbol(underlying, exp, put_call, strike)
    parsed = parse_occ_symbol(occ)
    assert parsed is not None
    assert parsed["underlying"] == underlying.upper()
    assert parsed["expiration_date"] == exp
    assert parsed["put_call"] == put_call
    assert parsed["strike"] == pytest.approx(strike)


def test_occ_symbol_is_schwab_padded():
    assert build_occ_symbol("AAPL", "2026-08-21", "CALL", 230) == "AAPL  260821C00230000"
    assert build_occ_symbol("GOOGL", "2026-08-21", "PUT", 182.5) == "GOOGL 260821P00182500"


# ── Liquidity gates ──────────────────────────────────────────────────────────

def _candidate(**over):
    base = {
        "bid": 4.10, "ask": 4.30, "mid": 4.20, "open_interest": 500,
    }
    base.update(over)
    return base


def test_gates_accept_liquid_contract():
    ok, _ = passes_liquidity_gates(_candidate())
    assert ok


@pytest.mark.parametrize(
    ("over", "reason_part"),
    [
        ({"bid": 0.0}, "bid"),                       # zero-bid weekend junk
        ({"bid": None}, "bid"),
        ({"ask": 3.0, "mid": None}, "crossed"),      # crossed market (ask < bid)
        ({"open_interest": 50}, "OI"),
        ({"bid": 0.05, "ask": 0.05, "mid": 0.04}, "penny"),
        ({"bid": 2.00, "ask": 3.20, "mid": 2.60}, "spread"),  # 46% spread
    ],
)
def test_gates_reject_junk(over, reason_part):
    ok, reason = passes_liquidity_gates(_candidate(**over))
    assert not ok
    assert reason_part.lower() in reason.lower()


def test_gates_allow_tight_absolute_spread_on_cheap_contract():
    # $0.08 spread is > 20% of a $0.40 mid but under the $0.10 absolute floor.
    ok, _ = passes_liquidity_gates(_candidate(bid=0.36, ask=0.44, mid=0.40))
    assert ok


# ── Expiry picking ───────────────────────────────────────────────────────────

def test_pick_expiry_prefers_target_in_window():
    assert pick_expiry([7, 14, 22, 35, 44, 90]) == 22


def test_pick_expiry_widens_once():
    assert pick_expiry([5, 8, 55, 58]) == 8  # nothing in [10,45]; widened pick nearest 21


def test_pick_expiry_none_when_out_of_range():
    assert pick_expiry([2, 3, 90, 120]) is None


# ── Schwab chain normalization ───────────────────────────────────────────────

def _schwab_payload():
    def contract(strike, bid, ask, delta, oi):
        return {
            "symbol": f"AAPL  260821C{int(strike * 1000):08d}",
            "bid": bid, "ask": ask, "delta": delta, "openInterest": oi,
            "strikePrice": strike, "putCall": "CALL",
        }
    return {
        "underlyingPrice": 232.0,
        "callExpDateMap": {
            f"{_exp(35)}:35": {
                "225.0": [contract(225.0, 9.8, 10.2, 0.62, 900)],
                "230.0": [contract(230.0, 6.9, 7.1, 0.51, 1500)],
                "235.0": [contract(235.0, 4.4, 4.6, 0.44, 2100)],
                "240.0": [contract(240.0, 2.7, 2.9, 0.33, 800)],
                "245.0": [contract(245.0, 1.5, 1.7, -999.0, 700)],  # greeks sentinel
            },
            f"{_exp(12)}:12": {
                "235.0": [contract(235.0, 2.1, 2.3, 0.46, 3000)],
            },
        },
        "putExpDateMap": {},
    }


def test_normalize_schwab_chain():
    cands = normalize_schwab_chain(_schwab_payload(), "AAPL", "CALL", ref_date=TODAY)
    assert len(cands) == 6
    by_strike = {(c["strike"], c["dte"]): c for c in cands}
    c = by_strike[(235.0, 35)]
    assert c["occ_symbol"] == build_occ_symbol("AAPL", _exp(35), "CALL", 235.0)
    assert c["mid"] == pytest.approx(4.5)
    assert c["delta"] == pytest.approx(0.44)
    assert c["underlying_price"] == pytest.approx(232.0)
    assert c["source"] == "schwab"
    # -999 delta sentinel is treated as absent, not a real delta.
    assert by_strike[(245.0, 35)]["delta"] is None


def test_select_contract_delta_pick():
    cands = normalize_schwab_chain(_schwab_payload(), "AAPL", "CALL", ref_date=TODAY)
    contract, notes = select_contract(cands)
    assert contract is not None
    # DTE 35 wins over 12 (nearer 21? |35-21|=14 vs |12-21|=9 -> 12 actually)
    # pick_expiry chooses 12 here; its only strike is 235 @ delta .46.
    assert contract["dte"] == 12
    assert contract["strike"] == pytest.approx(235.0)


def test_select_contract_steps_past_illiquid_strike():
    payload = _schwab_payload()
    # Make the preferred 12-DTE expiry vanish and best 35-DTE delta (235, .44)
    # fail the gates via zero bid; next-nearest delta should win.
    del payload["callExpDateMap"][f"{_exp(12)}:12"]
    payload["callExpDateMap"][f"{_exp(35)}:35"]["235.0"][0]["bid"] = 0.0
    cands = normalize_schwab_chain(payload, "AAPL", "CALL", ref_date=TODAY)
    contract, notes = select_contract(cands)
    assert contract is not None
    assert contract["strike"] in (230.0, 240.0)
    assert any("bid" in n for n in notes)


def test_select_contract_moneyness_fallback_without_deltas():
    payload = _schwab_payload()
    for strikes in payload["callExpDateMap"].values():
        for rows in strikes.values():
            rows[0]["delta"] = None
    cands = normalize_schwab_chain(payload, "AAPL", "CALL", ref_date=TODAY)
    contract, _ = select_contract(cands)
    assert contract is not None
    # Nearest to spot 232 within its chosen expiry.
    assert contract["strike"] in (230.0, 235.0)


def test_select_contract_empty():
    contract, notes = select_contract([])
    assert contract is None
    assert notes


# ── yfinance chain normalization ─────────────────────────────────────────────

def test_normalize_yf_chain():
    frame = pd.DataFrame([
        {"contractSymbol": "AAPL260821C00230000", "strike": 230.0, "bid": 6.9,
         "ask": 7.1, "openInterest": 1500.0, "lastPrice": 7.0},
        {"contractSymbol": "AAPL260821C00235000", "strike": 235.0, "bid": 4.4,
         "ask": 4.6, "openInterest": float("nan"), "lastPrice": 4.5},
    ])
    cands = normalize_yf_chain("AAPL", _exp(35), frame, "CALL", spot=232.0, ref_date=TODAY)
    assert len(cands) == 2
    assert cands[0]["source"] == "yfinance"
    assert cands[0]["delta"] is None
    assert cands[0]["occ_symbol"] == build_occ_symbol("AAPL", _exp(35), "CALL", 230.0)
    assert cands[1]["open_interest"] == 0  # NaN-safe


def test_normalize_yf_chain_empty():
    assert normalize_yf_chain("AAPL", _exp(35), None, "CALL", spot=None) == []


# ── fetch_candidates filtering ───────────────────────────────────────────────

def test_fetch_candidates_filters_direction_and_conviction(monkeypatch):
    calls = []

    def fake_fetch_contract(underlying, direction, spot_hint=None):
        calls.append((underlying, direction))
        return ({"occ_symbol": f"{underlying} X", "underlying": underlying,
                 "put_call": "CALL" if direction == "BUY" else "PUT",
                 "strike": 100.0, "expiration_date": _exp(21), "dte": 21,
                 "bid": 1.0, "ask": 1.2, "mid": 1.1, "delta": 0.45,
                 "open_interest": 500, "underlying_price": 100.0,
                 "spread": 0.2, "spread_pct": 0.18, "source": "schwab"}, [])

    monkeypatch.setattr(options_data, "fetch_contract", fake_fetch_contract)
    signals = [
        {"ticker": "AAA", "signal": "BUY", "conviction": 8, "reasoning": "up"},
        {"ticker": "BBB", "signal": "SELL", "conviction": 7, "reasoning": "down"},
        {"ticker": "CCC", "signal": "HOLD", "conviction": 9, "reasoning": "meh"},
        {"ticker": "DDD", "signal": "BUY", "conviction": 4, "reasoning": "weak"},
    ]
    cands, notes = options_data.fetch_candidates(signals)
    assert [(c["ticker"], c["put_call"]) for c in cands] == [("AAA", "CALL"), ("BBB", "PUT")]
    assert ("CCC", "HOLD") not in calls  # HOLD skipped before any chain fetch
    assert any("DDD" in n for n in notes)  # low conviction noted

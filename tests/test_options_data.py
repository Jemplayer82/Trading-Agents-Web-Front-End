"""Unit tests for web/options_data.py — OCC symbols, chain normalization,
liquidity gates, deterministic contract selection, and selected-contract caching."""

import threading
import time
from datetime import date, timedelta
from typing import Any

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


# ── Selected-contract same-day cache ───────────────────────────────────────────

class TestContractCache:
    @pytest.fixture(autouse=True)
    def _reset_cache_and_env(self, monkeypatch):
        monkeypatch.setenv("OPTIONS_CONTRACT_CACHE", "1")
        options_data._CONTRACT_CACHE.clear()
        yield
        options_data._CONTRACT_CACHE.clear()

    def _contract(self, underlying="AAPL", put_call="CALL", strike=230.0, bid=4.1, ask=4.3):
        exp = _exp(21)
        occ = build_occ_symbol(underlying, exp, put_call, strike)
        mid = options_data._mid(bid, ask)
        spread = round(float(ask) - float(bid), 4) if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) else None
        return {
            "occ_symbol": occ,
            "underlying": underlying.upper(),
            "put_call": put_call,
            "strike": float(strike),
            "expiration_date": exp,
            "dte": 21,
            "bid": float(bid),
            "ask": float(ask),
            "mid": mid,
            "delta": 0.45,
            "open_interest": 500,
            "underlying_price": 230.0,
            "spread": spread,
            "spread_pct": round(spread / mid, 4) if spread is not None and mid else None,
            "source": "schwab",
        }

    def test_first_call_is_a_miss_and_caches(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)

        contract, notes = options_data.fetch_contract("AAPL", "BUY")
        assert contract is not None
        assert notes == []
        assert len(calls) == 1
        assert options_data._CONTRACT_CACHE.stats()["size"] == 1

    def test_same_day_hit_refreshes_bid_ask_and_skips_the_chain_fetch(self, monkeypatch):
        calls = []
        quote_calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        def fake_quotes(symbols):
            quote_calls.append(list(symbols))
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 4.2, "askPrice": 4.4}}}

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        c1, _ = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1

        c2, notes2 = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1  # hit: no second chain fetch
        assert len(quote_calls) == 1
        assert quote_calls[0] == [c1["occ_symbol"]]
        assert c2["bid"] == pytest.approx(4.2)
        assert c2["ask"] == pytest.approx(4.4)
        assert c2["mid"] == pytest.approx(4.3)
        assert c2["spread"] == pytest.approx(0.2)
        assert c2["spread_pct"] == pytest.approx(round(0.2 / 4.3, 4))
        # Non-price fields are carried over unchanged.
        assert c2["open_interest"] == 500
        assert c2["delta"] == pytest.approx(0.45)
        assert c2["source"] == "schwab"
        assert notes2 == []

    def test_hit_that_fails_the_gates_falls_back_to_a_full_refetch(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)

        # Populate the cache.
        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1

        # Spread too wide -> re-validation fails against the gates.
        def wide_quotes(symbols):
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 2.0, "askPrice": 3.2}}}

        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", wide_quotes)
        c, notes = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert c is not None
        assert any("re-validation" in n for n in notes)

        # Zero bid -> no usable mid, also falls back.
        def zero_quotes(symbols):
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 0.0, "askPrice": 0.05}}}

        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", zero_quotes)
        c, notes = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 3
        assert c is not None
        assert any("refetching" in n for n in notes)

    def test_no_quote_for_the_symbol_falls_back(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)

        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1

        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", lambda symbols: {})
        c, notes = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert c is not None
        assert any("refetching" in n for n in notes)

    def test_quotes_none_falls_back(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)

        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1

        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", lambda symbols: None)
        c, notes = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert c is not None
        assert any("refetching" in n for n in notes)

    def test_market_data_disabled_falls_back(self, monkeypatch):
        calls = []
        quote_calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        def fake_quotes(symbols):
            quote_calls.append(symbols)
            return None

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: False)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1
        assert not quote_calls  # no quote attempt when market data is off

        c, notes = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert not quote_calls
        assert c is not None
        assert any("refetching" in n for n in notes)

    def test_get_quotes_exception_falls_back(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)

        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1

        def boom(symbols):
            raise RuntimeError("schwab unavailable")

        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", boom)
        c, notes = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert c is not None
        assert any("refetching" in n for n in notes)

    def test_ttl_expiry_refetches(self, monkeypatch):
        calls = []
        clock = [0.0]

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        def fake_now():
            return clock[0]

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr("web.market_cache._now", fake_now)

        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1

        clock[0] += options_data.CONTRACT_CACHE_TTL_SECONDS + 1
        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2

    def test_date_rollover_refetches_and_evicts(self, monkeypatch):
        calls = []
        dates = [TODAY]

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        monkeypatch.setattr(options_data, "today_et", lambda: dates[-1])
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)

        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1
        assert options_data._CONTRACT_CACHE.stats()["size"] == 1

        dates.append(TODAY + timedelta(days=1))
        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert options_data._CONTRACT_CACHE.stats()["size"] == 1

    def test_call_and_put_are_separate_keys(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            side = "CALL" if str(direction).upper() == "BUY" else "PUT"
            return (self._contract(put_call=side), [])

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)

        options_data.fetch_contract("AAPL", "BUY")
        options_data.fetch_contract("AAPL", "SELL")
        assert len(calls) == 2
        assert options_data._CONTRACT_CACHE.stats()["size"] == 2

    def test_returned_contract_is_a_copy(self, monkeypatch):
        quote_calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            return (self._contract(), [])

        def fake_quotes(symbols):
            quote_calls.append(symbols)
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 4.2, "askPrice": 4.4}}}

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        c1, _ = options_data.fetch_contract("AAPL", "BUY")
        c1["ticker"] = "AAA"  # mutation that fetch_candidates performs
        c1["mid"] = 999.0

        c2, _ = options_data.fetch_contract("AAPL", "BUY")
        assert "ticker" not in c2
        assert c2["mid"] != 999.0
        assert c2["mid"] == pytest.approx(options_data._mid(c2["bid"], c2["ask"]))

    def test_none_result_is_not_cached(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (None, ["no chain data"])

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)

        c1, notes1 = options_data.fetch_contract("AAPL", "BUY")
        assert c1 is None
        assert notes1 == ["no chain data"]
        assert options_data._CONTRACT_CACHE.stats()["size"] == 0

        c2, notes2 = options_data.fetch_contract("AAPL", "BUY")
        assert c2 is None
        assert notes2 == ["no chain data"]
        assert len(calls) == 2

    def test_kill_switch_bypasses_the_cache(self, monkeypatch):
        monkeypatch.setenv("OPTIONS_CONTRACT_CACHE", "0")
        calls = []
        quote_calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            return (self._contract(), [])

        def fake_quotes(symbols):
            quote_calls.append(symbols)
            return None

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        options_data.fetch_contract("AAPL", "BUY")
        options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert not quote_calls
        assert options_data._CONTRACT_CACHE.stats()["size"] == 0

    def test_concurrent_misses_do_not_deadlock(self, monkeypatch):
        barrier = threading.Barrier(4, timeout=5)

        def fake_fetch(underlying, direction, spot_hint=None):
            barrier.wait(timeout=5)
            return (self._contract(), [])

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)

        results: list[dict[str, Any] | None] = []
        errors: list[Exception] = []
        threads: list[threading.Thread] = []

        def worker():
            try:
                contract, _ = options_data.fetch_contract("AAPL", "BUY")
                results.append(contract)
            except Exception as exc:  # noqa: BLE001 - test harness
                errors.append(exc)

        for _ in range(4):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=5)

        assert all(not t.is_alive() for t in threads)
        assert not errors
        assert len(results) == 4
        assert all(c is not None for c in results)
        assert options_data._CONTRACT_CACHE.stats()["size"] == 1

    def test_hit_with_quote_delta_outside_band_refetches(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            c = self._contract()
            if len(calls) == 0:
                c["delta"] = 0.45
            else:
                c["delta"] = 0.40
            calls.append((underlying, direction))
            return (c, [])

        def fake_quotes(symbols):
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 4.2, "askPrice": 4.4, "delta": 0.75}}}

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        c1, _ = options_data.fetch_contract("AAPL", "BUY")
        assert c1["delta"] == pytest.approx(0.45)
        assert len(calls) == 1

        c2, notes2 = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert any("refetching" in n for n in notes2)
        assert any("delta" in n.lower() for n in notes2)
        assert c2["delta"] == pytest.approx(0.40)

    def test_hit_with_quote_delta_within_band_refreshes_delta(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            calls.append((underlying, direction))
            c = self._contract()
            c["delta"] = 0.45
            return (c, [])

        def fake_quotes(symbols):
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 4.2, "askPrice": 4.4, "delta": 0.55}}}

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        c1, _ = options_data.fetch_contract("AAPL", "BUY")
        assert c1["delta"] == pytest.approx(0.45)
        assert len(calls) == 1

        c2, notes2 = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 1
        assert notes2 == []
        assert c2["delta"] == pytest.approx(0.55)
        assert c2["bid"] == pytest.approx(4.2)

    def test_hit_no_delta_with_quote_underlyingprice_outside_band_refetches(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            c = self._contract()
            c["delta"] = None
            calls.append((underlying, direction))
            return (c, [])

        def fake_quotes(symbols):
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 4.2, "askPrice": 4.4, "underlyingPrice": 260.0}}}

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        c1, _ = options_data.fetch_contract("AAPL", "BUY")
        assert c1["delta"] is None
        assert len(calls) == 1

        c2, notes2 = options_data.fetch_contract("AAPL", "BUY")
        assert len(calls) == 2
        assert any("refetching" in n for n in notes2)
        assert any("moneyness" in n.lower() or "strike" in n.lower() for n in notes2)

    def test_hit_no_delta_spot_hint_outside_band_refetches(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            c = self._contract()
            c["delta"] = None
            calls.append((underlying, direction))
            return (c, [])

        def fake_quotes(symbols):
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 4.2, "askPrice": 4.4}}}

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        c1, _ = options_data.fetch_contract("AAPL", "BUY")
        assert c1["delta"] is None
        assert len(calls) == 1

        c2, notes2 = options_data.fetch_contract("AAPL", "BUY", spot_hint=260.0)
        assert len(calls) == 2
        assert any("refetching" in n for n in notes2)
        assert any("moneyness" in n.lower() or "strike" in n.lower() for n in notes2)

    def test_hit_no_delta_spot_hint_within_band_serves_cache(self, monkeypatch):
        calls = []

        def fake_fetch(underlying, direction, spot_hint=None):
            c = self._contract()
            c["delta"] = None
            calls.append((underlying, direction))
            return (c, [])

        def fake_quotes(symbols):
            occ = self._contract()["occ_symbol"]
            return {occ: {"quote": {"bidPrice": 4.2, "askPrice": 4.4}}}

        monkeypatch.setattr(options_data, "today_et", lambda: TODAY)
        monkeypatch.setattr(options_data, "_fetch_contract_uncached", fake_fetch)
        monkeypatch.setattr(options_data.schwab_mcp, "market_data_enabled", lambda: True)
        monkeypatch.setattr(options_data.schwab_mcp, "get_quotes", fake_quotes)

        c1, _ = options_data.fetch_contract("AAPL", "BUY")
        assert c1["delta"] is None
        assert len(calls) == 1

        c2, notes2 = options_data.fetch_contract("AAPL", "BUY", spot_hint=235.0)
        assert len(calls) == 1
        assert notes2 == []
        assert c2 is not None
        assert c2["delta"] is None
        assert c2["bid"] == pytest.approx(4.2)


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


def test_fetch_candidates_treats_5tier_overweight_underweight_as_directional(monkeypatch):
    """Deep dives return the system-wide 5-tier rating (Buy, Overweight, Hold,
    Underweight, Sell) via SignalProcessor.process_signal, not raw BUY/SELL/HOLD
    — see tradingagents/agents/utils/rating.py. Only "Buy"/"Sell" (the two most
    extreme tiers) used to match here, silently dropping every Overweight/
    Underweight call (real, if less extreme, directional signal) with zero
    note. Overweight must vet as a CALL candidate, Underweight as a PUT."""
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
        {"ticker": "OVW", "signal": "Overweight", "conviction": 8, "reasoning": "bullish"},
        {"ticker": "UDW", "signal": "Underweight", "conviction": 8, "reasoning": "bearish"},
        {"ticker": "HLD", "signal": "Hold", "conviction": 9, "reasoning": "meh"},
    ]
    cands, notes = options_data.fetch_candidates(signals)
    # calls is appended from inside fetch_contract, which now runs in worker
    # threads, so append order is genuinely nondeterministic. The ordering
    # guarantee is already covered by the cands assertion below.
    assert sorted(calls) == [("OVW", "BUY"), ("UDW", "SELL")]
    assert [(c["ticker"], c["put_call"], c["signal"]) for c in cands] == [
        ("OVW", "CALL", "BUY"), ("UDW", "PUT", "SELL"),
    ]
    assert ("HLD", "BUY") not in calls and ("HLD", "SELL") not in calls


def test_fetch_candidates_returns_input_order_under_out_of_order_completion(monkeypatch):
    completion_order: list[str] = []

    def fake_fetch_contract(underlying, direction, spot_hint=None):
        position = int(underlying[1:])  # T0..T5
        time.sleep(0.05 * (5 - position))  # T0 sleeps longest, T5 sleeps 0
        completion_order.append(underlying)
        return ({"occ_symbol": f"{underlying} X", "underlying": underlying,
                 "put_call": "CALL", "strike": 100.0, "expiration_date": _exp(21),
                 "dte": 21, "bid": 1.0, "ask": 1.2, "mid": 1.1, "delta": 0.45,
                 "open_interest": 500, "underlying_price": 100.0,
                 "spread": 0.2, "spread_pct": 0.18, "source": "schwab"}, [])

    monkeypatch.setattr(options_data, "fetch_contract", fake_fetch_contract)
    signals = [{"ticker": f"T{i}", "signal": "BUY", "conviction": 8} for i in range(6)]
    cands, _ = options_data.fetch_candidates(signals)
    input_order = [s["ticker"] for s in signals]
    assert completion_order != input_order
    assert [c["ticker"] for c in cands] == input_order


def test_fetch_candidates_runs_fetches_concurrently(monkeypatch):
    barrier = threading.Barrier(4, timeout=5)

    def fake_fetch_contract(underlying, direction, spot_hint=None):
        barrier.wait()
        return ({"occ_symbol": f"{underlying} X", "underlying": underlying,
                 "put_call": "CALL", "strike": 100.0, "expiration_date": _exp(21),
                 "dte": 21, "bid": 1.0, "ask": 1.2, "mid": 1.1, "delta": 0.45,
                 "open_interest": 500, "underlying_price": 100.0,
                 "spread": 0.2, "spread_pct": 0.18, "source": "schwab"}, [])

    monkeypatch.setattr(options_data, "fetch_contract", fake_fetch_contract)
    signals = [{"ticker": f"B{i}", "signal": "BUY", "conviction": 8} for i in range(4)]
    cands, _ = options_data.fetch_candidates(signals)
    assert [c["ticker"] for c in cands] == [s["ticker"] for s in signals]


def test_fetch_candidates_worker_count_capped_at_six(monkeypatch):
    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0

    def fake_fetch_contract(underlying, direction, spot_hint=None):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return ({"occ_symbol": f"{underlying} X", "underlying": underlying,
                 "put_call": "CALL", "strike": 100.0, "expiration_date": _exp(21),
                 "dte": 21, "bid": 1.0, "ask": 1.2, "mid": 1.1, "delta": 0.45,
                 "open_interest": 500, "underlying_price": 100.0,
                 "spread": 0.2, "spread_pct": 0.18, "source": "schwab"}, [])

    monkeypatch.setattr(options_data, "fetch_contract", fake_fetch_contract)
    signals = [{"ticker": f"W{i:02d}", "signal": "BUY", "conviction": 8} for i in range(20)]
    cands, _ = options_data.fetch_candidates(signals)
    assert len(cands) == 20
    assert 1 < max_in_flight <= 6


def test_fetch_candidates_notes_stay_in_input_order(monkeypatch):
    def fake_fetch_contract(underlying, direction, spot_hint=None):
        return (None, [f"{underlying}: schwab: spread too wide"])

    monkeypatch.setattr(options_data, "fetch_contract", fake_fetch_contract)
    signals = [
        {"ticker": "LOW1", "signal": "BUY", "conviction": 4},
        {"ticker": "HLD1", "signal": "HOLD", "conviction": 9},
        {"ticker": "TRD1", "signal": "BUY", "conviction": 8},
        {"ticker": "LOW2", "signal": "SELL", "conviction": 5},
        {"ticker": "HLD2", "signal": "HOLD", "conviction": 8},
        {"ticker": "TRD2", "signal": "SELL", "conviction": 8},
    ]
    cands, notes = options_data.fetch_candidates(signals)
    expected = [
        "LOW1: conviction 4 < 6",
        "TRD1: schwab: spread too wide",
        "TRD1: no tradeable contract",
        "LOW2: conviction 5 < 6",
        "TRD2: schwab: spread too wide",
        "TRD2: no tradeable contract",
    ]
    assert notes == expected
    assert cands == []


def test_fetch_candidates_propagates_fetch_errors(monkeypatch):
    def fake_fetch_contract(underlying, direction, spot_hint=None):
        if underlying == "BAD":
            raise RuntimeError("chain down")
        return ({"occ_symbol": f"{underlying} X", "underlying": underlying,
                 "put_call": "CALL", "strike": 100.0, "expiration_date": _exp(21),
                 "dte": 21, "bid": 1.0, "ask": 1.2, "mid": 1.1, "delta": 0.45,
                 "open_interest": 500, "underlying_price": 100.0,
                 "spread": 0.2, "spread_pct": 0.18, "source": "schwab"}, [])

    monkeypatch.setattr(options_data, "fetch_contract", fake_fetch_contract)
    signals = [
        {"ticker": "OK1", "signal": "BUY", "conviction": 8},
        {"ticker": "BAD", "signal": "BUY", "conviction": 8},
        {"ticker": "OK2", "signal": "BUY", "conviction": 8},
    ]
    with pytest.raises(RuntimeError, match="chain down"):
        options_data.fetch_candidates(signals)


def test_fetch_candidates_progress_cb_called_in_order(monkeypatch):
    progress_calls: list[tuple[int, int]] = []

    def fake_fetch_contract(underlying, direction, spot_hint=None):
        return ({"occ_symbol": f"{underlying} X", "underlying": underlying,
                 "put_call": "CALL", "strike": 100.0, "expiration_date": _exp(21),
                 "dte": 21, "bid": 1.0, "ask": 1.2, "mid": 1.1, "delta": 0.45,
                 "open_interest": 500, "underlying_price": 100.0,
                 "spread": 0.2, "spread_pct": 0.18, "source": "schwab"}, [])

    def progress_cb(done, total):
        progress_calls.append((done, total))

    monkeypatch.setattr(options_data, "fetch_contract", fake_fetch_contract)
    signals = [
        {"ticker": "A", "signal": "BUY", "conviction": 8},
        {"ticker": "B", "signal": "HOLD", "conviction": 9},
        {"ticker": "C", "signal": "BUY", "conviction": 4},
        {"ticker": "D", "signal": "SELL", "conviction": 8},
    ]
    cands, _ = options_data.fetch_candidates(signals, progress_cb=progress_cb)
    assert [c["ticker"] for c in cands] == ["A", "D"]
    assert progress_calls == [(1, 4), (4, 4)]


def test_fetch_candidates_empty_and_all_filtered(monkeypatch):
    calls: list[str] = []

    def fake_fetch_contract(underlying, direction, spot_hint=None):
        calls.append(underlying)
        return ({}, [])

    monkeypatch.setattr(options_data, "fetch_contract", fake_fetch_contract)
    assert options_data.fetch_candidates([]) == ([], [])
    signals = [
        {"ticker": "H1", "signal": "HOLD", "conviction": 9},
        {"ticker": "H2", "signal": "HOLD", "conviction": 8},
    ]
    assert options_data.fetch_candidates(signals) == ([], [])
    assert calls == []

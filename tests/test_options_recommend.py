"""On-demand single-ticker options recommendation (web/options_recommend.py).

All LLM calls, chain fetches, price downloads and the memory log are stubbed —
these tests pin the orchestration contract: quick read feeds the advisor, both
sides get vetted, the advisor's JSON is validated, hallucinated contracts are
pinned back to vetted ones, LLM failure degrades deterministically, and the
final directional call lands in the learning loop.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from web import options_recommend

pytestmark = pytest.mark.unit


CALL = {
    "occ_symbol": "AAPL  260821C00230000", "underlying": "AAPL", "put_call": "CALL",
    "strike": 230.0, "expiration_date": "2026-08-21", "dte": 21, "mid": 4.2,
    "delta": 0.45, "open_interest": 1500, "spread_pct": 0.04,
}
PUT = {
    "occ_symbol": "AAPL  260821P00210000", "underlying": "AAPL", "put_call": "PUT",
    "strike": 210.0, "expiration_date": "2026-08-21", "dte": 21, "mid": 3.1,
    "delta": -0.44, "open_interest": 900, "spread_pct": 0.05,
}


@pytest.fixture()
def stubbed(monkeypatch):
    """Stub every external dependency; return the mutable knobs."""
    knobs = {
        "quick": {"ticker": "AAPL", "signal": "BUY", "conviction": 8,
                  "reasoning": "strong momentum", "entry_price": 228.0},
        "call": dict(CALL), "put": dict(PUT),
        "llm_json": {"action": "CALL", "confidence": 7, "entry_premium": 4.2,
                     "target_premium": 6.5, "stop_premium": 2.5, "horizon_days": 10,
                     "thesis": "Momentum supports upside.", "risks": ["theta", "IV crush"]},
        "stored": [],
    }
    monkeypatch.setattr(options_recommend, "_price_data",
                        lambda t: {"close": [220.0 + i for i in range(20)], "volume": [1e6] * 20})
    monkeypatch.setattr(options_recommend.spy_scanner, "_quick_scan_one",
                        lambda t, pd, sector, llm, gate=None: dict(knobs["quick"]))
    monkeypatch.setattr(
        options_recommend.options_data, "fetch_contract",
        lambda t, direction, spot_hint=None: (
            (knobs["call"], []) if direction == "BUY" else (knobs["put"], [])
        ),
    )

    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=json.dumps(knobs["llm_json"]))
    monkeypatch.setattr(options_recommend, "llm_for", lambda cfg, deep=False, temperature=0.0: llm)
    knobs["llm"] = llm

    class FakeLog:
        def __init__(self, config=None):
            pass
        def get_past_context(self, ticker):
            return "CALIBRATION — stub"
        def store_decision(self, ticker, trade_date, final_trade_decision):
            knobs["stored"].append((ticker, trade_date, final_trade_decision))
    monkeypatch.setattr(options_recommend, "TradingMemoryLog", FakeLog)

    # No options accounts -> lessons path is a clean no-op.
    monkeypatch.setattr(options_recommend.db, "list_paper_accounts", lambda kind=None: [])
    return knobs


def test_happy_call_path(stubbed):
    out = options_recommend.recommend("aapl")
    rec = out["recommendation"]
    assert out["ticker"] == "AAPL"
    assert rec["action"] == "CALL"
    assert rec["confidence"] == 7
    assert rec["occ_symbol"] == CALL["occ_symbol"]  # pinned to the vetted contract
    assert out["contract"]["put_call"] == "CALL"
    assert out["quick"]["conviction"] == 8
    assert out["context_used"]["history"] is True


def test_advisor_hallucinated_side_without_contract_falls_back(stubbed):
    """Advisor says PUT but no put passed vetting -> deterministic fallback,
    never a rec pointing at a contract that doesn't exist."""
    stubbed["llm_json"]["action"] = "PUT"
    stubbed["llm"].invoke.return_value = MagicMock(content=json.dumps(stubbed["llm_json"]))
    def fetch(t, direction, spot_hint=None):
        return (dict(CALL), []) if direction == "BUY" else (None, ["AAPL: no put"])
    options_recommend.options_data.fetch_contract = fetch  # already monkeypatched attr
    out = options_recommend.recommend("AAPL")
    rec = out["recommendation"]
    # Quick read is BUY/8 -> fallback picks the CALL side.
    assert rec["action"] == "CALL"
    assert rec["occ_symbol"] == CALL["occ_symbol"]
    assert "fallback" in rec["thesis"].lower()


def test_llm_garbage_degrades_to_quick_signal(stubbed):
    stubbed["llm"].invoke.return_value = MagicMock(content="not json at all")
    out = options_recommend.recommend("AAPL")
    rec = out["recommendation"]
    assert rec["action"] == "CALL"  # BUY conviction 8 >= MIN_CONVICTION
    assert rec["confidence"] <= 6
    assert rec["entry_premium"] == pytest.approx(4.2)


def test_low_conviction_garbage_means_no_trade(stubbed):
    stubbed["quick"]["signal"] = "HOLD"
    stubbed["quick"]["conviction"] = 3
    stubbed["llm"].invoke.return_value = MagicMock(content="boom")
    out = options_recommend.recommend("AAPL")
    assert out["recommendation"]["action"] == "NO_TRADE"
    assert out["recommendation"]["entry_premium"] is None


def test_no_contracts_at_all_is_no_trade_without_llm(stubbed):
    options_recommend.options_data.fetch_contract = (
        lambda t, direction, spot_hint=None: (None, [f"{t}: illiquid"]))
    out = options_recommend.recommend("AAPL")
    assert out["recommendation"]["action"] == "NO_TRADE"
    # The deep advisor must not even be consulted without contracts.
    assert stubbed["llm"].invoke.call_count == 0
    assert out["vetting_notes"]


def test_confidence_clamped(stubbed):
    stubbed["llm_json"]["confidence"] = 47
    stubbed["llm"].invoke.return_value = MagicMock(content=json.dumps(stubbed["llm_json"]))
    out = options_recommend.recommend("AAPL")
    assert out["recommendation"]["confidence"] == 10


def test_decision_is_stored_for_grading(stubbed):
    options_recommend.recommend("AAPL")
    assert len(stubbed["stored"]) == 1
    ticker, _date, decision = stubbed["stored"][0]
    assert ticker == "AAPL"
    assert decision.startswith("Rating: Buy")  # CALL -> Buy, gradable by the sweep


def test_no_trade_stores_hold(stubbed):
    stubbed["llm_json"].update({"action": "NO_TRADE", "entry_premium": None,
                                "target_premium": None, "stop_premium": None})
    stubbed["llm"].invoke.return_value = MagicMock(content=json.dumps(stubbed["llm_json"]))
    options_recommend.recommend("AAPL")
    assert stubbed["stored"][0][2].startswith("Rating: Hold")


@pytest.mark.parametrize("raw,ok", [
    ("AAPL", "AAPL"), ("aapl ", "AAPL"), ("BRK.B", "BRK.B"), ("BF-B", "BF-B"),
    ("", None), ("TOO_LONG_TICKER", None), ("bad ticker", None), ("1BAD", None),
])
def test_valid_ticker(raw, ok):
    assert options_recommend.valid_ticker(raw) == ok

"""Unit tests for web/options_allocator.py — hard guardrails, LLM parse/clamp,
and the deterministic fallback."""

import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from web import options_allocator
from web.options_allocator import forced_closes, position_caps, run

pytestmark = pytest.mark.unit

TODAY = date(2026, 7, 17)


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch):
    monkeypatch.setattr(options_allocator.options_data, "today_et", lambda: TODAY)


def _exp(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _pos(pid, occ, dte=30, entry=4.0, mark=4.0, contracts=2, **over):
    base = {
        "id": pid, "occ_symbol": occ, "underlying": occ.split()[0],
        "put_call": "CALL", "strike": 230.0, "expiration_date": _exp(dte),
        "contracts": contracts, "entry_premium": entry,
        "cost_basis": round(entry * 100 * contracts, 2),
        "current_premium": mark, "current_value": round(mark * 100 * contracts, 2),
    }
    base.update(over)
    return base


def _cand(ticker, mid=5.0, conviction=8, dte=21):
    return {
        "occ_symbol": f"{ticker:<6s}260821C00100000", "ticker": ticker,
        "underlying": ticker, "put_call": "CALL", "strike": 100.0,
        "expiration_date": _exp(dte), "dte": dte, "bid": mid - 0.1,
        "ask": mid + 0.1, "mid": mid, "delta": 0.45, "open_interest": 500,
        "underlying_price": 100.0, "signal": "BUY", "conviction": conviction,
        "rationale": "test", "source": "schwab",
    }


def _mock_llm(monkeypatch, payload):
    llm = MagicMock()
    if isinstance(payload, Exception):
        llm.invoke.side_effect = payload
    else:
        llm.invoke.return_value = MagicMock(content=json.dumps(payload))
    monkeypatch.setattr(options_allocator, "llm_for", lambda *a, **k: llm)
    return llm


# ── Hard guardrails ──────────────────────────────────────────────────────────

def test_forced_close_dte_floor():
    positions = [_pos(1, "AAPL  X", dte=2), _pos(2, "MSFT  X", dte=30)]
    forced = forced_closes(positions)
    assert [(p["id"], reason) for p, reason in forced] == [(1, "dte_floor")]


def test_forced_close_stop_loss():
    positions = [
        _pos(1, "AAPL  X", entry=4.0, mark=1.55),  # -61% -> stopped
        _pos(2, "MSFT  X", entry=4.0, mark=1.70),  # -57% -> survives
    ]
    forced = forced_closes(positions)
    assert [(p["id"], reason) for p, reason in forced] == [(1, "stop_loss")]


def test_forced_closes_precede_llm(monkeypatch):
    """A guardrail close happens even if the LLM says HOLD (its decision for a
    force-closed contract is simply an unknown symbol by then)."""
    stopped = _pos(1, "AAPL  260821C00230000", entry=4.0, mark=1.0)
    _mock_llm(monkeypatch, [
        {"occ_symbol": stopped["occ_symbol"], "action": "HOLD", "rationale": "diamond hands"},
    ])
    result = run([], [stopped], "2026-07-17", {}, equity=100_000, cash=99_000)
    assert [c["exit_reason"] for c in result["closes"]] == ["stop_loss"]
    assert result["holds"] == []


def test_position_caps_tiers():
    assert position_caps(2) == (0.05, 0.15)
    assert position_caps(5) == (0.08, 0.30)
    assert position_caps(9) == (0.12, 0.50)


# ── LLM decision parsing + clamping ──────────────────────────────────────────

def test_llm_decisions_parsed_and_clamped(monkeypatch):
    held = _pos(1, "NVDA  260821C00190000", dte=30)
    ignored = _pos(2, "MSFT  260821C00420000", dte=30)
    cand = _cand("AAPL", mid=10.0, conviction=9)
    _mock_llm(monkeypatch, [
        {"occ_symbol": held["occ_symbol"], "action": "CLOSE", "rationale": "thesis done"},
        # wants 20 contracts @ $1000/contract = $20k; per-position cap at agg 5
        # is 8% of $100k = $8k -> clamped to 8 contracts.
        {"occ_symbol": cand["occ_symbol"], "action": "NEW", "contracts": 20, "rationale": "moon"},
        {"occ_symbol": "HALLU 260821C00001000", "action": "NEW", "contracts": 5},
    ])
    result = run([cand], [held, ignored], "2026-07-17", {},
                 equity=100_000, cash=60_000, aggressiveness=5)
    assert [c["exit_reason"] for c in result["closes"]] == ["llm_close"]
    # Ignored open position defaults to HOLD.
    assert [h["position_id"] for h in result["holds"]] == [2]
    assert len(result["opens"]) == 1
    assert result["opens"][0]["contracts"] == 8
    assert result["opens"][0]["cost"] == pytest.approx(8_000)


def test_total_premium_cap_across_opens(monkeypatch):
    # agg 5: total cap 30% of 100k = $30k. Three $10k requests -> ~3 fit but a
    # $9k held position already at risk leaves $21k -> only 2 full opens fit
    # (ranked by conviction), the third is clamped down.
    held = _pos(1, "MSFT  260821C00420000", dte=30, entry=45.0, mark=45.0, contracts=2)  # cost 9000
    cands = [_cand("AAA", mid=10.0, conviction=9), _cand("BBB", mid=10.0, conviction=8),
             _cand("CCC", mid=10.0, conviction=7)]
    _mock_llm(monkeypatch, [
        {"occ_symbol": c["occ_symbol"], "action": "NEW", "contracts": 10} for c in cands
    ])
    result = run(cands, [held], "2026-07-17", {},
                 equity=100_000, cash=91_000, aggressiveness=5)
    costs = {o["contract"]["ticker"]: o["cost"] for o in result["opens"]}
    assert costs["AAA"] == pytest.approx(8_000)   # per-position cap
    assert costs["BBB"] == pytest.approx(8_000)
    assert costs.get("CCC", 0) <= 5_000           # leftover budget only
    total_new = sum(o["cost"] for o in result["opens"])
    assert total_new <= 21_000 + 1e-6


def test_max_open_positions(monkeypatch):
    held = [_pos(i, f"T{i:<5d}260821C00100000", dte=30) for i in range(1, 15)]  # 14 holds
    cands = [_cand("AAA"), _cand("BBB")]
    _mock_llm(monkeypatch, [
        {"occ_symbol": c["occ_symbol"], "action": "NEW", "contracts": 1} for c in cands
    ])
    result = run(cands, held, "2026-07-17", {}, equity=1_000_000, cash=900_000)
    # 14 holds + max 15 -> only one new position fits.
    assert len(result["opens"]) == 1


def test_llm_failure_uses_fallback(monkeypatch):
    held = _pos(1, "NVDA  260821C00190000", dte=30)
    cands = [_cand("AAA", mid=5.0, conviction=9), _cand("BBB", mid=5.0, conviction=6)]
    _mock_llm(monkeypatch, RuntimeError("LLM down"))
    result = run(cands, [held], "2026-07-17", {}, equity=100_000, cash=99_000)
    # Everything held, conviction-ranked equal-dollar opens under caps.
    assert [h["position_id"] for h in result["holds"]] == [1]
    assert [o["contract"]["ticker"] for o in result["opens"]] == ["AAA", "BBB"]
    for o in result["opens"]:
        assert o["cost"] <= 0.08 * 100_000 + 1e-6


def test_llm_garbage_json_uses_fallback(monkeypatch):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="I think you should buy calls!")
    monkeypatch.setattr(options_allocator, "llm_for", lambda *a, **k: llm)
    result = run([_cand("AAA")], [], "2026-07-17", {}, equity=100_000, cash=100_000)
    assert len(result["opens"]) == 1  # fallback still produced a decision set
    assert "report_md" in result


def test_no_candidates_no_positions(monkeypatch):
    _mock_llm(monkeypatch, [])
    result = run([], [], "2026-07-17", {}, equity=100_000, cash=100_000)
    assert result["closes"] == [] and result["holds"] == [] and result["opens"] == []

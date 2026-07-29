"""Deep dives must contribute to System C (the memory log).

Regression cover for the learning-loop gap where run_deep_dives read past
context but never stored its own decisions — the highest-volume decision path
(options + S&P scans) contributed nothing to nightly outcome grading.
"""

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from web import db, spy_scanner

pytestmark = pytest.mark.unit

DECISION = "Rating: Buy\nStrong momentum thesis; enter on pullback."


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


def _fake_orchestrator_cls(memory_log, decision=DECISION, signal="BUY"):
    """Stand-in for SwitchboardOrchestrator matching what _dive_inner uses."""

    class FakeOrchestrator:
        def __init__(self, config=None, selected_analysts=None, **kw):
            self.memory_log = memory_log

        def run(self, ticker, trade_date, **kw):
            return {"final_trade_decision": decision}, signal

    return FakeOrchestrator


def _run(tmp_db, monkeypatch, tmp_path, *, config_extra=None, decision=DECISION,
         store_raises=False, tickers=("AAPL",)):
    scan_id = db.create_spy_scan("2026-07-29", kind="options")
    config = {"memory_log_path": str(tmp_path / "mem.md")}
    config.update(config_extra or {})
    memory_log = TradingMemoryLog(config)
    if store_raises:
        memory_log = MagicMock(wraps=memory_log)
        memory_log.store_decision.side_effect = RuntimeError("disk full")
    monkeypatch.setattr(
        spy_scanner, "SwitchboardOrchestrator",
        _fake_orchestrator_cls(memory_log, decision=decision),
    )
    candidates = [{"ticker": t, "signal": "BUY", "conviction": 8} for t in tickers]
    enriched = spy_scanner.run_deep_dives(
        scan_id, candidates, "2026-07-29", config, ["market"],
    )
    return enriched, TradingMemoryLog(config)


def test_deep_dive_stores_pending_decision(tmp_db, monkeypatch, tmp_path):
    enriched, log = _run(tmp_db, monkeypatch, tmp_path)
    assert not enriched[0].get("error")
    entries = log.load_entries()
    assert len(entries) == 1
    assert entries[0]["ticker"] == "AAPL"
    assert entries[0]["pending"] is True
    assert entries[0]["rating"] == "Buy"


def test_store_failure_never_fails_the_dive(tmp_db, monkeypatch, tmp_path):
    """A memory-log write failure must not turn a good analysis into an
    'error' row (which would get dropped before vetting)."""
    enriched, _ = _run(tmp_db, monkeypatch, tmp_path, store_raises=True)
    assert not enriched[0].get("error"), "store failure leaked into the dive result"
    assert enriched[0]["signal"] == "BUY"


def test_empty_decision_stores_nothing(tmp_db, monkeypatch, tmp_path):
    _, log = _run(tmp_db, monkeypatch, tmp_path, decision="")
    assert log.load_entries() == []


def test_kill_switch_disables_store(tmp_db, monkeypatch, tmp_path):
    _, log = _run(tmp_db, monkeypatch, tmp_path,
                  config_extra={"deep_dive_store_decisions": False})
    assert log.load_entries() == []


def test_same_day_rerun_dedupes(tmp_db, monkeypatch, tmp_path):
    """Options and equity scans deep-diving the same ticker on the same date
    must not double-count it in calibration."""
    _run(tmp_db, monkeypatch, tmp_path)
    _, log = _run(tmp_db, monkeypatch, tmp_path)
    assert len(log.load_entries()) == 1

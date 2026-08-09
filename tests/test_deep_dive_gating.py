"""Per-LLM-call gating for deep dives (web/spy_scanner.py).

run_deep_dives now passes the DynamicGate into SwitchboardOrchestrator so a
dive holds a permit only for each llm.invoke, not for its entire multi-agent
run. The worker pool defaults to `budget` (i.e. `OLLAMA_MAX_CONCURRENCY`)
even under per-call gating; `DEEP_DIVE_POOL_MULTIPLIER` is the separate,
explicit opt-in knob for widening it. The DEEP_DIVE_PER_CALL_GATING env var
defaults ON and still rolls the gating position (per-dive vs per-call) back
together with the legacy kill switch; splitting that gating position from the
flag would deadlock.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from web import db, spy_scanner

pytestmark = pytest.mark.unit

# Module-level observability hooks, reset between tests by _clear_state.
_RECORDED_ORCHESTRATORS: list[Any] = []
_CONSTRUCTED_GATES: list[spy_scanner.DynamicGate] = []
_CAPTURED_POOL_SIZES: list[int | None] = []


@pytest.fixture(autouse=True)
def _clear_state():
    _RECORDED_ORCHESTRATORS.clear()
    _CONSTRUCTED_GATES.clear()
    _CAPTURED_POOL_SIZES.clear()
    yield


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


class CountingGate(spy_scanner.DynamicGate):
    """Real DynamicGate with counted acquire/release and an ordered log."""

    def __init__(self, limit: int) -> None:
        super().__init__(limit)
        self.acquires = 0
        self.releases = 0
        self.log: list[str] = []
        self._count_lock = threading.Lock()

    def acquire(self, weight: int = 1) -> None:
        with self._count_lock:
            self.acquires += 1
            self.log.append(f"acquire {weight}")
        super().acquire(weight)

    def release(self, weight: int = 1) -> None:
        with self._count_lock:
            self.releases += 1
            self.log.append(f"release {weight}")
        super().release(weight)


def _recording_orchestrator_cls(tmp_path, *, forbid_run=False, run_raises=None, rerun_raises=None):
    class RecordingOrchestrator:
        def __init__(self, config=None, selected_analysts=None, gate=None, tool_gate=None, **kw):
            self.gate = gate
            self.tool_gate = tool_gate
            self.memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md")})
            _RECORDED_ORCHESTRATORS.append(self)

        def run(self, ticker, trade_date, **kw):
            if forbid_run:
                raise AssertionError("run() must not be called on a reuse hit")
            if run_raises:
                raise run_raises
            return ({"final_trade_decision": "Rating: Buy\ndetail"}, "BUY")

        def rerun_decision(self, cached_state, ticker):
            if rerun_raises:
                raise rerun_raises
            return ({"final_trade_decision": "Rating: Sell\ndetail"}, "SELL")

    return RecordingOrchestrator


def _recording_pool_cls():
    class RecordingThreadPoolExecutor(ThreadPoolExecutor):
        def __init__(self, max_workers=None, *args, **kwargs):
            _CAPTURED_POOL_SIZES.append(max_workers)
            super().__init__(max_workers, *args, **kwargs)

    return RecordingThreadPoolExecutor


@pytest.fixture()
def counting_gate(monkeypatch):
    def factory(limit: int) -> CountingGate:
        gate = CountingGate(limit)
        _CONSTRUCTED_GATES.append(gate)
        return gate

    monkeypatch.setattr(spy_scanner, "DynamicGate", factory)


@pytest.fixture()
def recording_orch(monkeypatch, tmp_path):
    def _install(**kwargs):
        monkeypatch.setattr(
            spy_scanner,
            "SwitchboardOrchestrator",
            _recording_orchestrator_cls(tmp_path, **kwargs),
        )

    return _install


@pytest.fixture()
def recording_pool(monkeypatch):
    monkeypatch.setattr(spy_scanner, "ThreadPoolExecutor", _recording_pool_cls())


def _seed_donor(
    ticker="AAPL",
    trade_date="2026-08-08",
    fingerprint="fp-donor",
    status="completed",
    reused_from=None,
    age_hours=None,
):
    """Create an analyses row directly to act as a same-day donor."""
    analysis_id = db.create_analysis({
        "ticker": ticker, "trade_date": trade_date, "config_fingerprint": fingerprint,
    })
    if status == "completed":
        db.complete_analysis(
            analysis_id,
            {"company_of_interest": ticker, "final_trade_decision": "donor's own decision"},
            "BUY",
        )
    elif status == "failed":
        db.fail_analysis(analysis_id, "boom")
    if reused_from is not None:
        db.mark_analysis_reused(analysis_id, reused_from)
    if age_hours is not None:
        stale = (datetime.utcnow() - timedelta(hours=age_hours)).isoformat(timespec="seconds") + "Z"
        with db.connect() as conn:
            conn.execute("UPDATE analyses SET created_at = ? WHERE id = ?", (stale, analysis_id))
    return analysis_id


def _run(monkeypatch, *, config_extra=None, candidates=None, trade_date="2026-08-08", tickers=("AAPL",)):
    scan_id = db.create_spy_scan(trade_date, kind="options")
    config = {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}
    config.update(config_extra or {})
    cands = candidates or [{"ticker": t, "signal": "BUY", "conviction": 8} for t in tickers]
    enriched = spy_scanner.run_deep_dives(scan_id, cands, trade_date, config, ["market"])
    return scan_id, enriched


# ---------- default mode: per-call gating ----------

def test_default_passes_the_gate_to_the_orchestrator_and_takes_no_per_dive_permit(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch()

    scan_id, enriched = _run(monkeypatch, tickers=("AAPL", "MSFT", "TSLA"))

    assert len(enriched) == 3
    gates = [o.gate for o in _RECORDED_ORCHESTRATORS]
    assert all(g is not None for g in gates), gates
    assert len({id(g) for g in gates}) == 1, "every orchestrator must receive the same gate instance"
    assert _CONSTRUCTED_GATES[0].acquires == 0
    assert _CONSTRUCTED_GATES[0].releases == 0


def test_default_passes_the_tool_gate_to_the_orchestrator(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    monkeypatch.delenv("DEEP_DIVE_TOOL_CONCURRENCY", raising=False)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch()

    scan_id, enriched = _run(monkeypatch, tickers=("AAPL", "MSFT", "TSLA"))

    assert len(enriched) == 3
    tool_gates = [o.tool_gate for o in _RECORDED_ORCHESTRATORS]
    assert all(g is not None for g in tool_gates), tool_gates
    assert len({id(g) for g in tool_gates}) == 1, "every orchestrator must receive the same tool_gate instance"


def test_reuse_path_also_receives_the_gate(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch(forbid_run=True)
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    donor_id = _seed_donor(fingerprint=fp)

    scan_id, enriched = _run(monkeypatch)

    assert len(enriched) == 1
    row = enriched[0]
    assert row["reused_from"] == donor_id
    assert all(o.gate is not None for o in _RECORDED_ORCHESTRATORS)


def test_pool_width_matches_budget_by_default(
    tmp_db, monkeypatch, tmp_path, recording_orch, recording_pool,
):
    monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    monkeypatch.delenv("DEEP_DIVE_POOL_MULTIPLIER", raising=False)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch()

    _run(monkeypatch, tickers=("AAPL",))

    assert _CAPTURED_POOL_SIZES == [3]


def test_pool_width_widens_with_explicit_multiplier(
    tmp_db, monkeypatch, tmp_path, recording_orch, recording_pool,
):
    monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("DEEP_DIVE_POOL_MULTIPLIER", "2")
    recording_orch()

    _run(monkeypatch, tickers=("AAPL",))

    assert _CAPTURED_POOL_SIZES == [6]


def test_gate_capacity_is_unchanged_by_the_switch(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch()

    _run(monkeypatch, tickers=("AAPL",))

    assert _CONSTRUCTED_GATES[0].limit == 3


def test_tool_gate_default_limit_equals_budget(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.delenv("DEEP_DIVE_TOOL_CONCURRENCY", raising=False)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch()

    _run(monkeypatch, tickers=("AAPL",))

    assert _CONSTRUCTED_GATES[1].limit == 3


def test_tool_gate_limit_overridden_by_env(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.setenv("DEEP_DIVE_TOOL_CONCURRENCY", "2")
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch()

    _run(monkeypatch, tickers=("AAPL",))

    assert _CONSTRUCTED_GATES[1].limit == 2


def test_flag_is_resolved_exactly_once_per_scan(
    tmp_db, monkeypatch, tmp_path, recording_orch, recording_pool,
):
    monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch()

    calls = []
    orig = spy_scanner._per_call_gating_enabled

    def counted():
        calls.append(1)
        return orig()

    monkeypatch.setattr(spy_scanner, "_per_call_gating_enabled", counted)

    _run(monkeypatch, tickers=("AAPL", "MSFT", "TSLA", "NVDA"))

    assert len(calls) == 1, "flag must be resolved exactly once per scan (deadlock guard)"


@pytest.mark.parametrize("value,budget,expected", [
    (None, 5, 5),
    ("", 5, 5),
    ("not-a-number", 5, 5),
    ("3", 5, 3),
    ("0", 5, 1),
    ("-2", 5, 1),
    (" 4 ", 5, 4),
])
def test_tool_concurrency_parsing(value, budget, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("DEEP_DIVE_TOOL_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("DEEP_DIVE_TOOL_CONCURRENCY", value)
    assert spy_scanner._deep_dive_tool_concurrency(budget) == expected


# ---------- kill switch OFF: both halves revert together ----------

def test_kill_switch_off_restores_both_halves_together(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.setenv("DEEP_DIVE_PER_CALL_GATING", "0")
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch()

    scan_id, enriched = _run(monkeypatch, tickers=("AAPL", "MSFT"))

    assert len(enriched) == 2
    # (a) orchestrator receives gate=None
    assert all(o.gate is None for o in _RECORDED_ORCHESTRATORS)
    # (b) pool is budget-sized, not doubled
    assert _CAPTURED_POOL_SIZES == [3]
    # (c) exactly one acquire/release pair per dive
    gate = _CONSTRUCTED_GATES[0]
    assert gate.acquires == 2
    assert gate.releases == 2
    assert gate.limit == 3


def test_kill_switch_off_also_passes_gate_none_on_the_reuse_path(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.setenv("DEEP_DIVE_PER_CALL_GATING", "0")
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch(forbid_run=True)
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    _seed_donor(fingerprint=fp)

    _, enriched = _run(monkeypatch)

    assert enriched[0]["reused_from"] is not None
    assert all(o.gate is None for o in _RECORDED_ORCHESTRATORS)


def test_failed_dive_still_releases_the_legacy_per_dive_permit(
    tmp_db, monkeypatch, tmp_path, counting_gate, recording_orch, recording_pool,
):
    monkeypatch.setenv("DEEP_DIVE_PER_CALL_GATING", "0")
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    recording_orch(run_raises=RuntimeError("boom"))

    _, enriched = _run(monkeypatch, tickers=("AAPL",))

    assert len(enriched) == 1
    assert enriched[0].get("error")
    gate = _CONSTRUCTED_GATES[0]
    assert gate.acquires == gate.releases


def test_pool_multiplier_ignored_when_gating_off(
    tmp_db, monkeypatch, tmp_path, recording_orch, recording_pool,
):
    monkeypatch.setenv("DEEP_DIVE_PER_CALL_GATING", "0")
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("DEEP_DIVE_POOL_MULTIPLIER", "5")
    recording_orch()

    _run(monkeypatch, tickers=("AAPL",))

    assert _CAPTURED_POOL_SIZES == [3]


# ---------- kill switch parsing ----------

@pytest.mark.parametrize("value,expected", [
    ("0", False),
    ("false", False),
    ("FALSE", False),
    ("No", False),
    ("off", False),
    (" Off ", False),
    ("1", True),
    ("true", True),
    ("yes", True),
    ("anything", True),
    ("", True),
    (None, True),
])
def test_kill_switch_parsing(value, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    else:
        monkeypatch.setenv("DEEP_DIVE_PER_CALL_GATING", value)
    assert spy_scanner._per_call_gating_enabled() == expected


@pytest.mark.parametrize("value,expected", [
    (None, 1),
    ("", 1),
    ("1", 1),
    ("2", 2),
    ("0", 1),
    ("-5", 1),
    ("not-a-number", 1),
    (" 3 ", 3),
])
def test_pool_multiplier_parsing(value, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("DEEP_DIVE_POOL_MULTIPLIER", raising=False)
    else:
        monkeypatch.setenv("DEEP_DIVE_POOL_MULTIPLIER", value)
    assert spy_scanner._deep_dive_pool_multiplier() == expected


# ---------- behavioural equivalence ----------

def test_dive_results_are_unchanged_by_the_switch(
    tmp_db, monkeypatch, tmp_path, recording_orch, recording_pool,
):
    def core(row):
        return {"ticker": row["ticker"], "signal": row["signal"], "final_decision": row["final_decision"]}

    # Disable reuse so the second run does not inherit a donor row created
    # by the first run; we are testing the gating switch, not same-day reuse.
    monkeypatch.delenv("DEEP_DIVE_PER_CALL_GATING", raising=False)
    recording_orch()
    _, enriched_on = _run(monkeypatch, tickers=("AAPL",), config_extra={"deep_dive_reuse": False})

    monkeypatch.setenv("DEEP_DIVE_PER_CALL_GATING", "0")
    _RECORDED_ORCHESTRATORS.clear()
    _CAPTURED_POOL_SIZES.clear()
    recording_orch()
    _, enriched_off = _run(monkeypatch, tickers=("AAPL",), config_extra={"deep_dive_reuse": False})

    assert core(enriched_on[0]) == core(enriched_off[0])
    assert enriched_on[0]["analysis_id"] is not None
    assert enriched_off[0]["analysis_id"] is not None

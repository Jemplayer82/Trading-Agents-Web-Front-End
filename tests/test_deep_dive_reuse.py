"""Same-day deep-dive reuse across paper accounts (web/spy_scanner.py).

When multiple scans (different paper accounts, or equity + options) deep-dive
the same ticker on the same trade_date with an identical shared-stage config,
the second (and later) scan should reuse the first's analyst/debate/research-
manager work instead of paying for it again — see _deep_dive_fingerprint and
_dive_inner's reuse branch. Reuse must never be able to fail a dive: any
problem with the donor or the rerun falls back to a full pipeline run.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from web import db, spy_scanner

pytestmark = pytest.mark.unit

DECISION = "Rating: Buy\nStrong momentum thesis; enter on pullback."
REUSED_DECISION = "Rating: Sell\nBias-shifted call from the rerun."


@pytest.fixture(autouse=True)
def _clear_spy_price_cache():
    spy_scanner._PRICE_DATA_CACHE.clear()
    yield
    spy_scanner._PRICE_DATA_CACHE.clear()


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


def _fake_orchestrator_cls(
    memory_log,
    *,
    run_result=({"final_trade_decision": DECISION}, "BUY"),
    run_raises=None,
    rerun_result=({"final_trade_decision": REUSED_DECISION}, "SELL"),
    rerun_raises=None,
    forbid_run=False,
):
    """Stand-in for SwitchboardOrchestrator matching what _dive_inner uses.

    `run_result`/`rerun_result` are (final_state, signal) tuples. The fake
    doesn't touch cached_state's contents at all — validation of the
    cached-state contract is covered at the orchestrator level in
    tests/test_rerun_decision.py.
    """

    class FakeOrchestrator:
        def __init__(self, config=None, selected_analysts=None, **kw):
            self.memory_log = memory_log

        def run(self, ticker, trade_date, **kw):
            if forbid_run:
                raise AssertionError("run() must not be called on a reuse hit")
            if run_raises:
                raise run_raises
            return run_result

        def rerun_decision(self, cached_state, ticker):
            if rerun_raises:
                raise rerun_raises
            return rerun_result

    return FakeOrchestrator


def _install_fake(monkeypatch, tmp_path, **kwargs):
    memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md")})
    monkeypatch.setattr(spy_scanner, "SwitchboardOrchestrator", _fake_orchestrator_cls(memory_log, **kwargs))
    return memory_log


def _seed_donor(
    ticker="AAPL",
    trade_date="2026-08-08",
    fingerprint="fp-donor",
    status="completed",
    reused_from=None,
    age_hours=None,
):
    """Create an analyses row directly (bypassing the orchestrator) to act
    as a same-day donor for a later reuse lookup."""
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
    # else: leave status='running' (the default from create_analysis)
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


# ---------- fingerprint ----------

def test_fingerprint_stable_for_identical_config():
    config = {"deep_think_llm": "claude-opus-5", "quick_think_llm": "claude-sonnet-5",
               "max_debate_rounds": 2, "max_risk_discuss_rounds": 2}
    assert (spy_scanner._deep_dive_fingerprint(config, ["market", "news"])
            == spy_scanner._deep_dive_fingerprint(config, ["market", "news"]))


def test_fingerprint_insensitive_to_analyst_order():
    config = {"deep_think_llm": "claude-opus-5"}
    assert (spy_scanner._deep_dive_fingerprint(config, ["news", "market"])
            == spy_scanner._deep_dive_fingerprint(config, ["market", "news"]))


def test_fingerprint_insensitive_to_bias_and_memory_paths():
    base = {"deep_think_llm": "claude-opus-5", "memory_log_path": "/a/mem.md"}
    other = {"deep_think_llm": "claude-opus-5", "memory_log_path": "/b/mem.md", "bias": "bullish"}
    assert spy_scanner._deep_dive_fingerprint(base, ["market"]) == spy_scanner._deep_dive_fingerprint(other, ["market"])


@pytest.mark.parametrize("field,a,b", [
    ("deep_think_llm", "claude-opus-5", "claude-sonnet-5"),
    ("max_debate_rounds", 1, 2),
    ("max_risk_discuss_rounds", 1, 2),
    ("output_language", "English", "Spanish"),
    ("anthropic_effort", None, "high"),
])
def test_fingerprint_changes_on_shared_stage_config(field, a, b):
    base = {"deep_think_llm": "claude-opus-5"}
    fp_a = spy_scanner._deep_dive_fingerprint({**base, field: a}, ["market"])
    fp_b = spy_scanner._deep_dive_fingerprint({**base, field: b}, ["market"])
    assert fp_a != fp_b


def test_fingerprint_changes_on_analyst_set():
    config = {"deep_think_llm": "claude-opus-5"}
    assert (spy_scanner._deep_dive_fingerprint(config, ["market"])
            != spy_scanner._deep_dive_fingerprint(config, ["market", "news"]))


def test_fingerprint_changes_on_data_vendor():
    config_a = {"data_vendors": {"technical_indicators": "yfinance"}}
    config_b = {"data_vendors": {"technical_indicators": "alpha_vantage"}}
    assert (spy_scanner._deep_dive_fingerprint(config_a, ["market"])
            != spy_scanner._deep_dive_fingerprint(config_b, ["market"]))


# ---------- end-to-end reuse hit ----------

def test_reuse_hit_skips_full_pipeline_and_marks_provenance(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    donor_id = _seed_donor(fingerprint=fp)
    _install_fake(monkeypatch, tmp_path, forbid_run=True)

    scan_id, enriched = _run(monkeypatch)

    assert len(enriched) == 1
    row = enriched[0]
    assert not row.get("error")
    assert row["reused_from"] == donor_id
    assert row["final_decision"] == REUSED_DECISION

    new_id = row["analysis_id"]
    assert new_id != donor_id
    saved = db.get_analysis(new_id)
    assert saved["status"] == "completed"
    assert saved["reused_from_analysis_id"] == donor_id
    assert saved["final_decision"] == REUSED_DECISION

    # spy_quick_results is backfilled with the NEW analysis id, not the donor's.
    with db.connect() as conn:
        qr = conn.execute(
            "SELECT analysis_id FROM spy_quick_results WHERE scan_id = ? AND ticker = ?",
            (scan_id, "AAPL"),
        ).fetchone()
    assert qr["analysis_id"] == new_id

    scan = db.get_spy_scan(scan_id)
    assert scan["deep_reused_count"] == 1


def test_reuse_stores_decision_in_memory_log(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    _seed_donor(fingerprint=fp)
    memory_log = _install_fake(monkeypatch, tmp_path, forbid_run=True)

    _run(monkeypatch)

    entries = memory_log.load_entries()
    assert len(entries) == 1
    assert entries[0]["ticker"] == "AAPL"


# ---------- miss cases ----------

def test_miss_on_fingerprint_mismatch_runs_full_pipeline(tmp_db, monkeypatch, tmp_path):
    _seed_donor(fingerprint="totally-different-fingerprint")
    _install_fake(monkeypatch, tmp_path)  # run() allowed

    scan_id, enriched = _run(monkeypatch)

    assert enriched[0]["final_decision"] == DECISION
    assert "reused_from" not in enriched[0]
    scan = db.get_spy_scan(scan_id)
    assert scan["deep_reused_count"] == 0


def test_miss_on_donor_still_running(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    _seed_donor(fingerprint=fp, status="running")
    _install_fake(monkeypatch, tmp_path)

    _, enriched = _run(monkeypatch)
    assert "reused_from" not in enriched[0]


def test_miss_on_donor_failed(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    _seed_donor(fingerprint=fp, status="failed")
    _install_fake(monkeypatch, tmp_path)

    _, enriched = _run(monkeypatch)
    assert "reused_from" not in enriched[0]


def test_miss_on_null_fingerprint_donor(tmp_db, monkeypatch, tmp_path):
    """Interactive Run Analysis rows (api container) never set a fingerprint
    and must never be picked up as a donor."""
    _seed_donor(fingerprint=None)
    _install_fake(monkeypatch, tmp_path)

    _, enriched = _run(monkeypatch)
    assert "reused_from" not in enriched[0]


def test_no_chaining_reused_row_is_never_itself_a_donor(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    original_id = _seed_donor(fingerprint=fp)
    _seed_donor(fingerprint=fp, reused_from=original_id)  # a copy — must be skipped
    _install_fake(monkeypatch, tmp_path, forbid_run=True)

    _, enriched = _run(monkeypatch)

    assert enriched[0]["reused_from"] == original_id


def test_miss_on_donor_older_than_max_age(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    _seed_donor(fingerprint=fp, age_hours=7)  # older than the 6h default
    _install_fake(monkeypatch, tmp_path)

    _, enriched = _run(monkeypatch)
    assert "reused_from" not in enriched[0]


def test_hit_on_donor_just_inside_max_age(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    donor_id = _seed_donor(fingerprint=fp, age_hours=5)
    _install_fake(monkeypatch, tmp_path, forbid_run=True)

    _, enriched = _run(monkeypatch)
    assert enriched[0]["reused_from"] == donor_id


def test_kill_switch_disables_reuse(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": False, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    _seed_donor(fingerprint=fp)
    _install_fake(monkeypatch, tmp_path)  # run() allowed — the point of this test

    _, enriched = _run(monkeypatch, config_extra={"deep_dive_reuse": False})
    assert "reused_from" not in enriched[0]
    assert enriched[0]["final_decision"] == DECISION


# ---------- fallback on rerun failure ----------

def test_rerun_failure_falls_back_to_full_pipeline_no_orphan_row(tmp_db, monkeypatch, tmp_path):
    fp = spy_scanner._deep_dive_fingerprint(
        {"deep_dive_reuse": True, "deep_dive_reuse_max_age_hours": 6}, ["market"],
    )
    _seed_donor(fingerprint=fp)
    _install_fake(monkeypatch, tmp_path, rerun_raises=RuntimeError("bus down"))

    scan_id, enriched = _run(monkeypatch)

    assert not enriched[0].get("error")
    assert "reused_from" not in enriched[0]
    assert enriched[0]["final_decision"] == DECISION

    # No analyses row for this dive should be left in 'running' — the reuse
    # attempt must not have pre-created a row that gets abandoned.
    with db.connect() as conn:
        running = conn.execute(
            "SELECT id FROM analyses WHERE ticker = 'AAPL' AND status = 'running'"
        ).fetchall()
    assert running == [], "a failed rerun must not leave an orphaned 'running' analysis row"


# =========================================================================
# Phase B — same-day quick-scan reuse (web/spy_scanner.py run_quick_scan)
# =========================================================================

QUICK_CONFIG = {"quick_think_llm": "gpt-4o-mini", "llm_provider": "openai"}


class _FakeQuickLLM:
    """Counts invocations so tests can assert reused tickers never call it."""

    def __init__(self):
        self.calls = 0
        self.seen_prompts: list[str] = []

    def invoke(self, messages):
        self.calls += 1
        self.seen_prompts.append(messages[-1]["content"])

        class _Resp:
            content = "SIGNAL: BUY\nCONVICTION: 7\nREASONING: fresh scan"

        return _Resp()


def _fake_price_frame(tickers, n=10, base_price=100.0):
    """Minimal stand-in for yf.download's multi-ticker MultiIndex shape:
    columns are (field, ticker) pairs, e.g. raw["Close"][ticker]."""
    idx = pd.date_range("2026-07-01", periods=n)
    data = {}
    for i, t in enumerate(tickers):
        data[("Close", t)] = [base_price + i * 10 + j for j in range(n)]
        data[("Volume", t)] = [1_000_000] * n
    return pd.DataFrame(data, index=idx)


def _install_quick_fakes(monkeypatch, tickers):
    fake_llm = _FakeQuickLLM()
    monkeypatch.setattr(spy_scanner, "_llm_quick", lambda config: fake_llm)
    monkeypatch.setattr(spy_scanner.yf, "download", lambda *a, **kw: _fake_price_frame(tickers))
    return fake_llm


def _seed_quick_donor(trade_date, fingerprint, rows, status="completed"):
    """A prior same-day scan whose spy_quick_results rows are reuse candidates.

    `rows` is {ticker: {"signal": ..., "conviction": ..., "reasoning": ..., "error": ...}}.
    """
    scan_id = db.create_spy_scan(trade_date, kind="options")
    db.update_spy_scan(scan_id, status=status, quick_fingerprint=fingerprint)
    for ticker, row in rows.items():
        db.upsert_spy_quick_result(
            scan_id=scan_id, ticker=ticker,
            signal=row.get("signal"), conviction=row.get("conviction"),
            reasoning=row.get("reasoning"), error=row.get("error"),
        )
    return scan_id


def test_quick_reuse_partial_coverage_skips_llm_only_for_covered_tickers(tmp_db, monkeypatch, tmp_path):
    trade_date = "2026-08-08"
    fp = spy_scanner._quick_scan_fingerprint(QUICK_CONFIG)
    _seed_quick_donor(trade_date, fp, {
        "AAPL": {"signal": "BUY", "conviction": 8, "reasoning": "donor reasoning AAPL"},
        "MSFT": {"signal": "SELL", "conviction": 6, "reasoning": "donor reasoning MSFT"},
    })
    fake_llm = _install_quick_fakes(monkeypatch, ["AAPL", "MSFT", "TSLA"])

    scan_id = db.create_spy_scan(trade_date, kind="options")
    results = spy_scanner.run_quick_scan(scan_id, ["AAPL", "MSFT", "TSLA"], trade_date, QUICK_CONFIG)

    by_ticker = {r["ticker"]: r for r in results}
    assert by_ticker["AAPL"]["signal"] == "BUY"
    assert by_ticker["AAPL"]["conviction"] == 8
    assert by_ticker["AAPL"]["reasoning"] == "donor reasoning AAPL"
    assert by_ticker["MSFT"]["signal"] == "SELL"

    # entry_price is always fresh — never copied from the donor (which never
    # even had one stored) — it comes from THIS scan's own price fetch.
    assert by_ticker["AAPL"]["entry_price"] == pytest.approx(100.0 + 9)  # last close, i=0 series
    assert by_ticker["MSFT"]["entry_price"] > 0

    # Only the uncovered ticker (TSLA) should have hit the LLM.
    assert fake_llm.calls == 1
    assert "TSLA" in fake_llm.seen_prompts[0]

    scan = db.get_spy_scan(scan_id)
    assert scan["quick_fingerprint"] == fp


def test_quick_reuse_error_rows_are_never_reused(tmp_db, monkeypatch, tmp_path):
    trade_date = "2026-08-08"
    fp = spy_scanner._quick_scan_fingerprint(QUICK_CONFIG)
    _seed_quick_donor(trade_date, fp, {
        "AAPL": {"signal": "HOLD", "conviction": 1, "reasoning": "scan error: boom", "error": "boom"},
    })
    fake_llm = _install_quick_fakes(monkeypatch, ["AAPL"])

    scan_id = db.create_spy_scan(trade_date, kind="options")
    results = spy_scanner.run_quick_scan(scan_id, ["AAPL"], trade_date, QUICK_CONFIG)

    assert fake_llm.calls == 1, "an error row must never be reused"
    assert results[0]["signal"] == "BUY"  # from the fake LLM, not the donor's HOLD


def test_quick_reuse_skips_donor_scans_that_never_completed(tmp_db, monkeypatch, tmp_path):
    trade_date = "2026-08-08"
    fp = spy_scanner._quick_scan_fingerprint(QUICK_CONFIG)
    _seed_quick_donor(trade_date, fp, {
        "AAPL": {"signal": "BUY", "conviction": 8, "reasoning": "from a scan that never finished"},
    }, status="failed")
    fake_llm = _install_quick_fakes(monkeypatch, ["AAPL"])

    scan_id = db.create_spy_scan(trade_date, kind="options")
    spy_scanner.run_quick_scan(scan_id, ["AAPL"], trade_date, QUICK_CONFIG)

    assert fake_llm.calls == 1, "a failed source scan's rows must never donate"


def test_quick_reuse_kill_switch_disables_reuse(tmp_db, monkeypatch, tmp_path):
    trade_date = "2026-08-08"
    fp = spy_scanner._quick_scan_fingerprint(QUICK_CONFIG)
    _seed_quick_donor(trade_date, fp, {
        "AAPL": {"signal": "BUY", "conviction": 8, "reasoning": "donor"},
    })
    fake_llm = _install_quick_fakes(monkeypatch, ["AAPL"])

    scan_id = db.create_spy_scan(trade_date, kind="options")
    config = {**QUICK_CONFIG, "quick_scan_reuse": False}
    results = spy_scanner.run_quick_scan(scan_id, ["AAPL"], trade_date, config)

    assert fake_llm.calls == 1, "kill switch must force every ticker through the LLM"
    assert results[0]["signal"] == "BUY"  # from the fake LLM (coincidentally same value as donor)

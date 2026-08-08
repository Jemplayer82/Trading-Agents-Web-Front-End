"""Scan-level shared macro-news brief (web/spy_scanner.py run_deep_dives).

One shared macro/global-news brief is computed ONCE per run_deep_dives call
(not once per ticker) and stashed on config['macro_brief'] so every dive's
news analyst can reuse it instead of independently re-fetching/re-summarizing
the same ticker-independent macro news.
"""
from types import SimpleNamespace

import pytest

from web import db, spy_scanner

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


class _FakeOrchestrator:
    """Stand-in for SwitchboardOrchestrator matching what _dive_inner uses."""

    def __init__(self, config=None, selected_analysts=None, **kw):
        pass

    def run(self, ticker, trade_date, **kw):
        return {"final_trade_decision": "Rating: Hold"}, "HOLD"


def _install(monkeypatch, *, fetch_raises=False, summarize_raises=False,
             fetch_text="Fed holds rates; oil up 2%.",
             summary_text="Macro brief: Fed steady, oil up."):
    monkeypatch.setattr(spy_scanner, "SwitchboardOrchestrator", _FakeOrchestrator)

    calls = {"fetch": 0, "summarize": 0}

    def fake_fetch(curr_date, *a, **kw):
        calls["fetch"] += 1
        if fetch_raises:
            raise RuntimeError("vendor down")
        return fetch_text

    monkeypatch.setattr(spy_scanner.get_global_news, "func", fake_fetch)

    class _FakeQuickLLM:
        def invoke(self, prompt):
            calls["summarize"] += 1
            if summarize_raises:
                raise RuntimeError("llm down")
            return SimpleNamespace(content=summary_text)

    class _FakeQuickClient:
        def get_llm(self):
            return _FakeQuickLLM()

    monkeypatch.setattr(spy_scanner, "create_llm_client", lambda **kw: _FakeQuickClient())
    return calls


def _run(tickers, config_extra=None, selected_analysts=("news",), trade_date="2026-08-08"):
    scan_id = db.create_spy_scan(trade_date, kind="options")
    config = {"quick_think_llm": "stub-model", "llm_provider": "openai", **(config_extra or {})}
    candidates = [{"ticker": t, "signal": "BUY", "conviction": 8} for t in tickers]
    spy_scanner.run_deep_dives(scan_id, candidates, trade_date, config, list(selected_analysts))
    return config


def test_macro_brief_computed_exactly_once_regardless_of_candidate_count(tmp_db, monkeypatch):
    calls = _install(monkeypatch)
    config = _run(tickers=["AAPL", "MSFT", "TSLA"])
    assert calls["fetch"] == 1
    assert calls["summarize"] == 1
    assert config["macro_brief"] == "Macro brief: Fed steady, oil up."


def test_fetch_failure_leaves_macro_brief_unset_and_does_not_raise(tmp_db, monkeypatch):
    calls = _install(monkeypatch, fetch_raises=True)
    config = _run(tickers=["AAPL"])
    assert config.get("macro_brief") is None
    assert calls["fetch"] == 1
    assert calls["summarize"] == 0


def test_summarizer_failure_leaves_macro_brief_unset_and_does_not_raise(tmp_db, monkeypatch):
    _install(monkeypatch, summarize_raises=True)
    config = _run(tickers=["AAPL"])
    assert config.get("macro_brief") is None


def test_macro_brief_enabled_false_skips_computation_entirely(tmp_db, monkeypatch):
    calls = _install(monkeypatch)
    config = _run(tickers=["AAPL"], config_extra={"macro_brief_enabled": False})
    assert calls["fetch"] == 0
    assert calls["summarize"] == 0
    assert "macro_brief" not in config


def test_news_not_selected_skips_macro_brief(tmp_db, monkeypatch):
    calls = _install(monkeypatch)
    config = _run(tickers=["AAPL"], selected_analysts=("market",))
    assert calls["fetch"] == 0
    assert "macro_brief" not in config


def test_preexisting_macro_brief_is_never_overwritten(tmp_db, monkeypatch):
    calls = _install(monkeypatch)
    config = _run(tickers=["AAPL"], config_extra={"macro_brief": "already set by caller"})
    assert calls["fetch"] == 0
    assert config["macro_brief"] == "already set by caller"

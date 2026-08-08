"""SwitchboardOrchestrator.rerun_decision — reruns only the Portfolio Manager
over a previously completed pipeline state, for same-day deep-dive reuse
across paper accounts (see web/spy_scanner.py _dive_inner).

Everything upstream of the Portfolio Manager is a pure function of
(ticker, trade_date, shared-stage config), so a same-day analysis from one
paper account can donate that stage to another. Only bias and the memory
log's past_context are account-specific, and both must be re-applied fresh
on every rerun — that's the behavior under test here.
"""

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator import CachedStateInvalid, SwitchboardOrchestrator
from tradingagents.orchestrator import switchboard_orchestrator as sbo_module

pytestmark = pytest.mark.unit

DECISION = "**Rating**: Buy\n\nStrong momentum thesis; enter on pullback."
VALID_HISTORY = "Aggressive: risk is worth it. " * 10  # > _MIN_RISK_HISTORY_CHARS


def _config(tmp_path, bias="neutral"):
    return {
        **DEFAULT_CONFIG,
        "llm_provider": "openai",
        "deep_think_llm": "gpt-4o-mini",
        "quick_think_llm": "gpt-4o-mini",
        "memory_log_path": str(tmp_path / "mem.md"),
        "data_cache_dir": str(tmp_path / "cache"),
        "results_dir": str(tmp_path / "results"),
        "bias": bias,
    }


def _valid_cached_state(ticker="AAPL", **overrides):
    state = {
        "company_of_interest": ticker,
        "asset_type": "stock",
        "trade_date": "2026-08-08",
        "past_context": "STALE — must never reach the Portfolio Manager",
        "bias_context": "STALE — must never reach the Portfolio Manager",
        "investment_debate_state": {"history": "bull vs bear transcript"},
        "risk_debate_state": {
            "history": VALID_HISTORY,
            "aggressive_history": "a", "conservative_history": "c", "neutral_history": "n",
            "current_aggressive_response": "", "current_conservative_response": "",
            "current_neutral_response": "", "count": 3,
        },
        "market_report": "market report text",
        "fundamentals_report": "fundamentals report text",
        "sentiment_report": "sentiment report text",
        "news_report": "news report text",
        "investment_plan": "research manager's plan",
        "trader_investment_plan": "trader's proposal",
        "final_trade_decision": "DONOR'S OWN DECISION — must never leak",
        "messages": [{"type": "human", "content": "serialized junk from json round-trip"}],
    }
    state.update(overrides)
    return state


def _fake_pm_factory(record, decision=DECISION):
    """Stand-in for create_portfolio_manager: records the state it receives
    and returns the same update shape the real node returns."""

    def factory(llm):
        def node(state):
            record.append(dict(state))
            new_risk = dict(state["risk_debate_state"])
            new_risk["judge_decision"] = decision
            new_risk["latest_speaker"] = "Judge"
            return {"risk_debate_state": new_risk, "final_trade_decision": decision}

        return node

    return factory


@pytest.fixture()
def orchestrator(tmp_path):
    return SwitchboardOrchestrator(config=_config(tmp_path, bias="bullish"), selected_analysts=["market"])


def test_happy_path_returns_new_state_and_parsed_signal(orchestrator, monkeypatch):
    record = []
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory(record))

    state, signal = orchestrator.rerun_decision(_valid_cached_state(), "AAPL")

    assert signal == "Buy"
    assert state["final_trade_decision"] == DECISION
    # Shared-stage fields carried through unchanged.
    assert state["market_report"] == "market report text"
    assert state["fundamentals_report"] == "fundamentals report text"
    assert state["sentiment_report"] == "sentiment report text"
    assert state["news_report"] == "news report text"
    assert state["investment_plan"] == "research manager's plan"
    assert state["trader_investment_plan"] == "trader's proposal"


def test_bias_override_wins_over_cached_bias_context(orchestrator, monkeypatch):
    """The donor's bias_context must never reach the PM — this account's
    config bias always wins."""
    record = []
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory(record))

    cached = _valid_cached_state(bias_context="Context: bearish market stance — lean Sell.")
    orchestrator.rerun_decision(cached, "AAPL")

    assert len(record) == 1
    seen_bias_context = record[0]["bias_context"]
    assert seen_bias_context == sbo_module._BIAS_CONTEXT["bullish"]
    assert seen_bias_context != cached["bias_context"]


def test_final_decision_is_new_pm_output_not_cached_text(orchestrator, monkeypatch):
    record = []
    new_decision = "**Rating**: Sell\n\nBias-shifted call."
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory(record, decision=new_decision))

    cached = _valid_cached_state()  # final_trade_decision = "DONOR'S OWN DECISION..."
    state, signal = orchestrator.rerun_decision(cached, "AAPL")

    assert state["final_trade_decision"] == new_decision
    assert state["final_trade_decision"] != cached["final_trade_decision"]
    assert signal == "Sell"


def test_past_context_is_freshly_loaded_not_cached(orchestrator, monkeypatch):
    record = []
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory(record))
    monkeypatch.setattr(orchestrator.memory_log, "get_past_context", lambda ticker: "FRESH_CONTEXT_MARKER")

    cached = _valid_cached_state(past_context="STALE_DONOR_CONTEXT")
    orchestrator.rerun_decision(cached, "AAPL")

    assert record[0]["past_context"] == "FRESH_CONTEXT_MARKER"


def test_messages_are_replaced_not_the_serialized_cached_value(orchestrator, monkeypatch):
    record = []
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory(record))

    orchestrator.rerun_decision(_valid_cached_state(), "AAPL")

    assert record[0]["messages"] == [("human", "AAPL")]


def test_risk_debate_state_new_values_replace_the_cached_ones(orchestrator, monkeypatch):
    record = []
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory(record))

    state, _ = orchestrator.rerun_decision(_valid_cached_state(), "AAPL")

    assert state["risk_debate_state"]["judge_decision"] == DECISION
    assert state["risk_debate_state"]["latest_speaker"] == "Judge"
    # The shared debate content underneath is still the donor's.
    assert state["risk_debate_state"]["history"] == VALID_HISTORY


@pytest.mark.parametrize(
    "mutate,expected_msg_fragment",
    [
        (lambda s: "not a dict", "not a dict"),
        (lambda s: {**s, "company_of_interest": "MSFT"}, "ticker mismatch"),
        (lambda s: {**s, "investment_plan": ""}, "investment_plan"),
        (lambda s: {**s, "investment_plan": None}, "investment_plan"),
        (lambda s: {**s, "trader_investment_plan": ""}, "trader_investment_plan"),
        (lambda s: {**s, "risk_debate_state": "not a dict"}, "risk_debate_state"),
        (
            lambda s: {**s, "risk_debate_state": {k: v for k, v in s["risk_debate_state"].items() if k != "history"}},
            "risk_debate_state",
        ),
        (
            lambda s: {**s, "risk_debate_state": {k: v for k, v in s["risk_debate_state"].items() if k != "count"}},
            "risk_debate_state",
        ),
        (
            lambda s: {**s, "risk_debate_state": {**s["risk_debate_state"], "history": "too short"}},
            "too short",
        ),
    ],
    ids=[
        "non-dict-state", "ticker-mismatch", "empty-investment-plan", "none-investment-plan",
        "empty-trader-plan", "risk-state-not-dict", "risk-state-missing-history",
        "risk-state-missing-count", "risk-history-too-short",
    ],
)
def test_cached_state_invalid_on_malformed_input(orchestrator, monkeypatch, mutate, expected_msg_fragment):
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory([]))
    bad_state = mutate(_valid_cached_state())

    with pytest.raises(CachedStateInvalid, match=expected_msg_fragment):
        orchestrator.rerun_decision(bad_state, "AAPL")


@pytest.mark.parametrize("key", [
    "aggressive_history", "conservative_history", "neutral_history",
    "current_aggressive_response", "current_conservative_response", "current_neutral_response",
])
def test_cached_state_invalid_on_each_missing_risk_key(orchestrator, monkeypatch, key):
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory([]))
    cached = _valid_cached_state()
    del cached["risk_debate_state"][key]

    with pytest.raises(CachedStateInvalid, match="risk_debate_state"):
        orchestrator.rerun_decision(cached, "AAPL")


def test_corrupt_string_full_state_is_invalid(orchestrator, monkeypatch):
    """Mirrors what db.get_analysis returns for a row whose full_state failed
    to json-decode: the raw string, not a dict."""
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory([]))

    with pytest.raises(CachedStateInvalid, match="not a dict"):
        orchestrator.rerun_decision("{not actually json-decoded}", "AAPL")


def test_empty_pm_output_raises_instead_of_silently_succeeding(orchestrator, monkeypatch):
    """A half-broken provider returning empty content must not look like a
    valid Hold decision — that's the exact 'silent all-Hold' failure mode
    the reuse path must never produce."""
    record = []
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory(record, decision=""))

    with pytest.raises(CachedStateInvalid, match="empty decision"):
        orchestrator.rerun_decision(_valid_cached_state(), "AAPL")


def test_whitespace_only_pm_output_raises(orchestrator, monkeypatch):
    record = []
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", _fake_pm_factory(record, decision="   \n  "))

    with pytest.raises(CachedStateInvalid, match="empty decision"):
        orchestrator.rerun_decision(_valid_cached_state(), "AAPL")

import pytest
from langchain_core.messages import AIMessage

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator import switchboard_orchestrator as sbo_module
from tradingagents.orchestrator.switchboard_orchestrator import SwitchboardOrchestrator

pytestmark = pytest.mark.unit


def _make_orchestrator(tmp_path, selected_analysts, extra_config=None):
    config = {
        **DEFAULT_CONFIG,
        "llm_provider": "openai",
        "deep_think_llm": "gpt-4o-mini",
        "quick_think_llm": "gpt-4o-mini",
        "memory_log_path": str(tmp_path / "mem.md"),
        "data_cache_dir": str(tmp_path / "cache"),
        "results_dir": str(tmp_path / "results"),
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
    }
    if extra_config is not None:
        config.update(extra_config)
    return SwitchboardOrchestrator(
        config=config, selected_analysts=selected_analysts
    )


def _fake_node_factory(update):
    def factory(llm):
        def node(state):
            return update
        return node
    return factory


def _investment_debate_state():
    return {
        "count": 1,
        "history": "bull vs bear debate",
        "bull_history": "",
        "bear_history": "",
        "current_response": "",
        "judge_decision": "",
    }


def _risk_debate_state():
    return {
        "count": 1,
        "history": "risk debate",
        "aggressive_history": "",
        "conservative_history": "",
        "neutral_history": "",
        "latest_speaker": "",
        "current_aggressive_response": "",
        "current_conservative_response": "",
        "current_neutral_response": "",
        "judge_decision": "",
    }


def _patch_downstream_factories(monkeypatch):
    """Monkeypatch every node factory run() calls after the analyst phase
    so the test exercises the real orchestrator code without real LLM calls.
    """
    monkeypatch.setattr(
        sbo_module,
        "create_bull_researcher",
        _fake_node_factory({"investment_debate_state": _investment_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_bear_researcher",
        _fake_node_factory({"investment_debate_state": _investment_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_research_manager",
        _fake_node_factory({
            "messages": [AIMessage(content="plan", tool_calls=[])],
            "investment_plan": "research manager plan",
        }),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_trader",
        _fake_node_factory({"trader_investment_plan": "trader plan"}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_aggressive_debator",
        _fake_node_factory({"risk_debate_state": _risk_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_conservative_debator",
        _fake_node_factory({"risk_debate_state": _risk_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_neutral_debator",
        _fake_node_factory({"risk_debate_state": _risk_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_portfolio_manager",
        _fake_node_factory({
            "messages": [AIMessage(content="decision", tool_calls=[])],
            "final_trade_decision": "**Rating**: Buy\n\nRationale.",
        }),
    )


@pytest.mark.parametrize(
    "extra_config, expected_macro_brief",
    [
        ({"macro_brief": "SENTINEL_MACRO_BRIEF_12345"}, "SENTINEL_MACRO_BRIEF_12345"),
        ({}, None),
    ],
)
def test_news_analyst_factory_passes_macro_brief_from_config(
    tmp_path, monkeypatch, extra_config, expected_macro_brief
):
    """The news analyst factory receives self._quick_llm and the live
    self.config.get('macro_brief') value when run() builds the pipeline.
    Exercises the real code path, not a regex over source text.
    """
    orchestrator = _make_orchestrator(
        tmp_path, selected_analysts=["news"], extra_config=extra_config
    )
    orchestrator.signal_processor.process_signal = lambda decision: "Buy"

    captured = {}

    def fake_create_news_analyst(llm, macro_brief=None):
        captured["llm"] = llm
        captured["macro_brief"] = macro_brief
        return lambda state: {
            "messages": [AIMessage(content="news report", tool_calls=[])],
            "news_report": "news report",
        }

    monkeypatch.setattr(sbo_module, "create_news_analyst", fake_create_news_analyst)
    _patch_downstream_factories(monkeypatch)

    state, signal = orchestrator.run("AAPL", "2026-08-08")

    assert captured["llm"] is orchestrator._quick_llm
    assert captured["macro_brief"] == expected_macro_brief
    assert state.get("news_report") == "news report"
    assert signal == "Buy"


def test_other_analyst_factories_receive_llm_not_macro_brief(tmp_path, monkeypatch):
    """market, sentiment, and fundamentals analysts still get only the quick LLM
    and are never passed a macro_brief keyword argument.
    """
    orchestrator = _make_orchestrator(
        tmp_path,
        selected_analysts=["market", "social", "fundamentals"],
    )
    orchestrator.signal_processor.process_signal = lambda decision: "Buy"

    captured = {}

    def make_analyst_factory(name, report_key):
        def factory(llm, **kwargs):
            captured[name] = {"llm": llm, "kwargs": kwargs}
            return lambda state: {
                "messages": [AIMessage(content=f"{name} report", tool_calls=[])],
                report_key: f"{name} report",
            }
        return factory

    monkeypatch.setattr(
        sbo_module,
        "create_market_analyst",
        make_analyst_factory("market", "market_report"),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_sentiment_analyst",
        make_analyst_factory("sentiment", "sentiment_report"),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_fundamentals_analyst",
        make_analyst_factory("fundamentals", "fundamentals_report"),
    )
    _patch_downstream_factories(monkeypatch)

    state, signal = orchestrator.run("AAPL", "2026-08-08")

    for name in ("market", "sentiment", "fundamentals"):
        assert captured[name]["llm"] is orchestrator._quick_llm
        assert "macro_brief" not in captured[name]["kwargs"]

    assert state.get("market_report") == "market report"
    assert state.get("sentiment_report") == "sentiment report"
    assert state.get("fundamentals_report") == "fundamentals report"
    assert signal == "Buy"

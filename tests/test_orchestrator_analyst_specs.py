"""Tests for the orchestrator analyst spec table and node builder."""

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.orchestrator import switchboard_orchestrator as sbo_module
from tradingagents.orchestrator.switchboard_orchestrator import (
    SwitchboardOrchestrator,
    _ANALYST_SPECS,
)

pytestmark = pytest.mark.unit


def _orch(tmp_path, **extra):
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
    config.update(extra)
    return SwitchboardOrchestrator(config=config, selected_analysts=["market"])


def test_spec_keys_are_the_four_supported_analysts():
    assert set(_ANALYST_SPECS) == {"market", "social", "news", "fundamentals"}
    for key, spec in _ANALYST_SPECS.items():
        assert spec.key == key


def test_node_names_match_the_frontend_contract():
    expected = {
        "market": "market_analyst",
        "social": "sentiment_analyst",
        "news": "news_analyst",
        "fundamentals": "fundamentals_analyst",
    }
    for key, node_name in expected.items():
        assert _ANALYST_SPECS[key].node_name == node_name, f"node_name for {key} changed"


def test_report_keys_do_not_drift_from_the_langgraph_spec_table():
    # agent_node values are deliberately NOT compared: they live in different
    # naming domains (e.g. "Market Analyst" vs "market_analyst").
    for key in _ANALYST_SPECS:
        assert _ANALYST_SPECS[key].report_key == ANALYST_NODE_SPECS[key].report_key


def test_build_analyst_node_resolves_factories_at_call_time(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    factory_map = {
        "market": "create_market_analyst",
        "social": "create_sentiment_analyst",
        "news": "create_news_analyst",
        "fundamentals": "create_fundamentals_analyst",
    }
    for key, factory_name in factory_map.items():
        sentinel = object()

        def make_fake(sentinel=sentinel):
            def fake(llm, **kwargs):
                return sentinel
            return fake

        monkeypatch.setattr(sbo_module, factory_name, make_fake())
        result = orch._build_analyst_node(key)
        assert result is sentinel, key


def test_build_analyst_node_passes_macro_brief_only_to_news(tmp_path, monkeypatch):
    captured = {}

    def fake_market(llm):
        captured["market"] = {"llm": llm}
        return object()

    def fake_social(llm):
        captured["social"] = {"llm": llm}
        return object()

    def fake_fundamentals(llm):
        captured["fundamentals"] = {"llm": llm}
        return object()

    def fake_news(llm, macro_brief=None):
        captured["news"] = {"llm": llm, "macro_brief": macro_brief}
        return object()

    monkeypatch.setattr(sbo_module, "create_market_analyst", fake_market)
    monkeypatch.setattr(sbo_module, "create_sentiment_analyst", fake_social)
    monkeypatch.setattr(sbo_module, "create_fundamentals_analyst", fake_fundamentals)
    monkeypatch.setattr(sbo_module, "create_news_analyst", fake_news)

    orch = _orch(tmp_path, macro_brief="SENTINEL")

    for key in ("market", "social", "fundamentals"):
        orch._build_analyst_node(key)
        assert captured[key]["llm"] is orch._quick_llm

    orch._build_analyst_node("news")
    assert captured["news"]["llm"] is orch._quick_llm
    assert captured["news"]["macro_brief"] == "SENTINEL"


def test_unknown_analyst_key_raises_keyerror(tmp_path):
    orch = _orch(tmp_path)
    with pytest.raises(KeyError):
        orch._build_analyst_node("bogus")

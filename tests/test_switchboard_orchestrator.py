"""Tests for SwitchboardOrchestrator._run_analyst's tool-calling loop.

Root cause under test: if an analyst's ReAct tool-calling loop never
naturally reaches a turn with zero tool_calls, it silently exhausts
max_iters and the report stays "" with no exception, no log line, and no
trace anywhere — confirmed in production across market/news/fundamentals
analysts and multiple providers (sentiment_analyst is immune: it doesn't
use tool-calling at all, hence never affected). The fix nudges the model
to stop and synthesize on the final allowed turn, and logs when even that
doesn't recover — so this failure mode is never invisible again.
"""

import logging
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator import switchboard_orchestrator as sbo_module
from tradingagents.orchestrator.switchboard_orchestrator import SwitchboardOrchestrator


def _make_orch_stub():
    """Bind the real small helper methods onto a MagicMock so _run_analyst
    executes against real state-mutation logic without constructing a full
    SwitchboardOrchestrator (which requires live LLM client config)."""
    stub = MagicMock(spec=SwitchboardOrchestrator)
    stub._current_node = "market_analyst"
    stub.on_progress = None
    stub._emit = SwitchboardOrchestrator._emit.__get__(stub)
    stub._merge = SwitchboardOrchestrator._merge.__get__(stub)
    return stub


def _ai_with_tool_call():
    # Tool name deliberately not in _TOOL_MAP so the "Unknown tool" branch
    # fires instantly instead of attempting a real network call.
    return AIMessage(content="", tool_calls=[
        {"name": "fake_tool_for_test", "args": {}, "id": "call_1"}
    ])


def _ai_final(text="Complete report."):
    return AIMessage(content=text, tool_calls=[])


class TestRunAnalystLoopTermination:

    def test_natural_termination_unaffected(self):
        """Baseline: stops calling tools on iteration 2 -> ends normally, no nudge involved."""
        stub = _make_orch_stub()
        calls = [
            {"messages": [_ai_with_tool_call()]},
            {"messages": [_ai_final("Market report content.")]},
        ]
        node = MagicMock(side_effect=lambda state: calls.pop(0))
        state = {"messages": [HumanMessage(content="AAPL")]}

        SwitchboardOrchestrator._run_analyst(stub, node, state)

        assert node.call_count == 2
        assert state["messages"][-1].content == "Market report content."

    def test_max_iters_exhaustion_gets_stop_nudge_and_recovers(self):
        """Analyst keeps calling tools every turn EXCEPT after the stop-nudge
        on the final allowed iteration, where it complies. Confirms the loop
        gets a real chance to terminate cleanly instead of silently
        discarding the whole conversation."""
        stub = _make_orch_stub()

        def node_side_effect(state):
            last = state["messages"][-1]
            if isinstance(last, HumanMessage) and "final turn" in last.content:
                return {"messages": [_ai_final("Recovered report after nudge.")]}
            return {"messages": [_ai_with_tool_call()]}

        node = MagicMock(side_effect=node_side_effect)
        state = {"messages": [HumanMessage(content="AAPL")]}

        SwitchboardOrchestrator._run_analyst(stub, node, state)

        assert state["messages"][-1].content == "Recovered report after nudge."
        assert any("final turn" in getattr(m, "content", "") for m in state["messages"])

    def test_max_iters_exhaustion_still_empty_logs_warning(self, caplog):
        """Analyst ignores the stop nudge and keeps calling tools even on the
        final turn. The report legitimately stays incomplete, but this must
        now be LOGGED — previously completely silent, zero trace anywhere."""
        stub = _make_orch_stub()
        node = MagicMock(return_value={"messages": [_ai_with_tool_call()]})
        state = {"messages": [HumanMessage(content="AAPL")]}

        with caplog.at_level(logging.WARNING):
            SwitchboardOrchestrator._run_analyst(stub, node, state)

        assert any("max_iters" in r.message for r in caplog.records)
        # Still no crash, and the last message is the tool-calling one — the
        # orchestrator's existing "if report:" guard correctly skips emitting.
        assert node.call_count == 20


class TestResearchManagerLlmSelection:

    def test_defaults_to_quick_llm(self):
        stub = _make_orch_stub()
        stub.config = {}
        stub._quick_llm = "QUICK_SENTINEL"
        stub._deep_llm = "DEEP_SENTINEL"

        result = SwitchboardOrchestrator._research_manager_llm(stub)

        assert result == "QUICK_SENTINEL"

    def test_explicit_quick_role_uses_quick_llm(self):
        stub = _make_orch_stub()
        stub.config = {"research_manager_role": "quick"}
        stub._quick_llm = "QUICK_SENTINEL"
        stub._deep_llm = "DEEP_SENTINEL"

        result = SwitchboardOrchestrator._research_manager_llm(stub)

        assert result == "QUICK_SENTINEL"

    def test_explicit_deep_role_uses_deep_llm(self):
        stub = _make_orch_stub()
        stub.config = {"research_manager_role": "deep"}
        stub._quick_llm = "QUICK_SENTINEL"
        stub._deep_llm = "DEEP_SENTINEL"

        result = SwitchboardOrchestrator._research_manager_llm(stub)

        assert result == "DEEP_SENTINEL"

    def test_unrecognized_role_uses_deep_llm(self):
        """Any non-'quick' value silently falls through to the deep LLM."""
        stub = _make_orch_stub()
        stub.config = {"research_manager_role": "Quick"}
        stub._quick_llm = "QUICK_SENTINEL"
        stub._deep_llm = "DEEP_SENTINEL"

        result = SwitchboardOrchestrator._research_manager_llm(stub)

        assert result == "DEEP_SENTINEL"

    @pytest.mark.parametrize(
        "role_override, expected_attr",
        [
            (None, "_quick_llm"),
            ({"research_manager_role": "deep"}, "_deep_llm"),
        ],
    )
    def test_run_passes_selected_llm_to_create_research_manager(
        self, tmp_path, monkeypatch, role_override, expected_attr
    ):
        """Regression: the Phase-3 call site must call create_research_manager
        with the LLM selected by _research_manager_llm(), not a hardcoded
        deep client. Exercises a real SwitchboardOrchestrator.run() end-to-end
        with all downstream nodes faked so no real LLM calls are made."""
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
        if role_override is not None:
            config.update(role_override)

        orchestrator = SwitchboardOrchestrator(
            config=config, selected_analysts=["market"]
        )
        # Avoid a real LLM call when the final signal is parsed.
        orchestrator.signal_processor.process_signal = lambda decision: "Buy"

        captured = {}

        def fake_create_research_manager(llm):
            captured["llm"] = llm
            return lambda state: {
                "messages": [AIMessage(content="plan", tool_calls=[])],
                "investment_plan": "research manager plan",
            }

        def fake_node_factory(update):
            def factory(llm):
                def node(state):
                    return update
                return node
            return factory

        investment_debate_state = {
            "count": 1,
            "history": "bull vs bear debate",
            "bull_history": "",
            "bear_history": "",
            "current_response": "",
            "judge_decision": "",
        }
        risk_debate_state = {
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

        monkeypatch.setattr(
            sbo_module,
            "create_market_analyst",
            fake_node_factory({
                "messages": [AIMessage(content="market report", tool_calls=[])],
                "market_report": "market report",
            }),
        )
        monkeypatch.setattr(
            sbo_module,
            "create_bull_researcher",
            fake_node_factory({"investment_debate_state": investment_debate_state}),
        )
        monkeypatch.setattr(
            sbo_module,
            "create_bear_researcher",
            fake_node_factory({"investment_debate_state": investment_debate_state}),
        )
        monkeypatch.setattr(
            sbo_module,
            "create_research_manager",
            fake_create_research_manager,
        )
        monkeypatch.setattr(
            sbo_module,
            "create_trader",
            fake_node_factory({"trader_investment_plan": "trader plan"}),
        )
        monkeypatch.setattr(
            sbo_module,
            "create_aggressive_debator",
            fake_node_factory({"risk_debate_state": risk_debate_state}),
        )
        monkeypatch.setattr(
            sbo_module,
            "create_conservative_debator",
            fake_node_factory({"risk_debate_state": risk_debate_state}),
        )
        monkeypatch.setattr(
            sbo_module,
            "create_neutral_debator",
            fake_node_factory({"risk_debate_state": risk_debate_state}),
        )
        monkeypatch.setattr(
            sbo_module,
            "create_portfolio_manager",
            fake_node_factory({
                "messages": [AIMessage(content="decision", tool_calls=[])],
                "final_trade_decision": "**Rating**: Buy\n\nRationale.",
            }),
        )

        state, signal = orchestrator.run("AAPL", "2026-08-08")

        expected_llm = getattr(orchestrator, expected_attr)
        assert captured["llm"] is expected_llm
        if role_override is None:
            assert captured["llm"] is not orchestrator._deep_llm
        assert signal == "Buy"
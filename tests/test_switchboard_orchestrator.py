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

from langchain_core.messages import AIMessage, HumanMessage

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

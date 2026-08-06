"""Tests for invoke_with_tool_call_retry (the "blank market report" bug).

Root cause under test: market_analyst_node / news_analyst_node /
fundamentals_analyst_node treat "the model's reply has zero tool_calls" as
the ONLY signal that it's done and the reply is the final report — a
genuine final report (written after real data came back) and a model that
merely narrated intent instead of emitting a real tool call ("I've
submitted the tool calls, awaiting results...") are indistinguishable from
the outside. Confirmed in production 2026-08-06: market/news/fundamentals
reports came back as stubs like "Awaiting stock data response for
SPCX..." while sentiment_analyst (which pre-fetches data with plain
Python and never binds tools) was unaffected.

This is a DIFFERENT failure point from the one covered in
test_switchboard_orchestrator.py (that one is the tool-calling loop
exhausting max_iters on turn 20; this one is turn 0, before anything has
ever been fetched). The fixes are complementary and both matter.
"""

import logging
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tradingagents.agents.utils.agent_utils import invoke_with_tool_call_retry


def _ai(content: str, tool_calls: list | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [])


def _chain(*results):
    """A LangChain-Runnable-shaped stub: .invoke() returns results in order."""
    return MagicMock(invoke=MagicMock(side_effect=list(results)))


@pytest.mark.unit
class TestInvokeWithToolCallRetry:
    def test_normal_tool_call_passes_through_untouched(self):
        """The common case: the model calls tools on its first turn. No retry,
        exactly one chain invocation."""
        chain = _chain(_ai("", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}]))
        state = {"messages": [HumanMessage(content="AAPL")]}

        result = invoke_with_tool_call_retry(chain, state, log_label="market_analyst")

        assert chain.invoke.call_count == 1
        assert result.tool_calls

    def test_legitimate_final_report_after_data_fetched_is_not_retried(self):
        """A ToolMessage already in state means real data arrived earlier this
        round — zero tool_calls now is a genuine final report, not narration.
        Must not be touched."""
        chain = _chain(_ai("Full report using the fetched data."))
        state = {
            "messages": [
                HumanMessage(content="AAPL"),
                _ai("", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}]),
                ToolMessage(content="price data...", tool_call_id="1"),
            ]
        }

        result = invoke_with_tool_call_retry(chain, state, log_label="market_analyst")

        assert chain.invoke.call_count == 1
        assert result.content == "Full report using the fetched data."

    def test_narration_without_tool_call_triggers_one_retry(self):
        """The actual bug: first turn, nothing fetched yet, model narrates
        instead of calling. Must retry once and use the retry's result."""
        chain = _chain(
            _ai("I have made tool calls to retrieve the data. Awaiting results now..."),
            _ai("", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}]),
        )
        state = {"messages": [HumanMessage(content="SPX")]}

        result = invoke_with_tool_call_retry(chain, state, log_label="market_analyst")

        assert chain.invoke.call_count == 2
        assert result.tool_calls  # the retry succeeded — this is what gets returned

    def test_retry_prompt_includes_original_reply_and_a_corrective_nudge(self):
        """The second invoke call must see the first (failed) reply plus an
        explicit instruction to stop narrating and just emit the call —
        otherwise the retry is just re-asking the same question."""
        chain = _chain(
            _ai("Awaiting results now..."),
            _ai("", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}]),
        )
        state = {"messages": [HumanMessage(content="SPX")]}

        invoke_with_tool_call_retry(chain, state, log_label="market_analyst")

        second_call_messages = chain.invoke.call_args_list[1].args[0]
        assert second_call_messages[0].content == "SPX"
        assert second_call_messages[1].content == "Awaiting results now..."
        nudge = second_call_messages[2]
        assert isinstance(nudge, HumanMessage)
        assert "tool" in nudge.content.lower()
        # state itself is untouched — the retry is local to this call, not
        # persisted as extra history for later turns.
        assert state["messages"] == [HumanMessage(content="SPX")]

    def test_retry_still_empty_is_accepted_not_looped_forever(self):
        """Bounded to exactly one retry. If the model refuses twice, accept its
        text same as before the fix — this is now a real model limitation,
        logged, not a silent coin flip."""
        chain = _chain(
            _ai("Awaiting results now..."),
            _ai("Still no data, sorry."),
        )
        state = {"messages": [HumanMessage(content="SPX")]}

        result = invoke_with_tool_call_retry(chain, state, log_label="market_analyst")

        assert chain.invoke.call_count == 2  # not 3+, no infinite loop
        assert result.content == "Still no data, sorry."

    def test_narration_without_calls_logs_warning_before_retry(self, caplog):
        chain = _chain(
            _ai("Awaiting results now..."),
            _ai("", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}]),
        )
        state = {"messages": [HumanMessage(content="SPX")]}

        with caplog.at_level(logging.WARNING):
            invoke_with_tool_call_retry(chain, state, log_label="market_analyst")

        assert any(
            "market_analyst" in r.message and "retrying" in r.message
            for r in caplog.records
        )

    def test_persistent_narration_logs_error_after_retry(self, caplog):
        chain = _chain(
            _ai("Awaiting results now..."),
            _ai("Still nothing."),
        )
        state = {"messages": [HumanMessage(content="SPX")]}

        with caplog.at_level(logging.WARNING):
            invoke_with_tool_call_retry(chain, state, log_label="fundamentals_analyst")

        assert any(
            r.levelno == logging.ERROR and "fundamentals_analyst" in r.message
            for r in caplog.records
        )

    def test_second_or_later_round_with_no_tool_calls_and_no_prior_fetch_still_retries(self):
        """Retry eligibility is based on 'has a tool ever executed this round',
        not on which iteration this is — a model that stalls on turn 2 without
        ever having fetched anything is exactly as broken as stalling on turn 1."""
        chain = _chain(
            _ai("Let me think about this some more..."),
            _ai("", tool_calls=[{"name": "get_news", "args": {}, "id": "2"}]),
        )
        # Two prior AI turns, neither of which produced a ToolMessage.
        state = {
            "messages": [
                HumanMessage(content="SPX"),
                _ai("I'll start by considering the request."),
                HumanMessage(content="Continue"),
            ]
        }

        result = invoke_with_tool_call_retry(chain, state, log_label="news_analyst")

        assert chain.invoke.call_count == 2
        assert result.tool_calls

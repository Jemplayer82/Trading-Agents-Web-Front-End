"""Tests for invoke_with_tool_call_retry (the "blank market report" bug).

Root cause under test: market_analyst_node / news_analyst_node /
fundamentals_analyst_node treat "the model's reply has zero tool_calls" as
the ONLY signal that it's done and the reply is the final report — a
genuine final report and a model that stalls out look identical from the
outside. Confirmed in production 2026-08-06 in TWO distinct shapes:

  1. First turn, nothing fetched yet, model narrates intent instead of
     calling anything.
  2. LATER turn, after some or even ALL requested tools already ran —
     model still claims to be "awaiting results" it's already holding.
     This is the sneakier one: an earlier version of the fix gated the
     retry on "has any tool ever run", which correctly caught case 1 but
     completely missed case 2 — a live re-test after deploying that
     first version still returned three stubs (market/news/fundamentals),
     each claiming to be waiting for data that was already in state.

sentiment_analyst is immune to both — it pre-fetches data with plain
Python and never binds tools.

This is a DIFFERENT failure point from the one covered in
test_switchboard_orchestrator.py (that one is the tool-calling loop
exhausting max_iters on turn 20; this one is turn 0-or-later, well before
that). The fixes are complementary and both matter.
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
        """A ToolMessage already in state AND content that doesn't read like a
        stall means real data arrived and this is a genuine final report.
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

    def test_forward_looking_analysis_language_is_not_mistaken_for_a_stall(self):
        """Real analysis prose can legitimately say things like 'once Q2
        earnings are released' — that must not trip the stall detector just
        because it shares a word with the stub phrasing."""
        chain = _chain(_ai(
            "Revenue grew 12% YoY. Once Q2 earnings are released, watch for "
            "guidance revisions as the key catalyst. | Metric | Value |"
        ))
        state = {
            "messages": [
                HumanMessage(content="AAPL"),
                _ai("", tool_calls=[{"name": "get_fundamentals", "args": {}, "id": "1"}]),
                ToolMessage(content="financials...", tool_call_id="1"),
            ]
        }

        result = invoke_with_tool_call_retry(chain, state, log_label="fundamentals_analyst")

        assert chain.invoke.call_count == 1
        assert "Once Q2 earnings" in result.content

    @pytest.mark.parametrize(
        "stub_text",
        [
            "I'm awaiting the tool results from the global and SPCX-specific "
            "news queries. Once they arrive, I'll immediately provide a "
            "comprehensive analysis. Please stand by.",
            "Let me wait for that result before proceeding with technical "
            "indicator analysis.",
            "I've submitted tool calls to fetch comprehensive fundamental "
            "data. Awaiting results to compile the analysis...",
            "[Awaiting stock data response for SPCX...]",
        ],
        ids=["news-stub", "market-stub", "fundamentals-stub", "terse-stub"],
    )
    def test_stalling_after_all_data_already_fetched_still_retries(self, stub_text):
        """The actual production bug, reproduced from real stored reports:
        EVERY requested tool already ran (has_fetched=True) and the model
        STILL claims to be waiting instead of using what it already has. The
        old 'has any tool ever run' gate let all four of these straight
        through as if they were genuine final reports."""
        chain = _chain(
            _ai(stub_text),
            _ai("", tool_calls=[{"name": "get_news", "args": {}, "id": "2"}]),
        )
        state = {
            "messages": [
                HumanMessage(content="SPCX"),
                _ai("", tool_calls=[
                    {"name": "get_news", "args": {}, "id": "1"},
                    {"name": "get_global_news", "args": {}, "id": "1b"},
                ]),
                ToolMessage(content="news data...", tool_call_id="1"),
                ToolMessage(content="global news data...", tool_call_id="1b"),
            ]
        }

        result = invoke_with_tool_call_retry(chain, state, log_label="news_analyst")

        assert chain.invoke.call_count == 2
        assert result.tool_calls

    def test_stalling_after_fetching_retry_prompt_tells_model_data_is_already_there(self):
        """The nudge for this case must be different from the 'call a tool'
        nudge — the model already called tools; telling it to call one again
        would just repeat the same mistake."""
        chain = _chain(
            _ai("Awaiting the results now..."),
            _ai("", tool_calls=[{"name": "get_indicators", "args": {}, "id": "2"}]),
        )
        state = {
            "messages": [
                HumanMessage(content="SPCX"),
                _ai("", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}]),
                ToolMessage(content="price csv...", tool_call_id="1"),
            ]
        }

        invoke_with_tool_call_retry(chain, state, log_label="market_analyst")

        nudge = chain.invoke.call_args_list[1].args[0][-1]
        assert isinstance(nudge, HumanMessage)
        assert "already" in nudge.content.lower()

    def test_persistent_stall_after_fetching_is_accepted_and_logged(self, caplog):
        """Same bounded-retry guarantee applies to this failure shape too —
        one retry, then accept and log loudly rather than loop."""
        chain = _chain(
            _ai("Awaiting the results..."),
            _ai("Still waiting, sorry."),
        )
        state = {
            "messages": [
                HumanMessage(content="SPCX"),
                _ai("", tool_calls=[{"name": "get_stock_data", "args": {}, "id": "1"}]),
                ToolMessage(content="price csv...", tool_call_id="1"),
            ]
        }

        with caplog.at_level(logging.WARNING):
            result = invoke_with_tool_call_retry(chain, state, log_label="market_analyst")

        assert chain.invoke.call_count == 2
        assert result.content == "Still waiting, sorry."
        assert any(r.levelno == logging.ERROR for r in caplog.records)

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

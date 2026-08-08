import pytest
from unittest.mock import MagicMock, patch

from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher

REPORT_MARKERS = [
    "MARKET_REPORT_BODY",
    "SENTIMENT_REPORT_BODY",
    "NEWS_REPORT_BODY",
    "FUNDAMENTALS_REPORT_BODY",
]


def _build_state(count: int) -> dict:
    return {
        "market_report": "MARKET_REPORT_BODY",
        "sentiment_report": "SENTIMENT_REPORT_BODY",
        "news_report": "NEWS_REPORT_BODY",
        "fundamentals_report": "FUNDAMENTALS_REPORT_BODY",
        "asset_type": "stock",
        "investment_debate_state": {
            "history": "HISTORY_CONTENT",
            "bull_history": "BULL_HISTORY",
            "bear_history": "BEAR_HISTORY",
            "current_response": "PREVIOUS_RESPONSE",
            "count": count,
        },
    }


def _run_node(node_factory, count: int):
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(content="Test argument")

    with patch(
        "tradingagents.agents.researchers.bull_researcher.get_language_instruction",
        return_value="",
    ), patch(
        "tradingagents.agents.researchers.bear_researcher.get_language_instruction",
        return_value="",
    ):
        node = node_factory(mock_llm)
        result = node(_build_state(count))

    prompt = mock_llm.invoke.call_args[0][0]
    return prompt, result["investment_debate_state"]


@pytest.mark.unit
class TestResearchersPromptDiet:

    def test_bull_first_turn_includes_resources(self):
        prompt, debate_state = _run_node(create_bull_researcher, 0)

        for marker in REPORT_MARKERS:
            assert marker in prompt

        assert "HISTORY_CONTENT" in prompt
        assert "Last bear argument:" not in prompt
        assert "PREVIOUS_RESPONSE" not in prompt
        assert debate_state["current_response"] == "Bull Analyst: Test argument"
        assert debate_state["count"] == 1

    def test_bull_later_turn_omits_resources(self):
        prompt, debate_state = _run_node(create_bull_researcher, 2)

        for marker in REPORT_MARKERS:
            assert marker not in prompt

        assert "HISTORY_CONTENT" in prompt
        assert "Last bear argument:" not in prompt
        assert debate_state["current_response"] == "Bull Analyst: Test argument"
        assert debate_state["count"] == 3

    def test_bear_first_turn_includes_resources(self):
        prompt, debate_state = _run_node(create_bear_researcher, 1)

        for marker in REPORT_MARKERS:
            assert marker in prompt

        assert "HISTORY_CONTENT" in prompt
        assert "Last bull argument:" not in prompt
        assert "PREVIOUS_RESPONSE" not in prompt
        assert debate_state["current_response"] == "Bear Analyst: Test argument"
        assert debate_state["count"] == 2

    def test_bear_later_turn_omits_resources(self):
        prompt, debate_state = _run_node(create_bear_researcher, 3)

        for marker in REPORT_MARKERS:
            assert marker not in prompt

        assert "HISTORY_CONTENT" in prompt
        assert "Last bull argument:" not in prompt
        assert debate_state["current_response"] == "Bear Analyst: Test argument"
        assert debate_state["count"] == 4

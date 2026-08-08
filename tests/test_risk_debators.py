import pytest

from tradingagents.agents.risk_mgmt.aggressive_debator import (
    create_aggressive_debator,
)
from tradingagents.agents.risk_mgmt.conservative_debator import (
    create_conservative_debator,
)
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator

MARKET_REPORT_SENTINEL = "MARKET_REPORT_SENTINEL"
SENTIMENT_REPORT_SENTINEL = "SENTIMENT_REPORT_SENTINEL"
NEWS_REPORT_SENTINEL = "NEWS_REPORT_SENTINEL"
FUNDAMENTALS_REPORT_SENTINEL = "FUNDAMENTALS_REPORT_SENTINEL"
INVESTMENT_PLAN_SENTINEL = "INVESTMENT_PLAN_SENTINEL"
TRADER_PLAN_SENTINEL = "TRADER_PLAN_SENTINEL"

AGGRESSIVE_DUPLICATE_SENTINEL = "AGGRESSIVE_DUPLICATE_SENTINEL"
CONSERVATIVE_DUPLICATE_SENTINEL = "CONSERVATIVE_DUPLICATE_SENTINEL"
NEUTRAL_DUPLICATE_SENTINEL = "NEUTRAL_DUPLICATE_SENTINEL"

AGGRESSIVE_HISTORY_SENTINEL = "AGGRESSIVE_HISTORY_SENTINEL"
CONSERVATIVE_HISTORY_SENTINEL = "CONSERVATIVE_HISTORY_SENTINEL"
NEUTRAL_HISTORY_SENTINEL = "NEUTRAL_HISTORY_SENTINEL"


class FakeResponse:
    content = "FAKE_LLM_RESPONSE"


class FakeLLM:
    def __init__(self):
        self.last_prompt = None

    def invoke(self, prompt):
        self.last_prompt = prompt
        return FakeResponse()


def build_state(
    count,
    history="",
    current_aggressive="",
    current_conservative="",
    current_neutral="",
):
    return {
        "risk_debate_state": {
            "history": history,
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "count": count,
            "current_aggressive_response": current_aggressive,
            "current_conservative_response": current_conservative,
            "current_neutral_response": current_neutral,
        },
        "market_report": MARKET_REPORT_SENTINEL,
        "sentiment_report": SENTIMENT_REPORT_SENTINEL,
        "news_report": NEWS_REPORT_SENTINEL,
        "fundamentals_report": FUNDAMENTALS_REPORT_SENTINEL,
        "trader_investment_plan": TRADER_PLAN_SENTINEL,
        "investment_plan": INVESTMENT_PLAN_SENTINEL,
    }


def assert_reports_present(prompt):
    assert MARKET_REPORT_SENTINEL in prompt
    assert SENTIMENT_REPORT_SENTINEL in prompt
    assert NEWS_REPORT_SENTINEL in prompt
    assert FUNDAMENTALS_REPORT_SENTINEL in prompt


def assert_reports_absent(prompt):
    assert MARKET_REPORT_SENTINEL not in prompt
    assert SENTIMENT_REPORT_SENTINEL not in prompt
    assert NEWS_REPORT_SENTINEL not in prompt
    assert FUNDAMENTALS_REPORT_SENTINEL not in prompt


def assert_state_keys_and_count(result, previous_count):
    new_state = result["risk_debate_state"]
    assert "current_aggressive_response" in new_state
    assert "current_conservative_response" in new_state
    assert "current_neutral_response" in new_state
    assert new_state["count"] == previous_count + 1


@pytest.mark.unit
class TestRiskDebatorsPromptDiet:

    # --- Aggressive Debator tests ---

    def test_aggressive_debator_first_call_embeds_raw_reports(self):
        fake_llm = FakeLLM()
        node = create_aggressive_debator(fake_llm)
        state = build_state(count=0)
        result = node(state)

        assert_reports_present(fake_llm.last_prompt)
        assert INVESTMENT_PLAN_SENTINEL not in fake_llm.last_prompt
        assert_state_keys_and_count(result, 0)

    def test_aggressive_debator_later_round_embeds_investment_plan(self):
        fake_llm = FakeLLM()
        node = create_aggressive_debator(fake_llm)
        state = build_state(count=3)
        result = node(state)

        assert_reports_absent(fake_llm.last_prompt)
        assert INVESTMENT_PLAN_SENTINEL in fake_llm.last_prompt
        assert_state_keys_and_count(result, 3)

    def test_aggressive_debator_drops_duplicate_other_responses_but_keeps_history(self):
        fake_llm = FakeLLM()
        node = create_aggressive_debator(fake_llm)
        history = (
            f"Conservative Analyst: {CONSERVATIVE_HISTORY_SENTINEL}\n"
            f"Neutral Analyst: {NEUTRAL_HISTORY_SENTINEL}"
        )
        state = build_state(
            count=3,
            history=history,
            current_conservative=CONSERVATIVE_DUPLICATE_SENTINEL,
            current_neutral=NEUTRAL_DUPLICATE_SENTINEL,
        )
        node(state)

        assert CONSERVATIVE_DUPLICATE_SENTINEL not in fake_llm.last_prompt
        assert NEUTRAL_DUPLICATE_SENTINEL not in fake_llm.last_prompt

        assert (
            f"Conservative Analyst: {CONSERVATIVE_HISTORY_SENTINEL}"
            in fake_llm.last_prompt
        )
        assert f"Neutral Analyst: {NEUTRAL_HISTORY_SENTINEL}" in fake_llm.last_prompt

    # --- Conservative Debator tests ---

    def test_conservative_debator_first_call_embeds_raw_reports(self):
        fake_llm = FakeLLM()
        node = create_conservative_debator(fake_llm)
        state = build_state(count=1)
        result = node(state)

        assert_reports_present(fake_llm.last_prompt)
        assert INVESTMENT_PLAN_SENTINEL not in fake_llm.last_prompt
        assert_state_keys_and_count(result, 1)

    def test_conservative_debator_later_round_embeds_investment_plan(self):
        fake_llm = FakeLLM()
        node = create_conservative_debator(fake_llm)
        state = build_state(count=4)
        result = node(state)

        assert_reports_absent(fake_llm.last_prompt)
        assert INVESTMENT_PLAN_SENTINEL in fake_llm.last_prompt
        assert_state_keys_and_count(result, 4)

    def test_conservative_debator_drops_duplicate_other_responses_but_keeps_history(self):
        fake_llm = FakeLLM()
        node = create_conservative_debator(fake_llm)
        history = (
            f"Aggressive Analyst: {AGGRESSIVE_HISTORY_SENTINEL}\n"
            f"Neutral Analyst: {NEUTRAL_HISTORY_SENTINEL}"
        )
        state = build_state(
            count=4,
            history=history,
            current_aggressive=AGGRESSIVE_DUPLICATE_SENTINEL,
            current_neutral=NEUTRAL_DUPLICATE_SENTINEL,
        )
        node(state)

        assert AGGRESSIVE_DUPLICATE_SENTINEL not in fake_llm.last_prompt
        assert NEUTRAL_DUPLICATE_SENTINEL not in fake_llm.last_prompt

        assert (
            f"Aggressive Analyst: {AGGRESSIVE_HISTORY_SENTINEL}" in fake_llm.last_prompt
        )
        assert f"Neutral Analyst: {NEUTRAL_HISTORY_SENTINEL}" in fake_llm.last_prompt

    # --- Neutral Debator tests ---

    def test_neutral_debator_first_call_embeds_raw_reports(self):
        fake_llm = FakeLLM()
        node = create_neutral_debator(fake_llm)
        state = build_state(count=2)
        result = node(state)

        assert_reports_present(fake_llm.last_prompt)
        assert INVESTMENT_PLAN_SENTINEL not in fake_llm.last_prompt
        assert_state_keys_and_count(result, 2)

    def test_neutral_debator_later_round_embeds_investment_plan(self):
        fake_llm = FakeLLM()
        node = create_neutral_debator(fake_llm)
        state = build_state(count=5)
        result = node(state)

        assert_reports_absent(fake_llm.last_prompt)
        assert INVESTMENT_PLAN_SENTINEL in fake_llm.last_prompt
        assert_state_keys_and_count(result, 5)

    def test_neutral_debator_drops_duplicate_other_responses_but_keeps_history(self):
        fake_llm = FakeLLM()
        node = create_neutral_debator(fake_llm)
        history = (
            f"Aggressive Analyst: {AGGRESSIVE_HISTORY_SENTINEL}\n"
            f"Conservative Analyst: {CONSERVATIVE_HISTORY_SENTINEL}"
        )
        state = build_state(
            count=5,
            history=history,
            current_aggressive=AGGRESSIVE_DUPLICATE_SENTINEL,
            current_conservative=CONSERVATIVE_DUPLICATE_SENTINEL,
        )
        node(state)

        assert AGGRESSIVE_DUPLICATE_SENTINEL not in fake_llm.last_prompt
        assert CONSERVATIVE_DUPLICATE_SENTINEL not in fake_llm.last_prompt

        assert (
            f"Aggressive Analyst: {AGGRESSIVE_HISTORY_SENTINEL}" in fake_llm.last_prompt
        )
        assert (
            f"Conservative Analyst: {CONSERVATIVE_HISTORY_SENTINEL}"
            in fake_llm.last_prompt
        )

import pytest

from tradingagents.agents.analysts.news_analyst import (
    _build_system_message,
    _select_tools,
)
from tradingagents.agents.utils.agent_utils import (
    get_global_news,
    get_language_instruction,
    get_news,
)

pytestmark = pytest.mark.unit


def _expected_default_message(asset_label: str = "company") -> str:
    return (
        f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(query, start_date, end_date) for {asset_label}-specific or targeted news searches, and get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
        + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
        + get_language_instruction()
    )


def test_select_tools_without_macro_brief_none():
    assert _select_tools(None) == [get_news, get_global_news]


def test_select_tools_without_macro_brief_empty():
    assert _select_tools("") == [get_news, get_global_news]


def test_build_system_message_without_macro_brief_none():
    message = _build_system_message(asset_label="company", macro_brief=None)
    assert message == _expected_default_message("company")
    assert "macro_brief" not in message.lower()


def test_build_system_message_without_macro_brief_empty():
    message = _build_system_message(asset_label="company", macro_brief="")
    assert message == _expected_default_message("company")
    assert "macro_brief" not in message.lower()


def test_select_tools_with_macro_brief():
    brief = "Fed holds rates steady; oil up on supply concerns."
    tools = _select_tools(brief)
    assert tools == [get_news]
    assert get_global_news not in tools


def test_build_system_message_with_macro_brief():
    brief = "Fed holds rates steady; oil up on supply concerns."
    message = _build_system_message(asset_label="company", macro_brief=brief)
    assert "<start_of_macro_brief>" in message
    assert brief in message
    assert "<end_of_macro_brief>" in message
    assert "get_global_news(curr_date" not in message
    assert "get_news(query, start_date, end_date)" in message

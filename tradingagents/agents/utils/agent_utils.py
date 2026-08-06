import logging

from langchain_core.messages import HumanMessage, RemoveMessage, ToolMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import get_stock_data
from tradingagents.agents.utils.fundamental_data_tools import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
)
from tradingagents.agents.utils.news_data_tools import get_global_news, get_insider_transactions, get_news
from tradingagents.agents.utils.technical_indicators_tools import get_indicators

# Public surface. The data tools above are imported purely to be re-exported
# from this single module (graph.trading_graph binds them onto the agent graph);
# listing them in __all__ documents that intent and marks them as used.
__all__ = [
    "get_stock_data",
    "get_indicators",
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_news",
    "get_insider_transactions",
    "get_global_news",
    "get_language_instruction",
    "build_instrument_context",
    "create_msg_delete",
    "invoke_with_tool_call_retry",
]

log = logging.getLogger(__name__)

_NO_TOOL_CALL_NUDGE = (
    "You have not called any tools yet, so no data has been fetched — writing "
    "an analysis now would be from memory, which is stale and not allowed. "
    "Do not narrate or describe what you are about to do. Emit ONLY the tool "
    "call(s) now."
)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Applied to every agent whose output reaches the saved report —
    analysts, researchers, debaters, research manager, trader, and
    portfolio manager — so a non-English run produces a fully localized
    report rather than a mix of languages.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def build_instrument_context(ticker: str, asset_type: str = "stock") -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    instrument_label = "asset" if asset_type == "crypto" else "instrument"
    extra_hint = (
        " Treat it as a crypto asset rather than a company, and do not assume company fundamentals are available."
        if asset_type == "crypto"
        else ""
    )
    return (
        f"The {instrument_label} to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`, `-USD`)."
        + extra_hint
    )

def invoke_with_tool_call_retry(chain, state, *, log_label: str):
    """Invoke a tool-bound analyst chain; retry once on a claim-without-call.

    ``market_analyst_node`` and its siblings treat "the reply has zero
    tool_calls" as "the model is done and this is the final report" — that's
    the only signal available, since a genuine final report and a model that
    merely narrated intent ("I've submitted the tool calls, awaiting
    results...") look identical from the outside. Weaker models occasionally
    do the latter on their very first turn, before ever fetching anything,
    and the narration silently becomes the stored report — the analysis
    "completes" with no error and an empty-looking result (bug found
    2026-08-06: market/news/fundamentals reports came back as stubs like
    "Awaiting stock data response...").

    The fix retries ONCE, and only when nothing has actually been fetched
    yet this round — a ``ToolMessage`` already in ``state["messages"]`` means
    the analyst is legitimately wrapping up after real data arrived, and that
    case must not be touched. Bounded to one retry so a model that keeps
    refusing to call anything doesn't loop forever; if the retry also comes
    back empty, its text is accepted as the report same as before — that is
    now a real model limitation rather than a coin flip.
    """
    messages = list(state["messages"])
    result = chain.invoke(messages)

    has_fetched = any(isinstance(m, ToolMessage) for m in state["messages"])
    if len(result.tool_calls) == 0 and not has_fetched:
        log.warning(
            "%s: zero tool_calls on first turn with nothing fetched yet — "
            "retrying once with a corrective nudge",
            log_label,
        )
        messages = messages + [result, HumanMessage(content=_NO_TOOL_CALL_NUDGE)]
        result = chain.invoke(messages)
        if len(result.tool_calls) == 0:
            log.error(
                "%s: still zero tool_calls after the retry — accepting the "
                "model's text as the report",
                log_label,
            )

    return result


def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages

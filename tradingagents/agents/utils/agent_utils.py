import logging
import re

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

_STILL_WAITING_NUDGE = (
    "The tool results you asked for are already in this conversation — look "
    "at the messages above; do not claim you are still waiting for them. "
    "Either call another tool if you still need more data, or write the full "
    "report now using the data you already have. Do not write a status "
    "update saying you are waiting."
)

# Real examples pulled from stored reports, 2026-08-06: "Let me wait for that
# result before proceeding", "I'm awaiting the tool results... Please stand
# by", "I've submitted tool calls... Awaiting results to compile the
# analysis...", "[Awaiting stock data response for SPCX...]", "Awaiting stock
# data results now to proceed". The common shape is the model announcing —
# in the first person, about its own status — that it's still waiting,
# rather than either calling another tool or writing the report.
#
# Deliberately narrow, and NOT just "\bawait\b": bare "await"/"awaits" is an
# ordinary verb in real trading analysis ("investors should await
# confirmation of a breakout", "insiders may await the lockup expiration") —
# an early version of this pattern matched those and would have wasted a
# retry on genuine analysis. The distinguishing signal in every real stub is
# either (a) first-person framing ("I'm awaiting", "I've submitted") or (b)
# the progressive "awaiting" paired specifically with a tool-output noun
# (results/response/data), not a market-related one like "confirmation" or
# "the lockup expiration".
_STILL_WAITING_RE = re.compile(
    r"\blet me wait\b"
    r"|\bi(?:'m| am)\s+(?:still\s+)?awaiting\b"
    r"|\bi'?ve submitted\b"
    r"|\A\s*still (?:awaiting|waiting)\b"  # bare status reply, not e.g. "traders are still waiting"
    r"|\bplease (?:stand by|wait)\b"
    r"|\bstand by\b"
    r"|\bawaiting\s+(?:the\s+)?(?:tool\s+)?(?:results?|responses?|stock data)\b",
    re.IGNORECASE,
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
    stalls out look identical from the outside. Weaker models stall in two
    distinct ways, both observed in production 2026-08-06 and both needing a
    retry, not silent acceptance:

    1. First turn, nothing fetched yet, model narrates intent instead of
       calling anything ("I'm retrieving the data, let me wait for that").
    2. LATER turn, after some or even ALL requested tools already ran and
       their ToolMessages are sitting right there in state — model still
       claims to be "awaiting results" instead of using them. This is the
       sneakier case: a ``ToolMessage`` already present does NOT mean the
       reply is a genuine final report, so gating the retry on "has any tool
       ever run" (an earlier version of this function did exactly that)
       misses it entirely. Real examples pulled from stored reports: "Let me
       wait for that result before proceeding", "I'm awaiting the tool
       results... Please stand by", "Awaiting results to compile the
       analysis...".

    Detection: zero tool_calls AND (nothing fetched yet, OR the reply text
    matches the "still waiting" phrasing above). A model correctly wrapping
    up rarely opens with "awaiting" / "still waiting" / "please stand by" —
    those are status-update phrases, not analysis phrases — so this is a
    fairly safe trigger. Bounded to exactly one retry regardless of which
    case fired, so a model that keeps stalling doesn't loop; if the retry
    also stalls, its text is accepted as the report same as before — now a
    logged, real model limitation instead of a silent bug.
    """
    messages = list(state["messages"])
    result = chain.invoke(messages)

    has_fetched = any(isinstance(m, ToolMessage) for m in state["messages"])
    stalled_without_calling = len(result.tool_calls) == 0 and not has_fetched
    stalled_after_fetching = (
        len(result.tool_calls) == 0
        and has_fetched
        and bool(_STILL_WAITING_RE.search(result.content or ""))
    )

    if stalled_without_calling or stalled_after_fetching:
        reason = (
            "nothing fetched yet" if stalled_without_calling
            else "claims to be waiting despite data already in context"
        )
        log.warning(
            "%s: zero tool_calls and %s — retrying once with a corrective nudge",
            log_label, reason,
        )
        nudge = _NO_TOOL_CALL_NUDGE if stalled_without_calling else _STILL_WAITING_NUDGE
        messages = messages + [result, HumanMessage(content=nudge)]
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

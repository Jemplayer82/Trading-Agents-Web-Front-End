from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_global_news,
    get_language_instruction,
    get_news,
    invoke_with_tool_call_retry,
)
from tradingagents.dataflows.config import get_config


def _select_tools(macro_brief: str | None):
    if macro_brief:
        return [get_news]
    return [get_news, get_global_news]


def _build_system_message(*, asset_label: str, macro_brief: str | None) -> str:
    if not macro_brief:
        return (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(query, start_date, end_date) for {asset_label}-specific or targeted news searches, and get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

    return (
        f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use ONLY the following available tool: get_news(query, start_date, end_date) for {asset_label}-specific or targeted searches. Do not search for broader macro/world news; rely on the macro/world news context already provided below. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
        + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
        + f"\n\nMacro/world news context (already gathered for this scan):\n<start_of_macro_brief>\n{macro_brief}\n<end_of_macro_brief>"
        + get_language_instruction()
    )


def create_news_analyst(llm, macro_brief: str | None = None):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = build_instrument_context(
            state["company_of_interest"], asset_type
        )

        tools = _select_tools(macro_brief)

        system_message = _build_system_message(
            asset_label=asset_label, macro_brief=macro_brief
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = invoke_with_tool_call_retry(chain, state, log_label="news_analyst")

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "news_report": report,
        }

    return news_analyst_node

"""SwitchboardOrchestrator — replaces TradingAgentsGraph / LangGraph StateGraph.

Plain-Python pipeline with an in-process tool-calling loop. Progress events
are emitted via an `on_progress(frame)` callback so the WebSocket streaming
layer in web/runner.py doesn't need to change.

Pipeline order:
  analysts (selected subset of market/sentiment/news/fundamentals)
    → bull/bear investment debate (max_debate_rounds full rounds)
    → research manager
    → trader
    → risk debate: aggressive/conservative/neutral (max_risk_discuss_rounds full rounds)
    → portfolio manager → final_trade_decision
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_global_news,
    get_income_statement,
    get_indicators,
    get_insider_transactions,
    get_news,
    get_stock_data,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.config import set_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.signal_processing import SignalProcessor
from tradingagents.llm_clients import create_llm_client

logger = logging.getLogger(__name__)

_TOOL_MAP = {t.name: t for t in [
    get_stock_data,
    get_indicators,
    get_news,
    get_global_news,
    get_insider_transactions,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
]}

_BIAS_CONTEXT: dict[str, str] = {
    "bullish": "Context: bullish market stance — in borderline Hold/Buy cases, lean Buy.",
    "bearish": "Context: bearish market stance — in borderline Hold/Sell cases, lean Sell.",
    "neutral": "",
}


class SwitchboardOrchestrator:
    """In-process pipeline replacing TradingAgentsGraph."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        selected_analysts: list[str] | None = None,
        on_progress=None,
    ) -> None:
        self.config = config or dict(DEFAULT_CONFIG)
        self.selected_analysts = selected_analysts or ["market", "social", "news", "fundamentals"]
        self.on_progress = on_progress  # callable(frame: dict) | None

        set_config(self.config)
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # Tracks which agent is currently running so streaming token frames
        # carry the right node name for the frontend progress grid.
        self._current_node: str | None = None

        deep_provider = self.config.get("deep_llm_provider") or self.config.get("llm_provider", "ollama")
        quick_provider = self.config.get("quick_llm_provider") or self.config.get("llm_provider", "ollama")
        deep_client = create_llm_client(
            provider=deep_provider,
            model=self.config["deep_think_llm"],
            base_url=self.config.get("deep_backend_url") or self.config.get("backend_url"),
            on_token=self._emit_token,
            **self._provider_kwargs(deep_provider),
        )
        quick_client = create_llm_client(
            provider=quick_provider,
            model=self.config["quick_think_llm"],
            base_url=self.config.get("quick_backend_url") or self.config.get("backend_url"),
            on_token=self._emit_token,
            **self._provider_kwargs(quick_provider),
        )
        self._deep_llm = deep_client.get_llm()
        self._quick_llm = quick_client.get_llm()
        self.memory_log = TradingMemoryLog(self.config)
        self.signal_processor = SignalProcessor(self._quick_llm)

    def _provider_kwargs(self, provider: str) -> dict[str, Any]:
        provider = provider.lower()
        if provider == "google":
            lvl = self.config.get("google_thinking_level")
            return {"thinking_level": lvl} if lvl else {}
        if provider == "openai":
            effort = self.config.get("openai_reasoning_effort")
            return {"reasoning_effort": effort} if effort else {}
        if provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            return {"effort": effort} if effort else {}
        return {}

    def _emit(self, frame: dict[str, Any]) -> None:
        if self.on_progress:
            try:
                self.on_progress(frame)
            except Exception:
                pass

    def _emit_token(self, text: str) -> None:
        if text and self._current_node:
            self._emit({"type": "token", "node": self._current_node, "text": text, "channel": "content"})

    def _merge(self, state: dict, update: dict) -> None:
        """Apply a node's return dict onto state (append semantics for messages)."""
        for key, val in update.items():
            if key == "messages" and isinstance(val, list):
                state["messages"] = state["messages"] + val
            else:
                state[key] = val

    def _clear_messages(self, state: dict) -> None:
        """Reset the messages list between analyst phases (Anthropic needs a placeholder)."""
        state["messages"] = [HumanMessage(content="Continue")]

    def _run_analyst(self, analyst_node, state: dict) -> None:
        """Run an analyst through its tool-calling loop until no tool_calls remain.

        If the model never naturally reaches a turn with zero tool_calls, the
        loop would previously exhaust ``max_iters`` and silently leave the
        report empty — no exception, no log line, no trace anywhere. Confirmed
        in production across market/news/fundamentals analysts (the three that
        use tool-calling; sentiment_analyst pre-fetches its data and never
        enters this loop, which is why it was never affected). On the final
        allowed iteration we now nudge the model to stop and synthesize from
        whatever has been gathered, giving the loop a real chance to terminate
        cleanly; if it still doesn't comply, that's now logged instead of
        silently discarded.
        """
        max_iters = 20
        for i in range(max_iters):
            if i == max_iters - 1:
                state["messages"].append(HumanMessage(
                    content="You are at your final turn. Do not call any more "
                            "tools — write your complete report now based on "
                            "everything gathered so far."
                ))

            update = analyst_node(state)
            self._merge(state, update)

            last = state["messages"][-1] if state["messages"] else None
            if not last or not getattr(last, "tool_calls", None):
                break

            if i == max_iters - 1:
                logger.warning(
                    "Analyst tool-calling loop for node=%s exhausted "
                    "max_iters=%d and ignored the stop instruction — "
                    "report will be empty for this run.",
                    self._current_node, max_iters,
                )
                break

            # Emit message summaries so the frontend knows tools are being called
            tool_summaries = [
                {"name": tc.get("name") if isinstance(tc, dict) else tc.name,
                 "args": tc.get("args") if isinstance(tc, dict) else tc.args}
                for tc in last.tool_calls
            ]
            self._emit({"type": "messages", "messages": [
                {"type": "ai", "name": None, "text": "", "tool_calls": tool_summaries}
            ]})

            for tc in last.tool_calls:
                tc_name = tc.get("name") if isinstance(tc, dict) else tc.name
                tc_args = tc.get("args") if isinstance(tc, dict) else tc.args
                tc_id = tc.get("id") if isinstance(tc, dict) else tc.id

                tool_fn = _TOOL_MAP.get(tc_name)
                if tool_fn is None:
                    result = f"Unknown tool: {tc_name}"
                else:
                    try:
                        result = tool_fn.invoke(tc_args)
                    except Exception as exc:
                        logger.warning(
                            "Tool call failed for node=%s tool=%s: %s",
                            self._current_node, tc_name, exc,
                        )
                        result = f"Tool error: {exc}"

                state["messages"].append(ToolMessage(content=str(result), tool_call_id=tc_id))

    def run(
        self,
        ticker: str,
        trade_date: str,
        asset_type: str = "stock",
    ) -> tuple[dict[str, Any], str]:
        """Run the full pipeline and return (final_state, signal)."""
        max_debate = self.config.get("max_debate_rounds", 1)
        max_risk = self.config.get("max_risk_discuss_rounds", 1)
        bias = self.config.get("bias", "neutral")
        bias_context = _BIAS_CONTEXT.get(bias, "")

        past_context = self.memory_log.get_past_context(ticker)

        state: dict[str, Any] = {
            "messages": [("human", ticker)],
            "company_of_interest": ticker,
            "asset_type": asset_type,
            "trade_date": str(trade_date),
            "past_context": past_context,
            "bias_context": bias_context,
            "investment_debate_state": {
                "bull_history": "", "bear_history": "", "history": "",
                "current_response": "", "judge_decision": "", "count": 0,
            },
            "risk_debate_state": {
                "aggressive_history": "", "conservative_history": "",
                "neutral_history": "", "history": "", "latest_speaker": "",
                "current_aggressive_response": "", "current_conservative_response": "",
                "current_neutral_response": "", "judge_decision": "", "count": 0,
            },
            "market_report": "", "fundamentals_report": "",
            "sentiment_report": "", "news_report": "",
            "investment_plan": "", "trader_investment_plan": "",
            "final_trade_decision": "",
        }

        # ── Phase 1: Analysts ────────────────────────────────────────────────────
        analyst_factories = {
            "market": lambda: create_market_analyst(self._quick_llm),
            "social": lambda: create_sentiment_analyst(self._quick_llm),
            "news": lambda: create_news_analyst(self._quick_llm),
            "fundamentals": lambda: create_fundamentals_analyst(self._quick_llm),
        }
        report_key_map = {
            "market": "market_report",
            "social": "sentiment_report",
            "news": "news_report",
            "fundamentals": "fundamentals_report",
        }

        _analyst_node_names = {
            "market": "market_analyst",
            "social": "sentiment_analyst",
            "news": "news_analyst",
            "fundamentals": "fundamentals_analyst",
        }
        for analyst_key in self.selected_analysts:
            if analyst_key not in analyst_factories:
                continue
            self._emit({"type": "status", "message": f"Running {analyst_key} analyst…"})
            self._current_node = _analyst_node_names.get(analyst_key, analyst_key)
            node = analyst_factories[analyst_key]()
            self._run_analyst(node, state)
            self._current_node = None
            report = state.get(report_key_map.get(analyst_key, ""), "")
            if report:
                self._emit({"type": "report_update", "reports": {report_key_map[analyst_key]: report}})
            self._clear_messages(state)

        # ── Phase 2: Investment debate ───────────────────────────────────────────
        self._emit({"type": "status", "message": f"Starting investment debate ({max_debate} round(s))…"})
        bull_node = create_bull_researcher(self._quick_llm)
        bear_node = create_bear_researcher(self._quick_llm)

        for rnd in range(max_debate):
            self._current_node = "bull_researcher"
            self._merge(state, bull_node(state))
            self._current_node = "bear_researcher"
            self._merge(state, bear_node(state))
            self._current_node = None
            count = state["investment_debate_state"]["count"]
            transcript = state["investment_debate_state"].get("history", "")
            self._emit({"type": "debate", "scope": "investment", "rounds": count, "judge": "",
                        "debate_state": {"investment_debate_state": state["investment_debate_state"]}})
            if transcript:
                self._emit({"type": "report_update", "reports": {"investment_debate": transcript}})

        # ── Phase 3: Research Manager ────────────────────────────────────────────
        self._emit({"type": "status", "message": "Research Manager synthesising debate…", "agent": "research_debate"})
        rm_node = create_research_manager(self._deep_llm)
        self._current_node = "research_manager"
        self._merge(state, rm_node(state))
        self._current_node = None
        if state.get("investment_plan"):
            self._emit({"type": "report_update", "reports": {"investment_plan": state["investment_plan"]}})

        # ── Phase 4: Trader ──────────────────────────────────────────────────────
        self._emit({"type": "status", "message": "Trader building transaction proposal…", "agent": "trader"})
        trader_fn = create_trader(self._quick_llm)
        self._current_node = "trader"
        update = trader_fn(state)
        self._current_node = None
        for key, val in update.items():
            if key != "messages":
                state[key] = val
        if state.get("trader_investment_plan"):
            self._emit({"type": "report_update", "reports": {"trader_investment_plan": state["trader_investment_plan"]}})

        # ── Phase 5: Risk debate ─────────────────────────────────────────────────
        self._emit({"type": "status", "message": f"Starting risk debate ({max_risk} round(s))…"})
        agg_node = create_aggressive_debator(self._quick_llm)
        cons_node = create_conservative_debator(self._quick_llm)
        neut_node = create_neutral_debator(self._quick_llm)

        for rnd in range(max_risk):
            self._current_node = "aggressive_debator"
            self._merge(state, agg_node(state))
            self._current_node = "conservative_debator"
            self._merge(state, cons_node(state))
            self._current_node = "neutral_debator"
            self._merge(state, neut_node(state))
            self._current_node = None
            count = state["risk_debate_state"]["count"]
            transcript = state["risk_debate_state"].get("history", "")
            self._emit({"type": "debate", "scope": "risk", "rounds": count, "judge": "",
                        "debate_state": {"risk_debate_state": state["risk_debate_state"]}})
            if transcript:
                self._emit({"type": "report_update", "reports": {"risk_debate": transcript}})

        # ── Phase 6: Portfolio Manager ───────────────────────────────────────────
        self._emit({"type": "status", "message": "Portfolio Manager making final decision…", "agent": "portfolio_manager"})
        pm_node = create_portfolio_manager(self._deep_llm)
        self._current_node = "portfolio_manager"
        self._merge(state, pm_node(state))
        self._current_node = None
        if state.get("final_trade_decision"):
            self._emit({"type": "report_update", "reports": {"final_trade_decision": state["final_trade_decision"]}})

        signal = self.signal_processor.process_signal(state.get("final_trade_decision", ""))
        return state, signal

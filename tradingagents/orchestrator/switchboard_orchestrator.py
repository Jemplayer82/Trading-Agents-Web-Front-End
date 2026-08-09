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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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

@dataclass(frozen=True)
class _AnalystSpec:
    """Static per-analyst metadata shared by both analyst execution paths.

    ``node_name`` is the label the frontend progress grid keys on. It is NOT
    the same string as tradingagents/graph/analyst_execution.py's
    ``AnalystNodeSpec.agent_node`` (a human-readable LangGraph display name,
    e.g. "Market Analyst") — only ``report_key`` is shared vocabulary
    between the two modules.
    """

    key: str
    node_name: str
    report_key: str


_ANALYST_SPECS: dict[str, _AnalystSpec] = {
    "market": _AnalystSpec("market", "market_analyst", "market_report"),
    # Wire key stays "social" for saved-config back-compat; the agent itself
    # is the sentiment analyst.
    "social": _AnalystSpec("social", "sentiment_analyst", "sentiment_report"),
    "news": _AnalystSpec("news", "news_analyst", "news_report"),
    "fundamentals": _AnalystSpec("fundamentals", "fundamentals_analyst", "fundamentals_report"),
}

# Keys tradingagents/agents/managers/portfolio_manager.py reads off
# risk_debate_state with bare `[]` access — a cached state missing any of
# these would KeyError mid-LLM-call instead of failing the validation gate.
_PM_RISK_KEYS = (
    "history", "aggressive_history", "conservative_history", "neutral_history",
    "current_aggressive_response", "current_conservative_response",
    "current_neutral_response", "count",
)
# Below this length a "completed" risk_debate_state['history'] is almost
# certainly an empty/degenerate draw (tool-loop exhaustion, provider outage
# mid-run) rather than a real debate — reject it rather than let one bad
# pipeline run get amplified into every account that reuses it same-day.
_MIN_RISK_HISTORY_CHARS = 200


class CachedStateInvalid(ValueError):
    """A cached ``final_state`` is missing or has malformed data the
    Portfolio Manager needs, or its rerun produced an empty decision.

    Reserved for problems with the cached-state *contract* — never for
    provider/network errors, which surface as their own exception types.
    Callers (see web/spy_scanner.py) catch this alongside any other
    cache-path exception and fall back to a full ``run()``.
    """


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
        # carry the right node name for the frontend progress grid. Per-thread
        # (threading.local) because the analyst phase can run several analysts
        # at once — see config['analyst_concurrency_limit'] — and a single
        # shared attribute would let one analyst's tokens be labelled with a
        # sibling's node name. Every other phase (debate, research manager,
        # trader, risk debate, portfolio manager) runs on the main thread and
        # behaves exactly as before: a thread always reads back what it wrote.
        self._node_local = threading.local()
        self._current_node = None

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

    @property
    def _current_node(self) -> str | None:
        """Name of the agent node running ON THIS THREAD, or None.

        Backed by threading.local() rather than a plain attribute so a
        parallel analyst phase cannot cross-label another analyst's streaming
        token frames. A thread that never set it reads None, which
        _emit_token already treats as "don't emit".
        """
        return getattr(self._node_local, "name", None)

    @_current_node.setter
    def _current_node(self, value: str | None) -> None:
        self._node_local.name = value

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

    def _research_manager_llm(self) -> Any:
        """Pick which LLM instance drives the Research Manager, per the
        `research_manager_role` config knob (default "quick" — see
        tradingagents/default_config.py). Kept as its own method so the
        selection is unit-testable without a live LLM client."""
        role = str(self.config.get("research_manager_role", "quick")).strip().lower()
        return self._deep_llm if role == "deep" else self._quick_llm

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

    def _build_analyst_node(self, analyst_key: str):
        """Construct the node callable for one analyst key.

        Deliberately an if-chain calling the module-global factory names at
        CALL time rather than a table of pre-bound callables: several tests
        monkeypatch e.g. sbo_module.create_market_analyst and depend on that
        late lookup (tests/test_news_analyst_wiring.py,
        tests/test_switchboard_orchestrator.py). The news analyst also needs
        the live self.config['macro_brief'] read at build time.
        """
        if analyst_key == "market":
            return create_market_analyst(self._quick_llm)
        if analyst_key == "social":
            return create_sentiment_analyst(self._quick_llm)
        if analyst_key == "news":
            return create_news_analyst(self._quick_llm, macro_brief=self.config.get("macro_brief"))
        if analyst_key == "fundamentals":
            return create_fundamentals_analyst(self._quick_llm)
        raise KeyError(analyst_key)

    def _run_analysts_sequential(self, state: dict, analyst_keys: list[str]) -> None:
        """Strict-sequential analyst phase — the historical behaviour.

        One shared ``state['messages']`` list carried across analysts, reset
        via ``_clear_messages`` after each one. Note the resulting
        first-vs-rest asymmetry: the first analyst sees run()'s
        ``[("human", ticker)]`` seed, every later one sees ``_clear_messages``'s
        ``[HumanMessage("Continue")]`` placeholder. That asymmetry is preserved
        exactly, which is why this path is kept as its own method rather than
        emulated by the parallel path with a pool of one.
        """
        for analyst_key in analyst_keys:
            spec = _ANALYST_SPECS[analyst_key]
            self._emit({"type": "status", "message": f"Running {analyst_key} analyst…"})
            self._current_node = spec.node_name
            node = self._build_analyst_node(analyst_key)
            self._run_analyst(node, state)
            self._current_node = None
            report = state.get(spec.report_key, "")
            if report:
                self._emit({"type": "report_update", "reports": {spec.report_key: report}})
            self._clear_messages(state)

    def _analyst_concurrency_limit(self, n_analysts: int) -> int:
        """How many analysts may run at once.

        Config knob `analyst_concurrency_limit` (tradingagents/default_config.py,
        default 1 = today's strictly sequential loop). Floors at 1 and caps at
        the number of selected analysts. Anything unparsable falls back to 1 --
        a bad config value must never turn the analyst phase into an unbounded
        fan-out at a live trading desk.
        """
        try:
            limit = int(self.config.get("analyst_concurrency_limit", 1))
        except (TypeError, ValueError):
            limit = 1
        return max(1, min(limit, max(1, n_analysts)))

    def _analyst_local_state(self, state: dict, ticker: str) -> dict:
        """A private, per-analyst copy of the shared pipeline state.

        A SHALLOW copy is correct and deliberate: analysts read only scalars
        off state and write only top-level `messages` plus their own report
        key, never a nested container. The messages list is a brand-new object
        seeded exactly like run()'s pre-analyst-phase state, so concurrent
        tool-calling loops can never interleave ToolMessages into each other's
        conversation, and it is non-empty with a leading human turn -- the same
        guarantee _clear_messages's placeholder exists to give Anthropic.

        Built on the MAIN thread before submit, so no worker ever reads the
        shared state dict while the main thread is merging into it.
        """
        local = dict(state)
        local["messages"] = [("human", ticker)]
        return local

    def _run_analyst_isolated(self, node, node_name: str, report_key: str, local_state: dict) -> str:
        """Worker-thread body: run ONE analyst against its OWN state copy.

        Returns only that analyst's report text; the caller merges it into the
        shared state ON THE MAIN THREAD. `local_state` must already be private
        to this call -- nothing in here may touch the shared state dict.
        """
        self._current_node = node_name
        try:
            self._run_analyst(node, local_state)
            return local_state.get(report_key, "") or ""
        finally:
            # Pool threads are reused -- never leak the label to the next task.
            self._current_node = None

    def _run_analysts_parallel(self, state: dict, analyst_keys: list[str], limit: int) -> None:
        """Run analysts concurrently, at most `limit` in flight.

        Thread-safety contract:
          * Nodes and per-analyst state copies are built on the MAIN thread at
            submit time, keeping factory-call order deterministic (several
            tests monkeypatch the factories and assert on what they received).
          * NOTHING writes the shared `state` dict from a worker; the main
            thread merges each analyst's own report key as its future resolves.
          * `self._current_node` is thread-local, so each worker labels its own
            streaming token frames.
          * `self.on_progress` may therefore be invoked concurrently from
            worker threads (via _run_analyst's tool-call summary frames and
            _emit_token). The only in-tree consumer with a non-None callback is
            web/runner.py, which pushes to a thread-safe queue.Queue;
            web/spy_scanner.py passes on_progress=None. `_emit` already
            swallows callback exceptions.

        Post-condition matches _run_analysts_sequential exactly: the shared
        state ends with _clear_messages applied, so every downstream phase sees
        identical input on both paths.
        """
        ticker = state["company_of_interest"]
        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="analyst") as pool:
            futures = {}
            for analyst_key in analyst_keys:
                spec = _ANALYST_SPECS[analyst_key]
                # Status frames are emitted on the main thread at submit time,
                # in selected_analysts order, so the frame text is identical to
                # the sequential path. With limit < len(analyst_keys) a frame
                # can precede the analyst actually starting; accepted, rather
                # than emitting from workers.
                self._emit({"type": "status", "message": f"Running {analyst_key} analyst…"})
                node = self._build_analyst_node(analyst_key)
                local_state = self._analyst_local_state(state, ticker)
                futures[pool.submit(
                    self._run_analyst_isolated, node, spec.node_name, spec.report_key, local_state,
                )] = analyst_key
            for fut in as_completed(futures):
                spec = _ANALYST_SPECS[futures[fut]]
                # Re-raises a worker exception on the main thread, matching the
                # sequential path's fail-the-run behaviour. The enclosing `with`
                # still shuts the pool down with wait=True first.
                report = fut.result()
                state[spec.report_key] = report
                if report:
                    self._emit({"type": "report_update", "reports": {spec.report_key: report}})
        self._clear_messages(state)

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
        analyst_keys = [k for k in self.selected_analysts if k in _ANALYST_SPECS]
        analyst_limit = self._analyst_concurrency_limit(len(analyst_keys))
        if analyst_limit <= 1:
            self._run_analysts_sequential(state, analyst_keys)
        elif analyst_keys:
            self._run_analysts_parallel(state, analyst_keys, analyst_limit)

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
        rm_node = create_research_manager(self._research_manager_llm())
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

    def rerun_decision(self, cached_state: dict[str, Any], ticker: str) -> tuple[dict[str, Any], str]:
        """Re-run only the Portfolio Manager over a previously completed
        pipeline state, substituting THIS orchestrator's bias and a freshly
        loaded memory context.

        Everything upstream of the Portfolio Manager — analysts, the
        investment debate, the research manager, the trader, the risk debate —
        is a pure function of (ticker, trade_date, shared-stage config): no
        agent up to and including the risk debate reads ``bias``/
        ``bias_context`` or the memory log's ``past_context`` (see
        tradingagents/agents/utils/memory.py's injection-point invariant).
        So a same-day analysis produced for one paper account can donate that
        entire shared stage to another account; only the Portfolio Manager
        — the one node that reads ``bias_context`` and ``past_context`` —
        needs to run again, with this account's own values.

        Builds the Portfolio Manager's input explicitly from validated
        fields rather than shallow-copying ``cached_state``, so the donor's
        own ``bias_context``, ``past_context``, ``final_trade_decision``, and
        serialized ``messages`` can never leak into this account's decision.

        Raises CachedStateInvalid if ``cached_state`` doesn't carry what the
        Portfolio Manager needs, or if the rerun produces an empty decision.
        Callers should catch this (and any other cache-path exception) and
        fall back to a full ``run()`` — reuse must never be able to fail a
        scan, only skip its own savings.
        """
        if not isinstance(cached_state, dict):
            raise CachedStateInvalid("cached_state is not a dict")
        if cached_state.get("company_of_interest") != ticker:
            raise CachedStateInvalid(
                f"cached_state ticker mismatch: expected {ticker!r}, "
                f"got {cached_state.get('company_of_interest')!r}"
            )
        investment_plan = cached_state.get("investment_plan")
        trader_plan = cached_state.get("trader_investment_plan")
        if not isinstance(investment_plan, str) or not investment_plan.strip():
            raise CachedStateInvalid("cached_state['investment_plan'] is empty")
        if not isinstance(trader_plan, str) or not trader_plan.strip():
            raise CachedStateInvalid("cached_state['trader_investment_plan'] is empty")
        risk_debate_state = cached_state.get("risk_debate_state")
        if not isinstance(risk_debate_state, dict):
            raise CachedStateInvalid("cached_state['risk_debate_state'] is not a dict")
        missing = [k for k in _PM_RISK_KEYS if k not in risk_debate_state]
        if missing:
            raise CachedStateInvalid(f"cached_state['risk_debate_state'] missing keys: {missing}")
        history = risk_debate_state.get("history")
        if not isinstance(history, str) or len(history) < _MIN_RISK_HISTORY_CHARS:
            raise CachedStateInvalid(
                "cached_state['risk_debate_state']['history'] is too short to be a real debate"
            )

        bias = self.config.get("bias", "neutral")
        state: dict[str, Any] = {
            "messages": [("human", ticker)],
            "company_of_interest": ticker,
            "asset_type": cached_state.get("asset_type", "stock"),
            "trade_date": cached_state.get("trade_date", ""),
            "past_context": self.memory_log.get_past_context(ticker),
            "bias_context": _BIAS_CONTEXT.get(bias, ""),
            "investment_debate_state": cached_state.get("investment_debate_state", {}),
            "risk_debate_state": dict(risk_debate_state),
            "market_report": cached_state.get("market_report", ""),
            "fundamentals_report": cached_state.get("fundamentals_report", ""),
            "sentiment_report": cached_state.get("sentiment_report", ""),
            "news_report": cached_state.get("news_report", ""),
            "investment_plan": investment_plan,
            "trader_investment_plan": trader_plan,
            "final_trade_decision": "",
        }

        self._emit({"type": "status", "message": "Portfolio Manager making final decision…", "agent": "portfolio_manager"})
        pm_node = create_portfolio_manager(self._deep_llm)
        self._current_node = "portfolio_manager"
        self._merge(state, pm_node(state))
        self._current_node = None

        final_trade_decision = state.get("final_trade_decision", "")
        if not isinstance(final_trade_decision, str) or not final_trade_decision.strip():
            # Zero-exception failure mode (e.g. a half-broken provider returning
            # empty content): without this check the scan would "succeed" as a
            # silent all-Hold instead of falling back to a full pipeline run.
            raise CachedStateInvalid("Portfolio Manager returned an empty decision on rerun")
        self._emit({"type": "report_update", "reports": {"final_trade_decision": final_trade_decision}})

        signal = self.signal_processor.process_signal(final_trade_decision)
        return state, signal

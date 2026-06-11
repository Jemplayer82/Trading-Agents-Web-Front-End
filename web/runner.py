"""Bridge between TradingAgentsGraph (synchronous LangGraph stream) and the
WebSocket async sender.

The producer runs in a worker thread (asyncio.to_thread) and pushes frames
to a queue. The WebSocket coroutine drains the queue and sends frames to
the browser.
"""

from __future__ import annotations

import os
import queue
import traceback
from collections.abc import Iterable
from typing import Any

from cli.models import AssetType
from cli.utils import detect_asset_type
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

from . import db

try:
    from .bus_mirror import RunMirror
except Exception:
    RunMirror = None  # type: ignore[assignment,misc]


REPORT_KEYS = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_plan",
    "trader_investment_plan",
    "final_trade_decision",
)

# Display name → analyst key (the AnalystType enum values used internally)
ALL_ANALYSTS = ["market", "social", "news", "fundamentals"]


def build_config(params: dict[str, Any]) -> dict[str, Any]:
    """Merge user params with DEFAULT_CONFIG to build the graph config."""
    cfg = dict(DEFAULT_CONFIG)

    provider = (params.get("provider") or "ollama").lower()
    cfg["llm_provider"] = provider
    cfg["deep_think_llm"] = params.get("deep_model") or cfg["deep_think_llm"]
    cfg["quick_think_llm"] = params.get("quick_model") or cfg["quick_think_llm"]
    cfg["output_language"] = params.get("language") or cfg.get("output_language", "English")

    # Research depth → debate / risk-discussion rounds.
    depth = int(params.get("research_depth") or 1)
    cfg["max_debate_rounds"] = depth
    cfg["max_risk_discuss_rounds"] = depth

    # Provider-specific thinking knobs (optional; ignored if provider doesn't use them).
    if params.get("openai_reasoning_effort"):
        cfg["openai_reasoning_effort"] = params["openai_reasoning_effort"]
    if params.get("anthropic_effort"):
        cfg["anthropic_effort"] = params["anthropic_effort"]
    if params.get("google_thinking_level"):
        cfg["google_thinking_level"] = params["google_thinking_level"]

    # Backend URL: Ollama uses the env var set in the container; other providers
    # rely on their client's default endpoint unless explicitly overridden.
    if provider == "ollama":
        cfg["backend_url"] = os.environ.get("OLLAMA_BASE_URL") or cfg.get("backend_url")
    elif params.get("backend_url"):
        cfg["backend_url"] = params["backend_url"]

    return cfg


def _normalize_analysts(requested: Iterable[str], asset_type: AssetType) -> list[str]:
    keys = [a for a in requested if a in ALL_ANALYSTS]
    if not keys:
        keys = list(ALL_ANALYSTS)
    if asset_type == AssetType.CRYPTO:
        keys = [a for a in keys if a != "fundamentals"]
    return keys


def _diff_reports(prev: dict[str, str], new_state: dict[str, Any]) -> dict[str, str]:
    """Return only the report fields whose content changed since `prev`."""
    out: dict[str, str] = {}
    for key in REPORT_KEYS:
        val = new_state.get(key, "")
        if not isinstance(val, str):
            continue
        if val and val != prev.get(key, ""):
            out[key] = val
    return out


def _debate_markdown(state: Any) -> str:
    """Render an InvestDebateState / RiskDebateState into a readable transcript."""
    if not isinstance(state, dict):
        return ""
    parts: list[str] = []
    history = (state.get("history") or "").strip()
    if history:
        parts.append(history)
    else:
        # Fall back to the per-side trails if the combined transcript is empty.
        for key in (
            "bull_history", "bear_history",
            "aggressive_history", "conservative_history", "neutral_history",
        ):
            v = (state.get(key) or "").strip()
            if v:
                parts.append(v)
    judge = (state.get("judge_decision") or "").strip()
    if judge:
        parts.append("---\n\n### Judge Decision\n\n" + judge)
    return "\n\n".join(p for p in parts if p).strip()


# Synthetic report keys → the LangGraph state key holding the debate.
DEBATE_REPORTS = {
    "investment_debate": "investment_debate_state",
    "risk_debate": "risk_debate_state",
}


def _token_text(msg: Any) -> tuple[str, str]:
    """Extract (content, reasoning) deltas from a streamed message chunk."""
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        text = "".join(
            (c.get("text", "") if isinstance(c, dict) else str(c)) for c in content
        )
    else:
        text = content or ""
    extra = getattr(msg, "additional_kwargs", None) or {}
    reasoning = extra.get("reasoning_content") or extra.get("reasoning") or ""
    return text, (reasoning if isinstance(reasoning, str) else "")


def _unwrap_stream_item(item: Any) -> tuple[str, Any]:
    """Normalise a multi-mode stream item to (mode, payload)."""
    if (
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        and item[0] in ("values", "messages", "updates")
    ):
        return item[0], item[1]
    return "values", item


def _message_summary(msg: Any) -> dict[str, Any] | None:
    """Reduce a LangChain message to a small dict the frontend can render."""
    if msg is None:
        return None
    msg_type = getattr(msg, "type", None) or msg.__class__.__name__.lower()
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        # Anthropic-style content blocks
        text = " ".join(
            (c.get("text", "") if isinstance(c, dict) else str(c)) for c in content
        ).strip()
    else:
        text = str(content) if content else ""

    name = getattr(msg, "name", None)
    tool_calls = getattr(msg, "tool_calls", None) or []
    tool_summaries = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            tool_summaries.append({
                "name": tc.get("name") or tc.get("function", {}).get("name"),
                "args": tc.get("args") or tc.get("function", {}).get("arguments"),
            })

    return {
        "type": msg_type,
        "name": name,
        "text": text[:500],  # cap to keep WS frames small
        "tool_calls": tool_summaries,
    }


def run_analysis_sync(params: dict[str, Any], analysis_id: int, frames: queue.Queue) -> None:
    """Run the graph and emit frames. Synchronous — call via asyncio.to_thread."""

    def emit(frame: dict[str, Any]) -> None:
        frames.put(frame)

    mirror = None
    try:
        ticker = params["ticker"].strip().upper()
        trade_date = params["trade_date"]
        asset_type = detect_asset_type(ticker)
        analysts = _normalize_analysts(params.get("analysts", []), asset_type)

        emit({
            "type": "status",
            "message": f"Initializing {len(analysts)} analyst(s) for {ticker} on {trade_date}",
            "analysts": analysts,
        })
        mirror = RunMirror.maybe_create(params, analysis_id, analysts=analysts) if RunMirror else None

        cfg = build_config(params)
        graph = TradingAgentsGraph(
            selected_analysts=analysts,
            debug=False,
            config=cfg,
        )

        emit({"type": "status", "message": "Graph compiled. Streaming chunks..."})

        init_state = graph.propagator.create_initial_state(
            ticker,
            trade_date,
            asset_type=asset_type.value,
            past_context=graph.memory_log.get_past_context(ticker),
        )
        args = graph.propagator.get_graph_args()
        # Stream full-state values (existing behaviour) AND per-token message
        # chunks so the UI can show each agent's train of thought live.
        args["stream_mode"] = ["values", "messages"]

        seen_reports: dict[str, str] = {}
        last_message_idx = 0
        final_state: dict[str, Any] = {}

        for item in graph.graph.stream(init_state, **args):
            mode, chunk = _unwrap_stream_item(item)

            # ---- token stream (train of thought) ----
            if mode == "messages":
                msg_chunk = chunk[0] if isinstance(chunk, tuple) else chunk
                meta = chunk[1] if isinstance(chunk, tuple) and len(chunk) > 1 else {}
                node = (meta or {}).get("langgraph_node")
                text, reasoning = _token_text(msg_chunk)
                if reasoning:
                    emit({"type": "token", "node": node, "text": reasoning, "channel": "reasoning"})
                if text:
                    emit({"type": "token", "node": node, "text": text, "channel": "content"})
                continue

            # ---- full-state values ----
            final_state = chunk
            if mirror:
                mirror.on_state(chunk)

            # Report deltas (+ synthesised bull/bear & risk debate transcripts)
            delta = _diff_reports(seen_reports, chunk)
            for rep_key, state_key in DEBATE_REPORTS.items():
                md = _debate_markdown(chunk.get(state_key))
                if md and md != seen_reports.get(rep_key, ""):
                    delta[rep_key] = md
            if delta:
                for key, val in delta.items():
                    seen_reports[key] = val
                emit({"type": "report_update", "reports": delta})
                if mirror:
                    mirror.on_report_delta(delta)

            # Messages tail
            messages = chunk.get("messages") or []
            if len(messages) > last_message_idx:
                new_msgs = messages[last_message_idx:]
                last_message_idx = len(messages)
                summaries = [m for m in (_message_summary(m) for m in new_msgs) if m]
                if summaries:
                    emit({"type": "messages", "messages": summaries})

            # Debate state — only emit while the debate is still ongoing.
            # investment_debate_state / risk_debate_state persist in the
            # LangGraph state after they finish, so gating on the completion
            # report prevents the UI from being reset to "in_progress" on
            # every subsequent chunk after the debate has already completed.
            inv = chunk.get("investment_debate_state") or {}
            risk = chunk.get("risk_debate_state") or {}
            if isinstance(inv, dict) and inv.get("count") and "investment_plan" not in seen_reports:
                emit({
                    "type": "debate",
                    "scope": "investment",
                    "rounds": inv.get("count"),
                    "judge": inv.get("judge_decision", ""),
                })
            if isinstance(risk, dict) and risk.get("count") and "final_trade_decision" not in seen_reports:
                emit({
                    "type": "debate",
                    "scope": "risk",
                    "rounds": risk.get("count"),
                    "judge": risk.get("judge_decision", ""),
                })

        # Final signal processing
        emit({"type": "status", "message": "Processing final signal..."})
        signal = graph.process_signal(final_state.get("final_trade_decision", ""))

        # Persist to DB
        db.complete_analysis(analysis_id, final_state, signal)

        # Also let the graph's own disk-logging fire (mirrors CLI behavior so the
        # `~/.tradingagents/logs/{ticker}/{date}/` tree stays consistent across services).
        try:
            graph.curr_state = final_state
            graph.ticker = ticker
            graph._log_state(trade_date, final_state)
            graph.memory_log.store_decision(
                ticker=ticker,
                trade_date=trade_date,
                final_trade_decision=final_state.get("final_trade_decision", ""),
            )
        except Exception:
            # disk logging is best-effort — never block on it
            pass

        emit({
            "type": "done",
            "analysis_id": analysis_id,
            "signal": signal,
            "final_decision": final_state.get("final_trade_decision", ""),
        })
        if mirror:
            mirror.on_done(signal, final_state.get("final_trade_decision", ""))
    except Exception as exc:
        tb = traceback.format_exc()
        try:
            db.fail_analysis(analysis_id, f"{exc}\n{tb}")
        except Exception:
            pass
        emit({"type": "error", "message": str(exc), "traceback": tb})
        if mirror:
            mirror.on_error(str(exc))
    finally:
        frames.put(None)  # sentinel — must run before mirror.close() to avoid WS delay
        if mirror:
            mirror.close()

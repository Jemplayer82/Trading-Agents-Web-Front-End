"""Bridge between SwitchboardOrchestrator and the WebSocket async sender.

The producer runs in a worker thread (asyncio.to_thread) and pushes frames
to a queue. The WebSocket coroutine (web/main.py /api/analyze) drains the
queue and sends frames to the browser.

Frame types emitted on the queue:

    status         human-readable progress line
    token          per-token delta {node, text, channel: content|reasoning}
    report_update  changed report sections {reports: {key: markdown}}
    messages       tail of new agent/tool messages (truncated summaries)
    debate         live round counter for the bull/bear and risk debates
    done / error   final signal + decision, or message + traceback
    None           sentinel — end of stream; always queued last

To add a frame type or report key, change this module and the frontend
consumer together: web/static/app.js (REPORT_KEYS and the WebSocket
dispatcher).
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
from tradingagents.orchestrator import SwitchboardOrchestrator

from . import alerts, db

try:
    from .bus_mirror import RunMirror
except Exception:
    RunMirror = None  # type: ignore[assignment,misc]


# Streamed-report state keys, in pipeline order. Must stay in sync with
# REPORT_KEYS in web/static/app.js (labels and render order live there).
REPORT_KEYS = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_plan",
    "trader_investment_plan",
    "final_trade_decision",
)

ALL_ANALYSTS = ["market", "social", "news", "fundamentals"]


def build_config(params: dict[str, Any]) -> dict[str, Any]:
    """Merge user params with DEFAULT_CONFIG to build the orchestrator config."""
    cfg = dict(DEFAULT_CONFIG)

    provider = (params.get("provider") or "ollama").lower()
    cfg["llm_provider"] = provider
    cfg["deep_think_llm"] = params.get("deep_model") or cfg["deep_think_llm"]
    cfg["quick_think_llm"] = params.get("quick_model") or cfg["quick_think_llm"]
    cfg["output_language"] = params.get("language") or cfg.get("output_language", "English")

    # Aggressiveness (1–10) → debate rounds. Overrides research_depth when set.
    aggressiveness = int(params.get("aggressiveness") or 0)
    if aggressiveness:
        if aggressiveness <= 3:
            rounds = 1
        elif aggressiveness <= 7:
            rounds = 2
        else:
            rounds = 3
        cfg["max_debate_rounds"] = rounds
        cfg["max_risk_discuss_rounds"] = rounds
    else:
        depth = int(params.get("research_depth") or 1)
        cfg["max_debate_rounds"] = depth
        cfg["max_risk_discuss_rounds"] = depth

    # Decision bias (bullish / neutral / bearish) → Portfolio Manager prompt modifier.
    cfg["bias"] = params.get("bias") or "neutral"

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
    """Keep only known analyst keys; an empty selection means all of them."""
    keys = [a for a in requested if a in ALL_ANALYSTS]
    if not keys:
        keys = list(ALL_ANALYSTS)
    if asset_type == AssetType.CRYPTO:
        keys = [a for a in keys if a != "fundamentals"]
    return keys


def run_analysis_sync(params: dict[str, Any], analysis_id: int, frames: queue.Queue) -> None:
    """Run the orchestrator and emit frames. Synchronous — call via asyncio.to_thread.

    Outcomes land in two places: frames on the queue for the live browser,
    and the analyses row in SQLite (db.complete_analysis / db.fail_analysis)
    for history. The None sentinel is queued in `finally` so the WebSocket
    drain loop always terminates.
    """

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
        orch = SwitchboardOrchestrator(
            config=cfg,
            selected_analysts=analysts,
            on_progress=emit,
        )

        final_state, signal = orch.run(ticker, trade_date, asset_type=asset_type.value)

        emit({"type": "status", "message": "Processing final signal..."})

        # Persist to DB
        db.complete_analysis(analysis_id, final_state, signal)

        # Disk logging (best-effort — never block on it)
        try:
            orch.memory_log.store_decision(
                ticker=ticker,
                trade_date=trade_date,
                final_trade_decision=final_state.get("final_trade_decision", ""),
            )
        except Exception:
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
        alerts.notify_run_failed(
            kind="Analysis", run_id=analysis_id,
            label=str(params.get("ticker") or "?"), error=str(exc),
        )
        if mirror:
            mirror.on_error(str(exc))
    finally:
        frames.put(None)  # sentinel — WebSocket drain loop terminates on this
        if mirror:
            mirror.close()

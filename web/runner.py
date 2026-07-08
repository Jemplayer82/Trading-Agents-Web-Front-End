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


def aggressiveness_to_rounds(aggressiveness: int) -> int:
    """Map an aggressiveness level (1–10) to debate/risk-discussion rounds (1–3).

    Shared by the single-ticker config (build_config), the Portfolio Scan, and
    the S&P 500 deep dive so the mapping can't drift between them.
    """
    if aggressiveness <= 3:
        return 1
    if aggressiveness <= 7:
        return 2
    return 3


def apply_indicator_vendor(cfg: dict[str, Any]) -> None:
    """Override cfg['data_vendors']['technical_indicators'] from the user setting.

    The Settings UI writes TECHNICAL_INDICATOR_VENDOR to app_settings → os.environ.
    Read it call-time and set it on a COPY of data_vendors (never mutate the shared
    DEFAULT_CONFIG dict). No-op for an unset/unknown value (keeps the yfinance
    default). Shared by all tabs so the choice applies everywhere.
    """
    vendor = (os.environ.get("TECHNICAL_INDICATOR_VENDOR") or "").strip().lower()
    if vendor not in ("yfinance", "alpha_vantage"):
        return
    dv = dict(cfg.get("data_vendors") or DEFAULT_CONFIG.get("data_vendors") or {})
    dv["technical_indicators"] = vendor
    cfg["data_vendors"] = dv


def build_config(params: dict[str, Any]) -> dict[str, Any]:
    """Merge user params with DEFAULT_CONFIG to build the orchestrator config."""
    cfg = dict(DEFAULT_CONFIG)

    quick_provider = (params.get("quick_provider") or params.get("provider") or "ollama").lower()
    deep_provider = (params.get("deep_provider") or params.get("provider") or "ollama").lower()
    cfg["quick_llm_provider"] = quick_provider
    cfg["deep_llm_provider"] = deep_provider
    cfg["llm_provider"] = deep_provider  # legacy alias — portfolio_main.py:256, spy_scanner.py:371 read this for display
    cfg["deep_think_llm"] = params.get("deep_model") or cfg["deep_think_llm"]
    cfg["quick_think_llm"] = params.get("quick_model") or cfg["quick_think_llm"]
    cfg["output_language"] = params.get("language") or cfg.get("output_language", "English")

    # Aggressiveness (1–10) → debate rounds. Overrides research_depth when set.
    aggressiveness = int(params.get("aggressiveness") or 0)
    if aggressiveness:
        rounds = aggressiveness_to_rounds(aggressiveness)
        cfg["max_debate_rounds"] = rounds
        cfg["max_risk_discuss_rounds"] = rounds
    else:
        depth = int(params.get("research_depth") or 1)
        cfg["max_debate_rounds"] = depth
        cfg["max_risk_discuss_rounds"] = depth

    # Decision bias (bullish / neutral / bearish) → Portfolio Manager prompt modifier.
    cfg["bias"] = params.get("bias") or "neutral"

    # Technical-indicator vendor (yfinance / alpha_vantage) from Settings.
    apply_indicator_vendor(cfg)

    # Provider-specific thinking knobs (optional; ignored if provider doesn't use them).
    if params.get("openai_reasoning_effort"):
        cfg["openai_reasoning_effort"] = params["openai_reasoning_effort"]
    if params.get("anthropic_effort"):
        cfg["anthropic_effort"] = params["anthropic_effort"]
    if params.get("google_thinking_level"):
        cfg["google_thinking_level"] = params["google_thinking_level"]

    # Backend URL, resolved independently per role: Ollama uses the env var set
    # in the container; other providers rely on their client's default endpoint
    # unless explicitly overridden. Each role checks its OWN provider — this is
    # what lets Quick=ollama + Deep=switchboard resolve OLLAMA_BASE_URL for Quick
    # only, without leaking into Deep's client construction.
    if quick_provider == "ollama":
        cfg["quick_backend_url"] = os.environ.get("OLLAMA_BASE_URL") or cfg.get("backend_url")
    else:
        cfg["quick_backend_url"] = params.get("quick_backend_url") or params.get("backend_url")

    if deep_provider == "ollama":
        cfg["deep_backend_url"] = os.environ.get("OLLAMA_BASE_URL") or cfg.get("backend_url")
    else:
        cfg["deep_backend_url"] = params.get("deep_backend_url") or params.get("backend_url")

    # Legacy alias some secondary call sites still read (web/llm_helpers.py
    # fallback path) — mirror the deep role, same convention as
    # cfg["llm_provider"] above.
    cfg["backend_url"] = cfg.get("deep_backend_url") or cfg.get("backend_url")

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
        if mirror:
            try:
                ft = frame.get("type")
                if ft == "report_update":
                    mirror.on_report_delta(frame.get("reports") or {})
                elif ft == "debate" and frame.get("debate_state"):
                    mirror.on_state(frame["debate_state"])
            except Exception:
                pass

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

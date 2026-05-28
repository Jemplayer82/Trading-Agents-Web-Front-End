"""FastAPI app for tradingagents-api: dashboard backend + Schwab OAuth.

Portfolio scan routes live in `web/portfolio_main.py` in a separate container.
Nginx routes /api/portfolio* there and everything else here.
"""
from __future__ import annotations

import asyncio
import logging
import queue
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .auth import schwab as schwab_auth
from .auth import token_store
from .llm_helpers import llm_for
from .providers import ANALYSTS, DEPTH_PRESETS, LANGUAGES, get_providers
from .runner import run_analysis_sync

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TradingAgents Web")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/providers")
def providers() -> dict[str, Any]:
    return {
        "providers": get_providers(),
        "analysts": ANALYSTS,
        "languages": LANGUAGES,
        "depth_presets": DEPTH_PRESETS,
    }


@app.get("/api/preferences")
def get_prefs() -> dict[str, Any]:
    return db.get_preferences()


@app.post("/api/preferences")
async def save_prefs(payload: dict[str, Any]) -> dict[str, str]:
    db.save_preferences(payload)
    return {"status": "saved"}


@app.get("/api/analyses")
def list_analyses() -> dict[str, Any]:
    return {"analyses": db.list_analyses()}


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: int) -> dict[str, Any]:
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return row


@app.delete("/api/analyses/{analysis_id}")
def delete_analysis_endpoint(analysis_id: int) -> dict[str, Any]:
    if not db.delete_analysis(analysis_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": analysis_id}


# ---------- Q&A and chart for saved analyses ----------

@app.post("/api/analyses/{analysis_id}/ask")
async def ask_about_analysis(analysis_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Multi-turn Q&A over a saved analysis.

    Body: {"question": str, "history": [{"role": "user"|"assistant", "content": str}, ...]}
    """
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")

    question = ((payload or {}).get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="missing 'question' in body")

    history = (payload or {}).get("history") or []
    if not isinstance(history, list):
        raise HTTPException(status_code=400, detail="'history' must be a list")

    sections = [
        ("Ticker", row.get("ticker", "")),
        ("Trade Date", row.get("trade_date", "")),
        ("Final Decision", row.get("final_decision", "")),
        ("Processed Signal", row.get("processed_signal", "")),
        ("Trader Plan", row.get("trader_plan") or row.get("trader_investment_plan") or ""),
        ("Investment Plan", row.get("investment_plan", "")),
        ("Market Report", row.get("market_report", "")),
        ("Sentiment Report", row.get("sentiment_report", "")),
        ("News Report", row.get("news_report", "")),
        ("Fundamentals Report", row.get("fundamentals_report", "")),
    ]
    context = "\n\n".join(f"## {name}\n{val}" for name, val in sections if val)

    system = (
        "You are a financial analyst answering follow-up questions about a saved stock analysis. "
        "Use ONLY the information provided in the analysis below. If the analysis does not address "
        "the question, say so directly rather than speculating. Be concise. When possible cite "
        "which section you're drawing from (e.g., 'per the Market Report' or 'per the Trader Plan')."
        f"\n\n=== ANALYSIS ===\n{context}\n=== END ANALYSIS ==="
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for turn in history:
        if (
            isinstance(turn, dict)
            and turn.get("role") in ("user", "assistant")
            and turn.get("content")
        ):
            messages.append({"role": turn["role"], "content": str(turn["content"])})
    messages.append({"role": "user", "content": question})

    prefs = db.get_preferences() or {}
    config: dict[str, Any] = {
        "llm_provider": prefs.get("provider") or "ollama",
        "deep_think_llm": prefs.get("deep_model"),
        "quick_think_llm": prefs.get("quick_model"),
    }

    try:
        llm_client = llm_for(config, deep=False, temperature=0.3)
        response = await asyncio.to_thread(llm_client.invoke, messages)
        answer = response.content if hasattr(response, "content") else str(response)
    except Exception as exc:
        log.exception("Q&A LLM call failed for analysis %s", analysis_id)
        raise HTTPException(status_code=500, detail=f"LLM error: {exc}")

    return {"answer": answer}


@app.get("/api/analyses/{analysis_id}/chart-data")
async def get_analysis_chart_data(
    analysis_id: int, lookback_days: int = 180
) -> dict[str, Any]:
    """Point-in-time OHLCV + indicator series for charting a saved analysis."""
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    ticker = row.get("ticker")
    trade_date = row.get("trade_date")
    if not ticker or not trade_date:
        raise HTTPException(status_code=400, detail="analysis missing ticker or trade_date")

    try:
        result = await asyncio.to_thread(_build_chart_data, ticker, trade_date, lookback_days)
    except Exception as exc:
        log.exception("chart data failed for %s on %s", ticker, trade_date)
        raise HTTPException(status_code=500, detail=str(exc))
    return result


def _build_chart_data(ticker: str, trade_date: str, lookback_days: int) -> dict[str, Any]:
    import pandas as pd
    from stockstats import wrap

    from tradingagents.dataflows.stockstats_utils import load_ohlcv

    df = load_ohlcv(ticker, trade_date)
    if df.empty:
        raise RuntimeError(f"no OHLCV data for {ticker} on or before {trade_date}")

    end_dt = pd.to_datetime(trade_date)
    start_dt = end_dt - pd.Timedelta(days=lookback_days)
    df = df[df["Date"] >= start_dt].copy()
    if df.empty:
        raise RuntimeError(
            f"no OHLCV data in {lookback_days}d lookback window for {ticker}"
        )

    df = df.sort_values("Date").reset_index(drop=True)
    ss = wrap(df.copy())

    indicator_map = {
        "rsi_14": "rsi_14",
        "macd": "macd",
        "macds": "macds",
        "macdh": "macdh",
        "sma_50": "close_50_sma",
        "sma_200": "close_200_sma",
        "ema_10": "close_10_ema",
        "boll": "boll",
        "boll_ub": "boll_ub",
        "boll_lb": "boll_lb",
    }
    # Trigger stockstats to compute each indicator (lazy columns)
    for ss_col in indicator_map.values():
        try:
            _ = ss[ss_col]
        except Exception:
            pass

    dates = df["Date"].dt.strftime("%Y-%m-%d").tolist()
    candles = [
        {
            "time": dates[i],
            "open": float(df["Open"].iloc[i]),
            "high": float(df["High"].iloc[i]),
            "low": float(df["Low"].iloc[i]),
            "close": float(df["Close"].iloc[i]),
        }
        for i in range(len(df))
    ]

    def _series(col_name: str) -> list[dict[str, Any]]:
        if col_name not in ss.columns:
            return []
        series = ss[col_name]
        out: list[dict[str, Any]] = []
        for i in range(len(series)):
            v = series.iloc[i] if hasattr(series, "iloc") else series[i]
            try:
                if pd.isna(v):
                    continue
                out.append({"time": dates[i], "value": float(v)})
            except (TypeError, ValueError):
                continue
        return out

    indicators = {friendly: _series(ss_col) for friendly, ss_col in indicator_map.items()}

    return {
        "ticker": ticker,
        "trade_date": trade_date,
        "lookback_days": lookback_days,
        "candles": candles,
        "indicators": indicators,
    }


# ---------- Schwab OAuth ----------

@app.get("/api/auth/schwab")
def schwab_login() -> RedirectResponse:
    return RedirectResponse(url=schwab_auth.build_auth_url(), status_code=302)


@app.get("/api/auth/schwab/callback")
def schwab_callback(code: str | None = None, error: str | None = None) -> HTMLResponse:
    if error:
        return HTMLResponse(f"<h1>Schwab auth error</h1><pre>{error}</pre>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Missing ?code= parameter</h1>", status_code=400)
    try:
        bundle = schwab_auth.exchange_code(code)
        token_store.save(bundle)
    except Exception as exc:
        log.exception("Schwab code exchange failed")
        return HTMLResponse(f"<h1>Exchange failed</h1><pre>{exc}</pre>", status_code=500)
    return HTMLResponse(
        """
        <html><body style='background:#0b0f14;color:#d6e1ea;font-family:monospace;padding:48px;text-align:center;'>
          <h2 style='color:#7be38c;'>✅ Schwab connected.</h2>
          <p>You can close this tab. The dashboard now has access.</p>
          <script>setTimeout(() => window.close(), 1500);</script>
        </body></html>
        """.strip()
    )


@app.get("/api/auth/schwab/status")
def schwab_status() -> dict[str, Any]:
    bundle = token_store.load()
    if not bundle:
        return {"connected": False, "days_until_refresh_expires": None}
    return {
        "connected": True,
        "days_until_refresh_expires": schwab_auth.refresh_days_remaining(bundle),
        "refresh_issued_at": bundle.refresh_issued_at,
    }


@app.delete("/api/auth/schwab")
def schwab_disconnect() -> dict[str, str]:
    token_store.clear()
    return {"status": "disconnected"}


# ---------- single-ticker analysis WebSocket (existing) ----------

@app.websocket("/api/analyze")
async def analyze(ws: WebSocket) -> None:
    await ws.accept()
    try:
        params = await ws.receive_json()
    except WebSocketDisconnect:
        return

    required = ("ticker", "trade_date")
    if any(not params.get(k) for k in required):
        await ws.send_json({"type": "error", "message": "missing ticker or trade_date"})
        await ws.close()
        return

    try:
        db.save_preferences(params)
    except Exception:
        pass

    try:
        analysis_id = db.create_analysis(params)
    except Exception as exc:
        await ws.send_json({"type": "error", "message": f"db init failed: {exc}"})
        await ws.close()
        return

    await ws.send_json({"type": "started", "analysis_id": analysis_id})

    frames: queue.Queue = queue.Queue()
    producer = asyncio.create_task(
        asyncio.to_thread(run_analysis_sync, params, analysis_id, frames)
    )

    loop = asyncio.get_running_loop()
    try:
        while True:
            frame = await loop.run_in_executor(None, frames.get)
            if frame is None:
                break
            try:
                await ws.send_json(frame)
            except (WebSocketDisconnect, RuntimeError):
                break
    finally:
        try:
            await producer
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass

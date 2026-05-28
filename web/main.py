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

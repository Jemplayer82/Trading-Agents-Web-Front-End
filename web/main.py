"""FastAPI app: dashboard + REST + WebSocket analyzer."""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .providers import ANALYSTS, DEPTH_PRESETS, LANGUAGES, get_providers
from .runner import run_analysis_sync


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

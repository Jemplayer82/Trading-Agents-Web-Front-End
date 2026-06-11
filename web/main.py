"""FastAPI app for tradingagents-api: dashboard backend + Schwab OAuth.

Portfolio scan routes live in `web/portfolio_main.py` in a separate container.
Nginx routes /api/portfolio* there and everything else here.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth_app, db
from . import credentials as creds
from .auth import schwab as schwab_auth
from .auth import token_store
from .llm_helpers import llm_for
from .providers import ANALYSTS, DEPTH_PRESETS, LANGUAGES, get_providers
from .runner import run_analysis_sync

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Custom app-setting keys must look like env vars.
_SETTING_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _total_budget() -> int:
    try:
        return max(1, int(os.environ.get("OLLAMA_MAX_CONCURRENCY", "5")))
    except (TypeError, ValueError):
        return 5


# Process-local cap on concurrent single-ticker analyses in THIS (api) container,
# so one user opening many tabs can't alone exceed the shared Ollama budget. The
# scanner in the portfolio container yields to these via the llm_activity table.
_SINGLE_SLOTS = threading.Semaphore(_total_budget())

app = FastAPI(title="TradingAgents Web")

# Gate every /api/ route behind a login session (allowlist + internal-token
# bypass live in auth_app). Registered before route handlers run.
app.middleware("http")(auth_app.auth_middleware)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    creds.apply_to_env()
    creds.apply_settings_to_env()
    try:
        db.purge_expired_sessions()
    except Exception:
        log.exception("session purge failed")
    try:
        db.purge_stale_activity()
    except Exception:
        log.exception("activity purge failed")


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


# ---------- authentication (dashboard login) ----------

@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    """Public. Reports auth state and whether first-run setup is needed."""
    if db.count_users() == 0:
        return {"authenticated": False, "setup_required": True}
    username = auth_app.current_username(request)
    return {"authenticated": bool(username), "username": username, "setup_required": False}


@app.post("/api/auth/setup")
def auth_setup(payload: dict[str, Any], response: Response) -> dict[str, Any]:
    """Public, but only works while no users exist. Creates the first admin."""
    if db.count_users() > 0:
        raise HTTPException(status_code=403, detail="setup already complete")
    username = ((payload or {}).get("username") or "").strip()
    password = (payload or {}).get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="username required, password >= 8 chars")
    db.create_user(username, auth_app.hash_password(password))
    token, _ = auth_app.new_session(username)
    auth_app.set_session_cookie(response, token)
    return {"status": "created", "username": username}


@app.post("/api/auth/login")
def auth_login(payload: dict[str, Any], response: Response) -> dict[str, Any]:
    username = ((payload or {}).get("username") or "").strip()
    password = (payload or {}).get("password") or ""
    user = db.get_user(username)
    if not user or not auth_app.verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid username or password")
    token, _ = auth_app.new_session(username)
    auth_app.set_session_cookie(response, token)
    return {"status": "ok", "username": username}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(auth_app.COOKIE_NAME)
    if token:
        db.delete_session(token)
    auth_app.clear_session_cookie(response)
    return {"status": "logged_out"}


@app.get("/api/auth/users")
def auth_list_users() -> dict[str, Any]:
    return {"users": db.list_users()}


@app.post("/api/auth/users")
def auth_add_user(payload: dict[str, Any]) -> dict[str, Any]:
    username = ((payload or {}).get("username") or "").strip()
    password = (payload or {}).get("password") or ""
    if not username or len(password) < 8:
        raise HTTPException(status_code=400, detail="username required, password >= 8 chars")
    try:
        db.create_user(username, auth_app.hash_password(password))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="username already exists") from None
    return {"status": "created", "username": username}


@app.post("/api/auth/password")
def auth_change_password(payload: dict[str, Any], request: Request) -> dict[str, str]:
    username = auth_app.current_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="not logged in")
    current = (payload or {}).get("current_password") or ""
    new = (payload or {}).get("new_password") or ""
    user = db.get_user(username)
    if not user or not auth_app.verify_password(current, user["password_hash"]):
        raise HTTPException(status_code=403, detail="current password incorrect")
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="new password must be >= 8 chars")
    db.set_user_password(username, auth_app.hash_password(new))
    return {"status": "changed"}


# ---------- app settings (env-style config managed from the UI) ----------

@app.get("/api/settings")
def list_settings_endpoint() -> dict[str, Any]:
    """Curated registry + custom settings, masked. Behind auth."""
    return creds.list_settings_meta()


@app.put("/api/settings/{key}")
def save_setting_endpoint(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = key.strip().upper()
    if not _SETTING_KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="key must match ^[A-Z][A-Z0-9_]*$")
    value = (payload or {}).get("value")
    if value is None or str(value) == "":
        raise HTTPException(status_code=400, detail="missing 'value' in body")
    db.set_app_setting(key, str(value))
    creds.apply_settings_to_env()
    return {"status": "saved", "key": key, "masked": creds.mask_setting(key, str(value))}


@app.delete("/api/settings/{key}")
def delete_setting_endpoint(key: str) -> dict[str, str]:
    key = key.strip().upper()
    if not db.delete_app_setting(key):
        raise HTTPException(status_code=404, detail="no setting stored for that key")
    # Unset from this process's env so the cleared value stops taking effect
    # immediately (other containers drop it at their next restart/refresh).
    os.environ.pop(key, None)
    return {"status": "cleared", "key": key}


# ---------- LLM provider API-key management ----------

@app.get("/api/credentials")
def list_credentials_endpoint() -> dict[str, Any]:
    """Per-provider credential metadata. Keys are masked — never raw."""
    return {"credentials": creds.list_meta()}


@app.put("/api/credentials/{provider}")
def save_credential_endpoint(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save an API key (and optional base URL) for `provider`.

    Body: {"api_key": str, "base_url": str?}
    """
    api_key = ((payload or {}).get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="missing 'api_key' in body")
    base_url = (payload or {}).get("base_url") or None
    if base_url:
        base_url = str(base_url).strip() or None
    db.set_credential(provider.lower(), api_key, base_url)
    creds.apply_to_env()
    return {"status": "saved", "provider": provider, "masked": creds.mask_key(api_key)}


@app.delete("/api/credentials/{provider}")
def delete_credential_endpoint(provider: str) -> dict[str, str]:
    """Clear the DB-stored credential for `provider`.

    Note: env vars set externally (.env, docker-compose) are NOT touched.
    """
    if not db.delete_credential(provider.lower()):
        raise HTTPException(status_code=404, detail="no DB credential set for that provider")
    return {"status": "cleared", "provider": provider}


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


@app.get("/api/ticker-info/{ticker}")
def ticker_info(ticker: str) -> dict[str, Any]:
    """Company name + website for a ticker, resolved via yfinance and cached.

    Falls back to the Yahoo Finance quote page when no corporate website is
    available, so the link is always usable.
    """
    t = (ticker or "").strip().upper()
    if not t:
        raise HTTPException(status_code=400, detail="missing ticker")
    # Validate before the value reaches yfinance / the Schwab MCP / a URL or the
    # cache path. safe_ticker_component enforces a strict charset and rejects
    # path-traversal-style input.
    try:
        from tradingagents.dataflows.utils import safe_ticker_component
        safe_ticker_component(t)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid ticker") from None
    yahoo = f"https://finance.yahoo.com/quote/{t}"

    cached = db.get_ticker_info(t)
    if cached:
        return {
            "ticker": t,
            "name": cached.get("name") or t,
            "website": cached.get("website") or yahoo,
        }

    name = t
    website = ""

    # Prefer the company name from a real-time Schwab quote (reference.description);
    # it's fast and avoids a yfinance .info call. yfinance still supplies the website.
    try:
        from tradingagents.dataflows import schwab_mcp
        quotes = schwab_mcp.get_quotes([t]) if schwab_mcp.market_data_enabled() else None
        desc = ((quotes or {}).get(t, {}).get("reference") or {}).get("description")
        if isinstance(desc, str) and desc.strip():
            name = desc.strip().title()
    except Exception:
        log.warning("schwab ticker name lookup failed for %s", t)

    try:
        import yfinance as yf  # lazy — keeps app startup light
        info = yf.Ticker(t).info or {}
        if name == t:
            name = info.get("longName") or info.get("shortName") or t
        site = info.get("website") or ""
        if isinstance(site, str) and site.startswith(("http://", "https://")):
            website = site
    except Exception:
        log.warning("ticker-info lookup failed for %s", t)

    final_website = website or yahoo
    # Only cache once we actually resolved something, so a transient yfinance
    # failure retries next time instead of sticking a poor result forever.
    if name != t or website:
        try:
            db.set_ticker_info(t, name, final_website)
        except Exception:
            pass
    return {"ticker": t, "name": name, "website": final_website}


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
        raise HTTPException(status_code=500, detail=f"LLM error: {exc}") from exc

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
        raise HTTPException(status_code=500, detail=str(exc)) from exc
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

# Short-lived cookie carrying the OAuth anti-CSRF state nonce between the
# /api/auth/schwab redirect and the Schwab callback. SameSite=lax (not strict)
# so it survives the top-level cross-site redirect back from Schwab.
_SCHWAB_STATE_COOKIE = "schwab_oauth_state"


@app.get("/api/auth/schwab")
def schwab_login() -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    resp = RedirectResponse(url=schwab_auth.build_auth_url(state), status_code=302)
    resp.set_cookie(
        _SCHWAB_STATE_COOKIE, state,
        max_age=600, httponly=True, samesite="lax", secure=True, path="/api/auth/schwab",
    )
    return resp


@app.get("/api/auth/schwab/callback")
def schwab_callback(
    request: Request, code: str | None = None, error: str | None = None, state: str | None = None
) -> HTMLResponse:
    # Verify the anti-CSRF state matches the nonce we set when starting the flow.
    expected_state = request.cookies.get(_SCHWAB_STATE_COOKIE)
    if not expected_state or not state or not hmac.compare_digest(state, expected_state):
        log.warning("Schwab callback rejected: missing or mismatched OAuth state")
        return HTMLResponse("<h1>Invalid or expired authorization request</h1>", status_code=400)
    if error:
        # Don't echo the raw upstream error back to the browser; log it instead.
        log.warning("Schwab auth returned error: %s", error)
        return HTMLResponse("<h1>Schwab authorization failed</h1>", status_code=400)
    if not code:
        return HTMLResponse("<h1>Missing ?code= parameter</h1>", status_code=400)
    try:
        bundle = schwab_auth.exchange_code(code)
        token_store.save(bundle)
    except Exception:
        log.exception("Schwab code exchange failed")
        return HTMLResponse("<h1>Authorization failed. Please try again.</h1>", status_code=500)
    resp = HTMLResponse(
        """
        <html><body style='background:#0b0f14;color:#d6e1ea;font-family:monospace;padding:48px;text-align:center;'>
          <h2 style='color:#7be38c;'>✅ Schwab connected.</h2>
          <p>You can close this tab. The dashboard now has access.</p>
          <script>setTimeout(() => window.close(), 1500);</script>
        </body></html>
        """.strip()
    )
    # One-time nonce; drop it now that the flow is complete.
    resp.delete_cookie(_SCHWAB_STATE_COOKIE, path="/api/auth/schwab")
    return resp


@app.get("/api/auth/schwab/status")
def schwab_status() -> dict[str, Any]:
    """Schwab connectivity via the MCP server. `enabled` is the master switch;
    `connected` reflects whether the MCP's Schwab session currently returns data."""
    from tradingagents.dataflows import schwab_mcp
    if not schwab_mcp.schwab_enabled():
        return {"enabled": False, "connected": False, "source": "mcp"}
    accounts = None
    try:
        accounts = schwab_mcp.get_accounts(fields="positions")
    except Exception:
        log.debug("[schwab_status] MCP read failed", exc_info=True)
    return {
        "enabled": True,
        "connected": bool(accounts),
        "num_accounts": len(accounts) if isinstance(accounts, list) else 0,
        "source": "mcp",
    }


@app.delete("/api/auth/schwab")
def schwab_disconnect() -> dict[str, str]:
    token_store.clear()
    return {"status": "disconnected"}


# ---------- single-ticker analysis WebSocket (existing) ----------

@app.websocket("/api/analyze")
async def analyze(ws: WebSocket) -> None:
    # The http auth middleware doesn't see websocket scope, so gate here:
    # require a valid session cookie (or the internal-token header).
    token = ws.cookies.get(auth_app.COOKIE_NAME)
    internal = ws.headers.get("x-internal-token")
    expected = os.environ.get("INTERNAL_API_TOKEN")
    authed = bool(token and db.get_session(token)) or bool(
        internal and expected and hmac.compare_digest(internal, expected)
    )
    if not authed:
        await ws.close(code=4401)
        return
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

    # Register this run in the cross-container LLM activity registry so the
    # S&P 500 scanner yields worker slots to it (single-ticker priority).
    ticker_label = str(params.get("ticker", "")).strip().upper()
    activity_id = db.register_activity("single", ticker_label)

    def _worker() -> None:
        # Process-local cap: block until a slot frees if this api container is
        # already at the shared budget. The scanner sees us via llm_activity
        # the moment we registered above, so it has already begun yielding.
        with _SINGLE_SLOTS:
            run_analysis_sync(params, analysis_id, frames)

    frames: queue.Queue = queue.Queue()
    producer = asyncio.create_task(asyncio.to_thread(_worker))

    loop = asyncio.get_running_loop()
    last_hb = 0.0
    try:
        while True:
            frame = await loop.run_in_executor(None, frames.get)
            if frame is None:
                break
            # Heartbeat the activity row at most every 10s so a long run keeps
            # its slot reservation fresh (stale rows are reclaimed after 120s).
            now = time.monotonic()
            if now - last_hb >= 10.0:
                last_hb = now
                try:
                    db.heartbeat_activity(activity_id)
                except Exception:
                    pass
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
            db.clear_activity(activity_id)
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass

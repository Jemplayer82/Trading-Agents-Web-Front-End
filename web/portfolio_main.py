"""FastAPI service dedicated to portfolio scans.

Runs inside the tradingagents-portfolio container. Reads Schwab tokens from the
shared volume, runs each holding through TradingAgentsGraph, calls the
aggregator, and writes results to web.db. The scheduler container fires this
at 22:00 ET nightly.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from fastapi import BackgroundTasks, FastAPI, HTTPException

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.portfolio_graph import run_portfolio_scan

from . import auth_app
from . import credentials as creds
from . import db
from .auth import schwab_client, token_store, schwab as schwab_auth
from .portfolio import aggregator
from . import spy_scanner, spy_allocator
from .spy_tickers import get_sp500_tickers

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="TradingAgents Portfolio")

# Same login gate as the api container. Validates the shared sessions
# table; scheduler->portfolio cron calls pass via the X-Internal-Token
# bypass in auth_app.
app.middleware("http")(auth_app.auth_middleware)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    creds.apply_to_env()
    creds.apply_settings_to_env()
    # Clear any LLM-activity rows left stale by a previous crash so the
    # scanner's dynamic concurrency starts from an accurate count.
    try:
        db.purge_stale_activity()
    except Exception:
        log.exception("[startup] purge_stale_activity failed")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "portfolio"}


@app.get("/api/auth/schwab/status")
def schwab_status() -> dict[str, Any]:
    """Same shape as the api container's endpoint — the scheduler hits whichever."""
    bundle = token_store.load()
    if not bundle:
        return {"connected": False, "days_until_refresh_expires": None}
    return {
        "connected": True,
        "days_until_refresh_expires": schwab_auth.refresh_days_remaining(bundle),
        "refresh_issued_at": bundle.refresh_issued_at,
    }


@app.post("/api/portfolio-scan")
async def start_scan(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Kick off a portfolio scan. Idempotent for the same date — returns the
    existing scan_id if a non-failed scan was already created today.
    """
    today = datetime.utcnow().date().isoformat()
    # Idempotency check
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, status FROM portfolio_scans WHERE trade_date = ? AND status != 'failed' ORDER BY id DESC LIMIT 1",
            (today,),
        ).fetchone()
    if row:
        return {"scan_id": int(row["id"]), "status": row["status"], "new": False}

    if not token_store.load():
        raise HTTPException(status_code=400, detail="Schwab not connected — visit /api/auth/schwab first")

    scan_id = db.create_portfolio_scan(today)
    background_tasks.add_task(_run_scan_thread, scan_id, today)
    return {"scan_id": scan_id, "status": "running", "new": True}


@app.get("/api/portfolio-scans")
def list_scans(limit: int = 50) -> dict[str, Any]:
    return {"scans": db.list_portfolio_scans(limit=limit)}


@app.get("/api/portfolio-scans/{scan_id}")
def get_scan(scan_id: int) -> dict[str, Any]:
    scan = db.get_portfolio_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="not found")
    return scan


@app.delete("/api/portfolio-scans/{scan_id}")
def delete_scan(scan_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM portfolio_scans WHERE id = ?", (scan_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": scan_id}


# ---------- background worker ----------

def _refresh_creds_from_db() -> None:
    """Re-apply DB-stored API keys to env before a scan starts.

    The api container hosts the UI where the user saves keys; this
    container only sees them via the shared sqlite DB. Refreshing
    here means a credential or app setting (Schwab key, etc.) saved
    mid-day takes effect on the very next scan without a restart.
    """
    try:
        creds.apply_to_env()
        creds.apply_settings_to_env()
    except Exception:
        log.exception("[creds] refresh failed")


def _run_scan_thread(scan_id: int, trade_date: str) -> None:
    """Synchronous worker run inside a thread by FastAPI BackgroundTasks."""
    _refresh_creds_from_db()
    try:
        _run_scan(scan_id, trade_date)
    except Exception as exc:
        log.exception("Scan %s crashed", scan_id)
        db.fail_portfolio_scan(scan_id, str(exc))


def _run_scan(scan_id: int, trade_date: str) -> None:
    log.info("[scan %s] starting for %s", scan_id, trade_date)

    # Step 1: fetch positions from Schwab
    account_data, bundle = schwab_client.get_account_numbers()
    if not account_data:
        raise RuntimeError("Schwab returned no accounts")
    account_hash = account_data[0].get("hashValue") or account_data[0].get("accountNumber")
    if not account_hash:
        raise RuntimeError(f"No accountHash in {account_data[0]}")
    positions, bundle = schwab_client.get_positions(account_hash, bundle=bundle)
    log.info("[scan %s] %d positions from Schwab", scan_id, len(positions))

    # Step 2: load user preferences for LLM / analyst config
    prefs = db.get_preferences() or {}
    config: dict[str, Any] = {
        "llm_provider": prefs.get("provider") or "ollama",
        "deep_think_llm": prefs.get("deep_model") or "gpt-oss:120b-cloud",
        "quick_think_llm": prefs.get("quick_model") or "gpt-oss:20b-cloud",
        "max_debate_rounds": int(prefs.get("research_depth") or 1),
    }
    selected_analysts = prefs.get("analysts") or ["market", "social", "news", "fundamentals"]

    # Step 3: for each position, create an analyses row + run the graph
    per_ticker_payload: list[dict[str, Any]] = []
    pos_dicts = [
        {"symbol": p.symbol, "quantity": p.quantity, "market_value": p.market_value, "asset_type": p.asset_type}
        for p in positions
    ]

    from tradingagents.graph.portfolio_graph import run_single_ticker

    counts = {"BUY": 0, "HOLD": 0, "SELL": 0}
    for i, pos in enumerate(pos_dicts, start=1):
        ticker = pos["symbol"]
        log.info("[scan %s] %d/%d: %s", scan_id, i, len(pos_dicts), ticker)
        analysis_id = db.create_analysis({
            "ticker": ticker,
            "trade_date": trade_date,
            "provider": config["llm_provider"],
            "deep_model": config["deep_think_llm"],
            "quick_model": config["quick_think_llm"],
            "analysts": selected_analysts,
            "research_depth": config["max_debate_rounds"],
            "language": prefs.get("language", "English"),
        })
        try:
            result = run_single_ticker(ticker, trade_date, config, selected_analysts)
            final_state = result["final_state"]
            signal = (result.get("signal") or "").upper()
            db.complete_analysis(analysis_id, final_state, signal)
            if signal in counts:
                counts[signal] += 1
            db.add_scan_ticker(scan_id, ticker, analysis_id, pos["quantity"], pos["market_value"], signal)
            per_ticker_payload.append({
                "ticker": ticker,
                "signal": signal,
                "quantity": pos["quantity"],
                "market_value": pos["market_value"],
                "trader_plan": final_state.get("trader_investment_plan", ""),
                "final_decision": final_state.get("final_trade_decision", ""),
            })
        except Exception as exc:
            log.exception("[scan %s] failed for %s", scan_id, ticker)
            db.fail_analysis(analysis_id, str(exc))
            db.add_scan_ticker(scan_id, ticker, analysis_id, pos["quantity"], pos["market_value"], None, error=str(exc))
            per_ticker_payload.append({
                "ticker": ticker,
                "signal": "",
                "quantity": pos["quantity"],
                "market_value": pos["market_value"],
                "trader_plan": "",
                "final_decision": f"(failed: {exc})",
            })

    # Step 4: aggregator pass
    log.info("[scan %s] running aggregator over %d tickers", scan_id, len(per_ticker_payload))
    aggregator_md = aggregator.run(per_ticker_payload, trade_date, config)

    # Step 5: persist final scan row
    db.complete_portfolio_scan(
        scan_id=scan_id,
        aggregator_report=aggregator_md,
        signal_counts=counts,
        num_tickers=len(per_ticker_payload),
        full_payload={"per_ticker": per_ticker_payload, "config": config},
    )
    log.info("[scan %s] done — %s", scan_id, counts)


# ---------- S&P 500 scanner endpoints ----------

@app.post("/api/spy-scan")
async def start_spy_scan(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Trigger a full S&P 500 scan. Idempotent for today."""
    today = datetime.utcnow().date().isoformat()
    # A failed or cancelled scan from today must NOT block a fresh run.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, status FROM spy_scans WHERE trade_date = ? AND status NOT IN ('failed', 'cancelled') ORDER BY id DESC LIMIT 1",
            (today,),
        ).fetchone()
    if row:
        return {"scan_id": int(row["id"]), "status": row["status"], "new": False}

    scan_id = db.create_spy_scan(today)
    background_tasks.add_task(_run_spy_scan_thread, scan_id, today)
    return {"scan_id": scan_id, "status": "running_quick", "new": True}


@app.get("/api/spy-scans")
def list_spy_scans(limit: int = 50) -> dict[str, Any]:
    return {"scans": db.list_spy_scans(limit=limit)}


@app.get("/api/spy-scans/{scan_id}")
def get_spy_scan(scan_id: int) -> dict[str, Any]:
    scan = db.get_spy_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="not found")
    return scan


@app.delete("/api/spy-scans/{scan_id}")
def delete_spy_scan_endpoint(scan_id: int) -> dict[str, Any]:
    if not db.delete_spy_scan(scan_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": scan_id}


@app.post("/api/spy-scans/{scan_id}/cancel")
def cancel_spy_scan(scan_id: int) -> dict[str, Any]:
    """Cooperatively cancel a running S&P 500 scan.

    Sets a flag the scan worker polls between LLM calls; the worker stops
    submitting new work, lets in-flight calls finish, and marks the scan
    'cancelled'. Returns immediately.
    """
    scan = db.get_spy_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="not found")
    if not str(scan.get("status", "")).startswith("running"):
        return {"status": scan.get("status"), "cancelling": False}
    db.request_spy_scan_cancel(scan_id)
    return {"status": "cancelling", "cancelling": True}


@app.post("/api/spy-scans/{scan_id}/refresh-prices")
def refresh_spy_prices(scan_id: int) -> dict[str, Any]:
    return spy_scanner.refresh_portfolio_prices(scan_id)


@app.post("/api/spy-scans/latest/refresh-prices")
def refresh_spy_prices_latest() -> dict[str, Any]:
    scan = db.latest_spy_scan()
    if not scan:
        raise HTTPException(status_code=404, detail="no scans found")
    return spy_scanner.refresh_portfolio_prices(int(scan["id"]))


def _run_spy_scan_thread(scan_id: int, trade_date: str) -> None:
    _refresh_creds_from_db()
    try:
        _run_spy_scan(scan_id, trade_date)
    except spy_scanner.ScanCancelled:
        log.info("SPY scan %s cancelled by user", scan_id)
        db.update_spy_scan(scan_id, status="cancelled")
    except Exception as exc:
        log.exception("SPY scan %s crashed", scan_id)
        db.fail_spy_scan(scan_id, str(exc))


@contextmanager
def _phase(label: str) -> Iterator[None]:
    """Tag any failure inside a scan phase with a human-readable prefix.

    A user-initiated cancellation (ScanCancelled) is passed through untouched
    so it is recorded as 'cancelled', not 'failed'.
    """
    try:
        yield
    except spy_scanner.ScanCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised with friendlier context
        raise RuntimeError(f"{label}: {exc}") from exc


def _run_spy_scan(scan_id: int, trade_date: str) -> None:
    log.info("[spy %s] starting for %s", scan_id, trade_date)
    prefs = db.get_preferences() or {}
    config: dict[str, Any] = {
        "llm_provider": prefs.get("provider") or "ollama",
        "deep_think_llm": prefs.get("deep_model") or DEFAULT_CONFIG.get("deep_think_llm"),
        "quick_think_llm": prefs.get("quick_model") or DEFAULT_CONFIG.get("quick_think_llm"),
        "max_debate_rounds": int(prefs.get("research_depth") or 1),
        "output_language": prefs.get("language", "English"),
    }
    selected_analysts = prefs.get("analysts") or ["market", "social", "news", "fundamentals"]

    # Look up the previous completed scan to enable rebalancing.
    prev_scan = db.get_latest_completed_spy_scan(exclude_id=scan_id)
    previous_portfolio: list[dict[str, Any]] | None = None
    previous_scan_id: int | None = None
    starting_value: float = 100_000.0

    if prev_scan:
        prev_portfolio_raw = prev_scan.get("portfolio_json") or []
        # Only use previous portfolio if it has active (non-exited) positions.
        active_prev = [p for p in prev_portfolio_raw if p.get("action") != "EXITED" and p.get("dollar_amount", 0) > 0]
        if active_prev:
            previous_portfolio = prev_portfolio_raw
            previous_scan_id = int(prev_scan["id"])
            # Use last refreshed value as capital; fall back to sum of allocations.
            if prev_scan.get("current_value"):
                starting_value = float(prev_scan["current_value"])
            else:
                starting_value = float(sum(
                    p.get("dollar_amount", 0) for p in active_prev
                )) or 100_000.0
            log.info(
                "[spy %s] rebalancing from scan #%s, capital $%s",
                scan_id, previous_scan_id, f"{starting_value:,.0f}",
            )

    # Phase 1: quick scan all S&P 500
    with _phase("Couldn't fetch the S&P 500 ticker list"):
        tickers = get_sp500_tickers()
    with _phase("Quick scan failed"):
        quick_results = spy_scanner.run_quick_scan(scan_id, tickers, config)

    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    # Phase 2: deep dive top 50 by conviction
    buy_or_hold = [r for r in quick_results if (r.get("signal") or "").upper() in ("BUY", "HOLD")]
    top50 = sorted(buy_or_hold, key=lambda r: -(r.get("conviction") or 0))[:50]
    if not top50:
        top50 = sorted(quick_results, key=lambda r: -(r.get("conviction") or 0))[:50]
    with _phase("Deep-dive analysis failed"):
        enriched = spy_scanner.run_deep_dives(scan_id, top50, trade_date, config, selected_analysts)

    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    # Phase 3: allocator (rebalance if a previous portfolio exists, else fresh $100k)
    db.update_spy_scan(scan_id, status="running_alloc")
    with _phase("Portfolio allocation failed"):
        alloc_result = spy_allocator.run(
            enriched,
            trade_date,
            config,
            previous_portfolio=previous_portfolio,
            starting_value=starting_value,
        )
    portfolio = alloc_result.get("allocations", [])

    db.complete_spy_scan(
        scan_id=scan_id,
        allocator_report=alloc_result.get("report_md", ""),
        portfolio_json=portfolio,
        previous_scan_id=previous_scan_id,
        starting_value=alloc_result.get("starting_value", starting_value),
    )
    log.info("[spy %s] done — %d positions, capital $%s → deployed $%s", scan_id,
             len([p for p in portfolio if p.get("action") != "EXITED"]),
             f"{starting_value:,.0f}", f"{alloc_result.get('total', 0):,.0f}")

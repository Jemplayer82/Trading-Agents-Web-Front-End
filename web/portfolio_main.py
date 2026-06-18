"""FastAPI service dedicated to portfolio + S&P 500 scans.

This is a SEPARATE app from web/main.py, run in its own container
(`uvicorn web.portfolio_main:app`), so multi-hour scans never block the ad-hoc
api app. nginx (web/nginx.conf) routes /api/spy*, /api/accounts and
/api/portfolio* here; everything else under /api/ goes to the api app. That
routing is a hard contract: an endpoint added here without a matching nginx
location block is a silent 404 in production — this has bitten us before.

Two scan pipelines, both fired by the scheduler container over the Docker
network (portfolio nightly at 22:00 ET, S&P Saturday 00:00) and also
triggerable from the dashboard:

- Portfolio scan (_run_scan): real holdings via brokerages.fetch_all_accounts()
  (normalized cross-brokerage dicts, account ids namespaced "schwab:12345678"),
  each equity through SwitchboardOrchestrator, then the aggregator briefing. Option
  positions are display-only on the dashboard and are skipped (logged) before
  the analysis loop.
- S&P scan (_run_spy_scan): quick-screen all ~500 tickers, deep-dive the top
  ~50 by conviction, then spy_allocator builds/rebalances a $100k paper
  portfolio. Cancellation is cooperative via spy_scans.cancel_requested.

Progress contract: _run_scan writes scan_total once, then scanned_count /
current_ticker per ticker (spy scans: quick_count/quick_total +
deep_count/deep_total); the frontend polls the scan detail endpoint every 5s
and renders a progress bar from those columns.

Everything persists to the shared SQLite DB (web/db.py). Credentials the user
saves in the api container's UI are re-read from that DB before each scan, so
a new key takes effect without restarting this container.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException

from tradingagents.constants import SIGNALS
from tradingagents.dataflows import schwab_mcp
from tradingagents.default_config import DEFAULT_CONFIG

from . import alerts, auth_app, brokerages, db, spy_allocator, spy_scanner
from . import credentials as creds
from ._logging import configure_logging
from .portfolio import aggregator
from .spy_tickers import get_sp500_tickers

log = logging.getLogger(__name__)
configure_logging()

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
    """Schwab connectivity via the MCP server (the scheduler hits whichever container).

    `enabled` is the master SCHWAB_ENABLED switch; `connected` reflects whether
    the MCP server currently returns account data (its Schwab session is authed).
    """
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

    if not schwab_mcp.schwab_enabled():
        raise HTTPException(status_code=400, detail="Schwab is disabled (SCHWAB_ENABLED=0). Enable it in Settings to run a portfolio scan.")
    if not schwab_mcp.get_accounts(fields="positions"):
        raise HTTPException(status_code=400, detail="Schwab MCP not connected — re-authorize at https://schwab.txferguson.net/auth")

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
        alerts.notify_run_failed(
            kind="Portfolio scan", run_id=scan_id, label=trade_date, error=str(exc)
        )


def _mcp_positions() -> list[dict[str, Any]]:
    """Real holdings across all enabled brokerages, aggregated by symbol.

    Returns [{symbol, quantity, market_value, asset_type}], or [] if no
    brokerage is reachable / authed.
    """
    agg: dict[str, dict[str, Any]] = {}
    for acct in brokerages.fetch_all_accounts():
        for pos in acct["positions"]:
            e = agg.setdefault(pos["symbol"], {
                "symbol": pos["symbol"], "quantity": 0.0, "market_value": 0.0,
                "asset_type": pos["asset_type"],
            })
            e["quantity"] += pos["shares"]
            e["market_value"] += pos["market_value"]
    return list(agg.values())


def _run_scan(scan_id: int, trade_date: str) -> None:
    """Portfolio scan worker: holdings -> per-ticker graph runs -> aggregator.

    A per-ticker failure is recorded (fail_analysis + an error row in the
    payload) and the loop continues; only a failure outside the loop fails
    the whole scan.
    """
    log.info("[scan %s] starting for %s", scan_id, trade_date)

    # Step 1: fetch positions via the Schwab MCP server
    pos_dicts = _mcp_positions()
    if not pos_dicts:
        raise RuntimeError("Schwab MCP returned no positions — re-authorize at https://schwab.txferguson.net/auth")
    log.info("[scan %s] %d positions from Schwab MCP", scan_id, len(pos_dicts))

    # Options are excluded from AI analysis — they still display on the
    # holdings cards (with expiration), but the agents only scan equities.
    skipped = [p for p in pos_dicts if p["asset_type"] == "OPTION"]
    pos_dicts = [p for p in pos_dicts if p["asset_type"] != "OPTION"]
    if skipped:
        log.info("[scan %s] skipping %d option position(s): %s",
                 scan_id, len(skipped), [p["symbol"] for p in skipped])
    if not pos_dicts:
        raise RuntimeError("Only option positions held — nothing to scan (options are excluded from AI analysis).")

    # Record the total number of tickers to be scanned so the frontend can show a progress bar.
    db.update_portfolio_scan(scan_id, scan_total=len(pos_dicts))

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

    from tradingagents.orchestrator import SwitchboardOrchestrator

    counts = {sig: 0 for sig in SIGNALS}
    for i, pos in enumerate(pos_dicts, start=1):
        ticker = pos["symbol"]
        # scanned_count = completed-so-far (i-1), current_ticker = the one being analyzed now.
        # This lets the UI read "k/N done, working on TICKER".
        db.update_portfolio_scan(scan_id, scanned_count=i - 1, current_ticker=ticker)
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
            orch = SwitchboardOrchestrator(config=config, selected_analysts=selected_analysts)
            final_state, signal = orch.run(ticker, trade_date)
            signal = (signal or "").upper()
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


# ---------- Paper trading accounts ----------

@app.get("/api/paper-accounts")
def list_paper_accounts() -> dict[str, Any]:
    return {"accounts": db.list_paper_accounts()}


@app.post("/api/paper-accounts")
def create_paper_account(body: dict[str, Any]) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        account_id = db.create_paper_account(
            name=name,
            starting_capital=float(body.get("starting_capital") or 100_000),
            aggressiveness=int(body.get("aggressiveness") or 5),
            bias=body.get("bias") or "neutral",
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(status_code=409, detail=f"Account '{name}' already exists")
        raise
    account = db.get_paper_account(account_id)
    return {"account": account}


@app.put("/api/paper-accounts/{account_id}")
def update_paper_account(account_id: int, body: dict[str, Any]) -> dict[str, Any]:
    if not db.get_paper_account(account_id):
        raise HTTPException(status_code=404, detail="not found")
    db.update_paper_account(
        account_id=account_id,
        name=body.get("name"),
        starting_capital=float(body["starting_capital"]) if "starting_capital" in body else None,
        aggressiveness=int(body["aggressiveness"]) if "aggressiveness" in body else None,
        bias=body.get("bias"),
    )
    return {"account": db.get_paper_account(account_id)}


@app.delete("/api/paper-accounts/{account_id}")
def delete_paper_account(account_id: int) -> dict[str, Any]:
    if not db.delete_paper_account(account_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"status": "deleted", "id": account_id}


# ---------- S&P 500 scanner endpoints ----------

@app.post("/api/spy-scan")
async def start_spy_scan(
    body: dict[str, Any] | None = None,
    background_tasks: BackgroundTasks = None,
) -> dict[str, Any]:
    """Trigger a full S&P 500 scan. Idempotent for today.

    Optional body: {account_id: int} — ties the scan to a paper account and
    inherits its starting_capital, aggressiveness, and bias settings.
    """
    body = body or {}
    today = datetime.utcnow().date().isoformat()

    account_id: int | None = body.get("account_id")
    account: dict[str, Any] | None = None
    if account_id:
        account = db.get_paper_account(int(account_id))
        if not account:
            raise HTTPException(status_code=404, detail="paper account not found")

    aggressiveness = int(body.get("aggressiveness") or (account or {}).get("aggressiveness") or 5)
    bias = body.get("bias") or (account or {}).get("bias") or "neutral"

    # A failed or cancelled scan from today must NOT block a fresh run.
    with db.connect() as conn:
        where = "trade_date = ? AND status NOT IN ('failed', 'cancelled')"
        params: list[Any] = [today]
        if account_id:
            where += " AND paper_account_id = ?"
            params.append(account_id)
        row = conn.execute(
            f"SELECT id, status FROM spy_scans WHERE {where} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
    if row:
        return {"scan_id": int(row["id"]), "status": row["status"], "new": False}

    scan_id = db.create_spy_scan(
        today,
        paper_account_id=account_id,
        aggressiveness=aggressiveness,
        bias=bias,
    )
    background_tasks.add_task(_run_spy_scan_thread, scan_id, today)
    return {"scan_id": scan_id, "status": "running_quick", "new": True}


@app.get("/api/spy-scans")
def list_spy_scans(limit: int = 50, account_id: int | None = None) -> dict[str, Any]:
    return {"scans": db.list_spy_scans(limit=limit, paper_account_id=account_id)}


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


# NOTE: /latest/... must come BEFORE /{scan_id}/... — FastAPI matches routes
# in declaration order and would greedily bind "latest" as an int scan_id
# (returning 422) if the parameterised route is declared first.
@app.post("/api/spy-scans/latest/refresh-prices")
def refresh_spy_prices_latest() -> dict[str, Any]:
    scan = db.latest_spy_scan()
    if not scan:
        raise HTTPException(status_code=404, detail="no scans found")
    return spy_scanner.refresh_portfolio_prices(int(scan["id"]))


@app.post("/api/spy-scans/{scan_id}/refresh-prices")
def refresh_spy_prices(scan_id: int) -> dict[str, Any]:
    return spy_scanner.refresh_portfolio_prices(scan_id)


# ---------- Live Schwab account (read-only, via Schwab MCP) ----------

def _parse_schwab_account(accounts: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """Aggregate a Schwab getAccounts payload (possibly several accounts) into a
    single combined view: positions summed by symbol, plus total cash + value.

    Balance fields vary by account: currentBalances often omits
    liquidationValue/cashBalance, so we fall back to equity / initialBalances.

    DEPRECATED: this is the legacy Schwab-only parser kept for the /api/spy-account
    drift views. New per-account/holdings code goes through the brokerage-agnostic
    ``brokerages.fetch_all_accounts()`` (web/brokerages.py), which also handles
    options and multiple providers. Migrate /api/spy-account to it when convenient.
    """
    if not accounts:
        return None

    agg: dict[str, dict[str, Any]] = {}
    cash = 0.0
    total_value = 0.0
    for a in accounts:
        sec = a.get("securitiesAccount") or a
        for p in sec.get("positions") or []:
            instr = p.get("instrument") or {}
            sym = instr.get("symbol")
            qty = float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)
            if not sym or qty == 0:
                continue
            e = agg.setdefault(sym, {"symbol": sym, "shares": 0.0, "market_value": 0.0, "_cost": 0.0})
            e["shares"] += qty
            e["market_value"] += float(p.get("marketValue") or 0)
            e["_cost"] += float(p.get("averagePrice") or 0) * qty

        cur = sec.get("currentBalances") or {}
        init = sec.get("initialBalances") or {}
        tv = cur.get("liquidationValue")
        if tv is None:
            tv = cur.get("equity")
        if tv is None:
            tv = init.get("liquidationValue") or init.get("accountValue") or 0
        total_value += float(tv or 0)
        c = cur.get("cashBalance")
        if c is None:
            c = init.get("cashBalance")
        if c is None:
            c = init.get("totalCash") or 0
        cash += float(c or 0)

    positions = []
    for e in agg.values():
        shares = e["shares"]
        positions.append({
            "symbol": e["symbol"],
            "shares": round(shares, 4),
            "market_value": round(e["market_value"], 2),
            "average_price": round(e["_cost"] / shares, 2) if shares else 0,
        })
    positions.sort(key=lambda x: -x["market_value"])
    return {
        "positions": positions,
        "cash": round(cash, 2),
        "liquidation_value": round(total_value, 2),
        "num_accounts": len(accounts),
    }


def _accounts_split() -> list[dict[str, Any]]:
    """Per-account live holdings for the live-holdings UI panel.

    Returns [all_entry, ...per_account] from all enabled brokerage providers
    (see web.brokerages). Each entry has: id, brokerage, label, positions,
    total_value, cash, cost_basis, gain_dollars, gain_percent. Each position
    is the normalized brokerages shape (incl. option fields), optionally with
    signal/analysis_id merged from the latest completed portfolio scan.
    """
    per_account = brokerages.fetch_all_accounts()
    if not per_account:
        return []

    # Build "All Accounts" aggregate across every brokerage, keyed by symbol.
    all_syms: dict[str, dict[str, Any]] = {}
    for acct in per_account:
        for pos in acct["positions"]:
            ae = all_syms.setdefault(pos["symbol"], {
                "symbol": pos["symbol"],
                "display_symbol": pos["display_symbol"],
                "shares": 0.0, "market_value": 0.0, "_cost": 0.0,
                "asset_type": pos["asset_type"],
                "multiplier": pos["multiplier"],
                "expiration_date": pos["expiration_date"],
                "strike": pos["strike"],
                "put_call": pos["put_call"],
                "underlying": pos["underlying"],
            })
            ae["shares"] += pos["shares"]
            ae["market_value"] += pos["market_value"]
            ae["_cost"] += pos["cost_basis"]

    all_positions: list[dict[str, Any]] = []
    all_cost = 0.0
    all_mv = 0.0
    for ae in all_syms.values():
        sh = ae["shares"]
        mult = ae["multiplier"]
        mv = ae["market_value"]
        cost = ae.pop("_cost")
        gain = mv - cost
        gain_pct = (gain / cost * 100) if cost else 0.0
        ae.update({
            "shares": round(sh, 4),
            "average_price": round(cost / (sh * mult), 4) if sh else 0.0,
            "current_price": round(mv / (sh * mult), 4) if sh else 0.0,
            "market_value": round(mv, 2),
            "cost_basis": round(cost, 2),
            "gain_dollars": round(gain, 2),
            "gain_percent": round(gain_pct, 4),
        })
        all_positions.append(ae)
        all_cost += cost
        all_mv += mv
    all_positions.sort(key=lambda x: -x["market_value"])
    all_gain = all_mv - all_cost
    all_entry: dict[str, Any] = {
        "id": "all",
        "label": "All Accounts",
        "positions": all_positions,
        "total_value": round(sum(a["total_value"] for a in per_account), 2),
        "cash": round(sum(a["cash"] for a in per_account), 2),
        "cost_basis": round(all_cost, 2),
        "gain_dollars": round(all_gain, 2),
        "gain_percent": round((all_gain / all_cost * 100) if all_cost else 0.0, 4),
    }

    # Merge latest scan signals onto matching positions (best-effort)
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM portfolio_scans WHERE status='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            latest = db.get_portfolio_scan(row["id"])
            ticker_map = {t["ticker"]: t for t in (latest.get("tickers") or [])}
            for acct in [all_entry] + per_account:
                for pos in acct["positions"]:
                    t = ticker_map.get(pos["symbol"])
                    if t:
                        pos["signal"] = t.get("signal")
                        pos["analysis_id"] = t.get("analysis_id")
    except Exception:
        pass

    return [all_entry] + per_account


@app.get("/api/accounts")
def accounts() -> dict[str, Any]:
    """Live per-account holdings with cost basis, gain/loss, and optional AI scan signals."""
    if not brokerages.any_enabled():
        return {"enabled": False, "connected": False}
    try:
        data = _accounts_split()
    except Exception:
        log.exception("[accounts] brokerage read failed")
        data = None
    if not data:
        return {"enabled": True, "connected": False}
    return {"enabled": True, "connected": True, "accounts": data}


@app.get("/api/spy-account")
def spy_account() -> dict[str, Any]:
    """Live Schwab holdings + balances via the Schwab MCP server (read-only)."""
    if not schwab_mcp.schwab_enabled():
        return {"enabled": False, "connected": False}
    try:
        parsed = _parse_schwab_account(schwab_mcp.get_accounts(fields="positions"))
    except Exception:
        log.exception("[spy-account] Schwab MCP read failed")
        parsed = None
    if not parsed:
        return {"enabled": True, "connected": False}
    return {"enabled": True, "connected": True, **parsed}


@app.get("/api/spy-account/compare")
def spy_account_compare() -> dict[str, Any]:
    """Drift between the latest completed paper SPY portfolio and the real account."""
    if not schwab_mcp.schwab_enabled():
        return {"enabled": False, "connected": False}
    try:
        parsed = _parse_schwab_account(schwab_mcp.get_accounts(fields="positions"))
    except Exception:
        log.exception("[spy-account/compare] Schwab MCP read failed")
        parsed = None
    if not parsed:
        return {"enabled": True, "connected": False}

    real = {p["symbol"]: p for p in parsed["positions"]}
    scan = db.get_latest_completed_spy_scan()
    paper: dict[str, dict[str, Any]] = {}
    paper_value = 0.0
    if scan:
        for a in scan.get("portfolio_json") or []:
            if a.get("action") == "EXITED":
                continue
            shares = a.get("shares") or 0
            if shares <= 0:
                continue
            val = a.get("current_value")
            if val is None:
                val = shares * (a.get("current_price") or a.get("entry_price") or 0)
            paper[a["ticker"]] = {"shares": shares, "value": float(val)}
            paper_value += float(val)

    rows = []
    for sym in sorted(set(real) | set(paper)):
        pp = paper.get(sym)
        rr = real.get(sym)
        rows.append({
            "ticker": sym,
            "paper_shares": pp["shares"] if pp else 0,
            "paper_value": round(pp["value"], 2) if pp else 0,
            "real_shares": rr["shares"] if rr else 0,
            "real_value": round(rr["market_value"], 2) if rr else 0,
            "in_paper": pp is not None,
            "in_real": rr is not None,
        })

    return {
        "connected": True,
        "paper_scan_id": scan.get("id") if scan else None,
        "paper_value": round(paper_value, 2),
        "real_positions_value": round(sum(p["market_value"] for p in parsed["positions"]), 2),
        "real_cash": round(parsed["cash"], 2),
        "real_total": round(parsed["liquidation_value"], 2),
        "rows": rows,
    }


# ---------- S&P 500 scan worker ----------

def _run_spy_scan_thread(scan_id: int, trade_date: str) -> None:
    """Thread entry: route ScanCancelled to status 'cancelled', anything else to 'failed'."""
    _refresh_creds_from_db()
    try:
        _run_spy_scan(scan_id, trade_date)
    except spy_scanner.ScanCancelled:
        # A user cancel is not a failure — don't alert.
        log.info("SPY scan %s cancelled by user", scan_id)
        db.update_spy_scan(scan_id, status="cancelled")
    except Exception as exc:
        log.exception("SPY scan %s crashed", scan_id)
        db.fail_spy_scan(scan_id, str(exc))
        alerts.notify_run_failed(
            kind="S&P 500 scan", run_id=scan_id, label=trade_date, error=str(exc)
        )


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
    """S&P scan worker: quick screen -> deep dives -> allocator.

    If the previous completed scan left active positions, its last refreshed
    value becomes this week's starting capital and the allocator runs in
    rebalance mode; otherwise it's a fresh $100k. The cancel flag is checked
    between phases here and per-ticker inside spy_scanner.
    """
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

    # Read aggressiveness and bias from the scan row (set at creation from the account).
    scan_row = db.get_spy_scan(scan_id) or {}
    aggressiveness = int(scan_row.get("aggressiveness") or 5)
    bias = scan_row.get("bias") or "neutral"
    paper_account_id = scan_row.get("paper_account_id")

    # Derive debate depth from aggressiveness (1–3→1 round, 4–7→2, 8–10→3).
    if aggressiveness <= 3:
        debate_rounds = 1
    elif aggressiveness <= 7:
        debate_rounds = 2
    else:
        debate_rounds = 3
    config["max_debate_rounds"] = debate_rounds
    config["max_risk_discuss_rounds"] = debate_rounds

    # Look up the previous completed scan for the same account to enable rebalancing.
    prev_scan = db.get_latest_completed_spy_scan(
        exclude_id=scan_id,
        paper_account_id=paper_account_id,
    )
    previous_portfolio: list[dict[str, Any]] | None = None
    previous_scan_id: int | None = None
    starting_value: float = float(
        (db.get_paper_account(paper_account_id) or {}).get("starting_capital") or 100_000.0
    ) if paper_account_id else 100_000.0

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
                )) or starting_value
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
        # Pathological quick scan (everything SELL or errored): deep-dive the
        # least-bad 50 anyway rather than abort the whole weekly run.
        top50 = sorted(quick_results, key=lambda r: -(r.get("conviction") or 0))[:50]
    with _phase("Deep-dive analysis failed"):
        enriched = spy_scanner.run_deep_dives(scan_id, top50, trade_date, config, selected_analysts)

    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    # Phase 3: allocator (rebalance if a previous portfolio exists, else fresh capital)
    db.update_spy_scan(scan_id, status="running_alloc")
    with _phase("Portfolio allocation failed"):
        alloc_result = spy_allocator.run(
            enriched,
            trade_date,
            config,
            previous_portfolio=previous_portfolio,
            starting_value=starting_value,
            aggressiveness=aggressiveness,
            bias=bias,
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

    # Mark the fresh portfolio to market immediately so the table shows live
    # share counts / current prices / P&L without waiting for the hourly cron.
    try:
        spy_scanner.refresh_portfolio_prices(scan_id)
    except Exception:
        log.exception("[spy %s] initial price refresh failed (non-fatal)", scan_id)

"""Daily options paper-trading engine.

Pipeline (run_options_build, behind POST /api/options-scan, cron Mon-Fri):
  settle expiries -> movers pre-screen (top PRESCREEN_TOP of the S&P 500) ->
  quick LLM scan (spy_scanner.run_quick_scan) -> full deep dive on the top
  DEEP_TOP directional names (BUY *and* SELL — puts need bearish candidates,
  unlike the equity scan's BUY/HOLD filter) -> market-open gate -> chain fetch +
  contract vetting (options_data) -> LLM allocator (options_allocator) ->
  apply decisions through db's transactional position/ledger helpers.

Options runs are spy_scans rows with kind='options', so progress counters,
cooperative cancel, and the stuck-run reaper all work unchanged.

Also owns the two standing maintenance passes:
  settle_expired    — idempotent expiry settlement (safety net; the DTE floor
                      force-close means it mostly fires after downtime),
  refresh_positions — mark open contracts to market (Schwab bulk quotes ->
                      yfinance chain fallback -> carry floored at intrinsic).

Paper only: nothing here (or anywhere) places real orders — the Schwab client
exposes market data and account reads exclusively.
"""
from __future__ import annotations

import logging
import time as time_mod
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf

from tradingagents.dataflows import schwab_mcp

from . import db, options_allocator, options_data, spy_scanner
from .runner import build_config
from .spy_tickers import get_sp500_tickers

log = logging.getLogger(__name__)

PRESCREEN_TOP = 150     # movers that get the cheap quick-LLM scan
DEEP_TOP = 25           # directional names that get the full agent graph
MARKET_OPEN_ET = (9, 35)  # allocation waits for live quotes on trading days
SETTLE_HOUR_ET = 17     # expiry-day positions settle only after this hour
STALE_ALERT_THRESHOLD = 3


@contextmanager
def _phase(label: str) -> Iterator[None]:
    """Tag failures with a phase prefix; cancellations pass through untouched."""
    try:
        yield
    except spy_scanner.ScanCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised with friendlier context
        raise RuntimeError(f"{label}: {exc}") from exc


# ── Pre-screen ───────────────────────────────────────────────────────────────

def _mover_score(closes: list[float], volumes: list[float]) -> float | None:
    """Direction-agnostic 'is something happening here' score: |5d| + half |20d|
    momentum plus a volume-surge kicker. Big losers rank too — they're put
    candidates."""
    if len(closes) < 5:
        return None
    ret5 = abs((closes[-1] / closes[-5]) - 1) * 100
    ret20 = abs((closes[-1] / closes[0]) - 1) * 100 if len(closes) >= 20 else 0.0
    vol_kick = 0.0
    if len(volumes) >= 20 and volumes[-1]:
        avg = sum(volumes[-20:-1]) / 19
        if avg:
            vol_kick = max(0.0, float(volumes[-1]) / avg - 1.0)
    return ret5 + 0.5 * ret20 + 3.0 * min(vol_kick, 3.0)


def prescreen(tickers: list[str], top_n: int = PRESCREEN_TOP) -> list[str]:
    """Rank the universe by mover score from one bulk download; top_n survive."""
    try:
        raw = yf.download(tickers, period="1mo", auto_adjust=True, progress=False, threads=True)
    except Exception as exc:
        raise RuntimeError(f"pre-screen bulk download failed: {exc}") from exc
    scored: list[tuple[float, str]] = []
    if raw is not None and not raw.empty:
        if hasattr(raw.columns, "levels"):
            for t in tickers:
                try:
                    closes = raw["Close"][t].dropna().tolist()
                    volumes = raw["Volume"][t].dropna().tolist()
                except (KeyError, TypeError):
                    continue
                s = _mover_score(closes, volumes)
                if s is not None:
                    scored.append((s, t))
        elif tickers:
            closes = raw["Close"].dropna().tolist()
            volumes = raw["Volume"].dropna().tolist()
            s = _mover_score(closes, volumes)
            if s is not None:
                scored.append((s, tickers[0]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, t in scored[:top_n]]


# ── Expiry settlement ────────────────────────────────────────────────────────

def is_settleable(expiration_date: str, now: datetime | None = None) -> bool:
    """True once the contract's last session is over: any day after expiry, or
    expiry day itself after SETTLE_HOUR_ET. Never intraday on expiry day."""
    now = now or options_data.now_et()
    try:
        exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    today = now.date()
    return today > exp or (today == exp and now.hour >= SETTLE_HOUR_ET)


def intrinsic_value(put_call: str, strike: float, underlying_close: float) -> float:
    if str(put_call).upper().startswith("C"):
        return max(0.0, float(underlying_close) - float(strike))
    return max(0.0, float(strike) - float(underlying_close))


def underlying_close_on_or_before(underlying: str, expiration_date: str) -> float | None:
    """Last available close on/before the expiration date (as-of pattern —
    covers holidays, half-days, and downtime catch-up without a market
    calendar)."""
    try:
        exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
        hist = yf.Ticker(underlying).history(
            start=(exp - timedelta(days=10)).isoformat(),
            end=(exp + timedelta(days=1)).isoformat(),
        )
        if hist is None or hist.empty:
            return None
        onto = hist[[d <= exp for d in hist.index.date]]
        if onto.empty:
            return None
        return float(onto["Close"].dropna().iloc[-1])
    except Exception:
        log.exception("[options] close lookup failed for %s @ %s", underlying, expiration_date)
        return None


def settle_expired(paper_account_id: int | None = None) -> dict[str, Any]:
    """Settle every open position whose expiry has passed. Idempotent (the
    status='open' guard in db.settle_options_position); safe to run from the
    nightly sweep, the start of every build, and the start of every refresh."""
    now = options_data.now_et()
    open_positions = db.list_options_positions(paper_account_id, status="open")
    due = [p for p in open_positions if is_settleable(p["expiration_date"], now)]
    settled = worthless = failed = 0
    close_cache: dict[tuple[str, str], float | None] = {}
    for pos in due:
        key = (pos["underlying"], pos["expiration_date"])
        if key not in close_cache:
            close_cache[key] = underlying_close_on_or_before(*key)
        close = close_cache[key]
        if close is None:
            n = db.bump_options_position_stale(pos["id"])
            if n >= STALE_ALERT_THRESHOLD:
                log.error(
                    "[options] cannot settle position %s (%s exp %s) after %d attempts — "
                    "no underlying close available",
                    pos["id"], pos["occ_symbol"], pos["expiration_date"], n,
                )
            failed += 1
            continue
        intr = intrinsic_value(pos["put_call"], pos["strike"], close)
        if db.settle_options_position(pos["id"], intr, close):
            if intr >= 0.01:
                settled += 1
            else:
                worthless += 1
            log.info("[options] settled %s at intrinsic $%.2f (close %.2f)",
                     pos["occ_symbol"], intr, close)
    return {"due": len(due), "settled_itm": settled,
            "expired_worthless": worthless, "failed": failed}


# ── Mark-to-market ───────────────────────────────────────────────────────────

def _yf_contract_price(pos: dict[str, Any], chain_cache: dict[tuple[str, str], Any]) -> float | None:
    """Price one contract off a yfinance chain (matched by strike + side —
    NOT contractSymbol, which yfinance formats unpadded)."""
    key = (pos["underlying"], pos["expiration_date"])
    if key not in chain_cache:
        try:
            chain_cache[key] = yf.Ticker(pos["underlying"]).option_chain(pos["expiration_date"])
        except Exception:
            log.debug("[options] yf chain fetch failed for %s %s", *key, exc_info=True)
            chain_cache[key] = None
    chain = chain_cache[key]
    if chain is None:
        return None
    frame = chain.calls if pos["put_call"].upper().startswith("C") else chain.puts
    if frame is None or getattr(frame, "empty", True):
        return None
    rows = frame[abs(frame["strike"] - float(pos["strike"])) < 0.001]
    if rows.empty:
        return None
    row = rows.iloc[0]
    bid, ask = row.get("bid"), row.get("ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask >= bid:
        return round((float(bid) + float(ask)) / 2, 4)
    last = row.get("lastPrice")
    if isinstance(last, (int, float)) and last > 0:
        return float(last)
    return None


def _underlying_prices(underlyings: list[str]) -> dict[str, float]:
    """Fresh underlying prices for the intrinsic floor on carried marks."""
    out: dict[str, float] = {}
    if not underlyings:
        return out
    if schwab_mcp.market_data_enabled():
        try:
            quotes = schwab_mcp.get_quotes(underlyings)
            if quotes:
                for u in underlyings:
                    p = schwab_mcp.quote_price(quotes.get(u, {}))
                    if p:
                        out[u] = p
        except Exception:
            log.debug("[options] underlying quotes via Schwab failed", exc_info=True)
    missing = [u for u in underlyings if u not in out]
    if missing:
        try:
            df = yf.download(missing, period="1d", auto_adjust=True, progress=False)
            if df is not None and not df.empty:
                if hasattr(df.columns, "levels"):
                    for u in missing:
                        try:
                            series = df["Close"][u].dropna()
                            if not series.empty:
                                out[u] = float(series.iloc[-1])
                        except (KeyError, TypeError):
                            continue
                else:
                    series = df["Close"].dropna()
                    if not series.empty:
                        out[missing[0]] = float(series.iloc[-1])
        except Exception:
            log.debug("[options] underlying prices via yfinance failed", exc_info=True)
    return out


def refresh_positions(paper_account_id: int | None = None) -> dict[str, Any]:
    """Settle due expiries, then mark every open position to market and roll
    the account equity onto its latest completed options scan row."""
    settle_summary = settle_expired(paper_account_id)
    positions = db.list_options_positions(paper_account_id, status="open")

    marked = 0
    priced: dict[int, tuple[float, str]] = {}
    if positions and schwab_mcp.market_data_enabled():
        try:
            quotes = schwab_mcp.get_quotes([p["occ_symbol"] for p in positions])
        except Exception:
            log.exception("[options] Schwab option quotes failed")
            quotes = None
        if quotes:
            for p in positions:
                price = schwab_mcp.option_quote_price(quotes.get(p["occ_symbol"], {}))
                if price:
                    priced[p["id"]] = (price, "schwab")

    chain_cache: dict[tuple[str, str], Any] = {}
    for p in positions:
        if p["id"] in priced:
            continue
        price = _yf_contract_price(p, chain_cache)
        if price:
            priced[p["id"]] = (price, "yfinance")

    # Carry-with-intrinsic-floor for anything still unpriced. Never mark to 0
    # on a missing quote.
    unpriced = [p for p in positions if p["id"] not in priced]
    spots = _underlying_prices(sorted({p["underlying"] for p in unpriced})) if unpriced else {}
    for p in unpriced:
        carried = float(p.get("current_premium") or p.get("entry_premium") or 0)
        spot = spots.get(p["underlying"])
        intr = intrinsic_value(p["put_call"], p["strike"], spot) if spot else 0.0
        price = max(carried, intr)
        if price <= 0:
            n = db.bump_options_position_stale(p["id"])
            if n >= STALE_ALERT_THRESHOLD:
                log.error("[options] no price for %s after %d refreshes", p["occ_symbol"], n)
            continue
        source = "intrinsic" if intr > carried else "carried"
        db.mark_options_position(p["id"], price, price * 100 * int(p["contracts"]),
                                 source, reset_stale=False)
        n = int(p.get("stale_count") or 0) + 1
        if n >= STALE_ALERT_THRESHOLD:
            log.warning("[options] %s marked '%s' %d refreshes in a row",
                        p["occ_symbol"], source, n)
        marked += 1

    for p in positions:
        got = priced.get(p["id"])
        if got:
            price, source = got
            db.mark_options_position(p["id"], price, price * 100 * int(p["contracts"]), source)
            marked += 1

    # Roll fresh equity onto each affected account's latest completed scan.
    accounts = ([db.get_paper_account(paper_account_id)] if paper_account_id
                else db.list_paper_accounts(kind="options"))
    account_values: dict[int, float] = {}
    for acct in accounts:
        if not acct:
            continue
        equity = account_equity(acct["id"])["equity"]
        account_values[acct["id"]] = equity
        latest = db.get_latest_completed_spy_scan(paper_account_id=acct["id"], kind="options")
        if latest:
            db.update_spy_scan(latest["id"], current_value=equity,
                               last_price_check=datetime.utcnow().isoformat(timespec="seconds") + "Z")
    return {"marked": marked, "open": len(positions),
            "settle": settle_summary, "account_values": account_values}


# ── Account math ─────────────────────────────────────────────────────────────

def account_equity(paper_account_id: int) -> dict[str, float]:
    cash = db.options_cash_balance(paper_account_id)
    open_positions = db.list_options_positions(paper_account_id, status="open")
    open_value = sum(float(p.get("current_value") or p.get("cost_basis") or 0)
                     for p in open_positions)
    deployed = sum(float(p.get("cost_basis") or 0) for p in open_positions)
    return {"cash": round(cash, 2), "open_value": round(open_value, 2),
            "deployed": round(deployed, 2), "equity": round(cash + open_value, 2)}


def account_summary(paper_account_id: int) -> dict[str, Any]:
    acct = db.get_paper_account(paper_account_id) or {}
    eq = account_equity(paper_account_id)
    realized = db.options_realized_pnl(paper_account_id)
    starting = float(acct.get("starting_capital") or 100_000.0)
    open_positions = db.list_options_positions(paper_account_id, status="open")
    settled = db.list_options_positions(paper_account_id, status="settled")
    funded = db.has_options_deposit(paper_account_id)
    if not funded:
        # The deposit ledger row lands on the first build; until then show the
        # account at its starting capital instead of a phantom -100% return.
        eq = {**eq, "cash": starting, "equity": starting}
    return {
        **eq,
        "realized_pnl": realized,
        "starting_capital": starting,
        "return_pct": round((eq["equity"] - starting) / starting * 100, 2) if starting else 0.0,
        "open_count": len(open_positions),
        "closed_count": len(settled),
        "funded": funded,
    }


# ── The daily build ──────────────────────────────────────────────────────────

def _wait_for_market_open(scan_id: int) -> None:
    """Block until MARKET_OPEN_ET on trading days so entries fill at live mids.
    Weekend manual runs proceed immediately (weekday check only). Heartbeats
    updated_at every few minutes so the stuck-run reaper doesn't mistake the
    wait for a crashed worker."""
    ticks = 0
    while True:
        now = options_data.now_et()
        if now.weekday() >= 5 or (now.hour, now.minute) >= MARKET_OPEN_ET:
            return
        if db.is_spy_scan_cancelled(scan_id):
            raise spy_scanner.ScanCancelled()
        if ticks % 6 == 0:  # every ~3 minutes
            db.update_spy_scan(scan_id, status="running_alloc")
        ticks += 1
        time_mod.sleep(30)


def _zero_candidate_reason(
    quick_results: list[dict[str, Any]],
    directional: list[dict[str, Any]],
    enriched: list[dict[str, Any]],
    usable: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str | None:
    """Explain a zero-candidate run, or None when there were candidates.

    A bare "0 new / 0 hold / 0 close" is indistinguishable from a broken run,
    which is exactly the confusion that made a fully-failed scan look like a
    quiet market. Counts are derived from each stage separately so the text can
    never misattribute a failure as "nothing passed vetting".

    Reachable only when the run was not TOTALLY broken — the guards in
    run_options_build fail the scan outright in that case.
    """
    if candidates:
        return None
    errored_quick = sum(1 for r in quick_results if r.get("error"))
    noise = f" ({errored_quick} of {len(quick_results)} quick scans errored)" if errored_quick else ""

    if not directional:
        return (f"No ticker in the movers pre-screen scored BUY or SELL today — "
                f"every name came back HOLD{noise}. Nothing to trade.")
    if not enriched:
        return f"{len(directional)} directional signals, but no deep dive ran{noise}."
    failed = len(enriched) - len(usable)
    if not usable:
        return (f"All {len(enriched)} deep dives failed{noise}. No contracts were vetted — "
                f"check the analysis history for the underlying error.")
    extra = f" ({failed} further dives failed and were skipped)" if failed else ""
    return (f"{len(usable)} of {len(enriched)} deep dives produced a usable signal, but no "
            f"contract passed liquidity/delta/DTE vetting{extra} — see the vetting notes below.")


def run_options_build(scan_id: int, trade_date: str) -> None:
    """Worker for one daily options run. Raises on failure (the endpoint's
    thread wrapper records failed/cancelled status, mirroring the equity scan)."""
    scan = db.get_spy_scan(scan_id) or {}
    account_id = scan.get("paper_account_id")
    if not account_id:
        raise RuntimeError("options scan requires a paper_account_id")
    account = db.get_paper_account(int(account_id))
    if not account:
        raise RuntimeError(f"paper account {account_id} not found")
    account_id = int(account_id)
    aggressiveness = int(scan.get("aggressiveness") or account.get("aggressiveness") or 5)
    bias = scan.get("bias") or account.get("bias") or "neutral"

    prefs = db.get_preferences() or {}
    selected_analysts = prefs.get("analysts") or ["market", "social", "news", "fundamentals"]
    config = build_config({**prefs, "aggressiveness": aggressiveness, "bias": bias})

    log.info("[options %s] starting for %s (account %s)", scan_id, trade_date, account_id)

    # Fund the account on first build; settle anything that expired since the
    # last run so carry-forward math never sees a dead position.
    if not db.has_options_deposit(account_id):
        db.append_options_cash(account_id, "deposit",
                               float(account.get("starting_capital") or 100_000.0),
                               scan_id=scan_id, note="initial deposit")
    with _phase("Expiry settlement failed"):
        settle_expired(account_id)

    # Phase 1: movers pre-screen + quick scan.
    with _phase("Couldn't fetch the S&P 500 ticker list"):
        universe = get_sp500_tickers()
    with _phase("Movers pre-screen failed"):
        movers = prescreen(universe, PRESCREEN_TOP)
    if not movers:
        raise RuntimeError("Movers pre-screen returned no tickers")
    with _phase("Quick scan failed"):
        quick_results = spy_scanner.run_quick_scan(scan_id, movers, config)
    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()
    # Wholesale failure must fail the run, not complete green with an empty
    # portfolio. Checked here rather than inside run_quick_scan because the
    # equity scan has its own deliberate degrade policy for a bad quick scan.
    spy_scanner.assert_quick_scan_healthy(quick_results)

    # Phase 2: deep dive the top directional names — BUY *and* SELL (puts need
    # bearish candidates; deliberately unlike the equity scan's BUY/HOLD cut).
    directional = [r for r in quick_results
                   if (r.get("signal") or "").upper() in ("BUY", "SELL")]
    top = sorted(directional, key=lambda r: -(r.get("conviction") or 0))[:DEEP_TOP]
    enriched: list[dict[str, Any]] = []
    if top:
        with _phase("Deep-dive analysis failed"):
            enriched = spy_scanner.run_deep_dives(scan_id, top, trade_date, config, selected_analysts)
        # Quick and deep resolve independent providers/models, so a deep-only
        # outage passes the quick guard above and lands here.
        spy_scanner.assert_deep_dives_healthy(enriched)
    else:
        log.info("[options %s] no directional quick-scan signals today", scan_id)
    if db.is_spy_scan_cancelled(scan_id):
        raise spy_scanner.ScanCancelled()

    # Phase 3: wait for live quotes, then vet contracts and allocate.
    db.update_spy_scan(scan_id, status="running_alloc")
    _wait_for_market_open(scan_id)
    with _phase("Position mark-to-market failed"):
        refresh_positions(account_id)

    # A FAILED deep dive still carries its quick-scan signal/conviction
    # (run_deep_dives returns {**candidate, "error": ...}), and fetch_candidates
    # filters only on signal/conviction — so without this, contracts get vetted
    # and real paper positions opened off analyses that crashed. Drop them.
    usable = [e for e in enriched if not e.get("error")]
    if len(usable) != len(enriched):
        log.warning("[options %s] dropping %d failed deep dives before vetting",
                    scan_id, len(enriched) - len(usable))
    with _phase("Chain fetch failed"):
        candidates, chain_notes = options_data.fetch_candidates(usable)
    log.info("[options %s] %d vetted candidates from %d usable deep dives (%d total)",
             scan_id, len(candidates), len(usable), len(enriched))

    open_positions = db.list_options_positions(account_id, status="open")
    eq = account_equity(account_id)
    realized = db.options_realized_pnl(account_id)
    starting_equity = eq["equity"]
    fresh_signals = {
        (r.get("ticker") or "").upper(): {"signal": (r.get("signal") or "").upper(),
                                          "conviction": r.get("conviction")}
        for r in quick_results if r.get("ticker")
    }

    with _phase("Options allocation failed"):
        alloc = options_allocator.run(
            candidates, open_positions, trade_date, config,
            equity=eq["equity"], cash=eq["cash"], realized_pnl=realized,
            aggressiveness=aggressiveness, bias=bias, fresh_signals=fresh_signals,
        )

    # Phase 4: apply decisions through the transactional helpers.
    decisions_log: list[dict[str, Any]] = []
    for c in alloc["closes"]:
        pos = db.get_options_position(int(c["position_id"])) or {}
        exit_premium = float(c.get("exit_premium") or pos.get("current_premium")
                             or pos.get("entry_premium") or 0)
        if db.close_options_position(int(c["position_id"]), exit_premium,
                                     c["exit_reason"], close_scan_id=scan_id):
            decisions_log.append({
                "occ_symbol": c["occ_symbol"], "action": "CLOSE",
                "exit_reason": c["exit_reason"], "exit_premium": exit_premium,
                "contracts": pos.get("contracts"), "rationale": c.get("rationale"),
            })
    skipped_opens: list[str] = []
    for o in alloc["opens"]:
        contract = o["contract"]
        cash_now = db.options_cash_balance(account_id)
        if o["cost"] > cash_now + 0.01:
            skipped_opens.append(f"{contract['occ_symbol']}: cost ${o['cost']:,.0f} > cash ${cash_now:,.0f}")
            continue
        db.open_options_position(account_id, scan_id, {
            "occ_symbol": contract["occ_symbol"],
            "underlying": contract["underlying"],
            "put_call": contract["put_call"],
            "strike": contract["strike"],
            "expiration_date": contract["expiration_date"],
            "contracts": o["contracts"],
            "entry_premium": contract["mid"],
            "entry_underlying": contract.get("underlying_price"),
            "entry_delta": contract.get("delta"),
            "entry_bid": contract.get("bid"),
            "entry_ask": contract.get("ask"),
            "entry_oi": contract.get("open_interest"),
            "signal": contract.get("signal"),
            "conviction": contract.get("conviction"),
            "rationale": o.get("rationale"),
            "data_source": contract.get("source"),
        })
        decisions_log.append({
            "occ_symbol": contract["occ_symbol"], "action": "NEW",
            "contracts": o["contracts"], "entry_premium": contract["mid"],
            "cost": o["cost"], "rationale": o.get("rationale"),
        })
    for h in alloc["holds"]:
        decisions_log.append({"occ_symbol": h["occ_symbol"], "action": "HOLD",
                              "rationale": h.get("rationale")})

    report = alloc["report_md"]
    reason = _zero_candidate_reason(quick_results, directional, enriched, usable, candidates)
    if reason:
        report += f"\n## Why no new positions\n{reason}\n"
    if skipped_opens:
        report += "\n## Skipped opens (cash)\n" + "\n".join(f"- {s}" for s in skipped_opens) + "\n"
    if chain_notes:
        report += ("\n## Contract vetting notes\n"
                   + "\n".join(f"- {n}" for n in chain_notes[:40]) + "\n")

    prev = db.get_latest_completed_spy_scan(exclude_id=scan_id,
                                            paper_account_id=account_id, kind="options")
    db.complete_spy_scan(
        scan_id=scan_id,
        allocator_report=report,
        portfolio_json=decisions_log,
        previous_scan_id=int(prev["id"]) if prev else None,
        starting_value=starting_equity,
    )

    final = account_equity(account_id)
    db.update_spy_scan(scan_id, current_value=final["equity"],
                       last_price_check=datetime.utcnow().isoformat(timespec="seconds") + "Z")
    if final["cash"] < -0.01:
        log.error("[options %s] LEDGER INVARIANT VIOLATION: cash $%.2f < 0 on account %s",
                  scan_id, final["cash"], account_id)
    log.info("[options %s] done — %d closes / %d opens / %d holds, equity $%s (cash $%s)",
             scan_id, len(alloc["closes"]), len(alloc["opens"]), len(alloc["holds"]),
             f"{final['equity']:,.0f}", f"{final['cash']:,.0f}")

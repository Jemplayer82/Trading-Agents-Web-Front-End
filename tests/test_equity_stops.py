"""Equity paper-account stop-loss enforcement for the S&P scanner."""

import pandas as pd
import pytest

from web import db, spy_scanner

pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


@pytest.fixture(autouse=True)
def _no_yfinance_network(monkeypatch):
    """Disable real yfinance downloads; tests inject prices via Schwab."""
    monkeypatch.setattr(spy_scanner.yf, "download", lambda *a, **k: pd.DataFrame())


def _account(name, stop_type="none", stop_value=None, stop_limit_offset=None):
    return db.create_paper_account(
        name,
        kind="equity",
        stop_type=stop_type,
        stop_value=stop_value,
        stop_limit_offset=stop_limit_offset,
    )


def _seed_scan(scan_id, starting_value, portfolio):
    with db.connect() as conn:
        conn.execute(
            "UPDATE spy_scans SET starting_value = ? WHERE id = ?",
            (starting_value, scan_id),
        )
        conn.execute(
            "UPDATE spy_scans SET status = 'completed' WHERE id = ?",
            (scan_id,),
        )
    db.update_spy_scan_prices(
        scan_id, current_value=0.0, rebalance_notes="", portfolio_json=portfolio
    )


def _refresh(scan_id, prices, monkeypatch):
    monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: True)
    monkeypatch.setattr(
        spy_scanner.schwab_mcp,
        "get_quotes",
        lambda ts: {t: {"last": prices[t]} for t in ts if t in prices},
    )
    monkeypatch.setattr(
        spy_scanner.schwab_mcp, "quote_price", lambda q: q.get("last")
    )
    return spy_scanner.refresh_portfolio_prices(scan_id)


def _load_portfolio(scan_id):
    return db.get_spy_scan(scan_id)["portfolio_json"]


def _mock_schwab_prices(monkeypatch, prices):
    monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: True)
    monkeypatch.setattr(
        spy_scanner.schwab_mcp,
        "get_quotes",
        lambda ts: {t: {"last": prices[t]} for t in ts if t in prices},
    )
    monkeypatch.setattr(
        spy_scanner.schwab_mcp, "quote_price", lambda q: q.get("last")
    )


def test_policy_none_and_null_account_no_stop(tmp_db, monkeypatch):
    aid = _account("acct-none", stop_type="none")
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    result = _refresh(scan_id, {"TICK": 70.0}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "BUY"
    assert "exit_proceeds" not in row
    assert result["stopped"] == 0
    assert result["cash"] == 9000.0

    scan_id2 = db.create_spy_scan("2026-08-10")
    portfolio2 = [
        {
            "ticker": "NULLT",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_id2, 10000, portfolio2)
    result2 = _refresh(scan_id2, {"NULLT": 70.0}, monkeypatch)
    row2 = _load_portfolio(scan_id2)[0]
    assert row2["action"] == "BUY"
    assert result2["cash"] == 9000.0


def test_hard_stop_filled_at_stop_level(tmp_db, monkeypatch):
    aid = _account("acct-stop", stop_type="stop", stop_value=20)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    result = _refresh(scan_id, {"TICK": 70.0}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "EXITED"
    assert row["exit_reason"] == "stop_loss"
    assert row["exit_price"] == 80.0
    assert row["exit_proceeds"] == 800.0
    assert row["current_value"] == 0.0
    assert result["stopped"] == 1


def test_hard_stop_gap_through_fills_at_mark(tmp_db, monkeypatch):
    aid = _account("acct-gap", stop_type="stop", stop_value=20)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 75.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    result = _refresh(scan_id, {"TICK": 70.0}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "EXITED"
    assert row["exit_reason"] == "stop_loss"
    assert row["exit_price"] == 70.0
    assert row["exit_proceeds"] == 700.0
    assert result["stopped"] == 1


def test_cash_reflects_realized_loss(tmp_db, monkeypatch):
    aid = _account("acct-realized", stop_type="stop", stop_value=20)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    result = _refresh(scan_id, {"TICK": 70.0}, monkeypatch)
    assert result["realized"] == -200.0
    # cash = basis - deployed + realized = 10000 - 0 + (800 - 1000)
    assert result["cash"] == 9800.0


def test_cash_persists_across_refreshes(tmp_db, monkeypatch):
    aid = _account("acct-persist", stop_type="stop", stop_value=20)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    r1 = _refresh(scan_id, {"TICK": 70.0}, monkeypatch)
    assert r1["cash"] == 9800.0
    assert r1["stopped"] == 1

    r2 = _refresh(scan_id, {"TICK": 65.0}, monkeypatch)
    portfolio = _load_portfolio(scan_id)
    assert portfolio[0]["exit_price"] == 80.0
    assert portfolio[0]["exit_proceeds"] == 800.0
    assert r2["cash"] == 9800.0
    assert r2["realized"] == -200.0
    assert r2["stopped"] == 0


def test_peak_price_seeded_and_ratchets(tmp_db, monkeypatch):
    aid = _account("acct-peak", stop_type="none")
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    _refresh(scan_id, {"TICK": 120.0}, monkeypatch)
    assert _load_portfolio(scan_id)[0]["peak_price"] == 120.0
    _refresh(scan_id, {"TICK": 90.0}, monkeypatch)
    assert _load_portfolio(scan_id)[0]["peak_price"] == 120.0
    assert _load_portfolio(scan_id)[0]["action"] == "BUY"
    _refresh(scan_id, {"TICK": 130.0}, monkeypatch)
    assert _load_portfolio(scan_id)[0]["peak_price"] == 130.0


def test_trailing_pct_stop(tmp_db, monkeypatch):
    aid = _account("acct-trailpct", stop_type="trailing_pct", stop_value=30)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "peak_price": 120.0,
            "current_price": 85.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    _refresh(scan_id, {"TICK": 85.0}, monkeypatch)
    assert _load_portfolio(scan_id)[0]["action"] == "BUY"
    _refresh(scan_id, {"TICK": 83.0}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "EXITED"
    assert row["exit_reason"] == "trail_stop"
    assert row["exit_price"] == 84.0


def test_trailing_dollar_stop(tmp_db, monkeypatch):
    aid = _account("acct-traildol", stop_type="trailing_dollar", stop_value=15)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "peak_price": 120.0,
            "current_price": 106.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    _refresh(scan_id, {"TICK": 106.0}, monkeypatch)
    assert _load_portfolio(scan_id)[0]["action"] == "BUY"
    _refresh(scan_id, {"TICK": 104.0}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "EXITED"
    assert row["exit_reason"] == "trail_stop"
    assert row["exit_price"] == 105.0


def test_stop_limit_variants(tmp_db, monkeypatch):
    # (a) gap below limit arms the stop-limit, no exit this refresh
    aid = _account("acct-sla", stop_type="stop_limit", stop_value=20, stop_limit_offset=5)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    r1 = _refresh(scan_id, {"TICK": 70.0}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "BUY"
    assert row.get("pending_stop_limit") is True
    assert row["stop_limit_price"] == 76.0
    assert "exit_proceeds" not in row
    assert r1["cash"] == 9000.0
    assert r1["deployed"] == 1000.0

    # (b) next refresh at 78 (armed) fills at the better of limit and market
    _refresh(scan_id, {"TICK": 78.0}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "EXITED"
    assert row["exit_reason"] == "stop_limit"
    assert row["exit_price"] == 78.0
    assert row["exit_proceeds"] == 780.0
    assert "pending_stop_limit" not in row
    assert "stop_limit_price" not in row

    # (c) armed, then a 74 refresh leaves it resting and live
    aid_c = _account(
        "acct-slc", stop_type="stop_limit", stop_value=20, stop_limit_offset=5
    )
    scan_c = db.create_spy_scan("2026-08-10", paper_account_id=aid_c)
    portfolio_c = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_c, 10000, portfolio_c)
    _refresh(scan_c, {"TICK": 70.0}, monkeypatch)
    assert _load_portfolio(scan_c)[0]["pending_stop_limit"] is True
    _refresh(scan_c, {"TICK": 74.0}, monkeypatch)
    row_c = _load_portfolio(scan_c)[0]
    assert row_c["action"] == "BUY"
    assert row_c.get("pending_stop_limit") is True
    assert "exit_proceeds" not in row_c

    # (d) non-gapping 77 exits immediately at the 80 trigger, never armed
    aid_d = _account(
        "acct-sld", stop_type="stop_limit", stop_value=20, stop_limit_offset=5
    )
    scan_d = db.create_spy_scan("2026-08-10", paper_account_id=aid_d)
    portfolio_d = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_d, 10000, portfolio_d)
    r_d = _refresh(scan_d, {"TICK": 77.0}, monkeypatch)
    row_d = _load_portfolio(scan_d)[0]
    assert row_d["action"] == "EXITED"
    assert row_d["exit_reason"] == "stop_limit"
    assert row_d["exit_price"] == 80.0
    assert row_d["exit_proceeds"] == 800.0
    assert "pending_stop_limit" not in row_d
    assert r_d["stopped"] == 1


def test_freshness_gate_no_quote_no_stop(tmp_db, monkeypatch):
    aid = _account("acct-fresh", stop_type="stop", stop_value=20)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 70.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    result = _refresh(scan_id, {}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "BUY"
    assert "exit_proceeds" not in row
    assert row["current_price"] == 70.0
    assert result["stopped"] == 0


def test_allocator_exited_row_ignored(tmp_db, monkeypatch):
    aid = _account("acct-alloc", stop_type="stop", stop_value=20)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "OLD",
            "action": "EXITED",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 70.0,
        },
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        },
    ]
    _seed_scan(scan_id, 10000, portfolio)
    result = _refresh(scan_id, {"OLD": 60.0, "TICK": 100.0}, monkeypatch)
    rows = _load_portfolio(scan_id)
    assert rows[0]["action"] == "EXITED"
    assert "exit_proceeds" not in rows[0]
    assert rows[0].get("exit_price") is None
    assert rows[0]["current_price"] == 70.0
    assert result["realized"] == 0.0
    assert rows[1]["action"] == "BUY"


def test_rebalance_notes_stopped_persists_and_flips_unchanged(tmp_db, monkeypatch):
    # Stop-out note is rebuilt from the row and survives a second refresh.
    aid = _account("acct-notes", stop_type="stop", stop_value=20)
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)
    r1 = _refresh(scan_id, {"TICK": 70.0}, monkeypatch)
    assert "Stopped out:" in r1["rebalance_notes"]
    assert "- TICK: stop_loss at $80.00" in r1["rebalance_notes"]

    r2 = _refresh(scan_id, {"TICK": 60.0}, monkeypatch)
    assert "Stopped out:" in r2["rebalance_notes"]
    assert "- TICK: stop_loss at $80.00" in r2["rebalance_notes"]
    assert r2["stopped"] == 0

    # Signal-flip-only notes must stay byte-identical to the pre-change output.
    aid2 = _account("acct-flips", stop_type="none")
    scan_id2 = db.create_spy_scan("2026-08-10", paper_account_id=aid2)
    portfolio2 = [
        {
            "ticker": "FLIP",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 100.0,
        }
    ]
    _seed_scan(scan_id2, 10000, portfolio2)
    db.upsert_spy_quick_result(scan_id2, "FLIP", "SELL", 5, "reason")
    r3 = _refresh(scan_id2, {"FLIP": 100.0}, monkeypatch)
    expected = "Signal flips detected:\n- FLIP: was BUY at entry, now SELL"
    assert r3["rebalance_notes"] == expected


def test_refresh_all_stops_every_account_not_just_the_newest_scan(
    tmp_db, monkeypatch, captured_alerts
):
    # Account A: created first -> lower scan id, but still must get its stop evaluated.
    aid_a = _account("acct-a-stop", stop_type="stop", stop_value=20)
    scan_a = db.create_spy_scan("2026-08-10", paper_account_id=aid_a)
    portfolio_a = [
        {
            "ticker": "TICK-A",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_a, 10000, portfolio_a)

    # Account B: created second -> higher scan id; the old db.latest_spy_scan()
    # would have selected only this one.
    aid_b = _account("acct-b-none", stop_type="none")
    scan_b = db.create_spy_scan("2026-08-10", paper_account_id=aid_b)
    portfolio_b = [
        {
            "ticker": "TICK-B",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_b, 10000, portfolio_b)

    _mock_schwab_prices(monkeypatch, {"TICK-A": 70.0, "TICK-B": 70.0})

    result = spy_scanner.refresh_all_portfolio_prices(kind="equity")

    row_a = _load_portfolio(scan_a)[0]
    row_b = _load_portfolio(scan_b)[0]

    # Account A's stop fired even though it is NOT the globally-latest scan.
    assert row_a["action"] == "EXITED"
    assert row_a["exit_reason"] == "stop_loss"

    # Account B has no stop but was still marked to market.
    assert row_b["action"] == "BUY"
    assert row_b["current_price"] == 70.0

    assert set(result["scans"].keys()) == {str(aid_a), str(aid_b)}
    assert captured_alerts == []


def test_refresh_all_portfolio_prices_exception_isolation_continues_to_other_accounts(
    tmp_db, monkeypatch, captured_alerts,
):
    # Account A: created first -> lower paper_account_id, so it is processed
    # first. Its per-account refresh will blow up.
    aid_a = _account("acct-a-fail", stop_type="none")
    scan_a = db.create_spy_scan("2026-08-10", paper_account_id=aid_a)
    portfolio_a = [
        {
            "ticker": "TICK-A",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_a, 10000, portfolio_a)

    # Account B: created second; has a hard stop that the mocked price will hit.
    aid_b = _account("acct-b-stop", stop_type="stop", stop_value=20)
    scan_b = db.create_spy_scan("2026-08-10", paper_account_id=aid_b)
    portfolio_b = [
        {
            "ticker": "TICK-B",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_b, 10000, portfolio_b)

    _mock_schwab_prices(monkeypatch, {"TICK-B": 70.0})

    original_refresh = spy_scanner.refresh_portfolio_prices

    def _patched_refresh(scan_id):
        if scan_id == scan_a:
            raise RuntimeError("simulated refresh failure for account A")
        return original_refresh(scan_id)

    monkeypatch.setattr(spy_scanner, "refresh_portfolio_prices", _patched_refresh)

    result = spy_scanner.refresh_all_portfolio_prices(kind="equity")

    # The loop did not raise; the failing account is recorded as an error.
    assert "error" in result["scans"][str(aid_a)]

    # The loop continued and evaluated account B's stop policy.
    row_b = _load_portfolio(scan_b)[0]
    assert row_b["action"] == "EXITED"
    assert row_b["exit_reason"] == "stop_loss"

    # Both accounts are represented in the result.
    assert set(result["scans"].keys()) == {str(aid_a), str(aid_b)}
    assert captured_alerts == []


def test_refresh_all_includes_no_account_scan(tmp_db, monkeypatch, captured_alerts):
    scan_id = db.create_spy_scan("2026-08-10")
    portfolio = [
        {
            "ticker": "NOACCT",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    _seed_scan(scan_id, 10000, portfolio)

    _mock_schwab_prices(monkeypatch, {"NOACCT": 90.0})

    result = spy_scanner.refresh_all_portfolio_prices(kind="equity")

    assert "unassigned" in result["scans"]
    assert _load_portfolio(scan_id)[0]["current_price"] == 90.0
    assert captured_alerts == []


@pytest.fixture(autouse=True)
def captured_alerts(monkeypatch):
    """Intercept spy_scanner's total-outage page so tests can assert fired/not-fired."""
    calls = []

    def _record(summary, detail="", *, link=None):
        calls.append((summary, detail, link))

    monkeypatch.setattr(spy_scanner.alerts, "notify", _record)
    return calls


def test_refresh_all_completed_empty_portfolios_alert_as_outage(tmp_db, captured_alerts):
    # A completed scan with an empty portfolio is no longer "no portfolio yet".
    # Because refresh_all_portfolio_prices only sees completed scans, both of
    # these empty accounts must return the real allocation-failure error and
    # together trigger the total-outage alert.
    aid_a = _account("acct-empty-a", stop_type="none")
    scan_a = db.create_spy_scan("2026-08-10", paper_account_id=aid_a)
    _seed_scan(scan_a, 10000, [])

    aid_b = _account("acct-empty-b", stop_type="none")
    scan_b = db.create_spy_scan("2026-08-10", paper_account_id=aid_b)
    _seed_scan(scan_b, 10000, [])

    result = spy_scanner.refresh_all_portfolio_prices(kind="equity")

    key_a = str(aid_a)
    key_b = str(aid_b)
    assert key_a in result["scans"]
    assert key_b in result["scans"]
    assert result["scans"][key_a]["error"] == "allocation produced no positions"
    assert result["scans"][key_b]["error"] == "allocation produced no positions"
    assert "skipped" not in result["scans"][key_a]
    assert "skipped" not in result["scans"][key_b]
    assert len(captured_alerts) == 1
    assert captured_alerts[0][0] == "All 2 equity account price refreshes failed"
    assert str(aid_a) in captured_alerts[0][1]
    assert str(aid_b) in captured_alerts[0][1]


def test_refresh_portfolio_prices_unallocated_scan_skips_before_completed(tmp_db):
    # The only legitimate "no portfolio yet" skip is a scan that has not yet
    # reached status 'completed'. Verify the skip shape directly through the
    # single-scan API before allocation has run.
    aid = _account("acct-pending", stop_type="none")
    scan_id = db.create_spy_scan("2026-08-10", paper_account_id=aid)

    result = spy_scanner.refresh_portfolio_prices(scan_id)
    assert result == {"skipped": "no portfolio yet"}
    assert "error" not in result


def test_refresh_all_every_account_erroring_still_alerts(tmp_db, captured_alerts, monkeypatch):
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]

    aid_a = _account("acct-err-a", stop_type="none")
    scan_a = db.create_spy_scan("2026-08-10", paper_account_id=aid_a)
    _seed_scan(scan_a, 10000, portfolio)

    aid_b = _account("acct-err-b", stop_type="none")
    scan_b = db.create_spy_scan("2026-08-10", paper_account_id=aid_b)
    _seed_scan(scan_b, 10000, portfolio)

    def _boom(scan_id):
        raise RuntimeError("simulated total outage")

    monkeypatch.setattr(spy_scanner, "refresh_portfolio_prices", _boom)

    result = spy_scanner.refresh_all_portfolio_prices(kind="equity")

    for entry in result["scans"].values():
        assert "error" in entry

    assert len(captured_alerts) == 1
    assert captured_alerts[0][0] == "All 2 equity account price refreshes failed"
    assert str(aid_a) in captured_alerts[0][1]
    assert str(aid_b) in captured_alerts[0][1]


def test_refresh_all_mixed_real_error_and_empty_completed_alert_as_outage(tmp_db, captured_alerts, monkeypatch):
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]

    aid_a = _account("acct-mix-a", stop_type="none")
    scan_a = db.create_spy_scan("2026-08-10", paper_account_id=aid_a)
    _seed_scan(scan_a, 10000, portfolio)

    aid_b = _account("acct-mix-b", stop_type="none")
    scan_b = db.create_spy_scan("2026-08-10", paper_account_id=aid_b)
    _seed_scan(scan_b, 10000, [])

    original_refresh = spy_scanner.refresh_portfolio_prices

    def _patched_refresh(scan_id):
        if scan_id == scan_a:
            raise RuntimeError("simulated refresh failure for account A")
        return original_refresh(scan_id)

    monkeypatch.setattr(spy_scanner, "refresh_portfolio_prices", _patched_refresh)

    result = spy_scanner.refresh_all_portfolio_prices(kind="equity")

    assert "error" in result["scans"][str(aid_a)]
    assert result["scans"][str(aid_b)]["error"] == "allocation produced no positions"
    assert "skipped" not in result["scans"][str(aid_b)]
    assert len(captured_alerts) == 1
    assert captured_alerts[0][0] == "All 2 equity account price refreshes failed"
    assert str(aid_a) in captured_alerts[0][1]
    assert str(aid_b) in captured_alerts[0][1]


# --- direct unit tests for the extracted pure helpers -----------------------


def test_apply_stops_hard_stop_fills_at_stop_level():
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    policy = spy_scanner.account_policy.StopPolicy("stop", 20)
    result = spy_scanner.apply_stops_and_value(
        portfolio, basis=10000.0, policy=policy, prices={"TICK": 70.0}
    )
    row = portfolio[0]
    assert row["action"] == "EXITED"
    assert row["exit_reason"] == "stop_loss"
    assert row["exit_price"] == 80.0
    assert row["exit_proceeds"] == 800.0
    assert row["current_value"] == 0.0
    assert result["stopped"] == 1


def test_apply_stops_gap_through_fills_at_mark():
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 75.0,
        }
    ]
    policy = spy_scanner.account_policy.StopPolicy("stop", 20)
    result = spy_scanner.apply_stops_and_value(
        portfolio, basis=10000.0, policy=policy, prices={"TICK": 70.0}
    )
    row = portfolio[0]
    assert row["action"] == "EXITED"
    assert row["exit_reason"] == "stop_loss"
    assert row["exit_price"] == 70.0
    assert row["exit_proceeds"] == 700.0
    assert result["stopped"] == 1


def test_apply_stops_absent_price_never_stops():
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 70.0,
        }
    ]
    policy = spy_scanner.account_policy.StopPolicy("stop", 20)
    result = spy_scanner.apply_stops_and_value(
        portfolio, basis=10000.0, policy=policy, prices={}
    )
    row = portfolio[0]
    assert row["action"] == "BUY"
    assert "exit_proceeds" not in row
    assert row["current_price"] == 70.0
    assert result["stopped"] == 0


def test_apply_stops_cash_equals_basis_minus_deployed_plus_realized():
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    policy = spy_scanner.account_policy.StopPolicy("stop", 20)
    result = spy_scanner.apply_stops_and_value(
        portfolio, basis=10000.0, policy=policy, prices={"TICK": 70.0}
    )
    assert result["realized"] == -200.0
    assert result["deployed"] == 0.0
    assert result["cash"] == 9800.0


def test_apply_stops_second_call_leaves_exited_row_and_cash_unchanged():
    portfolio = [
        {
            "ticker": "TICK",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 10,
            "cost_basis": 1000.0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
            "current_price": 95.0,
        }
    ]
    policy = spy_scanner.account_policy.StopPolicy("stop", 20)
    r1 = spy_scanner.apply_stops_and_value(
        portfolio, basis=10000.0, policy=policy, prices={"TICK": 70.0}
    )
    assert r1["cash"] == 9800.0
    assert r1["stopped"] == 1

    r2 = spy_scanner.apply_stops_and_value(
        portfolio, basis=10000.0, policy=policy, prices={"TICK": 65.0}
    )
    row = portfolio[0]
    assert row["action"] == "EXITED"
    assert row["exit_price"] == 80.0
    assert row["exit_proceeds"] == 800.0
    assert r2["cash"] == 9800.0
    assert r2["realized"] == -200.0
    assert r2["stopped"] == 0


def test_apply_stops_derives_shares_and_cost_basis_for_legacy_row():
    portfolio = [
        {
            "ticker": "LEG",
            "action": "BUY",
            "entry_price": 100.0,
            "dollar_amount": 950.0,
            "signal": "BUY",
        }
    ]
    policy = spy_scanner.account_policy.StopPolicy("none")
    result = spy_scanner.apply_stops_and_value(
        portfolio, basis=10000.0, policy=policy, prices={"LEG": 105.0}
    )
    row = portfolio[0]
    assert row["shares"] == 9
    assert row["cost_basis"] == 900.0
    assert row["current_price"] == 105.0
    assert row["current_value"] == 945.0
    assert result["deployed"] == 900.0
    assert result["cash"] == 9100.0


def test_apply_stops_zero_shares_row_is_skipped():
    portfolio = [
        {
            "ticker": "ZERO",
            "action": "BUY",
            "entry_price": 100.0,
            "shares": 0,
            "dollar_amount": 1000.0,
            "signal": "BUY",
        }
    ]
    policy = spy_scanner.account_policy.StopPolicy("none")
    result = spy_scanner.apply_stops_and_value(
        portfolio, basis=10000.0, policy=policy, prices={"ZERO": 50.0}
    )
    row = portfolio[0]
    assert row["shares"] == 0
    assert "cost_basis" not in row
    assert "current_value" not in row
    assert "current_price" not in row
    assert result["deployed"] == 0.0
    assert result["positions_value"] == 0.0
    assert result["cash"] == 10000.0
    assert result["stopped"] == 0


def test_format_rebalance_notes_empty_when_no_stops_or_flips():
    assert spy_scanner._format_rebalance_notes([], []) == ""


def test_format_rebalance_notes_stop_includes_entry_and_pct():
    portfolio = [
        {
            "ticker": "TICK",
            "exit_reason": "stop_loss",
            "exit_price": 80.0,
            "entry_price": 100.0,
        }
    ]
    result = spy_scanner._format_rebalance_notes(portfolio, [])
    expected = "Stopped out:\n- TICK: stop_loss at $80.00 (entry $100.00, -20.0%)"
    assert result == expected


def test_format_rebalance_notes_stops_and_flips_joined_with_blank_line():
    portfolio = [
        {
            "ticker": "TICK",
            "exit_reason": "stop_loss",
            "exit_price": 80.0,
            "entry_price": 100.0,
        }
    ]
    flips = ["FLIP: was BUY at entry, now SELL"]
    result = spy_scanner._format_rebalance_notes(portfolio, flips)
    expected = (
        "Stopped out:\n- TICK: stop_loss at $80.00 (entry $100.00, -20.0%)\n\n"
        "Signal flips detected:\n- FLIP: was BUY at entry, now SELL"
    )
    assert result == expected


# --- outage predicate ------------------------------------------------------


def test_outage_predicate_empty_dict_is_not_an_outage():
    assert spy_scanner.is_total_price_refresh_outage({}) is False


def test_outage_predicate_all_real_errors_is_an_outage():
    scans = {"1": {"error": "boom"}, "2": {"error": "scan not found"}}
    assert spy_scanner.is_total_price_refresh_outage(scans) is True


def test_outage_predicate_all_skips_is_not_an_outage():
    scans = {"1": {"skipped": "no portfolio yet"}, "2": {"skipped": "no portfolio yet"}}
    assert spy_scanner.is_total_price_refresh_outage(scans) is False


def test_outage_predicate_mixed_error_and_skip_is_an_outage():
    scans = {"1": {"error": "boom"}, "2": {"skipped": "no portfolio yet"}}
    assert spy_scanner.is_total_price_refresh_outage(scans) is True


def test_outage_predicate_mixed_error_and_success_is_not_an_outage():
    scans = {"1": {"error": "boom"}, "2": {"current_value": 10000.0, "cash": 0.0, "stopped": 0}}
    assert spy_scanner.is_total_price_refresh_outage(scans) is False


def test_outage_predicate_all_successes_is_not_an_outage():
    scans = {
        "1": {"current_value": 10000.0, "cash": 0.0, "stopped": 0},
        "2": {"current_value": 20000.0, "cash": 500.0, "stopped": 1},
    }
    assert spy_scanner.is_total_price_refresh_outage(scans) is False


def test_outage_predicate_single_error_account_is_an_outage():
    scans = {"unassigned": {"error": "boom"}}
    assert spy_scanner.is_total_price_refresh_outage(scans) is True


def test_outage_predicate_entry_with_both_keys_counts_as_an_error():
    scans = {"1": {"error": "boom", "skipped": "x"}}
    assert spy_scanner.is_total_price_refresh_outage(scans) is True


def test_refresh_portfolio_prices_missing_scan_is_a_real_error(tmp_db, monkeypatch):
    # Calling the real production path with a scan id that does not exist must
    # return {"error": "scan not found"} -- not a "skipped" key. This is the
    # regression class that produced the bug this branch fixed: an overloaded
    # return key silently reclassified as benign. A missing scan is an anomaly
    # worth counting toward the outage check.
    monkeypatch.setattr(spy_scanner.schwab_mcp, "market_data_enabled", lambda: True)
    monkeypatch.setattr(
        spy_scanner.schwab_mcp,
        "get_quotes",
        lambda ts: {},
    )
    result = spy_scanner.refresh_portfolio_prices(999999)
    assert result == {"error": "scan not found"}
    assert "skipped" not in result
    assert spy_scanner.is_total_price_refresh_outage({"999999": result}) is True

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

    # (b) next refresh at 78 (armed) fills at the 76 limit
    _refresh(scan_id, {"TICK": 78.0}, monkeypatch)
    row = _load_portfolio(scan_id)[0]
    assert row["action"] == "EXITED"
    assert row["exit_reason"] == "stop_limit"
    assert row["exit_price"] == 76.0
    assert row["exit_proceeds"] == 760.0
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
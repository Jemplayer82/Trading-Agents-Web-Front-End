"""Lifecycle tests for the options paper trader: DB migrations, the cash
ledger's transactional invariants, expiry settlement idempotency, and the
kind-scoped scan queries."""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from web import db, options_engine

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


@pytest.fixture()
def account_id(tmp_db):
    aid = db.create_paper_account("Options Test", starting_capital=100_000.0, kind="options")
    db.append_options_cash(aid, "deposit", 100_000.0, note="initial deposit")
    return aid


def _pos_dict(**over):
    base = {
        "occ_symbol": "AAPL  260821C00230000",
        "underlying": "AAPL", "put_call": "CALL", "strike": 230.0,
        "expiration_date": "2026-08-21", "contracts": 2, "entry_premium": 4.20,
        "entry_underlying": 232.0, "entry_delta": 0.45,
        "entry_bid": 4.10, "entry_ask": 4.30, "entry_oi": 1500,
        "signal": "BUY", "conviction": 8, "rationale": "test", "data_source": "schwab",
    }
    base.update(over)
    return base


# ── Migrations ───────────────────────────────────────────────────────────────

def test_init_db_idempotent(tmp_db):
    db.init_db()
    db.init_db()  # migrations must be re-runnable on every boot


def test_kind_migration_on_pre_kind_db(tmp_path, monkeypatch):
    """A database created before the kind columns gains them (default 'equity')."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE spy_scans (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE paper_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, starting_capital REAL NOT NULL DEFAULT 100000, "
        "aggressiveness INTEGER NOT NULL DEFAULT 5, bias TEXT NOT NULL DEFAULT 'neutral', "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO spy_scans (created_at, trade_date, status) VALUES ('x', '2026-07-01', 'completed')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    with db.connect() as c:
        row = c.execute("SELECT kind FROM spy_scans WHERE id = 1").fetchone()
        assert row["kind"] == "equity"
        cols = {r["name"] for r in c.execute("PRAGMA table_info(paper_accounts)")}
        assert "kind" in cols


# ── Kind-scoped scan queries ─────────────────────────────────────────────────

def test_scan_kind_scoping(tmp_db):
    eq = db.create_spy_scan("2026-07-17", kind="equity")
    opt = db.create_spy_scan("2026-07-17", kind="options")
    assert [s["id"] for s in db.list_spy_scans()] == [eq]
    assert [s["id"] for s in db.list_spy_scans(kind="options")] == [opt]
    assert db.latest_spy_scan()["id"] == eq
    assert db.latest_spy_scan(kind="options")["id"] == opt

    db.complete_spy_scan(eq, "r", [], starting_value=100_000)
    db.complete_spy_scan(opt, "r", [], starting_value=100_000)
    assert db.get_latest_completed_spy_scan()["id"] == eq
    assert db.get_latest_completed_spy_scan(kind="options")["id"] == opt

    # Clearing equity history must not touch options runs (and vice versa).
    assert db.delete_all_spy_scans(kind="equity") == 1
    assert [s["id"] for s in db.list_spy_scans(kind="options")] == [opt]


def test_paper_account_kind_filter(tmp_db):
    e = db.create_paper_account("Equity A", kind="equity")
    o = db.create_paper_account("Options A", kind="options")
    assert [a["id"] for a in db.list_paper_accounts(kind="equity")] == [e]
    assert [a["id"] for a in db.list_paper_accounts(kind="options")] == [o]
    assert {a["id"] for a in db.list_paper_accounts()} == {e, o}


# ── Ledger + position lifecycle ──────────────────────────────────────────────

def test_open_close_ledger_flow(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    # 2 contracts x $4.20 x 100 = $840 debited.
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 840)
    pos = db.get_options_position(pid)
    assert pos["status"] == "open"
    assert pos["cost_basis"] == pytest.approx(840)

    assert db.close_options_position(pid, 5.00, "llm_close", close_scan_id=scan)
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 840 + 1000)
    pos = db.get_options_position(pid)
    assert pos["status"] == "closed"
    assert pos["realized_pnl"] == pytest.approx(160)
    assert db.options_realized_pnl(account_id) == pytest.approx(160)

    # Closing again is a no-op (no double credit).
    assert not db.close_options_position(pid, 6.00, "llm_close")
    assert db.options_cash_balance(account_id) == pytest.approx(100_160)


def test_settlement_itm_and_idempotency(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    # ITM by $2.50 at expiry: proceeds 2.50 * 100 * 2 = $500.
    assert db.settle_options_position(pid, 2.50, settlement_close=232.50)
    pos = db.get_options_position(pid)
    assert pos["status"] == "expired_itm"
    assert pos["exit_value"] == pytest.approx(500)
    assert pos["realized_pnl"] == pytest.approx(500 - 840)
    assert pos["settlement_close"] == pytest.approx(232.50)
    cash_after = db.options_cash_balance(account_id)
    assert cash_after == pytest.approx(100_000 - 840 + 500)

    # Settling twice must not double-credit.
    assert not db.settle_options_position(pid, 2.50, settlement_close=232.50)
    assert db.options_cash_balance(account_id) == pytest.approx(cash_after)
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM options_cash_ledger WHERE position_id = ? AND kind = 'expire'",
            (pid,),
        ).fetchone()["n"]
    assert n == 1


def test_settlement_worthless(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    assert db.settle_options_position(pid, 0.004, settlement_close=229.99)
    pos = db.get_options_position(pid)
    assert pos["status"] == "expired_worthless"
    assert pos["exit_value"] == 0
    assert pos["realized_pnl"] == pytest.approx(-840)
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 840)


def test_equity_invariant_across_builds(account_id):
    """cash + open value stays consistent across two scans of activity."""
    scan1 = db.create_spy_scan("2026-07-16", paper_account_id=account_id, kind="options")
    p1 = db.open_options_position(account_id, scan1, _pos_dict())
    p2 = db.open_options_position(
        account_id, scan1,
        _pos_dict(occ_symbol="MSFT  260821P00420000", underlying="MSFT",
                  put_call="PUT", strike=420.0, entry_premium=6.00, contracts=1),
    )
    scan2 = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    db.close_options_position(p1, 5.50, "llm_close", close_scan_id=scan2)

    eq = options_engine.account_equity(account_id)
    open_positions = db.list_options_positions(account_id, status="open")
    assert [p["id"] for p in open_positions] == [p2]
    assert eq["cash"] == pytest.approx(100_000 - 840 - 600 + 1100)
    assert eq["equity"] == pytest.approx(eq["cash"] + eq["open_value"])
    assert db.options_realized_pnl(account_id) == pytest.approx(1100 - 840)

    summary = options_engine.account_summary(account_id)
    assert summary["open_count"] == 1
    assert summary["closed_count"] == 1
    assert summary["realized_pnl"] == pytest.approx(260)


def test_mark_options_position(account_id):
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict())
    db.mark_options_position(pid, 5.10, 1020.0, "schwab")
    pos = db.get_options_position(pid)
    assert pos["current_premium"] == pytest.approx(5.10)
    assert pos["current_value"] == pytest.approx(1020.0)
    assert pos["price_source"] == "schwab"
    assert pos["stale_count"] == 0
    # Marking does not touch cash.
    assert db.options_cash_balance(account_id) == pytest.approx(100_000 - 840)


# ── Engine settlement rules ──────────────────────────────────────────────────

def test_is_settleable_rules():
    exp = "2026-07-17"
    before_close = datetime(2026, 7, 17, 14, 0)
    after_close = datetime(2026, 7, 17, 17, 5)
    next_day = datetime(2026, 7, 18, 8, 0)
    prior_day = datetime(2026, 7, 16, 23, 0)
    assert not options_engine.is_settleable(exp, before_close)
    assert options_engine.is_settleable(exp, after_close)
    assert options_engine.is_settleable(exp, next_day)
    assert not options_engine.is_settleable(exp, prior_day)
    assert not options_engine.is_settleable("garbage", next_day)


def test_intrinsic_value():
    assert options_engine.intrinsic_value("CALL", 230.0, 232.5) == pytest.approx(2.5)
    assert options_engine.intrinsic_value("CALL", 230.0, 225.0) == 0.0
    assert options_engine.intrinsic_value("PUT", 230.0, 225.0) == pytest.approx(5.0)
    assert options_engine.intrinsic_value("PUT", 230.0, 232.5) == 0.0


def test_settle_expired_sweep(account_id, monkeypatch):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    due = db.open_options_position(
        account_id, scan, _pos_dict(expiration_date=yesterday))
    live = db.open_options_position(
        account_id, scan,
        _pos_dict(occ_symbol="MSFT  270115C00420000", underlying="MSFT",
                  expiration_date=(date.today() + timedelta(days=180)).isoformat()))
    monkeypatch.setattr(options_engine, "underlying_close_on_or_before",
                        lambda u, e: 232.5)
    summary = options_engine.settle_expired(account_id)
    assert summary["due"] == 1
    assert summary["settled_itm"] == 1
    assert db.get_options_position(due)["status"] == "expired_itm"
    assert db.get_options_position(live)["status"] == "open"

    # Second sweep finds nothing (idempotent end to end).
    summary2 = options_engine.settle_expired(account_id)
    assert summary2["due"] == 0


def test_settle_expired_missing_close_leaves_open(account_id, monkeypatch):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    scan = db.create_spy_scan("2026-07-17", paper_account_id=account_id, kind="options")
    pid = db.open_options_position(account_id, scan, _pos_dict(expiration_date=yesterday))
    monkeypatch.setattr(options_engine, "underlying_close_on_or_before", lambda u, e: None)
    summary = options_engine.settle_expired(account_id)
    assert summary["failed"] == 1
    pos = db.get_options_position(pid)
    assert pos["status"] == "open"       # never guess a settlement price
    assert pos["stale_count"] == 1


def test_dequeue_dispatches_options_rows_to_options_thread(tmp_db, monkeypatch):
    """A queued kind='options' spy_scans row must start the options build, not
    the equity pipeline (the queue predates the kind column)."""
    import threading

    from web import portfolio_main

    started: dict[str, int] = {}
    done = threading.Event()

    def _rec(name):
        def _target(scan_id, trade_date):
            started[name] = scan_id
            done.set()
        return _target

    monkeypatch.setattr(portfolio_main, "_run_options_scan_thread", _rec("options"))
    monkeypatch.setattr(portfolio_main, "_run_spy_scan_thread", _rec("equity"))
    monkeypatch.setattr(portfolio_main, "_run_scan_thread", _rec("portfolio"))

    acct = db.create_paper_account("Q Opt", kind="options")
    opt = db.create_spy_scan("2026-07-17", paper_account_id=acct,
                             status="queued", kind="options")
    eq = db.create_spy_scan("2026-07-17", status="queued", kind="equity")
    with db.connect() as conn:  # make the options row strictly older
        conn.execute("UPDATE spy_scans SET created_at = '2026-07-17T00:00:00Z' WHERE id = ?", (opt,))
        conn.execute("UPDATE spy_scans SET created_at = '2026-07-17T00:00:01Z' WHERE id = ?", (eq,))

    portfolio_main._dequeue_next_scan()
    assert done.wait(5)
    assert started == {"options": opt}
    with db.connect() as conn:
        st = conn.execute("SELECT status FROM spy_scans WHERE id = ?", (opt,)).fetchone()["status"]
    assert st == "running_quick"

    # Simulate that run finishing; next dequeue starts the equity row.
    db.update_spy_scan(opt, status="completed")
    done.clear()
    portfolio_main._dequeue_next_scan()
    assert done.wait(5)
    assert started["equity"] == eq


def test_pending_counts_as_busy(tmp_db):
    from web import portfolio_main

    db.create_spy_scan("2026-07-17", kind="options")  # status 'pending'
    with db.connect() as conn:
        busy = portfolio_main._is_any_scan_running(conn)
    assert busy is not None and busy["scan_type"] == "spy"


def test_mover_score_direction_agnostic():
    closes_up = [100 + i for i in range(21)]
    closes_down = [100 - i * 0.8 for i in range(21)]
    flat = [100.0] * 21
    vols = [1_000_000] * 21
    up = options_engine._mover_score(closes_up, vols)
    down = options_engine._mover_score(closes_down, vols)
    quiet = options_engine._mover_score(flat, vols)
    assert up > quiet and down > quiet   # losers are put candidates, not noise
    assert options_engine._mover_score([100, 101], vols) is None  # too short

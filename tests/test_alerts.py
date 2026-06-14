"""Tests for run-failure alerts (web/alerts.py) and the stuck-run reaper.

No network / SMTP: the webhook + email channels are monkeypatched, and the reaper
runs against a temp SQLite DB (monkeypatch db.DB_PATH, like test_portfolio_progress).
"""
from __future__ import annotations

import sqlite3

import pytest

from web import alerts, db, newsletter, scheduler

# Pre-updated_at schemas, for the migration test.
_OLD_PORTFOLIO_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL
);
"""
_OLD_SPY_DDL = """
CREATE TABLE IF NOT EXISTS spy_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, trade_date TEXT NOT NULL, status TEXT NOT NULL
);
"""


def _set_ts(table: str, run_id: int, *, updated_at=None, created_at=None) -> None:
    with db.connect() as conn:
        if updated_at is not None:
            conn.execute(f"UPDATE {table} SET updated_at=? WHERE id=?", (updated_at, run_id))
        if created_at is not None:
            conn.execute(f"UPDATE {table} SET created_at=? WHERE id=?", (created_at, run_id))


class _RecordingNotifier:
    def __init__(self):
        self.calls = []

    def send(self, text, link=None):
        self.calls.append((text, link))


# ---------------------------------------------------------------------------
# notify_run_failed — both channels, never raises
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNotifyRunFailed:
    def test_fires_both_channels(self, monkeypatch):
        notifier = _RecordingNotifier()
        emails = []
        monkeypatch.setattr(alerts, "default_notifier", lambda: notifier)
        monkeypatch.setattr(newsletter, "send_alert", lambda subject, html: emails.append((subject, html)))

        alerts.notify_run_failed(
            kind="Portfolio scan", run_id=7, label="2026-01-02", error="boom"
        ).join(timeout=5)

        assert len(notifier.calls) == 1
        text, _link = notifier.calls[0]
        assert "Portfolio scan #7 failed: 2026-01-02" in text
        assert "boom" in text
        assert len(emails) == 1
        assert "Portfolio scan #7" in emails[0][0]

    def test_one_channel_failing_doesnt_block_the_other(self, monkeypatch):
        class Boom:
            def send(self, text, link=None):
                raise RuntimeError("webhook down")

        emails = []
        monkeypatch.setattr(alerts, "default_notifier", lambda: Boom())
        monkeypatch.setattr(newsletter, "send_alert", lambda s, h: emails.append(s))

        alerts.notify_run_failed(kind="Analysis", run_id=1, label="AAPL", error="x").join(timeout=5)

        assert len(emails) == 1  # email still fired despite the webhook crash

    def test_never_raises_even_if_both_fail(self, monkeypatch):
        def boom_notifier():
            raise RuntimeError("no notifier")

        def boom_email(s, h):
            raise RuntimeError("smtp down")

        monkeypatch.setattr(alerts, "default_notifier", boom_notifier)
        monkeypatch.setattr(newsletter, "send_alert", boom_email)
        # Must complete without propagating an exception.
        alerts.notify_run_failed(kind="Analysis", run_id=2, label="MSFT", error="x").join(timeout=5)


# ---------------------------------------------------------------------------
# newsletter.send_alert — email transport
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSendAlert:
    def test_noop_when_smtp_unset(self, monkeypatch):
        for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "NEWSLETTER_TO"):
            monkeypatch.delenv(var, raising=False)
        assert newsletter.send_alert("subj", "<p>hi</p>") is None

    def test_smtp_send_invoked_when_configured(self, monkeypatch):
        sent: dict = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=30):
                sent["host"] = host

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                pass

            def login(self, u, p):
                sent["login"] = (u, p)

            def send_message(self, msg):
                sent["subject"] = msg["Subject"]

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_PORT", "587")
        monkeypatch.setenv("SMTP_USER", "u@example.com")
        monkeypatch.setenv("SMTP_PASS", "pw")
        monkeypatch.setenv("NEWSLETTER_TO", "to@example.com")
        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)

        mid = newsletter.send_alert("Alert: scan failed", "<p>boom</p>")
        assert mid is not None
        assert sent["subject"] == "Alert: scan failed"
        assert sent["host"] == "smtp.example.com"


# ---------------------------------------------------------------------------
# Reaper queries — find_stuck_*
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFindStuck:
    def test_portfolio_stall_detection(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        stale = db.create_portfolio_scan("2026-01-01")
        fresh = db.create_portfolio_scan("2026-01-01")
        db.update_portfolio_scan(fresh, scan_total=5)  # stamps updated_at = now
        _set_ts("portfolio_scans", stale, updated_at="2020-01-01T00:00:00Z")

        ids = [s["id"] for s in db.find_stuck_portfolio_scans("2025-01-01T00:00:00Z")]
        assert stale in ids
        assert fresh not in ids

    def test_portfolio_premigration_row_uses_created_at(self, monkeypatch, tmp_path):
        # A scan that died before its first progress write has updated_at NULL;
        # COALESCE(updated_at, created_at) must fall back to created_at.
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        sid = db.create_portfolio_scan("2026-01-01")  # updated_at stays NULL
        _set_ts("portfolio_scans", sid, created_at="2020-01-01T00:00:00Z")

        ids = [s["id"] for s in db.find_stuck_portfolio_scans("2025-01-01T00:00:00Z")]
        assert sid in ids

    def test_spy_excludes_terminal_states(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        running = db.create_spy_scan("2026-01-01")  # status 'pending'
        done = db.create_spy_scan("2026-01-01")
        db.update_spy_scan(done, status="completed")
        _set_ts("spy_scans", running, updated_at="2020-01-01T00:00:00Z")
        _set_ts("spy_scans", done, updated_at="2020-01-01T00:00:00Z")

        ids = [s["id"] for s in db.find_stuck_spy_scans("2025-01-01T00:00:00Z")]
        assert running in ids   # 'pending' past the cutoff is stuck
        assert done not in ids  # completed is terminal — never reaped

    def test_analysis_age_detection(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        aid = db.create_analysis({"ticker": "AAPL", "trade_date": "2026-01-01"})
        _set_ts("analyses", aid, created_at="2020-01-01T00:00:00Z")

        ids = [a["id"] for a in db.find_stuck_analyses("2025-01-01T00:00:00Z")]
        assert aid in ids


# ---------------------------------------------------------------------------
# Reaper job — marks failed + alerts once per stuck run, leaves healthy ones
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReaperJob:
    def test_marks_failed_and_alerts(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        stuck = db.create_portfolio_scan("2026-01-02")
        _set_ts("portfolio_scans", stuck, updated_at="2020-01-01T00:00:00Z")
        healthy = db.create_portfolio_scan("2026-01-02")
        db.update_portfolio_scan(healthy, scan_total=3)  # updated_at = now

        notified: list = []
        monkeypatch.setattr(alerts, "notify_run_failed", lambda **kw: notified.append(kw))

        scheduler.job_reap_stuck_runs()

        assert db.get_portfolio_scan(stuck)["status"] == "failed"
        assert db.get_portfolio_scan(healthy)["status"] == "running"
        hits = [(n["kind"], n["run_id"]) for n in notified]
        assert ("Portfolio scan", stuck) in hits
        assert all(rid != healthy for _, rid in hits)


# ---------------------------------------------------------------------------
# updated_at migration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUpdatedAtMigration:
    def test_idempotent_add_to_old_schema(self, monkeypatch, tmp_path):
        dbfile = tmp_path / "web.db"
        conn = sqlite3.connect(dbfile)
        conn.executescript(_OLD_PORTFOLIO_DDL + _OLD_SPY_DDL)
        conn.commit()
        conn.close()

        monkeypatch.setattr(db, "DB_PATH", dbfile)
        db.init_db()  # adds updated_at via _COLUMN_MIGRATIONS
        db.init_db()  # idempotent — second boot must not error

        with db.connect() as c:
            pcols = {r["name"] for r in c.execute("PRAGMA table_info(portfolio_scans)")}
            scols = {r["name"] for r in c.execute("PRAGMA table_info(spy_scans)")}
        assert "updated_at" in pcols
        assert "updated_at" in scols

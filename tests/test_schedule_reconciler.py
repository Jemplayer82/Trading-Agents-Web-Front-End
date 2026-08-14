"""Unit tests for the per-account scheduler reconciler and nightly time resolver.

These tests intentionally import only pytest, APScheduler, web.db and web.scheduler
so they stay tier-agnostic and need no manifest entry.
"""
import itertools

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import web.db as db
import web.scheduler as scheduler

pytestmark = pytest.mark.unit

_account_names = itertools.count(1)


def _features_from_env():
    """Feature toggle stub that reads the FEATURES env var dynamically."""
    def _enabled(feature):
        import os
        return feature in os.environ.get("FEATURES", "").split(",")
    return _enabled


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Use an isolated sqlite DB and enable all relevant features by default."""
    monkeypatch.setenv("FEATURES", "schwab,sp500,options")
    monkeypatch.setattr(scheduler.features, "enabled", _features_from_env())
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "schedule_test.db")
    db.init_db()
    yield


def _create_account(kind, schedule_time=None):
    """Create a paper account with a name unique within the test process
    (multiple accounts of the same kind in one test would otherwise collide
    on paper_accounts.name, which is UNIQUE)."""
    name = f"{kind} test {next(_account_names)}"
    aid = db.create_paper_account(name=name, kind=kind)
    if schedule_time is not None:
        db.update_paper_account(aid, schedule_time=schedule_time)
    return aid


def _delete_account(aid):
    """Delete a paper account, tolerating either helper name or direct SQL."""
    delete = getattr(db, "delete_paper_account", None)
    if delete is not None:
        try:
            delete(aid)
            return
        except Exception:
            pass
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("DELETE FROM paper_accounts WHERE id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()


# --- nightly_scan_time() ---

def test_nightly_scan_time_default(tmp_db):
    assert scheduler.nightly_scan_time() == (22, 0)


def test_nightly_scan_time_reads_db(tmp_db):
    db.set_app_setting(scheduler.NIGHTLY_SCAN_TIME_SETTING, "06:15")
    assert scheduler.nightly_scan_time() == (6, 15)


def test_nightly_scan_time_prefers_db_over_env(tmp_db, monkeypatch):
    db.set_app_setting(scheduler.NIGHTLY_SCAN_TIME_SETTING, "06:15")
    monkeypatch.setenv(scheduler.NIGHTLY_SCAN_TIME_SETTING, "07:30")
    assert scheduler.nightly_scan_time() == (6, 15)


def test_nightly_scan_time_malformed_stored_returns_default(tmp_db):
    db.set_app_setting(scheduler.NIGHTLY_SCAN_TIME_SETTING, "25:99")
    assert scheduler.nightly_scan_time() == (22, 0)


# --- per-account reconciler ---

def test_reconcile_creates_equity_job(tmp_db):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    aid = _create_account("equity", "09:30")
    scheduler.job_reconcile_schedules(sched)
    job = sched.get_job(f"spy_scan_acct_{aid}")
    assert job is not None
    assert job.func is scheduler.job_spy_scan_account
    assert job.args == (aid,)  # APScheduler stores args as a tuple
    ts = str(job.trigger)
    assert "day_of_week='sat'" in ts
    assert "hour='9'" in ts
    assert "minute='30'" in ts


def test_reconcile_creates_options_job(tmp_db):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    aid = _create_account("options", "07:45")
    scheduler.job_reconcile_schedules(sched)
    job = sched.get_job(f"options_scan_acct_{aid}")
    assert job is not None
    assert job.func is scheduler.job_options_scan_account
    assert job.args == (aid,)  # APScheduler stores args as a tuple
    ts = str(job.trigger)
    assert "day_of_week='mon-fri'" in ts
    assert "hour='7'" in ts
    assert "minute='45'" in ts


def test_reconcile_skips_null_schedule_time(tmp_db):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    aid = _create_account("equity")
    scheduler.job_reconcile_schedules(sched)
    assert sched.get_job(f"spy_scan_acct_{aid}") is None


def test_reconcile_updates_existing_job(tmp_db):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    aid = _create_account("equity", "09:30")
    scheduler.job_reconcile_schedules(sched)
    job1 = sched.get_job(f"spy_scan_acct_{aid}")
    db.update_paper_account(aid, schedule_time="10:45")
    scheduler.job_reconcile_schedules(sched)
    job2 = sched.get_job(f"spy_scan_acct_{aid}")
    assert job2 is not None
    assert job1.id == job2.id
    assert str(job2.trigger) != str(job1.trigger)
    ts = str(job2.trigger)
    assert "hour='10'" in ts
    assert "minute='45'" in ts


def test_reconcile_twice_no_duplicate(tmp_db, monkeypatch):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    _create_account("equity", "09:30")
    scheduler.job_reconcile_schedules(sched)
    assert len(sched.get_jobs()) == 1

    original_add_job = sched.add_job
    calls = []

    def counting_add_job(*args, **kwargs):
        calls.append((args, kwargs))
        return original_add_job(*args, **kwargs)

    monkeypatch.setattr(sched, "add_job", counting_add_job)
    scheduler.job_reconcile_schedules(sched)
    assert len(calls) == 0
    assert len(sched.get_jobs()) == 1


def test_reconcile_removes_job_when_schedule_cleared(tmp_db):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    aid = _create_account("equity", "09:30")
    scheduler.job_reconcile_schedules(sched)
    assert sched.get_job(f"spy_scan_acct_{aid}") is not None
    db.update_paper_account(aid, schedule_time=None)
    scheduler.job_reconcile_schedules(sched)
    assert sched.get_job(f"spy_scan_acct_{aid}") is None


def test_reconcile_removes_job_when_account_deleted(tmp_db):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    aid = _create_account("equity", "09:30")
    scheduler.job_reconcile_schedules(sched)
    assert sched.get_job(f"spy_scan_acct_{aid}") is not None
    _delete_account(aid)
    scheduler.job_reconcile_schedules(sched)
    assert sched.get_job(f"spy_scan_acct_{aid}") is None


def test_reconcile_malformed_schedule_does_not_block_others(tmp_db):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    bad_id = _create_account("equity", "25:99")
    good_id = _create_account("equity", "08:00")
    scheduler.job_reconcile_schedules(sched)
    assert sched.get_job(f"spy_scan_acct_{bad_id}") is None
    assert sched.get_job(f"spy_scan_acct_{good_id}") is not None
    assert len(sched.get_jobs()) == 1


def test_reconcile_preserves_unrelated_jobs(tmp_db):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    sched.add_job(
        lambda: None,
        CronTrigger(day_of_week="mon", hour=1, minute=0, timezone=scheduler.TIMEZONE),
        id="reap_stuck_runs",
    )
    sched.add_job(
        lambda: None,
        CronTrigger(day_of_week="tue", hour=2, minute=0, timezone=scheduler.TIMEZONE),
        id="options_refresh",
    )
    aid = _create_account("equity", "09:30")
    scheduler.job_reconcile_schedules(sched)
    assert sched.get_job("reap_stuck_runs") is not None
    assert sched.get_job("options_refresh") is not None
    assert sched.get_job(f"spy_scan_acct_{aid}") is not None
    assert len(sched.get_jobs()) == 3


def test_reconcile_skips_equity_when_sp500_disabled(tmp_db, monkeypatch):
    monkeypatch.setenv("FEATURES", "schwab")
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    aid = _create_account("equity", "09:30")
    scheduler.job_reconcile_schedules(sched)
    assert sched.get_job(f"spy_scan_acct_{aid}") is None


def test_reconcile_survives_db_list_failure(tmp_db, monkeypatch):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)

    def boom(kind=None):
        raise Exception("DB locked")

    monkeypatch.setattr(db, "list_paper_accounts", boom)
    scheduler.job_reconcile_schedules(sched)
    assert len(sched.get_jobs()) == 0


# --- per-account scan job direct invocation ---

class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_job_spy_scan_account_posts_to_portfolio_url(monkeypatch):
    account_id = 4242
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse(200, "ok")

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(scheduler.alerts, "notify", lambda *args, **kwargs: None)

    scheduler.job_spy_scan_account(account_id)

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == f"{scheduler.PORTFOLIO_URL}/api/spy-scan"
    assert kwargs.get("json") == {"account_id": account_id}
    assert kwargs.get("timeout") == 60
    assert isinstance(kwargs.get("headers"), dict)


def test_job_options_scan_account_posts_to_portfolio_url(monkeypatch):
    account_id = 4243
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse(200, "ok")

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(scheduler.alerts, "notify", lambda *args, **kwargs: None)

    scheduler.job_options_scan_account(account_id)

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == f"{scheduler.PORTFOLIO_URL}/api/options-scan"
    assert kwargs.get("json") == {"account_id": account_id}
    assert kwargs.get("timeout") == 60
    assert isinstance(kwargs.get("headers"), dict)


def test_job_spy_scan_account_notifies_on_5xx(monkeypatch):
    account_id = 4244
    error_text = "database timeout in spy scan queue"

    def fake_post(_url, **_kwargs):
        return _FakeResponse(500, error_text)

    notify_calls = []

    def fake_notify(*args, **kwargs):
        notify_calls.append((args, kwargs))

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(scheduler.alerts, "notify", fake_notify)

    scheduler.job_spy_scan_account(account_id)

    assert len(notify_calls) == 1
    summary, detail = notify_calls[0][0]
    assert str(account_id) in summary
    assert "500" in summary
    assert detail == error_text[: scheduler._ALERT_DETAIL_MAX]
    assert notify_calls[0][1].get("link") == scheduler.DASHBOARD_URL


def test_job_options_scan_account_notifies_on_5xx(monkeypatch):
    account_id = 4245
    error_text = "options chain provider unavailable"

    def fake_post(_url, **_kwargs):
        return _FakeResponse(502, error_text)

    notify_calls = []

    def fake_notify(*args, **kwargs):
        notify_calls.append((args, kwargs))

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr(scheduler.alerts, "notify", fake_notify)

    scheduler.job_options_scan_account(account_id)

    assert len(notify_calls) == 1
    summary, detail = notify_calls[0][0]
    assert str(account_id) in summary
    assert "502" in summary
    assert detail == error_text[: scheduler._ALERT_DETAIL_MAX]
    assert notify_calls[0][1].get("link") == scheduler.DASHBOARD_URL


def test_job_spy_scan_account_does_not_notify_on_2xx(monkeypatch):
    account_id = 4246
    notify_calls = []

    monkeypatch.setattr("httpx.post", lambda *_a, **_k: _FakeResponse(200, "queued"))
    monkeypatch.setattr(
        scheduler.alerts,
        "notify",
        lambda *args, **kwargs: notify_calls.append((args, kwargs)),
    )

    scheduler.job_spy_scan_account(account_id)

    assert len(notify_calls) == 0


def test_job_options_scan_account_does_not_notify_on_2xx(monkeypatch):
    account_id = 4247
    notify_calls = []

    monkeypatch.setattr("httpx.post", lambda *_a, **_k: _FakeResponse(200, "queued"))
    monkeypatch.setattr(
        scheduler.alerts,
        "notify",
        lambda *args, **kwargs: notify_calls.append((args, kwargs)),
    )

    scheduler.job_options_scan_account(account_id)

    assert len(notify_calls) == 0


# --- nightly scan reconciler branch ---

def test_reconcile_updates_nightly_scan_time(tmp_db, monkeypatch):
    sched = BackgroundScheduler(timezone=scheduler.TIMEZONE)
    sched.add_job(
        scheduler.job_nightly_scan,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=0, timezone=scheduler.TIMEZONE),
        id="nightly_scan",
    )
    db.set_app_setting(scheduler.NIGHTLY_SCAN_TIME_SETTING, "06:15")
    scheduler.job_reconcile_schedules(sched)
    job = sched.get_job("nightly_scan")
    ts = str(job.trigger)
    assert "day_of_week='mon-fri'" in ts
    assert "hour='6'" in ts
    assert "minute='15'" in ts

    original_add_job = sched.add_job
    calls = []

    def counting_add_job(*args, **kwargs):
        calls.append((args, kwargs))
        return original_add_job(*args, **kwargs)

    monkeypatch.setattr(sched, "add_job", counting_add_job)
    scheduler.job_reconcile_schedules(sched)
    assert len(calls) == 0
    assert str(sched.get_job("nightly_scan").trigger) == ts

"""Tests for web/scheduler.py's tier-gated register_jobs()."""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from web import scheduler


def _registered_ids(monkeypatch, features_env: str) -> set[str]:
    monkeypatch.setenv("FEATURES", features_env)
    sched = BackgroundScheduler()
    scheduler.register_jobs(sched)
    return {job.id for job in sched.get_jobs()}


class TestRegisterJobs:
    def test_tier1_registers_only_universal_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "")
        assert ids == {"reap_stuck_runs", "outcome_sweep"}

    def test_tier2_adds_schwab_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "schwab")
        assert ids == {
            "reap_stuck_runs", "outcome_sweep",
            "nightly_scan", "morning_newsletter", "token_health",
        }

    def test_tier3_adds_sp500_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "schwab,sp500")
        assert ids == {
            "reap_stuck_runs", "outcome_sweep",
            "nightly_scan", "morning_newsletter", "token_health",
            "spy_scan", "spy_price_refresh",
        }

    def test_tier4_registers_all_twelve_jobs(self, monkeypatch):
        ids = _registered_ids(monkeypatch, "schwab,sp500,options")
        assert ids == {
            "reap_stuck_runs", "outcome_sweep",
            "nightly_scan", "morning_newsletter", "token_health",
            "spy_scan", "spy_price_refresh",
            "options_scan", "options_refresh", "options_refresh_close",
            "options_settle", "options_grade",
        }
        assert len(ids) == 12

    def test_options_alone_without_sp500_does_not_register_sp500_jobs(self, monkeypatch):
        # Not a real tier combination (options implies sp500 in the cumulative
        # model), but register_jobs should still gate each block independently.
        ids = _registered_ids(monkeypatch, "options")
        assert "spy_scan" not in ids
        assert "options_scan" in ids

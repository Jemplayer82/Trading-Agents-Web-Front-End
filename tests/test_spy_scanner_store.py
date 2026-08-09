"""Deep dives must contribute to System C (the memory log).

Regression cover for the learning-loop gap where run_deep_dives read past
context but never stored its own decisions — the highest-volume decision path
(options + S&P scans) contributed nothing to nightly outcome grading.
"""

import threading
from unittest.mock import MagicMock

import pandas as pd
import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from web import db, spy_scanner

pytestmark = pytest.mark.unit

DECISION = "Rating: Buy\nStrong momentum thesis; enter on pullback."


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


def _fake_orchestrator_cls(memory_log, decision=DECISION, signal="BUY"):
    """Stand-in for SwitchboardOrchestrator matching what _dive_inner uses."""

    class FakeOrchestrator:
        def __init__(self, config=None, selected_analysts=None, **kw):
            self.memory_log = memory_log

        def run(self, ticker, trade_date, **kw):
            return {"final_trade_decision": decision}, signal

    return FakeOrchestrator


def _run(tmp_db, monkeypatch, tmp_path, *, config_extra=None, decision=DECISION,
         store_raises=False, tickers=("AAPL",)):
    scan_id = db.create_spy_scan("2026-07-29", kind="options")
    config = {"memory_log_path": str(tmp_path / "mem.md")}
    config.update(config_extra or {})
    memory_log = TradingMemoryLog(config)
    if store_raises:
        memory_log = MagicMock(wraps=memory_log)
        memory_log.store_decision.side_effect = RuntimeError("disk full")
    monkeypatch.setattr(
        spy_scanner, "SwitchboardOrchestrator",
        _fake_orchestrator_cls(memory_log, decision=decision),
    )
    candidates = [{"ticker": t, "signal": "BUY", "conviction": 8} for t in tickers]
    enriched = spy_scanner.run_deep_dives(
        scan_id, candidates, "2026-07-29", config, ["market"],
    )
    return enriched, TradingMemoryLog(config)


def test_deep_dive_stores_pending_decision(tmp_db, monkeypatch, tmp_path):
    enriched, log = _run(tmp_db, monkeypatch, tmp_path)
    assert not enriched[0].get("error")
    entries = log.load_entries()
    assert len(entries) == 1
    assert entries[0]["ticker"] == "AAPL"
    assert entries[0]["pending"] is True
    assert entries[0]["rating"] == "Buy"


def test_store_failure_never_fails_the_dive(tmp_db, monkeypatch, tmp_path):
    """A memory-log write failure must not turn a good analysis into an
    'error' row (which would get dropped before vetting)."""
    enriched, _ = _run(tmp_db, monkeypatch, tmp_path, store_raises=True)
    assert not enriched[0].get("error"), "store failure leaked into the dive result"
    assert enriched[0]["signal"] == "BUY"


def test_empty_decision_stores_nothing(tmp_db, monkeypatch, tmp_path):
    _, log = _run(tmp_db, monkeypatch, tmp_path, decision="")
    assert log.load_entries() == []


def test_kill_switch_disables_store(tmp_db, monkeypatch, tmp_path):
    _, log = _run(tmp_db, monkeypatch, tmp_path,
                  config_extra={"deep_dive_store_decisions": False})
    assert log.load_entries() == []


def test_same_day_rerun_dedupes(tmp_db, monkeypatch, tmp_path):
    """Options and equity scans deep-diving the same ticker on the same date
    must not double-count it in calibration."""
    _run(tmp_db, monkeypatch, tmp_path)
    _, log = _run(tmp_db, monkeypatch, tmp_path)
    assert len(log.load_entries()) == 1


class TestQuickScanPriceDataCache:
    """Same-day TTL cache for the bulk price fetch that feeds run_quick_scan."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        spy_scanner._PRICE_DATA_CACHE.clear()
        yield
        spy_scanner._PRICE_DATA_CACHE.clear()

    @staticmethod
    def _frame(tickers, n=5):
        data = {}
        for i, t in enumerate(tickers):
            data[("Close", t)] = [float(100 + i * 10 + j) for j in range(n)]
            data[("Volume", t)] = [1000] * n
        return pd.DataFrame(data)

    def test_same_day_same_tickers_downloads_once(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        m1 = spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, ["AAA", "BBB"], "2026-08-08")

        assert calls["count"] == 1
        assert m1 is m2

    def test_ticker_order_does_not_matter(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        m1 = spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, ["BBB", "AAA"], "2026-08-08")

        assert calls["count"] == 1
        assert m1 == m2

    def test_different_ticker_set_is_a_separate_key(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        spy_scanner._fetch_price_data_map(2, ["AAA", "BBB", "CCC"], "2026-08-08")

        assert calls["count"] == 2

    def test_different_trade_date_refetches(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        spy_scanner._fetch_price_data_map(2, ["AAA", "BBB"], "2026-08-09")

        assert calls["count"] == 2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 1

    def test_prior_day_entries_are_evicted(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        spy_scanner._fetch_price_data_map(1, ["AAA", "BBB"], "2026-08-08")
        spy_scanner._fetch_price_data_map(2, ["AAA", "BBB"], "2026-08-09")

        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 1

        spy_scanner._fetch_price_data_map(3, ["AAA", "BBB"], "2026-08-08")

        assert calls["count"] == 3

    def test_ttl_expiry_refetches(self, monkeypatch):
        now = [0.0]
        monkeypatch.setattr(spy_scanner.market_cache, "_now", lambda: now[0])

        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        spy_scanner._fetch_price_data_map(1, ["AAA"], "2026-08-08")
        now[0] += spy_scanner._PRICE_DATA_TTL_SECONDS + 1
        spy_scanner._fetch_price_data_map(2, ["AAA"], "2026-08-08")

        assert calls["count"] == 2

    @pytest.mark.parametrize("behavior", ["exception", "empty"])
    def test_failed_download_is_not_cached(self, monkeypatch, behavior):
        calls = {"count": 0}

        def _download(*a, **kw):
            calls["count"] += 1
            if behavior == "exception":
                raise RuntimeError("boom")
            return pd.DataFrame()

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        m1 = spy_scanner._fetch_price_data_map(1, ["AAA"], "2026-08-08")
        assert m1 == {}
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 0

        m2 = spy_scanner._fetch_price_data_map(2, ["AAA"], "2026-08-08")
        assert m2 == {}
        assert calls["count"] == 2

    def test_concurrent_callers_share_the_cache(self, monkeypatch):
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(tickers, n=3)

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        results = [None, None]
        errors = [None, None]

        def worker(idx):
            try:
                results[idx] = spy_scanner._fetch_price_data_map(
                    idx + 10, ["X", "Y"], "2026-08-08"
                )
            except Exception as exc:
                errors[idx] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert all(not t.is_alive() for t in threads)
        assert errors == [None, None]
        assert results[0] is not None
        assert results[0] == results[1]
        assert calls["count"] <= 2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 1

    def test_partial_download_below_threshold_is_not_cached(self, monkeypatch):
        """Only 1 of 3 tickers came back — below 80% completeness, so the
        cache write is skipped even though the function still returns the
        partial map to the caller.
        """
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(["AAA"])

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        tickers = ["AAA", "BBB", "CCC"]
        m1 = spy_scanner._fetch_price_data_map(1, tickers, "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, tickers, "2026-08-08")

        assert calls["count"] == 2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 0
        assert list(m1.keys()) == ["AAA"]
        assert m1 == m2

    def test_partial_download_still_returns_usable_tickers(self, monkeypatch):
        """The completeness gate must block only the cache write, never the
        return value.
        """
        def _download(tickers, *a, **kw):
            return self._frame(["AAA"])

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        m = spy_scanner._fetch_price_data_map(1, ["AAA", "BBB", "CCC"], "2026-08-08")
        assert list(m.keys()) == ["AAA"]
        assert m["AAA"]["close"]
        assert m["AAA"]["volume"]

    def test_download_just_above_threshold_still_caches(self, monkeypatch):
        """4 of 5 tickers present clears the 80% bar and is cached for reuse."""
        calls = {"count": 0}

        def _download(tickers, *a, **kw):
            calls["count"] += 1
            return self._frame(["T1", "T2", "T3", "T4"])

        monkeypatch.setattr(spy_scanner.yf, "download", _download)

        tickers = ["T1", "T2", "T3", "T4", "T5"]
        m1 = spy_scanner._fetch_price_data_map(1, tickers, "2026-08-08")
        m2 = spy_scanner._fetch_price_data_map(2, tickers, "2026-08-08")

        assert calls["count"] == 1
        assert m1 is m2
        assert spy_scanner._PRICE_DATA_CACHE.stats()["size"] == 1
        assert sorted(m1.keys()) == ["T1", "T2", "T3", "T4"]

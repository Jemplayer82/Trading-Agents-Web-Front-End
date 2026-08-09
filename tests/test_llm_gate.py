"""Unit tests for the shared LLM concurrency gate.

These tests live at the lowest tier-independent layer (`web.llm_helpers`) so
they survive tier-2 builds where `web.spy_scanner.py` is removed.
"""


import threading
import time

import pytest

from web.llm_helpers import DynamicGate, _GateMonitor, _total_budget

pytestmark = pytest.mark.unit


def test_total_budget_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("OLLAMA_MAX_CONCURRENCY", raising=False)
    assert _total_budget() == 5

    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "7")
    assert _total_budget() == 7

    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "0")
    assert _total_budget() == 1

    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "abc")
    assert _total_budget() == 5


def test_gate_limits_concurrency():
    gate = DynamicGate(2)
    lock = threading.Lock()
    current = 0
    max_seen = 0
    errors = []

    def worker():
        nonlocal current, max_seen
        try:
            with gate:
                with lock:
                    current += 1
                    if current > max_seen:
                        max_seen = current
                time.sleep(0.05)
                with lock:
                    current -= 1
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join(timeout=5)
    finally:
        for t in threads:
            t.join(timeout=1)

    assert not errors
    assert max_seen <= 2
    assert max_seen == 2
    assert not any(t.is_alive() for t in threads)


def test_gate_resize_down_floors_at_one():
    gate = DynamicGate(2)
    gate.set_limit(0)
    assert gate.limit == 1
    gate.set_limit(4)
    assert gate.limit == 4


def test_gate_monitor_applies_initial_limit_synchronously(monkeypatch):
    monkeypatch.setattr("web.db.count_active_single", lambda: 3)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "5")

    with _GateMonitor(DynamicGate(5)) as gate:
        assert gate.limit == 2


def test_gate_monitor_survives_a_db_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("web.db.count_active_single", boom)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "5")

    with _GateMonitor(DynamicGate(5)) as gate:
        assert gate.limit == 5


def test_gate_monitor_stops_its_thread_on_exit(monkeypatch):
    monkeypatch.setattr("web.db.count_active_single", lambda: 0)
    monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "5")

    monitor = _GateMonitor(DynamicGate(5))
    with monitor:
        assert monitor._thread.is_alive()

    deadline = time.time() + 2
    while time.time() < deadline and monitor._thread.is_alive():
        time.sleep(0.05)

    assert not monitor._thread.is_alive()
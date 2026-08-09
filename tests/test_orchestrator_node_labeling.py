"""Tests covering thread-safety of the orchestrator's streaming node labels."""

import threading
from unittest.mock import MagicMock

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator.switchboard_orchestrator import SwitchboardOrchestrator

pytestmark = pytest.mark.unit


def _orch(tmp_path, **extra):
    config = {
        **DEFAULT_CONFIG,
        "llm_provider": "openai",
        "deep_think_llm": "gpt-4o-mini",
        "quick_think_llm": "gpt-4o-mini",
        "memory_log_path": str(tmp_path / "mem.md"),
        "data_cache_dir": str(tmp_path / "cache"),
        "results_dir": str(tmp_path / "results"),
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
    }
    config.update(extra)
    return SwitchboardOrchestrator(config=config, selected_analysts=["market"])


def test_current_node_defaults_to_none_on_every_thread(tmp_path):
    orch = _orch(tmp_path)

    assert orch._current_node is None

    captured = []

    def worker():
        captured.append(orch._current_node)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert captured == [None]


def test_current_node_writes_are_thread_isolated(tmp_path):
    orch = _orch(tmp_path)
    orch._current_node = "main_node"

    captured = []

    def worker():
        captured.append(orch._current_node)
        orch._current_node = "worker_node"
        captured.append(orch._current_node)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert captured == [None, "worker_node"]
    assert orch._current_node == "main_node"


def test_emit_token_labels_frames_with_the_calling_threads_node(tmp_path):
    orch = _orch(tmp_path)

    frames = []
    lock = threading.Lock()

    def record(frame):
        with lock:
            frames.append(frame)

    orch.on_progress = record

    barrier = threading.Barrier(2, timeout=10)

    def emit_for(label, node):
        orch._current_node = node
        barrier.wait(timeout=10)
        for _ in range(50):
            orch._emit_token(label)

    threads = [
        threading.Thread(target=emit_for, args=("A", "market_analyst")),
        threading.Thread(target=emit_for, args=("B", "news_analyst")),
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(frames) == 100

    a_frames = [f for f in frames if f["text"] == "A"]
    b_frames = [f for f in frames if f["text"] == "B"]

    assert len(a_frames) == 50
    assert len(b_frames) == 50

    for frame in frames:
        assert frame["type"] == "token"
        assert frame["channel"] == "content"
        if frame["text"] == "A":
            assert frame["node"] == "market_analyst"
        elif frame["text"] == "B":
            assert frame["node"] == "news_analyst"
        else:
            raise AssertionError(f"Unexpected frame text: {frame['text']!r}")


def test_emit_token_is_silent_when_no_node_is_set(tmp_path):
    orch = _orch(tmp_path)

    frames = []

    def record(frame):
        frames.append(frame)

    orch.on_progress = record
    orch._emit_token("x")

    assert frames == []


def test_set_then_clear_round_trips_on_one_thread(tmp_path):
    orch = _orch(tmp_path)

    orch._current_node = "market_analyst"
    assert orch._current_node == "market_analyst"

    orch._current_node = None
    assert orch._current_node is None


def test_magicmock_spec_stub_can_still_set_current_node():
    stub = MagicMock(spec=SwitchboardOrchestrator)
    stub._current_node = "market_analyst"
    assert stub._current_node == "market_analyst"

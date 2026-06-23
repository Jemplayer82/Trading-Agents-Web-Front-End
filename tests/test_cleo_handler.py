"""Regression tests for scripts/cleo_llm_handler.py call_claude_streaming.

These guard the subprocess-handling fix: ``claude -p --verbose`` is chatty on
stderr, and the original code never drained it. Past ~64KB the OS pipe buffer
fills, the CLI blocks writing stderr, stops producing stdout, and the reader
hangs forever (the "works on one request, fails the next" bug). We now drain
stderr on a thread and enforce a per-call deadline with a watchdog kill.

The tests drive the REAL call_claude_streaming with a fake 'claude' binary that
reproduces each failure condition, so the protections are verified, not assumed.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HANDLER = _REPO / "scripts" / "cleo_llm_handler.py"

# A fake 'claude' speaking just enough stream-json for the handler. The scenario
# is chosen via the FAKE_SCENARIO env var the test sets on the subprocess.
_FAKE = r'''
import sys, time, threading, os
def _drain():
    try:
        for _ in sys.stdin:
            pass
    except Exception:
        pass
threading.Thread(target=_drain, daemon=True).start()
scenario = os.environ["FAKE_SCENARIO"]
if scenario == "stderr_flood":
    # ~300KB of stderr BEFORE any stdout. Old code deadlocks at the 64KB buffer.
    blob = "x" * 1000
    for _ in range(300):
        sys.stderr.write(blob + "\n")
    sys.stderr.flush()
    sys.stdout.write('{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"hello world"}}}\n')
    sys.stdout.write('{"type":"result","is_error":false,"result":"ok"}\n')
    sys.stdout.flush()
    sys.exit(0)
elif scenario == "hang":
    # Never emit a result, never exit; the watchdog must kill us.
    while True:
        time.sleep(0.5)
'''


@pytest.fixture
def cleo():
    spec = importlib.util.spec_from_file_location("cleo_llm_handler_under_test", _HANDLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_claude(tmp_path):
    path = tmp_path / "fake_claude.py"
    path.write_text(_FAKE, encoding="utf-8")
    return path


def _patch_popen(cleo, fake_claude, scenario, monkeypatch):
    orig = subprocess.Popen

    def _popen(_cmd, **kwargs):
        env = dict(os.environ, FAKE_SCENARIO=scenario)
        return orig([sys.executable, str(fake_claude)], env=env, **kwargs)

    monkeypatch.setattr(cleo.subprocess, "Popen", _popen)


def _consume(cleo, timeout):
    """Run call_claude_streaming on a thread; return (chunks, error, hung)."""
    out: dict = {"chunks": [], "error": None}

    def _go():
        try:
            for ch in cleo.call_claude_streaming("m", "sys", [{"role": "user", "content": "hi"}], [], 8192):
                out["chunks"].append(ch)
        except Exception as exc:
            out["error"] = exc

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(timeout)
    return out["chunks"], out["error"], t.is_alive()


def test_stderr_flood_does_not_deadlock(cleo, fake_claude, monkeypatch):
    """A subprocess that floods stderr past the pipe buffer must still complete."""
    _patch_popen(cleo, fake_claude, "stderr_flood", monkeypatch)
    chunks, error, hung = _consume(cleo, timeout=20)

    assert not hung, "call_claude_streaming hung on a stderr flood (deadlock)"
    assert error is None, f"unexpected error: {error}"
    deltas = "".join(c["delta"] for c in chunks if "delta" in c)
    assert "hello world" in deltas
    assert any(c.get("done") for c in chunks), "never yielded the done chunk"


def test_hung_subprocess_is_killed_by_watchdog(cleo, fake_claude, monkeypatch):
    """A subprocess that never responds must be killed and surface a timeout."""
    monkeypatch.setattr(cleo, "CLAUDE_CALL_TIMEOUT_S", 2.0)
    _patch_popen(cleo, fake_claude, "hang", monkeypatch)
    chunks, error, hung = _consume(cleo, timeout=15)

    assert not hung, "watchdog did not kill the hung subprocess"
    assert error is not None and "timed out" in str(error), f"expected a timeout error, got {error}"

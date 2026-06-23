#!/usr/bin/env python3
"""Cleo LLM handler — Claude CLI-backed bus daemon for TradingAgents.

Registers on the mcp-switchboard bus, polls for llm_request DMs, drives the
local ``claude`` CLI in headless streaming mode

    claude -p --input-format stream-json --output-format stream-json \\
           --verbose --include-partial-messages

which uses the machine's Claude Code subscription session — NO Anthropic API
key, NO per-token billing. Replies are streamed back as ``llm_stream_chunk``
DMs so tokens appear live in the dashboard.

Run this on a machine where ``claude -p "hi"`` already works (i.e. Claude Code
is logged in). It reaches the switchboard purely over HTTP, so it can run
anywhere that can hit SWITCHBOARD_URL.

Required env vars:
  SWITCHBOARD_URL         e.g. http://172.21.0.3:3107 or http://192.168.7.50:3109
  SWITCHBOARD_MCP_TOKEN   Bearer token matching the stack's SWITCHBOARD_MCP_TOKEN

Optional:
  SWITCHBOARD_AGENT_ID    Agent name to register as (default: cleo)
  DEFAULT_MODEL           Fallback model if the request doesn't specify one
                          (default: claude-sonnet-4-6)
  CLAUDE_BIN              Path to claude CLI binary (default: claude)

Usage:
  SWITCHBOARD_URL=http://172.21.0.3:3107 \\
  SWITCHBOARD_MCP_TOKEN=<token> \\
  python3 scripts/cleo_llm_handler.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-sonnet-4-6")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# Hard ceiling on a single claude -p call. The watchdog kills the subprocess past
# this, so a stuck CLI (auth wedge, no output, etc.) can never hang a worker
# forever. Keep it STRICTLY below the client-side timeout (switchboard_client
# defaults to 180s) so Cleo fails itself and emits an error before the requester
# gives up waiting.
CLAUDE_CALL_TIMEOUT_S = float(os.environ.get("CLEO_CALL_TIMEOUT_S", "150"))

# Text protocol for tool calling. The CLI can't accept Anthropic tool schemas
# (that's an API-only feature), so we teach the model an inline marker syntax in
# the system prompt and parse it back out of the generated text.
_TOOL_OPEN = "<tool_call"
_TOOL_CLOSE = "</tool_call>"
_TOOL_RE = re.compile(r'<tool_call\s+name="([^"]+)"\s*>([\s\S]*?)</tool_call>')


# ---------------------------------------------------------------------------
# Bus helpers
# ---------------------------------------------------------------------------

def _parse_sse(resp: httpx.Response) -> dict:
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/json"):
        return resp.json()
    last_data = None
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            last_data = line[5:].strip()
    if last_data is None:
        raise RuntimeError(f"No data: line in SSE body: {resp.text!r}")
    return json.loads(last_data)


def bus_call(url: str, token: str, tool: str, args: dict, timeout: float = 35.0) -> dict:
    resp = httpx.post(
        url.rstrip("/") + "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": tool, "arguments": args}},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    rpc = _parse_sse(resp)
    if rpc.get("error"):
        err = rpc["error"]
        raise RuntimeError(f"Bus RPC error: {err.get('message') if isinstance(err, dict) else err}")
    result = rpc.get("result") or {}
    if result.get("isError"):
        content = result.get("content") or []
        raise RuntimeError(f"Bus tool error: {content[0].get('text', '') if content else result}")
    content = result.get("content") or []
    return json.loads(content[0].get("text", "{}")) if content else {}


# ---------------------------------------------------------------------------
# Message / tool conversion helpers
# ---------------------------------------------------------------------------

def _flatten_content(content) -> str:
    """Collapse an Anthropic content value (str or block list) to plain text.

    tool_use / tool_result blocks are rendered as the inline text protocol so
    the CLI (which has no API tool awareness) still sees the full history.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            args = block.get("input", {})
            parts.append(f'{_TOOL_OPEN} name="{block.get("name", "")}">'
                         f'{json.dumps(args)}{_TOOL_CLOSE}')
        elif btype == "tool_result":
            inner = block.get("content", "")
            if isinstance(inner, list):
                inner = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in inner
                )
            parts.append(f"[Tool result]\n{inner}")
    return "\n".join(p for p in parts if p)


def _augment_system_with_tools(system: str, tools: list) -> str:
    """Wrap the system prompt with mandatory inline tool-call instructions.

    The CLI can't accept Anthropic tool schemas, so we teach the model an inline
    marker syntax. Analysts run with NO pre-loaded data — the model must call
    tools to fetch it. A permissive "when you need to" framing led the model to
    write reports from memory instead, so the directive is now imperative and
    brackets the caller's prompt (first and last thing the model sees).
    """
    if not tools:
        return system
    tool_names = ", ".join(t.get("name", "?") for t in tools)
    directive = (
        "## CRITICAL — you have NO pre-loaded data\n"
        "You have NO market, price, news, sentiment, or fundamentals data in "
        "context. Any figures you recall from training are stale and MUST NOT be "
        "used. You are REQUIRED to call the tools below to fetch live data BEFORE "
        "writing any analysis.\n\n"
        "To call a tool, emit it on its own line in EXACTLY this form:\n"
        f'{_TOOL_OPEN} name=\"TOOL_NAME\">{{\"arg\": \"value\"}}{_TOOL_CLOSE}\n'
        "The content between the tags must be a single valid JSON object of "
        "arguments. Emit one block per tool call. After emitting your tool "
        "call(s), STOP immediately — write nothing else and do not invent "
        "results; wait for the tool results to be returned to you.\n\n"
        f"Available tools: {tool_names}\n\n"
        "Tool JSON schemas:\n"
        f"{json.dumps(tools, indent=2)}"
    )
    if not system:
        return directive
    return (
        f"{directive}\n\n---\n\n{system}\n\n---\n\n"
        "REMINDER: Do not fabricate data. If you have not yet called the tools "
        "above for the data you need, emit the tool call(s) now and write nothing else."
    )


def _parse_tool_calls(text: str) -> list[dict]:
    """Extract inline <tool_call> markers from generated text → bus tool_calls."""
    calls: list[dict] = []
    for name, raw_args in _TOOL_RE.findall(text):
        try:
            args = json.loads(raw_args.strip()) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": f"toolu_{uuid4().hex[:16]}", "name": name, "args": args})
    return calls


class _ToolMarkerFilter:
    """Streams text but withholds anything between <tool_call> and </tool_call>.

    Markers can be split across deltas, so a small holdback buffer prevents a
    partial ``<tool_call`` prefix from leaking to the dashboard before we know
    whether it's really the start of a tool block.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._in_tool = False
        self._raw: list[str] = []

    def feed(self, text: str) -> str:
        self._raw.append(text)
        self._buf += text
        out: list[str] = []
        while self._buf:
            if not self._in_tool:
                idx = self._buf.find(_TOOL_OPEN)
                if idx == -1:
                    safe = self._safe_len(self._buf, _TOOL_OPEN)
                    if safe:
                        out.append(self._buf[:safe])
                        self._buf = self._buf[safe:]
                    break
                if idx > 0:
                    out.append(self._buf[:idx])
                self._buf = self._buf[idx:]
                self._in_tool = True
            else:
                idx = self._buf.find(_TOOL_CLOSE)
                if idx == -1:
                    break  # still inside a tool block; hold everything back
                self._buf = self._buf[idx + len(_TOOL_CLOSE):]
                self._in_tool = False
        return "".join(out)

    @staticmethod
    def _safe_len(s: str, marker: str) -> int:
        """How much of s is safe to emit (not a partial-marker suffix)."""
        for k in range(min(len(marker) - 1, len(s)), 0, -1):
            if marker.startswith(s[-k:]):
                return len(s) - k
        return len(s)

    def flush(self) -> str:
        if not self._in_tool and self._buf:
            out, self._buf = self._buf, ""
            return out
        return ""

    @property
    def full_text(self) -> str:
        return "".join(self._raw)


# ---------------------------------------------------------------------------
# Claude CLI invocation
# ---------------------------------------------------------------------------

def call_claude_streaming(model: str, system: str, messages: list, tools: list, max_tokens: int):
    """Drive ``claude -p`` in stream-json mode and yield text deltas + tool_calls.

    Uses the local Claude Code subscription session (free — no API key). Yields:
      {"delta": "text"}                    — streamed text (tool markers stripped)
      {"done": True, "tool_calls": [...]}  — completion signal
    """
    system = _augment_system_with_tools(system, tools)

    cmd = [
        CLAUDE_BIN, "-p",
        "--strict-mcp-config",  # no MCP servers; stops leaked docker MCP containers
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--no-session-persistence",
        "--tools", "",            # disable built-in Claude Code tools
        "--model", model,
    ]
    if system:
        cmd += ["--system-prompt", system]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    # Feed the conversation on a thread so a large history can't deadlock against
    # a full stdout pipe.
    def _write_stdin() -> None:
        try:
            for m in messages:
                role = m.get("role", "user")
                evt_type = "assistant" if role == "assistant" else "user"
                event = {
                    "type": evt_type,
                    "message": {
                        "role": role,
                        "content": [{"type": "text", "text": _flatten_content(m.get("content", ""))}],
                    },
                }
                proc.stdin.write(json.dumps(event) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    # Drain stderr on a thread too. ``claude -p --verbose`` is chatty on stderr;
    # if nobody reads it, its ~64KB OS pipe buffer fills, the CLI blocks writing,
    # stops producing stdout, and the read loop below hangs forever (the original
    # "works once, fails the next" bug). Keep only the tail for error reporting.
    stderr_tail: deque[str] = deque(maxlen=50)

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                stderr_tail.append(line)
        except (BrokenPipeError, ValueError):
            pass

    # Watchdog: hard-kill the subprocess if it blows past the per-call deadline,
    # so a stuck claude (no output at all, auth wedge, network stall) can't hang
    # the worker indefinitely. Killing closes stdout, which ends the read loop.
    done_evt = threading.Event()
    timed_out = threading.Event()

    def _watchdog() -> None:
        if not done_evt.wait(CLAUDE_CALL_TIMEOUT_S):
            timed_out.set()
            log.warning("claude CLI exceeded %.0fs deadline — killing", CLAUDE_CALL_TIMEOUT_S)
            try:
                proc.kill()
            except Exception:
                pass

    threading.Thread(target=_write_stdin, daemon=True).start()
    threading.Thread(target=_drain_stderr, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()

    flt = _ToolMarkerFilter()
    error_result: str | None = None
    got_result = False

    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            otype = obj.get("type", "")

            if otype == "stream_event":
                event = obj.get("event", {})
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        visible = flt.feed(delta.get("text", ""))
                        if visible:
                            yield {"delta": visible}

            elif otype == "result":
                got_result = True
                if obj.get("is_error"):
                    error_result = obj.get("result") or "claude CLI returned an error"
                break

        tail = flt.flush()
        if tail:
            yield {"delta": tail}
    finally:
        # Always stop the watchdog and reap the child — covers normal completion,
        # an exception, and an abandoned generator (requester went away). Safe to
        # kill even on success: we've already parsed the result event by here.
        done_evt.set()
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    if error_result is not None:
        raise RuntimeError(f"claude CLI error: {error_result}")
    if got_result:
        # A clean result event means the call succeeded — don't misread the
        # post-result kill above as a failure via returncode.
        yield {"done": True, "tool_calls": _parse_tool_calls(flt.full_text)}
        return
    # No result event ⇒ the CLI died early. Explain why: deadline first, then exit.
    if timed_out.is_set():
        raise RuntimeError(f"claude CLI timed out after {CLAUDE_CALL_TIMEOUT_S:.0f}s")
    err = "".join(stderr_tail).strip()
    raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {err}")


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def handle_request(msg: dict, url: str, token: str, agent_id: str) -> None:
    req = json.loads(msg["content"])
    model = req.get("model") or DEFAULT_MODEL
    system = req.get("system") or ""
    messages = req.get("messages") or []
    tools = req.get("tools") or []
    try:
        max_tokens = int(req.get("max_tokens") or 8192)
    except (TypeError, ValueError):
        max_tokens = 8192
    sender = msg.get("from")
    msg_id = msg.get("id")
    thread_id = msg.get("thread_id") or str(uuid4())

    log.info("llm_request from=%s model=%s tools=%d", sender, model, len(tools))

    def send(msg_type: str, content: str) -> None:
        bus_call(url, token, "send_message", {
            "from": agent_id,
            "to": sender,
            "type": msg_type,
            "thread_id": thread_id,
            "reply_to": msg_id,
            "content": content,
        })

    tool_calls: list = []
    for chunk in call_claude_streaming(model, system, messages, tools, max_tokens):
        if "delta" in chunk:
            # Best-effort: a transient bus hiccup on one delta must not tear down
            # an otherwise-healthy stream (and orphan the live claude subprocess).
            try:
                send("llm_stream_chunk", json.dumps({"delta": chunk["delta"], "done": False}))
            except Exception as exc:
                log.warning("delta send failed (continuing stream): %s", exc)
        elif chunk.get("done"):
            tool_calls = chunk.get("tool_calls", [])

    send("llm_stream_chunk", json.dumps({"delta": "", "done": True, "tool_calls": tool_calls}))
    log.info("  → streamed reply to %s (%d tool_calls)", sender, len(tool_calls))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    url = os.environ["SWITCHBOARD_URL"]
    token = os.environ["SWITCHBOARD_MCP_TOKEN"]  # pragma: allowlist secret
    agent_id = os.environ.get("SWITCHBOARD_AGENT_ID", "cleo")

    def bus(tool: str, args: dict) -> dict:
        return bus_call(url, token, tool, args)

    # Split-brain heads-up: if a SECOND process polls the same agent_id, the two
    # silently split the request stream (each wait_for_message drains the shared
    # inbox) — one streams while the other answers in one shot, corrupting
    # replies. Presence alone can't prove a duplicate (a just-restarted self
    # lingers ~60s, and a heartbeat script keeps presence warm), so this is an
    # advisory note, not a hard failure. The real tell is `llm_response` /
    # reply_to:null messages from this agent on the bus while a handler streams.
    try:
        agents = bus("list_agents", {}).get("agents", [])
        peer = next(
            (a for a in agents if a.get("id") == agent_id and a.get("online")),
            None,
        )
        if peer:
            log.info(
                "Note: '%s' already shows online (likely this restart's lingering "
                "presence or a heartbeat script). If replies look truncated, make "
                "sure only ONE handler polls this agent_id (or use a unique "
                "SWITCHBOARD_AGENT_ID).",
                agent_id,
            )
    except Exception as exc:
        log.debug("dup-registration check skipped: %s", exc)

    bus("register_agent", {"agent_id": agent_id, "name": "Cleo (Claude daemon)"})
    bus("set_status", {"agent_id": agent_id, "activity": "ready"})
    log.info("cleo-llm-handler registered as '%s' — waiting for llm_request DMs", agent_id)

    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cleo")

    while True:
        try:
            result = bus("wait_for_message", {"agent_id": agent_id, "timeout_seconds": 25})
        except Exception as exc:
            log.warning("Poll error: %s — retrying in 2s", exc)
            time.sleep(2)
            continue

        for msg in result.get("messages", []):
            if msg.get("type") != "llm_request":
                continue
            executor.submit(_dispatch, msg, url, token, agent_id)


def _dispatch(msg: dict, url: str, token: str, agent_id: str) -> None:
    try:
        handle_request(msg, url, token, agent_id)
    except Exception as exc:
        log.exception("Error handling message %s", msg.get("id"))
        try:
            bus_call(url, token, "send_message", {
                "from": agent_id,
                "to": msg.get("from"),
                "type": "llm_error",
                "thread_id": msg.get("thread_id"),
                "reply_to": msg.get("id"),
                "content": json.dumps({"error": str(exc)}),
            })
        except Exception:
            pass


if __name__ == "__main__":
    main()

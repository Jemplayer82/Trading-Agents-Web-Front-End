#!/usr/bin/env python3
"""Cleo LLM handler — Claude CLI-backed bus daemon for TradingAgents.

Registers on the mcp-switchboard bus, polls for llm_request DMs, calls
``claude api messages create`` (uses the local Claude Code session auth —
no separate ANTHROPIC_API_KEY needed), and replies with streaming
llm_stream_chunk DMs so tokens appear live in the dashboard.

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
  python3 ~/cleo_llm_handler.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-sonnet-4-6")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")


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
# Claude CLI invocation
# ---------------------------------------------------------------------------

def call_claude_streaming(model: str, system: str, messages: list, tools: list, max_tokens: int):
    """Invoke ``claude api messages create`` and yield text deltas + final tool_calls.

    Yields either:
      {"delta": "text"}           — partial text
      {"done": True, "tool_calls": [...]}  — completion signal
    """
    api_payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": True,
    }
    if system:
        api_payload["system"] = system
    if tools:
        api_payload["tools"] = tools

    cmd = [CLAUDE_BIN, "api", "messages", "create", "--data", json.dumps(api_payload)]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    tool_calls: list = []
    current_tool: dict | None = None
    current_tool_input_json = ""

    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")

        if etype == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                current_tool = {"id": block.get("id"), "name": block.get("name"), "args": {}}
                current_tool_input_json = ""

        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            dtype = delta.get("type", "")
            if dtype == "text_delta":
                text = delta.get("text", "")
                if text:
                    yield {"delta": text}
            elif dtype == "input_json_delta" and current_tool is not None:
                current_tool_input_json += delta.get("partial_json", "")

        elif etype == "content_block_stop":
            if current_tool is not None:
                try:
                    current_tool["args"] = json.loads(current_tool_input_json) if current_tool_input_json else {}
                except json.JSONDecodeError:
                    current_tool["args"] = {}
                tool_calls.append(current_tool)
                current_tool = None
                current_tool_input_json = ""

        elif etype == "message_stop":
            break

    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read() if proc.stderr else "unknown error"
        raise RuntimeError(f"claude api call failed (exit {proc.returncode}): {err.strip()}")

    yield {"done": True, "tool_calls": tool_calls}


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def handle_request(msg: dict, url: str, token: str, agent_id: str) -> None:
    req = json.loads(msg["content"])
    model = req.get("model") or DEFAULT_MODEL
    system = req.get("system") or ""
    messages = req.get("messages") or []
    tools = req.get("tools") or []
    max_tokens = int(req.get("max_tokens") or 8192)
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
            send("llm_stream_chunk", json.dumps({"delta": chunk["delta"], "done": False}))
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

#!/usr/bin/env python3
"""Cleo LLM handler — Anthropic-backed bus daemon for TradingAgents.

Registers on the mcp-switchboard bus, polls for llm_request DMs, calls the
Anthropic API, and replies with streaming llm_stream_chunk DMs so tokens
appear live in the dashboard as Claude generates them.

Handles both request types transparently:
  - stream: true  → streams llm_stream_chunk DMs (one per text delta) then a
                    final chunk with {"done": true, "tool_calls": [...]}
  - stream: false → sends one llm_response DM (legacy; not used by default)

Required env vars:
  ANTHROPIC_API_KEY       Your Anthropic API key
  SWITCHBOARD_URL         e.g. http://192.168.7.50:3109 or http://switchboard:3107
  SWITCHBOARD_MCP_TOKEN   Bearer token matching the stack's SWITCHBOARD_MCP_TOKEN

Optional:
  SWITCHBOARD_AGENT_ID    Agent name to register as (default: cleo)
  DEFAULT_MODEL           Fallback model if the request doesn't specify one
                          (default: claude-sonnet-4-6)

Usage:
  pip install anthropic httpx
  ANTHROPIC_API_KEY=sk-ant-... \\
  SWITCHBOARD_URL=http://192.168.7.50:3109 \\
  SWITCHBOARD_MCP_TOKEN=<token> \\
  python scripts/cleo_llm_handler.py
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import anthropic
import httpx

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "claude-sonnet-4-6")


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
# Request handler
# ---------------------------------------------------------------------------

def handle_request(
    msg: dict,
    url: str,
    token: str,
    agent_id: str,
    client: anthropic.Anthropic,
) -> None:
    req = json.loads(msg["content"])
    model = req.get("model") or DEFAULT_MODEL
    system = req.get("system") or ""
    messages = req.get("messages") or []
    tools = req.get("tools") or []
    max_tokens = int(req.get("max_tokens") or 8192)
    do_stream = req.get("stream", True)
    sender = msg.get("from")
    msg_id = msg.get("id")
    thread_id = msg.get("thread_id") or str(uuid4())

    log.info("llm_request from=%s model=%s stream=%s tools=%d", sender, model, do_stream, len(tools))

    def send(msg_type: str, content: str) -> None:
        bus_call(url, token, "send_message", {
            "from": agent_id,
            "to": sender,
            "type": msg_type,
            "thread_id": thread_id,
            "reply_to": msg_id,
            "content": content,
        })

    api_kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        api_kwargs["system"] = system
    if tools:
        api_kwargs["tools"] = tools

    if do_stream:
        tool_calls: list = []
        with client.messages.stream(**api_kwargs) as stream:
            for text in stream.text_stream:
                if text:
                    send("llm_stream_chunk", json.dumps({"delta": text, "done": False}))
            final = stream.get_final_message()
            for block in final.content:
                if block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "name": block.name,
                        "args": block.input,
                    })
        send("llm_stream_chunk", json.dumps({"delta": "", "done": True, "tool_calls": tool_calls}))
        log.info("  → streamed reply to %s (%d tool_calls)", sender, len(tool_calls))

    else:
        resp = client.messages.create(**api_kwargs)
        content_text = ""
        tool_calls = []
        for block in resp.content:
            if hasattr(block, "text"):
                content_text += block.text
            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "args": block.input})
        send("llm_response", json.dumps({"content": content_text, "tool_calls": tool_calls}))
        log.info("  → response to %s (%d tool_calls)", sender, len(tool_calls))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    url = os.environ["SWITCHBOARD_URL"]
    token = os.environ["SWITCHBOARD_MCP_TOKEN"]  # pragma: allowlist secret
    agent_id = os.environ.get("SWITCHBOARD_AGENT_ID", "cleo")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is required")

    client = anthropic.Anthropic(api_key=api_key)

    def bus(tool: str, args: dict) -> dict:
        return bus_call(url, token, tool, args)

    bus("register_agent", {"agent_id": agent_id, "name": "Cleo (Claude daemon)"})
    bus("set_status", {"agent_id": agent_id, "activity": "ready"})
    log.info("cleo-llm-handler registered as '%s' — waiting for llm_request DMs", agent_id)

    # Concurrent dispatch so multiple analysts in a scan don't block each other.
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
            executor.submit(_dispatch, msg, url, token, agent_id, client)


def _dispatch(msg: dict, url: str, token: str, agent_id: str, client: anthropic.Anthropic) -> None:
    try:
        handle_request(msg, url, token, agent_id, client)
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

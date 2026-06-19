"""Minimal LLM router daemon for non-Claude providers via the switchboard bus.

Registers as an agent on the bus, receives llm_request DMs, dispatches to
Ollama / OpenAI / xAI / Grok via the OpenAI-compatible API, and sends back
llm_response DMs.

Claude is NOT handled here — use a Claude CLI session with the switchboard
hook configured. For claude provider requests this script forwards the DM to
CLAUDE_AGENT_ID (default: "claude-code") and relays the reply back.

Usage:
    SWITCHBOARD_URL=http://host:3107 \
    SWITCHBOARD_MCP_TOKEN=<token> \
    OLLAMA_BASE_URL=http://localhost:11434 \
    python -m tradingagents.llm_router

Env vars:
    SWITCHBOARD_URL         Required
    SWITCHBOARD_MCP_TOKEN   Required
    SWITCHBOARD_AGENT_ID    Agent name to register as (default: llm-router)
    OLLAMA_BASE_URL         Enables ollama provider (default: http://localhost:11434)
    OPENAI_API_KEY          Enables openai / grok / xai / deepseek providers
    OPENAI_BASE_URL         Override base URL for non-OpenAI OpenAI-compat APIs
    CLAUDE_AGENT_ID         Bus agent to forward claude requests to (default: claude-code)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Bus helpers (standalone — no web package import)
# ---------------------------------------------------------------------------

def _parse_sse(resp: httpx.Response) -> dict:
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/json"):
        return resp.json()
    last_data = None
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            last_data = line[len("data:"):].strip()
    if last_data is None:
        raise RuntimeError(f"No data: line in SSE body: {resp.text!r}")
    return json.loads(last_data)


def _bus_call(url: str, token: str, tool: str, args: dict, timeout: float = 35.0) -> dict:
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
# Format conversion helpers
# ---------------------------------------------------------------------------

def _anthropic_to_openai_messages(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-format messages to OpenAI-format."""
    out = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        # content is a list of blocks
        if role == "assistant":
            text_parts = []
            tool_calls = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
            entry: dict = {"role": "assistant", "content": " ".join(text_parts)}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)

        elif role == "user":
            for block in content:
                if block.get("type") == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": str(block.get("content", "")),
                    })
                elif block.get("type") == "text":
                    out.append({"role": "user", "content": block.get("text", "")})
                else:
                    out.append({"role": "user", "content": json.dumps(block)})
        else:
            out.append({"role": role, "content": json.dumps(content)})

    return out


def _anthropic_to_openai_tools(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool schema format to OpenAI function format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def _openai_response_to_common(choice) -> dict:
    """Convert an OpenAI ChatCompletionMessage to the common response format."""
    msg = choice.message
    content = msg.content or ""
    tool_calls = []
    for tc in getattr(msg, "tool_calls", None) or []:
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, AttributeError):
            args = {}
        tool_calls.append({
            "id": tc.id,
            "name": tc.function.name,
            "args": args,
        })
    return {"content": content, "tool_calls": tool_calls}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    url = os.environ["SWITCHBOARD_URL"]
    token = os.environ["SWITCHBOARD_MCP_TOKEN"]  # pragma: allowlist secret
    agent_id = os.environ.get("SWITCHBOARD_AGENT_ID", "llm-router")
    # OLLAMA_BASE_URL already includes the /v1 suffix by convention (default
    # http://localhost:11434/v1; Ollama Cloud is https://ollama.com/v1) — use
    # it as-is, matching tradingagents.llm_clients.defaults.
    ollama_base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    ollama_key = os.environ.get("OLLAMA_API_KEY", "")  # pragma: allowlist secret
    openai_key = os.environ.get("OPENAI_API_KEY", "")  # pragma: allowlist secret
    openai_base = os.environ.get("OPENAI_BASE_URL", "")
    claude_agent = os.environ.get("CLAUDE_AGENT_ID", "claude-code")

    def bus(tool: str, args: dict) -> dict:
        return _bus_call(url, token, tool, args)

    # Register
    bus("register_agent", {"agent_id": agent_id, "name": "LLM Router"})
    bus("set_status", {"agent_id": agent_id, "activity": "ready"})
    log.info("llm-router registered as '%s'", agent_id)

    # Lazy openai import; client cache shared across worker threads (lock-guarded
    # because check-then-create on a plain dict races under concurrency).
    _openai_client_cache: dict = {}
    _cache_lock = threading.Lock()

    def get_openai_client(provider: str, model: str):
        import openai
        if provider == "ollama":
            key = f"ollama:{ollama_base}"
            with _cache_lock:
                if key not in _openai_client_cache:
                    _openai_client_cache[key] = openai.OpenAI(
                        base_url=ollama_base,
                        api_key=ollama_key or "ollama",  # pragma: allowlist secret
                    )
                return _openai_client_cache[key]
        base = openai_base
        # Provider-specific default base URLs
        if not base:
            if provider in ("xai", "grok"):
                base = "https://api.x.ai/v1"
            elif provider == "deepseek":
                base = "https://api.deepseek.com"
        key = f"{provider}:{base}"
        with _cache_lock:
            if key not in _openai_client_cache:
                kwargs: dict = {"api_key": openai_key or "x"}  # pragma: allowlist secret
                if base:
                    kwargs["base_url"] = base
                _openai_client_cache[key] = openai.OpenAI(**kwargs)
            return _openai_client_cache[key]

    def dispatch(msg: dict) -> None:
        """Handle one llm_request in a worker thread; reply llm_error on failure."""
        try:
            _handle(
                msg=msg,
                bus=bus,
                agent_id=agent_id,
                claude_agent=claude_agent,
                get_openai_client=get_openai_client,
                url=url,
                token=token,  # pragma: allowlist secret
            )
        except Exception:
            log.exception("Error handling message %s", msg.get("id"))
            try:
                bus("send_message", {
                    "from": agent_id,
                    "to": msg.get("from"),
                    "type": "llm_error",
                    "thread_id": msg.get("thread_id"),
                    "reply_to": msg.get("id"),
                    "content": json.dumps({"error": "router failed handling request"}),
                })
            except Exception:
                pass

    # Concurrent dispatch: analysts fan out in parallel, so requests must be
    # handled concurrently or later ones blow past the client's wall-clock
    # timeout. The main loop is the SOLE poller of `agent_id`; workers either
    # don't poll (openai path) or poll their own private inbox (claude path),
    # so no worker can drain a request out from under the main loop.
    concurrency = max(1, int(os.environ.get("LLM_ROUTER_CONCURRENCY", "8")))
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="llm-router")
    log.info("llm-router dispatching with up to %d concurrent workers", concurrency)

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
            executor.submit(dispatch, msg)


def _handle(msg, bus, agent_id, claude_agent, get_openai_client, url, token):
    req = json.loads(msg["content"])
    provider = (req.get("provider") or "ollama").lower()
    model = req.get("model") or "llama3"
    system = req.get("system") or ""
    messages = req.get("messages") or []
    tools = req.get("tools") or []
    max_tokens = req.get("max_tokens") or 8192
    thread_id = msg.get("thread_id") or str(uuid4())
    sender = msg.get("from")
    msg_id = msg.get("id")

    log.info("llm_request from=%s provider=%s model=%s", sender, provider, model)

    if provider == "claude":
        # Forward to Claude CLI agent; relay the reply back to the original sender.
        # Poll on a PRIVATE inbox (unique `from`), not the router's main agent_id —
        # otherwise this blocking wait would drain (and discard) other analysts'
        # incoming llm_requests while we sit here waiting on Claude.
        fwd_inbox = f"{agent_id}-fwd-{uuid4().hex[:12]}"
        fwd_thread = str(uuid4())
        send_result = bus("send_message", {
            "from": fwd_inbox,
            "to": claude_agent,
            "type": "llm_request",
            "thread_id": fwd_thread,
            "content": msg["content"],
        })
        fwd_id = send_result.get("message_id")  # send_message returns message_id, not id

        # Wait for Claude's reply
        deadline = time.monotonic() + 180
        reply_content = None
        reply_type = "llm_response"
        while time.monotonic() < deadline:
            res = _bus_call(url, token, "wait_for_message", {
                "agent_id": fwd_inbox, "timeout_seconds": 25
            })
            for m in res.get("messages", []):
                if (fwd_id and m.get("reply_to") == fwd_id) or m.get("thread_id") == fwd_thread:
                    reply_content = m.get("content", "{}")
                    reply_type = m.get("type", "llm_response")
                    break
            if reply_content is not None:
                break

        if reply_content is None:
            raise TimeoutError("Claude agent did not respond in 180s")

        bus("send_message", {
            "from": agent_id,
            "to": sender,
            "type": reply_type,
            "thread_id": thread_id,
            "reply_to": msg_id,
            "content": reply_content,
        })
        return

    # OpenAI-compatible dispatch (Ollama, OpenAI, xAI, Grok, DeepSeek)
    client = get_openai_client(provider, model)
    oai_messages = []
    if system:
        oai_messages.append({"role": "system", "content": system})
    oai_messages.extend(_anthropic_to_openai_messages(messages))
    oai_tools = _anthropic_to_openai_tools(tools) if tools else []

    kwargs: dict = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": max_tokens,
    }
    if oai_tools:
        kwargs["tools"] = oai_tools
        kwargs["tool_choice"] = "auto"

    completion = client.chat.completions.create(**kwargs)
    response = _openai_response_to_common(completion.choices[0])

    bus("send_message", {
        "from": agent_id,
        "to": sender,
        "type": "llm_response",
        "thread_id": thread_id,
        "reply_to": msg_id,
        "content": json.dumps(response),
    })


if __name__ == "__main__":
    main()

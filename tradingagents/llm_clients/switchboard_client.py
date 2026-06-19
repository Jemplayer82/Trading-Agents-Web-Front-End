"""Switchboard LLM client — routes all LLM calls through the mcp-switchboard bus.

The bus acts as the gateway: SwitchboardChatModel sends an `llm_request` DM
to a registered handler (e.g. a running Claude CLI via hook, or llm_router.py
for Ollama/OpenAI), and waits for an `llm_response` DM back.

No Anthropic/OpenAI SDK calls here — TradingAgents is purely the bus sender.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterator, List, Optional
from uuid import uuid4

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from pydantic import Field, PrivateAttr

from .base_client import BaseLLMClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SSE / JSON response parser (copy of web/bus.py pattern; no cross-package dep)
# ---------------------------------------------------------------------------

def _parse_sse(resp: httpx.Response) -> dict:
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/json"):
        return resp.json()
    text = resp.text
    last_data: str | None = None
    for line in text.splitlines():
        if line.startswith("data:"):
            last_data = line[len("data:"):].strip()
    if last_data is None:
        raise RuntimeError(f"No data: line in SSE body: {text!r}")
    return json.loads(last_data)


# ---------------------------------------------------------------------------
# Message format conversion  (LangChain → Anthropic wire format)
# ---------------------------------------------------------------------------

def _to_anthropic_format(messages: list[BaseMessage]) -> tuple[str, list[dict]]:
    """Convert LangChain messages to (system_str, [Anthropic-format dicts]).

    Anthropic needs system extracted as a top-level param. Tool results
    must be bundled in the user role as content blocks.
    """
    system = ""
    out: list[dict] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            system = str(msg.content)
        elif isinstance(msg, HumanMessage):
            out.append({"role": "user", "content": str(msg.content)})
        elif isinstance(msg, AIMessage):
            if msg.tool_calls:
                blocks: list[dict] = []
                if msg.content:
                    blocks.append({"type": "text", "text": str(msg.content)})
                for tc in msg.tool_calls:
                    tc_id   = tc.get("id")   if isinstance(tc, dict) else tc.id
                    tc_name = tc.get("name") if isinstance(tc, dict) else tc.name
                    tc_args = tc.get("args") if isinstance(tc, dict) else tc.args
                    blocks.append({
                        "type": "tool_use",
                        "id": tc_id or str(uuid4()),
                        "name": tc_name,
                        "input": tc_args or {},
                    })
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "assistant", "content": str(msg.content)})
        elif isinstance(msg, ToolMessage):
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": str(msg.content),
                }],
            })
    return system, out


def _format_tools(tools: list) -> list[dict]:
    """Convert LangChain BaseTool list to Anthropic tool schema format."""
    result = []
    for t in tools:
        schema: dict = {}
        if hasattr(t, "args_schema") and t.args_schema:
            try:
                schema = t.args_schema.schema()
            except Exception:
                pass
        result.append({
            "name": getattr(t, "name", str(t)),
            "description": getattr(t, "description", "") or "",
            "input_schema": schema,
        })
    return result


# ---------------------------------------------------------------------------
# SwitchboardChatModel — LangChain BaseChatModel backed by the bus
# ---------------------------------------------------------------------------

class SwitchboardChatModel(BaseChatModel):
    """Routes .invoke() calls through the mcp-switchboard bus.

    Sends an llm_request DM to ``target_agent_id`` and polls for the
    llm_response reply. The external handler (Claude CLI + hook, or
    llm_router.py for Ollama/OpenAI) does the actual model call.
    """

    bus_url: str = Field(default="")
    token: str = Field(default="")  # pragma: allowlist secret
    self_agent_id: str = Field(default="tradingagents-llm")
    target_agent_id: str = Field(default="llm-router")
    model_name: str = Field(default="claude-sonnet-4-6")
    timeout_s: float = Field(default=180.0)

    _registered: bool = PrivateAttr(default=False)

    def __init__(self, **data):
        if not data.get("bus_url"):
            data["bus_url"] = os.environ.get("SWITCHBOARD_URL", "")
        if not data.get("token"):
            data["token"] = os.environ.get("SWITCHBOARD_MCP_TOKEN", "")
        if not data.get("target_agent_id") or data.get("target_agent_id") == "llm-router":
            env_agent = os.environ.get("SWITCHBOARD_TARGET_AGENT", "")
            if env_agent:
                data["target_agent_id"] = env_agent
        super().__init__(**data)

    @property
    def _llm_type(self) -> str:
        return "switchboard"

    def bind_tools(self, tools, **kwargs):
        formatted = _format_tools(tools)
        return super().bind(tools=formatted, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        if not self.bus_url or not self.token:
            raise RuntimeError(
                "SwitchboardChatModel: SWITCHBOARD_URL and SWITCHBOARD_MCP_TOKEN must be set"
            )

        system, anthro_msgs = _to_anthropic_format(messages)
        tools: list[dict] = kwargs.get("tools", [])
        thread_id = str(uuid4())

        self._lazy_register()

        payload = json.dumps({
            "model": self.model_name,
            "system": system,
            "messages": anthro_msgs,
            "tools": tools,
            "max_tokens": 8192,
        })

        send_result = self._bus_call("send_message", {
            "from": self.self_agent_id,
            "to": self.target_agent_id,
            "type": "llm_request",
            "thread_id": thread_id,
            "content": payload,
        })
        sent_id: int | None = send_result.get("id")

        # Poll for response
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            poll_s = min(25.0, max(1.0, remaining))
            try:
                result = self._bus_call("wait_for_message", {
                    "agent_id": self.self_agent_id,
                    "timeout_seconds": int(poll_s),
                })
            except Exception as exc:
                log.warning("SwitchboardChatModel: bus poll error: %s", exc)
                time.sleep(1.0)
                continue

            for msg in result.get("messages", []):
                if (sent_id and msg.get("reply_to") == sent_id) or (
                    msg.get("thread_id") == thread_id
                ):
                    return self._parse_response(msg)

        raise TimeoutError(
            f"SwitchboardChatModel: no response from '{self.target_agent_id}' "
            f"after {self.timeout_s:.0f}s (thread_id={thread_id})"
        )

    def _parse_response(self, msg: dict) -> ChatResult:
        if msg.get("type") == "llm_error":
            try:
                err = json.loads(msg["content"]).get("error", "unknown error")
            except Exception:
                err = str(msg.get("content", "unknown"))
            raise RuntimeError(f"LLM router error: {err}")

        try:
            resp = json.loads(msg["content"])
        except (json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(f"Bad llm_response payload: {exc}") from exc

        content = resp.get("content") or ""
        raw_tcs = resp.get("tool_calls", [])

        from langchain_core.messages.tool import ToolCall
        tool_calls = []
        for tc in raw_tcs:
            tool_calls.append(ToolCall(
                id=tc.get("id") or str(uuid4()),
                name=tc["name"],
                args=tc.get("args") or tc.get("input") or {},
            ))

        ai_msg = AIMessage(content=content, tool_calls=tool_calls)
        return ChatResult(generations=[ChatGeneration(message=ai_msg)])

    def _bus_call(self, tool: str, args: dict) -> dict:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        resp = httpx.post(
            self.bus_url.rstrip("/") + "/mcp",
            json=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=35.0,
        )
        resp.raise_for_status()
        rpc = _parse_sse(resp)
        if rpc.get("error"):
            err = rpc["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"Bus JSON-RPC error: {msg}")
        result = rpc.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or []
            text = content[0].get("text", "") if content else str(result)
            raise RuntimeError(f"Bus tool error: {text}")
        content = result.get("content") or []
        if not content:
            return {}
        return json.loads(content[0].get("text", "{}"))

    def _lazy_register(self) -> None:
        if self._registered:
            return
        try:
            self._bus_call("register_agent", {
                "agent_id": self.self_agent_id,
                "name": "TradingAgents LLM Client",
            })
            self._registered = True
        except Exception as exc:
            log.debug("SwitchboardChatModel: register_agent skipped: %s", exc)


# ---------------------------------------------------------------------------
# SwitchboardLLMClient — BaseLLMClient wrapper for the factory
# ---------------------------------------------------------------------------

class SwitchboardLLMClient(BaseLLMClient):
    """BaseLLMClient that creates a SwitchboardChatModel.

    Reads SWITCHBOARD_URL, SWITCHBOARD_MCP_TOKEN, SWITCHBOARD_TARGET_AGENT
    from env vars (already present in docker-compose.yml for the api/portfolio
    services).
    """

    def get_llm(self) -> SwitchboardChatModel:
        return SwitchboardChatModel(
            bus_url=os.environ.get("SWITCHBOARD_URL", ""),
            token=os.environ.get("SWITCHBOARD_MCP_TOKEN", ""),  # pragma: allowlist secret
            target_agent_id=os.environ.get("SWITCHBOARD_TARGET_AGENT", "llm-router"),
            model_name=self.model,
            timeout_s=float(self.kwargs.get("timeout_s", 180)),
        )

    def validate_model(self) -> bool:
        return True  # model is validated by the external handler, not locally

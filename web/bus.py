"""Switchboard MCP client and resilient fire-and-forget publisher.

SwitchboardClient — raw JSON-RPC over httpx (no MCP SDK).
BusPublisher — daemon-thread queue with circuit breaker; never raises, never blocks callers.
get_publisher() — lazy singleton gated on env vars.

Wire protocol notes:
    - The switchboard server (Node @modelcontextprotocol/sdk StreamableHTTPServerTransport,
      stateless mode, enableJsonResponse=false) responds to each POST with a short
      text/event-stream body of the form:
          event: message
          data: {"jsonrpc":"2.0","id":1,"result":{...}}
    - Stateless mode requires NO initialize handshake — a bare tools/call POST works.
    - Headers Authorization + Content-Type + Accept are mandatory (406/415 otherwise).
    - Tool results double-wrap JSON: result.content[0].text holds the actual payload.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class BusError(Exception):
    """Raised on JSON-RPC level errors or tool-level isError responses."""


# ---------------------------------------------------------------------------
# Internal SSE / JSON response parser
# ---------------------------------------------------------------------------


def _parse_mcp_response(resp: httpx.Response) -> dict:
    """Parse an MCP server response into a raw JSON-RPC dict.

    The switchboard's StreamableHTTPServerTransport (stateless mode) always
    replies with text/event-stream, but we handle application/json too so the
    client works against any compliant MCP server.

    For SSE: scan lines for ``data: ...`` prefixes, take the LAST one
    (multiple data events can appear in one response body; the last is
    what counts for stateless RPC).
    """
    ct = resp.headers.get("content-type", "")
    if ct.startswith("application/json"):
        return resp.json()

    # SSE path — parse text body, take last data: line
    text = resp.text
    last_data: str | None = None
    for line in text.splitlines():
        if line.startswith("data:"):
            last_data = line[len("data:"):].strip()
    if last_data is None:
        raise BusError(f"No data: line in SSE response body: {text!r}")
    return json.loads(last_data)


# ---------------------------------------------------------------------------
# Switchboard client
# ---------------------------------------------------------------------------


class SwitchboardClient:
    """Minimal synchronous JSON-RPC client for the switchboard MCP server.

    All calls go to POST /mcp with the standard JSON-RPC 2.0 envelope.
    Responses are unwrapped two levels deep:
        JSON-RPC result → MCP content block → JSON-encoded tool payload.
    """

    def __init__(self, url: str, token: str, timeout: float = 3.0) -> None:
        self._http = httpx.Client(
            base_url=url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    def call(self, tool: str, args: dict) -> dict | list:
        """Invoke a switchboard tool and return the decoded payload.

        Raises:
            httpx.HTTPStatusError — on non-2xx HTTP status.
            BusError — on JSON-RPC error or tool-level isError.
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        resp = self._http.post("/mcp", json=body)
        resp.raise_for_status()
        rpc = _parse_mcp_response(resp)

        # JSON-RPC protocol error
        if rpc.get("error"):
            err = rpc["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise BusError(f"JSON-RPC error: {msg}")

        result = rpc.get("result") or {}

        # Tool-level application error
        if result.get("isError"):
            content = result.get("content") or []
            text = content[0].get("text", "") if content else str(result)
            raise BusError(f"Tool error: {text}")

        # Unwrap double-encoded JSON
        content = result.get("content") or []
        if not content:
            return {}
        raw_text = content[0].get("text", "{}")
        return json.loads(raw_text)

    # ------------------------------------------------------------------
    # Typed helpers — argument names match tools.js inputSchema exactly
    # ------------------------------------------------------------------

    def register_agent(
        self,
        agent_id: str,
        name: str,
        capabilities: list[str] | None = None,
    ) -> dict:
        """Register or refresh an agent on the bus (idempotent)."""
        args: dict[str, Any] = {"agent_id": agent_id, "name": name}
        if capabilities is not None:
            args["capabilities"] = capabilities
        return self.call("register_agent", args)

    def create_channel(self, channel_id: str, name: str | None = None) -> dict:
        """Create a channel (idempotent)."""
        args: dict[str, Any] = {"channel_id": channel_id}
        if name is not None:
            args["name"] = name
        return self.call("create_channel", args)

    def send_message(
        self,
        from_agent: str,
        content: str,
        *,
        to: str | None = None,
        channel_id: str | None = None,
        type: str = "chat",
        thread_id: str | None = None,
        reply_to: int | None = None,
    ) -> dict:
        """Send a direct message or channel broadcast.

        Exactly one of ``to`` or ``channel_id`` must be provided (enforced
        server-side; we pass both if given and let the server error).

        The wire field for the sender is ``from`` (a Python reserved keyword),
        so we build the args dict manually.
        """
        args: dict[str, Any] = {"from": from_agent, "content": content, "type": type}
        if to is not None:
            args["to"] = to
        if channel_id is not None:
            args["channel_id"] = channel_id
        if thread_id is not None:
            args["thread_id"] = thread_id
        if reply_to is not None:
            args["reply_to"] = reply_to
        return self.call("send_message", args)

    def set_status(
        self, agent_id: str, activity: str, detail: str | None = None
    ) -> dict:
        """Self-report current activity for the awareness layer."""
        args: dict[str, Any] = {"agent_id": agent_id, "activity": activity}
        if detail is not None:
            args["detail"] = detail
        return self.call("set_status", args)

    def get_messages(
        self,
        agent_id: str,
        *,
        channel_id: str | None = None,
        since_id: int | None = None,
        limit: int = 50,
        peek: bool = False,
    ) -> dict:
        """Non-blocking read of this agent's inbox (or a channel history slice)."""
        args: dict[str, Any] = {"agent_id": agent_id, "limit": limit, "peek": peek}
        if channel_id is not None:
            args["channel_id"] = channel_id
        if since_id is not None:
            args["since_id"] = since_id
        return self.call("get_messages", args)

    def list_channels(self) -> dict:
        """List all channels with member counts."""
        return self.call("list_channels", {})

    def wait_for_message(self, agent_id: str, timeout_s: float = 25) -> dict:
        """Long-poll: blocks up to ``timeout_s`` seconds for a message.

        Uses a per-request timeout slightly longer than the server's own
        long-poll window to avoid a spurious httpx.ReadTimeout.
        """
        # Give httpx 5s more headroom beyond the server's long-poll window
        per_req_timeout = timeout_s + 5.0
        args: dict[str, Any] = {"agent_id": agent_id, "timeout_seconds": int(timeout_s)}
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "wait_for_message", "arguments": args},
        }
        resp = self._http.post("/mcp", json=body, timeout=per_req_timeout)
        resp.raise_for_status()
        rpc = _parse_mcp_response(resp)
        if rpc.get("error"):
            err = rpc["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise BusError(f"JSON-RPC error: {msg}")
        result = rpc.get("result") or {}
        if result.get("isError"):
            content = result.get("content") or []
            text = content[0].get("text", "") if content else str(result)
            raise BusError(f"Tool error: {text}")
        content = result.get("content") or []
        if not content:
            return {}
        return json.loads(content[0].get("text", "{}"))


# ---------------------------------------------------------------------------
# Circuit breaker state
# ---------------------------------------------------------------------------

_CB_CLOSED = "closed"
_CB_OPEN = "open"
_CB_HALF_OPEN = "half_open"

_CB_FAILURE_THRESHOLD = 3
_CB_DEFAULT_OPEN_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Resilient publisher
# ---------------------------------------------------------------------------


class BusPublisher:
    """Fire-and-forget publisher with a bounded queue and circuit breaker.

    Runs a single daemon thread that drains a bounded queue of
    ``(tool, args)`` tuples.  The circuit breaker silently drops items when
    the switchboard is unreachable so the hot analysis path is never impacted.

    Design constraints (from task spec):
    - ``publish()`` never blocks, never raises.
    - The worker thread never propagates exceptions or terminates.
    - ``flush()`` returns when the queue is empty or the timeout expires.
    - Drop counter is public so callers can observe backpressure.
    """

    def __init__(
        self,
        client: SwitchboardClient,
        *,
        _queue_maxsize: int = 200,
        _breaker_open_seconds: float = _CB_DEFAULT_OPEN_SECONDS,
    ) -> None:
        self._client = client
        self._queue: queue.Queue[tuple[str, dict] | None] = queue.Queue(
            maxsize=_queue_maxsize
        )
        self._breaker_open_seconds = _breaker_open_seconds
        self.drop_count: int = 0
        self._drop_lock = threading.Lock()

        # Circuit breaker state (accessed only from worker thread — no lock needed)
        self._cb_state: str = _CB_CLOSED
        self._cb_failures: int = 0
        self._cb_opened_at: float = 0.0

        self._thread = threading.Thread(target=self._worker, daemon=True, name="bus-publisher")
        self._thread.start()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def publish(self, tool: str, args: dict) -> None:
        """Enqueue a tool call.  Drops silently if the queue is full."""
        try:
            self._queue.put_nowait((tool, args))
        except queue.Full:
            with self._drop_lock:
                self.drop_count += 1
                if self.drop_count % 50 == 0:
                    log.warning(
                        "bus publisher queue full — %d total drops", self.drop_count
                    )

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue is empty or ``timeout`` seconds elapse.

        Returns True if the queue drained, False if the timeout was reached.
        Any remaining items past the deadline are not discarded — they stay
        in the queue for the worker to process eventually.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._queue.empty():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """Drain the queue forever.  Never raises, never exits."""
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                # Sentinel — put it back so flush() sees an empty queue
                # and re-enqueue for any future drain attempt.
                # (In practice we never send None; this is defensive.)
                continue
            tool, args = item
            try:
                self._dispatch(tool, args)
            except Exception:
                pass  # _dispatch swallows and handles; this is a last resort
            finally:
                self._queue.task_done()

    def _dispatch(self, tool: str, args: dict) -> None:
        """Call the tool through the circuit breaker.  All exceptions logged."""
        now = time.monotonic()

        # ---- circuit breaker state machine ----
        if self._cb_state == _CB_OPEN:
            elapsed = now - self._cb_opened_at
            if elapsed < self._breaker_open_seconds:
                # Still open — drop silently
                return
            # Transition to half-open for a probe
            self._cb_state = _CB_HALF_OPEN
            log.debug("bus circuit breaker: half-open (probe)")

        try:
            self._client.call(tool, args)
        except Exception as exc:
            log.warning("bus publish failed (%s %s): %s", tool, args, exc)
            self._cb_failures += 1
            if self._cb_state == _CB_HALF_OPEN:
                # Probe failed — re-open for another full window
                self._cb_state = _CB_OPEN
                self._cb_opened_at = time.monotonic()
                log.warning("bus circuit breaker: re-opened after half-open probe failure")
            elif self._cb_failures >= _CB_FAILURE_THRESHOLD:
                self._cb_state = _CB_OPEN
                self._cb_opened_at = time.monotonic()
                log.warning(
                    "bus circuit breaker: opened after %d consecutive failures",
                    self._cb_failures,
                )
        else:
            if self._cb_state == _CB_HALF_OPEN:
                log.info("bus circuit breaker: closed (probe succeeded)")
            self._cb_state = _CB_CLOSED
            self._cb_failures = 0


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_publisher_lock = threading.Lock()
_publisher_instance: BusPublisher | None = None


def get_publisher() -> BusPublisher | None:
    """Return the process-global BusPublisher, or None if env vars are unset.

    Lazy-initialised on first call.  Thread-safe.  Missing env vars → None,
    which callers treat as "bus unavailable — skip mirroring".
    """
    global _publisher_instance

    url = os.environ.get("SWITCHBOARD_URL")
    token = os.environ.get("SWITCHBOARD_MCP_TOKEN")
    if not url or not token:
        return None

    if _publisher_instance is not None:
        return _publisher_instance

    with _publisher_lock:
        # Double-checked locking
        if _publisher_instance is not None:
            return _publisher_instance
        client = SwitchboardClient(url=url, token=token)
        _publisher_instance = BusPublisher(client)
        log.info("bus publisher initialised (url=%s)", url)
        return _publisher_instance

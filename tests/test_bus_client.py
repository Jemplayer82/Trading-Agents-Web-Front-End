"""Unit tests for web/bus.py — SwitchboardClient, BusPublisher, and helpers.

No network access, no Docker. All tests run with `uv run pytest tests/test_bus_client.py -v`.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers to build fake httpx.Response objects
# ---------------------------------------------------------------------------


def _make_response(content: str | bytes, content_type: str, status_code: int = 200) -> httpx.Response:
    """Build a minimal httpx.Response without a real network round-trip."""
    if isinstance(content, str):
        content = content.encode()
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": content_type},
        content=content,
    )


# ---------------------------------------------------------------------------
# 1. _parse_mcp_response — SSE body (single data line)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseMcpResponseSSE:
    def test_single_data_line_parsed(self):
        from web.bus import _parse_mcp_response

        payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{}"}]}}
        sse_body = f'event: message\ndata: {json.dumps(payload)}\n\n'
        resp = _make_response(sse_body, "text/event-stream")
        result = _parse_mcp_response(resp)
        assert result == payload

    def test_multi_data_line_last_wins(self):
        """The last `data:` line in an SSE body is what matters."""
        from web.bus import _parse_mcp_response

        first = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": '"first"'}]}}
        last = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": '"last"'}]}}
        sse_body = (
            f'event: message\ndata: {json.dumps(first)}\n'
            f'event: message\ndata: {json.dumps(last)}\n\n'
        )
        resp = _make_response(sse_body, "text/event-stream")
        result = _parse_mcp_response(resp)
        assert result == last

    def test_content_type_with_charset_suffix(self):
        """text/event-stream; charset=utf-8 should still be treated as SSE."""
        from web.bus import _parse_mcp_response

        payload = {"jsonrpc": "2.0", "id": 1, "result": {}}
        sse_body = f'data: {json.dumps(payload)}\n\n'
        resp = _make_response(sse_body, "text/event-stream; charset=utf-8")
        result = _parse_mcp_response(resp)
        assert result == payload


# ---------------------------------------------------------------------------
# 2. _parse_mcp_response — plain application/json body
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseMcpResponseJSON:
    def test_plain_json_body(self):
        from web.bus import _parse_mcp_response

        payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": '{"hello": 1}'}]}}
        resp = _make_response(json.dumps(payload), "application/json")
        result = _parse_mcp_response(resp)
        assert result == payload

    def test_json_with_charset_suffix(self):
        from web.bus import _parse_mcp_response

        payload = {"jsonrpc": "2.0", "id": 1, "result": {}}
        resp = _make_response(json.dumps(payload), "application/json; charset=utf-8")
        result = _parse_mcp_response(resp)
        assert result == payload


# ---------------------------------------------------------------------------
# 3. SwitchboardClient.call — happy path: unwraps double-encoded JSON
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSwitchboardClientCall:
    def _make_client(self, transport: httpx.MockTransport) -> "SwitchboardClient":
        from web.bus import SwitchboardClient

        # Bypass the normal httpx.Client construction so we can inject transport
        client = SwitchboardClient.__new__(SwitchboardClient)
        client._http = httpx.Client(
            base_url="http://switchboard:3107",
            transport=transport,
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        return client

    def test_unwraps_double_encoded_json(self):
        """result.content[0].text holds JSON-encoded data; call() must parse it."""
        inner = {"ok": True, "message_id": 42}
        rpc_result = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(inner)}]
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(rpc_result).encode(),
            )

        client = self._make_client(httpx.MockTransport(handler))
        result = client.call("send_message", {"from": "me", "content": "hi", "channel_id": "general"})
        assert result == inner

    def test_unwraps_double_encoded_json_via_sse(self):
        """Same unwrapping via SSE transport."""
        inner = {"agents": []}
        rpc_result = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": json.dumps(inner)}]
            },
        }
        sse_body = f'event: message\ndata: {json.dumps(rpc_result)}\n\n'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body.encode(),
            )

        client = self._make_client(httpx.MockTransport(handler))
        result = client.call("list_agents", {})
        assert result == inner


# ---------------------------------------------------------------------------
# 4. call() raises BusError on isError and on JSON-RPC error
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSwitchboardClientErrors:
    def _make_client_with_rpc(self, rpc_result: dict) -> "SwitchboardClient":
        from web.bus import SwitchboardClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(rpc_result).encode(),
            )

        client = SwitchboardClient.__new__(SwitchboardClient)
        client._http = httpx.Client(
            base_url="http://switchboard:3107",
            transport=httpx.MockTransport(handler),
        )
        return client

    def test_raises_bus_error_on_is_error(self):
        from web.bus import BusError

        rpc_result = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "Provide exactly one of `to` or `channel_id`."}],
            },
        }
        client = self._make_client_with_rpc(rpc_result)
        with pytest.raises(BusError, match="Provide exactly one"):
            client.call("send_message", {})

    def test_raises_bus_error_on_jsonrpc_error(self):
        from web.bus import BusError

        rpc_result = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
        client = self._make_client_with_rpc(rpc_result)
        with pytest.raises(BusError, match="Method not found"):
            client.call("nonexistent_tool", {})

    def test_raises_on_http_error(self):
        from web.bus import SwitchboardClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(406, content=b"Not Acceptable")

        client = SwitchboardClient.__new__(SwitchboardClient)
        client._http = httpx.Client(
            base_url="http://switchboard:3107",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.call("any_tool", {})


# ---------------------------------------------------------------------------
# 5. BusPublisher — drop-on-overflow
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBusPublisherDropOnOverflow:
    def _make_publisher(self, maxsize: int = 3) -> "BusPublisher":
        """Build a BusPublisher with an artificially tiny queue and a stalled client."""
        from web.bus import BusPublisher

        mock_client = MagicMock()
        # Make the client block indefinitely so the queue fills up
        block_event = threading.Event()
        mock_client.call.side_effect = lambda *a, **kw: block_event.wait()

        pub = BusPublisher(mock_client, _queue_maxsize=maxsize)
        return pub, block_event

    def test_publish_does_not_block_when_full(self):
        pub, block_event = self._make_publisher(maxsize=2)
        try:
            # Fill queue plus one more — should not raise, not block
            for _ in range(5):
                start = time.monotonic()
                pub.publish("test_tool", {"x": 1})
                elapsed = time.monotonic() - start
                assert elapsed < 0.1, "publish() blocked — queue should drop, not block"
        finally:
            block_event.set()
            pub.flush(timeout=1.0)

    def test_drops_are_counted(self):
        pub, block_event = self._make_publisher(maxsize=2)
        try:
            # One item will be consumed by the worker (it blocks), 2 fill the queue
            # remaining are drops
            for _ in range(6):
                pub.publish("test_tool", {"x": 1})
            # Give worker thread a moment to consume one
            time.sleep(0.05)
            assert pub.drop_count > 0
        finally:
            block_event.set()
            pub.flush(timeout=1.0)

    def test_flush_waits_for_queue_to_drain(self):
        from web.bus import BusPublisher

        mock_client = MagicMock()
        mock_client.call.return_value = {"ok": True}

        pub = BusPublisher(mock_client, _queue_maxsize=10)
        for i in range(3):
            pub.publish("heartbeat", {"agent_id": f"test-{i}"})

        # flush should drain within 2 seconds with a fast mock
        drained = pub.flush(timeout=2.0)
        assert drained is True


# ---------------------------------------------------------------------------
# 6. Circuit breaker transitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCircuitBreaker:
    def _make_publisher(self, mock_client: MagicMock) -> "BusPublisher":
        from web.bus import BusPublisher
        return BusPublisher(mock_client, _queue_maxsize=50)

    def test_three_failures_open_breaker(self):
        """After 3 consecutive failures the breaker opens; subsequent calls are skipped."""
        from web.bus import BusPublisher

        call_count = [0]

        def failing_call(tool, args):
            call_count[0] += 1
            raise ConnectionError("switchboard down")

        mock_client = MagicMock()
        mock_client.call.side_effect = failing_call

        pub = self._make_publisher(mock_client)

        # Publish 3 items — these should trigger the 3 failures that open the breaker
        for _ in range(3):
            pub.publish("heartbeat", {"agent_id": "test"})

        # Wait for worker to process them
        time.sleep(0.2)

        calls_before = call_count[0]

        # Publish more — these should be silently dropped by open breaker
        for _ in range(5):
            pub.publish("heartbeat", {"agent_id": "test"})

        time.sleep(0.2)
        calls_after = call_count[0]

        # The breaker should have opened after 3 failures; subsequent items dropped without calling
        assert calls_before >= 3, "Expected at least 3 calls before breaker opened"
        assert calls_after == calls_before, "Breaker should have stopped further calls"

        pub.flush(timeout=1.0)

    def test_half_open_probe_success_closes_breaker(self):
        """After the open window expires, the next item is a probe. Success closes the breaker."""
        from web.bus import BusPublisher

        fail_until = [True]
        call_count = [0]

        def sometimes_failing(tool, args):
            call_count[0] += 1
            if fail_until[0]:
                raise ConnectionError("down")
            return {"ok": True}

        mock_client = MagicMock()
        mock_client.call.side_effect = sometimes_failing

        # Use a long open window so we can control when it expires via the flag
        pub = BusPublisher(mock_client, _queue_maxsize=50, _breaker_open_seconds=60.0)

        # Trip the breaker with 3 failures
        for _ in range(3):
            pub.publish("heartbeat", {"agent_id": "test"})
        # Wait for the 3 failures to be processed
        deadline = time.monotonic() + 2.0
        while call_count[0] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert call_count[0] >= 3, "Expected 3 calls to trip the breaker"
        time.sleep(0.05)  # let the breaker open

        calls_at_open = call_count[0]

        # Force the open window to expire by backdating the open timestamp
        pub._cb_opened_at = time.monotonic() - 61.0  # past the 60s window

        # Allow the probe to succeed
        fail_until[0] = False

        # Next publish is the half-open probe
        pub.publish("heartbeat", {"agent_id": "test"})
        deadline = time.monotonic() + 1.0
        while call_count[0] == calls_at_open and time.monotonic() < deadline:
            time.sleep(0.01)

        probe_calls = call_count[0]
        assert probe_calls > calls_at_open, "Probe should have been attempted"

        # Probe succeeded; breaker should be closed — further publishes go through
        pub.publish("heartbeat", {"agent_id": "test"})
        deadline = time.monotonic() + 1.0
        while call_count[0] == probe_calls and time.monotonic() < deadline:
            time.sleep(0.01)

        assert call_count[0] > probe_calls, "Follow-on call should have gone through (breaker closed)"
        pub.flush(timeout=1.0)

    def test_half_open_probe_failure_reopens_breaker(self):
        """Half-open probe failure should re-open the breaker for another window."""
        from web.bus import BusPublisher

        call_count = [0]

        def always_fail(tool, args):
            call_count[0] += 1
            raise ConnectionError("still down")

        mock_client = MagicMock()
        mock_client.call.side_effect = always_fail

        # Long open window — we'll expire it manually
        pub = BusPublisher(mock_client, _queue_maxsize=50, _breaker_open_seconds=60.0)

        # Trip the breaker
        for _ in range(3):
            pub.publish("heartbeat", {"agent_id": "test"})
        deadline = time.monotonic() + 2.0
        while call_count[0] < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert call_count[0] >= 3
        time.sleep(0.05)
        calls_after_trip = call_count[0]

        # Force the open window to expire
        pub._cb_opened_at = time.monotonic() - 61.0

        # Probe — will fail, re-opening the breaker
        pub.publish("heartbeat", {"agent_id": "test"})
        deadline = time.monotonic() + 1.0
        while call_count[0] == calls_after_trip and time.monotonic() < deadline:
            time.sleep(0.01)

        probe_calls = call_count[0]
        assert probe_calls > calls_after_trip, "Probe should have been attempted"
        time.sleep(0.05)  # let the breaker re-open

        # These should be dropped (breaker re-opened for full window)
        for _ in range(5):
            pub.publish("heartbeat", {"agent_id": "test"})
        time.sleep(0.15)

        assert call_count[0] == probe_calls, "Should be dropped after probe failure re-opened breaker"
        pub.flush(timeout=1.0)


# ---------------------------------------------------------------------------
# 7. get_publisher — env var behaviour + singleton
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetPublisher:
    def _reset_singleton(self):
        """Clear the module-level singleton between tests."""
        import web.bus as bus_mod
        with bus_mod._publisher_lock:
            bus_mod._publisher_instance = None

    def test_returns_none_without_env_vars(self, monkeypatch):
        monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
        monkeypatch.delenv("SWITCHBOARD_MCP_TOKEN", raising=False)
        self._reset_singleton()

        from web.bus import get_publisher
        assert get_publisher() is None

    def test_returns_none_with_only_url(self, monkeypatch):
        monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard:3107")
        monkeypatch.delenv("SWITCHBOARD_MCP_TOKEN", raising=False)
        self._reset_singleton()

        from web.bus import get_publisher
        assert get_publisher() is None

    def test_returns_none_with_only_token(self, monkeypatch):
        monkeypatch.delenv("SWITCHBOARD_URL", raising=False)
        monkeypatch.setenv("SWITCHBOARD_MCP_TOKEN", "secret-token")
        self._reset_singleton()

        from web.bus import get_publisher
        assert get_publisher() is None

    def test_returns_publisher_with_both_env_vars(self, monkeypatch):
        from web.bus import BusPublisher

        monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard:3107")
        monkeypatch.setenv("SWITCHBOARD_MCP_TOKEN", "secret-token")
        self._reset_singleton()

        from web.bus import get_publisher
        pub = get_publisher()
        assert isinstance(pub, BusPublisher)

    def test_returns_same_singleton_on_repeated_calls(self, monkeypatch):
        monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard:3107")
        monkeypatch.setenv("SWITCHBOARD_MCP_TOKEN", "secret-token")
        self._reset_singleton()

        from web.bus import get_publisher
        pub1 = get_publisher()
        pub2 = get_publisher()
        assert pub1 is pub2

    def test_singleton_reset_between_tests(self, monkeypatch):
        """Verify _reset_singleton() works — re-create after clear gives a fresh instance."""
        monkeypatch.setenv("SWITCHBOARD_URL", "http://switchboard:3107")
        monkeypatch.setenv("SWITCHBOARD_MCP_TOKEN", "secret-token")
        self._reset_singleton()

        from web.bus import get_publisher
        pub1 = get_publisher()
        self._reset_singleton()
        pub2 = get_publisher()
        assert pub1 is not pub2

"""Tests for the /api/bus WebSocket endpoint and its pure helper functions.

Unit tests cover:
- _pick_latest_analysis_channel (channel picking logic)
- _to_frame (row → wire frame mapping)

Integration tests use fastapi.testclient.TestClient with monkeypatched
web.bus.get_reader returning a controllable fake client.  Auth is bypassed via
X-Internal-Token header with INTERNAL_API_TOKEN set in the environment.
"""
from __future__ import annotations

import json

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(
    id: int,
    from_agent: str = "bull-researcher",
    channel_id: str = "analysis-1",
    content: str = "hello",
    msg_type: str = "chat",
    thread_id: str | None = None,
    created_at: int = 1000000,
) -> dict:
    """Build a minimal bus message row as returned by get_messages."""
    return {
        "id": id,
        "channel_id": channel_id,
        "from": from_agent,
        "to": None,
        "thread_id": thread_id,
        "reply_to": None,
        "type": msg_type,
        "content": content,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPickLatestAnalysisChannel:
    def _pick(self, channels):
        from web.main import _pick_latest_analysis_channel
        return _pick_latest_analysis_channel(channels)

    def test_returns_none_for_empty_list(self):
        assert self._pick([]) is None

    def test_returns_none_when_no_analysis_channels(self):
        channels = [
            {"id": "general", "name": "General", "member_count": 2},
            {"id": "#activity", "name": "Activity", "member_count": 0},
        ]
        assert self._pick(channels) is None

    def test_single_analysis_channel(self):
        channels = [{"id": "analysis-5", "name": "AAPL 2025-01-01", "member_count": 3}]
        assert self._pick(channels) == "analysis-5"

    def test_picks_highest_numeric_suffix(self):
        channels = [
            {"id": "analysis-3", "name": "old", "member_count": 1},
            {"id": "analysis-10", "name": "newest", "member_count": 2},
            {"id": "analysis-9", "name": "recent", "member_count": 2},
        ]
        assert self._pick(channels) == "analysis-10"

    def test_ignores_non_numeric_analysis_channels(self):
        channels = [
            {"id": "analysis-foo", "name": "bad", "member_count": 0},
            {"id": "analysis-2", "name": "good", "member_count": 1},
        ]
        assert self._pick(channels) == "analysis-2"

    def test_ignores_non_analysis_channels_mixed(self):
        channels = [
            {"id": "general", "name": "General", "member_count": 5},
            {"id": "analysis-1", "name": "first", "member_count": 1},
            {"id": "analysis-42", "name": "latest", "member_count": 2},
        ]
        assert self._pick(channels) == "analysis-42"

    def test_9_vs_10_numeric_ordering(self):
        """String ordering would pick 9 over 10; correct numeric ordering picks 10."""
        channels = [
            {"id": "analysis-9", "name": "nine", "member_count": 1},
            {"id": "analysis-10", "name": "ten", "member_count": 1},
        ]
        assert self._pick(channels) == "analysis-10"


@pytest.mark.unit
class TestToFrame:
    def _frame(self, row, channel="analysis-1"):
        from web.main import _to_frame
        return _to_frame(row, channel)

    def test_basic_field_mapping(self):
        row = _make_message(id=7, from_agent="bear-researcher", channel_id="analysis-1")
        frame = self._frame(row, "analysis-1")
        assert frame["type"] == "bus_message"
        assert frame["id"] == 7
        assert frame["agent"] == "bear-researcher"   # 'from' → 'agent'
        assert frame["channel"] == "analysis-1"
        assert frame["content"] == "hello"
        assert frame["ts"] == 1000000

    def test_msg_type_rename(self):
        """Bus 'type' field must become 'msg_type' to not clobber the frame envelope."""
        row = _make_message(id=1, msg_type="result")
        frame = self._frame(row)
        assert frame["msg_type"] == "result"
        assert frame["type"] == "bus_message"   # envelope key unchanged

    def test_thread_id_present(self):
        row = _make_message(id=2, thread_id="investment-debate")
        frame = self._frame(row)
        assert frame["thread_id"] == "investment-debate"

    def test_thread_id_absent_is_none(self):
        row = _make_message(id=3, thread_id=None)
        frame = self._frame(row)
        assert frame["thread_id"] is None

    def test_missing_optional_fields_dont_raise(self):
        """Row with only required fields should not raise."""
        row = {"id": 5, "from": "trader", "type": "chat", "content": "buy", "created_at": 999}
        frame = self._frame(row)
        assert frame["id"] == 5
        assert frame["agent"] == "trader"
        assert frame["thread_id"] is None

    def test_channel_in_frame_matches_arg(self):
        row = _make_message(id=4, channel_id="analysis-7")
        frame = self._frame(row, "analysis-7")
        assert frame["channel"] == "analysis-7"


# ---------------------------------------------------------------------------
# Fake client for endpoint tests
# ---------------------------------------------------------------------------


class FakeClient:
    """Synchronous fake SwitchboardClient. All methods are controllable."""

    def __init__(
        self,
        channels=None,
        messages=None,
        list_channels_exc=None,
        get_messages_exc=None,
    ):
        # list_channels returns {channels: [...]}
        self._channels = channels if channels is not None else [
            {"id": "analysis-1", "name": "AAPL 2025-01-01", "member_count": 2}
        ]
        # get_messages returns {messages: [...]}
        self._messages: list[dict] = messages if messages is not None else []
        self.list_channels_exc = list_channels_exc
        self.get_messages_exc = get_messages_exc
        self.get_messages_calls: list[dict] = []
        self.list_channels_calls: int = 0

    def list_channels(self) -> dict:
        self.list_channels_calls += 1
        if self.list_channels_exc:
            raise self.list_channels_exc
        return {"channels": self._channels}

    def get_messages(
        self,
        agent_id: str,
        *,
        channel_id=None,
        since_id=None,
        limit=50,
        peek=False,
    ) -> dict:
        self.get_messages_calls.append(
            {"agent_id": agent_id, "channel_id": channel_id, "since_id": since_id, "limit": limit, "peek": peek}
        )
        if self.get_messages_exc:
            raise self.get_messages_exc
        return {"messages": list(self._messages), "cursor": self._messages[-1]["id"] if self._messages else since_id}


# ---------------------------------------------------------------------------
# Auth/env fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def internal_token(monkeypatch):
    """Set INTERNAL_API_TOKEN so X-Internal-Token header auth passes."""
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-secret-token")
    monkeypatch.setenv("BUS_POLL_INTERVAL", "0.05")
    return "test-secret-token"


@pytest.fixture()
def reset_reader(monkeypatch):
    """Clear the get_reader singleton between tests."""
    import web.bus as bus_mod
    with bus_mod._reader_lock:
        bus_mod._reader_instance = None
        bus_mod._reader_failed = False
    yield
    with bus_mod._reader_lock:
        bus_mod._reader_instance = None
        bus_mod._reader_failed = False


def _ws_headers(token):
    return {"x-internal-token": token}


# ---------------------------------------------------------------------------
# Endpoint integration tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBusEndpointAuth:
    def test_no_auth_closes_with_4401(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "secret")
        from fastapi.testclient import TestClient

        from web.main import app
        with TestClient(app) as client:
            with pytest.raises(Exception):
                # TestClient raises WebSocketDisconnect / ConnectionClosedError on 4401
                with client.websocket_connect("/api/bus") as ws:
                    ws.receive_json()  # should not reach here

    def test_wrong_internal_token_closes_with_4401(self, monkeypatch):
        # A non-matching X-Internal-Token must be rejected (the auth gate compares
        # with hmac.compare_digest — a wrong token is still a closed socket).
        monkeypatch.setenv("INTERNAL_API_TOKEN", "secret")
        from fastapi.testclient import TestClient

        from web.main import app
        with TestClient(app) as client:
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/api/bus", headers={"x-internal-token": "wrong-token"}
                ) as ws:
                    ws.receive_json()  # should not reach here


@pytest.mark.unit
class TestBusEndpointNotConfigured:
    def test_not_configured_sends_bus_status_and_stays_open(self, internal_token, monkeypatch):
        """When get_reader() returns None: accept, send bus_status not-configured, keep open."""
        import web.bus as bus_mod
        monkeypatch.setattr(bus_mod, "get_reader", lambda: None)

        from fastapi.testclient import TestClient

        from web.main import app

        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/bus", headers=_ws_headers(internal_token)
            ) as ws:
                frame = ws.receive_json()
                assert frame["type"] == "bus_status"
                assert frame["ok"] is False
                assert frame["reason"] == "bus not configured"
                # Socket stays open — we can still receive a ping eventually
                # (but we don't wait 25s in tests; just verify it didn't close)


@pytest.mark.unit
class TestBusEndpointHappyPath:
    def test_channel_then_backfill_then_live_message(self, internal_token, monkeypatch):
        """Happy path: channel frame → backfill frame → live bus_message on next poll."""
        msg1 = _make_message(id=1, from_agent="bull-researcher", content="bullish")
        msg2 = _make_message(id=2, from_agent="bear-researcher", content="bearish")
        fake = FakeClient(
            channels=[{"id": "analysis-1", "name": "AAPL", "member_count": 2}],
            messages=[msg1, msg2],
        )

        # First poll returns both as backfill (since_id=0); subsequent returns empty.
        call_count = [0]
        live_msg = _make_message(id=3, from_agent="research-manager", content="final")

        def patched_get(agent_id, *, channel_id=None, since_id=None, limit=50, peek=False):
            call_count[0] += 1
            if call_count[0] == 1:
                # Backfill call
                return {"messages": [msg1, msg2], "cursor": 2}
            elif call_count[0] == 2:
                # First live poll: return new message
                return {"messages": [live_msg], "cursor": 3}
            else:
                return {"messages": [], "cursor": 3}

        fake.get_messages = patched_get

        import web.bus as bus_mod
        monkeypatch.setattr(bus_mod, "get_reader", lambda: fake)

        from fastapi.testclient import TestClient

        from web.main import app

        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/bus", headers=_ws_headers(internal_token)
            ) as ws:
                # 1. channel announcement
                frame = ws.receive_json()
                assert frame["type"] == "channel"
                assert frame["channel"] == "analysis-1"

                # 2. backfill
                frame = ws.receive_json()
                assert frame["type"] == "backfill"
                assert len(frame["messages"]) == 2
                # Verify _to_frame structure: 'from' → 'agent', 'type' → 'msg_type'
                assert frame["messages"][0]["agent"] == "bull-researcher"
                assert frame["messages"][0]["msg_type"] == "chat"
                assert frame["messages"][1]["agent"] == "bear-researcher"

                # 3. live bus_message
                frame = ws.receive_json()
                assert frame["type"] == "bus_message"
                assert frame["id"] == 3
                assert frame["agent"] == "research-manager"
                assert frame["content"] == "final"

    def test_query_param_channel_skips_list(self, internal_token, monkeypatch):
        """?channel=analysis-5 skips list_channels entirely."""
        fake = FakeClient(
            channels=[{"id": "analysis-99", "name": "other", "member_count": 1}],
            messages=[],
        )

        import web.bus as bus_mod
        monkeypatch.setattr(bus_mod, "get_reader", lambda: fake)

        from fastapi.testclient import TestClient

        from web.main import app

        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/bus?channel=analysis-5", headers=_ws_headers(internal_token)
            ) as ws:
                frame = ws.receive_json()
                assert frame["type"] == "channel"
                assert frame["channel"] == "analysis-5"

                frame = ws.receive_json()
                assert frame["type"] == "backfill"

                # list_channels should NOT have been called
                assert fake.list_channels_calls == 0


@pytest.mark.unit
class TestBusEndpointChannelSwitch:
    def test_client_text_switches_channel(self, internal_token, monkeypatch):
        """Client sending {"channel": "analysis-2"} triggers re-announce + re-backfill."""
        fake = FakeClient(
            channels=[
                {"id": "analysis-1", "name": "first", "member_count": 1},
                {"id": "analysis-2", "name": "second", "member_count": 1},
            ],
            messages=[],
        )

        import web.bus as bus_mod
        monkeypatch.setattr(bus_mod, "get_reader", lambda: fake)

        from fastapi.testclient import TestClient

        from web.main import app

        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/bus", headers=_ws_headers(internal_token)
            ) as ws:
                # Initial channel (analysis-2 is newest)
                frame = ws.receive_json()
                assert frame["type"] == "channel"

                frame = ws.receive_json()
                assert frame["type"] == "backfill"

                # Switch to analysis-1
                ws.send_text(json.dumps({"channel": "analysis-1"}))

                # Should receive new channel frame
                frames = []
                for _ in range(5):
                    try:
                        f = ws.receive_json()
                        frames.append(f)
                        if f["type"] == "channel" and f["channel"] == "analysis-1":
                            break
                    except Exception:
                        break

                channel_frames = [f for f in frames if f["type"] == "channel"]
                assert any(f["channel"] == "analysis-1" for f in channel_frames), \
                    f"Expected channel=analysis-1 in {frames}"


@pytest.mark.unit
class TestBusEndpointOutage:
    def test_outage_sends_bus_status_false_once_then_recovery(self, internal_token, monkeypatch):
        """On bus failure: send bus_status ok:false once per outage; ok:true on recovery."""
        call_count = [0]
        fail_until = [2]  # fail on calls 1 and 2

        fake = FakeClient(
            channels=[{"id": "analysis-1", "name": "AAPL", "member_count": 1}],
            messages=[],
        )

        def patched_list():
            call_count[0] += 1
            if call_count[0] <= fail_until[0]:
                raise ConnectionError("switchboard down")
            return {"channels": [{"id": "analysis-1", "name": "AAPL", "member_count": 1}]}

        fake.list_channels = patched_list

        import web.bus as bus_mod
        monkeypatch.setattr(bus_mod, "get_reader", lambda: fake)
        # Use a very short poll so the test doesn't wait 5s backoff
        monkeypatch.setenv("BUS_POLL_INTERVAL", "0.02")

        from fastapi.testclient import TestClient

        from web.main import app

        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/bus", headers=_ws_headers(internal_token)
            ) as ws:
                # First frame should be bus_status ok:false (outage on list_channels call 1)
                frame = ws.receive_json()
                assert frame["type"] == "bus_status"
                assert frame["ok"] is False

                # Eventually recovery: bus_status ok:true then channel frame
                recovered = False
                for _ in range(20):
                    try:
                        f = ws.receive_json()
                        if f["type"] == "bus_status" and f["ok"] is True:
                            recovered = True
                            break
                        if f["type"] == "channel":
                            # recovery implied by channel frame (status sent before it)
                            recovered = True
                            break
                    except Exception:
                        break

                assert recovered, "Expected bus_status ok:true or channel frame after recovery"

    def test_invalid_json_from_client_ignored(self, internal_token, monkeypatch):
        """Invalid JSON from client must not crash the handler."""
        fake = FakeClient(
            channels=[{"id": "analysis-1", "name": "AAPL", "member_count": 1}],
            messages=[],
        )

        import web.bus as bus_mod
        monkeypatch.setattr(bus_mod, "get_reader", lambda: fake)

        from fastapi.testclient import TestClient

        from web.main import app

        with TestClient(app) as client:
            with client.websocket_connect(
                "/api/bus", headers=_ws_headers(internal_token)
            ) as ws:
                frame = ws.receive_json()
                assert frame["type"] == "channel"

                frame = ws.receive_json()
                assert frame["type"] == "backfill"

                # Send garbage — handler should silently ignore it
                ws.send_text("this is not JSON {{{{")

                # Should still receive poll frames (not a crash)
                # We just verify we can keep receiving without exception
                frame = ws.receive_json()
                assert frame["type"] in ("bus_message", "ping", "bus_status")

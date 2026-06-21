"""Tests for web/bus_mirror.py — RunMirror agent-bus mirroring.

Fake publisher records (tool, args) tuples so we can assert call order and
content without touching the network. Module-level _agents_registered flag is
reset in the fixture that needs registration isolation.
"""
from __future__ import annotations

import pytest

from web.bus_mirror import RunMirror

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakePublisher:
    """Records every publish() call as (tool, args) — mimics BusPublisher API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.flush_count: int = 0
        self.last_flush_timeout: float = 0.0

    def publish(self, tool: str, args: dict) -> None:
        self.calls.append((tool, args))

    def flush(self, timeout: float = 5.0) -> bool:
        self.flush_count += 1
        self.last_flush_timeout = timeout
        return True

    # -- Query helpers for tests --

    def tools_called(self) -> list[str]:
        return [t for t, _ in self.calls]

    def calls_for(self, tool: str) -> list[dict]:
        return [args for t, args in self.calls if t == tool]

    def messages_sent(self) -> list[dict]:
        return self.calls_for("send_message")

    def messages_from(self, sender: str) -> list[dict]:
        return [a for a in self.messages_sent() if a.get("from") == sender]

    def statuses(self) -> list[dict]:
        return self.calls_for("set_status")


def _reset_module_flag():
    """Reset the per-process registration guard between tests."""
    import web.bus_mirror as mod
    mod._agents_registered = False


def _make_mirror(pub: FakePublisher, analysis_id: int = 1) -> RunMirror:
    """Directly construct a RunMirror (bypasses maybe_create gating)."""
    from web.bus_mirror import RunMirror
    return RunMirror(pub, f"analysis-{analysis_id}", analysis_id)


# ---------------------------------------------------------------------------
# 1. maybe_create — gating
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMaybeCreateGating:

    def test_returns_none_when_bus_mirror_off(self, monkeypatch):
        monkeypatch.setenv("BUS_MIRROR", "off")
        monkeypatch.setattr("web.bus_mirror.get_publisher", lambda: FakePublisher())
        from web.bus_mirror import RunMirror
        result = RunMirror.maybe_create({"ticker": "AAPL", "trade_date": "2024-01-15"}, 1)
        assert result is None

    def test_returns_none_when_publisher_none(self, monkeypatch):
        monkeypatch.delenv("BUS_MIRROR", raising=False)
        monkeypatch.setattr("web.bus_mirror.get_publisher", lambda: None)
        from web.bus_mirror import RunMirror
        result = RunMirror.maybe_create({"ticker": "AAPL", "trade_date": "2024-01-15"}, 1)
        assert result is None

    def test_returns_mirror_when_enabled(self, monkeypatch):
        _reset_module_flag()
        monkeypatch.delenv("BUS_MIRROR", raising=False)
        pub = FakePublisher()
        monkeypatch.setattr("web.bus_mirror.get_publisher", lambda: pub)
        from web.bus_mirror import RunMirror
        result = RunMirror.maybe_create({"ticker": "AAPL", "trade_date": "2024-01-15"}, 42)
        assert result is not None

    def test_default_bus_mirror_value_enables_mirroring(self, monkeypatch):
        """Default BUS_MIRROR env (unset) should allow mirror creation."""
        _reset_module_flag()
        monkeypatch.delenv("BUS_MIRROR", raising=False)
        pub = FakePublisher()
        monkeypatch.setattr("web.bus_mirror.get_publisher", lambda: pub)
        from web.bus_mirror import RunMirror
        result = RunMirror.maybe_create({"ticker": "TSLA", "trade_date": "2024-06-01"}, 99)
        assert result is not None


# ---------------------------------------------------------------------------
# 2. Registration — once per process
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAgentRegistration:

    def test_registers_all_agents_on_first_create(self, monkeypatch):
        _reset_module_flag()
        monkeypatch.delenv("BUS_MIRROR", raising=False)
        pub = FakePublisher()
        monkeypatch.setattr("web.bus_mirror.get_publisher", lambda: pub)
        from web.bus_mirror import _AGENTS, RunMirror
        RunMirror.maybe_create({"ticker": "AAPL", "trade_date": "2024-01-15"}, 1)
        reg_calls = pub.calls_for("register_agent")
        registered_ids = [a["agent_id"] for a in reg_calls]
        expected_ids = [agent_id for agent_id, _ in _AGENTS]
        assert registered_ids == expected_ids

    def test_registers_only_once_across_two_mirrors(self, monkeypatch):
        _reset_module_flag()
        monkeypatch.delenv("BUS_MIRROR", raising=False)
        pub = FakePublisher()
        monkeypatch.setattr("web.bus_mirror.get_publisher", lambda: pub)
        from web.bus_mirror import _AGENTS, RunMirror
        # First mirror
        RunMirror.maybe_create({"ticker": "AAPL", "trade_date": "2024-01-15"}, 1)
        count_after_first = len(pub.calls_for("register_agent"))
        # Second mirror — should not register again
        RunMirror.maybe_create({"ticker": "TSLA", "trade_date": "2024-01-16"}, 2)
        count_after_second = len(pub.calls_for("register_agent"))
        assert count_after_first == len(_AGENTS)
        assert count_after_second == count_after_first  # no new registrations


# ---------------------------------------------------------------------------
# 3. Kickoff sequence order
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKickoffSequence:

    def _create_and_get_pub(
        self,
        monkeypatch,
        ticker="AAPL",
        trade_date="2024-01-15",
        analysis_id=5,
        analysts=None,
    ):
        _reset_module_flag()
        monkeypatch.delenv("BUS_MIRROR", raising=False)
        pub = FakePublisher()
        monkeypatch.setattr("web.bus_mirror.get_publisher", lambda: pub)
        from web.bus_mirror import RunMirror
        RunMirror.maybe_create(
            {"ticker": ticker, "trade_date": trade_date},
            analysis_id,
            analysts=analysts,
        )
        return pub

    def test_register_before_create_channel(self, monkeypatch):
        pub = self._create_and_get_pub(monkeypatch)
        tools = pub.tools_called()
        last_reg_idx = max(i for i, t in enumerate(tools) if t == "register_agent")
        first_channel_idx = next(i for i, t in enumerate(tools) if t == "create_channel")
        assert last_reg_idx < first_channel_idx

    def test_create_channel_before_kickoff_message(self, monkeypatch):
        pub = self._create_and_get_pub(monkeypatch)
        tools = pub.tools_called()
        channel_idx = next(i for i, t in enumerate(tools) if t == "create_channel")
        msg_idx = next(i for i, t in enumerate(tools) if t == "send_message")
        assert channel_idx < msg_idx

    def test_channel_id_matches_analysis_id(self, monkeypatch):
        pub = self._create_and_get_pub(monkeypatch, analysis_id=42)
        channel_calls = pub.calls_for("create_channel")
        assert len(channel_calls) == 1
        assert channel_calls[0]["channel_id"] == "analysis-42"

    def test_channel_name_contains_ticker_and_date(self, monkeypatch):
        pub = self._create_and_get_pub(monkeypatch, ticker="NVDA", trade_date="2024-03-20")
        channel_calls = pub.calls_for("create_channel")
        assert channel_calls[0]["name"] == "NVDA 2024-03-20"

    def test_kickoff_instruction_from_orchestrator(self, monkeypatch):
        pub = self._create_and_get_pub(monkeypatch, ticker="AAPL")
        msgs = pub.messages_from("switchboard-orchestrator")
        assert len(msgs) >= 1
        kickoff = msgs[0]
        assert kickoff["type"] == "instruction"
        assert "AAPL" in kickoff["content"]
        assert kickoff["channel_id"] == "analysis-5"

    def test_set_status_called_for_orchestrator(self, monkeypatch):
        pub = self._create_and_get_pub(monkeypatch, ticker="AAPL")
        status_calls = [s for s in pub.statuses() if s["agent_id"] == "switchboard-orchestrator"]
        assert len(status_calls) >= 1
        assert "AAPL" in status_calls[0]["activity"]

    def test_kickoff_lists_only_selected_analysts(self, monkeypatch):
        """A 2-analyst run's kickoff contains exactly those display names, not the others."""
        pub = self._create_and_get_pub(
            monkeypatch,
            ticker="AAPL",
            analysts=["market", "news"],
        )
        msgs = pub.messages_from("switchboard-orchestrator")
        kickoff = msgs[0]["content"]
        # Selected analysts must appear by display name
        assert "Market Analyst" in kickoff
        assert "News Analyst" in kickoff
        # Non-selected analysts must NOT appear
        assert "Sentiment Analyst" not in kickoff
        assert "Fundamentals Analyst" not in kickoff

    def test_kickoff_default_analysts_lists_all_four(self, monkeypatch):
        """When analysts=None all four display names appear in the kickoff."""
        pub = self._create_and_get_pub(monkeypatch, ticker="AAPL", analysts=None)
        msgs = pub.messages_from("switchboard-orchestrator")
        kickoff = msgs[0]["content"]
        for name in ("Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst"):
            assert name in kickoff, f"{name!r} missing from default kickoff"


# ---------------------------------------------------------------------------
# 4. on_report_delta — report key handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOnReportDelta:

    REPORT_SENDER_MAPPING = {
        "market_report": "market-analyst",
        "sentiment_report": "sentiment-analyst",
        "news_report": "news-analyst",
        "fundamentals_report": "fundamentals-analyst",
        "investment_plan": "research-manager",
        "trader_investment_plan": "trader",
        "final_trade_decision": "portfolio-manager",
    }

    def test_correct_sender_for_each_report_key(self):
        for key, expected_sender in self.REPORT_SENDER_MAPPING.items():
            pub = FakePublisher()
            m = _make_mirror(pub)
            m.on_report_delta({key: "some content"})
            msgs = pub.messages_sent()
            assert len(msgs) >= 1, f"No message for {key}"
            assert msgs[0]["from"] == expected_sender, f"Wrong sender for {key}"

    def test_result_type_for_report(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_report_delta({"market_report": "market data here"})
        assert pub.messages_sent()[0]["type"] == "result"

    def test_content_under_500_chars_not_truncated(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        text = "x" * 100
        m.on_report_delta({"market_report": text})
        content = pub.messages_sent()[0]["content"]
        assert text in content
        assert "…" not in content

    def test_content_over_500_chars_truncated(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        text = "y" * 600
        m.on_report_delta({"market_report": text})
        content = pub.messages_sent()[0]["content"]
        assert "…" in content
        # Truncated at 500 chars
        assert len(content.split(" …")[0]) == 500

    def test_full_report_marker_appended(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_report_delta({"news_report": "some news"})
        content = pub.messages_sent()[0]["content"]
        assert "[full report: news_report]" in content

    def test_synthetic_debate_keys_ignored(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_report_delta({
            "investment_debate": "bull vs bear transcript",
            "risk_debate": "risk debate transcript",
        })
        assert len(pub.messages_sent()) == 0

    def test_investment_plan_triggers_trader_handoff(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_report_delta({"investment_plan": "Buy AAPL because..."})
        msgs = pub.messages_sent()
        senders = [msg["from"] for msg in msgs]
        # First: research-manager with result; second: research-manager with instruction
        assert "research-manager" in senders
        handoff_msgs = [msg for msg in msgs if msg.get("type") == "instruction"]
        assert len(handoff_msgs) == 1
        assert "Trader" in handoff_msgs[0]["content"]

    def test_trader_investment_plan_triggers_risk_handoff(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_report_delta({"trader_investment_plan": "Buy 100 shares..."})
        msgs = pub.messages_sent()
        handoff_msgs = [msg for msg in msgs if msg.get("type") == "instruction"]
        assert len(handoff_msgs) == 1
        assert handoff_msgs[0]["from"] == "trader"
        assert "Risk team" in handoff_msgs[0]["content"]
        assert handoff_msgs[0].get("thread_id") == "risk-debate"

    def test_channel_id_correct_in_message(self):
        pub = FakePublisher()
        m = _make_mirror(pub, analysis_id=7)
        m.on_report_delta({"market_report": "data"})
        assert pub.messages_sent()[0]["channel_id"] == "analysis-7"


# ---------------------------------------------------------------------------
# 5. on_state — investment debate turns
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInvestmentDebate:

    def _inv_state(self, count: int, current_response: str) -> dict:
        return {
            "investment_debate_state": {
                "count": count,
                "current_response": current_response,
                "history": "",
            }
        }

    def test_no_message_when_count_unchanged(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m._inv_count = 2
        m.on_state(self._inv_state(2, "Bull Analyst: something"))
        assert len(pub.messages_sent()) == 0

    def test_first_turn_publishes_orchestrator_instruction(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._inv_state(1, "Bull Analyst: strong growth ahead"))
        orchestrator_msgs = pub.messages_from("switchboard-orchestrator")
        instructions = [msg for msg in orchestrator_msgs if msg.get("type") == "instruction"]
        assert len(instructions) == 1
        assert "debate" in instructions[0]["content"].lower()
        assert instructions[0].get("thread_id") == "investment-debate"

    def test_first_turn_orchestrator_instruction_only_once(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._inv_state(1, "Bull Analyst: buy now"))
        m.on_state(self._inv_state(2, "Bear Analyst: overvalued"))
        orchestrator_instructions = [
            msg for msg in pub.messages_from("switchboard-orchestrator")
            if msg.get("type") == "instruction"
        ]
        assert len(orchestrator_instructions) == 1

    def test_bull_sender_attribution(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._inv_state(1, "Bull Analyst: growth potential is massive"))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert any(msg["from"] == "bull-researcher" for msg in chat_msgs)

    def test_bear_sender_attribution(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m._inv_count = 1  # skip first-turn orchestrator instruction
        m.on_state(self._inv_state(2, "Bear Analyst: risks are too high"))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert any(msg["from"] == "bear-researcher" for msg in chat_msgs)

    def test_bull_prefix_stripped(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._inv_state(1, "Bull Analyst: amazing upside here"))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        bull_msg = next(msg for msg in chat_msgs if msg["from"] == "bull-researcher")
        assert not bull_msg["content"].startswith("Bull Analyst:")

    def test_bear_prefix_stripped(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m._inv_count = 1
        m.on_state(self._inv_state(2, "Bear Analyst: downside risk is significant"))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        bear_msg = next(msg for msg in chat_msgs if msg["from"] == "bear-researcher")
        assert not bear_msg["content"].startswith("Bear Analyst:")

    def test_content_capped_at_700_chars(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        long_text = "Bull Analyst: " + "x" * 800
        m.on_state(self._inv_state(1, long_text))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert all(len(msg["content"]) <= 700 for msg in chat_msgs)

    def test_thread_id_is_investment_debate(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._inv_state(1, "Bull Analyst: buy"))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert all(msg.get("thread_id") == "investment-debate" for msg in chat_msgs)

    def test_counter_updated_after_turn(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._inv_state(3, "Bull Analyst: final argument"))
        # count jumped from 0 to 3; counter should now be 3
        assert m._inv_count == 3

    def test_count_jump_publishes_once(self):
        """If count jumps by >1 in one chunk, we still publish exactly once."""
        pub = FakePublisher()
        m = _make_mirror(pub)
        # Jump from 0 to 3 in one chunk
        m.on_state(self._inv_state(3, "Bull Analyst: many things happened"))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert len(chat_msgs) == 1
        assert m._inv_count == 3


# ---------------------------------------------------------------------------
# 6. on_state — risk debate turns
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRiskDebate:

    def _risk_state(
        self,
        count: int,
        latest_speaker: str,
        agg: str = "",
        cons: str = "",
        neut: str = "",
    ) -> dict:
        return {
            "risk_debate_state": {
                "count": count,
                "latest_speaker": latest_speaker,
                "current_aggressive_response": agg,
                "current_conservative_response": cons,
                "current_neutral_response": neut,
                "history": "",
            }
        }

    def test_aggressive_speaker_maps_to_risk_aggressive(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._risk_state(
            1, "Aggressive",
            agg="Aggressive Analyst: go big or go home",
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert any(msg["from"] == "risk-aggressive" for msg in chat_msgs)

    def test_conservative_speaker_maps_to_risk_conservative(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m._risk_count = 1
        m.on_state(self._risk_state(
            2, "Conservative",
            cons="Conservative Analyst: reduce exposure",
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert any(msg["from"] == "risk-conservative" for msg in chat_msgs)

    def test_neutral_speaker_maps_to_risk_neutral(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m._risk_count = 1
        m.on_state(self._risk_state(
            2, "Neutral",
            neut="Neutral Analyst: balanced view",
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert any(msg["from"] == "risk-neutral" for msg in chat_msgs)

    def test_prefix_stripped_aggressive(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._risk_state(
            1, "Aggressive",
            agg="Aggressive Analyst: high risk high reward",
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        agg_msg = next(msg for msg in chat_msgs if msg["from"] == "risk-aggressive")
        assert not agg_msg["content"].startswith("Aggressive Analyst:")

    def test_prefix_stripped_conservative(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m._risk_count = 1
        m.on_state(self._risk_state(
            2, "Conservative",
            cons="Conservative Analyst: be careful",
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        cons_msg = next(msg for msg in chat_msgs if msg["from"] == "risk-conservative")
        assert not cons_msg["content"].startswith("Conservative Analyst:")

    def test_prefix_stripped_neutral(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m._risk_count = 1
        m.on_state(self._risk_state(
            2, "Neutral",
            neut="Neutral Analyst: balance is key",
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        neut_msg = next(msg for msg in chat_msgs if msg["from"] == "risk-neutral")
        assert not neut_msg["content"].startswith("Neutral Analyst:")

    def test_thread_id_is_risk_debate(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._risk_state(
            1, "Aggressive",
            agg="Aggressive Analyst: go for it",
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert all(msg.get("thread_id") == "risk-debate" for msg in chat_msgs)

    def test_first_risk_turn_sets_status(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._risk_state(
            1, "Aggressive",
            agg="Aggressive Analyst: risk it",
        ))
        status_calls = [s for s in pub.statuses() if s["agent_id"] == "switchboard-orchestrator"]
        assert any("risk" in s["activity"].lower() for s in status_calls)

    def test_risk_content_capped_at_700(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._risk_state(
            1, "Aggressive",
            agg="Aggressive Analyst: " + "a" * 800,
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert all(len(msg["content"]) <= 700 for msg in chat_msgs)

    def test_risk_count_jump_handled(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_state(self._risk_state(
            5, "Neutral",
            neut="Neutral Analyst: many rounds later",
        ))
        chat_msgs = [msg for msg in pub.messages_sent() if msg.get("type") == "chat"]
        assert len(chat_msgs) == 1
        assert m._risk_count == 5


# ---------------------------------------------------------------------------
# 7. on_done
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOnDone:

    def test_publishes_final_result_from_portfolio_manager(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_done("BUY", "The stock looks good based on fundamentals.")
        msgs = pub.messages_sent()
        assert len(msgs) == 1
        assert msgs[0]["from"] == "portfolio-manager"
        assert msgs[0]["type"] == "result"

    def test_final_content_format(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_done("SELL", "Downside risks dominate.")
        content = pub.messages_sent()[0]["content"]
        assert content.startswith("FINAL: SELL — ")

    def test_decision_truncated_at_300(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        long_decision = "x" * 400
        m.on_done("HOLD", long_decision)
        content = pub.messages_sent()[0]["content"]
        # Extract decision part after "FINAL: HOLD — "
        decision_part = content.split(" — ", 1)[1]
        assert len(decision_part) <= 300

    def test_sets_orchestrator_idle_status(self):
        pub = FakePublisher()
        m = _make_mirror(pub, analysis_id=7)
        m.on_done("BUY", "Strong fundamentals.")
        status_calls = [s for s in pub.statuses() if s["agent_id"] == "switchboard-orchestrator"]
        assert len(status_calls) == 1
        assert "idle" in status_calls[0]["activity"]
        assert "7" in status_calls[0]["activity"]


# ---------------------------------------------------------------------------
# 8. on_error
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOnError:

    def test_publishes_error_from_orchestrator(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_error("Connection refused")
        msgs = pub.messages_sent()
        assert len(msgs) == 1
        assert msgs[0]["from"] == "switchboard-orchestrator"
        assert msgs[0]["type"] == "chat"

    def test_error_content_format(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_error("Something went wrong")
        content = pub.messages_sent()[0]["content"]
        assert "Analysis failed:" in content
        assert "Something went wrong" in content

    def test_error_message_truncated_at_300(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        long_msg = "e" * 400
        m.on_error(long_msg)
        content = pub.messages_sent()[0]["content"]
        # The truncated message should not exceed 300 chars in the error part
        assert "e" * 301 not in content


# ---------------------------------------------------------------------------
# 9. close() calls flush
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClose:

    def test_close_calls_flush(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        assert pub.flush_count == 0
        m.close()
        assert pub.flush_count == 1

    def test_close_flush_timeout_is_5s(self):
        """Flush must be called with a 5.0-second timeout."""
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.close()
        assert pub.flush_count == 1
        assert pub.last_flush_timeout == 5.0


# ---------------------------------------------------------------------------
# 10. Exception safety — no method should raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExceptionSafety:

    def test_on_state_with_invalid_chunk_no_raise(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        # None, garbage, missing keys — must not raise
        m.on_state(None)  # type: ignore[arg-type]
        m.on_state({})
        m.on_state({"investment_debate_state": "not a dict"})
        m.on_state({"risk_debate_state": 42})

    def test_on_report_delta_with_bad_input_no_raise(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_report_delta({})
        m.on_report_delta({"market_report": 123})  # non-string value
        m.on_report_delta({"unknown_key": "value"})

    def test_on_done_with_empty_strings(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_done("", "")

    def test_on_error_with_empty_string(self):
        pub = FakePublisher()
        m = _make_mirror(pub)
        m.on_error("")

    def test_methods_no_raise_when_publish_raises(self):
        """If publisher.publish() somehow raises, methods must still not propagate."""
        class BrokenPublisher(FakePublisher):
            def publish(self, tool, args):
                raise RuntimeError("bus exploded")

        m = _make_mirror(BrokenPublisher())
        # These should swallow the RuntimeError from publish
        m.on_report_delta({"market_report": "data"})
        m.on_state({"investment_debate_state": {"count": 1, "current_response": "Bull Analyst: hi"}})
        m.on_done("BUY", "go for it")
        m.on_error("something failed")

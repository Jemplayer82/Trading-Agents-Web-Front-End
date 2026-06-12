"""Mirror the LangGraph trading pipeline's inter-agent handoffs onto the switchboard bus.

RunMirror taps the existing run_analysis_sync stream loop via four hook methods:
    on_state(chunk)       — called on every full-state values chunk
    on_report_delta(delta) — called after each non-empty report delta
    on_done(signal, decision)
    on_error(message)
    close()

All methods are no-op safe: any internal exception is caught and swallowed so a
mirroring bug can never affect the analysis.
"""
from __future__ import annotations

import logging
import os

from .bus import BusPublisher, get_publisher

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

_REPORT_EXCERPT_CHARS = 500
_DEBATE_TURN_CHARS = 700
_DECISION_CHARS = 300

# Analyst short-key → display name (used in kickoff message)
_ANALYST_DISPLAY: dict[str, str] = {
    "market":       "Market Analyst",
    "social":       "Sentiment Analyst",
    "news":         "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    """Strip the first matching prefix from text, if any."""
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent identities
# ---------------------------------------------------------------------------

# (agent_id, human display name)
_AGENTS: list[tuple[str, str]] = [
    ("market-analyst",       "Market Analyst"),
    ("sentiment-analyst",    "Sentiment Analyst"),
    ("news-analyst",         "News Analyst"),
    ("fundamentals-analyst", "Fundamentals Analyst"),
    ("bull-researcher",      "Bull Researcher"),
    ("bear-researcher",      "Bear Researcher"),
    ("research-manager",     "Research Manager"),
    ("trader",               "Trader"),
    ("risk-aggressive",      "Aggressive Risk Analyst"),
    ("risk-conservative",    "Conservative Risk Analyst"),
    ("risk-neutral",         "Neutral Risk Analyst"),
    ("portfolio-manager",    "Portfolio Manager"),
    ("langgraph-orchestrator", "LangGraph Orchestrator"),
]

# report key → sender agent-id
_REPORT_SENDER: dict[str, str] = {
    "market_report":          "market-analyst",
    "sentiment_report":       "sentiment-analyst",
    "news_report":            "news-analyst",
    "fundamentals_report":    "fundamentals-analyst",
    "investment_plan":        "research-manager",
    "trader_investment_plan": "trader",
    "final_trade_decision":   "portfolio-manager",
}

# Synthetic debate keys from DEBATE_REPORTS — ignored in on_report_delta
_DEBATE_KEYS = frozenset({"investment_debate", "risk_debate"})

# Module-level flag: agents registered once per process
_agents_registered: bool = False


# ---------------------------------------------------------------------------
# RunMirror
# ---------------------------------------------------------------------------


class RunMirror:
    """Tap the streaming analysis pipeline and publish agent interactions to the bus."""

    def __init__(self, publisher: BusPublisher, channel_id: str, analysis_id: int) -> None:
        self._pub = publisher
        self._channel_id = channel_id
        self._analysis_id = analysis_id
        # Debate turn counters — last known count from investment/risk debate state
        self._inv_count: int = 0
        self._risk_count: int = 0

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def maybe_create(
        cls,
        params: dict,
        analysis_id: int,
        analysts: list[str] | None = None,
    ) -> RunMirror | None:
        """Return a RunMirror instance, or None if mirroring is disabled.

        Returns None when:
        - Env var BUS_MIRROR == "off"  (default value is "analysis", which enables it)
        - get_publisher() returns None (bus not configured)

        analysts: normalized analyst keys (e.g. ["market", "social"]).  When
        provided, the kickoff message lists only those analysts' display names.
        Falls back to all four when None.
        """
        if os.environ.get("BUS_MIRROR", "analysis") == "off":
            return None

        publisher = get_publisher()
        if publisher is None:
            return None

        try:
            global _agents_registered

            ticker = (params.get("ticker") or "").strip().upper()
            trade_date = params.get("trade_date") or ""
            channel_id = f"analysis-{analysis_id}"
            channel_name = f"{ticker} {trade_date}"

            # Register every agent once per process
            if not _agents_registered:
                for agent_id, name in _AGENTS:
                    publisher.publish("register_agent", {"agent_id": agent_id, "name": name})
                _agents_registered = True

            # Create the run channel
            publisher.publish("create_channel", {"channel_id": channel_id, "name": channel_name})

            # Kickoff instruction from orchestrator — list selected analysts by display name
            selected_keys = analysts if analysts is not None else list(_ANALYST_DISPLAY.keys())
            on_deck = ", ".join(
                _ANALYST_DISPLAY[k] for k in selected_keys if k in _ANALYST_DISPLAY
            )
            kickoff_msg = (
                f"Begin analysis of {ticker} for {trade_date}. "
                f"Analysts on deck: {on_deck}."
            )
            publisher.publish("send_message", {
                "from": "langgraph-orchestrator",
                "content": kickoff_msg,
                "channel_id": channel_id,
                "type": "instruction",
            })

            # Set orchestrator status
            publisher.publish("set_status", {
                "agent_id": "langgraph-orchestrator",
                "activity": f"analyzing {ticker}",
            })

            return cls(publisher, channel_id, analysis_id)

        except Exception:
            log.warning("bus_mirror.maybe_create failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def on_report_delta(self, delta: dict) -> None:
        """Publish a message for each new/changed report field in delta."""
        try:
            for key, val in delta.items():
                # Ignore synthetic debate transcript keys
                if key in _DEBATE_KEYS:
                    continue

                sender = _REPORT_SENDER.get(key)
                if not sender:
                    continue

                if not isinstance(val, str):
                    continue

                # Truncate content and add full-report marker
                marker = f"\n[full report: {key}]"
                if len(val) > _REPORT_EXCERPT_CHARS:
                    content = val[:_REPORT_EXCERPT_CHARS] + f" …{marker}"
                else:
                    content = val + marker

                self._pub.publish("send_message", {
                    "from": sender,
                    "content": content,
                    "channel_id": self._channel_id,
                    "type": "result",
                })

                # Handoff instructions on specific milestones
                if key == "investment_plan":
                    self._pub.publish("send_message", {
                        "from": "research-manager",
                        "content": "Trader: draft an execution plan from this thesis.",
                        "channel_id": self._channel_id,
                        "type": "instruction",
                    })
                elif key == "trader_investment_plan":
                    self._pub.publish("send_message", {
                        "from": "trader",
                        "content": "Risk team: stress-test this plan.",
                        "channel_id": self._channel_id,
                        "thread_id": "risk-debate",
                        "type": "instruction",
                    })

        except Exception:
            log.warning("bus_mirror.on_report_delta failed", exc_info=True)

    def on_state(self, chunk: dict) -> None:
        """Detect and publish debate turns from investment_debate_state and risk_debate_state."""
        try:
            self._handle_investment_debate(chunk)
        except Exception:
            log.warning("bus_mirror.on_state investment failed", exc_info=True)
        try:
            self._handle_risk_debate(chunk)
        except Exception:
            log.warning("bus_mirror.on_state risk failed", exc_info=True)

    def _handle_investment_debate(self, chunk: dict) -> None:
        inv = chunk.get("investment_debate_state")
        if not isinstance(inv, dict):
            return

        count = inv.get("count")
        if not isinstance(count, int) or count <= self._inv_count:
            return

        current_response = inv.get("current_response") or ""

        # On the first turn, publish orchestrator instruction + status change
        if self._inv_count == 0:
            self._pub.publish("send_message", {
                "from": "langgraph-orchestrator",
                "content": "Reports are in — Bull and Bear: debate the investment case.",
                "channel_id": self._channel_id,
                "thread_id": "investment-debate",
                "type": "instruction",
            })
            self._pub.publish("set_status", {
                "agent_id": "langgraph-orchestrator",
                "activity": "investment debate in progress",
            })

        # Determine sender from response prefix
        if current_response.startswith("Bull"):
            sender = "bull-researcher"
        elif current_response.startswith("Bear"):
            sender = "bear-researcher"
        else:
            sender = "bear-researcher"
            log.warning(
                "bus_mirror: investment debate response has unexpected prefix "
                "(expected Bull/Bear) — attributing to bear-researcher. "
                "response[:40]=%r",
                current_response[:40],
            )

        # Strip the "Bull Analyst: " / "Bear Analyst: " prefix
        stripped = _strip_prefix(current_response, ("Bull Analyst: ", "Bear Analyst: "))

        # Cap at _DEBATE_TURN_CHARS
        content = stripped[:_DEBATE_TURN_CHARS]

        # Note: if count jumps by >1 in one chunk, only current_response is available —
        # we publish it once and sync the counter to the new count value.
        self._pub.publish("send_message", {
            "from": sender,
            "content": content,
            "channel_id": self._channel_id,
            "thread_id": "investment-debate",
            "type": "chat",
        })

        self._inv_count = count

    def _handle_risk_debate(self, chunk: dict) -> None:
        risk = chunk.get("risk_debate_state")
        if not isinstance(risk, dict):
            return

        count = risk.get("count")
        if not isinstance(count, int) or count <= self._risk_count:
            return

        latest_speaker = risk.get("latest_speaker") or ""

        # Map latest_speaker prefix → (sender id, current response field)
        if latest_speaker.startswith("Aggressive"):
            sender = "risk-aggressive"
            response_field = "current_aggressive_response"
        elif latest_speaker.startswith("Conservative"):
            sender = "risk-conservative"
            response_field = "current_conservative_response"
        elif latest_speaker.startswith("Neutral"):
            sender = "risk-neutral"
            response_field = "current_neutral_response"
        else:
            sender = "risk-neutral"
            response_field = "current_neutral_response"
            log.warning(
                "bus_mirror: unknown latest_speaker %r in risk_debate_state "
                "— attributing to risk-neutral",
                latest_speaker,
            )

        current_response = risk.get(response_field) or ""

        # On the first turn, set orchestrator status
        if self._risk_count == 0:
            self._pub.publish("set_status", {
                "agent_id": "langgraph-orchestrator",
                "activity": "risk debate in progress",
            })

        # Strip the "Aggressive Analyst: " / "Conservative Analyst: " / "Neutral Analyst: " prefix
        stripped = _strip_prefix(
            current_response,
            ("Aggressive Analyst: ", "Conservative Analyst: ", "Neutral Analyst: "),
        )

        # Cap at _DEBATE_TURN_CHARS
        content = stripped[:_DEBATE_TURN_CHARS]

        # Note: if count jumps by >1 in one chunk, only the latest speaker's
        # current response is available — publish it once and sync the counter.
        self._pub.publish("send_message", {
            "from": sender,
            "content": content,
            "channel_id": self._channel_id,
            "thread_id": "risk-debate",
            "type": "chat",
        })

        self._risk_count = count

    def on_done(self, signal: str, decision: str) -> None:
        """Publish final decision and reset orchestrator status."""
        try:
            decision_excerpt = decision[:_DECISION_CHARS]
            self._pub.publish("send_message", {
                "from": "portfolio-manager",
                "content": f"FINAL: {signal} — {decision_excerpt}",
                "channel_id": self._channel_id,
                "type": "result",
            })
            self._pub.publish("set_status", {
                "agent_id": "langgraph-orchestrator",
                "activity": f"idle — analysis #{self._analysis_id} complete",
            })
        except Exception:
            log.warning("bus_mirror.on_done failed", exc_info=True)

    def on_error(self, message: str) -> None:
        """Publish an error notification from the orchestrator."""
        try:
            self._pub.publish("send_message", {
                "from": "langgraph-orchestrator",
                "content": f"Analysis failed: {message[:_DECISION_CHARS]}",
                "channel_id": self._channel_id,
                "type": "chat",
            })
        except Exception:
            log.warning("bus_mirror.on_error failed", exc_info=True)

    def close(self) -> None:
        """Flush the publisher queue before the run thread exits."""
        try:
            self._pub.flush(5.0)
        except Exception:
            log.warning("bus_mirror.close failed", exc_info=True)

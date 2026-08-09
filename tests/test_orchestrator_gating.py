"""SwitchboardOrchestrator gating — Step 5.

Every LLM round-trip made by the orchestrator must take a permit when a gate
is supplied, and the default (gate=None) must preserve the pre-existing
object graph byte-for-byte.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage

from tests.test_gated_llm import CountingGate, FakeChatModel
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator import switchboard_orchestrator as sbo_module
from tradingagents.orchestrator.gated_llm import GatedLLM
from tradingagents.orchestrator.switchboard_orchestrator import SwitchboardOrchestrator
from web.llm_helpers import DynamicGate

pytestmark = pytest.mark.unit


def _config(tmp_path, extra_config=None):
    cfg = {
        **DEFAULT_CONFIG,
        "llm_provider": "openai",
        "deep_think_llm": "gpt-4o-mini",
        "quick_think_llm": "gpt-4o-mini",
        "memory_log_path": str(tmp_path / "mem.md"),
        "data_cache_dir": str(tmp_path / "cache"),
        "results_dir": str(tmp_path / "results"),
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
    }
    if extra_config is not None:
        cfg.update(extra_config)
    return cfg


@pytest.fixture
def offline_orchestrator_factory(tmp_path, monkeypatch):
    """Build an orchestrator against offline stub LLM clients.

    Returns a callable ``factory(gate=None, tool_gate=None,
    selected_analysts=None, extra_config=None) -> (orchestrator, fake_deep_llm,
    fake_quick_llm)``.
    """

    class StubClient:
        def __init__(self, llm):
            self._llm = llm

        def get_llm(self):
            return self._llm

    def factory(gate=None, tool_gate=None, selected_analysts=None, extra_config=None):
        fakes = {}

        def fake_create_llm_client(
            provider, model, base_url=None, on_token=None, **kwargs
        ):
            if "deep" not in fakes:
                fakes["deep"] = FakeChatModel()
                return StubClient(fakes["deep"])
            fakes["quick"] = FakeChatModel()
            return StubClient(fakes["quick"])

        monkeypatch.setattr(sbo_module, "create_llm_client", fake_create_llm_client)

        orchestrator = SwitchboardOrchestrator(
            config=_config(tmp_path, extra_config),
            selected_analysts=selected_analysts,
            gate=gate,
            tool_gate=tool_gate,
        )
        orchestrator.signal_processor.process_signal = lambda decision: "Buy"
        return orchestrator, fakes.get("deep"), fakes.get("quick")

    return factory


def _investment_debate_state():
    return {
        "count": 1,
        "history": "bull vs bear debate",
        "bull_history": "",
        "bear_history": "",
        "current_response": "",
        "judge_decision": "",
    }


def _risk_debate_state():
    return {
        "count": 1,
        "history": "risk debate",
        "aggressive_history": "",
        "conservative_history": "",
        "neutral_history": "",
        "latest_speaker": "",
        "current_aggressive_response": "",
        "current_conservative_response": "",
        "current_neutral_response": "",
        "judge_decision": "",
    }


def _analyst_update(report_key):
    return {
        "messages": [AIMessage(content="report", tool_calls=[])],
        report_key: "report",
    }


def _make_invoking_node(update):
    """Return a factory that, when called with an LLM, returns a node that
    invokes the LLM once and returns the supplied update dict."""
    def factory(llm):
        def node(state):
            llm.invoke("prompt")
            return update
        return node
    return factory


def _news_factory(report_key):
    def factory(llm, macro_brief=None):
        return _make_invoking_node(_analyst_update(report_key))(llm)
    return factory


def _tool_looping_analyst_factory(report_key, tool_name):
    """Return a factory that, when called with an LLM, returns an analyst node
    that calls a fake tool on its first invocation and returns a final report
    on its second."""
    def factory(llm):
        calls = 0

        def node(state):
            nonlocal calls
            llm.invoke("prompt")
            calls += 1
            if calls == 1:
                return {
                    "messages": [AIMessage(
                        content="",
                        tool_calls=[{"name": tool_name, "args": {}, "id": f"tc_{report_key}"}],
                    )],
                    report_key: "",
                }
            return {
                "messages": [AIMessage(content="report", tool_calls=[])],
                report_key: "report",
            }

        return node

    return factory


def _tool_looping_news_factory(tool_name):
    def factory(llm, macro_brief=None):
        return _tool_looping_analyst_factory("news_report", tool_name)(llm)
    return factory


def _patch_all_factories(
    monkeypatch,
    market_factory=None,
    sentiment_factory=None,
    news_factory=None,
    fundamentals_factory=None,
    portfolio_manager_factory=None,
):
    """Replace every create_* factory run() and rerun_decision() use.

    Each returned node calls its received LLM exactly once and returns the
    minimal update that lets the orchestrator continue.
    """
    if market_factory is None:
        market_factory = _make_invoking_node(_analyst_update("market_report"))
    monkeypatch.setattr(sbo_module, "create_market_analyst", market_factory)

    if sentiment_factory is None:
        sentiment_factory = _make_invoking_node(_analyst_update("sentiment_report"))
    monkeypatch.setattr(sbo_module, "create_sentiment_analyst", sentiment_factory)

    if news_factory is None:
        news_factory = _news_factory("news_report")
    monkeypatch.setattr(sbo_module, "create_news_analyst", news_factory)

    if fundamentals_factory is None:
        fundamentals_factory = _make_invoking_node(_analyst_update("fundamentals_report"))
    monkeypatch.setattr(sbo_module, "create_fundamentals_analyst", fundamentals_factory)

    monkeypatch.setattr(
        sbo_module, "create_bull_researcher", _make_invoking_node({"investment_debate_state": _investment_debate_state()})
    )
    monkeypatch.setattr(
        sbo_module, "create_bear_researcher", _make_invoking_node({"investment_debate_state": _investment_debate_state()})
    )
    monkeypatch.setattr(
        sbo_module,
        "create_research_manager",
        _make_invoking_node(
            {
                "messages": [AIMessage(content="plan", tool_calls=[])],
                "investment_plan": "research manager plan",
            }
        ),
    )
    monkeypatch.setattr(
        sbo_module, "create_trader", _make_invoking_node({"trader_investment_plan": "trader plan"})
    )
    monkeypatch.setattr(
        sbo_module, "create_aggressive_debator", _make_invoking_node({"risk_debate_state": _risk_debate_state()})
    )
    monkeypatch.setattr(
        sbo_module, "create_conservative_debator", _make_invoking_node({"risk_debate_state": _risk_debate_state()})
    )
    monkeypatch.setattr(
        sbo_module, "create_neutral_debator", _make_invoking_node({"risk_debate_state": _risk_debate_state()})
    )

    if portfolio_manager_factory is None:
        portfolio_manager_factory = _make_invoking_node(
            {
                "messages": [AIMessage(content="decision", tool_calls=[])],
                "final_trade_decision": "**Rating**: Buy\n\nRationale.",
            }
        )
    monkeypatch.setattr(sbo_module, "create_portfolio_manager", portfolio_manager_factory)


def _valid_cached_state(ticker="AAPL"):
    history = "Aggressive: risk is worth it. " * 10  # > _MIN_RISK_HISTORY_CHARS
    return {
        "company_of_interest": ticker,
        "asset_type": "stock",
        "trade_date": "2026-08-08",
        "investment_debate_state": {"history": "bull vs bear transcript"},
        "risk_debate_state": {
            "history": history,
            "aggressive_history": "a",
            "conservative_history": "c",
            "neutral_history": "n",
            "latest_speaker": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "judge_decision": "",
            "count": 3,
        },
        "market_report": "market report text",
        "fundamentals_report": "fundamentals report text",
        "sentiment_report": "sentiment report text",
        "news_report": "news report text",
        "investment_plan": "research manager's plan",
        "trader_investment_plan": "trader's proposal",
        "final_trade_decision": "DONOR'S OWN DECISION — must never leak",
        "messages": [("human", "AAPL")],
    }


def test_ungated_construction_leaves_llms_unwrapped(offline_orchestrator_factory):
    orch, fake_deep, fake_quick = offline_orchestrator_factory(gate=None)
    assert orch._quick_llm is fake_quick
    assert orch._deep_llm is fake_deep


def test_gated_construction_wraps_both_roles(offline_orchestrator_factory):
    gate = CountingGate()
    orch, fake_deep, fake_quick = offline_orchestrator_factory(gate=gate)
    assert isinstance(orch._quick_llm, GatedLLM)
    assert orch._quick_llm._inner is fake_quick
    assert isinstance(orch._deep_llm, GatedLLM)
    assert orch._deep_llm._inner is fake_deep


def test_every_llm_call_in_a_full_run_takes_a_permit(offline_orchestrator_factory, monkeypatch):
    gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(
        gate=gate,
        selected_analysts=["market"],
        extra_config={"analyst_concurrency_limit": 1},
    )
    _patch_all_factories(monkeypatch)

    state, signal = orch.run("AAPL", "2026-08-08")

    assert signal == "Buy"
    assert gate.acquires == 9
    assert gate.releases == 9
    assert gate.max_held == 1
    assert gate.acquires > 1, "gating must be per LLM call, not one permit for the whole dive"


def test_permits_never_held_across_the_gap_between_calls(offline_orchestrator_factory, monkeypatch):
    gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(
        gate=gate,
        selected_analysts=["market"],
        extra_config={"analyst_concurrency_limit": 1},
    )
    _patch_all_factories(monkeypatch)

    orch.run("AAPL", "2026-08-08")

    for i, (action, _weight) in enumerate(gate.log):
        expected = "acquire" if i % 2 == 0 else "release"
        assert action == expected
    assert gate.log[-1][0] == "release"
    assert gate.max_held == 1


def test_analyst_tool_loop_gates_each_turn(offline_orchestrator_factory, monkeypatch):
    gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(
        gate=gate,
        selected_analysts=["market"],
        extra_config={"analyst_concurrency_limit": 1},
    )

    def looping_market_factory(llm):
        calls = 0

        def node(state):
            nonlocal calls
            llm.invoke("prompt")
            calls += 1
            if calls == 1:
                return {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[{"name": "fake_tool_for_test", "args": {}, "id": "c1"}],
                        )
                    ],
                    "market_report": "",
                }
            return {
                "messages": [AIMessage(content="report", tool_calls=[])],
                "market_report": "market report",
            }

        return node

    monkeypatch.setattr(sbo_module, "create_market_analyst", looping_market_factory)

    state = {"messages": [("human", "AAPL")]}
    orch._run_analysts_sequential(state, ["market"])

    assert gate.acquires == 2
    assert gate.releases == 2
    assert gate.max_held == 1
    assert gate.log == [("acquire", 1), ("release", 1), ("acquire", 1), ("release", 1)]


def test_parallel_analysts_are_each_gated(offline_orchestrator_factory, monkeypatch):
    gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(
        gate=gate,
        selected_analysts=["market", "social", "news", "fundamentals"],
        extra_config={"analyst_concurrency_limit": 4},
    )
    _patch_all_factories(monkeypatch)

    state, signal = orch.run("AAPL", "2026-08-08")

    assert signal == "Buy"
    assert gate.acquires == 12
    assert gate.releases == 12


def test_parallel_analysts_respect_real_dynamic_gate_and_finish(offline_orchestrator_factory, monkeypatch):
    """A saturated DynamicGate must serialize parallel analysts without deadlocking."""
    gate = DynamicGate(1)
    orch, _fake_deep, fake_quick = offline_orchestrator_factory(
        gate=gate,
        selected_analysts=["market", "social", "news", "fundamentals"],
        extra_config={"analyst_concurrency_limit": 4},
    )
    _patch_all_factories(monkeypatch)

    peak_lock = threading.Lock()
    in_flight = 0
    peak = 0
    original_invoke = fake_quick.invoke

    def tracking_invoke(value, config=None, **kwargs):
        nonlocal in_flight, peak
        with peak_lock:
            in_flight += 1
            if in_flight > peak:
                peak = in_flight
        try:
            # Small delay inside the protected section so competing threads have
            # a window to overlap if the gate were not actually limiting them.
            time.sleep(0.05)
            return original_invoke(value, config=config, **kwargs)
        finally:
            with peak_lock:
                in_flight -= 1

    monkeypatch.setattr(fake_quick, "invoke", tracking_invoke)

    start = time.monotonic()
    state, signal = orch.run("AAPL", "2026-08-08")
    elapsed = time.monotonic() - start

    assert signal == "Buy"
    assert peak <= 1, f"observed peak concurrency {peak} exceeded gate limit 1"
    assert elapsed < 10.0, f"orchestrator run took {elapsed:.2f}s; possible deadlock/hang"


def test_multi_round_debate_scales_the_permit_count(offline_orchestrator_factory, monkeypatch):
    gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(
        gate=gate,
        selected_analysts=["market"],
        extra_config={
            "max_debate_rounds": 2,
            "max_risk_discuss_rounds": 2,
            "analyst_concurrency_limit": 1,
        },
    )
    _patch_all_factories(monkeypatch)

    state, signal = orch.run("AAPL", "2026-08-08")

    assert signal == "Buy"
    assert gate.acquires == 14
    assert gate.releases == 14
    assert gate.max_held == 1


def test_rerun_decision_is_gated(offline_orchestrator_factory, monkeypatch):
    gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(gate=gate)

    def pm_factory(llm):
        def node(state):
            llm.invoke("prompt")
            return {"final_trade_decision": "**Rating**: Buy\n\nRationale."}

        return node

    monkeypatch.setattr(sbo_module, "create_portfolio_manager", pm_factory)

    state, signal = orch.rerun_decision(_valid_cached_state(), "AAPL")

    assert signal == "Buy"
    assert state["final_trade_decision"] == "**Rating**: Buy\n\nRationale."
    assert gate.acquires == 1
    assert gate.releases == 1
    assert gate.max_held == 1


def test_rerun_decision_ungated_makes_no_gate_calls(offline_orchestrator_factory, monkeypatch):
    unused_gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(gate=None)

    def pm_factory(llm):
        def node(state):
            llm.invoke("prompt")
            return {"final_trade_decision": "**Rating**: Buy\n\nRationale."}

        return node

    monkeypatch.setattr(sbo_module, "create_portfolio_manager", pm_factory)

    state, signal = orch.rerun_decision(_valid_cached_state(), "AAPL")

    assert signal == "Buy"
    assert unused_gate.acquires == 0
    assert unused_gate.releases == 0


def test_gate_is_released_when_a_node_raises_mid_run(offline_orchestrator_factory, monkeypatch):
    gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(
        gate=gate,
        selected_analysts=["market"],
    )
    # Make the first quick-LLM invocation fail so the GatedLLM finally-block
    # is exercised.
    orch._quick_llm._inner.raise_on_invoke = True
    _patch_all_factories(monkeypatch)

    with pytest.raises(RuntimeError, match="invoke failed"):
        orch.run("AAPL", "2026-08-08")

    assert gate.acquires == gate.releases
    assert gate.held == 0


def test_factories_still_receive_the_orchestrators_own_llm_object(offline_orchestrator_factory, monkeypatch):
    gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(gate=gate, selected_analysts=["market"])
    captured = {}

    def market_factory(llm):
        captured["market_llm"] = llm
        return _make_invoking_node(_analyst_update("market_report"))(llm)

    def pm_factory(llm):
        captured["pm_llm"] = llm

        def node(state):
            llm.invoke("prompt")
            return {"final_trade_decision": "**Rating**: Buy\n\nRationale."}

        return node

    _patch_all_factories(monkeypatch, market_factory=market_factory, portfolio_manager_factory=pm_factory)

    orch.run("AAPL", "2026-08-08")

    assert captured["market_llm"] is orch._quick_llm
    assert captured["pm_llm"] is orch._deep_llm


def test_tool_gate_counts_real_tool_calls_and_llm_gate_unaffected(offline_orchestrator_factory, monkeypatch):
    llm_gate = CountingGate()
    tool_gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(
        gate=llm_gate,
        tool_gate=tool_gate,
        selected_analysts=["market"],
        extra_config={"analyst_concurrency_limit": 1},
    )

    fake_tool = MagicMock()
    fake_tool.invoke.return_value = "ok"
    monkeypatch.setitem(sbo_module._TOOL_MAP, "fake_tool_for_test_real", fake_tool)

    _patch_all_factories(
        monkeypatch,
        market_factory=_tool_looping_analyst_factory("market_report", "fake_tool_for_test_real"),
    )

    state, signal = orch.run("AAPL", "2026-08-08")

    assert signal == "Buy"
    assert fake_tool.invoke.call_count == 1
    assert tool_gate.acquires == 1
    assert tool_gate.releases == 1
    assert tool_gate.held == 0
    # 2 market-analyst LLM turns + 8 downstream nodes; the tool gate must not
    # inflate or deflate the LLM gate count.
    assert llm_gate.acquires == 10
    assert llm_gate.releases == 10


def test_shared_tool_gate_bounds_concurrent_tool_dispatches(offline_orchestrator_factory, monkeypatch):
    llm_gate = CountingGate()
    tool_gate = CountingGate()
    orch, *_ = offline_orchestrator_factory(
        gate=llm_gate,
        tool_gate=tool_gate,
        selected_analysts=["market", "social", "news", "fundamentals"],
        extra_config={"analyst_concurrency_limit": 4},
    )

    fake_tool = MagicMock()
    fake_tool.invoke.return_value = "ok"
    monkeypatch.setitem(sbo_module._TOOL_MAP, "fake_tool_for_test_real", fake_tool)

    _patch_all_factories(
        monkeypatch,
        market_factory=_tool_looping_analyst_factory("market_report", "fake_tool_for_test_real"),
        sentiment_factory=_tool_looping_analyst_factory("sentiment_report", "fake_tool_for_test_real"),
        news_factory=_tool_looping_news_factory("fake_tool_for_test_real"),
        fundamentals_factory=_tool_looping_analyst_factory("fundamentals_report", "fake_tool_for_test_real"),
    )

    state, signal = orch.run("AAPL", "2026-08-08")

    assert signal == "Buy"
    assert fake_tool.invoke.call_count == 4
    assert tool_gate.acquires == 4
    assert tool_gate.releases == 4
    assert tool_gate.max_held <= 4


def test_tool_gate_none_leaves_tool_calls_ungated(offline_orchestrator_factory, monkeypatch):
    orch, *_ = offline_orchestrator_factory(
        tool_gate=None,
        selected_analysts=["market"],
        extra_config={"analyst_concurrency_limit": 1},
    )

    fake_tool = MagicMock()
    fake_tool.invoke.return_value = "ok"
    monkeypatch.setitem(sbo_module._TOOL_MAP, "fake_tool_for_test_real", fake_tool)

    _patch_all_factories(
        monkeypatch,
        market_factory=_tool_looping_analyst_factory("market_report", "fake_tool_for_test_real"),
    )

    state, signal = orch.run("AAPL", "2026-08-08")

    assert signal == "Buy"
    assert fake_tool.invoke.call_count == 1
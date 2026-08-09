import threading
import time

import pytest
from langchain_core.messages import AIMessage

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator import switchboard_orchestrator as sbo_module
from tradingagents.orchestrator.switchboard_orchestrator import SwitchboardOrchestrator

pytestmark = pytest.mark.unit


# ── helpers copied verbatim from tests/test_news_analyst_wiring.py ──────────
def _make_orchestrator(tmp_path, selected_analysts, extra_config=None):
    config = {
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
        config.update(extra_config)
    return SwitchboardOrchestrator(
        config=config, selected_analysts=selected_analysts
    )


def _fake_node_factory(update):
    def factory(llm):
        def node(state):
            return update
        return node
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


def _patch_downstream_factories(monkeypatch):
    """Monkeypatch every node factory run() calls after the analyst phase
    so the test exercises the real orchestrator code without real LLM calls.
    """
    monkeypatch.setattr(
        sbo_module,
        "create_bull_researcher",
        _fake_node_factory({"investment_debate_state": _investment_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_bear_researcher",
        _fake_node_factory({"investment_debate_state": _investment_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_research_manager",
        _fake_node_factory({
            "messages": [AIMessage(content="plan", tool_calls=[])],
            "investment_plan": "research manager plan",
        }),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_trader",
        _fake_node_factory({"trader_investment_plan": "trader plan"}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_aggressive_debator",
        _fake_node_factory({"risk_debate_state": _risk_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_conservative_debator",
        _fake_node_factory({"risk_debate_state": _risk_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_neutral_debator",
        _fake_node_factory({"risk_debate_state": _risk_debate_state()}),
    )
    monkeypatch.setattr(
        sbo_module,
        "create_portfolio_manager",
        _fake_node_factory({
            "messages": [AIMessage(content="decision", tool_calls=[])],
            "final_trade_decision": "**Rating**: Buy\n\nRationale.",
        }),
    )


# ── analyst helpers ──────────────────────────────────────────────────────────
_ANALYST_FACTORY_MAP = {
    "market": ("create_market_analyst", "market_report"),
    "social": ("create_sentiment_analyst", "sentiment_report"),
    "news": ("create_news_analyst", "news_report"),
    "fundamentals": ("create_fundamentals_analyst", "fundamentals_report"),
}


def _patch_default_analyst_factories(monkeypatch):
    """Patch all four analyst factories to deterministic one-turn nodes."""
    def make(key, report_key):
        def factory(llm, **kwargs):
            def node(state):
                return {
                    "messages": [AIMessage(content=f"{key} report", tool_calls=[])],
                    report_key: f"{key} report",
                }
            return node
        return factory

    for key, (factory_name, report_key) in _ANALYST_FACTORY_MAP.items():
        monkeypatch.setattr(sbo_module, factory_name, make(key, report_key))


def _selected_all():
    return ["market", "social", "news", "fundamentals"]


# ── tests ───────────────────────────────────────────────────────────────────
def test_default_config_analyst_concurrency_limit_is_still_one():
    assert DEFAULT_CONFIG["analyst_concurrency_limit"] == 1


def test_limit_1_and_limit_4_produce_identical_state_and_signal(tmp_path, monkeypatch):
    _patch_downstream_factories(monkeypatch)
    _patch_default_analyst_factories(monkeypatch)

    memory_log_path = str(tmp_path / "shared_mem.md")
    shared_extra = {
        "analyst_concurrency_limit": 1,
        "memory_log_path": memory_log_path,
    }

    seq_orch = _make_orchestrator(
        tmp_path, selected_analysts=_selected_all(), extra_config=shared_extra
    )
    seq_orch.signal_processor.process_signal = lambda decision: "Buy"

    par_orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={**shared_extra, "analyst_concurrency_limit": 4},
    )
    par_orch.signal_processor.process_signal = lambda decision: "Buy"

    state_seq, signal_seq = seq_orch.run("AAPL", "2026-08-08")
    state_par, signal_par = par_orch.run("AAPL", "2026-08-08")

    assert signal_seq == signal_par
    assert state_seq == state_par


@pytest.mark.parametrize(
    "raw_limit, expected_limit",
    [
        (1, 1),
        (0, 1),
        (-3, 1),
        ("banana", 1),
        (None, 1),
        (4, 4),
        (99, 4),
    ],
)
def test_effective_limit_clamping(tmp_path, raw_limit, expected_limit):
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": raw_limit},
    )
    assert orch._analyst_concurrency_limit(4) == expected_limit
    assert orch._analyst_concurrency_limit(0) == 1


def test_analysts_actually_run_concurrently(tmp_path, monkeypatch):
    barrier = threading.Barrier(4, timeout=10)

    def make_with_barrier(key, report_key):
        def factory(llm, **kwargs):
            def node(state):
                barrier.wait()
                return {
                    "messages": [AIMessage(content=f"{key} report", tool_calls=[])],
                    report_key: f"{key} report",
                }
            return node
        return factory

    for key, (factory_name, report_key) in _ANALYST_FACTORY_MAP.items():
        monkeypatch.setattr(sbo_module, factory_name, make_with_barrier(key, report_key))

    _patch_downstream_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": 4},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"

    state, signal = orch.run("AAPL", "2026-08-08")

    for key, (_, report_key) in _ANALYST_FACTORY_MAP.items():
        assert state[report_key] == f"{key} report"
    assert signal == "Buy"


def test_limit_2_caps_concurrency_at_2(tmp_path, monkeypatch):
    lock = threading.Lock()
    active = [0]
    peak = [0]

    def make_counting(key, report_key):
        def factory(llm, **kwargs):
            def node(state):
                with lock:
                    active[0] += 1
                    if active[0] > peak[0]:
                        peak[0] = active[0]
                time.sleep(0.02)
                with lock:
                    active[0] -= 1
                return {
                    "messages": [AIMessage(content=f"{key} report", tool_calls=[])],
                    report_key: f"{key} report",
                }
            return node
        return factory

    for key, (factory_name, report_key) in _ANALYST_FACTORY_MAP.items():
        monkeypatch.setattr(sbo_module, factory_name, make_counting(key, report_key))

    _patch_downstream_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": 2},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"

    state, _ = orch.run("AAPL", "2026-08-08")

    assert peak[0] <= 2
    for _, report_key in _ANALYST_FACTORY_MAP.values():
        assert state[report_key]


def test_each_parallel_analyst_gets_its_own_messages_list(tmp_path, monkeypatch):
    observations = {}
    lock = threading.Lock()

    def make_observing(key, report_key):
        def factory(llm, **kwargs):
            def node(state):
                with lock:
                    observations[key] = (id(state["messages"]), list(state["messages"]))
                state["messages"].append(f"SENTINEL_{key}")
                return {
                    "messages": [AIMessage(content=f"{key} report", tool_calls=[])],
                    report_key: f"{key} report",
                }
            return node
        return factory

    for key, (factory_name, report_key) in _ANALYST_FACTORY_MAP.items():
        monkeypatch.setattr(sbo_module, factory_name, make_observing(key, report_key))

    _patch_downstream_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": 4},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"

    state, _ = orch.run("AAPL", "2026-08-08")

    ids = {obs[0] for obs in observations.values()}
    assert len(ids) == 4

    for key, (_, _report_key) in _ANALYST_FACTORY_MAP.items():
        msg_id, seed = observations[key]
        assert seed == [("human", "AAPL")]
        # No other analyst ever saw this analyst's sentinel.
        for other_key in observations:
            if other_key == key:
                continue
            assert f"SENTINEL_{key}" not in observations[other_key][1]


def test_worker_threads_never_write_the_shared_state(tmp_path, monkeypatch):
    def make_leaky(key, report_key):
        def factory(llm, **kwargs):
            def node(state):
                state[f"LEAKED_{key}"] = True
                return {
                    "messages": [AIMessage(content=f"{key} report", tool_calls=[])],
                    report_key: f"{key} report",
                }
            return node
        return factory

    for key, (factory_name, report_key) in _ANALYST_FACTORY_MAP.items():
        monkeypatch.setattr(sbo_module, factory_name, make_leaky(key, report_key))

    _patch_downstream_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": 4},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"

    state, _ = orch.run("AAPL", "2026-08-08")

    assert not any(k.startswith("LEAKED_") for k in state)
    for _, report_key in _ANALYST_FACTORY_MAP.values():
        assert state[report_key] == report_key.replace("_report", " report").replace("sentiment", "social")


def test_one_report_update_frame_per_analyst_not_batched(tmp_path, monkeypatch):
    frames_lock = threading.Lock()
    frames = []

    def capture(frame):
        with frames_lock:
            frames.append(frame)

    _patch_default_analyst_factories(monkeypatch)
    _patch_downstream_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": 4},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"
    orch.on_progress = capture

    state, _ = orch.run("AAPL", "2026-08-08")

    analyst_report_keys = {report_key for _, report_key in _ANALYST_FACTORY_MAP.values()}
    analyst_frames = [
        f
        for f in frames
        if f.get("type") == "report_update"
        and isinstance(f.get("reports"), dict)
        and set(f["reports"]).issubset(analyst_report_keys)
    ]

    assert len(analyst_frames) == 4
    for f in analyst_frames:
        assert len(f["reports"]) == 1

    union = set().union(*(f["reports"].keys() for f in analyst_frames))
    assert union == analyst_report_keys


def test_status_frame_emitted_for_every_analyst(tmp_path, monkeypatch):
    frames_lock = threading.Lock()
    frames = []

    def capture(frame):
        with frames_lock:
            frames.append(frame)

    _patch_default_analyst_factories(monkeypatch)
    _patch_downstream_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": 4},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"
    orch.on_progress = capture

    orch.run("AAPL", "2026-08-08")

    expected = [
        {"type": "status", "message": f"Running {key} analyst…"}
        for key in _selected_all()
    ]
    actual = [
        f
        for f in frames
        if f.get("type") == "status"
        and f.get("message", "").startswith("Running ")
    ]
    assert actual == expected


def test_token_frames_are_labelled_with_the_producing_analyst(tmp_path, monkeypatch):
    barrier = threading.Barrier(4, timeout=10)
    frames_lock = threading.Lock()
    frames = []

    def capture(frame):
        with frames_lock:
            frames.append(frame)

    orch_ref = [None]

    def make_token_emitter(key, report_key):
        def factory(llm, **kwargs):
            def node(state):
                barrier.wait()
                for _ in range(25):
                    orch_ref[0]._emit_token(f"<{key}>")
                    time.sleep(0.001)
                return {
                    "messages": [AIMessage(content=f"{key} report", tool_calls=[])],
                    report_key: f"{key} report",
                }
            return node
        return factory

    for key, (factory_name, report_key) in _ANALYST_FACTORY_MAP.items():
        monkeypatch.setattr(sbo_module, factory_name, make_token_emitter(key, report_key))

    _patch_downstream_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": 4},
    )
    orch_ref[0] = orch
    orch.signal_processor.process_signal = lambda decision: "Buy"
    orch.on_progress = capture

    orch.run("AAPL", "2026-08-08")

    marker_to_node = {
        "market": "market_analyst",
        "social": "sentiment_analyst",
        "news": "news_analyst",
        "fundamentals": "fundamentals_analyst",
    }

    token_frames = [f for f in frames if f.get("type") == "token"]

    for marker, node in marker_to_node.items():
        node_frames = [f for f in token_frames if f.get("text") == f"<{marker}>"]
        assert len(node_frames) >= 20, f"{marker} produced only {len(node_frames)} token frames"
        assert all(f.get("node") == node for f in node_frames)


def test_analyst_exception_propagates_out_of_run(tmp_path, monkeypatch):
    _patch_default_analyst_factories(monkeypatch)
    _patch_downstream_factories(monkeypatch)

    def market_boom_factory(llm, **kwargs):
        def node(state):
            raise RuntimeError("boom")
        return node

    monkeypatch.setattr(sbo_module, "create_market_analyst", market_boom_factory)

    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=_selected_all(),
        extra_config={"analyst_concurrency_limit": 4},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"

    with pytest.raises(RuntimeError, match="boom"):
        orch.run("AAPL", "2026-08-08")

    assert [t for t in threading.enumerate() if t.name.startswith("analyst")] == []


@pytest.mark.parametrize("limit", [1, 4])
def test_unknown_analyst_keys_are_skipped_at_both_limits(tmp_path, monkeypatch, limit):
    _patch_downstream_factories(monkeypatch)
    _patch_default_analyst_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=["market", "bogus"],
        extra_config={"analyst_concurrency_limit": limit},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"

    state, signal = orch.run("AAPL", "2026-08-08")

    assert signal == "Buy"
    assert state["market_report"] == "market report"
    assert state["sentiment_report"] == ""
    assert state["news_report"] == ""
    assert state["fundamentals_report"] == ""


def test_empty_analyst_selection_completes_at_limit_4(tmp_path, monkeypatch):
    _patch_downstream_factories(monkeypatch)
    _patch_default_analyst_factories(monkeypatch)
    orch = _make_orchestrator(
        tmp_path,
        selected_analysts=["nonexistent"],
        extra_config={"analyst_concurrency_limit": 4},
    )
    orch.signal_processor.process_signal = lambda decision: "Buy"

    state, signal = orch.run("AAPL", "2026-08-08")

    assert signal == "Buy"
    assert state["market_report"] == ""
    assert state["sentiment_report"] == ""
    assert state["news_report"] == ""
    assert state["fundamentals_report"] == ""


import ast
import inspect
import threading
import time
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from pydantic import BaseModel

from tradingagents.agents.utils.structured import bind_structured
from tradingagents.orchestrator.gated_llm import GatedLLM, wrap_llm

pytestmark = pytest.mark.unit


class CountingGate:
    """Duck-typed gate that records every acquire/release for inspection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquires = 0
        self.releases = 0
        self.held = 0
        self.max_held = 0
        self.log: list[tuple[str, int]] = []

    def acquire(self, weight: int = 1) -> None:
        with self._lock:
            self.acquires += 1
            self.held += weight
            if self.held > self.max_held:
                self.max_held = self.held
            self.log.append(("acquire", weight))

    def release(self, weight: int = 1) -> None:
        with self._lock:
            self.releases += 1
            self.held = max(0, self.held - weight)
            self.log.append(("release", weight))


class FakeChatModel:
    """Minimal chat-model stand-in for the orchestrator wrapper tests."""

    model_name = "fake-model"

    def __init__(
        self,
        raise_on_invoke: bool = False,
        raise_on_structured: Exception | None = None,
        invoke_delay: float = 0.0,
    ) -> None:
        self.calls: list[Any] = []
        self.bound_tools: Any = None
        self.structured_schema: Any = None
        self.raise_on_invoke = raise_on_invoke
        self.raise_on_structured = raise_on_structured
        self.invoke_delay = invoke_delay

    def invoke(self, value: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append(("invoke", value, config, kwargs))
        if self.invoke_delay:
            time.sleep(self.invoke_delay)
        if self.raise_on_invoke:
            raise RuntimeError("invoke failed")
        return AIMessage(content="ok", tool_calls=[])

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeChatModel":
        self.bound_tools = tools
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> "FakeChatModel":
        self.structured_schema = schema
        if self.raise_on_structured is not None:
            raise self.raise_on_structured
        return self


class BareModel:
    """Chat-model stand-in that lacks `with_structured_output`."""

    model_name = "bare"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def invoke(self, value: Any, config: Any = None, **kwargs: Any) -> AIMessage:
        self.calls.append(value)
        return AIMessage(content="ok")

    def bind_tools(self, tools: Any, **kwargs: Any) -> "BareModel":
        return self


class SomePydanticModel(BaseModel):
    answer: str


def test_wrap_llm_returns_the_raw_object_when_gate_is_none() -> None:
    model = FakeChatModel()
    assert wrap_llm(model, None) is model


def test_wrap_llm_wraps_when_a_gate_is_given() -> None:
    model = FakeChatModel()
    gate = CountingGate()
    wrapped = wrap_llm(model, gate)
    assert isinstance(wrapped, GatedLLM)
    assert wrapped.inner is model


def test_direct_invoke_takes_exactly_one_permit() -> None:
    model = FakeChatModel()
    gate = CountingGate()
    result = GatedLLM(model, gate).invoke("hi")
    assert result.content == "ok"
    assert gate.log == [("acquire", 1), ("release", 1)]
    assert gate.max_held == 1


def test_permit_is_released_when_the_inner_call_raises() -> None:
    model = FakeChatModel(raise_on_invoke=True)
    gate = CountingGate()
    with pytest.raises(RuntimeError, match="invoke failed"):
        GatedLLM(model, gate).invoke("hi")
    assert gate.acquires == 1
    assert gate.releases == 1


def test_bind_tools_chain_composes_and_gates_once() -> None:
    model = FakeChatModel()
    gate = CountingGate()
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "s"),
            MessagesPlaceholder(variable_name="messages"),
        ]
    )
    chain = prompt | GatedLLM(model, gate).bind_tools([])
    result = chain.invoke({"messages": [HumanMessage(content="AAPL")]})
    assert isinstance(result, AIMessage)
    assert result.content == "ok"
    assert gate.log == [("acquire", 1), ("release", 1)]
    assert model.bound_tools == []


def test_plain_pipe_composition_gates_once() -> None:
    model = FakeChatModel()
    gate = CountingGate()
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "s"),
            MessagesPlaceholder(variable_name="messages"),
        ]
    )
    chain = prompt | GatedLLM(model, gate)
    result = chain.invoke({"messages": [HumanMessage(content="AAPL")]})
    assert result.content == "ok"
    assert gate.log == [("acquire", 1), ("release", 1)]


def test_with_structured_output_returns_a_gated_object_that_gates_once() -> None:
    model = FakeChatModel()
    gate = CountingGate()
    structured = GatedLLM(model, gate).with_structured_output(SomePydanticModel)
    assert isinstance(structured, GatedLLM)
    result = structured.invoke("p")
    assert result.content == "ok"
    assert gate.log == [("acquire", 1), ("release", 1)]
    assert model.structured_schema is SomePydanticModel


def test_with_structured_output_propagates_to_bind_structured_fallback() -> None:
    # NotImplementedError branch.
    model = FakeChatModel(raise_on_structured=NotImplementedError("unsupported"))
    gate = CountingGate()
    wrapped = GatedLLM(model, gate)
    assert bind_structured(wrapped, SomePydanticModel, "TestAgent") is None

    # AttributeError branch.
    bare = BareModel()
    gate2 = CountingGate()
    wrapped_bare = GatedLLM(bare, gate2)
    assert bind_structured(wrapped_bare, SomePydanticModel, "TestAgent") is None


def test_two_sequential_invokes_alternate() -> None:
    model = FakeChatModel()
    gate = CountingGate()
    wrapper = GatedLLM(model, gate)
    wrapper.invoke("a")
    wrapper.invoke("b")
    assert gate.log == [
        ("acquire", 1),
        ("release", 1),
        ("acquire", 1),
        ("release", 1),
    ]


def test_attribute_passthrough() -> None:
    model = FakeChatModel()
    gate = CountingGate()
    wrapper = GatedLLM(model, gate)
    assert wrapper.model_name == "fake-model"
    with pytest.raises(AttributeError):
        _ = wrapper.definitely_not_an_attribute


def test_concurrent_invokes_are_each_gated() -> None:
    model = FakeChatModel(invoke_delay=0.01)
    gate = CountingGate()
    wrapper = GatedLLM(model, gate)
    errors: list[Exception] = []

    def run() -> None:
        try:
            wrapper.invoke("x")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert gate.acquires == 8
    assert gate.releases == 8
    assert gate.held == 0


def test_against_the_real_dynamic_gate_limits_concurrency() -> None:
    from web.llm_helpers import DynamicGate

    peak_lock = threading.Lock()
    peak = 0
    in_flight = 0
    completed = 0

    class SlowFake:
        model_name = "slow"

        def invoke(self, value: Any, config: Any = None, **kwargs: Any) -> AIMessage:
            nonlocal peak, in_flight, completed
            with peak_lock:
                in_flight += 1
                if in_flight > peak:
                    peak = in_flight
            time.sleep(0.05)
            with peak_lock:
                in_flight -= 1
                completed += 1
            return AIMessage(content="ok")

    wrapper = GatedLLM(SlowFake(), DynamicGate(1))
    errors: list[Exception] = []

    def run() -> None:
        try:
            wrapper.invoke("x")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert peak == 1
    assert completed == 4
    assert not any(t.is_alive() for t in threads)


def test_orchestrator_package_does_not_import_web() -> None:
    import tradingagents.orchestrator.gated_llm as gated_mod
    import tradingagents.orchestrator.switchboard_orchestrator as sb_mod

    def _imports_web(source: str) -> bool:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "web":
                        return True
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "web":
                    return True
        return False

    for mod in (gated_mod, sb_mod):
        path = inspect.getsourcefile(mod)
        assert path is not None
        with open(path, encoding="utf-8") as f:
            source = f.read()
        assert not _imports_web(source), f"{path} imports web"

"""
Gated concurrency wrapper for LLM objects used by tradingagents.

The agent factories in `tradingagents/agents/**` invoke the model inside closures
the orchestrator never sees, and several make multiple LLM round-trips per
logical node call (e.g. tool-call retry and structured-output fallback).
Gating at the orchestrator would undercount real round-trips; gating the LLM
object itself counts every `.invoke()` automatically with no factory changes.

The gate is duck-typed -- any object exposing `acquire(weight=1)` /
`release(weight=1)` works -- and this module deliberately imports nothing from
`web/` so `tradingagents/` remains free of web-layer dependencies.

Hard invariant: a permit wraps exactly ONE `.invoke()` and is NEVER held while
acquiring another. The production `DynamicGate.acquire` blocks when
`in_use > 0 and in_use + weight > limit`, so nested permit-holding would
deadlock it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from langchain_core.runnables import Runnable


@runtime_checkable
class GateLike(Protocol):
    """Minimal concurrency-gate protocol. web.llm_helpers.DynamicGate matches."""

    def acquire(self, weight: int = 1) -> None: ...
    def release(self, weight: int = 1) -> None: ...


class GatedLLM(Runnable):
    """Wraps a chat model (or a bound/structured runnable derived from one) so
    every `.invoke()` takes and returns exactly one gate permit.

    Why wrap the LLM object instead of adding acquire/release at call sites:
    the agent factories in `tradingagents/agents/**` invoke the model inside
    closures the orchestrator never sees, and several make multiple LLM
    round-trips per logical node call (e.g. tool-call retry and structured-
    output fallback). Gating at the orchestrator would undercount real
    round-trips; gating the LLM counts every one automatically with no factory
    changes.

    The gate is duck-typed -- any object exposing `acquire(weight=1)` /
    `release(weight=1)` works -- and this module deliberately imports nothing
    from `web/` so `tradingagents/` remains free of web-layer dependencies.

    Hard invariant: a permit wraps exactly ONE `.invoke()` and is never held
    while acquiring another. The production `DynamicGate.acquire` blocks when
    `in_use > 0 and in_use + weight > limit`, so nested permit-holding would
    deadlock it.

    Subclasses `langchain_core.Runnable` (not a plain proxy) because the
    analyst factories compose chains with `prompt | llm.bind_tools(tools)`,
    and LCEL's `|` only composes `Runnable`s. Verified against
    langchain-core 1.4.4.

    Only the synchronous `.invoke()` path is gated; `.stream()`,
    `.astream()`, `.batch()`, `.abatch()`, and `.ainvoke()` intentionally
    raise `NotImplementedError`.
    """

    def __init__(self, inner: Any, gate: GateLike) -> None:
        self._inner = inner
        self._gate = gate

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self._gate.acquire(1)
        try:
            return self._inner.invoke(input, config, **kwargs)
        finally:
            self._gate.release(1)

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "GatedLLM only gates the synchronous invoke() path; "
            "stream() is not supported."
        )

    def astream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "GatedLLM only gates the synchronous invoke() path; "
            "astream() is not supported."
        )

    def batch(
        self,
        inputs: Any,
        config: Any = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError(
            "GatedLLM only gates the synchronous invoke() path; "
            "batch() is not supported."
        )

    def abatch(
        self,
        inputs: Any,
        config: Any = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Any,
    ) -> Any:
        raise NotImplementedError(
            "GatedLLM only gates the synchronous invoke() path; "
            "abatch() is not supported."
        )

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "GatedLLM only gates the synchronous invoke() path; "
            "ainvoke() is not supported."
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> GatedLLM:
        # Forward to the raw inner model and re-wrap so the chain still
        # contains exactly one GatedLLM and a single call can never
        # double-acquire.
        return GatedLLM(self._inner.bind_tools(tools, **kwargs), self._gate)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> GatedLLM:
        # NotImplementedError / AttributeError must propagate unchanged:
        # structured.py::bind_structured catches exactly those two to fall
        # back to free-text generation.
        return GatedLLM(
            self._inner.with_structured_output(schema, **kwargs), self._gate
        )

    def __getattr__(self, name: str) -> Any:
        # Only fires for attributes this class doesn't define. Guard the two
        # own attributes so a partially-initialised instance can't recurse.
        if name in {"_inner", "_gate"}:
            raise AttributeError(name)
        # Refuse to proxy raw BaseChatModel entry points that bypass the gate.
        # These are not on Runnable, so without this block they would silently
        # perform an ungated network round-trip.
        if name in {"generate", "agenerate", "predict", "predict_messages", "get_num_tokens"}:
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __repr__(self) -> str:
        return f"GatedLLM({type(self._inner).__name__})"


def wrap_llm(llm: Any, gate: GateLike | None) -> Any:
    """Return `llm` untouched when `gate is None`, else a GatedLLM around it.

    Returning the raw object when ungated guarantees zero behaviour change
    for every caller that supplies no gate -- the CLI, web/runner.py's
    single-ticker runs, web/portfolio_routes.py, and the whole existing suite.
    """
    return llm if gate is None else GatedLLM(llm, gate)
from __future__ import annotations

"""
Shared home for per-paper-account stop policy primitives.

The options stop path lives in `web/options_allocator.py` (tier-4 only) and the
equity stop path must live in `web/spy_scanner.py` (tier-3 only), so neither can
import the other.  `web/spy_routes.py` (tier 3) and `web/scheduler.py` (every
tier) both need the same validation.  One shared home is the only way to keep
the two stop implementations provably identical.

This module imports only the Python stdlib and nothing from `web/`.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "STOP_TYPES",
    "StopPolicy",
    "NONE",
    "StopOutcome",
    "parse_hhmm",
    "validate_policy",
    "evaluate",
    "describe_policy",
]

STOP_TYPES = ("none", "stop", "stop_limit", "trailing_pct", "trailing_dollar")

_EXIT_REASON = {
    "stop": "stop_loss",
    "stop_limit": "stop_limit",
    "trailing_pct": "trail_stop",
    "trailing_dollar": "trail_stop",
}

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: object) -> tuple[int, int] | None:
    """'07:30' -> (7, 30). None for None, non-str, blank, or anything that is
    not a strict 24-hour HH:MM. Never raises — callers use None to mean
    'skip / fall back'."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    match = _HHMM_RE.match(value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class StopPolicy:
    stop_type: str = "none"
    stop_value: float | None = None
    stop_limit_offset: float | None = None

    @classmethod
    def from_account(cls, account: Mapping[str, Any] | None) -> StopPolicy:
        """Tolerant runtime parser.  An unparsable policy means NO stop."""
        if not account:
            return NONE

        raw_type = account.get("stop_type")
        if not isinstance(raw_type, str):
            return NONE
        stop_type = raw_type.strip().lower()
        if stop_type not in STOP_TYPES:
            return NONE
        if stop_type == "none":
            return NONE

        value = _coerce_float(account.get("stop_value"))
        if value is None or value <= 0:
            return NONE

        if stop_type == "stop_limit":
            offset = _coerce_float(account.get("stop_limit_offset"))
            if offset is None or offset <= 0:
                return NONE
            return cls(stop_type=stop_type, stop_value=value, stop_limit_offset=offset)

        return cls(stop_type=stop_type, stop_value=value, stop_limit_offset=None)


NONE = StopPolicy()


def validate_policy(stop_type: object, stop_value: object, stop_limit_offset: object) -> StopPolicy:
    """Strict parser for HTTP routes. Raises ValueError with a short human
    message on any violation; returns the NORMALIZED policy otherwise."""
    if stop_type is None or stop_type == "":
        stop_type = "none"
    else:
        stop_type = str(stop_type).strip().lower()

    if stop_type not in STOP_TYPES:
        raise ValueError(f"stop_type must be one of {STOP_TYPES}")

    if stop_type == "none":
        return NONE

    if stop_value is None or stop_value == "":
        raise ValueError("stop_value is required for this stop_type")

    try:
        value = float(stop_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("stop_value must be a positive number") from None

    if value <= 0:
        raise ValueError("stop_value must be greater than 0")

    if stop_type in ("stop", "stop_limit", "trailing_pct") and value >= 100:
        raise ValueError("stop_value must be less than 100 for percentage stops")

    if stop_type == "stop_limit":
        if stop_limit_offset is None or stop_limit_offset == "":
            raise ValueError("stop_limit_offset is required for stop_limit")

        try:
            offset = float(stop_limit_offset)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("stop_limit_offset must be a positive number less than 100") from None

        if offset <= 0 or offset >= 100:
            raise ValueError("stop_limit_offset must be a positive number less than 100")

        return StopPolicy(stop_type=stop_type, stop_value=value, stop_limit_offset=offset)

    return StopPolicy(stop_type=stop_type, stop_value=value, stop_limit_offset=None)


@dataclass(frozen=True)
class StopOutcome:
    action: str  # "hold" | "fill" | "arm"
    level: float
    limit_price: float | None
    fill_price: float | None
    exit_reason: str | None
    crossed: bool


def evaluate(
    policy: StopPolicy,
    *,
    entry: float,
    peak: float,
    mark: float,
    prev_mark: float | None = None,
    armed: bool = False,
) -> StopOutcome:
    """The single simulated-stop decision function shared by equity and options."""
    entry_f = float(entry or 0)
    if policy.stop_type == "none" or entry_f <= 0:
        return StopOutcome("hold", 0.0, None, None, None, False)

    value = float(policy.stop_value or 0)
    if value <= 0:
        return StopOutcome("hold", 0.0, None, None, None, False)

    peak_eff = max(float(peak or 0), entry_f)

    if policy.stop_type in ("stop", "stop_limit"):
        level = entry_f * (1 - value / 100)
    elif policy.stop_type == "trailing_pct":
        level = peak_eff * (1 - value / 100)
    else:  # trailing_dollar
        level = peak_eff - value

    level = round(level, 4)

    if policy.stop_type == "stop_limit":
        limit_price = round(level * (1 - float(policy.stop_limit_offset or 0) / 100), 4)
    else:
        limit_price = None

    if level <= 0:
        return StopOutcome("hold", level, limit_price, None, None, False)

    mark_f = float(mark)
    crossed = prev_mark is not None and float(prev_mark) > level
    reason = _EXIT_REASON[policy.stop_type]

    if policy.stop_type == "stop_limit":
        if armed:
            if mark_f >= limit_price:
                fill_price = round(max(limit_price, mark_f), 4)
                return StopOutcome("fill", level, limit_price, fill_price, reason, False)
            return StopOutcome("hold", level, limit_price, None, None, False)

        if mark_f > level:
            return StopOutcome("hold", level, limit_price, None, None, crossed)

        if mark_f >= limit_price:
            fill_price = round(level if crossed else mark_f, 4)
            return StopOutcome("fill", level, limit_price, fill_price, reason, crossed)

        return StopOutcome("arm", level, limit_price, None, reason, crossed)

    if mark_f > level:
        return StopOutcome("hold", level, None, None, None, crossed)

    fill_price = round(level if crossed else mark_f, 4)
    return StopOutcome("fill", level, None, fill_price, reason, crossed)


def describe_policy(policy: StopPolicy) -> str:
    """One plain-English sentence for the options allocator's LLM prompt."""
    if policy.stop_type == "none":
        return (
            "This account has NO automatic stop, so besides the DTE floor your "
            "CLOSE decisions are the only risk control."
        )

    value = policy.stop_value
    if policy.stop_type == "stop":
        return f"A hard stop force-closes any position whose premium falls {value:g}% below its entry premium."

    if policy.stop_type == "stop_limit":
        offset = policy.stop_limit_offset
        return (
            f"A stop-limit triggers {value:g}% below entry and only fills at or above "
            f"{offset:g}% below that trigger — a gap through can leave it unfilled."
        )

    if policy.stop_type == "trailing_pct":
        return (
            f"A trailing stop force-closes any position whose premium falls {value:g}% "
            "below its peak since entry, so gains ratchet in mechanically."
        )

    # trailing_dollar
    return (
        f"A trailing stop force-closes any position whose premium falls ${value:.2f} "
        "below its peak since entry, so gains ratchet in mechanically."
    )


def _coerce_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
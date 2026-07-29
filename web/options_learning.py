"""Options-ledger learning loop: grade closed positions, distill lessons.

The System C memory log grades DIRECTIONAL calls (the deep dives' Buy/Sell on
the underlying) by forward alpha — the right metric for a thesis about the
stock, and exactly the wrong one for an option position: a correct directional
call can still lose 100% of premium to time decay, and an early stop-out
diverges from the 5-day window entirely. This module grades what the options
ledger can actually measure, per closed/settled position:

    total   = exit_premium - entry_premium            (== realized_pnl / 100 / contracts)
    dU      = exit underlying - entry underlying
    directional ~= |entry_delta| * (+1 CALL / -1 PUT) * dU
    residual    = total - directional                 "time/vol decay"

HONESTY — what is NOT computable from the captured data, so we never pretend:
theta-vs-vega separation (no entry IV/theta/vega — only delta is captured),
IV-crush detection, MFE/MAE (marks are overwritten in place, no history), and
exit-side spread cost. The residual is labelled "time/vol decay (theta+IV,
not separable)" everywhere it surfaces.

Pipeline (nightly, web/scheduler.py::job_options_grade at 20:15 ET, after the
20:00 settle): backfill_exit_underlyings -> run_batch_reflection (ONE quick-LLM
call over recent graded closes, gated on enough NEW closes). At scan time
format_track_record() renders the latest lessons + mechanical stats into the
allocator prompt — pure Python, zero LLM cost.

Feedback-pathology guards: min-N gates before any stats are shown, [n<5] tags
on thin buckets, watch-for-only lesson phrasing, a fixed CAUTION footer, a hard
char cap, and lessons that regenerate only when new outcomes exist. The
allocator's hard guardrails (stop-loss, DTE floor, caps) are code-enforced and
never relaxed by lessons.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from . import db

log = logging.getLogger(__name__)

# Flat-move threshold for "direction correct": below this, the underlying's
# move is judged flat rather than right/wrong.
FLAT_MOVE_PCT = 0.005

# How far back the nightly job backfills missing exit underlyings. Older rows
# stay ungraded-for-attribution (still counted in win-rate stats).
BACKFILL_WINDOW_DAYS = 14

_CAUTION = (
    "CAUTION: small paper sample — treat as context, not rules; never override "
    "contract-level judgment or the hard risk limits because of it."
)


# ── Per-position attribution ─────────────────────────────────────────────────

def _days_between(a: str | None, b: str | None) -> int | None:
    try:
        da = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        dbb = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        return max(0, (dbb - da).days)
    except (TypeError, ValueError):
        return None


def _dte_at(expiration_date: str | None, at_iso: str | None) -> int | None:
    try:
        exp = datetime.strptime(str(expiration_date), "%Y-%m-%d").date()
        at = datetime.fromisoformat(str(at_iso).replace("Z", "+00:00")).date()
        return (exp - at).days
    except (TypeError, ValueError):
        return None


def exit_underlying_of(row: dict[str, Any]) -> float | None:
    """Underlying spot at exit: settlement_close for expiries, else the
    exit_underlying column (live capture or EOD backfill)."""
    if row.get("exit_reason") == "expiry" and row.get("settlement_close") is not None:
        return float(row["settlement_close"])
    v = row.get("exit_underlying")
    return float(v) if v is not None else None


def grade_position(row: dict[str, Any]) -> dict[str, Any]:
    """Attribution for one closed/settled position; NULL-tolerant.

    Returns a dict always containing return_pct/days_held/dte_entry/exit_reason
    and, when entry_delta + an exit underlying exist, the directional/residual
    split (per-share premium points) plus direction_correct and quadrant.
    """
    cost = float(row.get("cost_basis") or 0)
    realized = row.get("realized_pnl")
    out: dict[str, Any] = {
        "position_id": row.get("id"),
        "return_pct": (float(realized) / cost) if realized is not None and cost > 0 else None,
        "days_held": _days_between(row.get("opened_at"), row.get("closed_at")),
        "dte_entry": _dte_at(row.get("expiration_date"), row.get("opened_at")),
        "exit_reason": row.get("exit_reason"),
        "won": (float(realized) > 0) if realized is not None else None,
    }

    entry_u = row.get("entry_underlying")
    exit_u = exit_underlying_of(row)
    delta = row.get("entry_delta")
    entry_p = row.get("entry_premium")
    exit_p = row.get("exit_premium")
    if None in (entry_u, exit_u, delta, entry_p, exit_p) or not float(entry_u):
        out["attributed"] = False
        return out

    is_call = str(row.get("put_call") or "").upper().startswith("C")
    d_u = float(exit_u) - float(entry_u)
    total = float(exit_p) - float(entry_p)
    directional = abs(float(delta)) * (1.0 if is_call else -1.0) * d_u
    move_pct = d_u / float(entry_u)
    if move_pct > FLAT_MOVE_PCT:
        direction = "up"
    elif move_pct < -FLAT_MOVE_PCT:
        direction = "down"
    else:
        direction = "flat"
    # Three-valued on purpose: a flat underlying is neither a right nor a wrong
    # directional call — flat+lost is the PURE-theta loss, and lumping it into
    # "wrong" would blame direction for what decay did.
    correct: bool | None
    if direction == "flat":
        correct = None
    else:
        correct = direction == ("up" if is_call else "down")

    won = out["won"]
    if won is None:
        quadrant = None
    elif correct is None:
        quadrant = "flat_won" if won else "flat_lost"  # pure theta/vol outcome
    elif correct and won:
        quadrant = "right_won"
    elif correct and not won:
        quadrant = "right_lost"  # the decay toll — the key options-native stat
    elif not correct and won:
        quadrant = "wrong_won"
    else:
        quadrant = "wrong_lost"

    out.update({
        "attributed": True,
        "underlying_move_pct": move_pct,
        "total_points": total,
        "directional_points": directional,
        # theta + IV change + gamma convexity + mark noise — not separable
        # with only entry_delta captured.
        "residual_points": total - directional,
        "direction_correct": correct,
        "quadrant": quadrant,
    })
    return out


# ── Aggregate stats ──────────────────────────────────────────────────────────

def _bucket_dte(dte: int | None) -> str | None:
    if dte is None:
        return None
    if dte <= 7:
        return "<=7d"
    if dte <= 21:
        return "8-21d"
    return ">21d"


def _bucket_delta(delta: float | None) -> str | None:
    if delta is None:
        return None
    d = abs(float(delta))
    if d < 0.35:
        return "<0.35"
    if d <= 0.60:
        return "0.35-0.60"
    return ">0.60"


def _rate_line(items: list[dict[str, Any]]) -> str:
    n = len(items)
    wins = sum(1 for g in items if g.get("won"))
    tag = " [n<5 — ignore]" if n < 5 else ""
    return f"n={n} ({wins / n:.0%} win){tag}"


def compute_options_stats(rows: list[dict[str, Any]], min_closed: int = 10) -> dict[str, Any]:
    """Mechanical track-record stats over non-open positions.

    Returns {} below min_closed — with a handful of trades every stat is noise,
    and showing it would invite the allocator to overfit a tiny sample.
    """
    closed = [r for r in rows if r.get("status") != "open"]
    if len(closed) < min_closed:
        return {}
    grades = [grade_position(r) for r in closed]
    returns = [g["return_pct"] for g in grades if g["return_pct"] is not None]
    held = [g["days_held"] for g in grades if g["days_held"] is not None]
    wins = sum(1 for g in grades if g.get("won"))

    by_reason: dict[str, list[dict[str, Any]]] = {}
    by_dte: dict[str, list[dict[str, Any]]] = {}
    by_delta: dict[str, list[dict[str, Any]]] = {}
    for r, g in zip(closed, grades):
        if g.get("exit_reason"):
            by_reason.setdefault(str(g["exit_reason"]), []).append(g)
        b = _bucket_dte(g.get("dte_entry"))
        if b:
            by_dte.setdefault(b, []).append(g)
        b = _bucket_delta(r.get("entry_delta"))
        if b:
            by_delta.setdefault(b, []).append(g)

    attributed = [g for g in grades if g.get("attributed")]
    # Denominator matches the numerator's population: only ATTRIBUTED losers
    # can be classified right/flat/wrong, so unattributed losers must not
    # dilute the share (they'd understate the decay toll).
    attributed_losers = [g for g in attributed if g.get("won") is False]
    decay_lost = [g for g in attributed_losers
                  if g.get("quadrant") in ("right_lost", "flat_lost")]

    return {
        "n": len(closed),
        "win_rate": wins / len(closed),
        "avg_return_pct": sum(returns) / len(returns) if returns else None,
        "median_return_pct": median(returns) if returns else None,
        "avg_days_held": sum(held) / len(held) if held else None,
        "by_exit_reason": {k: _rate_line(v) for k, v in sorted(by_reason.items())},
        "by_dte_entry": {k: _rate_line(by_dte[k]) for k in ("<=7d", "8-21d", ">21d") if k in by_dte},
        "by_delta": {k: _rate_line(by_delta[k]) for k in ("<0.35", "0.35-0.60", ">0.60") if k in by_delta},
        "n_attributed": len(attributed),
        "n_attributed_losers": len(attributed_losers),
        # right_lost + flat_lost: direction didn't fail, decay did.
        "decay_lost_share_of_losers": (len(decay_lost) / len(attributed_losers))
        if attributed_losers else None,
    }


def format_track_record(stats: dict[str, Any], lessons_md: str | None,
                        max_chars: int = 1200) -> str:
    """Render the allocator prompt block; "" when stats are gated empty."""
    if not stats:
        return ""
    lines = [f"=== OPTIONS TRACK RECORD (this paper account, {stats['n']} closed) ==="]
    core = f"Win rate {stats['win_rate']:.0%}"
    if stats.get("avg_return_pct") is not None:
        core += f" | avg return {stats['avg_return_pct']:+.0%}"
    if stats.get("median_return_pct") is not None:
        core += f" | median {stats['median_return_pct']:+.0%}"
    if stats.get("avg_days_held") is not None:
        core += f" | avg hold {stats['avg_days_held']:.0f}d"
    lines.append(core)
    if stats.get("by_exit_reason"):
        lines.append("By exit: " + " | ".join(f"{k} {v}" for k, v in stats["by_exit_reason"].items()))
    if stats.get("by_dte_entry"):
        lines.append("By DTE at entry: " + " | ".join(f"{k} {v}" for k, v in stats["by_dte_entry"].items()))
    if stats.get("by_delta"):
        lines.append("By |delta|: " + " | ".join(f"{k} {v}" for k, v in stats["by_delta"].items()))
    if stats.get("decay_lost_share_of_losers") is not None:
        lines.append(
            f"Losses where direction did NOT fail (right or flat underlying): "
            f"{stats['decay_lost_share_of_losers']:.0%} of attributable losers "
            f"(time/vol decay toll — theta+IV, not separable; "
            f"{stats['n_attributed_losers']} attributable losers of {stats['n']} closed)"
        )
    if lessons_md and lessons_md.strip():
        lines.append("LESSONS from past closes (watch-fors, not rules):")
        lines.append(lessons_md.strip())
    lines.append(_CAUTION)
    block = "\n".join(lines)
    if len(block) > max_chars:
        # Trim at a line boundary, never into negative slice territory, and
        # always keep the CAUTION footer (worst tiny-cap case: footer only).
        keep = max(0, max_chars - len(_CAUTION) - 1)
        truncated = block[:keep]
        if "\n" in truncated:
            truncated = truncated.rsplit("\n", 1)[0]
        block = (truncated.rstrip() + "\n" + _CAUTION) if truncated.strip() else _CAUTION
    return block


# ── Nightly grading pipeline ─────────────────────────────────────────────────

def backfill_exit_underlyings(paper_account_id: int) -> int:
    """Fill missing exit underlyings on recent non-expiry closes via as-of EOD
    close lookup. Best-effort per row; returns how many were filled.

    'eod_close' is honest labelling: a 09:35 ET close backfilled with that
    day's EOD close includes post-close drift, so stats can discount it.
    """
    from . import options_engine  # lazy — options_engine imports this module

    cutoff = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_WINDOW_DAYS)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")
    rows = [
        r for r in db.list_options_positions(paper_account_id, status="settled")
        if r.get("exit_underlying") is None
        and r.get("exit_reason") != "expiry"  # expiries carry settlement_close
        and (r.get("closed_at") or "") >= cutoff
    ]
    filled = 0
    for r in rows:
        try:
            close = options_engine.underlying_close_on_or_before(
                r["underlying"], str(r.get("closed_at") or "")[:10]
            )
            if close is not None:
                db.set_options_exit_underlying(int(r["id"]), close, "eod_close")
                filled += 1
        except Exception:
            log.exception("[options_learning] backfill failed for position %s", r.get("id"))
    return filled


def _grade_line(row: dict[str, Any], g: dict[str, Any]) -> str:
    """One compact line per graded close for the reflection prompt."""
    cp = "C" if str(row.get("put_call") or "").upper().startswith("C") else "P"
    strike = row.get("strike")
    strike_txt = f"{strike:g}" if isinstance(strike, (int, float)) else "?"
    base = (
        f"{row.get('underlying')} {strike_txt}{cp} | {row.get('signal')} "
        f"{row.get('conviction')}/10 | delta {row.get('entry_delta') if row.get('entry_delta') is not None else 'n/a'} | "
        f"{g.get('dte_entry')}d DTE, held {g.get('days_held')}d | {g.get('exit_reason')}"
    )
    ret = g.get("return_pct")
    base += f" | total {ret:+.0%}" if ret is not None else " | total n/a"
    if g.get("attributed"):
        dc = g["direction_correct"]
        label = "FLAT" if dc is None else ("RIGHT" if dc else "WRONG")
        base += (
            f" (dir {g['directional_points']:+.2f}pt, decay {g['residual_points']:+.2f}pt)"
            f" | direction {label}"
        )
    return base


_REFLECTION_SYSTEM = (
    "You are reviewing your own recent OPTIONS paper trades (long single-leg "
    "calls/puts). Each line shows one closed position with its P&L split into a "
    "directional component (delta x underlying move) and a residual labelled "
    "'decay' (theta + IV change + convexity — not separable with the data "
    "captured; do not attribute it more precisely than 'time/vol decay').\n\n"
    "Rules:\n"
    "1. Lessons must be falsifiable watch-fors phrased as \"watch for ...\" — "
    "never \"always ...\" or \"never ...\". A pattern from a handful of trades "
    "is a hypothesis, not a rule.\n"
    "2. Focus on options-specific craft: entry DTE vs realized hold time, delta "
    "choice vs decay toll, exit discipline (stop_loss/dte_floor/llm_close/expiry "
    "outcomes), conviction calibration.\n"
    "3. At most 5 lessons, total under 600 characters. Plain lines starting "
    "with \"- watch for\", no markdown headers, no restating the stats."
)


def run_batch_reflection(
    paper_account_id: int,
    llm: Any,
    config: dict[str, Any],
) -> bool:
    """ONE LLM call distilling recent closes into lessons; False when gated.

    Gate: at least options_reflect_min_new_closed positions closed since the
    last lessons row — lessons regenerate only on new outcomes, never by
    rewriting themselves over the same data (a self-reinforcement guard).
    """
    min_new = int(config.get("options_reflect_min_new_closed", 5))
    batch_max = int(config.get("options_reflect_batch_max", 20))
    last = db.latest_options_lesson(paper_account_id)
    n_new = db.count_closed_options_since(
        paper_account_id, last["created_at"] if last else None
    )
    if n_new < min_new:
        log.info("[options_learning] account %s: %d new closes (< %d) — skipping reflection",
                 paper_account_id, n_new, min_new)
        return False
    if llm is None:
        log.warning("[options_learning] account %s: LLM unavailable — reflection deferred",
                    paper_account_id)
        return False

    closed = db.list_options_positions(paper_account_id, status="settled")
    closed.sort(key=lambda r: r.get("closed_at") or "", reverse=True)
    batch = closed[:batch_max]
    grades = [grade_position(r) for r in batch]
    lines = [_grade_line(r, g) for r, g in zip(batch, grades)]
    stats = compute_options_stats(closed, min_closed=1)  # full stats for the record

    resp = llm.invoke([
        {"role": "system", "content": _REFLECTION_SYSTEM},
        {"role": "user", "content": "Recent closed positions (newest first):\n" + "\n".join(lines)},
    ])
    lessons = (resp.content if hasattr(resp, "content") else str(resp)).strip()[:600]
    db.insert_options_lesson(
        paper_account_id,
        n_closed_total=len(closed),
        n_new=n_new,
        stats_json=json.dumps(stats, default=str),
        lessons_md=lessons,
        model=str(config.get("quick_think_llm") or ""),
    )
    log.info("[options_learning] account %s: reflected over %d closes (%d new)",
             paper_account_id, len(batch), n_new)
    return True

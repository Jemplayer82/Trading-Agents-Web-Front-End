"""Aggregate calibration statistics over the resolved trading memory log.

Individual reflections are n=1 stories contaminated by luck; across the whole
resolved log, luck averages toward zero, so per-rating hit rates and average
alpha are the *trustworthy* learning signal. This module computes those stats
from ``TradingMemoryLog.load_entries()`` output and renders the compact block
injected above the anecdotal lessons in agent context.

Honesty guards built into the presentation:
- rates with n<10 are flagged as small-sample noise;
- the classification distribution is tallied so a self-excuse skew (too many
  "exogenous surprise" labels on misses) becomes visible automatically;
- censored/unrated/crypto entries are counted but excluded from equity rates;
- fixed CAUTION lines state the horizon and the overlapping-window caveat.
"""

from __future__ import annotations

import re
from datetime import datetime

from tradingagents.agents.utils.rating import RATINGS_5_TIER
from tradingagents.agents.utils.scoring import HIT, MISS, UNINFORMATIVE, score_outcome

# First line of sweep-era reflections: "CLASS: <LABEL> | HORIZON: ..."
_CLASS_RE = re.compile(r"^CLASS:\s*([A-Z-]+)", re.MULTILINE)

# Share of misses labelled exogenous above which we flag probable self-excuse.
_EXO_WARN_FRAC = 0.40
_SMALL_SAMPLE_N = 10


def _parse_pct(value: str | None) -> float | None:
    """Parse a formatted percent string from the log tag ('+15.1%' -> 0.151)."""
    if not value:
        return None
    try:
        return float(value.replace("%", "").strip()) / 100.0
    except ValueError:
        return None


def compute_calibration(entries: list[dict], config: dict | None = None) -> dict:
    """Per-rating scoring + classification distribution over resolved entries."""
    config = config or {}
    band = config.get("noise_alpha_threshold", 0.02)
    censor_after = config.get("sweep_censor_after_days", 30)
    holding = config.get("reflection_holding_days", 5)

    per_rating: dict[str, dict] = {
        r: {"hit": 0, "miss": 0, "uninformative": 0, "alphas": []} for r in RATINGS_5_TIER
    }
    classes: dict[str, int] = {}
    miss_classes: dict[str, int] = {}
    tickers: set[str] = set()
    n_scored_total = 0
    n_crypto = 0
    n_unrated = 0
    n_censored = 0

    today = datetime.now()
    n_stale_pending = 0

    for e in entries:
        if e.get("pending"):
            # Pending entries old enough that the sweep would have censored or
            # resolved them are counted as unresolvable (probable delistings).
            try:
                age = (today - datetime.strptime(e["date"], "%Y-%m-%d")).days
                if age > censor_after + holding * 2:
                    n_stale_pending += 1
            except ValueError:
                pass
            continue

        reflection = e.get("reflection") or ""
        m = _CLASS_RE.search(reflection)
        cls = m.group(1) if m else None
        if cls:
            classes[cls] = classes.get(cls, 0) + 1
        if cls == "CENSORED":
            n_censored += 1
            continue

        if e["ticker"].upper().endswith("-USD"):
            # Crypto resolves with alpha == raw ("absolute" benchmark) — mixing
            # it into equity alpha rates would corrupt both.
            n_crypto += 1
            continue

        rating = e.get("rating") or "Unrated"
        alpha = _parse_pct(e.get("alpha"))
        if rating not in per_rating or alpha is None:
            n_unrated += 1
            continue

        outcome = score_outcome(rating, alpha, band)
        if outcome is None:
            n_unrated += 1
            continue

        bucket = per_rating[rating]
        bucket[outcome] += 1
        bucket["alphas"].append(alpha)
        tickers.add(e["ticker"])
        n_scored_total += 1
        if outcome == MISS and cls:
            miss_classes[cls] = miss_classes.get(cls, 0) + 1

    return {
        "per_rating": per_rating,
        "classes": classes,
        "miss_classes": miss_classes,
        "n_total": n_scored_total,
        "n_tickers": len(tickers),
        "n_crypto": n_crypto,
        "n_unrated": n_unrated,
        "n_censored": n_censored + n_stale_pending,
        "band": band,
        "holding": holding,
    }


def format_calibration(stats: dict) -> str:
    """Render the calibration block injected above anecdotal lessons.

    Returns "" when nothing is scored yet — no block beats an empty table.
    """
    if stats["n_total"] == 0:
        return ""

    band = stats["band"]
    holding = stats["holding"]
    lines = [
        f"CALIBRATION — {stats['n_total']} resolved decisions across "
        f"{stats['n_tickers']} tickers, {holding}d alpha vs benchmark, "
        f"noise band +/-{band:.1%}:"
    ]

    for rating in RATINGS_5_TIER:
        b = stats["per_rating"][rating]
        n = b[HIT] + b[MISS] + b[UNINFORMATIVE]
        if n == 0:
            continue
        small = "  [SMALL SAMPLE — ignore]" if n < _SMALL_SAMPLE_N else ""
        avg_alpha = sum(b["alphas"]) / len(b["alphas"])
        if rating == "Hold":
            avg_abs = sum(abs(a) for a in b["alphas"]) / len(b["alphas"])
            lines.append(
                f"  {rating + ':':<12} {b[HIT]}/{n} stayed inside band, "
                f"avg |alpha| {avg_abs:.1%}  (n={n}){small}"
            )
        else:
            n_scored = b[HIT] + b[MISS]
            lines.append(
                f"  {rating + ':':<12} {b[HIT]}/{n_scored} directional hits, "
                f"avg alpha {avg_alpha:+.1%}  (n={n}, {b[UNINFORMATIVE]} in noise band){small}"
            )

    if stats["classes"]:
        total_cls = sum(stats["classes"].values())
        dist = ", ".join(
            f"{k.lower()} {100 * v // total_cls}%"
            for k, v in sorted(stats["classes"].items(), key=lambda kv: -kv[1])
        )
        exo_flag = ""
        n_miss = sum(stats["miss_classes"].values())
        if n_miss:
            exo_share = stats["miss_classes"].get("EXOGENOUS-SURPRISE", 0) / n_miss
            if exo_share > _EXO_WARN_FRAC:
                exo_flag = (
                    f"  [WARNING: exogenous share of misses >{int(_EXO_WARN_FRAC * 100)}% "
                    "— likely self-excuse bias]"
                )
        lines.append(f"  Outcome classes: {dist}{exo_flag}")

    excluded = []
    if stats["n_censored"]:
        excluded.append(f"{stats['n_censored']} censored/unresolvable (possible delistings)")
    if stats["n_unrated"]:
        excluded.append(f"{stats['n_unrated']} unrated")
    if stats["n_crypto"]:
        excluded.append(f"{stats['n_crypto']} crypto (absolute-return, scored separately)")
    if excluded:
        lines.append(f"  Excluded from the rates above: {', '.join(excluded)}.")

    lines.append(
        f"CAUTION: rates with n<{_SMALL_SAMPLE_N} are noise — do not change behavior based on them. "
        "Consecutive same-ticker windows overlap, so effective sample sizes are smaller than n. "
        f"These stats describe {holding}-day outcomes only, not long-horizon thesis quality."
    )
    return "\n".join(lines)

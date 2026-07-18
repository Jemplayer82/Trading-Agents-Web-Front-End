"""Mechanical per-rating outcome scoring for calibration statistics.

Computed in Python, never by the LLM — the whole point is a grading rule the
model can't argue with. The rule (noise band N, alpha vs benchmark over the
holding window):

    Rating                Hit           Miss          Uninformative
    Buy / Overweight      alpha >= +N   alpha <= -N   |alpha| < N
    Hold                  |alpha| < N   |alpha| >= N  —
    Underweight / Sell    alpha <= -N   alpha >= +N   |alpha| < N

Buy vs Overweight (and Sell vs Underweight) share the same direction test —
conviction is a position-sizing distinction, not a directional one; per-class
average alpha captures whether conviction tiers actually separate.

``Unrated`` (parse failure sentinel) returns ``None`` and is excluded from
per-rating statistics entirely.
"""

from __future__ import annotations

BULLISH = {"Buy", "Overweight"}
BEARISH = {"Sell", "Underweight"}

HIT = "hit"
MISS = "miss"
UNINFORMATIVE = "uninformative"


def score_outcome(rating: str, alpha: float, band: float) -> str | None:
    """Score one resolved outcome; ``None`` when the rating isn't scoreable."""
    if rating in BULLISH:
        if alpha >= band:
            return HIT
        if alpha <= -band:
            return MISS
        return UNINFORMATIVE
    if rating in BEARISH:
        if alpha <= -band:
            return HIT
        if alpha >= band:
            return MISS
        return UNINFORMATIVE
    if rating == "Hold":
        return HIT if abs(alpha) < band else MISS
    return None

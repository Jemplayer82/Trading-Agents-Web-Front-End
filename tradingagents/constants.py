"""Project-wide domain constants.

Small, stable vocabulary shared across the web layer and the agent graph.
Centralising these avoids the same string literals drifting apart (or picking
up typos) in separate modules.
"""

from __future__ import annotations

# The three trade signals an analysis can resolve to. Used wherever code needs
# the full set — e.g. initialising a per-signal tally. NOTE: this is the *set*
# of signals; code that depends on a particular match/priority order (such as
# graph.portfolio_graph._signal_from_decision) keeps its own ordered tuple on
# purpose, so don't assume this ordering is significant.
SIGNALS = ("BUY", "HOLD", "SELL")

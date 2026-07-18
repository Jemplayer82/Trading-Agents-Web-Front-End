# TradingAgents/graph/reflection.py

from typing import Any


class Reflector:
    """Handles reflection on trading decisions.

    Grades the decision *process*, not the outcome: a sound thesis can lose
    over a short window and a broken one can win. To keep hindsight bias and
    self-serving attribution out of the memory log, the prompt (a) restricts
    judgment to the explicitly-labelled holding window, (b) requires cited
    in-window evidence before an outcome may be classified as an exogenous
    surprise, and (c) only permits falsifiable "watch for ..." lessons —
    never "always/never" directives generalized from a single outcome.
    """

    def __init__(self, quick_thinking_llm: Any):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm

    _PROMPT_TEMPLATE = (
        "You are reviewing one of your own past trading decisions now that the "
        "{holding_days}-trading-day outcome is known. You will receive: the original "
        "decision (rating: {rating}), the raw return, the alpha vs {benchmark}, the "
        "noise band (+/-{noise_pct:.1%}), and news published during the holding window "
        "(may be empty or incomplete — news retrieval is unreliable for past windows).\n\n"
        "Rules:\n"
        "1. This grades a {holding_days}-day window ONLY. If the thesis was longer-term, "
        "judge the decision process, not the thesis verdict — a sound process can lose "
        "in {holding_days} days.\n"
        "2. Classify the outcome as exactly one of: CONFIRMED-THESIS, FORESEEABLE-MISS, "
        "EXOGENOUS-SURPRISE, NOISE.\n"
        "3. EXOGENOUS-SURPRISE requires citing a specific headline (date + source) from "
        "the provided news. No citable headline = the label is forbidden; use "
        "FORESEEABLE-MISS (note \"no in-window evidence\") or NOISE instead. Genuine "
        "exogenous surprises are rare.\n"
        "4. Never invent causes. If you cannot point to evidence, say \"no in-window "
        "evidence\".\n"
        "5. A lesson from one outcome is a hypothesis, not a rule. Phrase it as "
        "\"watch for ...\", never \"always/never ...\". \"none — outcome uninformative\" "
        "is a valid lesson.\n"
        "6. Separate what is specific to this ticker (THESIS line) from what transfers "
        "(LESSON line).\n\n"
        "Output exactly 5 lines, plain text, no markdown:\n"
        "CLASS: <label> | HORIZON: {holding_days}d | EVIDENCE: <headline date+source, or \"none\">\n"
        "CALL: <direction right/wrong/inside noise band, citing the alpha figure>\n"
        "THESIS: <the specific claim in the decision that held or broke — ticker-specific>\n"
        "LESSON: <one transferable, falsifiable hypothesis, or \"none — outcome uninformative\">\n"
        "CONFIDENCE: <low|medium|high — strength of support this single outcome gives the lesson>"
    )

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        benchmark_name: str = "SPY",
        *,
        rating: str = "Unrated",
        holding_days: int = 5,
        noise_pct: float = 0.02,
        news_context: str = "",
    ) -> str:
        """Single reflection call on the final trade decision with outcome context.

        Used by the outcome-resolution sweep (and the same-ticker resolution in
        ``TradingAgentsGraph.propagate``). ``news_context`` is the best-effort
        holding-window headline pull — empty when unavailable, which the prompt
        treats as "no evidence" (exogenous claims are then forbidden).
        """
        system = self._PROMPT_TEMPLATE.format(
            holding_days=holding_days,
            rating=rating,
            benchmark=benchmark_name,
            noise_pct=noise_pct,
        )
        news_block = news_context.strip() or "(no news retrieved for this window)"
        messages = [
            ("system", system),
            (
                "human",
                (
                    f"Raw return: {raw_return:+.1%}\n"
                    f"Alpha vs {benchmark_name}: {alpha_return:+.1%}\n\n"
                    f"News during the holding window:\n{news_block}\n\n"
                    f"Final Decision:\n{final_decision}"
                ),
            ),
        ]
        return self.quick_thinking_llm.invoke(messages).content

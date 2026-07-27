"""
Decides whether ranked results are confident enough to act on directly,
or whether the user should be asked to pick between close matches.

This replaces the old behavior of silently opening result[0] whenever
confidence dipped below a threshold, or opening the top hit even when a
near-tied second candidate existed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .ranking import ScoredResult

ABSOLUTE_MARGIN_THRESHOLD = 20.0   # points
RELATIVE_MARGIN_THRESHOLD = 0.15   # 15% of the top score
MIN_ACT_SCORE = 40.0               # below this, don't act even if unopposed


@dataclass
class Decision:
    action: str  # "act", "clarify", "no_match"
    best: Optional[ScoredResult] = None
    candidates: Optional[List[ScoredResult]] = None
    reason: str = ""


class Disambiguator:
    def decide(self, ranked: List[ScoredResult], top_n: int = 5) -> Decision:
        if not ranked:
            return Decision(action="no_match", reason="No results matched the query.")

        best = ranked[0]
        if best.score < MIN_ACT_SCORE:
            return Decision(
                action="clarify",
                candidates=ranked[:top_n],
                reason="No result was confident enough to act on automatically.",
            )

        if len(ranked) == 1:
            return Decision(action="act", best=best)

        second = ranked[1]
        margin = best.score - second.score
        relative_margin = margin / best.score if best.score else 0

        if margin < ABSOLUTE_MARGIN_THRESHOLD and relative_margin < RELATIVE_MARGIN_THRESHOLD:
            return Decision(
                action="clarify",
                candidates=ranked[:top_n],
                reason="Multiple results have similar confidence.",
            )

        return Decision(action="act", best=best)

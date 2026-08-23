"""Choosing what goes in the cut like an editor, not like a threshold.

Selection up to Session 9 is local: ``usefulness >= 0.40``, dead air goes,
danger stays. Every one of those judgements is made from eight seconds of
footage, and no amount of tuning lets them see that a dull stretch is the
setup for something twenty minutes later, that the episode opens on walking,
or that the same joke has now landed three times.

This package reads the whole structured episode -- transcript, beats, open
loops, setups and payoffs, risk zones, hook candidates, the style guide, and
what the rule-based pass already thinks -- and asks a model to decide what the
cut is. Then it checks every answer with rules.

**The model proposes; the deterministic layer disposes.** A decision arrives
with ``accepted=False`` and stays that way unless eleven checks say otherwise.
Ranges come from segment IDs rather than from timestamps a model typed, so a
hallucinated range resolves to nothing and is rejected with a reason instead
of becoming footage that does not exist.

Nothing here executes anything, requires Premiere, or replaces the heuristic
selector -- which remains the fallback whenever the model is unreachable,
unparseable, or was never asked.
"""
from __future__ import annotations

from editing.director.schema import (
    DirectorConfig, DirectorContext, DirectorDecision, DirectorFailure,
    DirectorPlan, DirectorPrompt, DirectorRange, DirectorReason,
    DirectorResult, DirectorSafetyReview, StyleGuide,
)

__all__ = [
    "DirectorConfig", "DirectorContext", "DirectorDecision", "DirectorFailure",
    "DirectorPlan", "DirectorPrompt", "DirectorRange", "DirectorReason",
    "DirectorResult", "DirectorSafetyReview", "StyleGuide",
]

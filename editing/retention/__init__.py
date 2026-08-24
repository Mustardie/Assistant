"""Wiring the retention findings into the cut itself.

Session 8 built the retention planner -- hook candidates, risk zones, setups
and payoffs, a peak, an ending -- and executed nothing. The handoff has said
"nothing consumes the retention suggestions" ever since.

This package is the consumer. It reshapes a cut around what that planner found:

* the strongest hook moves to the front as a cold open, and the footage it was
  lifted from does not play twice
* stretches the planner called sagging are compressed -- sped up when the
  picture is changing, cut when it is not
* a setup whose payoff is in the cut is protected before anything that removes
  footage runs, so no later rule can take it out
* silence that is doing nothing is trimmed hard; silence that is doing
  something is left alone

Nothing here executes anything or requires Premiere, the cut it reads is never
modified, and every refusal names the rule that made it. No count in any of it
is a claim about an audience.
"""
from __future__ import annotations

from editing.retention.schema import (
    ColdOpenPlan, DeadAirDecision, PayoffProtectionDecision,
    RetentionCutComparison, RetentionCutConfig, RetentionCutDecision,
    RetentionCutFailure, RetentionCutPlan, RetentionCutReport,
    SagCompressionPlan, SetupProtectionDecision, SourceSpan,
)

__all__ = [
    "ColdOpenPlan", "DeadAirDecision", "PayoffProtectionDecision",
    "RetentionCutComparison", "RetentionCutConfig", "RetentionCutDecision",
    "RetentionCutFailure", "RetentionCutPlan", "RetentionCutReport",
    "SagCompressionPlan", "SetupProtectionDecision", "SourceSpan",
]

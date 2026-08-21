"""Episode-level intelligence: what the video is, and where it might lose people.

Sessions 1-7 reason about clips, segments and moments. This layer reasons about
one *episode*: its objective, its beats, the questions it raises, whether it
answers them, where it sags, and what could open it.

It executes nothing. It produces two JSON artifacts and two reports:

* ``EpisodeMemory`` -- what happened
* ``EpisodeRetentionPlan`` -- what to do about it

and a list of ``RetentionSuggestion`` records that Sessions 3, 5 and 6 can read
without this layer knowing anything about Premiere.

**It cannot know retention.** See ``schema.NOT_ANALYTICS``.
"""
from editing.episode.schema import (  # noqa: F401
    ClimaxCandidate, EndingCandidate, EpisodeBeat, EpisodeCallback,
    EpisodeCharacterRole, EpisodeEvidence, EpisodeLocation, EpisodeMemory,
    EpisodeMotif, EpisodeObjective, EpisodeOpenLoop, EpisodePayoff,
    EpisodeRetentionPlan, EpisodeRiskZone, EpisodeSetup, HookCandidate,
    NOT_ANALYTICS, RetentionSuggestion,
)

__all__ = [
    "ClimaxCandidate", "EndingCandidate", "EpisodeBeat", "EpisodeCallback",
    "EpisodeCharacterRole", "EpisodeEvidence", "EpisodeLocation",
    "EpisodeMemory", "EpisodeMotif", "EpisodeObjective", "EpisodeOpenLoop",
    "EpisodePayoff", "EpisodeRetentionPlan", "EpisodeRiskZone", "EpisodeSetup",
    "HookCandidate", "NOT_ANALYTICS", "RetentionSuggestion",
]

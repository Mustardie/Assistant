"""Record types for the episode layer.

Sessions 1-7 think in clips, segments and moments. This layer thinks in one
*episode*: a thing with an objective, a middle that can sag, a question the
viewer is waiting to have answered, and an ending that either pays off or does
not. Nothing here executes anything. It produces two artifacts:

* ``EpisodeMemory`` -- **what happened**: beats, objectives, places, people,
  recurring motifs, setups, payoffs, callbacks and open loops.
* ``EpisodeRetentionPlan`` -- **what to do about it**: risk zones, hook
  candidates, a climax candidate, an ending candidate, and the suggestions a
  later pass can consume.

The split is deliberate. Memory is an observation and survives a restyle; the
plan is an opinion about the observation, and re-planning must never be able to
rewrite what was observed.

## What this layer can and cannot know

It cannot know retention. It has never seen a retention graph, it has no
audience data, and it is not connected to anything that does. Every "risk" here
is a *creative* risk read off edit evidence -- a long stretch with no stakes,
an objective that is never stated, a setup with no payoff. Those are real
things worth flagging, and they are not predictions of a curve.

That honesty is structural rather than a disclaimer at the bottom of a report:

* **Confidence is capped by how many independent channels agree.** One channel
  -- a keyword, say -- can never exceed ``CONFIDENCE_CAP[1]``, which sits below
  ``MIN_EDIT_CONFIDENCE``. So a keyword-only finding is structurally incapable
  of affecting an edit. Three agreeing channels still cap below 0.9, because
  nothing here is ever certain.
* **A recommendation is not an observation.** Sessions 2-6 derived their
  records *from* the visual, transcript and audio records, so counting them as
  a fourth channel would let one observation vote twice. They add a small
  bonus; they never raise the cap.
* **Nothing is deleted, only marked.** Same rule as every pass before it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from editing.schema import (
    _slug, as_bool, as_float, as_str_list, clamp01, short_hash,
)

# ---------------------------------------------------------------------------
# The honesty statement, in one place
# ---------------------------------------------------------------------------

#: Printed on every report this layer produces. It is a constant rather than
#: prose in six render functions so it cannot drift into a claim.
NOT_ANALYTICS = (
    "This is not retention analytics. Nothing here has seen an audience, a "
    "retention graph or a single view. Every risk below is a creative risk "
    "read off edit evidence, and every confidence is a statement about how "
    "much evidence agreed -- not about what viewers will do."
)

#: Phrases this layer must never put in a finding. Not a style preference: a
#: suggestion that says "this will boost retention" is claiming a measurement
#: nothing here has taken, and a person reading a hundred of them would
#: reasonably start believing it. ``contains_claim`` exists so the rule can be
#: asserted over every generated string instead of reviewed by eye.
FORBIDDEN_CLAIMS = (
    "guarantee", "guaranteed", "will retain", "retention rate",
    "watch time", "boost retention", "viewers will", "audience will",
    "increase views", "more views", "algorithm", "proven to",
)


def contains_claim(text: Any) -> Optional[str]:
    """The first forbidden phrase in ``text``, or ``None``.

    Applies to the *findings* -- reasons, marker text, suggested copy. The
    ``NOT_ANALYTICS`` banner deliberately contains the word "analytics" while
    denying it, and is checked separately rather than run through here.
    """
    lowered = str(text or "").lower()
    for phrase in FORBIDDEN_CLAIMS:
        if phrase in lowered:
            return phrase
    return None


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

#: The independent observation channels. Deliberately three: what the vision
#: model saw, what was said, and what was measured in the audio.
OBSERVATION_CHANNELS = ("visual", "transcript", "audio")

#: The highest confidence reachable with N agreeing channels. Note ``1`` sits
#: below ``MIN_EDIT_CONFIDENCE``: a single-channel finding cannot drive an edit
#: no matter how strong it looks, which is what "do not depend only on
#: keywords" means when it is enforced rather than asked for.
CONFIDENCE_CAP = {0: 0.25, 1: 0.45, 2: 0.70, 3: 0.88}

#: Below this, an item is recorded but never marked as edit-affecting.
MIN_EDIT_CONFIDENCE = 0.55

#: At or below this, an item is flagged for a human regardless of what it says.
REVIEW_AT_OR_BELOW = 0.50


def cap_for(channel_count: int) -> float:
    """The confidence ceiling for a finding backed by ``channel_count`` channels."""
    return CONFIDENCE_CAP.get(max(0, min(3, int(channel_count))), 0.88)


def capped(score: Any, channels: Iterable[str]) -> float:
    """Clamp ``score`` to what the given evidence channels can support."""
    names = {c for c in channels if c in OBSERVATION_CHANNELS}
    return round(min(clamp01(score, 0.0), cap_for(len(names))), 3)


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: The story beats this layer can label. Closed, because a downstream planner
#: that pattern-matches free text breaks the day the detector says "buildup".
BEAT_KINDS = (
    "setup",             # the episode establishing itself
    "objective_stated",  # someone says what they are trying to do
    "plan_explained",    # how they intend to do it
    "travel",            # getting somewhere
    "preparation",       # gearing up before the thing
    "grind",             # the same action, repeated, for a while
    "discovery",         # finding something
    "danger",            # something could go wrong right now
    "failure",           # it went wrong
    "recovery",          # getting back from it going wrong
    "escalation",        # the stakes go up
    "joke",              # a laugh or a bit
    "callback",          # a reference to something earlier
    "payoff",            # the thing that was set up, delivered
    "reveal",            # the viewer learns something
    "climax",            # the biggest moment
    "resolution",        # it is settled
    "outro",             # signing off
    "unknown",           # detected nothing worth naming -- kept, not hidden
)

#: Beats that answer a question rather than ask one.
PAYOFF_BEATS = frozenset({
    "discovery", "payoff", "reveal", "climax", "resolution", "recovery",
})

#: Beats that ask a question rather than answer one.
SETUP_BEATS = frozenset({
    "setup", "objective_stated", "plan_explained", "preparation", "danger",
    "escalation",
})

#: Beats that are structurally low-interest. Not "bad" -- an episode needs
#: them -- but a long unbroken run of them is the thing this layer looks for.
QUIET_BEATS = frozenset({"travel", "grind", "setup", "plan_explained", "unknown"})

OBJECTIVE_STATUSES = ("stated", "implied", "achieved", "failed", "abandoned",
                      "unresolved", "unknown")

#: What a recurring person is to the episode. ``unknown`` is the honest default
#: for a name heard twice with no other information.
ROLE_KINDS = ("narrator", "co_op", "guest", "rival", "npc", "mentioned",
              "unknown")

#: What kind of thing recurs. One class covers recurring jokes, repeated
#: failures, repeated dangers and important items because the *detection* is
#: identical -- a thing observed more than once, in more than one place -- and
#: only the label differs.
MOTIF_KINDS = ("joke", "failure", "danger", "item", "phrase", "place",
               "unknown")

LOOP_STATUSES = ("open", "resolved", "possibly_resolved", "abandoned",
                 "unknown")

#: What an open loop, setup or callback suggests doing. Every one of these is
#: a *suggestion to a later pass*, never an operation.
SUGGESTED_USES = (
    "keep_setup", "shorten_setup", "tease_payoff", "add_callback_marker",
    "add_card_marker", "flag_for_hook", "use_as_climax",
    "use_as_ending_payoff", "needs_human_review",
)

#: The ways an episode can lose someone, as far as edit evidence can show it.
RISK_TYPES = (
    "boring_repetition",     # the same action for a long time
    "no_clear_objective",    # nobody ever says what this is for
    "overlong_explanation",  # talking, at length, over nothing
    "confusing_transition",  # a cut that drops the viewer somewhere new
    "dead_air",              # measured silence, aggregated
    "low_visual_change",     # the picture stops moving
    "no_stakes",             # nothing could go wrong for a long time
    "payoff_delayed",        # a question asked far too long ago
    "unresolved_setup",      # a question never answered at all
    "weak_hook",             # the opening carries no reason to stay
    "mid_video_slump",       # the middle is the flattest part
    "anticlimax",            # the biggest moment is not near the end
    "unclear_ending",        # it stops rather than ends
)

#: Risks whose evidence is *measured* rather than inferred. Only these may ever
#: carry an automatically-safe fix, and even then only above
#: ``MIN_EDIT_CONFIDENCE``.
MEASURED_RISKS = frozenset({"dead_air", "low_visual_change"})

SEVERITIES = ("low", "medium", "high")

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

HOOK_TYPES = ("danger", "mystery", "failure", "comedy", "reveal", "goal",
              "challenge", "unknown")

#: Where a hook's suggested text came from. A line lifted from the transcript
#: is something that was actually said; anything else is this system writing
#: copy, and is labelled so nobody ships it unread.
TEXT_SOURCES = ("transcript_quote", "generated_description", "none")

#: What a later pass could do about a finding.
SUGGESTION_TYPES = (
    "keep_setup",
    "shorten_boring",
    "add_callback_caption",
    "add_teaser_marker",
    "add_card",
    "add_music_rise_marker",
    "hold_silence_for_comedy",
    "speed_up_grind",
    "clarify_objective",
    "add_goal_marker",
    "mark_climax",
    "mark_ending_payoff",
    "needs_human_review",
)

#: Suggestions that cannot remove or retime anything. Most of them annotate;
#: ``keep_setup`` protects, which is the same guarantee from the other side --
#: it can only ever stop a later pass cutting something. These are the only
#: suggestions that can be automatically safe, because the worst case of a
#: wrong one is a marker in the wrong place or a range nobody trimmed.
MARKER_SUGGESTIONS = frozenset({
    "add_callback_caption", "add_teaser_marker", "add_card",
    "add_music_rise_marker", "clarify_objective", "add_goal_marker",
    "mark_climax", "mark_ending_payoff", "keep_setup",
})

#: Suggestions that would change what the viewer sees or how long for. Never
#: automatic from this layer, whatever the confidence.
TIMING_SUGGESTIONS = frozenset({
    "shorten_boring", "speed_up_grind", "hold_silence_for_comedy",
})

#: Which pass would act on a suggestion. ``human`` means no pass should.
DOWNSTREAM = ("roughcut", "style", "assets", "human")

#: Default routing. A suggestion that changes ranges goes to the rough cut, one
#: that draws goes to the style pass, one that makes noise goes to assets.
DOWNSTREAM_FOR = {
    "keep_setup": "roughcut",
    "shorten_boring": "roughcut",
    "speed_up_grind": "roughcut",
    "add_callback_caption": "style",
    "add_teaser_marker": "style",
    "add_card": "style",
    "clarify_objective": "style",
    "add_goal_marker": "style",
    "mark_climax": "style",
    "mark_ending_payoff": "style",
    "add_music_rise_marker": "assets",
    "hold_silence_for_comedy": "assets",
    "needs_human_review": "human",
}

#: What a suggestion is meant to do to the viewer. Same vocabulary as Session
#: 2 plus ``curiosity``, so a downstream pass mostly already knows it.
VIEWER_EFFECTS = (
    "clarity", "tension", "comedy", "impact", "pacing", "explanation",
    "anticipation", "payoff", "curiosity", "unknown",
)

#: Which time domain an episode's ranges are in. This matters more than it
#: looks: ``roughcut`` ranges are sequence time and can be handed straight to a
#: later pass, while ``timeline`` ranges are a synthetic ordering over source
#: footage that no Premiere sequence has ever seen.
TIMEBASES = ("roughcut", "timeline", "empty")


def coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def coerce_many(value: Any, allowed: Sequence[str], limit: int = 20) -> list[str]:
    out: list[str] = []
    for item in as_str_list(value, limit=limit * 3):
        token = _slug(item)
        if token in allowed and token not in out:
            out.append(token)
    return out[:limit]


def severity_at_least(value: str, floor: str) -> bool:
    return _SEVERITY_ORDER.get(value, 0) >= _SEVERITY_ORDER.get(floor, 0)


def severity_from(score: Any, *, medium: float = 0.45, high: float = 0.7) -> str:
    value = clamp01(score, 0.0)
    if value >= high:
        return "high"
    if value >= medium:
        return "medium"
    return "low"


def _text(value: Any, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

@dataclass
class EpisodeEvidence:
    """The records a finding was read off.

    IDs rather than copies, so an episode file stays small and cannot drift out
    of sync with the timeline it describes. ``quotes`` carries the handful of
    spoken lines needed to review a finding without cross-referencing.
    """

    segment_ids: list[str] = field(default_factory=list)
    visual_event_ids: list[str] = field(default_factory=list)
    audio_event_ids: list[str] = field(default_factory=list)
    audio_types: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    #: Session 2 recommendations that agree. Corroboration, never a channel.
    recommendation_ids: list[str] = field(default_factory=list)
    #: Rough-cut clips this finding sits on.
    placement_ids: list[str] = field(default_factory=list)
    #: Session 5 layer items and Session 6 placeholders that agree.
    layer_item_ids: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.segment_ids or self.visual_event_ids or self.audio_event_ids
            or self.quotes or self.recommendation_ids
        )

    @property
    def channels(self) -> list[str]:
        """The independent observation channels present.

        ``recommendation_ids`` and ``layer_item_ids`` are deliberately absent:
        both were derived from the three below, and letting them count would
        let a single observation raise its own confidence ceiling.
        """
        present = []
        if self.visual_event_ids:
            present.append("visual")
        if self.quotes:
            present.append("transcript")
        if self.audio_event_ids:
            present.append("audio")
        return present

    @property
    def corroborated(self) -> bool:
        """Whether an earlier pass independently proposed something here."""
        return bool(self.recommendation_ids or self.layer_item_ids)

    def merged(self, other: "EpisodeEvidence") -> "EpisodeEvidence":
        """Union of two evidence records, order-preserving and deduplicated."""
        def union(a: list, b: list, limit: int) -> list:
            out = list(a)
            for item in b:
                if item not in out:
                    out.append(item)
            return out[:limit]

        return EpisodeEvidence(
            segment_ids=union(self.segment_ids, other.segment_ids, 200),
            visual_event_ids=union(
                self.visual_event_ids, other.visual_event_ids, 200),
            audio_event_ids=union(
                self.audio_event_ids, other.audio_event_ids, 200),
            audio_types=union(self.audio_types, other.audio_types, 20),
            quotes=union(self.quotes, other.quotes, 30),
            recommendation_ids=union(
                self.recommendation_ids, other.recommendation_ids, 100),
            placement_ids=union(
                self.placement_ids, other.placement_ids, 100),
            layer_item_ids=union(
                self.layer_item_ids, other.layer_item_ids, 100),
            summary=self.summary or other.summary,
        )

    def to_dict(self) -> dict:
        return {
            "segment_ids": list(self.segment_ids),
            "visual_event_ids": list(self.visual_event_ids),
            "audio_event_ids": list(self.audio_event_ids),
            "audio_types": list(self.audio_types),
            "quotes": list(self.quotes),
            "recommendation_ids": list(self.recommendation_ids),
            "placement_ids": list(self.placement_ids),
            "layer_item_ids": list(self.layer_item_ids),
            "summary": self.summary,
            "channels": self.channels,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "EpisodeEvidence":
        data = data or {}
        return cls(
            segment_ids=as_str_list(data.get("segment_ids"), limit=200),
            visual_event_ids=as_str_list(
                data.get("visual_event_ids"), limit=200),
            audio_event_ids=as_str_list(data.get("audio_event_ids"), limit=200),
            audio_types=as_str_list(data.get("audio_types"), limit=20),
            quotes=[str(q)[:300] for q in (data.get("quotes") or [])][:30],
            recommendation_ids=as_str_list(
                data.get("recommendation_ids"), limit=100),
            placement_ids=as_str_list(data.get("placement_ids"), limit=100),
            layer_item_ids=as_str_list(data.get("layer_item_ids"), limit=100),
            summary=_text(data.get("summary"), 500),
        )


# ---------------------------------------------------------------------------
# The common shape
# ---------------------------------------------------------------------------

@dataclass
class EpisodeItem:
    """What every finding in this layer carries.

    Subclasses add their own fields; the base guarantees that anything this
    layer produces can be placed on a timeline, traced to its evidence, ranked
    by confidence, and told apart from something a person still has to look at.
    """

    item_id: str = ""
    start: float = 0.0
    end: float = 0.0
    confidence: float = 0.0
    why: str = ""
    evidence: EpisodeEvidence = field(default_factory=EpisodeEvidence)
    #: Whether a later pass may act on this without a human first.
    affects_edit: bool = False
    needs_human_review: bool = True
    notes: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def segment_ids(self) -> list[str]:
        return list(self.evidence.segment_ids)

    @property
    def channels(self) -> list[str]:
        return self.evidence.channels

    def overlaps(self, start: float, end: float) -> float:
        return max(0.0, min(self.end, end) - max(self.start, start))

    def settle(self) -> "EpisodeItem":
        """Apply the two confidence rules to this item, in place.

        Called by every builder before an item is returned. Keeping it in one
        place is what makes "a keyword-only finding cannot drive an edit" a
        property of the layer rather than a habit of whoever wrote the detector.
        """
        self.confidence = capped(self.confidence, self.channels)
        if self.confidence < MIN_EDIT_CONFIDENCE:
            self.affects_edit = False
        if self.confidence <= REVIEW_AT_OR_BELOW:
            self.needs_human_review = True
        return self

    def base_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "confidence": round(self.confidence, 3),
            "why": self.why,
            "segment_ids": self.segment_ids,
            "evidence": self.evidence.to_dict(),
            "channels": self.channels,
            "affects_edit": self.affects_edit,
            "needs_human_review": self.needs_human_review,
            "notes": self.notes,
        }

    @staticmethod
    def base_kwargs(data: dict) -> dict:
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        evidence = EpisodeEvidence.from_dict(data.get("evidence"))
        # A file written by hand may put segment IDs at the top level; keep
        # them rather than lose the only trace back to the footage.
        for segment_id in as_str_list(data.get("segment_ids"), limit=200):
            if segment_id not in evidence.segment_ids:
                evidence.segment_ids.append(segment_id)
        return {
            "item_id": str(data.get("item_id") or ""),
            "start": start,
            "end": end,
            "confidence": clamp01(data.get("confidence"), 0.0),
            "why": _text(data.get("why"), 600),
            "evidence": evidence,
            "affects_edit": as_bool(data.get("affects_edit")),
            "needs_human_review": as_bool(
                data.get("needs_human_review"), True),
            "notes": _text(data.get("notes"), 600),
        }


# ---------------------------------------------------------------------------
# What happened
# ---------------------------------------------------------------------------

@dataclass
class EpisodeBeat(EpisodeItem):
    """One stretch of episode doing one story job."""

    kind: str = "unknown"
    #: The label's runner-up, kept so a merge or a review can see how close it
    #: was. A beat that scored 0.51 against 0.49 is not the same claim as one
    #: that scored 0.9 against 0.1, and the difference should survive.
    alternative: str = ""
    #: Detector scores per kind, best first. Debugging aid, not a contract.
    scores: dict = field(default_factory=dict)
    #: How many detector slots were merged into this beat.
    span_count: int = 1
    #: 0..1 position of the beat's midpoint in the episode.
    position: float = 0.0
    #: 0..1 measured interest, from importance weight, motion and reactions.
    interest: float = 0.0

    @property
    def is_quiet(self) -> bool:
        return self.kind in QUIET_BEATS

    def summary(self) -> str:
        return (
            f"[{self.start:7.2f}-{self.end:7.2f}] {self.kind:<17} "
            f"c={self.confidence:.2f} i={self.interest:.2f}  {self.why[:60]}"
        )

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "kind": self.kind,
            "alternative": self.alternative,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "span_count": self.span_count,
            "position": round(self.position, 4),
            "interest": round(self.interest, 4),
            "is_quiet": self.is_quiet,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeBeat":
        scores = {}
        for key, value in (data.get("scores") or {}).items():
            token = _slug(key)
            if token in BEAT_KINDS:
                scores[token] = as_float(value)
        return cls(
            **EpisodeItem.base_kwargs(data),
            kind=coerce_one(data.get("kind"), BEAT_KINDS, "unknown"),
            alternative=coerce_one(data.get("alternative"), BEAT_KINDS, ""),
            scores=scores,
            span_count=max(1, int(as_float(data.get("span_count"), 1))),
            position=clamp01(data.get("position"), 0.0),
            interest=clamp01(data.get("interest"), 0.0),
        )


@dataclass
class EpisodeObjective(EpisodeItem):
    """Something the episode is trying to achieve."""

    text: str = ""
    status: str = "unknown"
    #: The content words the objective is about, used to match a later payoff.
    topic: list[str] = field(default_factory=list)
    #: When it was achieved or abandoned, if it was.
    resolved_at: Optional[float] = None
    #: The open loop this objective opened, when one was created for it.
    open_loop_id: str = ""
    primary: bool = False

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "text": self.text,
            "status": self.status,
            "topic": list(self.topic),
            "resolved_at": (
                None if self.resolved_at is None
                else round(self.resolved_at, 3)
            ),
            "open_loop_id": self.open_loop_id,
            "primary": self.primary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeObjective":
        resolved = data.get("resolved_at")
        return cls(
            **EpisodeItem.base_kwargs(data),
            text=_text(data.get("text"), 300),
            status=coerce_one(
                data.get("status"), OBJECTIVE_STATUSES, "unknown"),
            topic=as_str_list(data.get("topic"), limit=20),
            resolved_at=None if resolved is None else as_float(resolved),
            open_loop_id=str(data.get("open_loop_id") or ""),
            primary=as_bool(data.get("primary")),
        )


@dataclass
class EpisodeCharacterRole(EpisodeItem):
    """A person who recurs: a co-op partner, a guest, a name that keeps coming up.

    ``start``/``end`` are first and last mention, so the range is the span of
    their involvement rather than one moment.
    """

    name: str = ""
    role: str = "unknown"
    mentions: int = 0
    #: Every time they were named, in episode time.
    mention_times: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "name": self.name,
            "role": self.role,
            "mentions": self.mentions,
            "mention_times": [round(t, 3) for t in self.mention_times],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeCharacterRole":
        return cls(
            **EpisodeItem.base_kwargs(data),
            name=_text(data.get("name"), 80),
            role=coerce_one(data.get("role"), ROLE_KINDS, "unknown"),
            mentions=max(0, int(as_float(data.get("mentions")))),
            mention_times=[
                as_float(t) for t in (data.get("mention_times") or [])
            ][:80],
        )


@dataclass
class EpisodeLocation(EpisodeItem):
    """A place the episode spends time in, with how long and how often."""

    environment: str = "unknown"
    #: Seconds spent there in total, which is not ``duration`` when the episode
    #: returns to it -- and the difference is what makes a place a *return*.
    total_seconds: float = 0.0
    visits: int = 1
    visit_starts: list[float] = field(default_factory=list)
    #: True when this is the place the episode spends the most time.
    primary: bool = False

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "environment": self.environment,
            "total_seconds": round(self.total_seconds, 3),
            "visits": self.visits,
            "visit_starts": [round(t, 3) for t in self.visit_starts],
            "primary": self.primary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeLocation":
        return cls(
            **EpisodeItem.base_kwargs(data),
            environment=str(data.get("environment") or "unknown"),
            total_seconds=as_float(data.get("total_seconds")),
            visits=max(1, int(as_float(data.get("visits"), 1))),
            visit_starts=[
                as_float(t) for t in (data.get("visit_starts") or [])
            ][:80],
            primary=as_bool(data.get("primary")),
        )


@dataclass
class EpisodeMotif(EpisodeItem):
    """A thing that happens more than once.

    Recurring jokes, repeated failures, repeated dangers and the items that
    keep mattering are one class because the detection is identical -- a thing
    observed in more than one place -- and only the label differs.
    """

    label: str = ""
    kind: str = "unknown"
    occurrences: int = 0
    occurrence_times: list[float] = field(default_factory=list)

    @property
    def spread(self) -> float:
        """How far apart the first and last occurrence are."""
        if len(self.occurrence_times) < 2:
            return 0.0
        return max(self.occurrence_times) - min(self.occurrence_times)

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "label": self.label,
            "kind": self.kind,
            "occurrences": self.occurrences,
            "occurrence_times": [round(t, 3) for t in self.occurrence_times],
            "spread": round(self.spread, 3),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeMotif":
        return cls(
            **EpisodeItem.base_kwargs(data),
            label=_text(data.get("label"), 120),
            kind=coerce_one(data.get("kind"), MOTIF_KINDS, "unknown"),
            occurrences=max(0, int(as_float(data.get("occurrences")))),
            occurrence_times=[
                as_float(t) for t in (data.get("occurrence_times") or [])
            ][:80],
        )


@dataclass
class EpisodeSetup(EpisodeItem):
    """A moment that plants something the episode can spend later."""

    text: str = ""
    topic: list[str] = field(default_factory=list)
    #: The payoff that spent it, when one was found.
    payoff_id: str = ""
    suggested_use: str = "keep_setup"

    @property
    def paid_off(self) -> bool:
        return bool(self.payoff_id)

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "text": self.text,
            "topic": list(self.topic),
            "payoff_id": self.payoff_id,
            "paid_off": self.paid_off,
            "suggested_use": self.suggested_use,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeSetup":
        return cls(
            **EpisodeItem.base_kwargs(data),
            text=_text(data.get("text"), 300),
            topic=as_str_list(data.get("topic"), limit=20),
            payoff_id=str(data.get("payoff_id") or ""),
            suggested_use=coerce_one(
                data.get("suggested_use"), SUGGESTED_USES, "keep_setup"),
        )


@dataclass
class EpisodePayoff(EpisodeItem):
    """A moment that spends something planted earlier."""

    text: str = ""
    topic: list[str] = field(default_factory=list)
    setup_id: str = ""
    #: Seconds between the setup and this. A payoff nobody remembers the setup
    #: of is a different problem from one that lands too fast.
    gap_seconds: float = 0.0
    #: How the topics matched, so a weak link is visible as a weak link.
    match_reason: str = ""
    suggested_use: str = "use_as_ending_payoff"

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "text": self.text,
            "topic": list(self.topic),
            "setup_id": self.setup_id,
            "gap_seconds": round(self.gap_seconds, 3),
            "match_reason": self.match_reason,
            "suggested_use": self.suggested_use,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodePayoff":
        return cls(
            **EpisodeItem.base_kwargs(data),
            text=_text(data.get("text"), 300),
            topic=as_str_list(data.get("topic"), limit=20),
            setup_id=str(data.get("setup_id") or ""),
            gap_seconds=as_float(data.get("gap_seconds")),
            match_reason=_text(data.get("match_reason"), 300),
            suggested_use=coerce_one(
                data.get("suggested_use"), SUGGESTED_USES,
                "use_as_ending_payoff"),
        )


@dataclass
class EpisodeCallback(EpisodeItem):
    """A later moment that refers back to an earlier one.

    ``start``/``end`` are the *later* moment -- the place a caption would go --
    and ``refers_to_time`` is what it points at.
    """

    label: str = ""
    kind: str = "unknown"
    refers_to_time: float = 0.0
    refers_to_id: str = ""
    #: The shared content words. Empty means the link is positional, not
    #: topical, which is a much weaker claim and should look like one.
    topic: list[str] = field(default_factory=list)
    suggested_use: str = "add_callback_marker"
    suggested_text: str = ""

    @property
    def gap_seconds(self) -> float:
        return max(0.0, self.start - self.refers_to_time)

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "label": self.label,
            "kind": self.kind,
            "refers_to_time": round(self.refers_to_time, 3),
            "refers_to_id": self.refers_to_id,
            "topic": list(self.topic),
            "gap_seconds": round(self.gap_seconds, 3),
            "suggested_use": self.suggested_use,
            "suggested_text": self.suggested_text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeCallback":
        return cls(
            **EpisodeItem.base_kwargs(data),
            label=_text(data.get("label"), 120),
            kind=coerce_one(data.get("kind"), MOTIF_KINDS, "unknown"),
            refers_to_time=as_float(data.get("refers_to_time")),
            refers_to_id=str(data.get("refers_to_id") or ""),
            topic=as_str_list(data.get("topic"), limit=20),
            suggested_use=coerce_one(
                data.get("suggested_use"), SUGGESTED_USES,
                "add_callback_marker"),
            suggested_text=_text(data.get("suggested_text"), 200),
        )


@dataclass
class EpisodeOpenLoop(EpisodeItem):
    """A question the episode raises, and whether it ever answers it.

    ``start``/``end`` are where the question is *asked*. ``resolved_at`` is
    where it is answered, which may be nowhere.
    """

    question: str = ""
    #: The words the question is about, used to match a later answer.
    topic: list[str] = field(default_factory=list)
    why_viewer_cares: str = ""
    status: str = "open"
    resolved_at: Optional[float] = None
    resolution_id: str = ""
    resolution_reason: str = ""
    #: Later moments that *might* answer it, best first. A candidate is not a
    #: resolution: it is a place to look.
    candidate_payoffs: list[float] = field(default_factory=list)
    suggested_use: str = "keep_setup"

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"

    @property
    def wait_seconds(self) -> float:
        """How long the viewer is asked to hold the question."""
        if self.resolved_at is None:
            return 0.0
        return max(0.0, self.resolved_at - self.start)

    def summary(self) -> str:
        where = (
            f"-> {self.resolved_at:7.2f}" if self.resolved_at is not None
            else "-> (open)   "
        )
        return (
            f"[{self.start:7.2f}] {where} {self.status:<18} "
            f"c={self.confidence:.2f}  {self.question[:56]}"
        )

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "question": self.question,
            "topic": list(self.topic),
            "why_viewer_cares": self.why_viewer_cares,
            "status": self.status,
            "resolved": self.resolved,
            "resolved_at": (
                None if self.resolved_at is None
                else round(self.resolved_at, 3)
            ),
            "resolution_id": self.resolution_id,
            "resolution_reason": self.resolution_reason,
            "wait_seconds": round(self.wait_seconds, 3),
            "candidate_payoffs": [
                round(t, 3) for t in self.candidate_payoffs
            ],
            "suggested_use": self.suggested_use,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeOpenLoop":
        resolved_at = data.get("resolved_at")
        return cls(
            **EpisodeItem.base_kwargs(data),
            question=_text(data.get("question"), 300),
            topic=as_str_list(data.get("topic"), limit=20),
            why_viewer_cares=_text(data.get("why_viewer_cares"), 300),
            status=coerce_one(data.get("status"), LOOP_STATUSES, "open"),
            resolved_at=None if resolved_at is None else as_float(resolved_at),
            resolution_id=str(data.get("resolution_id") or ""),
            resolution_reason=_text(data.get("resolution_reason"), 300),
            candidate_payoffs=[
                as_float(t) for t in (data.get("candidate_payoffs") or [])
            ][:20],
            suggested_use=coerce_one(
                data.get("suggested_use"), SUGGESTED_USES, "keep_setup"),
        )


# ---------------------------------------------------------------------------
# What to do about it
# ---------------------------------------------------------------------------

@dataclass
class EpisodeRiskZone(EpisodeItem):
    """A stretch that may cost the episode a viewer, and why it might.

    "May". Nothing here has measured a viewer. See :data:`NOT_ANALYTICS`.
    """

    risk: str = "boring_repetition"
    severity: str = "low"
    #: 0..1 magnitude before it was bucketed into a severity, kept so two
    #: "medium" risks can still be ranked against each other.
    score: float = 0.0
    suggested_fix: str = "needs_human_review"
    #: Whether the fix could be applied without a human looking first. False
    #: for anything that changes timing, at any confidence.
    fix_is_safe_automatically: bool = False
    #: What to write on the timeline instead, when the fix is not automatic.
    marker_fallback: str = ""
    #: Beats this zone covers, for context in the report.
    beat_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mark = {"high": "!!", "medium": "! ", "low": "  "}
        return (
            f"{mark.get(self.severity, '  ')} "
            f"[{self.start:7.2f}-{self.end:7.2f}] {self.risk:<21} "
            f"{self.severity:<6} c={self.confidence:.2f}  {self.why[:52]}"
        )

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "risk": self.risk,
            "severity": self.severity,
            "score": round(self.score, 3),
            "suggested_fix": self.suggested_fix,
            "fix_is_safe_automatically": self.fix_is_safe_automatically,
            "marker_fallback": self.marker_fallback,
            "beat_ids": list(self.beat_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeRiskZone":
        return cls(
            **EpisodeItem.base_kwargs(data),
            risk=coerce_one(data.get("risk"), RISK_TYPES, "boring_repetition"),
            severity=coerce_one(data.get("severity"), SEVERITIES, "low"),
            score=clamp01(data.get("score"), 0.0),
            suggested_fix=coerce_one(
                data.get("suggested_fix"), SUGGESTION_TYPES,
                "needs_human_review"),
            fix_is_safe_automatically=as_bool(
                data.get("fix_is_safe_automatically")),
            marker_fallback=_text(data.get("marker_fallback"), 200),
            beat_ids=as_str_list(data.get("beat_ids"), limit=100),
        )


@dataclass
class HookCandidate(EpisodeItem):
    """A moment that could open the episode.

    ``suggested_text`` is either something that was actually said or a plain
    description of what is on screen, and ``text_source`` says which. There is
    no third option: this layer does not write copy the footage cannot support.
    """

    hook_type: str = "unknown"
    suggested_text: str = ""
    text_source: str = "none"
    viewer_question: str = ""
    #: Where the question this hook opens gets answered, if it does.
    payoff_at: Optional[float] = None
    payoff_id: str = ""
    #: 0..1 ranking score. Not a confidence: a hook can be well-evidenced and
    #: still be a weak hook, and the two must not be collapsed.
    score: float = 0.0
    #: The score, itemised. This is what makes a ranking arguable.
    score_parts: dict = field(default_factory=dict)
    #: Seconds of prior context needed for the moment to land. Lower is better:
    #: a hook that needs explaining is not a hook.
    setup_seconds: float = 0.0
    risks: list[str] = field(default_factory=list)

    @property
    def has_payoff(self) -> bool:
        return self.payoff_at is not None

    def summary(self) -> str:
        payoff = (
            f"payoff@{self.payoff_at:.1f}" if self.payoff_at is not None
            else "no payoff found"
        )
        return (
            f"[{self.start:7.2f}-{self.end:7.2f}] {self.hook_type:<9} "
            f"s={self.score:.2f} c={self.confidence:.2f} {payoff:<18} "
            f"{self.suggested_text[:44]}"
        )

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "hook_type": self.hook_type,
            "suggested_text": self.suggested_text,
            "text_source": self.text_source,
            "viewer_question": self.viewer_question,
            "payoff_at": (
                None if self.payoff_at is None else round(self.payoff_at, 3)
            ),
            "payoff_id": self.payoff_id,
            "has_payoff": self.has_payoff,
            "score": round(self.score, 3),
            "score_parts": {
                k: round(v, 3) for k, v in self.score_parts.items()
            },
            "setup_seconds": round(self.setup_seconds, 3),
            "risks": list(self.risks),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HookCandidate":
        payoff_at = data.get("payoff_at")
        return cls(
            **EpisodeItem.base_kwargs(data),
            hook_type=coerce_one(data.get("hook_type"), HOOK_TYPES, "unknown"),
            suggested_text=_text(data.get("suggested_text"), 200),
            text_source=coerce_one(
                data.get("text_source"), TEXT_SOURCES, "none"),
            viewer_question=_text(data.get("viewer_question"), 200),
            payoff_at=None if payoff_at is None else as_float(payoff_at),
            payoff_id=str(data.get("payoff_id") or ""),
            score=clamp01(data.get("score"), 0.0),
            score_parts={
                str(k): as_float(v)
                for k, v in (data.get("score_parts") or {}).items()
            },
            setup_seconds=as_float(data.get("setup_seconds")),
            risks=as_str_list(data.get("risks"), limit=20),
        )


@dataclass
class ClimaxCandidate(EpisodeItem):
    """The biggest moment, and how clearly it is the biggest.

    ``margin`` is what separates "this is the climax" from "these three are
    about equal": a small margin over the runner-up means the episode does not
    have an obvious peak, which is itself worth knowing.
    """

    score: float = 0.0
    score_parts: dict = field(default_factory=dict)
    position: float = 0.0
    #: How far ahead of the runner-up this scored, 0..1.
    margin: float = 0.0
    #: Open loops this moment closes.
    resolves_loop_ids: list[str] = field(default_factory=list)
    beat_ids: list[str] = field(default_factory=list)

    @property
    def is_late(self) -> bool:
        """Whether it falls where a climax usually belongs."""
        return self.position >= 0.55

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "score": round(self.score, 3),
            "score_parts": {
                k: round(v, 3) for k, v in self.score_parts.items()
            },
            "position": round(self.position, 4),
            "margin": round(self.margin, 3),
            "is_late": self.is_late,
            "resolves_loop_ids": list(self.resolves_loop_ids),
            "beat_ids": list(self.beat_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClimaxCandidate":
        return cls(
            **EpisodeItem.base_kwargs(data),
            score=clamp01(data.get("score"), 0.0),
            score_parts={
                str(k): as_float(v)
                for k, v in (data.get("score_parts") or {}).items()
            },
            position=clamp01(data.get("position"), 0.0),
            margin=clamp01(data.get("margin"), 0.0),
            resolves_loop_ids=as_str_list(
                data.get("resolves_loop_ids"), limit=50),
            beat_ids=as_str_list(data.get("beat_ids"), limit=50),
        )


@dataclass
class EndingCandidate(EpisodeItem):
    """A moment the episode could end on."""

    kind: str = "resolution"
    score: float = 0.0
    position: float = 0.0
    #: Whether it closes the episode's stated objective, rather than any loop.
    closes_main_objective: bool = False
    resolves_loop_ids: list[str] = field(default_factory=list)
    suggested_text: str = ""
    text_source: str = "none"

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "kind": self.kind,
            "score": round(self.score, 3),
            "position": round(self.position, 4),
            "closes_main_objective": self.closes_main_objective,
            "resolves_loop_ids": list(self.resolves_loop_ids),
            "suggested_text": self.suggested_text,
            "text_source": self.text_source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EndingCandidate":
        return cls(
            **EpisodeItem.base_kwargs(data),
            kind=coerce_one(data.get("kind"), BEAT_KINDS, "resolution"),
            score=clamp01(data.get("score"), 0.0),
            position=clamp01(data.get("position"), 0.0),
            closes_main_objective=as_bool(data.get("closes_main_objective")),
            resolves_loop_ids=as_str_list(
                data.get("resolves_loop_ids"), limit=50),
            suggested_text=_text(data.get("suggested_text"), 200),
            text_source=coerce_one(
                data.get("text_source"), TEXT_SOURCES, "none"),
        )


@dataclass
class RetentionSuggestion(EpisodeItem):
    """One thing a later pass could do, and everything needed to judge it.

    This is the layer's actual output -- the thing Sessions 3, 5 and 6 will
    eventually consume. It is a *suggestion*: it carries no Premiere operation,
    and ``downstream`` names who would have to build one.
    """

    type: str = "needs_human_review"
    reason: str = ""
    viewer_effect: str = "unknown"
    #: 0..1 ranking against other suggestions. Not a confidence.
    priority: float = 0.5
    #: Whether a pass may act on this without a human looking first.
    auto_safe: bool = False
    #: What to write on the timeline when it is not safe to act. Every
    #: suggestion has one, including the safe ones: the fallback is what makes
    #: refusing to act still useful.
    marker_fallback: str = ""
    downstream: str = "human"
    beat_ids: list[str] = field(default_factory=list)
    open_loop_ids: list[str] = field(default_factory=list)
    risk_ids: list[str] = field(default_factory=list)
    setup_ids: list[str] = field(default_factory=list)
    payoff_ids: list[str] = field(default_factory=list)

    @property
    def is_marker_only(self) -> bool:
        return not self.auto_safe

    def summary(self) -> str:
        mark = "+" if self.auto_safe else "="
        return (
            f"{mark} [{self.start:7.2f}-{self.end:7.2f}] {self.type:<24} "
            f"{self.downstream:<9} p={self.priority:.2f} c={self.confidence:.2f}"
            f"  {self.reason[:48]}"
        )

    def to_dict(self) -> dict:
        return {
            **self.base_dict(),
            "type": self.type,
            "reason": self.reason,
            "viewer_effect": self.viewer_effect,
            "priority": round(self.priority, 3),
            "auto_safe": self.auto_safe,
            "is_marker_only": self.is_marker_only,
            "marker_fallback": self.marker_fallback,
            "downstream": self.downstream,
            "beat_ids": list(self.beat_ids),
            "open_loop_ids": list(self.open_loop_ids),
            "risk_ids": list(self.risk_ids),
            "setup_ids": list(self.setup_ids),
            "payoff_ids": list(self.payoff_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RetentionSuggestion":
        return cls(
            **EpisodeItem.base_kwargs(data),
            type=coerce_one(
                data.get("type"), SUGGESTION_TYPES, "needs_human_review"),
            reason=_text(data.get("reason"), 500),
            viewer_effect=coerce_one(
                data.get("viewer_effect"), VIEWER_EFFECTS, "unknown"),
            priority=clamp01(data.get("priority"), 0.5),
            auto_safe=as_bool(data.get("auto_safe")),
            marker_fallback=_text(data.get("marker_fallback"), 200),
            downstream=coerce_one(data.get("downstream"), DOWNSTREAM, "human"),
            beat_ids=as_str_list(data.get("beat_ids"), limit=100),
            open_loop_ids=as_str_list(data.get("open_loop_ids"), limit=100),
            risk_ids=as_str_list(data.get("risk_ids"), limit=100),
            setup_ids=as_str_list(data.get("setup_ids"), limit=100),
            payoff_ids=as_str_list(data.get("payoff_ids"), limit=100),
        )


# ---------------------------------------------------------------------------
# The two artifacts
# ---------------------------------------------------------------------------

@dataclass
class EpisodeMemory:
    """What the episode is, as far as the evidence shows.

    Every range in here is in ``timebase``. When that is ``roughcut`` the
    numbers are sequence time on ``sequence_name`` and a later pass can use
    them directly; when it is ``timeline`` they are a synthetic ordering over
    source footage that no sequence has ever seen, and a consumer has to go
    through ``segment_ids``. Getting that wrong would put captions in the wrong
    places, so it is a field rather than a convention.
    """

    episode_id: str = ""
    name: str = "structure"
    sequence_name: str = ""
    timebase: str = "empty"
    duration: float = 0.0

    main_objective: Optional[EpisodeObjective] = None
    secondary_objectives: list[EpisodeObjective] = field(default_factory=list)
    locations: list[EpisodeLocation] = field(default_factory=list)
    roles: list[EpisodeCharacterRole] = field(default_factory=list)
    motifs: list[EpisodeMotif] = field(default_factory=list)
    beats: list[EpisodeBeat] = field(default_factory=list)
    setups: list[EpisodeSetup] = field(default_factory=list)
    payoffs: list[EpisodePayoff] = field(default_factory=list)
    callbacks: list[EpisodeCallback] = field(default_factory=list)
    open_loops: list[EpisodeOpenLoop] = field(default_factory=list)

    #: Measured interest per sampled point: ``[[time, 0..1], ...]``. An
    #: observation, which is why it lives in the memory rather than the plan.
    interest_curve: list[list] = field(default_factory=list)
    #: The measured local maxima of that curve.
    retention_spikes: list[float] = field(default_factory=list)

    #: What was actually available when this was built. A memory built without
    #: a transcript is a different claim from one built with, and the report
    #: has to be able to say so.
    sources: dict = field(default_factory=dict)
    generated_at: str = ""
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def objectives(self) -> list[EpisodeObjective]:
        """Every objective, main first."""
        out = [self.main_objective] if self.main_objective else []
        return out + list(self.secondary_objectives)

    @property
    def resolved_loops(self) -> list[EpisodeOpenLoop]:
        return [loop for loop in self.open_loops if loop.resolved]

    @property
    def unresolved_loops(self) -> list[EpisodeOpenLoop]:
        return [loop for loop in self.open_loops if not loop.resolved]

    @property
    def is_empty(self) -> bool:
        return not self.beats and not self.open_loops

    def beat_at(self, when: float) -> Optional[EpisodeBeat]:
        for beat in self.beats:
            if beat.start <= when < beat.end:
                return beat
        return self.beats[-1] if self.beats and when >= self.duration else None

    def beats_of(self, *kinds: str) -> list[EpisodeBeat]:
        wanted = set(kinds)
        return [beat for beat in self.beats if beat.kind in wanted]

    def interest_at(self, when: float) -> float:
        """The measured interest nearest ``when``. 0.0 with no curve."""
        if not self.interest_curve:
            return 0.0
        best = min(
            self.interest_curve, key=lambda point: abs(as_float(point[0]) - when)
        )
        return clamp01(best[1] if len(best) > 1 else 0.0, 0.0)

    def position_of(self, when: float) -> float:
        return clamp01(when / self.duration, 0.0) if self.duration > 0 else 0.0

    def stats(self) -> dict:
        by_kind: dict[str, int] = {}
        for beat in self.beats:
            by_kind[beat.kind] = by_kind.get(beat.kind, 0) + 1
        return {
            "duration": round(self.duration, 2),
            "beats": len(self.beats),
            "by_beat_kind": by_kind,
            "labelled_beats": sum(
                1 for beat in self.beats if beat.kind != "unknown"),
            "objectives": len(self.objectives),
            "locations": len(self.locations),
            "roles": len(self.roles),
            "motifs": len(self.motifs),
            "setups": len(self.setups),
            "payoffs": len(self.payoffs),
            "callbacks": len(self.callbacks),
            "open_loops": len(self.open_loops),
            "resolved_loops": len(self.resolved_loops),
            "unresolved_loops": len(self.unresolved_loops),
            "edit_affecting": sum(
                1 for item in self._all_items() if item.affects_edit),
            "needs_human_review": sum(
                1 for item in self._all_items() if item.needs_human_review),
        }

    def _all_items(self) -> list[EpisodeItem]:
        return [
            *self.objectives, *self.locations, *self.roles, *self.motifs,
            *self.beats, *self.setups, *self.payoffs, *self.callbacks,
            *self.open_loops,
        ]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "name": self.name,
            "sequence_name": self.sequence_name,
            "timebase": self.timebase,
            "duration": round(self.duration, 3),
            "generated_at": self.generated_at,
            "basis": NOT_ANALYTICS,
            "sources": dict(self.sources),
            "stats": self.stats(),
            "main_objective": (
                self.main_objective.to_dict() if self.main_objective else None
            ),
            "secondary_objectives": [
                item.to_dict() for item in self.secondary_objectives],
            "locations": [item.to_dict() for item in self.locations],
            "roles": [item.to_dict() for item in self.roles],
            "motifs": [item.to_dict() for item in self.motifs],
            "beats": [item.to_dict() for item in self.beats],
            "setups": [item.to_dict() for item in self.setups],
            "payoffs": [item.to_dict() for item in self.payoffs],
            "callbacks": [item.to_dict() for item in self.callbacks],
            "open_loops": [item.to_dict() for item in self.open_loops],
            "interest_curve": [
                [round(as_float(p[0]), 3), round(clamp01(p[1], 0.0), 4)]
                for p in self.interest_curve if len(p) > 1
            ],
            "retention_spikes": [round(t, 3) for t in self.retention_spikes],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeMemory":
        data = data or {}
        main = data.get("main_objective")
        return cls(
            episode_id=str(data.get("episode_id") or ""),
            name=str(data.get("name") or "structure"),
            sequence_name=str(data.get("sequence_name") or ""),
            timebase=coerce_one(data.get("timebase"), TIMEBASES, "empty"),
            duration=as_float(data.get("duration")),
            main_objective=(
                EpisodeObjective.from_dict(main)
                if isinstance(main, dict) else None
            ),
            secondary_objectives=[
                EpisodeObjective.from_dict(item)
                for item in (data.get("secondary_objectives") or [])
            ],
            locations=[
                EpisodeLocation.from_dict(item)
                for item in (data.get("locations") or [])
            ],
            roles=[
                EpisodeCharacterRole.from_dict(item)
                for item in (data.get("roles") or [])
            ],
            motifs=[
                EpisodeMotif.from_dict(item)
                for item in (data.get("motifs") or [])
            ],
            beats=[
                EpisodeBeat.from_dict(item)
                for item in (data.get("beats") or [])
            ],
            setups=[
                EpisodeSetup.from_dict(item)
                for item in (data.get("setups") or [])
            ],
            payoffs=[
                EpisodePayoff.from_dict(item)
                for item in (data.get("payoffs") or [])
            ],
            callbacks=[
                EpisodeCallback.from_dict(item)
                for item in (data.get("callbacks") or [])
            ],
            open_loops=[
                EpisodeOpenLoop.from_dict(item)
                for item in (data.get("open_loops") or [])
            ],
            interest_curve=[
                [as_float(p[0]), clamp01(p[1], 0.0)]
                for p in (data.get("interest_curve") or [])
                if isinstance(p, (list, tuple)) and len(p) > 1
            ],
            retention_spikes=[
                as_float(t) for t in (data.get("retention_spikes") or [])
            ][:200],
            sources=dict(data.get("sources") or {}),
            generated_at=str(data.get("generated_at") or ""),
            warnings=as_str_list(data.get("warnings"), limit=200),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )


@dataclass
class EpisodeRetentionPlan:
    """What to do about what the memory found.

    Separate from the memory because it is an *opinion* about an observation.
    Re-planning must never be able to rewrite what was observed, and a person
    reading a risk should be able to go back to the beat it came from and
    disagree.
    """

    episode_id: str = ""
    name: str = "structure"
    sequence_name: str = ""
    timebase: str = "empty"
    duration: float = 0.0

    risks: list[EpisodeRiskZone] = field(default_factory=list)
    hooks: list[HookCandidate] = field(default_factory=list)
    climax: Optional[ClimaxCandidate] = None
    #: Runners-up, so a weak margin is inspectable rather than just reported.
    climax_alternatives: list[ClimaxCandidate] = field(default_factory=list)
    ending: Optional[EndingCandidate] = None
    ending_alternatives: list[EndingCandidate] = field(default_factory=list)
    #: A moment in the middle that could restate the goal and re-engage.
    midpoint_reset: Optional[RetentionSuggestion] = None
    suggestions: list[RetentionSuggestion] = field(default_factory=list)

    generated_at: str = ""
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def auto_safe_suggestions(self) -> list[RetentionSuggestion]:
        return [item for item in self.suggestions if item.auto_safe]

    @property
    def marker_only_suggestions(self) -> list[RetentionSuggestion]:
        return [item for item in self.suggestions if not item.auto_safe]

    def risks_of(self, *types: str) -> list[EpisodeRiskZone]:
        wanted = set(types)
        return [risk for risk in self.risks if risk.risk in wanted]

    def suggestions_for(self, stage: str) -> list[RetentionSuggestion]:
        """Every suggestion a given downstream pass could act on.

        This is the seam Sessions 3, 5 and 6 will read. It is a filter over a
        list of records with no operations in them, on purpose: a later pass
        decides what an operation looks like, this one only says what it wants.
        """
        return [item for item in self.suggestions if item.downstream == stage]

    def top_hooks(self, limit: int = 5) -> list[HookCandidate]:
        return sorted(
            self.hooks, key=lambda h: h.score, reverse=True)[:max(0, limit)]

    def stats(self) -> dict:
        by_risk: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for risk in self.risks:
            by_risk[risk.risk] = by_risk.get(risk.risk, 0) + 1
            by_severity[risk.severity] = by_severity.get(risk.severity, 0) + 1
        by_type: dict[str, int] = {}
        by_stage: dict[str, int] = {}
        for item in self.suggestions:
            by_type[item.type] = by_type.get(item.type, 0) + 1
            by_stage[item.downstream] = by_stage.get(item.downstream, 0) + 1
        return {
            "risks": len(self.risks),
            "by_risk": by_risk,
            "by_severity": by_severity,
            "high_severity": sum(
                1 for r in self.risks if r.severity == "high"),
            "hooks": len(self.hooks),
            "hooks_with_payoff": sum(1 for h in self.hooks if h.has_payoff),
            "has_climax": self.climax is not None,
            "has_ending": self.ending is not None,
            "suggestions": len(self.suggestions),
            "by_type": by_type,
            "by_downstream": by_stage,
            "auto_safe": len(self.auto_safe_suggestions),
            "marker_only": len(self.marker_only_suggestions),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "name": self.name,
            "sequence_name": self.sequence_name,
            "timebase": self.timebase,
            "duration": round(self.duration, 3),
            "generated_at": self.generated_at,
            "basis": NOT_ANALYTICS,
            "stats": self.stats(),
            "risks": [item.to_dict() for item in self.risks],
            "hooks": [item.to_dict() for item in self.hooks],
            "climax": self.climax.to_dict() if self.climax else None,
            "climax_alternatives": [
                item.to_dict() for item in self.climax_alternatives],
            "ending": self.ending.to_dict() if self.ending else None,
            "ending_alternatives": [
                item.to_dict() for item in self.ending_alternatives],
            "midpoint_reset": (
                self.midpoint_reset.to_dict() if self.midpoint_reset else None
            ),
            "suggestions": [item.to_dict() for item in self.suggestions],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EpisodeRetentionPlan":
        data = data or {}
        climax = data.get("climax")
        ending = data.get("ending")
        midpoint = data.get("midpoint_reset")
        return cls(
            episode_id=str(data.get("episode_id") or ""),
            name=str(data.get("name") or "structure"),
            sequence_name=str(data.get("sequence_name") or ""),
            timebase=coerce_one(data.get("timebase"), TIMEBASES, "empty"),
            duration=as_float(data.get("duration")),
            risks=[
                EpisodeRiskZone.from_dict(item)
                for item in (data.get("risks") or [])
            ],
            hooks=[
                HookCandidate.from_dict(item)
                for item in (data.get("hooks") or [])
            ],
            climax=(
                ClimaxCandidate.from_dict(climax)
                if isinstance(climax, dict) else None
            ),
            climax_alternatives=[
                ClimaxCandidate.from_dict(item)
                for item in (data.get("climax_alternatives") or [])
            ],
            ending=(
                EndingCandidate.from_dict(ending)
                if isinstance(ending, dict) else None
            ),
            ending_alternatives=[
                EndingCandidate.from_dict(item)
                for item in (data.get("ending_alternatives") or [])
            ],
            midpoint_reset=(
                RetentionSuggestion.from_dict(midpoint)
                if isinstance(midpoint, dict) else None
            ),
            suggestions=[
                RetentionSuggestion.from_dict(item)
                for item in (data.get("suggestions") or [])
            ],
            generated_at=str(data.get("generated_at") or ""),
            warnings=as_str_list(data.get("warnings"), limit=200),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )


def new_id(prefix: str, *parts: Any) -> str:
    """A stable ID for an episode record: ``<prefix>_<hash of its inputs>``."""
    return f"{prefix}_{short_hash(*parts, length=10)}"

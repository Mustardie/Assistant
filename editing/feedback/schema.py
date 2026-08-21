"""Record types for the feedback collector.

Every pass before this one produced an opinion about the footage. This one
records *the editor's* opinion about those opinions, in a shape a later session
can turn into a dataset.

The whole design is one sentence: **feedback is only worth keeping if you can
find the thing it was about again.** So a ``FeedbackItem`` is not a star rating
with a comment; it is a rating, a reason, an optional correction, and a
``FeedbackTarget`` that names the exact record -- a placement, a caption, an
asset, a hook -- or, failing that, a timeline range. A note with nothing
attached is still recorded, and is marked unresolved so nobody later mistakes
it for evidence.

## What this layer does not do

It does not train anything, tune anything, or change any pass's behaviour.
``PreferenceSignal`` and ``TrainingSignal`` are *summaries of what was said*,
built so Session 10 has clean material; nothing reads them yet, and
``safe_to_apply_automatically`` is a claim about the shape of the evidence, not
a licence this layer grants itself.

## Three structural rules

* **Append-only.** A rating is never edited in place. Changing your mind
  appends a new item carrying ``supersedes``, and the log keeps both. See
  ``editing.feedback.store``.
* **Uncertainty is a first-class answer.** ``unsure`` is a rating, not a
  missing one, and it is deliberately excluded from training by default --
  ``TRAINING_EXCLUDED_RATINGS`` -- because "I don't know" is a real signal
  about the *item* and a terrible label.
* **A human judgement is never restated as a measurement.** ``strong_retention``
  and ``weak_retention`` record what a person thought; nothing here has seen a
  retention graph, and ``NOT_MEASURED`` says so on every report this layer
  writes. That is Session 8's ``NOT_ANALYTICS`` rule from the other direction:
  there it constrained the system's claim, here it labels the human's.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from editing.schema import (
    _slug, as_bool, as_float, as_str_list, clamp01, short_hash,
)

#: Printed on every report and every export this layer produces. A constant
#: rather than prose in each renderer, for the same reason Session 8's banner
#: is: it cannot soften into a claim if there is only one copy of it.
NOT_MEASURED = (
    "This is collected human opinion, not measurement. Ratings like "
    "'strong retention' record what a person thought while reviewing an edit; "
    "nothing here has seen a retention graph, a view count or an audience. "
    "Nothing in this layer trains anything -- it stores what was said so a "
    "later session can decide what, if anything, is learnable from it."
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


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


def new_id(prefix: str, *parts: Any) -> str:
    """A stable ID for a feedback record: ``<prefix>_<hash of its inputs>``."""
    return f"{prefix}_{short_hash(*parts, length=10)}"


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

#: What a piece of feedback can be about. Every one except the last two names a
#: record produced by an earlier session, so feedback about it can be joined
#: back to the artifact that proposed it.
TARGET_TYPES = (
    "roughcut_placement",       # Session 3: a clip in the cut
    "recommendation",           # Session 2: a proposed edit
    "critic_finding",           # Session 4: something the critic saw
    "revision_recommendation",  # Session 4: what it proposed doing about it
    "layer_item",               # Session 5: a caption, card, zoom, marker
    "asset_placement",          # Session 6: an SFX, bed, ambience or graphic
    "episode_beat",             # Session 8: a stretch doing one story job
    "retention_suggestion",     # Session 8: what a later pass could do
    "hook_candidate",           # Session 8: a candidate opening
    "open_loop",                # Session 8: a question the episode raises
    "callback",                 # Session 8: a reference back to earlier
    "timeline_range",           # no record: just a span of the edit
    "whole_edit",               # the thing as a whole
)

#: Which artifact each target type lives in, relative to the artifact root.
#: Used to fill ``FeedbackTarget.artifact`` and to give an actionable error
#: when an ID cannot be found -- "look in this file" beats "unknown ID".
ARTIFACT_FOR_TARGET = {
    "roughcut_placement": "roughcut/{name}.json",
    "recommendation": "recommendations/{name}.json",
    "critic_finding": "critic/{name}.critique.json",
    "revision_recommendation": "critic/{name}.revisions.json",
    "layer_item": "layers/{name}.json",
    "asset_placement": "assets/{name}.plan.json",
    "episode_beat": "episode/{name}.memory.json",
    "retention_suggestion": "episode/{name}.retention.json",
    "hook_candidate": "episode/{name}.retention.json",
    "open_loop": "episode/{name}.memory.json",
    "callback": "episode/{name}.memory.json",
    "timeline_range": "timelines/{name}.json",
    "whole_edit": "",
}

#: Target types that name a record with an ID. ``timeline_range`` and
#: ``whole_edit`` do not, and must never be reported as unresolved.
IDENTIFIED_TARGETS = frozenset(TARGET_TYPES) - {"timeline_range", "whole_edit"}


def artifact_for(target_type: str, name: str = "structure") -> str:
    template = ARTIFACT_FOR_TARGET.get(target_type, "")
    return template.format(name=name) if template else ""


@dataclass
class FeedbackTarget:
    """What one piece of feedback is about.

    ``resolved`` is the field a consumer branches on. False means the ID was
    not found in the artifacts that were loaded -- which is different from "we
    never looked" (``checked`` False), and both are different from a target
    that never had an ID at all. Session 10 will want to drop the first and
    keep the third, so the three cases stay distinguishable.
    """

    target_type: str = "whole_edit"
    target_id: str = ""
    start: float = 0.0
    end: float = 0.0
    #: What the target is, in words, so feedback reads without the artifact.
    label: str = ""
    #: Which artifact holds the record, relative to the artifact root.
    artifact: str = ""
    #: Records the target was itself built from: segment IDs, recommendation
    #: IDs, placement IDs. The trail back to the footage.
    source_ids: list[str] = field(default_factory=list)
    checked: bool = False
    resolved: bool = False
    resolution_note: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def has_range(self) -> bool:
        return self.end > self.start

    @property
    def is_identified(self) -> bool:
        return self.target_type in IDENTIFIED_TARGETS

    @property
    def is_traceable(self) -> bool:
        """Whether this feedback can be joined to anything at all.

        A resolved record, a real time range, or source IDs will all do. Only a
        target with none of the three is untraceable, and that is the one case
        Session 10 has to throw away.
        """
        return bool(
            (self.is_identified and self.resolved)
            or self.has_range
            or self.source_ids
            or self.target_type == "whole_edit"
        )

    def key(self) -> str:
        """What makes two targets the same thing, for deduplication."""
        if self.target_id:
            return f"{self.target_type}:{self.target_id}"
        return f"{self.target_type}:{self.start:.2f}-{self.end:.2f}"

    def describe(self) -> str:
        where = f"[{self.start:.2f}-{self.end:.2f}]" if self.has_range else "[--]"
        what = self.label or self.target_id or "(no detail)"
        return f"{where} {self.target_type}: {what}"

    def to_dict(self) -> dict:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "label": self.label,
            "artifact": self.artifact,
            "source_ids": list(self.source_ids),
            "checked": self.checked,
            "resolved": self.resolved,
            "resolution_note": self.resolution_note,
            "is_traceable": self.is_traceable,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "FeedbackTarget":
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        return cls(
            target_type=coerce_one(
                data.get("target_type"), TARGET_TYPES, "whole_edit"),
            target_id=_text(data.get("target_id"), 120),
            start=start,
            end=end,
            label=_text(data.get("label"), 300),
            artifact=_text(data.get("artifact"), 300),
            source_ids=as_str_list(data.get("source_ids"), limit=100),
            checked=as_bool(data.get("checked")),
            resolved=as_bool(data.get("resolved")),
            resolution_note=_text(data.get("resolution_note"), 300),
        )


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

#: Every rating this layer accepts, grouped by what it is a judgement *of*.
#: Closed on purpose: a preference extractor that pattern-matches free text
#: breaks the day someone types "meh".
RATING_GROUPS = {
    "verdict": ("good", "bad", "okay", "unsure"),
    "action": ("keep", "cut", "shorten", "extend", "move_earlier",
               "move_later"),
    "amount": ("too_much", "too_little"),
    "placement": ("wrong_moment", "wrong_style"),
    "tone": ("funny", "boring", "confusing", "hype"),
    "context": ("useful_context", "useless_context"),
    "caption": ("bad_caption", "good_caption"),
    "audio": ("bad_music_sfx", "good_music_sfx"),
    "hook": ("bad_hook", "good_hook"),
    "pacing": ("bad_pacing",),
    "payoff": ("strong_payoff", "weak_payoff"),
    "callback": ("good_callback", "forced_callback"),
    "retention": ("strong_retention", "weak_retention"),
}

RATINGS = tuple(
    rating for group in RATING_GROUPS.values() for rating in group
)

GROUP_FOR_RATING = {
    rating: group
    for group, ratings in RATING_GROUPS.items() for rating in ratings
}

#: What a rating says about the decision it is attached to. ``corrective``
#: means "the idea was right and the execution was not", which is a different
#: training label from "this was wrong" -- a caption someone wants shortened is
#: still a caption someone wanted.
POLARITIES = ("positive", "negative", "corrective", "neutral", "uncertain")

POLARITY_FOR_RATING = {
    "good": "positive", "bad": "negative", "okay": "neutral",
    "unsure": "uncertain",

    "keep": "positive", "cut": "negative",
    "shorten": "corrective", "extend": "corrective",
    "move_earlier": "corrective", "move_later": "corrective",

    "too_much": "corrective", "too_little": "corrective",

    "wrong_moment": "negative", "wrong_style": "negative",

    "funny": "positive", "boring": "negative", "confusing": "negative",
    "hype": "positive",

    "useful_context": "positive", "useless_context": "negative",

    "bad_caption": "negative", "good_caption": "positive",
    "bad_music_sfx": "negative", "good_music_sfx": "positive",
    "bad_hook": "negative", "good_hook": "positive",
    "bad_pacing": "negative",
    "strong_payoff": "positive", "weak_payoff": "negative",
    "good_callback": "positive", "forced_callback": "negative",
    "strong_retention": "positive", "weak_retention": "negative",
}

#: Ratings that are a statement about a person's uncertainty rather than about
#: the edit. Kept, and never trained on by default.
TRAINING_EXCLUDED_RATINGS = frozenset({"unsure"})

#: Ratings whose meaning is incomplete without a correction. "shorten" with no
#: idea of by how much is still useful as a direction, so these are not
#: rejected -- an item carrying one and no correction is flagged for follow-up.
WANTS_CORRECTION = frozenset({
    "shorten", "extend", "move_earlier", "move_later", "wrong_moment",
    "wrong_style", "too_much", "too_little",
})

#: The reason vocabulary. Closed so it can be grouped, filtered and counted.
REASON_CATEGORIES = (
    "pacing", "story", "clarity", "retention", "comedy", "emotion", "visual",
    "audio", "caption", "timing", "style", "safety", "technical", "preference",
)

#: What a rating is about when no reason was given. Every rating maps to
#: exactly one category, so an item is never uncategorised -- which is what
#: makes ``--category`` on the queue and the report trustworthy rather than
#: silently lossy.
DEFAULT_REASON_FOR_RATING = {
    "good": "preference", "bad": "preference", "okay": "preference",
    "unsure": "preference",
    "keep": "preference", "cut": "pacing",
    "shorten": "timing", "extend": "timing",
    "move_earlier": "timing", "move_later": "timing",
    "too_much": "style", "too_little": "style",
    "wrong_moment": "timing", "wrong_style": "style",
    "funny": "comedy", "boring": "pacing", "confusing": "clarity",
    "hype": "emotion",
    "useful_context": "story", "useless_context": "story",
    "bad_caption": "caption", "good_caption": "caption",
    "bad_music_sfx": "audio", "good_music_sfx": "audio",
    "bad_hook": "retention", "good_hook": "retention",
    "bad_pacing": "pacing",
    "strong_payoff": "story", "weak_payoff": "story",
    "good_callback": "story", "forced_callback": "story",
    "strong_retention": "retention", "weak_retention": "retention",
}


def polarity_of(rating: str) -> str:
    return POLARITY_FOR_RATING.get(_slug(rating), "neutral")


def default_reason_for(rating: str) -> str:
    return DEFAULT_REASON_FOR_RATING.get(_slug(rating), "preference")


@dataclass
class FeedbackRating:
    """One rating, with its polarity worked out once.

    ``strength`` is how emphatic the rating is, not how sure the person is --
    that is ``FeedbackItem.confidence``. The two are genuinely different:
    "this is definitely a bit boring" is high confidence and low strength, and
    a preference extractor that conflated them would weight a firm mild opinion
    like a hesitant strong one.
    """

    rating: str = "okay"
    strength: float = 0.5

    @property
    def polarity(self) -> str:
        return polarity_of(self.rating)

    @property
    def group(self) -> str:
        return GROUP_FOR_RATING.get(self.rating, "verdict")

    @property
    def is_uncertain(self) -> bool:
        return self.polarity == "uncertain"

    @property
    def wants_correction(self) -> bool:
        return self.rating in WANTS_CORRECTION

    def to_dict(self) -> dict:
        return {
            "rating": self.rating,
            "polarity": self.polarity,
            "group": self.group,
            "strength": round(self.strength, 3),
            "is_uncertain": self.is_uncertain,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "FeedbackRating":
        if isinstance(data, str):
            return cls(rating=coerce_one(data, RATINGS, "okay"))
        data = data or {}
        return cls(
            rating=coerce_one(data.get("rating"), RATINGS, "okay"),
            strength=clamp01(data.get("strength"), 0.5),
        )


def reason_from(value: Any) -> "FeedbackReason":
    """One ``--reason`` word, coerced without losing what was typed.

    Three cases, in order:

    * an actual category (``pacing``) -- used as given;
    * a *rating* word (``boring``) -- mapped to the category that rating
      belongs to, with the word kept in ``detail``. People reach for the
      rating vocabulary here constantly, and quietly dropping "boring"
      because it is not in the reason list loses the most specific thing they
      said;
    * anything else -- filed under ``preference`` with the text kept, because
      an uncategorised reason is still a reason.
    """
    text = str(value or "").strip()
    token = _slug(text)
    if token in REASON_CATEGORIES:
        return FeedbackReason(category=token)
    if token in POLARITY_FOR_RATING:
        return FeedbackReason(
            category=default_reason_for(token),
            detail=f"given as '{text}'",
        )
    if not text:
        return FeedbackReason(category="preference")
    return FeedbackReason(category="preference", detail=text[:400])


def reasons_from(values: Any) -> list["FeedbackReason"]:
    """A list of ``--reason`` words, deduplicated by category.

    Details are merged rather than dropped when two words land in the same
    category, so ``--reason boring --reason pacing`` keeps the word "boring".
    """
    out: list[FeedbackReason] = []
    for item in as_str_list(values, limit=20):
        reason = reason_from(item)
        existing = next(
            (r for r in out if r.category == reason.category), None)
        if existing is None:
            out.append(reason)
        elif reason.detail and reason.detail not in existing.detail:
            existing.detail = (
                f"{existing.detail}; {reason.detail}" if existing.detail
                else reason.detail
            )[:400]
    return out


@dataclass
class FeedbackReason:
    """Why the rating is what it is.

    ``category`` is closed so it can be grouped and filtered; ``detail`` is
    free text so nothing is lost when the category is a poor fit.
    """

    category: str = "preference"
    detail: str = ""

    def to_dict(self) -> dict:
        return {"category": self.category, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: Any) -> "FeedbackReason":
        if isinstance(data, str):
            return cls(
                category=coerce_one(data, REASON_CATEGORIES, "preference"))
        data = data or {}
        return cls(
            category=coerce_one(
                data.get("category"), REASON_CATEGORIES, "preference"),
            detail=_text(data.get("detail"), 400),
        )


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

#: What a correction asks for. Closed, and deliberately *not* the Premiere
#: operation vocabulary: a correction is a statement of intent for a later
#: session to interpret, not an edit this one knows how to perform.
CORRECTION_ACTIONS = (
    "cut", "shorten", "extend", "move_earlier", "move_later", "replace",
    "retime", "restyle", "change_text", "remove", "add", "reorder",
    "none", "other",
)

#: Corrections that name a change with a size. Only these can carry a number,
#: and only these can produce a training signal with a usable "after" side.
MEASURABLE_CORRECTIONS = frozenset({
    "shorten", "extend", "move_earlier", "move_later", "retime",
})

#: Free-text openings that map onto an action, matched longest-first so "cut
#: this shorter" scores ``shorten`` rather than ``cut``. Same ordering rule as
#: Session 8's cue families, for the same reason: a short phrase inside a
#: longer one must not be able to claim it.
_ACTION_PHRASES = (
    ("cut this shorter", "shorten"),
    ("make this shorter", "shorten"),
    ("move it earlier", "move_earlier"),
    ("move it later", "move_later"),
    ("move earlier", "move_earlier"),
    ("move later", "move_later"),
    ("start earlier", "move_earlier"),
    ("start later", "move_later"),
    ("change the text", "change_text"),
    ("different text", "change_text"),
    ("different music", "replace"),
    ("different sound", "replace"),
    ("speed this up", "retime"),
    ("slow this down", "retime"),
    ("hold longer", "extend"),
    ("swap this", "replace"),
    ("shorter", "shorten"),
    ("trim", "shorten"),
    ("longer", "extend"),
    ("extend", "extend"),
    ("replace", "replace"),
    ("reorder", "reorder"),
    ("restyle", "restyle"),
    ("remove", "remove"),
    ("delete", "remove"),
    ("cut this", "cut"),
    ("cut", "cut"),
    ("add", "add"),
)


def action_from_text(text: Any) -> str:
    """Guess a correction action from what the editor typed.

    A guess, and flagged as one by ``FeedbackCorrection.inferred``: the action
    is a convenience for grouping, and the text is always kept verbatim beside
    it so a wrong guess loses nothing.
    """
    lowered = str(text or "").lower()
    if not lowered.strip():
        return "none"
    for phrase, action in _ACTION_PHRASES:
        if phrase in lowered:
            return action
    return "other"


@dataclass
class FeedbackCorrection:
    """What the editor would have done instead.

    ``text`` is the record; ``action`` and ``seconds`` are a parse of it. When
    they disagree the text wins, which is why both are stored rather than the
    text being discarded once it has been parsed.
    """

    action: str = "other"
    text: str = ""
    #: How much, for a measurable correction. Negative shortens or moves
    #: earlier, positive extends or moves later. ``None`` means the editor gave
    #: a direction and no number, which is common and still useful.
    seconds: Optional[float] = None
    #: Where the correction applies, when it is not the target's own range.
    start: float = 0.0
    end: float = 0.0
    #: True when the action was inferred from the text rather than given.
    inferred: bool = False

    @property
    def has_range(self) -> bool:
        return self.end > self.start

    @property
    def is_measurable(self) -> bool:
        return self.action in MEASURABLE_CORRECTIONS

    @property
    def is_specific(self) -> bool:
        """Whether a later session could turn this into a concrete change.

        A measurable action needs a number or a range to be specific; a
        non-measurable one ("replace this music") is specific enough as text.
        """
        if not self.text or self.action == "none":
            return False
        if self.is_measurable:
            return self.seconds is not None or self.has_range
        return True

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "text": self.text,
            "seconds": (round(self.seconds, 3)
                        if self.seconds is not None else None),
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "inferred": self.inferred,
            "is_measurable": self.is_measurable,
            "is_specific": self.is_specific,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["FeedbackCorrection"]:
        if not data:
            return None
        if isinstance(data, str):
            return cls(action=action_from_text(data), text=_text(data, 600),
                       inferred=True)
        seconds = data.get("seconds")
        start = max(0.0, as_float(data.get("start")))
        return cls(
            action=coerce_one(data.get("action"), CORRECTION_ACTIONS, "other"),
            text=_text(data.get("text"), 600),
            seconds=(as_float(seconds) if seconds is not None else None),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            inferred=as_bool(data.get("inferred")),
        )


def build_correction(
    text: Any, *, action: str = "", seconds: Optional[float] = None,
    start: float = 0.0, end: float = 0.0,
) -> Optional[FeedbackCorrection]:
    """A correction from what the CLI was given, with the action inferred."""
    body = _text(text, 600)
    if not body and not action:
        return None
    chosen = coerce_one(action, CORRECTION_ACTIONS, "") if action else ""
    return FeedbackCorrection(
        action=chosen or action_from_text(body),
        text=body,
        seconds=(as_float(seconds) if seconds is not None else None),
        start=max(0.0, as_float(start)),
        end=max(max(0.0, as_float(start)), as_float(end)),
        inferred=not chosen,
    )


# ---------------------------------------------------------------------------
# The feedback item
# ---------------------------------------------------------------------------

#: Priority below which an item is not worth surfacing in a short report.
LOW_PRIORITY = 0.35

#: Confidence at or below which an item is flagged for follow-up regardless of
#: what it says. The same threshold Session 8 uses, and for the same reason: a
#: judgement the person themselves was unsure of is not evidence.
UNSURE_AT_OR_BELOW = 0.40


@dataclass
class FeedbackItem:
    """One rating of one thing, with everything needed to use it later.

    Immutable by convention. ``store.append`` writes it once; a change of mind
    appends a *new* item whose ``supersedes`` names this one, so the history of
    an opinion survives alongside its current state.
    """

    feedback_id: str = ""
    created_at: str = ""
    session_id: str = ""
    #: The auto run this feedback is about, when there was one.
    run_id: str = ""
    #: The queue prompt this answers, when it came from the queue.
    prompt_id: str = ""

    target: FeedbackTarget = field(default_factory=FeedbackTarget)
    rating: FeedbackRating = field(default_factory=FeedbackRating)
    reasons: list[FeedbackReason] = field(default_factory=list)
    note: str = ""
    correction: Optional[FeedbackCorrection] = None

    #: How much this matters to the editor, 0..1.
    priority: float = 0.5
    #: How sure the editor is of this judgement, 0..1.
    confidence: float = 0.7

    #: Whether this should become training data. Defaults are computed by
    #: ``settle``; a person can always override either way.
    usable_for_training: bool = True
    training_note: str = ""
    needs_follow_up: bool = False
    follow_up_note: str = ""

    #: Artifacts this feedback should be read alongside, relative to the
    #: artifact root. Filled from the target and from whatever the queue knew.
    source_artifacts: list[str] = field(default_factory=list)
    #: One line a person can read in a list without opening anything.
    summary: str = ""
    #: The feedback ID this one replaces, if any.
    supersedes: str = ""
    schema_version: int = 1

    # -- derived ---------------------------------------------------------

    @property
    def polarity(self) -> str:
        return self.rating.polarity

    @property
    def categories(self) -> list[str]:
        """Reason categories, with the rating's default when none were given."""
        given = [reason.category for reason in self.reasons]
        return given or [default_reason_for(self.rating.rating)]

    @property
    def category(self) -> str:
        return self.categories[0]

    @property
    def is_uncertain(self) -> bool:
        return self.rating.is_uncertain or self.confidence <= UNSURE_AT_OR_BELOW

    @property
    def has_correction(self) -> bool:
        return self.correction is not None and self.correction.action != "none"

    def settle(self) -> "FeedbackItem":
        """Fill the derived fields and apply the two exclusion rules.

        Called by every builder before an item is written. Keeping it in one
        place is what makes "an unsure rating is never training data" a
        property of the layer rather than a habit of whoever wrote the CLI.
        """
        if not self.created_at:
            self.created_at = now()
        if not self.feedback_id:
            # ``supersedes`` is part of the hash so two links of one chain
            # written inside the same second -- a rating and an immediate
            # correction, say -- cannot collide on an ID.
            self.feedback_id = new_id(
                "fb", self.session_id, self.target.key(),
                self.rating.rating, self.created_at, self.note,
                self.supersedes,
            )

        self.priority = clamp01(self.priority, 0.5)
        self.confidence = clamp01(self.confidence, 0.7)

        artifact = self.target.artifact
        if artifact and artifact not in self.source_artifacts:
            self.source_artifacts.append(artifact)

        reasons = []
        for reason in self.reasons:
            if reason.category not in {r.category for r in reasons}:
                reasons.append(reason)
        self.reasons = reasons

        # -- why this is or is not training material ---------------------
        why_not = []
        if self.rating.rating in TRAINING_EXCLUDED_RATINGS:
            why_not.append(
                "the rating is 'unsure', which says something about the item "
                "and nothing usable about what the right answer was")
        if not self.target.is_traceable:
            why_not.append(
                "the target could not be joined to any record, range or "
                "source ID, so there is nothing to attach a label to")
        if self.confidence <= UNSURE_AT_OR_BELOW:
            why_not.append(
                f"the editor's own confidence is {self.confidence:.2f}, at or "
                f"below the {UNSURE_AT_OR_BELOW:.2f} floor")
        if why_not:
            self.usable_for_training = False
            self.training_note = "; ".join(why_not)
        elif not self.training_note:
            self.training_note = (
                f"{self.rating.polarity} rating on a traceable "
                f"{self.target.target_type}")

        # -- what still needs a person -----------------------------------
        if self.rating.wants_correction and not self.has_correction:
            self.needs_follow_up = True
            if not self.follow_up_note:
                self.follow_up_note = (
                    f"'{self.rating.rating}' says which direction to move but "
                    "not how far; add a correction with `feedback correct`")
        if self.target.checked and self.target.is_identified \
                and not self.target.resolved:
            self.needs_follow_up = True
            if not self.follow_up_note:
                self.follow_up_note = (
                    f"target '{self.target.target_id}' was not found in "
                    f"{self.target.artifact or 'the artifacts'}; the rating is "
                    "kept but cannot be joined to a record")

        if not self.summary:
            self.summary = self.render_summary()
        return self

    def render_summary(self) -> str:
        bits = [f"{self.rating.rating}"]
        if self.reasons:
            bits.append("/".join(r.category for r in self.reasons))
        bits.append(self.target.describe())
        if self.note:
            bits.append(f'"{self.note[:60]}"')
        if self.has_correction and self.correction is not None:
            bits.append(f"-> {self.correction.text[:50]}")
        return "  ".join(bits)

    def line(self) -> str:
        mark = {"positive": "+", "negative": "-", "corrective": "~",
                "uncertain": "?", "neutral": "="}.get(self.polarity, "=")
        train = "T" if self.usable_for_training else " "
        follow = "!" if self.needs_follow_up else " "
        return (
            f"{mark}{train}{follow} {self.feedback_id}  "
            f"{self.rating.rating:<16} {self.category:<11} "
            f"{self.target.describe()[:56]}"
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "feedback_id": self.feedback_id,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "prompt_id": self.prompt_id,
            "target": self.target.to_dict(),
            "rating": self.rating.to_dict(),
            "reasons": [reason.to_dict() for reason in self.reasons],
            "categories": self.categories,
            "note": self.note,
            "correction": (
                self.correction.to_dict() if self.correction else None),
            "priority": round(self.priority, 3),
            "confidence": round(self.confidence, 3),
            "usable_for_training": self.usable_for_training,
            "training_note": self.training_note,
            "needs_follow_up": self.needs_follow_up,
            "follow_up_note": self.follow_up_note,
            "source_artifacts": list(self.source_artifacts),
            "summary": self.summary,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FeedbackItem":
        data = data or {}
        return cls(
            feedback_id=_text(data.get("feedback_id"), 80),
            created_at=_text(data.get("created_at"), 40),
            session_id=_text(data.get("session_id"), 120),
            run_id=_text(data.get("run_id"), 120),
            prompt_id=_text(data.get("prompt_id"), 80),
            target=FeedbackTarget.from_dict(data.get("target")),
            rating=FeedbackRating.from_dict(data.get("rating")),
            reasons=[
                FeedbackReason.from_dict(item)
                for item in (data.get("reasons") or [])
            ],
            note=_text(data.get("note"), 2000),
            correction=FeedbackCorrection.from_dict(data.get("correction")),
            priority=clamp01(data.get("priority"), 0.5),
            confidence=clamp01(data.get("confidence"), 0.7),
            usable_for_training=as_bool(
                data.get("usable_for_training"), True),
            training_note=_text(data.get("training_note"), 600),
            needs_follow_up=as_bool(data.get("needs_follow_up")),
            follow_up_note=_text(data.get("follow_up_note"), 600),
            source_artifacts=as_str_list(
                data.get("source_artifacts"), limit=40),
            summary=_text(data.get("summary"), 400),
            supersedes=_text(data.get("supersedes"), 80),
        )


# ---------------------------------------------------------------------------
# The review queue
# ---------------------------------------------------------------------------

#: Which pass a queue prompt came from. Used for ``--source`` filtering and to
#: guarantee that a limited queue still covers more than one pass.
PROMPT_SOURCES = (
    "roughcut", "recommend", "critic", "style", "assets", "episode",
    "retention", "edit",
)

#: How much a decision changes the finished video. ``high`` means a viewer
#: would notice if it were wrong.
IMPACTS = ("high", "medium", "low")

_IMPACT_ORDER = {"low": 0, "medium": 1, "high": 2}

#: Why a prompt is in the queue. More than one can apply, and an item with
#: several is exactly what the queue should be surfacing first.
PROMPT_FLAGS = (
    "uncertain",        # the system was not sure
    "high_impact",      # a viewer would notice
    "risky_automatic",  # it was decided automatically and could be wrong
    "structural",       # a hook, the peak, the ending
    "retention_risk",   # a Session 8 risk zone
    "setup_payoff",     # one half of a setup/payoff pair
    "refused",          # a pass declined to do something
    "positive_sample",  # deliberately included as an example of a good call
)


@dataclass
class ReviewPrompt:
    """One thing the editor is being asked to look at.

    It carries the *question*, what the system decided, and the evidence, so a
    review can happen against the prompt alone. That matters more than it
    looks: feedback given without seeing what the system thought is feedback
    about the video, and feedback given with it is feedback about the decision.
    Only the second kind can train anything.
    """

    prompt_id: str = ""
    source: str = "edit"
    target: FeedbackTarget = field(default_factory=FeedbackTarget)

    question: str = ""
    why_asked: str = ""
    system_decision: str = ""
    system_confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    category: str = "preference"
    suggested_ratings: list[str] = field(default_factory=list)
    priority: float = 0.5
    impact: str = "medium"
    flags: list[str] = field(default_factory=list)

    #: Prompts near each other in the timeline share a group so a reviewer
    #: reads one moment at a time instead of jumping around.
    group_id: str = ""
    #: How many near-identical candidates collapsed into this one.
    duplicates: int = 0
    #: Whether feedback has already been given on this prompt. Filled at read
    #: time from the log; never stored in ``queue.json``.
    answered: bool = False

    @property
    def start(self) -> float:
        return self.target.start

    @property
    def end(self) -> float:
        return self.target.end

    @property
    def is_uncertain(self) -> bool:
        return "uncertain" in self.flags

    @property
    def is_positive_sample(self) -> bool:
        return "positive_sample" in self.flags

    def has_flag(self, flag: str) -> bool:
        return flag in self.flags

    def rank(self) -> tuple:
        """Sort key: priority, then impact, then earliest in the episode."""
        return (
            -round(self.priority, 4),
            -_IMPACT_ORDER.get(self.impact, 1),
            round(self.start, 3),
            self.prompt_id,
        )

    def line(self) -> str:
        marks = "".join(
            m for flag, m in (
                ("uncertain", "?"), ("high_impact", "*"),
                ("risky_automatic", "!"), ("structural", "^"),
                ("retention_risk", "r"), ("setup_payoff", "p"),
                ("refused", "x"), ("positive_sample", "+"),
            ) if flag in self.flags
        ) or "-"
        return (
            f"{'[done] ' if self.answered else ''}{self.prompt_id}  "
            f"[{self.start:7.2f}-{self.end:7.2f}] {self.source:<9} "
            f"{marks:<6} p={self.priority:.2f}  {self.question[:52]}"
        )

    def to_dict(self) -> dict:
        return {
            "prompt_id": self.prompt_id,
            "source": self.source,
            "target": self.target.to_dict(),
            "question": self.question,
            "why_asked": self.why_asked,
            "system_decision": self.system_decision,
            "system_confidence": round(self.system_confidence, 3),
            "evidence": list(self.evidence),
            "category": self.category,
            "suggested_ratings": list(self.suggested_ratings),
            "priority": round(self.priority, 3),
            "impact": self.impact,
            "flags": list(self.flags),
            "group_id": self.group_id,
            "duplicates": self.duplicates,
            "rate_command": (
                f"python -m editing.cli feedback rate {self.prompt_id} "
                f"<{'|'.join(self.suggested_ratings[:3]) or 'good|bad'}>"
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewPrompt":
        data = data or {}
        return cls(
            prompt_id=_text(data.get("prompt_id"), 80),
            source=coerce_one(data.get("source"), PROMPT_SOURCES, "edit"),
            target=FeedbackTarget.from_dict(data.get("target")),
            question=_text(data.get("question"), 400),
            why_asked=_text(data.get("why_asked"), 500),
            system_decision=_text(data.get("system_decision"), 400),
            system_confidence=clamp01(data.get("system_confidence"), 0.0),
            evidence=[str(line)[:300] for line in (data.get("evidence") or [])],
            category=coerce_one(
                data.get("category"), REASON_CATEGORIES, "preference"),
            suggested_ratings=coerce_many(
                data.get("suggested_ratings"), RATINGS, limit=8),
            priority=clamp01(data.get("priority"), 0.5),
            impact=coerce_one(data.get("impact"), IMPACTS, "medium"),
            flags=coerce_many(data.get("flags"), PROMPT_FLAGS, limit=8),
            group_id=_text(data.get("group_id"), 80),
            duplicates=max(0, int(as_float(data.get("duplicates")))),
        )


@dataclass
class ReviewQueue:
    """What is worth reviewing, in the order it is worth reviewing it.

    ``candidates`` records how many prompts existed before the limit was
    applied. Without it a queue of twenty looks like the whole story, and the
    editor has no way to know that eighty more were dropped.
    """

    queue_id: str = ""
    session_id: str = ""
    run_id: str = ""
    name: str = "structure"
    sequence_name: str = ""
    timebase: str = "empty"
    duration: float = 0.0

    prompts: list[ReviewPrompt] = field(default_factory=list)
    candidates: int = 0
    limit: int = 0
    filters: dict = field(default_factory=dict)
    #: Which artifacts were available when this was built. A queue built with
    #: no critic report is a different claim from one built with.
    sources: dict = field(default_factory=dict)
    generated_at: str = ""
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.prompts)

    @property
    def is_empty(self) -> bool:
        return not self.prompts

    def prompt(self, prompt_id: str) -> Optional[ReviewPrompt]:
        for item in self.prompts:
            if item.prompt_id == prompt_id:
                return item
        return None

    def of_source(self, source: str) -> list[ReviewPrompt]:
        return [item for item in self.prompts if item.source == source]

    def flagged(self, flag: str) -> list[ReviewPrompt]:
        return [item for item in self.prompts if flag in item.flags]

    def groups(self) -> list[list[ReviewPrompt]]:
        """Prompts bundled by timeline neighbourhood, in queue order."""
        seen: dict[str, list[ReviewPrompt]] = {}
        order: list[str] = []
        for item in self.prompts:
            key = item.group_id or item.prompt_id
            if key not in seen:
                seen[key] = []
                order.append(key)
            seen[key].append(item)
        return [seen[key] for key in order]

    def stats(self) -> dict:
        by_source: dict[str, int] = {}
        by_flag: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for item in self.prompts:
            by_source[item.source] = by_source.get(item.source, 0) + 1
            by_category[item.category] = by_category.get(item.category, 0) + 1
            for flag in item.flags:
                by_flag[flag] = by_flag.get(flag, 0) + 1
        return {
            "prompts": len(self.prompts),
            "candidates": self.candidates,
            "dropped_by_limit": max(0, self.candidates - len(self.prompts)),
            "groups": len(self.groups()),
            "by_source": by_source,
            "by_flag": by_flag,
            "by_category": by_category,
            "answered": sum(1 for item in self.prompts if item.answered),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "queue_id": self.queue_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "name": self.name,
            "sequence_name": self.sequence_name,
            "timebase": self.timebase,
            "duration": round(self.duration, 3),
            "generated_at": self.generated_at,
            "basis": NOT_MEASURED,
            "candidates": self.candidates,
            "limit": self.limit,
            "filters": dict(self.filters),
            "sources": dict(self.sources),
            "stats": self.stats(),
            "prompts": [item.to_dict() for item in self.prompts],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewQueue":
        data = data or {}
        return cls(
            queue_id=_text(data.get("queue_id"), 80),
            session_id=_text(data.get("session_id"), 120),
            run_id=_text(data.get("run_id"), 120),
            name=_text(data.get("name"), 80) or "structure",
            sequence_name=_text(data.get("sequence_name"), 200),
            timebase=_text(data.get("timebase"), 20) or "empty",
            duration=as_float(data.get("duration")),
            prompts=[
                ReviewPrompt.from_dict(item)
                for item in (data.get("prompts") or [])
            ],
            candidates=max(0, int(as_float(data.get("candidates")))),
            limit=max(0, int(as_float(data.get("limit")))),
            filters=dict(data.get("filters") or {}),
            sources=dict(data.get("sources") or {}),
            generated_at=_text(data.get("generated_at"), 40),
            warnings=[str(w)[:400] for w in (data.get("warnings") or [])],
        )


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

SESSION_STATUSES = ("open", "closed")


@dataclass
class FeedbackSession:
    """One sitting of review, and the folder it owns.

    A session is metadata; the feedback itself lives in an append-only log
    beside it. That split is what makes "never overwrite old feedback" cheap:
    the only mutable file is this one, and it holds counts and timestamps
    rather than opinions.
    """

    session_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    status: str = "open"

    run_id: str = ""
    name: str = "structure"
    sequence_name: str = ""
    timebase: str = "empty"
    duration: float = 0.0
    style: str = ""
    #: Where the artifacts being reviewed live, so a session opened tomorrow
    #: still knows which run it was about.
    artifact_root: str = ""
    #: What existed when the session was started.
    sources: dict = field(default_factory=dict)

    title: str = ""
    notes: str = ""
    counts: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "run_id": self.run_id,
            "name": self.name,
            "sequence_name": self.sequence_name,
            "timebase": self.timebase,
            "duration": round(self.duration, 3),
            "style": self.style,
            "artifact_root": self.artifact_root,
            "sources": dict(self.sources),
            "title": self.title,
            "notes": self.notes,
            "counts": dict(self.counts),
            "warnings": list(self.warnings),
            "basis": NOT_MEASURED,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FeedbackSession":
        data = data or {}
        return cls(
            session_id=_text(data.get("session_id"), 120),
            created_at=_text(data.get("created_at"), 40),
            updated_at=_text(data.get("updated_at"), 40),
            status=coerce_one(data.get("status"), SESSION_STATUSES, "open"),
            run_id=_text(data.get("run_id"), 120),
            name=_text(data.get("name"), 80) or "structure",
            sequence_name=_text(data.get("sequence_name"), 200),
            timebase=_text(data.get("timebase"), 20) or "empty",
            duration=as_float(data.get("duration")),
            style=_text(data.get("style"), 80),
            artifact_root=_text(data.get("artifact_root"), 500),
            sources=dict(data.get("sources") or {}),
            title=_text(data.get("title"), 200),
            notes=_text(data.get("notes"), 2000),
            counts=dict(data.get("counts") or {}),
            warnings=[str(w)[:400] for w in (data.get("warnings") or [])],
        )


# ---------------------------------------------------------------------------
# Preference signals
# ---------------------------------------------------------------------------

#: What a preference can be *about*. A dimension plus a direction is what makes
#: "user dislikes too many captions" and "user likes danger captions" two
#: comparable records rather than two sentences -- which is the difference
#: between something a later session can count and something it has to parse.
PREFERENCE_DIMENSIONS = (
    "cut_pace",             # slow cinematic holds vs fast cutting
    "caption_density",
    "caption_tone",         # danger/hype copy specifically
    "caption_quality",
    "sfx_use",
    "music_use",
    "hud_visibility",       # keeping the game readable under overlays
    "callbacks",
    "callback_naturalness",
    "grind_length",
    "marker_clutter",
    "story_vs_speed",
    "hook_strength",
    "clickbait",
    "objective_clarity",
    "transition_clarity",
    "payoff_strength",
    "context_amount",
    "comedy",
    "unknown",
)

PREFERENCE_DIRECTIONS = ("more", "less", "keep", "unknown")

PREFERENCE_SCOPES = ("episode", "global")

#: How each (dimension, direction) reads in a sentence. Generated rather than
#: typed per signal so two sessions describing the same preference produce the
#: same words, which is what makes them countable.
PREFERENCE_STATEMENTS = {
    ("cut_pace", "less"): "prefers slower, longer holds over fast cutting",
    ("cut_pace", "more"): "prefers a faster cut with shorter holds",
    ("caption_density", "less"): "dislikes having many captions on screen",
    ("caption_density", "more"): "wants more captions than the edit used",
    ("caption_tone", "more"): "likes danger and hype captions",
    ("caption_tone", "less"): "dislikes danger and hype captions",
    ("caption_quality", "less"): "found the caption copy itself weak",
    ("caption_quality", "more"): "found the caption copy itself strong",
    ("sfx_use", "less"): "dislikes sound effects placed without a clear reason",
    ("sfx_use", "more"): "wants more sound effects",
    ("music_use", "less"): "wants less music under the edit",
    ("music_use", "more"): "wants more music under the edit",
    ("hud_visibility", "more"): "wants the game HUD kept clear and readable",
    ("hud_visibility", "less"): "is comfortable with overlays covering the HUD",
    ("callbacks", "more"): "likes callbacks to earlier moments",
    ("callbacks", "less"): "wants fewer callbacks",
    ("callback_naturalness", "less"): "dislikes callbacks that feel forced",
    ("callback_naturalness", "more"): "found the callbacks well judged",
    ("grind_length", "less"): "dislikes long grind sections",
    ("grind_length", "more"): "is happy to leave grind sections long",
    ("marker_clutter", "less"): "wants less marker-only clutter",
    ("marker_clutter", "more"): "wants more markers left for review",
    ("story_vs_speed", "more"): "prefers story over speed",
    ("story_vs_speed", "less"): "prefers speed over story",
    ("hook_strength", "more"): "wants stronger hooks",
    ("hook_strength", "keep"): "found the hooks strong",
    ("clickbait", "less"): "dislikes hooks that overpromise",
    ("objective_clarity", "more"): "wants the objective stated clearly",
    ("transition_clarity", "more"): "dislikes confusing transitions",
    ("payoff_strength", "more"): "wants payoffs to land harder",
    ("payoff_strength", "keep"): "found the payoffs strong",
    ("context_amount", "less"): "wants less explanatory context",
    ("context_amount", "more"): "wants more explanatory context",
    ("comedy", "more"): "wants more of the comedy kept in",
    ("comedy", "keep"): "found the comedy well judged",
}


def statement_for(dimension: str, direction: str) -> str:
    """The sentence for a preference, or an honest placeholder."""
    found = PREFERENCE_STATEMENTS.get((dimension, direction))
    if found:
        return found
    return f"has an unclassified preference about {dimension} ({direction})"


#: How many independent feedback items a preference needs before it is
#: anything more than one person's one-off reaction. Below this the signal is
#: still recorded -- it is real data -- and its confidence stays low.
MIN_EVIDENCE = 2

#: Ceiling on a preference's confidence by evidence count. Same shape as
#: Session 8's channel cap and the same intent: nothing here is ever certain,
#: and a preference stated once cannot outvote one stated six times.
PREFERENCE_CAP = {0: 0.0, 1: 0.30, 2: 0.50, 3: 0.65, 4: 0.75}
PREFERENCE_CAP_MAX = 0.85

#: A preference below this is never marked safe to apply automatically.
SAFE_TO_APPLY_AT_OR_ABOVE = 0.65


def preference_cap(count: int) -> float:
    return PREFERENCE_CAP.get(max(0, int(count)), PREFERENCE_CAP_MAX)


@dataclass
class PreferenceSignal:
    """A tendency read off several pieces of feedback.

    Never applied by anything. ``safe_to_apply_automatically`` describes the
    evidence -- enough of it, consistent, and about something that only ever
    adds or removes an annotation -- and remains a suggestion to a future
    session rather than permission this one has granted.
    """

    signal_id: str = ""
    dimension: str = "unknown"
    direction: str = "unknown"
    statement: str = ""

    evidence_count: int = 0
    positive_examples: list[str] = field(default_factory=list)
    negative_examples: list[str] = field(default_factory=list)
    #: Feedback IDs behind this signal, in the order they were given.
    source_feedback_ids: list[str] = field(default_factory=list)
    #: The one-line summaries, so the signal reads without the log.
    quotes: list[str] = field(default_factory=list)

    confidence: float = 0.0
    #: How consistently the evidence pointed the same way, 0..1.
    agreement: float = 0.0
    #: Feedback that pointed the other way. Kept, not dropped.
    contradictions: int = 0

    is_style_specific: bool = False
    style: str = ""
    scope: str = "episode"
    safe_to_apply_automatically: bool = False
    why_not_safe: str = ""

    first_seen: str = ""
    last_seen: str = ""
    schema_version: int = 1

    @property
    def is_global(self) -> bool:
        return self.scope == "global"

    def line(self) -> str:
        scope = "global" if self.is_global else "episode"
        style = f" [{self.style}]" if self.is_style_specific else ""
        return (
            f"{'A' if self.safe_to_apply_automatically else '.'} "
            f"{self.dimension:<22} {self.direction:<5} "
            f"n={self.evidence_count:<3} c={self.confidence:.2f} "
            f"{scope}{style}  {self.statement[:44]}"
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "dimension": self.dimension,
            "direction": self.direction,
            "statement": self.statement,
            "preference": f"{self.dimension}:{self.direction}",
            "evidence_count": self.evidence_count,
            "positive_examples": list(self.positive_examples),
            "negative_examples": list(self.negative_examples),
            "source_feedback_ids": list(self.source_feedback_ids),
            "quotes": list(self.quotes),
            "confidence": round(self.confidence, 3),
            "agreement": round(self.agreement, 3),
            "contradictions": self.contradictions,
            "is_style_specific": self.is_style_specific,
            "style": self.style,
            "scope": self.scope,
            "safe_to_apply_automatically": self.safe_to_apply_automatically,
            "why_not_safe": self.why_not_safe,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PreferenceSignal":
        data = data or {}
        return cls(
            signal_id=_text(data.get("signal_id"), 80),
            dimension=coerce_one(
                data.get("dimension"), PREFERENCE_DIMENSIONS, "unknown"),
            direction=coerce_one(
                data.get("direction"), PREFERENCE_DIRECTIONS, "unknown"),
            statement=_text(data.get("statement"), 300),
            evidence_count=max(0, int(as_float(data.get("evidence_count")))),
            positive_examples=as_str_list(
                data.get("positive_examples"), limit=100),
            negative_examples=as_str_list(
                data.get("negative_examples"), limit=100),
            source_feedback_ids=as_str_list(
                data.get("source_feedback_ids"), limit=200),
            quotes=[str(q)[:300] for q in (data.get("quotes") or [])][:20],
            confidence=clamp01(data.get("confidence"), 0.0),
            agreement=clamp01(data.get("agreement"), 0.0),
            contradictions=max(0, int(as_float(data.get("contradictions")))),
            is_style_specific=as_bool(data.get("is_style_specific")),
            style=_text(data.get("style"), 80),
            scope=coerce_one(data.get("scope"), PREFERENCE_SCOPES, "episode"),
            safe_to_apply_automatically=as_bool(
                data.get("safe_to_apply_automatically")),
            why_not_safe=_text(data.get("why_not_safe"), 500),
            first_seen=_text(data.get("first_seen"), 40),
            last_seen=_text(data.get("last_seen"), 40),
        )


# ---------------------------------------------------------------------------
# Training signals
# ---------------------------------------------------------------------------

#: What a piece of feedback could teach. Chosen so each one names a decision
#: some pass already makes, rather than a model architecture.
TASK_TYPES = (
    "ranking",                  # which proposals should win
    "classification",           # what kind of thing is this
    "edit_decision",            # keep, cut, shorten, move
    "caption_decision",         # should there be text here, and what
    "retention_decision",       # is this stretch worth keeping as-is
    "hook_selection",           # which moment opens the video
    "episode_memory_judgment",  # is this what the episode was about
    "callback_decision",        # is this callback worth making
    "asset_matching",           # is this the right sound for this moment
    "critique",                 # was the critic right
    "style_preference",         # does this styling suit the channel
    "unknown",
)

#: Default task for each target type. A layer item is refined further by
#: ``training.task_for`` -- a caption teaches a caption decision, a zoom
#: teaches a style preference -- but the table is the floor.
TASK_FOR_TARGET = {
    "roughcut_placement": "edit_decision",
    "recommendation": "ranking",
    "critic_finding": "critique",
    "revision_recommendation": "critique",
    "layer_item": "style_preference",
    "asset_placement": "asset_matching",
    "episode_beat": "episode_memory_judgment",
    "retention_suggestion": "retention_decision",
    "hook_candidate": "hook_selection",
    "open_loop": "episode_memory_judgment",
    "callback": "callback_decision",
    "timeline_range": "edit_decision",
    "whole_edit": "classification",
}


@dataclass
class TrainingSignal:
    """One feedback item, reshaped into something a dataset builder can read.

    Deliberately not a training example. There is no prompt, no completion and
    no tokenisation here -- those are Session 10's decisions, and baking them
    in now would mean rebuilding every signal the first time that session
    changed its mind about a format.

    ``usable_for_training`` is carried with its reason on both sides. A signal
    that says no *and why* is more useful than one that is quietly absent: it
    tells the next session what kind of feedback the collector is losing.
    """

    signal_id: str = ""
    feedback_id: str = ""
    session_id: str = ""
    run_id: str = ""
    created_at: str = ""

    task: str = "unknown"
    #: References into the artifacts, as ``{"artifact": ..., "id": ...}``.
    input_refs: list[dict] = field(default_factory=list)
    #: What the system decided, in words.
    system_decision: str = ""
    system_confidence: float = 0.0
    #: What the human said about it.
    human_rating: str = ""
    human_polarity: str = "neutral"
    human_correction: str = ""
    correction_action: str = "none"
    reason_labels: list[str] = field(default_factory=list)
    note: str = ""

    #: The state before and after the correction, when both are known.
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)

    target_type: str = "whole_edit"
    target_id: str = ""
    start: float = 0.0
    end: float = 0.0
    timebase: str = "empty"

    usable_for_training: bool = False
    why: str = ""
    why_not: str = ""
    #: How much this example should count, 0..1. Editor confidence and
    #: priority, not a model's opinion of anything.
    weight: float = 0.0
    schema_version: int = 1

    @property
    def has_before_after(self) -> bool:
        return bool(self.before and self.after)

    def line(self) -> str:
        mark = "T" if self.usable_for_training else "."
        return (
            f"{mark} {self.task:<24} {self.human_rating:<16} "
            f"w={self.weight:.2f}  {self.target_type}:"
            f"{self.target_id or f'{self.start:.1f}-{self.end:.1f}'}"
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "feedback_id": self.feedback_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "task": self.task,
            "input_refs": [dict(ref) for ref in self.input_refs],
            "system_decision": self.system_decision,
            "system_confidence": round(self.system_confidence, 3),
            "human_rating": self.human_rating,
            "human_polarity": self.human_polarity,
            "human_correction": self.human_correction,
            "correction_action": self.correction_action,
            "reason_labels": list(self.reason_labels),
            "note": self.note,
            "before": dict(self.before),
            "after": dict(self.after),
            "has_before_after": self.has_before_after,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "timebase": self.timebase,
            "usable_for_training": self.usable_for_training,
            "why": self.why,
            "why_not": self.why_not,
            "weight": round(self.weight, 3),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrainingSignal":
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        return cls(
            signal_id=_text(data.get("signal_id"), 80),
            feedback_id=_text(data.get("feedback_id"), 80),
            session_id=_text(data.get("session_id"), 120),
            run_id=_text(data.get("run_id"), 120),
            created_at=_text(data.get("created_at"), 40),
            task=coerce_one(data.get("task"), TASK_TYPES, "unknown"),
            input_refs=[
                dict(ref) for ref in (data.get("input_refs") or [])
                if isinstance(ref, dict)
            ],
            system_decision=_text(data.get("system_decision"), 500),
            system_confidence=clamp01(data.get("system_confidence"), 0.0),
            human_rating=coerce_one(data.get("human_rating"), RATINGS, "okay"),
            human_polarity=coerce_one(
                data.get("human_polarity"), POLARITIES, "neutral"),
            human_correction=_text(data.get("human_correction"), 600),
            correction_action=coerce_one(
                data.get("correction_action"), CORRECTION_ACTIONS, "none"),
            reason_labels=coerce_many(
                data.get("reason_labels"), REASON_CATEGORIES, limit=14),
            note=_text(data.get("note"), 2000),
            before=dict(data.get("before") or {}),
            after=dict(data.get("after") or {}),
            target_type=coerce_one(
                data.get("target_type"), TARGET_TYPES, "whole_edit"),
            target_id=_text(data.get("target_id"), 120),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            timebase=_text(data.get("timebase"), 20) or "empty",
            usable_for_training=as_bool(data.get("usable_for_training")),
            why=_text(data.get("why"), 600),
            why_not=_text(data.get("why_not"), 600),
            weight=clamp01(data.get("weight"), 0.0),
        )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

EXPORT_FORMATS = ("jsonl", "json", "csv")

#: What an export can contain. ``feedback`` is the raw log; the other two are
#: derived and are regenerated on every export rather than stored.
EXPORT_PARTS = ("feedback", "preferences", "training", "queue")


@dataclass
class FeedbackExport:
    """A record that an export happened, and of exactly what.

    Written next to the exported file. An export whose provenance is not
    recorded is the thing that makes a dataset unreproducible six months later,
    and this layer exists specifically to feed a dataset builder.
    """

    export_id: str = ""
    created_at: str = ""
    session_id: str = ""
    run_id: str = ""
    format: str = "jsonl"
    parts: list[str] = field(default_factory=list)
    path: str = ""
    #: Rows written, per part.
    counts: dict = field(default_factory=dict)
    filters: dict = field(default_factory=dict)
    #: sha256 of the exported bytes, so a later session can tell two exports
    #: of the same session apart.
    checksum: str = ""
    bytes_written: int = 0
    notes: str = ""
    schema_version: int = 1

    @property
    def total_rows(self) -> int:
        return sum(int(value) for value in self.counts.values())

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "export_id": self.export_id,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "format": self.format,
            "parts": list(self.parts),
            "path": self.path,
            "counts": dict(self.counts),
            "total_rows": self.total_rows,
            "filters": dict(self.filters),
            "checksum": self.checksum,
            "bytes_written": self.bytes_written,
            "notes": self.notes,
            "basis": NOT_MEASURED,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FeedbackExport":
        data = data or {}
        return cls(
            export_id=_text(data.get("export_id"), 80),
            created_at=_text(data.get("created_at"), 40),
            session_id=_text(data.get("session_id"), 120),
            run_id=_text(data.get("run_id"), 120),
            format=coerce_one(data.get("format"), EXPORT_FORMATS, "jsonl"),
            parts=coerce_many(data.get("parts"), EXPORT_PARTS, limit=4),
            path=_text(data.get("path"), 500),
            counts=dict(data.get("counts") or {}),
            filters=dict(data.get("filters") or {}),
            checksum=_text(data.get("checksum"), 80),
            bytes_written=max(0, int(as_float(data.get("bytes_written")))),
            notes=_text(data.get("notes"), 600),
        )

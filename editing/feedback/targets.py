"""Finding the thing a piece of feedback is about.

Two jobs, and they are the same job from opposite ends:

* ``Artifacts`` gathers whatever the earlier sessions produced -- the cut, the
  recommendations, the critique, the layers, the assets, the episode memory,
  the retention plan -- into one object, and records which of them were
  actually there. A queue built without a critic report is a different claim
  from one built with, and every consumer needs to be able to tell.
* ``resolve`` takes an ID a person typed and finds the record it names, across
  every one of those collections, returning a ``FeedbackTarget`` with the
  range, the label and the artifact path already filled in.

The second is the reason the first exists. Feedback that cannot be joined back
to a record is a diary entry; this module is what makes the join possible from
a single ID with no other context.

**An ID that resolves to nothing is not an error to swallow.** ``resolve``
returns ``None`` and the caller raises with the list of places it looked, so
"unknown ID" always comes with somewhere to go next.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from editing.errors import EditingError
from editing.feedback.schema import (
    FeedbackTarget, TARGET_TYPES, artifact_for,
)

#: How a record's ID prefix hints at its type. Only a hint -- every collection
#: is searched anyway -- but it makes the error message for a typo much better
#: ("that looks like a layer item ID") than a bare failure.
ID_HINTS = (
    ("p_", "roughcut_placement"),
    ("rec_", "recommendation"),
    ("f_", "critic_finding"),
    ("rev_", "revision_recommendation"),
    ("li_", "layer_item"),
    ("ap_", "asset_placement"),
    ("beat_", "episode_beat"),
    ("sug_", "retention_suggestion"),
    ("hook_", "hook_candidate"),
    ("loop_", "open_loop"),
    ("cb_", "callback"),
)


@dataclass
class Artifacts:
    """Everything the earlier sessions left behind, and what was missing.

    Every field is optional. The whole layer is built to work on a partial
    pipeline -- reviewing a rough cut before the critic has ever run is a
    completely reasonable thing to want -- so absence is recorded in
    ``sources`` and never worked around.
    """

    name: str = "structure"
    style: str = ""
    run_id: str = ""
    artifact_root: str = ""

    timeline: Any = None
    recommendations: Any = None
    roughcut: Any = None
    critique: Any = None
    revisions: Any = None
    layers: Any = None
    asset_plan: Any = None
    memory: Any = None
    retention: Any = None

    #: Problems hit while loading, kept rather than raised.
    warnings: list[str] = field(default_factory=list)

    # -- what is here ----------------------------------------------------

    @property
    def sources(self) -> dict:
        """Which artifacts were available, as a flat dict for the record."""
        return {
            "timeline": self.timeline is not None,
            "recommendations": self.recommendations is not None,
            "roughcut": self.roughcut is not None,
            "critique": self.critique is not None,
            "revisions": self.revisions is not None,
            "layers": self.layers is not None,
            "asset_plan": self.asset_plan is not None,
            "episode_memory": self.memory is not None,
            "retention_plan": self.retention is not None,
        }

    @property
    def is_empty(self) -> bool:
        return not any(self.sources.values())

    @property
    def missing(self) -> list[str]:
        return [key for key, present in self.sources.items() if not present]

    @property
    def sequence_name(self) -> str:
        for candidate in (self.roughcut, self.layers, self.asset_plan,
                          self.memory, self.retention):
            found = getattr(candidate, "sequence_name", "")
            if found:
                return found
        return ""

    @property
    def timebase(self) -> str:
        """Which clock the ranges in this feedback are on.

        Taken from the episode memory when there is one, because that is the
        artifact that decided it. Otherwise a rough cut means sequence time and
        anything else means the synthetic timeline ordering -- the same
        distinction Session 8 draws, and getting it wrong would put feedback on
        the wrong moments.
        """
        if self.memory is not None:
            return getattr(self.memory, "timebase", "empty")
        if self.roughcut is not None:
            return "roughcut"
        if self.timeline is not None:
            return "timeline"
        return "empty"

    @property
    def duration(self) -> float:
        if self.memory is not None:
            duration = float(getattr(self.memory, "duration", 0.0) or 0.0)
            if duration > 0:
                return duration
        if self.roughcut is not None:
            return float(getattr(self.roughcut, "total_duration", 0.0) or 0.0)
        if self.timeline is not None:
            return max(
                (float(getattr(segment, "end", 0.0) or 0.0)
                 for segment in getattr(self.timeline, "segments", ())),
                default=0.0,
            )
        return 0.0

    def artifact(self, target_type: str) -> str:
        return artifact_for(target_type, self.name)

    # -- the collections, in one place -----------------------------------

    def records(self, target_type: str) -> list:
        """Every record of one target type, or an empty list if absent.

        One table rather than nine branches, so adding a target type means
        adding a row here and a builder below, and nothing else in the package
        needs to know.
        """
        getter = _COLLECTIONS.get(target_type)
        return list(getter(self)) if getter else []

    def counts(self) -> dict:
        return {
            target_type: len(self.records(target_type))
            for target_type in _COLLECTIONS
        }


def _attr(holder: Any, name: str, default=()) -> Any:
    if holder is None:
        return default
    return getattr(holder, name, default) or default


def _some(*values) -> list:
    """Flatten optionals and lists into one list, dropping the ``None``s."""
    out = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            out.extend(item for item in value if item is not None)
        else:
            out.append(value)
    return out


#: target type -> how to get its records out of an ``Artifacts``.
#:
#: Three of these are deliberately wider than one field, because a target type
#: names *a kind of thing a person can point at* rather than a field name:
#:
#: * ``episode_beat`` covers beats, setups and payoffs -- all stretches of the
#:   memory with an ``item_id``, all reviewed the same way;
#: * ``hook_candidate`` covers hooks, the climax, the ending and their
#:   alternatives -- all "a moment proposed for a structural job";
#: * ``retention_suggestion`` covers suggestions, risk zones and the midpoint
#:   reset -- all "a finding with something to do about it".
#:
#: Keeping them narrow would mean an ID printed in the queue not resolving when
#: the reviewer typed it back, which is the one failure this module exists to
#: prevent.
_COLLECTIONS = {
    "roughcut_placement": lambda a: _attr(a.roughcut, "placements"),
    "recommendation": lambda a: _attr(a.recommendations, "recommendations"),
    "critic_finding": lambda a: _attr(a.critique, "findings"),
    "revision_recommendation": lambda a: _attr(a.revisions, "revisions"),
    "layer_item": lambda a: _attr(a.layers, "items"),
    "asset_placement": lambda a: _attr(a.asset_plan, "placements"),
    "episode_beat": lambda a: _some(
        _attr(a.memory, "beats"), _attr(a.memory, "setups"),
        _attr(a.memory, "payoffs"),
    ),
    "retention_suggestion": lambda a: _some(
        _attr(a.retention, "suggestions"), _attr(a.retention, "risks"),
        getattr(a.retention, "midpoint_reset", None) if a.retention else None,
    ),
    "hook_candidate": lambda a: _some(
        _attr(a.retention, "hooks"),
        getattr(a.retention, "climax", None) if a.retention else None,
        _attr(a.retention, "climax_alternatives"),
        getattr(a.retention, "ending", None) if a.retention else None,
        _attr(a.retention, "ending_alternatives"),
    ),
    "open_loop": lambda a: _attr(a.memory, "open_loops"),
    "callback": lambda a: _attr(a.memory, "callbacks"),
}

#: target type -> the attribute holding that record's ID.
ID_FIELD = {
    "roughcut_placement": "placement_id",
    "recommendation": "recommendation_id",
    "critic_finding": "finding_id",
    "revision_recommendation": "revision_id",
    "layer_item": "item_id",
    "asset_placement": "placement_id",
    "episode_beat": "item_id",
    "retention_suggestion": "item_id",
    "hook_candidate": "item_id",
    "open_loop": "item_id",
    "callback": "item_id",
}


def record_id(target_type: str, record: Any) -> str:
    return str(getattr(record, ID_FIELD.get(target_type, "item_id"), "") or "")


# ---------------------------------------------------------------------------
# Ranges and labels
# ---------------------------------------------------------------------------

def range_of(target_type: str, record: Any) -> tuple[float, float]:
    """The record's place on the timeline, in whatever clock it uses.

    A rough-cut placement is the awkward one: it carries source in/out *and*
    a sequence start, and the sequence numbers are the ones feedback should be
    in, because that is where the reviewer is looking.
    """
    if target_type == "roughcut_placement":
        start = float(getattr(record, "sequence_start", 0.0) or 0.0)
        return start, start + float(
            getattr(record, "sequence_duration", 0.0) or 0.0)
    start = float(getattr(record, "start", 0.0) or 0.0)
    end = float(getattr(record, "end", start) or start)
    if target_type == "critic_finding" and end <= start:
        # A finding is about one frame; give it a nominal second so it can be
        # grouped with its neighbours rather than sorting as a zero-length gap.
        moment = float(getattr(record, "sequence_time", start) or start)
        return moment, moment + 1.0
    return start, max(start, end)


def label_of(target_type: str, record: Any) -> str:
    """One line describing the record, readable without opening the artifact."""
    def text(*names: str, limit: int = 120) -> str:
        for name in names:
            value = getattr(record, name, "")
            if value:
                return str(value)[:limit]
        return ""

    if target_type == "roughcut_placement":
        return (f"clip {getattr(record, 'index', '?')} "
                f"from {getattr(record, 'source_file', '')[-40:]} -- "
                f"{text('keep_reason') or 'no reason recorded'}")
    if target_type == "recommendation":
        return f"{getattr(record, 'category', '?')}: {text('reason')}"
    if target_type == "critic_finding":
        return f"{getattr(record, 'issue', '?')}: {text('evidence', 'notes')}"
    if target_type == "revision_recommendation":
        return (f"{getattr(record, 'suggested_fix', '?')} for "
                f"{getattr(record, 'issue', '?')}: {text('fix_detail')}")
    if target_type == "layer_item":
        payload = getattr(record, "payload", {}) or {}
        copy = payload.get("text") or payload.get("title") or ""
        return (f"{getattr(record, 'kind', '?')}"
                + (f" \"{str(copy)[:60]}\"" if copy else "")
                + f" -- {text('reason')}")
    if target_type == "asset_placement":
        return (f"{getattr(record, 'kind', '?')} "
                f"[{getattr(record, 'status', '?')}] "
                f"{getattr(record, 'asset_filename', '') or '(nothing placed)'}"
                f" -- {text('reason')}")
    if target_type == "episode_beat":
        # Covers setups and payoffs too, which carry ``text`` and no ``kind``.
        kind = getattr(record, "kind", "") or (
            "setup" if hasattr(record, "payoff_id") else
            "payoff" if hasattr(record, "setup_id") else "beat"
        )
        return f"{kind}: {text('text', 'why')}"
    if target_type == "retention_suggestion":
        return f"{getattr(record, 'type', '?')}: {text('reason')}"
    if target_type == "hook_candidate":
        return (f"{getattr(record, 'hook_type', '?')} hook: "
                f"{text('suggested_text', 'why')}")
    if target_type == "open_loop":
        return text("question", "why") or "an unstated question"
    if target_type == "callback":
        return f"{getattr(record, 'kind', '?')}: {text('label', 'why')}"
    return text("summary", "reason", "why") or target_type


def source_ids_of(target_type: str, record: Any) -> list[str]:
    """The records this one was built from -- the trail back to the footage."""
    out: list[str] = []

    def add(values: Any) -> None:
        for value in (values or ()):
            text = str(value)
            if text and text not in out:
                out.append(text)

    for name in ("segment_ids", "recommendation_ids", "placement_ids",
                 "visual_event_ids", "audio_event_ids", "beat_ids",
                 "risk_ids", "setup_ids", "payoff_ids", "layer_item_ids"):
        add(getattr(record, name, ()))
    for name in ("recommendation_id", "placement_id", "item_id", "finding_id",
                 "frame_id", "source_recommendation_id", "library_asset_id",
                 "resolution_id", "payoff_id", "refers_to_id"):
        value = getattr(record, name, "")
        if value and str(value) not in out:
            out.append(str(value))

    evidence = getattr(record, "evidence", None)
    for name in ("segment_ids", "visual_event_ids", "audio_event_ids",
                 "recommendation_ids", "placement_ids", "layer_item_ids"):
        add(getattr(evidence, name, ()))
    return out[:60]


def confidence_of(record: Any) -> float:
    value = getattr(record, "confidence", None)
    if value is None:
        value = getattr(record, "score", None)
    if value is None:
        value = getattr(record, "priority", None)
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Building targets
# ---------------------------------------------------------------------------

def target_for(
    target_type: str, record: Any, artifacts: Optional[Artifacts] = None
) -> FeedbackTarget:
    """A resolved ``FeedbackTarget`` for a record we are holding."""
    start, end = range_of(target_type, record)
    name = artifacts.name if artifacts else "structure"
    return FeedbackTarget(
        target_type=target_type,
        target_id=record_id(target_type, record),
        start=start,
        end=end,
        label=label_of(target_type, record),
        artifact=artifact_for(target_type, name),
        source_ids=source_ids_of(target_type, record),
        checked=True,
        resolved=True,
        resolution_note="found in " + artifact_for(target_type, name),
    )


def resolve(
    artifacts: Artifacts, target_id: str, *, target_type: str = ""
) -> Optional[FeedbackTarget]:
    """Find the record an ID names, across every loaded collection.

    ``target_type`` narrows the search when the caller already knows. Without
    it every collection is searched, which is what lets the CLI take a bare ID
    -- the reviewer copied it out of the queue and should not have to remember
    which pass produced it.
    """
    wanted = str(target_id or "").strip()
    if not wanted:
        return None
    types = [target_type] if target_type else list(_COLLECTIONS)
    for candidate in types:
        for record in artifacts.records(candidate):
            if record_id(candidate, record) == wanted:
                return target_for(candidate, record, artifacts)
    return None


def unresolved_target(
    target_id: str, artifacts: Artifacts, *, target_type: str = ""
) -> FeedbackTarget:
    """A target for an ID nothing matched.

    Returned rather than raised where a caller has decided to keep the feedback
    anyway. It is marked ``checked`` and not ``resolved``, which is what makes
    ``FeedbackItem.settle`` flag it for follow-up instead of quietly treating
    an unjoinable rating as evidence.
    """
    guess = target_type or guess_type(target_id)
    return FeedbackTarget(
        target_type=guess if guess in TARGET_TYPES else "timeline_range",
        target_id=str(target_id or ""),
        label="(no record found for this ID)",
        artifact=artifacts.artifact(guess) if guess else "",
        checked=True,
        resolved=False,
        resolution_note=(
            "searched " + (", ".join(
                f"{key}({value})"
                for key, value in artifacts.counts().items() if value
            ) or "no loaded artifacts")
        ),
    )


def guess_type(target_id: str) -> str:
    """What kind of record an ID looks like, from its prefix."""
    text = str(target_id or "")
    for prefix, target_type in ID_HINTS:
        if text.startswith(prefix):
            return target_type
    return ""


def range_target(
    start: float, end: float, *, label: str = "", name: str = "structure"
) -> FeedbackTarget:
    """Feedback about a span of the edit, with no record behind it."""
    start = max(0.0, float(start))
    end = max(start, float(end))
    return FeedbackTarget(
        target_type="timeline_range",
        start=start,
        end=end,
        label=label or f"the edit from {start:.1f}s to {end:.1f}s",
        artifact=artifact_for("timeline_range", name),
        checked=True,
        resolved=True,
        resolution_note="a time range, not a record",
    )


def whole_edit_target(*, label: str = "") -> FeedbackTarget:
    return FeedbackTarget(
        target_type="whole_edit",
        label=label or "the edit as a whole",
        checked=True,
        resolved=True,
        resolution_note="the whole edit; no record to join to",
    )


def require(artifacts: Artifacts, target_id: str, *, target_type: str = "") -> FeedbackTarget:
    """Resolve an ID or raise with somewhere to look next.

    The error names every collection that was searched and how many records
    were in each, because the usual cause of a miss is not a typo -- it is that
    the pass that would have produced the record never ran.
    """
    found = resolve(artifacts, target_id, target_type=target_type)
    if found is not None:
        return found

    counts = artifacts.counts()
    searched = ", ".join(
        f"{key} ({value})" for key, value in counts.items() if value
    ) or "nothing -- no artifacts were loaded"
    guess = guess_type(target_id)
    hint = (
        f"Searched: {searched}. "
        "See the queue with `python -m editing.cli feedback queue`, or rate a "
        "time range instead with `feedback rate --range <start>-<end> ...`."
    )
    if guess:
        hint = (f"That looks like a {guess} ID, and "
                f"{counts.get(guess, 0)} of those are loaded. ") + hint
    raise EditingError(
        f"No record found with ID '{target_id}'",
        hint=hint,
        detail={"target_id": target_id, "counts": counts,
                "missing_artifacts": artifacts.missing},
    )

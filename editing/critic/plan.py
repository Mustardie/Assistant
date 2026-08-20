"""Assembling accepted revisions into one operation plan.

The same shape as ``roughcut.convert``: offline, catalog-only, and with the
operation order fixed here rather than left to the caller, because the order is
load-bearing.

1. ``sequence.activate`` -- the rough cut's own scratch sequence, by name. This
   plan never *creates* a sequence: it revises one that already exists, and
   that difference is what the scratch guard checks.
2. Zoom fixes (``property.reset``, then ``animate``). These change one clip's
   Scale and move nothing, so they are safe to do first and safe to redo.
3. Timing fixes (``clip.trim`` with ripple), **back to front**. Rippling shifts
   every later clip, so working backwards means each clip is still where the
   plan says when its turn comes -- the same reasoning Session 3 uses for
   speed operations.
4. Marker operations, at positions **corrected for the ripple above**. A marker
   is placed at an absolute timeline position, so a trim earlier in the
   sequence moves the picture out from under it. The correction is computed
   offline from the trims in this very plan.
5. ``REVIEW`` markers for findings that could not be fixed. These are the
   honest tail of the pass: a real problem, no safe automatic fix, so it goes
   in front of a person at the right moment on the timeline.

Step 4 is the one genuinely uncertain part, and the plan says so out loud:
Premiere's sequence markers do not ripple with clips, so any *pre-existing*
rough-cut marker after a trim ends up describing the wrong frame. This module
cannot fix those without moving markers it did not create, so it counts them
and warns instead.
"""
from __future__ import annotations

import time
from typing import Optional, Sequence

from editing.critic.schema import (
    SEVERITY_ORDER, NotApplied, RevisionPlan, RevisionRecommendation,
    RevisionSet,
)
from editing.roughcut.schema import RoughCutPlan

#: Operations that change clip timing, and therefore ripple.
_TIMING_OPS = frozenset({"clip.trim", "clip.speed", "clip.remove", "gap.remove"})

#: Operations that touch markers.
_MARKER_OPS = frozenset({"marker.add", "marker.remove"})

#: A deferred finding at or above this severity earns a REVIEW marker, so a
#: problem the system could not fix is still visible where it happens rather
#: than only in a report nobody opens.
DEFAULT_MARKER_SEVERITY = "medium"

#: Below this confidence a deferred finding is recorded but not marked -- a
#: timeline peppered with low-confidence guesses is worse than a clean one.
DEFAULT_MARKER_CONFIDENCE = 0.45


def build_revision_plan(
    revisions: RevisionSet,
    roughcut: RoughCutPlan,
    *,
    marker_severity: str = DEFAULT_MARKER_SEVERITY,
    marker_confidence: float = DEFAULT_MARKER_CONFIDENCE,
    mark_deferred: bool = True,
    roughcut_executed: bool = False,
) -> RevisionPlan:
    """Everything accepted, in an order that survives its own ripples."""
    plan = RevisionPlan(
        sequence_name=roughcut.sequence_name or revisions.sequence_name,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        on_scratch=roughcut.on_scratch,
        roughcut_executed=roughcut_executed,
    )

    accepted = revisions.actionable()
    if not accepted and not (mark_deferred and revisions.needing_human()):
        plan.warnings.append(
            "Nothing in this critique converts into an operation, so the "
            "revision plan is empty. The findings are still in the revision "
            "report."
        )
        _record_not_applied(plan, revisions)
        return plan

    placements = {p.placement_id: p for p in roughcut.placements}

    zoom_ops: list[dict] = []
    timing: list[tuple[float, float, dict]] = []   # (anchor, shift, op)
    marker_ops: list[tuple[float, dict]] = []      # (original time, op)
    applied: list[str] = []

    for revision in sorted(accepted, key=lambda r: r.start):
        used = False
        for op in revision.premiere_ops:
            name = str(op.get("op") or "")
            if name in _TIMING_OPS:
                anchor, shift = _ripple_of(op, revision, placements)
                timing.append((anchor, shift, dict(op)))
                used = True
            elif name in _MARKER_OPS:
                at = float(op.get("time", op.get("at", revision.start)) or 0.0)
                marker_ops.append((at, dict(op)))
                used = True
            else:
                zoom_ops.append(dict(op))
                used = True
        if used:
            applied.append(revision.revision_id)

    if mark_deferred:
        for revision in revisions.needing_human():
            if SEVERITY_ORDER.get(revision.severity, 0) < SEVERITY_ORDER.get(
                marker_severity, 1
            ):
                plan.not_applied.append(NotApplied(
                    revision.revision_id, revision.issue, revision.suggested_fix,
                    revision.start,
                    f"{revision.status_reason} Not marked either: severity "
                    f"'{revision.severity}' is below '{marker_severity}'.",
                ))
                continue
            if revision.confidence < marker_confidence:
                plan.not_applied.append(NotApplied(
                    revision.revision_id, revision.issue, revision.suggested_fix,
                    revision.start,
                    f"{revision.status_reason} Not marked either: "
                    f"{revision.confidence:.0%} confidence is below "
                    f"{marker_confidence:.0%}.",
                ))
                continue
            marker_ops.append((revision.start, _review_marker_op(revision)))
            applied.append(revision.revision_id)
            plan.not_applied.append(NotApplied(
                revision.revision_id, revision.issue, revision.suggested_fix,
                revision.start,
                f"{revision.status_reason} A REVIEW marker was placed at "
                f"{revision.start:.2f}s so a person sees it on the timeline.",
            ))

    _record_not_applied(plan, revisions, skip={
        entry.revision_id for entry in plan.not_applied
    })

    if not (zoom_ops or timing or marker_ops):
        # An activate-only plan is not a plan. Leaving ``ops`` empty makes the
        # dry run say "nothing to validate" rather than reporting a pass for a
        # plan that would do nothing but change which sequence is open.
        plan.warnings.append(
            "Every finding was kept as a recommendation, so there is nothing "
            "to apply. See the revision report for what the critic found."
        )
        _add_warnings(plan, revisions, roughcut, [])
        return plan

    ops: list[dict] = [{
        "op": "sequence.activate",
        "name": plan.sequence_name,
        "note": "Revise the rough cut's own scratch sequence. This plan never "
                "creates one.",
    }]
    ops.extend(zoom_ops)

    # Back to front: each trim ripples everything after it, so a later clip
    # must be dealt with while it is still where the plan believes it is.
    for _anchor, _shift, op in sorted(timing, key=lambda t: t[0], reverse=True):
        ops.append(op)

    shifts = [(anchor, shift) for anchor, shift, _ in timing]
    for at, op in sorted(marker_ops, key=lambda pair: pair[0]):
        ops.append(_shifted_marker(op, at, shifts))

    plan.ops = ops
    plan.revision_ids = list(dict.fromkeys(applied))
    _add_warnings(plan, revisions, roughcut, shifts)
    return plan


# ---------------------------------------------------------------------------
# Ripple arithmetic
# ---------------------------------------------------------------------------

def _ripple_of(
    op: dict, revision: RevisionRecommendation, placements: dict
) -> tuple[float, float]:
    """Where a timing op takes effect, and how much it moves later content.

    Returns ``(anchor, shift)``. Content strictly after ``anchor`` ends up
    ``shift`` seconds later -- negative when the clip got shorter.
    """
    placement = placements.get(revision.placement_id)
    by = float(op.get("by") or 0.0)
    edge = str(op.get("edge") or "out")

    if placement is None:
        return revision.start, -by
    # Trimming the head closes the gap from the clip's start; trimming the tail
    # closes it from the clip's end. Everything past that point moves.
    anchor = (
        placement.sequence_start if edge == "in" else placement.sequence_end
    )
    return anchor, -by


def _shifted_marker(op: dict, at: float, shifts: Sequence[tuple]) -> dict:
    """A marker op moved to where its content ends up after the trims."""
    delta = sum(shift for anchor, shift in shifts if anchor <= at)
    if not delta:
        return op
    moved = max(0.0, at + delta)
    out = dict(op)
    key = "time" if "time" in op else "at"
    out[key] = round(moved, 3)
    note = str(out.get("note") or "")
    out["note"] = (
        f"{note} | moved {delta:+.2f}s to follow a trim earlier in this plan"
    ).strip(" |")
    return out


# ---------------------------------------------------------------------------
# Markers for what could not be fixed
# ---------------------------------------------------------------------------

def _review_marker_op(revision: RevisionRecommendation) -> dict:
    parts = [
        f"NEEDS HUMAN REVIEW: {revision.issue}",
        f"{revision.severity} severity, {revision.confidence:.0%} confident",
        revision.visual_evidence[:200],
        revision.status_reason[:200],
        f"[{revision.revision_id}]",
    ]
    return {
        "op": "marker.add",
        "time": round(revision.start, 3),
        "name": "REVIEW",
        "type": "comment",
        "comment": " | ".join(part for part in parts if part)[:500],
        "note": f"revision {revision.revision_id}: could not be fixed "
                f"automatically",
    }


def _record_not_applied(
    plan: RevisionPlan, revisions: RevisionSet, skip: Optional[set] = None
) -> None:
    """Everything that produced no operation, with the reason it did not."""
    skip = skip or set()
    for revision in revisions.revisions:
        if revision.revision_id in skip:
            continue
        if revision.revision_id in plan.revision_ids:
            continue
        if revision.is_actionable:
            continue
        plan.not_applied.append(NotApplied(
            revision.revision_id, revision.issue, revision.suggested_fix,
            revision.start,
            revision.status_reason or "No operation was produced for this "
                                      "revision.",
        ))


def _add_warnings(
    plan: RevisionPlan,
    revisions: RevisionSet,
    roughcut: RoughCutPlan,
    shifts: Sequence[tuple],
) -> None:
    """Say what a person needs to know before running this."""
    if revisions.mock:
        plan.warnings.append(
            "This plan came from the mock critic. It exercises the pipeline; "
            "it is not a judgement about the pictures."
        )

    if not plan.on_scratch:
        plan.warnings.append(
            "The rough cut this revises is not marked as being on a scratch "
            "sequence. Executing would edit whatever that sequence is."
        )

    if not plan.roughcut_executed:
        plan.warnings.append(
            "There is no record of this rough cut having been executed into "
            "Premiere, so the sequence this plan activates may not exist yet. "
            "Run `roughcut execute --yes` first, or expect the activate to "
            "fail."
        )

    if shifts:
        total = sum(abs(shift) for _anchor, shift in shifts)
        earliest = min(anchor for anchor, _shift in shifts)
        stale = [
            marker for marker in roughcut.markers if marker.time > earliest
        ]
        plan.warnings.append(
            f"{len(shifts)} timing change(s) move later clips by "
            f"{total:.2f}s in total. Markers this plan places are corrected "
            "for that, but the correction is computed offline and has not "
            "been verified against Premiere's real ripple behaviour."
        )
        if stale:
            plan.warnings.append(
                f"{len(stale)} marker(s) already on the sequence sit after the "
                "first timing change and will end up describing the wrong "
                "frame. This pass does not move markers it did not create -- "
                "check them, or re-run with --no-timing for a pass that "
                "changes no timing at all."
            )

    if plan.not_applied:
        plan.warnings.append(
            f"{len(plan.not_applied)} finding(s) did not become an operation. "
            "See the not-applied list for the reason on each."
        )

"""``DirectorPlan`` -> the rough cut Session 3 already knows how to build.

The seam, and it is deliberately narrow. This module turns accepted director
ranges into ``SelectedRange`` objects and then hands them to the *existing*
builder -- which means every guard that has ever applied to a rough cut still
applies to a director cut: the same merge, the same handles, the same
assembly arithmetic, the same conversion to operations, the same dry run, the
same execution refusal.

Nothing here writes an operation, and nothing here is a new code path into
Premiere. A director cut is a rough cut whose ranges came from somewhere else.

## Three modes

``heuristic``   Session 3, untouched. The fallback, and what runs when the
                model is unreachable, produced nothing usable, or was never
                asked.
``director``    only the ranges the director asked for and safety accepted.
``hybrid``      the director's ranges, plus the heuristic's for every segment
                the director said nothing about.

**Hybrid is the interesting one.** A director given 160 candidates will make
forty decisions, not 160 -- the prompt explicitly says it does not need one
per range. In ``director`` mode the other 120 are simply absent, which
produces a short, choppy cut. In ``hybrid`` they fall through to the rule the
system has always used, and the director's decisions override wherever they
exist. That is what "supplement the thresholds" means in practice.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from editing.director.schema import DirectorPlan, DirectorRange
from editing.recommend.schema import RecommendationSet
from editing.roughcut.select import SelectedRange, select_ranges
from editing.schema import StructureTimeline

logger = logging.getLogger("nova.editing.director.convert")

#: How much of a heuristic range has to be covered by a director range before
#: the director is considered to have spoken about that footage.
CLAIMED_COVERAGE = 0.5


def to_selected(plan: DirectorPlan) -> list[SelectedRange]:
    """The director's accepted ranges, in the shape the builder consumes."""
    out: list[SelectedRange] = []
    for item in plan.ranges:
        if item.duration <= 0:
            continue
        out.append(SelectedRange(
            asset_id=item.asset_id,
            source_file=item.source_file,
            start=item.start,
            end=item.end,
            keep_reason=item.keep_reason,
            speed=item.speed,
            protected=item.protected,
            recommendation_ids=[],
            segment_ids=list(item.segment_ids),
            notes=_note_for(item),
        ))
    return out


def _note_for(item: DirectorRange) -> str:
    """Why this footage is in the cut, in one line that survives to Premiere.

    Carried onto the ``ClipPlacement`` and therefore into the review notes and
    the render report, so a person watching the proxy at 4:12 can read the
    director's own sentence about it.
    """
    head = f"director[{item.decision_id}]"
    body = item.notes.strip()
    return f"{head}: {body}"[:500] if body else head


def merged_with_heuristic(
    plan: DirectorPlan,
    timeline: StructureTimeline,
    recommendations: Optional[RecommendationSet] = None,
    *,
    keep_threshold: float = 0.40,
    filler_speed: float = 2.0,
    handle: float = 0.25,
    keep_filler: bool = True,
    asset_durations: Optional[dict] = None,
) -> tuple:
    """Director ranges, with the heuristic filling every gap it left.

    Returns ``(ranges, notes)`` -- the notes say how many ranges came from
    each side, which is the number that answers "is the director doing
    anything" and is printed in every report.

    A heuristic range is dropped when a director range already covers most of
    it. Half rather than all, because handles and merges mean the two will
    rarely agree to the frame, and a 90%-covered heuristic range added
    alongside a director one would duplicate that footage.
    """
    director_ranges = to_selected(plan)
    heuristic = select_ranges(
        timeline, recommendations,
        keep_threshold=keep_threshold,
        filler_speed=filler_speed,
        handle=handle,
        keep_filler=keep_filler,
        asset_durations=asset_durations,
    )

    # Everything the director spoke about, including its cuts: a range the
    # director explicitly cut must not be re-added by the heuristic, which is
    # the entire point of asking it.
    claimed = _claimed_spans(plan)

    kept: list[SelectedRange] = []
    dropped = 0
    for entry in heuristic:
        if _is_claimed(entry, claimed):
            dropped += 1
            continue
        entry.notes = (entry.notes + "  [heuristic: the director said "
                                     "nothing about this range]").strip()
        kept.append(entry)

    # Source order, except that a hook keeps its place at the front: the
    # director putting minute nine first is the single most valuable thing it
    # does, and sorting it back into chronological order would undo it.
    hooks = [item for item in plan.ranges if item.is_hook]
    hook_ids = {(item.asset_id, round(item.start, 3)) for item in hooks}
    combined = director_ranges + kept
    combined.sort(key=lambda item: (
        0 if (item.asset_id, round(item.start, 3)) in hook_ids else 1,
        item.source_file,
        item.start,
    ))
    notes = {
        "from_director": len(director_ranges),
        "from_heuristic": len(kept),
        "heuristic_dropped": dropped,
        "heuristic_total": len(heuristic),
    }
    return combined, notes


def _claimed_spans(plan: DirectorPlan) -> dict:
    """``asset_id -> [(start, end), ...]`` for everything the director judged.

    Rejected decisions do not claim anything: if safety threw a decision out,
    the heuristic should get its say on that footage rather than the footage
    silently vanishing because a model mentioned it once.
    """
    spans: dict = {}
    for decision in plan.decisions:
        if not decision.accepted:
            continue
        spans.setdefault(decision.asset_id, []).append(
            (decision.start, decision.end))
    return spans


def _is_claimed(entry: SelectedRange, claimed: dict) -> bool:
    spans = claimed.get(entry.asset_id) or []
    if not spans or entry.duration <= 0:
        return False
    covered = 0.0
    for start, end in spans:
        covered += max(0.0, min(entry.end, end) - max(entry.start, start))
    return covered >= entry.duration * CLAIMED_COVERAGE


def apply_paths(ranges: Sequence[SelectedRange], assets) -> None:
    """Point every range at the file discovery actually found.

    The same correction ``build_rough_cut`` makes: a segment carries the path
    it was analysed from, and an explicitly discovered asset list is more
    authoritative because it is what was probed on disk.
    """
    paths = {asset.asset_id: asset.path for asset in (assets or ())}
    for entry in ranges:
        if entry.asset_id in paths:
            entry.source_file = paths[entry.asset_id]

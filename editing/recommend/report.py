"""The human-readable side of a recommendation set.

The JSON is the machine's copy; this is the one a person reads before deciding
whether to trust any of it. It is organised around the question an editor
actually asks — *which moments matter, and why does this thing think so* —
rather than around the data structure.

Deliberately plain text: it has to survive being pasted into a terminal, a
commit message or a Discord channel. No colour, no box drawing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from editing.recommend.premiere_plan import DraftPlan
from editing.recommend.schema import EditRecommendation, RecommendationSet
from editing.schema import StructureTimeline, TimelineSegment


def _rule(title: str, width: int = 76) -> str:
    return f"\n{title}\n{'-' * min(width, max(len(title), 8))}"


def _clock(seconds: float) -> str:
    minutes, remainder = divmod(max(0.0, seconds), 60.0)
    return f"{int(minutes):02d}:{remainder:05.2f}"


def render(
    recommendations: RecommendationSet,
    *,
    timeline: Optional[StructureTimeline] = None,
    draft: Optional[DraftPlan] = None,
    limit: int = 25,
) -> str:
    """The full report."""
    lines: list[str] = ["EDITING BRAIN V1 -- EDIT RECOMMENDATIONS"]
    stats = recommendations.stats()

    lines.append(f"generated {recommendations.generated_at}   "
                 f"style: {recommendations.style}")
    lines.append(
        f"{stats['total']} recommendation(s): {stats['accepted']} accepted, "
        f"{stats['by_status'].get('downgraded', 0)} downgraded, "
        f"{stats['by_status'].get('rejected', 0)} rejected, "
        f"{stats['by_status'].get('hold', 0)} held"
    )

    if timeline is not None:
        summary = timeline.stats()
        lines.append(
            f"from {summary['segments']} segment(s) over "
            f"{summary['assets']} file(s), {summary['covered_seconds']:.0f}s covered"
        )

    lines.append(_rule("TOP MOMENTS"))
    top = recommendations.top(limit)
    if not top:
        lines.append("  (nothing was accepted -- see REMOVED below)")
    for entry in top:
        lines.append(
            f"  {_clock(entry.start)}-{_clock(entry.end)}  "
            f"{entry.category:<16} {entry.intensity:<6} p={entry.priority:.2f}"
        )
        lines.append(f"      {entry.reason}")
        lines.append(f"      evidence: {_evidence_line(entry)}")
        if entry.risks:
            lines.append(f"      risk: {', '.join(entry.risks)}")

    lines.extend(_audio_section(timeline))
    lines.extend(_held_section(recommendations))
    lines.extend(_removed_section(recommendations))
    lines.extend(_plan_section(draft))

    if recommendations.warnings:
        lines.append(_rule("WARNINGS"))
        for warning in recommendations.warnings:
            lines.append(f"  ! {warning}")

    lines.append("")
    lines.append("Nothing in this report has been applied. It is a proposal.")
    return "\n".join(lines)


def _evidence_line(entry: EditRecommendation) -> str:
    parts = []
    if entry.evidence.visual_event_ids:
        parts.append(f"{len(entry.evidence.visual_event_ids)} visual")
    if entry.evidence.transcript_quotes:
        quote = entry.evidence.transcript_quotes[0]
        parts.append(f'said "{quote[:44]}"')
    if entry.evidence.audio_types:
        parts.append("audio " + ", ".join(entry.evidence.audio_types[:3]))
    return "; ".join(parts) if parts else "none"


def _audio_section(timeline: Optional[StructureTimeline]) -> list[str]:
    """Moments the audio layer made interesting on its own.

    Worth its own section: these are the ones a purely visual pass would have
    walked straight past.
    """
    if timeline is None:
        return []
    reactions = [
        segment for segment in timeline.segments
        if segment.audio_reaction is not None
    ]
    if not reactions:
        return []

    lines = [_rule("AUDIO REACTION MOMENTS")]
    for segment in sorted(
        reactions, key=lambda s: s.audio_reaction.confidence, reverse=True
    )[:15]:
        reaction = segment.audio_reaction
        lines.append(
            f"  {_clock(segment.start)}-{_clock(segment.end)}  "
            f"{reaction.type:<18} {reaction.detection:<18} "
            f"conf={reaction.confidence:.2f}"
        )
        lines.append(
            f"      picture: {segment.importance}"
            + (f"; said \"{segment.said[:44]}\"" if segment.has_speech else "; silent")
        )
    return lines


def _held_section(recommendations: RecommendationSet) -> list[str]:
    held = recommendations.deliberate_holds()
    if not held:
        return []
    lines = [_rule("LEAVE ALONE (deliberate holds)")]
    for entry in held[:15]:
        lines.append(f"  {_clock(entry.start)}-{_clock(entry.end)}  {entry.reason}")
    return lines


def _removed_section(recommendations: RecommendationSet) -> list[str]:
    removed = recommendations.removed()
    if not removed:
        return []
    lines = [_rule("REMOVED OR SOFTENED BY THE SAFETY PASS")]
    for entry in removed[:25]:
        mark = {
            "rejected": "rejected", "downgraded": "softened", "hold": "held back",
        }.get(entry.status, entry.status)
        lines.append(
            f"  [{mark}] {_clock(entry.start)} {entry.category:<16} "
            f"{entry.status_reason}"
        )
    return lines


def _plan_section(draft: Optional[DraftPlan]) -> list[str]:
    if draft is None:
        return []
    lines = [_rule("DRAFT PREMIERE PLAN")]
    lines.append(f"  operations : {draft.operation_count}")
    lines.append(f"  dry run    : {'valid' if draft.valid else 'INVALID'}")
    lines.append(f"  executed   : {draft.executed}  (nothing has been applied)")

    if draft.validation_error:
        lines.append(f"  error      : {draft.validation_error.get('error')}")
        if draft.validation_error.get("hint"):
            lines.append(f"  hint       : {draft.validation_error['hint']}")

    for line in draft.explanation[:20]:
        lines.append(f"    {line}")

    if draft.not_convertible:
        lines.append("")
        lines.append("  Kept as recommendations (no Premiere operation yet):")
        seen: dict = {}
        for entry in draft.not_convertible:
            seen.setdefault(entry["category"], entry["reason"])
        for category, reason in sorted(seen.items()):
            count = sum(
                1 for e in draft.not_convertible if e["category"] == category
            )
            lines.append(f"    {category:<18} x{count:<3} {reason}")
    return lines


def render_top_moments(
    timeline: StructureTimeline, *, limit: int = 20
) -> str:
    """Just the moments, ranked -- the quickest useful read of a timeline."""
    lines = ["TOP MOMENTS"]
    ranked = sorted(
        timeline.segments, key=lambda s: s.usefulness, reverse=True
    )
    for segment in [s for s in ranked if s.usable][:limit]:
        lines.append(
            f"  {_clock(segment.start)}-{_clock(segment.end)}  "
            f"{segment.importance:<8} {segment.alignment:<8} "
            f"score={segment.usefulness:.2f}"
        )
        lines.append(f"      {_segment_line(segment)}")
    if len(lines) == 1:
        lines.append("  (no segment cleared the usable threshold)")
    return "\n".join(lines)


def _segment_line(segment: TimelineSegment) -> str:
    event = segment.events[0] if segment.events else None
    visual = f"{event.environment}/{event.primary_action}" if event else "no visual"
    audio = ", ".join(sorted(segment.audio_types())[:3]) or "no audio events"
    said = segment.said[:50] or "(silence)"
    return f"{visual} | {audio} | {said}"


def write(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target

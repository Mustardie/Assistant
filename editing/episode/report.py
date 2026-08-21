"""The human-readable episode and retention reports.

Both lead with what the layer could not see, for the same reason the Session 7
run report leads with what it did not do: a short risk list reads as "the
episode is fine" unless the reader knows half the detectors had no transcript
to work with. The limits are the first thing on the page, not a footnote.

Every report also carries ``NOT_ANALYTICS`` verbatim. It is a constant rather
than prose written per renderer so it cannot soften into a claim over time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from editing.episode.schema import (
    EpisodeMemory, EpisodeRetentionPlan, MIN_EDIT_CONFIDENCE, NOT_ANALYTICS,
)

_RULE = "=" * 78
_THIN = "-" * 78


def _wrap(text: str, width: int = 76, indent: str = "  ") -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = indent
    for word in words:
        if len(current) + len(word) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = indent
        current += word + " "
    if current.strip():
        lines.append(current.rstrip())
    return lines


def _clock(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):d}:{seconds % 60:05.2f}"


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def render_memory(memory: EpisodeMemory, *, limit: int = 40) -> str:
    lines: list[str] = []
    add = lines.append
    stats = memory.stats()

    add(_RULE)
    add(f"EPISODE MEMORY -- {memory.name}")
    add(_RULE)
    add("")
    add(f"  sequence  : {memory.sequence_name or '(no rough cut)'}")
    add(f"  runtime   : {_clock(memory.duration)}  ({memory.duration:.1f}s)")
    add(f"  timebase  : {memory.timebase}")
    if memory.timebase == "timeline":
        for line in _wrap(
            "these times are a synthetic ordering over the source footage, "
            "not sequence time -- a consumer has to go through segment_ids "
            "rather than using the numbers directly",
            indent="              ",
        ):
            add(line)
    add("")

    add("WHAT THIS COULD AND COULD NOT SEE")
    add(_THIN)
    for label, key in (
        ("rough cut", "roughcut"),
        ("transcript", "transcript"),
        ("visual events", "visual_events"),
        ("audio events", "audio_events"),
        ("motion probed", "motion_probed"),
        ("recommendations", "recommendations"),
        ("style layers", "layers"),
        ("asset plan", "asset_plan"),
    ):
        present = memory.sources.get(key)
        add(f"  {'yes' if present else 'NO ':<4} {label}")
    add("")
    for warning in memory.warnings:
        for line in _wrap("! " + warning):
            add(line)
    if memory.warnings:
        add("")

    add("THE EPISODE")
    add(_THIN)
    objective = memory.main_objective
    if objective is None:
        add("  objective : none stated and none inferable")
    else:
        add(f"  objective : {objective.text}")
        add(f"              status {objective.status}, "
            f"confidence {objective.confidence:.2f}"
            + (f", achieved at {objective.resolved_at:.0f}s"
               if objective.resolved_at is not None else ""))
    for extra in memory.secondary_objectives[:4]:
        add(f"  also      : {extra.text[:60]} ({extra.status})")
    if memory.locations:
        places = ", ".join(
            f"{place.environment} {place.total_seconds:.0f}s"
            + (f" x{place.visits}" if place.visits > 1 else "")
            for place in memory.locations[:6]
        )
        add(f"  places    : {places}")
    if memory.roles:
        people = ", ".join(
            f"{role.name} ({role.role}, x{role.mentions})"
            for role in memory.roles[:6]
        )
        add(f"  people    : {people}   [low confidence: names are guessed]")
    if memory.motifs:
        add("  recurring :")
        for motif in memory.motifs[:8]:
            add(f"      {motif.kind:<8} {motif.label[:34]:<34} "
                f"x{motif.occurrences}  c={motif.confidence:.2f}")
    add("")

    add(f"BEATS ({stats['labelled_beats']} named of {stats['beats']})")
    add(_THIN)
    for beat in memory.beats[:limit]:
        add("  " + beat.summary())
        if beat.alternative and beat.kind != "unknown":
            add(f"      runner-up: {beat.alternative}")
    if len(memory.beats) > limit:
        add(f"  ... and {len(memory.beats) - limit} more")
    add("")

    add(f"OPEN LOOPS ({stats['resolved_loops']} resolved of "
        f"{stats['open_loops']})")
    add(_THIN)
    if not memory.open_loops:
        add("  none -- the episode never asks the viewer a question out loud")
    for loop in memory.open_loops[:limit]:
        add("  " + loop.summary())
        if loop.resolution_reason:
            for line in _wrap(loop.resolution_reason, indent="      "):
                add(line)
        elif loop.candidate_payoffs:
            add("      candidates: " + ", ".join(
                f"{when:.0f}s" for when in loop.candidate_payoffs[:5]))
    add("")

    if memory.setups or memory.payoffs:
        add("SETUP AND PAYOFF")
        add(_THIN)
        for setup in memory.setups[:limit]:
            mark = "+" if setup.paid_off else "-"
            add(f"  {mark} [{setup.start:7.2f}] {setup.text[:58]}")
        for payoff in memory.payoffs[:limit]:
            add(f"    -> [{payoff.start:7.2f}] pays it off "
                f"{payoff.gap_seconds:.0f}s later: {payoff.match_reason[:44]}")
        add("")

    if memory.callbacks:
        add("CALLBACK OPPORTUNITIES")
        add(_THIN)
        for callback in memory.callbacks[:limit]:
            add(f"  [{callback.start:7.2f}] {callback.kind:<7} refers to "
                f"{callback.refers_to_time:7.2f}  c={callback.confidence:.2f}  "
                f"{callback.label[:34]}")
        add("")

    add(_THIN)
    for line in _wrap(NOT_ANALYTICS):
        add(line)
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Retention plan
# ---------------------------------------------------------------------------

def render_plan(
    plan: EpisodeRetentionPlan,
    *,
    memory: Optional[EpisodeMemory] = None,
    limit: int = 40,
) -> str:
    lines: list[str] = []
    add = lines.append
    stats = plan.stats()

    add(_RULE)
    add(f"RETENTION PLAN -- {plan.name}")
    add(_RULE)
    add("")
    for line in _wrap(NOT_ANALYTICS):
        add(line)
    add("")
    add(f"  runtime     : {_clock(plan.duration)}")
    add(f"  timebase    : {plan.timebase}")
    add(f"  risks       : {stats['risks']} "
        f"({stats['high_severity']} high severity)")
    add(f"  hooks       : {stats['hooks']} candidates, "
        f"{stats['hooks_with_payoff']} with a payoff later")
    add(f"  climax      : "
        + ("picked" if stats["has_climax"] else "no clear peak"))
    add(f"  suggestions : {stats['suggestions']} "
        f"({stats['auto_safe']} safe to apply, "
        f"{stats['marker_only']} for a person)")
    add("")

    if plan.warnings:
        add("WHAT THIS PLAN COULD NOT SEE")
        add(_THIN)
        for warning in plan.warnings:
            for line in _wrap("! " + warning):
                add(line)
        add("")

    add("RISK ZONES  (worst first)")
    add(_THIN)
    if not plan.risks:
        add("  none found -- which is not the same as none present; see the "
            "limits above")
    for zone in plan.risks[:limit]:
        add("  " + zone.summary())
        add(f"      fix: {zone.suggested_fix}"
            + ("  [safe to apply]" if zone.fix_is_safe_automatically
               else "  [marker only]"))
    add("")

    add("HOOK CANDIDATES")
    add(_THIN)
    if not plan.hooks:
        add("  none cleared the floor")
    for hook in plan.top_hooks(limit):
        add("  " + hook.summary())
        add(f"      asks: {hook.viewer_question}")
        add(f"      text: {hook.text_source}")
        for risk in hook.risks:
            add(f"      ! {risk}")
    add("")

    add("PEAK AND ENDING")
    add(_THIN)
    if plan.climax is None:
        add("  climax: no single moment stands out; candidates were")
        for alt in plan.climax_alternatives[:4]:
            add(f"      [{alt.start:7.2f}-{alt.end:7.2f}] {alt.why}")
    else:
        add(f"  climax: [{plan.climax.start:7.2f}-{plan.climax.end:7.2f}] "
            f"{plan.climax.why}")
        for alt in plan.climax_alternatives[:3]:
            add(f"      also considered [{alt.start:7.2f}] "
                f"score {alt.score:.2f}")
    if plan.ending is None:
        add("  ending: nothing near the end reads as one")
    else:
        add(f"  ending: [{plan.ending.start:7.2f}-{plan.ending.end:7.2f}] "
            f"{plan.ending.why}")
        if plan.ending.suggested_text:
            add(f"      \"{plan.ending.suggested_text}\" "
                f"({plan.ending.text_source})")
    if plan.midpoint_reset is not None:
        add(f"  midpoint: [{plan.midpoint_reset.start:7.2f}] "
            f"{plan.midpoint_reset.reason[:56]}")
    add("")

    add("SUGGESTIONS  (+ safe to apply, = marker only)")
    add(_THIN)
    if not plan.suggestions:
        add("  none")
    for stage in ("roughcut", "style", "assets", "human"):
        for_stage = plan.suggestions_for(stage)
        if not for_stage:
            continue
        add(f"  -> {stage}  ({len(for_stage)})")
        for suggestion in for_stage[:limit]:
            add("    " + suggestion.summary())
    add("")

    add(_THIN)
    for line in _wrap(
        "Nothing above has been applied. Every suggestion names a range and a "
        f"reason; anything below {MIN_EDIT_CONFIDENCE:.2f} confidence is a "
        "marker for a person rather than something a later pass may act on."
    ):
        add(line)
    add("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Focused views, for the CLI
# ---------------------------------------------------------------------------

def render_beats(memory: EpisodeMemory, *, kind: str = "", limit: int = 200) -> str:
    beats = [
        beat for beat in memory.beats
        if not kind or beat.kind == kind
    ]
    lines = [f"{len(beats)} beat(s)" + (f" of kind '{kind}'" if kind else "")
             + f" across {_clock(memory.duration)}:", ""]
    for beat in beats[:limit]:
        lines.append("  " + beat.summary())
        if beat.evidence.quotes:
            lines.append(f'      "{beat.evidence.quotes[0][:64]}"')
    if not beats:
        lines.append("  none")
    return "\n".join(lines)


def render_risks(
    plan: EpisodeRetentionPlan, *, severity: str = "", limit: int = 200
) -> str:
    zones = [
        zone for zone in plan.risks
        if not severity or zone.severity == severity
    ]
    lines = [
        NOT_ANALYTICS, "",
        f"{len(zones)} risk zone(s)"
        + (f" at severity '{severity}'" if severity else "") + ":",
        "",
    ]
    for zone in zones[:limit]:
        lines.append("  " + zone.summary())
        lines.append(
            f"      fix: {zone.suggested_fix}"
            + ("  [safe to apply]" if zone.fix_is_safe_automatically
               else "  [marker only]")
        )
        lines.extend(_wrap(zone.why, indent="      "))
    if not zones:
        lines.append("  none")
    return "\n".join(lines)


def render_hooks(plan: EpisodeRetentionPlan, *, limit: int = 10) -> str:
    lines = [f"{len(plan.hooks)} hook candidate(s), best first:", ""]
    for index, hook in enumerate(plan.top_hooks(limit), start=1):
        lines.append(
            f"  {index}. [{hook.start:7.2f}-{hook.end:7.2f}] "
            f"{hook.hook_type}  score {hook.score:.2f}  "
            f"confidence {hook.confidence:.2f}"
        )
        lines.append(f'      text : "{hook.suggested_text}" '
                     f'({hook.text_source})')
        lines.append(f"      asks : {hook.viewer_question}")
        lines.append(
            "      pays : "
            + (f"{hook.payoff_at:.0f}s" if hook.payoff_at is not None
               else "nothing later answers it")
        )
        parts = ", ".join(
            f"{name}={value:.2f}" for name, value in hook.score_parts.items()
            if value
        )
        lines.append(f"      score: {parts}")
        for risk in hook.risks:
            lines.append(f"      !    {risk}")
        lines.append("")
    if not plan.hooks:
        lines.append("  none cleared the floor")
    return "\n".join(lines)


def render_open_loops(memory: EpisodeMemory, *, unresolved_only: bool = False,
                      limit: int = 200) -> str:
    loops = [
        loop for loop in memory.open_loops
        if not unresolved_only or not loop.resolved
    ]
    lines = [f"{len(loops)} open loop(s):", ""]
    for loop in loops[:limit]:
        lines.append("  " + loop.summary())
        lines.extend(_wrap(loop.why_viewer_cares, indent="      "))
        if loop.resolution_reason:
            lines.extend(_wrap(loop.resolution_reason, indent="      "))
        if loop.candidate_payoffs:
            lines.append("      candidates: " + ", ".join(
                f"{when:.0f}s" for when in loop.candidate_payoffs[:6]))
        lines.append(f"      suggests: {loop.suggested_use}")
        lines.append("")
    if not loops:
        lines.append("  none")
    return "\n".join(lines)


def render_callbacks(memory: EpisodeMemory, *, limit: int = 200) -> str:
    lines = [f"{len(memory.callbacks)} callback opportunity(ies):", ""]
    for callback in memory.callbacks[:limit]:
        lines.append(
            f"  [{callback.start:7.2f}] {callback.kind:<7} refers back to "
            f"{callback.refers_to_time:7.2f} "
            f"({callback.gap_seconds / 60.0:.1f} min earlier)  "
            f"c={callback.confidence:.2f}"
        )
        lines.extend(_wrap(callback.why, indent="      "))
        if callback.suggested_text:
            lines.append(f'      caption: "{callback.suggested_text}"')
    if not memory.callbacks:
        lines.append("  none")
    return "\n".join(lines)


def write(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target

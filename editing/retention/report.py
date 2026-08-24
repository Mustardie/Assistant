"""What the retention pass did, said plainly.

Organised around what people ask after a pass that reshapes an episode:

1. what does it open on now?
2. what came out, and what stayed?
3. **what did the rules refuse?**
4. what is still unresolved?
5. what do I type to watch it?

Point three again gets its own section. Every layer in this system that lets
something propose a change also lets rules refuse one, and a report that
listed the accepted half would misrepresent all of them.
"""
from __future__ import annotations

from typing import Optional

from editing.retention.schema import (
    NOT_MEASURED, RetentionCutPlan, RetentionCutReport, now,
)

_RULE = "=" * 78
_THIN = "-" * 78

#: True of every retention cut, however well the pass went.
LIMITATIONS = (
    "Nothing here measures retention. Every count below is a count of what "
    "was changed in the edit, not a prediction about an audience.",
    "The findings this acts on came from Session 8, whose thresholds are "
    "calibrated against intuition rather than against outcomes.",
    "A cold open is chosen from hook candidates ranked by a scoring formula. "
    "It has no idea what your channel usually opens on.",
    "Silence is judged by what surrounds it. A pause that is funny for a "
    "reason the footage does not show will read as dead air.",
    "Compression keeps context at each end, but a heavily compressed grind "
    "can still leave a viewer wondering how they got somewhere.",
    "This executes nothing and touches no other cut. It writes a variant.",
)


def build_report(plan: RetentionCutPlan) -> RetentionCutReport:
    """Everything worth saying about one retention pass."""
    stats = plan.stats()
    cold = plan.cold_open

    report = RetentionCutReport(
        name=plan.name,
        mode=plan.mode,
        base=plan.base,
        applied=plan.applied,
        stats=stats,
        generated_at=now(),
        warnings=list(plan.warnings),
        limitations=list(LIMITATIONS),
        unresolved=list(plan.unresolved_warnings),
        failure=plan.failure.to_dict() if plan.failure else None,
    )

    report.cold_open = {
        "chosen": cold.chosen,
        "hook_type": cold.hook_type,
        "seconds": round(cold.duration, 2),
        "lifted_from": round(cold.original_start, 2),
        "viewer_question": cold.viewer_question,
        "payoff_at": cold.payoff_at,
        "duplicate_policy": cold.duplicate_policy,
        "original_removed": cold.original_removed,
        "refused": cold.rejected,
        "warnings": cold.warnings,
        "fallback_reason": cold.fallback_reason,
    }
    report.compression = {
        "zones": plan.sag.zones,
        "compressed": plan.sag.zones_compressed,
        "marked_only": plan.sag.zones_marked_only,
        "refused": plan.sag.zones_refused,
        "seconds_removed": round(plan.sag.seconds_removed, 2),
        "seconds_sped_up": round(plan.sag.seconds_sped_up, 2),
    }
    report.protection = {
        "setups": [item.to_dict() for item in plan.setups],
        "payoffs": [item.to_dict() for item in plan.payoffs],
        "protected_seconds": round(
            sum(span.duration for span in plan.protected_spans), 2),
    }
    report.dead_air = {
        "trimmed": sum(1 for item in plan.dead_air if item.accepted),
        "kept": sum(1 for item in plan.dead_air if not item.accepted),
        "seconds_removed": round(sum(
            item.seconds_removed for item in plan.dead_air
            if item.accepted), 2),
        "by_purpose": _by_purpose(plan),
    }
    report.rejected = [
        {
            "action": decision.action,
            "source": decision.source_type,
            "at": round(decision.episode_start, 1),
            "code": decision.reject_code,
            "why": decision.rejected_reason[:300],
        }
        for decision in plan.rejected
    ]
    report.next_commands = next_commands(plan)
    return report


def _by_purpose(plan: RetentionCutPlan) -> dict:
    out: dict = {}
    for item in plan.dead_air:
        if item.accepted:
            continue
        key = item.purpose or "ordinary"
        out[key] = out.get(key, 0) + 1
    return out


def next_commands(plan: RetentionCutPlan) -> list[str]:
    if plan.failure is not None:
        out = [plan.failure.hint] if plan.failure.hint else []
        out.append("python -m editing.cli retention plan --mode report_only")
        return out

    out = [f"python -m editing.cli retention show-cold-open --name {plan.name}"]
    if plan.sag.zones:
        out.append(
            f"python -m editing.cli retention show-compression "
            f"--name {plan.name}")
    if plan.rejected:
        out.append(
            f"python -m editing.cli retention show-rejected --name {plan.name}")
    out.append(f"python -m editing.cli retention compare --name {plan.name}")
    if plan.applied:
        out.append("python -m editing.cli retention render --quality proxy")
    else:
        out.append("python -m editing.cli retention plan --mode retention")
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(plan: RetentionCutPlan) -> str:
    """The full report, for ``<name>.plan.txt`` and ``retention report``."""
    stats = plan.stats()
    lines: list[str] = []
    add = lines.append

    add(_RULE)
    add(f"RETENTION CUT -- {plan.name}")
    add(_RULE)
    add(f"mode       : {plan.mode}"
        + ("" if plan.applied else "   (nothing was applied)"))
    add(f"built on   : the {plan.base} cut")
    add(f"timebase   : {plan.timebase}")
    add(f"decisions  : {stats['decisions']} made, {stats['accepted']} "
        f"accepted, {stats['rejected']} refused")
    add(f"runtime    : {stats['cut_duration']:.0f}s "
        f"(was {stats['base_duration']:.0f}s)")
    add("")

    if plan.failure is not None:
        add(_THIN)
        add("NOTHING WAS DONE")
        add(_THIN)
        for line in plan.failure.render().splitlines():
            add(f"  {line}")
        add("")
        add("  The cut you already had is untouched.")
        add("")
        add(_RULE)
        return "\n".join(lines)

    # -- cold open ---------------------------------------------------------
    cold = plan.cold_open
    add(_THIN)
    add("OPENS ON")
    add(_THIN)
    if cold.chosen:
        add(f"  {cold.hook_type} from {cold.original_start:.0f}s, "
            f"{cold.duration:.0f}s long  (score {cold.score:.2f})")
        if cold.viewer_question:
            add(f"  asks     : {cold.viewer_question[:70]}")
        if cold.suggested_text:
            add(f"  said     : \"{cold.suggested_text[:66]}\" "
                f"[{cold.text_source}]")
        add(f"  answered : "
            + (f"{cold.payoff_at:.0f}s" if cold.payoff_at is not None
               else "NEVER -- opens a question the episode does not close"))
        add(f"  original : {cold.duplicate_policy}"
            + ("  (removed, so it does not play twice)"
               if cold.original_removed else ""))
    else:
        add("  Nothing. The episode opens where it always did.")
        for line in _wrap(cold.fallback_reason, 72):
            add(f"  {line}")
    for warning in cold.warnings:
        add(f"  ! {warning[:150]}")
    if cold.rejected:
        add("")
        add(f"  {len(cold.rejected)} candidate(s) were refused:")
        for entry in cold.rejected[:6]:
            add(f"    {entry.get('start', 0):.0f}s {entry.get('hook_type', '')}"
                f" (score {entry.get('score', 0):.2f}): "
                f"{str(entry.get('why', ''))[:52]}")
    add("")

    # -- compression -------------------------------------------------------
    add(_THIN)
    add(f"COMPRESSED ({plan.sag.zones_compressed} zone(s), "
        f"{plan.sag.seconds_removed:.0f}s out, "
        f"{plan.sag.seconds_sped_up:.0f}s sped up)")
    add(_THIN)
    if not plan.sag.zones:
        add("  The retention planner found no risk zones to compress.")
    for zone in plan.sag.zones[:20]:
        mark = "+" if zone.get("accepted") else "."
        add(f"  {mark} {zone.get('start', 0):>7.0f}-{zone.get('end', 0):<7.0f} "
            f"{zone.get('risk', ''):<22} {zone.get('severity', ''):<7} "
            f"{zone.get('action', '')}")
        add(f"        {str(zone.get('why', ''))[:68]}")
    if len(plan.sag.zones) > 20:
        add(f"  ... and {len(plan.sag.zones) - 20} more.")
    add("")

    # -- protection --------------------------------------------------------
    protected_setups = [item for item in plan.setups if item.protected]
    protected_payoffs = [item for item in plan.payoffs if item.protected]
    add(_THIN)
    add(f"PROTECTED ({len(protected_setups)} setup(s), "
        f"{len(protected_payoffs)} payoff(s))")
    add(_THIN)
    add("  Claimed before anything that removes footage runs, so no later")
    add("  rule can take these out.")
    add("")
    for item in protected_payoffs[:12]:
        label = "PEAK  " if item.is_climax else "payoff"
        add(f"  {label} {item.episode_start:>7.0f}s  {item.payoff_id[:16]:<16} "
            f"{item.reason[:40]}")
    for item in protected_setups[:12]:
        add(f"  setup  {item.episode_start:>7.0f}s  {item.setup_id[:16]:<16} "
            f"-> {item.payoff_id[:16]}")
    if not protected_setups and not protected_payoffs:
        add("  Nothing was protected: the episode memory found no setup or")
        add("  payoff pairs in this footage.")
    add("")

    # -- dead air ----------------------------------------------------------
    trimmed = [item for item in plan.dead_air if item.accepted]
    kept = [item for item in plan.dead_air if not item.accepted]
    add(_THIN)
    add(f"SILENCE ({len(trimmed)} trimmed, {len(kept)} kept)")
    add(_THIN)
    add(f"  Ordinary silence is cut past "
        f"{plan.config.ordinary_silence_limit:.1f}s "
        f"({plan.config.dead_air_aggressiveness} setting). Silence doing a "
        "job is")
    add(f"  held to {plan.config.max_purposeful_silence:.1f}s.")
    add("")
    for item in trimmed[:10]:
        add(f"  - {item.episode_start:>7.0f}s  {item.duration:.1f}s -> "
            f"{item.seconds_kept:.1f}s   {item.reason[:44]}")
    by_purpose = _by_purpose(plan)
    if by_purpose:
        add("")
        add("  Kept because it was doing something:")
        for purpose, count in sorted(by_purpose.items(),
                                     key=lambda pair: -pair[1]):
            add(f"    {count:>3}  {purpose.replace('_', ' ')}")
    add("")

    # -- refusals ----------------------------------------------------------
    add(_THIN)
    add(f"WHAT THE RULES REFUSED ({len(plan.rejected)})")
    add(_THIN)
    if not plan.rejected:
        add("  Nothing was refused.")
    for decision in plan.rejected[:20]:
        add(f"  x {decision.action:<12} {decision.source_type:<16} "
            f"{decision.episode_start:>7.0f}s  [{decision.reject_code}]")
        add(f"      {decision.rejected_reason[:66]}")
    if len(plan.rejected) > 20:
        add(f"  ... and {len(plan.rejected) - 20} more.")
    add("")

    # -- unresolved --------------------------------------------------------
    unresolved = plan.unresolved_warnings
    if unresolved:
        add(_THIN)
        add(f"UNRESOLVED ({len(unresolved)})")
        add(_THIN)
        add("  Story problems this pass found and cannot fix.")
        add("")
        for warning in unresolved[:12]:
            for line in _wrap(warning, 72):
                add(f"  {line}")
            add("")

    if plan.warnings:
        add(_THIN)
        add(f"WARNINGS ({len(plan.warnings)})")
        add(_THIN)
        for warning in plan.warnings[:20]:
            add(f"  ! {warning[:150]}")
        add("")

    add(_THIN)
    add("NEXT")
    add(_THIN)
    for command in next_commands(plan):
        add(f"  {command}")
    add("")

    add(_THIN)
    add("LIMITATIONS")
    add(_THIN)
    for limitation in LIMITATIONS:
        for line in _wrap(f"- {limitation}", 74):
            add(f"  {line}")
    add("")
    for line in _wrap(NOT_MEASURED, 74):
        add(f"  {line}")
    add("")
    add(_RULE)
    return "\n".join(lines)


def render_cold_open(plan: RetentionCutPlan) -> str:
    """``retention show-cold-open`` -- the opening, and everything refused."""
    cold = plan.cold_open
    lines = [_RULE, f"COLD OPEN -- {plan.name}", _RULE, ""]

    if cold.chosen:
        lines.append(f"  chosen   : {cold.hook_type} "
                     f"[{cold.hook_id}]  score {cold.score:.2f}, "
                     f"confidence {cold.confidence:.2f}")
        lines.append(f"  was at   : {cold.original_start:.1f}-"
                     f"{cold.original_end:.1f}s")
        lines.append(f"  now      : the first {cold.duration:.1f}s of the cut")
        lines.append(f"  question : {cold.viewer_question or '(none stated)'}")
        lines.append(f"  answered : "
                     + (f"{cold.payoff_at:.1f}s" if cold.payoff_at is not None
                        else "never"))
        lines.append(f"  original : {cold.duplicate_policy}"
                     + (f", shortened to {cold.original_shortened_to:.0f}s"
                        if cold.original_shortened_to else ""))
        if cold.suggested_text:
            lines.append(f"  text     : \"{cold.suggested_text[:66]}\" "
                         f"[{cold.text_source}]")
        if cold.risks:
            lines.append(f"  risks    : {', '.join(cold.risks[:5])}")
        lines.append("")
        lines.append("  footage:")
        for span in cold.spans:
            lines.append(f"    {span.source_file} "
                         f"{span.start:.1f}-{span.end:.1f}s")
    else:
        lines.append("  No cold open was chosen.")
        lines.append("")
        for line in _wrap(cold.fallback_reason, 74):
            lines.append(f"  {line}")

    for warning in cold.warnings:
        lines.append("")
        for line in _wrap(f"! {warning}", 74):
            lines.append(f"  {line}")

    if cold.rejected:
        lines.append("")
        lines.append(_THIN)
        lines.append(f"CANDIDATES REFUSED ({len(cold.rejected)})")
        lines.append(_THIN)
        for entry in cold.rejected:
            lines.append(
                f"  {entry.get('start', 0):>7.0f}-{entry.get('end', 0):<7.0f} "
                f"{entry.get('hook_type', ''):<10} score "
                f"{entry.get('score', 0):.2f}  [{entry.get('code', '')}]")
            for line in _wrap(str(entry.get("why", "")), 66):
                lines.append(f"      {line}")
    return "\n".join(lines)


def render_compression(plan: RetentionCutPlan, *, limit: int = 40) -> str:
    """``retention show-compression`` -- every risk zone and its verdict."""
    lines = [_RULE, f"COMPRESSION -- {plan.name}", _RULE, ""]
    lines.append(f"  {plan.sag.zones_compressed} zone(s) compressed, "
                 f"{plan.sag.zones_marked_only} marked only, "
                 f"{plan.sag.zones_refused} refused")
    lines.append(f"  {plan.sag.seconds_removed:.0f}s removed, "
                 f"{plan.sag.seconds_sped_up:.0f}s sped up")
    lines.append("")
    if not plan.sag.zones:
        lines.append("  No risk zones were found in this episode.")
        return "\n".join(lines)

    for zone in plan.sag.zones[:limit]:
        mark = "+" if zone.get("accepted") else "x"
        lines.append(
            f"  {mark} {zone.get('start', 0):>7.0f}-{zone.get('end', 0):<7.0f} "
            f"{zone.get('risk', ''):<22} {zone.get('severity', ''):<7} "
            f"-> {zone.get('action', '')}")
        for line in _wrap(str(zone.get("why", "")), 68):
            lines.append(f"        {line}")
        lines.append("")
    for warning in plan.sag.warnings:
        lines.append(f"  ! {warning[:150]}")
    return "\n".join(lines)


def render_protected(plan: RetentionCutPlan) -> str:
    """``retention show-protected`` -- what nothing may touch, and why."""
    lines = [_RULE, f"PROTECTED -- {plan.name}", _RULE, ""]
    lines.append("  Claimed before compression runs. Nothing that removes")
    lines.append("  footage can reach any of these.")
    lines.append("")

    payoffs = [item for item in plan.payoffs if item.protected]
    setups = [item for item in plan.setups if item.protected]

    lines.append(_THIN)
    lines.append(f"PAYOFFS ({len(payoffs)})")
    lines.append(_THIN)
    for item in payoffs:
        label = "PEAK" if item.is_climax else "payoff"
        lines.append(f"  {label} {item.payoff_id}  "
                     f"{item.episode_start:.1f}-{item.episode_end:.1f}s")
        for line in _wrap(item.reason, 70):
            lines.append(f"      {line}")
        if item.warning:
            for line in _wrap(f"! {item.warning}", 70):
                lines.append(f"      {line}")
        lines.append("")
    if not payoffs:
        lines.append("  None.")
        lines.append("")

    lines.append(_THIN)
    lines.append(f"SETUPS ({len(setups)})")
    lines.append(_THIN)
    for item in setups:
        lines.append(f"  {item.setup_id} -> {item.payoff_id}  "
                     f"{item.episode_start:.1f}-{item.episode_end:.1f}s")
        for line in _wrap(item.reason, 70):
            lines.append(f"      {line}")
        lines.append("")
    if not setups:
        lines.append("  None.")
        lines.append("")

    unprotected = [item for item in plan.setups if not item.protected]
    if unprotected:
        lines.append(_THIN)
        lines.append(f"NOT PROTECTED ({len(unprotected)})")
        lines.append(_THIN)
        for item in unprotected[:15]:
            lines.append(f"  {item.setup_id}  {item.episode_start:.1f}s")
            for line in _wrap(item.reason, 70):
                lines.append(f"      {line}")
    return "\n".join(lines)


def render_rejected(plan: RetentionCutPlan, *, limit: int = 60) -> str:
    """``retention show-rejected`` -- every refusal, with the rule."""
    lines = [_RULE, f"REFUSED RETENTION ACTIONS -- {plan.name} "
                    f"({len(plan.rejected)})", _RULE, ""]
    if not plan.rejected:
        lines.append("  Nothing was refused.")
        return "\n".join(lines)

    by_code: dict = {}
    for decision in plan.rejected:
        by_code[decision.reject_code] = by_code.get(decision.reject_code, 0) + 1
    lines.append("  by rule: " + ", ".join(
        f"{code or 'unknown'} x{count}" for code, count in
        sorted(by_code.items(), key=lambda pair: -pair[1])))
    lines.append("")

    for decision in plan.rejected[:limit]:
        lines.append(f"  {decision.decision_id}  {decision.action.upper()}")
        lines.append(f"    would have : {decision.source_type} at "
                     f"{decision.episode_start:.1f}-"
                     f"{decision.episode_end:.1f}s")
        lines.append(f"    rule       : {decision.reject_code or 'unknown'}")
        for line in _wrap(decision.rejected_reason, 66):
            lines.append(f"    why        : {line}"
                         if line == _wrap(decision.rejected_reason, 66)[0]
                         else f"                 {line}")
        lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = str(text or "").split()
    out: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out or [""]

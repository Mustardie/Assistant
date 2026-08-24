"""The retention cut, measured against the cut it was built from.

Counts, and only counts. There is deliberately no score, no grade and no
percentage that could be read as an audience prediction -- "risk zones
compressed: 4" is a fact about the edit, and "retention improved 12%" would be
a fabrication, because nothing in this system has ever seen a viewer.

That distinction is the whole reason this module is written the way it is. The
temptation with a retention feature is to produce a number that goes up, and a
number that goes up is exactly what a person would trust without checking. So
what this reports is *what changed*: seconds removed, zones compressed, setups
protected, silence trimmed, and every action that was refused.

The last one matters most. A comparison showing twelve accepted changes and
hiding eight refusals would misrepresent a layer whose whole design is that
rules get to say no.
"""
from __future__ import annotations

from typing import Optional, Sequence

from editing.retention.schema import (
    NOT_MEASURED, RetentionCutComparison, RetentionCutPlan, now,
)
from editing.roughcut.select import SelectedRange


def compare(
    plan: RetentionCutPlan,
    before: Sequence[SelectedRange],
    after: Sequence[SelectedRange],
    *,
    name: str = "structure",
) -> RetentionCutComparison:
    """Everything measurable about what the retention pass did."""
    before_seconds = _runtime(before)
    after_seconds = _runtime(after)

    comparison = RetentionCutComparison(
        name=name,
        mode=plan.mode,
        base=plan.base,
        generated_at=now(),
        before={
            "ranges": len(before),
            "cut_seconds": round(before_seconds, 2),
            "source_seconds": round(
                sum(entry.duration for entry in before), 2),
            "protected": sum(1 for entry in before if entry.protected),
            "sped_up": sum(1 for entry in before if entry.speed != 1.0),
            "opens_on": _opens_on(before),
        },
        after={
            "ranges": len(after),
            "cut_seconds": round(after_seconds, 2),
            "source_seconds": round(
                sum(entry.duration for entry in after), 2),
            "protected": sum(1 for entry in after if entry.protected),
            "sped_up": sum(1 for entry in after if entry.speed != 1.0),
            "opens_on": _opens_on(after),
        },
    )

    comparison.difference = {
        "cut_seconds": round(after_seconds - before_seconds, 2),
        "ranges": len(after) - len(before),
        "seconds_removed": plan.seconds_removed,
        "seconds_sped_up": round(plan.sag.seconds_sped_up, 2),
        "risk_zones_compressed": plan.sag.zones_compressed,
        "risk_zones_marked_only": plan.sag.zones_marked_only,
        "dead_air_stretches_trimmed": sum(
            1 for item in plan.dead_air if item.accepted),
        "dead_air_stretches_kept": sum(
            1 for item in plan.dead_air if not item.accepted),
        "setups_protected": sum(1 for item in plan.setups if item.protected),
        "payoffs_protected": sum(1 for item in plan.payoffs if item.protected),
        "actions_refused": len(plan.rejected),
    }

    cold = plan.cold_open
    comparison.cold_open = {
        "chosen": cold.chosen,
        "hook_type": cold.hook_type,
        "hook_id": cold.hook_id,
        "seconds": round(cold.duration, 2),
        "lifted_from": round(cold.original_start, 2) if cold.chosen else None,
        "viewer_question": cold.viewer_question,
        "payoff_at": cold.payoff_at,
        "original_removed": cold.original_removed,
        "duplicate_policy": cold.duplicate_policy,
        "fallback_reason": cold.fallback_reason,
        "candidates_refused": len(cold.rejected),
    }

    comparison.changes = [
        {
            "action": decision.action,
            "source": decision.source_type,
            "at": round(decision.episode_start, 1),
            "seconds": decision.source_seconds,
            "why": decision.reason[:300],
        }
        for decision in plan.accepted
        if decision.changes_footage
    ][:60]

    comparison.protected = [
        {
            "kind": "setup",
            "id": item.setup_id,
            "for_payoff": item.payoff_id,
            "at": round(item.episode_start, 1),
            "why": item.reason[:200],
        }
        for item in plan.setups if item.protected
    ] + [
        {
            "kind": "climax" if item.is_climax else "payoff",
            "id": item.payoff_id,
            "for_payoff": "",
            "at": round(item.episode_start, 1),
            "why": item.reason[:200],
        }
        for item in plan.payoffs if item.protected
    ]

    comparison.rejected = [
        {
            "action": decision.action,
            "source": decision.source_type,
            "at": round(decision.episode_start, 1),
            "code": decision.reject_code,
            "why": decision.rejected_reason[:300],
        }
        for decision in plan.rejected
    ][:60]

    comparison.unresolved = list(plan.unresolved_warnings)
    comparison.duplicated_footage = _duplicates(after)
    comparison.notes = _notes(plan, comparison)
    return comparison


def _duplicates(ranges: Sequence[SelectedRange]) -> list[dict]:
    """Footage that appears more than once in the finished cut.

    Checked on the *result* rather than trusted from the cold-open policy: a
    teaser removal that failed to reach some of the original would leave the
    opening playing twice, and the policy would still say it had been removed.
    """
    out: list[dict] = []
    ordered = sorted(ranges, key=lambda entry: (entry.asset_id, entry.start))
    for index, entry in enumerate(ordered):
        for other in ordered[index + 1:]:
            if other.asset_id != entry.asset_id:
                break
            overlap = max(
                0.0, min(entry.end, other.end) - max(entry.start, other.start))
            if overlap <= 0.5:
                continue
            out.append({
                "asset_id": entry.asset_id,
                "first": [round(entry.start, 1), round(entry.end, 1)],
                "second": [round(other.start, 1), round(other.end, 1)],
                "seconds": round(overlap, 1),
            })
    return out[:20]


def _notes(plan: RetentionCutPlan,
           comparison: RetentionCutComparison) -> list[str]:
    """What a person should take from this, in sentences that claim nothing.

    Every one of these is a statement about the *edit*. None of them is a
    statement about a viewer, and the phrasing is deliberate: "risk zones
    compressed" is a count, "more watchable" would be a guess.
    """
    out: list[str] = []
    difference = comparison.difference

    if not plan.applied:
        out.append(
            "Nothing was applied: this was a report-only pass. Every decision "
            "above is what it *would* do."
        )

    if difference["cut_seconds"] < 0:
        out.append(
            f"The cut is {abs(difference['cut_seconds']):.0f}s shorter: "
            f"{difference['seconds_removed']:.0f}s removed across "
            f"{difference['risk_zones_compressed']} risk zone(s) and "
            f"{difference['dead_air_stretches_trimmed']} stretch(es) of "
            "silence."
        )
    elif difference["cut_seconds"] > 0:
        out.append(
            f"The cut is {difference['cut_seconds']:.0f}s longer, which "
            "happens when a cold open adds footage the base cut had dropped."
        )

    if comparison.cold_open["chosen"]:
        question = comparison.cold_open["viewer_question"]
        out.append(
            f"It now opens on the {comparison.cold_open['hook_type']} from "
            f"{comparison.cold_open['lifted_from']:.0f}s"
            + (f", which asks: {question}" if question else "")
            + ("." if comparison.cold_open["original_removed"]
               else ", and the original is still in place.")
        )
    else:
        out.append(
            "No cold open was chosen, so the opening is unchanged. "
            + comparison.cold_open["fallback_reason"][:200]
        )

    if difference["setups_protected"]:
        out.append(
            f"{difference['setups_protected']} setup(s) were protected "
            "because the payoff they build to is in the cut."
        )
    if difference["actions_refused"]:
        out.append(
            f"{difference['actions_refused']} retention action(s) were "
            "refused by the rules. `retention show-rejected` says which rule "
            "refused each one."
        )
    if comparison.unresolved:
        out.append(
            f"{len(comparison.unresolved)} unresolved story warning(s): a "
            "payoff without its setup, or a setup that never pays off."
        )
    if comparison.duplicated_footage:
        out.append(
            f"{len(comparison.duplicated_footage)} stretch(es) of footage "
            "appear twice in the finished cut. Unless that is a deliberate "
            "teaser, it will read as a mistake."
        )

    out.append(NOT_MEASURED)
    return out


def _runtime(ranges: Sequence[SelectedRange]) -> float:
    return sum(
        entry.duration / (entry.speed if entry.speed > 0 else 1.0)
        for entry in ranges
    )


def _opens_on(ranges: Sequence[SelectedRange]) -> dict:
    if not ranges:
        return {}
    first = ranges[0]
    return {
        "keep_reason": first.keep_reason,
        "start": round(first.start, 1),
        "seconds": round(first.duration, 1),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(comparison: RetentionCutComparison) -> str:
    """The comparison, for the terminal."""
    rule = "=" * 78
    thin = "-" * 78
    before = comparison.before
    after = comparison.after
    difference = comparison.difference

    lines = [rule, f"RETENTION CUT vs {comparison.base.upper()} CUT", rule, ""]
    lines.append(f"  mode: {comparison.mode}")
    lines.append("")
    lines.append(f"  {'':<24}{'before':>12}{'after':>12}")
    for label, key in (
        ("ranges", "ranges"),
        ("runtime (s)", "cut_seconds"),
        ("source used (s)", "source_seconds"),
        ("protected", "protected"),
        ("sped up", "sped_up"),
    ):
        lines.append(f"  {label:<24}{before.get(key, 0):>12}"
                     f"{after.get(key, 0):>12}")
    lines.append("")

    lines.append(thin)
    lines.append("WHAT CHANGED")
    lines.append(thin)
    lines.append(f"  seconds removed          : "
                 f"{difference.get('seconds_removed', 0):.0f}")
    lines.append(f"  seconds sped up          : "
                 f"{difference.get('seconds_sped_up', 0):.0f}")
    lines.append(f"  risk zones compressed    : "
                 f"{difference.get('risk_zones_compressed', 0)}")
    lines.append(f"  risk zones marked only   : "
                 f"{difference.get('risk_zones_marked_only', 0)}")
    lines.append(f"  silence trimmed          : "
                 f"{difference.get('dead_air_stretches_trimmed', 0)}")
    lines.append(f"  silence kept on purpose  : "
                 f"{difference.get('dead_air_stretches_kept', 0)}")
    lines.append(f"  setups protected         : "
                 f"{difference.get('setups_protected', 0)}")
    lines.append(f"  payoffs protected        : "
                 f"{difference.get('payoffs_protected', 0)}")
    lines.append(f"  actions refused          : "
                 f"{difference.get('actions_refused', 0)}")
    lines.append("")

    cold = comparison.cold_open
    lines.append(thin)
    lines.append("COLD OPEN")
    lines.append(thin)
    if cold.get("chosen"):
        lines.append(f"  {cold['hook_type']} lifted from "
                     f"{cold['lifted_from']:.0f}s, {cold['seconds']:.0f}s long")
        if cold.get("viewer_question"):
            lines.append(f"  asks     : {cold['viewer_question'][:70]}")
        payoff = cold.get("payoff_at")
        lines.append(
            f"  answered : {payoff:.0f}s" if payoff is not None
            else "  answered : NEVER -- this opens a question the episode "
                 "does not close")
        lines.append(f"  original : "
                     f"{'removed' if cold['original_removed'] else cold['duplicate_policy']}")
    else:
        lines.append("  None chosen. The episode opens where it always did.")
        if cold.get("fallback_reason"):
            lines.append(f"  why : {cold['fallback_reason'][:120]}")
    if cold.get("candidates_refused"):
        lines.append(f"  {cold['candidates_refused']} candidate(s) were "
                     "refused; `retention show-cold-open` says why.")
    lines.append("")

    if comparison.unresolved:
        lines.append(thin)
        lines.append(f"UNRESOLVED ({len(comparison.unresolved)})")
        lines.append(thin)
        for warning in comparison.unresolved[:10]:
            lines.append(f"  ! {warning[:150]}")
        lines.append("")

    if comparison.duplicated_footage:
        lines.append(thin)
        lines.append(f"DUPLICATED FOOTAGE ({len(comparison.duplicated_footage)})")
        lines.append(thin)
        for entry in comparison.duplicated_footage[:8]:
            lines.append(f"  {entry['first'][0]:.0f}-{entry['first'][1]:.0f}s "
                         f"and {entry['second'][0]:.0f}-{entry['second'][1]:.0f}s "
                         f"overlap by {entry['seconds']:.0f}s")
        lines.append("")

    lines.append(thin)
    lines.append("WHAT THIS MEANS")
    lines.append(thin)
    for note in comparison.notes:
        for line in _wrap(note, 74):
            lines.append(f"  {line}")
        lines.append("")

    lines.append(thin)
    lines.append("HOW TO ACTUALLY TELL")
    lines.append(thin)
    lines.append("  Nothing above says the episode is better, because nothing")
    lines.append("  here can. Render both and watch them:")
    lines.append("")
    lines.append("    python -m editing.cli render roughcut")
    lines.append("    python -m editing.cli retention render --quality proxy")
    lines.append("")
    lines.append(rule)
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

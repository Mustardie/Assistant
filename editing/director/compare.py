"""Director cut against heuristic cut, side by side.

The question this answers is the only one that matters about this whole
session: **is the director actually doing anything?**

A director that agrees with ``usefulness >= 0.40`` on every range is an
expensive threshold. So the comparison measures disagreement rather than
quality -- because quality is a thing only a person watching both proxies can
judge, and this module refuses to pretend otherwise.

What it reports:

* how long each cut is, and how much source each uses
* how many ranges each keeps
* **agreement**: footage both cuts keep, as a share of footage either keeps
* what the director kept that the heuristic dropped, and the reason it gave
* what the director dropped that the heuristic kept, and the reason
* which decisions the director made that no threshold could have

The last one is the interesting column. A decision citing a setup/payoff link,
an open loop, a callback or the style guide is a decision that came from
reading the episode -- the heuristic has no access to any of those, so
whatever else is true, that decision is *new information*.

Nothing here says which cut is better. There is no metric for that and
inventing one would be the same mistake Session 8 refused to make about
retention.
"""
from __future__ import annotations

from typing import Optional, Sequence

from editing.director.schema import DirectorPlan
from editing.roughcut.select import SelectedRange

#: Reason categories a threshold could not have produced, because each rests
#: on something only the whole-episode view carries.
STORY_REASONS = frozenset({
    "setup_payoff", "callback", "viewer_curiosity", "objective_clarity",
    "hook_strength", "comedy_timing", "confusion_risk", "climax", "ending",
    "style_guide",
})


def compare(
    plan: DirectorPlan,
    heuristic: Sequence[SelectedRange],
    *,
    name: str = "structure",
) -> dict:
    """Everything measurable about the difference between two cuts."""
    director = plan.ranges
    director_seconds = sum(item.cut_duration for item in director)
    heuristic_seconds = sum(
        entry.duration / (entry.speed if entry.speed > 0 else 1.0)
        for entry in heuristic
    )

    shared, only_director, only_heuristic = _spans(director, heuristic)
    union = shared + only_director + only_heuristic

    added = _added(plan, heuristic)
    removed = _removed(plan, heuristic)
    story = [
        decision for decision in plan.accepted
        if decision.reason.category in STORY_REASONS
    ]

    return {
        "name": name,
        "not_measured": plan.not_measured,
        "director": {
            "ranges": len(director),
            "cut_seconds": round(director_seconds, 2),
            "source_seconds": round(
                sum(item.duration for item in director), 2),
            "protected": sum(1 for item in director if item.protected),
            "sped_up": sum(1 for item in director if item.speed != 1.0),
            "mock": plan.mock,
            "model": plan.model,
        },
        "heuristic": {
            "ranges": len(heuristic),
            "cut_seconds": round(heuristic_seconds, 2),
            "source_seconds": round(
                sum(entry.duration for entry in heuristic), 2),
            "protected": sum(1 for entry in heuristic if entry.protected),
            "sped_up": sum(1 for entry in heuristic if entry.speed != 1.0),
        },
        "difference": {
            "cut_seconds": round(director_seconds - heuristic_seconds, 2),
            "ranges": len(director) - len(heuristic),
            # Shared footage over footage either cut uses. 1.0 means the
            # director reproduced the threshold exactly.
            "agreement": round(shared / union, 3) if union else 0.0,
            "shared_seconds": round(shared, 2),
            "director_only_seconds": round(only_director, 2),
            "heuristic_only_seconds": round(only_heuristic, 2),
        },
        "director_kept_that_heuristic_dropped": added[:40],
        "director_dropped_that_heuristic_kept": removed[:40],
        "decisions_no_threshold_could_make": [
            {
                "decision_id": decision.decision_id,
                "action": decision.action,
                "start": round(decision.start, 1),
                "end": round(decision.end, 1),
                "category": decision.reason.category,
                "why": decision.reason.text[:300],
                "style_rule": decision.reason.style_rule[:200],
            }
            for decision in story[:40]
        ],
        "story_decision_count": len(story),
        "safety": {
            "proposed": plan.safety.proposed,
            "accepted": plan.safety.accepted,
            "rejected": plan.safety.rejected,
            "modified": plan.safety.modified,
            "by_check": plan.safety.by_check(),
        },
    }


def _spans(director, heuristic) -> tuple:
    """``(shared, director-only, heuristic-only)`` seconds of source footage.

    Measured on *source* footage rather than cut runtime: two cuts that use
    the same twenty seconds at different speeds agree about what is worth
    showing, which is the thing being compared.
    """
    by_asset: dict = {}
    for item in director:
        by_asset.setdefault(item.asset_id, [[], []])[0].append(
            (item.start, item.end))
    for entry in heuristic:
        by_asset.setdefault(entry.asset_id, [[], []])[1].append(
            (entry.start, entry.end))

    shared = director_only = heuristic_only = 0.0
    for left, right in by_asset.values():
        left_merged = _merge(left)
        right_merged = _merge(right)
        both = _intersection(left_merged, right_merged)
        shared += both
        director_only += _total(left_merged) - both
        heuristic_only += _total(right_merged) - both
    return shared, max(0.0, director_only), max(0.0, heuristic_only)


def _merge(spans: list) -> list:
    if not spans:
        return []
    ordered = sorted(spans)
    out = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def _total(spans: list) -> float:
    return sum(max(0.0, end - start) for start, end in spans)


def _intersection(left: list, right: list) -> float:
    total = 0.0
    for l_start, l_end in left:
        for r_start, r_end in right:
            total += max(0.0, min(l_end, r_end) - max(l_start, r_start))
    return total


def _added(plan: DirectorPlan, heuristic) -> list:
    """Footage the director kept that the heuristic would have dropped."""
    out = []
    for decision in plan.accepted:
        if not decision.keeps_footage:
            continue
        covered = _covered(decision.asset_id, decision.out_start,
                           decision.out_end, heuristic)
        if covered >= decision.out_duration * 0.5:
            continue
        out.append({
            "decision_id": decision.decision_id,
            "action": decision.action,
            "start": round(decision.out_start, 1),
            "end": round(decision.out_end, 1),
            "seconds": round(decision.out_duration - covered, 1),
            "category": decision.reason.category,
            "why": decision.reason.text[:300],
        })
    return out


def _removed(plan: DirectorPlan, heuristic) -> list:
    """Footage the heuristic kept that the director cut."""
    cuts = [
        decision for decision in plan.accepted
        if decision.action == "cut"
    ]
    out = []
    for entry in heuristic:
        for decision in cuts:
            if decision.asset_id != entry.asset_id:
                continue
            overlap = max(0.0, min(entry.end, decision.end)
                          - max(entry.start, decision.start))
            if overlap < entry.duration * 0.5:
                continue
            out.append({
                "decision_id": decision.decision_id,
                "start": round(entry.start, 1),
                "end": round(entry.end, 1),
                "seconds": round(overlap, 1),
                "heuristic_reason": entry.keep_reason,
                "category": decision.reason.category,
                "why": decision.reason.text[:300],
            })
            break
    return out


def _covered(asset_id: str, start: float, end: float, ranges) -> float:
    total = 0.0
    for entry in ranges:
        if entry.asset_id != asset_id:
            continue
        total += max(0.0, min(end, entry.end) - max(start, entry.start))
    return total


def render(payload: dict) -> str:
    """The comparison, for the terminal."""
    rule = "=" * 78
    thin = "-" * 78
    director = payload.get("director", {})
    heuristic = payload.get("heuristic", {})
    difference = payload.get("difference", {})

    lines = [rule, "DIRECTOR vs HEURISTIC", rule, ""]
    lines.append(f"  {'':<22}{'director':>14}{'heuristic':>14}")
    for label, key in (
        ("ranges kept", "ranges"),
        ("cut runtime (s)", "cut_seconds"),
        ("source used (s)", "source_seconds"),
        ("protected", "protected"),
        ("sped up", "sped_up"),
    ):
        lines.append(f"  {label:<22}{director.get(key, 0):>14}"
                     f"{heuristic.get(key, 0):>14}")
    lines.append("")
    lines.append(f"  agreement on footage : "
                 f"{difference.get('agreement', 0):.0%}")
    lines.append(f"  only in the director cut : "
                 f"{difference.get('director_only_seconds', 0):.0f}s")
    lines.append(f"  only in the heuristic cut: "
                 f"{difference.get('heuristic_only_seconds', 0):.0f}s")
    if director.get("mock"):
        lines.append("")
        lines.append("  ! The director cut is a MOCK. It applies four fixed "
                     "rules and is")
        lines.append("    not an editorial judgement; the agreement figure "
                     "above measures")
        lines.append("    two rule sets against each other.")
    lines.append("")

    story = payload.get("decisions_no_threshold_could_make") or []
    lines.append(thin)
    lines.append(f"DECISIONS NO THRESHOLD COULD MAKE ({len(story)})")
    lines.append(thin)
    if not story:
        lines.append("  None. Every decision rests on something the "
                     "rule-based pass can already see,")
        lines.append("  which means the director is agreeing with it rather "
                     "than adding to it.")
    for entry in story[:12]:
        lines.append(f"  {entry['start']:>7.0f}-{entry['end']:<7.0f} "
                     f"{entry['action']:<12} {entry['category']}")
        lines.append(f"          {entry['why'][:100]}")
        if entry.get("style_rule"):
            lines.append(f"          style: \"{entry['style_rule'][:80]}\"")
    lines.append("")

    added = payload.get("director_kept_that_heuristic_dropped") or []
    lines.append(thin)
    lines.append(f"KEPT, WHERE THE RULES WOULD HAVE DROPPED ({len(added)})")
    lines.append(thin)
    for entry in added[:10]:
        lines.append(f"  {entry['start']:>7.0f}-{entry['end']:<7.0f} "
                     f"{entry['seconds']:>5.0f}s  {entry['category']}: "
                     f"{entry['why'][:70]}")
    if not added:
        lines.append("  Nothing.")
    lines.append("")

    removed = payload.get("director_dropped_that_heuristic_kept") or []
    lines.append(thin)
    lines.append(f"CUT, WHERE THE RULES WOULD HAVE KEPT ({len(removed)})")
    lines.append(thin)
    for entry in removed[:10]:
        lines.append(f"  {entry['start']:>7.0f}-{entry['end']:<7.0f} "
                     f"{entry['seconds']:>5.0f}s  was '{entry['heuristic_reason']}'"
                     f" -- {entry['why'][:60]}")
    if not removed:
        lines.append("  Nothing.")
    lines.append("")

    lines.append(thin)
    lines.append("HOW TO ACTUALLY TELL")
    lines.append(thin)
    lines.append("  Nothing above says which cut is better, because nothing "
                 "here can. Render")
    lines.append("  both and watch them:")
    lines.append("")
    lines.append("    python -m editing.cli roughcut build --mode heuristic")
    lines.append("    python -m editing.cli render roughcut --name structure")
    lines.append("    python -m editing.cli director render")
    lines.append("")
    lines.append(f"  {payload.get('not_measured', '')}")
    lines.append("")
    lines.append(rule)
    return "\n".join(lines)

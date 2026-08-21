"""Human-readable output for the asset pass.

Organised around the one question a user actually has: **what did it not do,
and what would fix that?** For most libraries most of a plan is markers, and a
report that buries that under a list of successes is a report that gets read
once.

So the order is: what is missing (go and find these), what was refused (the
asset was right, the moment was wrong), what could not qualify (tag these
better), and only then what was placed.

``show-missing`` is deliberately a *shopping list* grouped by kind rather than a
list of moments, because "you have no whooshes" is one errand and "you have no
whoosh at 41.2s, none at 88.0s and none at 132.4s" is the same errand written
three times.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from editing.assets.match import REQUIREMENTS, coverage
from editing.assets.schema import AssetLibrary, AssetPlacementPlan

_RULE = "=" * 78
_THIN = "-" * 78


def render(
    plan: AssetPlacementPlan,
    *,
    library: Optional[AssetLibrary] = None,
    limit: int = 30,
) -> str:
    lines: list[str] = []
    add = lines.append
    stats = plan.stats()

    add(_RULE)
    add(f"ASSET PLACEMENT -- {plan.sequence_name or 'rough cut'}")
    add(_RULE)
    add(f"style     : {plan.style}")
    add(f"generated : {plan.generated_at}")
    add(f"library   : {plan.library_root or '(none)'}")
    library_stats = plan.library_stats or {}
    if library_stats:
        add(f"            {library_stats.get('total', 0)} asset(s), "
            f"{library_stats.get('usable', 0)} usable")
    add(f"tracks    : " + ", ".join(
        f"{role}={name}" for role, name in sorted(plan.tracks.items())
    ) or "(none)")
    add("")

    add(_THIN)
    add("OUTCOME")
    add(_THIN)
    add(f"  placeholders : {stats['placeholders']}")
    add(f"  placed       : {stats['placed']}  "
        f"({stats['distinct_assets']} distinct asset(s))")
    add(f"  missing      : {stats['missing']}   -- nothing of that kind exists")
    add(f"  rejected     : {stats['rejected']}   -- candidates, none qualified")
    add(f"  unsafe       : {stats['unsafe']}   -- good match, wrong moment")
    add(f"  marker only  : {stats['marker_only']}")
    add("")
    for warning in plan.warnings:
        add(f"  ! {warning}")
    if plan.warnings:
        add("")

    for heading, entries, note in (
        ("MISSING -- nothing in the library could fill these",
         plan.missing(),
         "Each line is something to go and find."),
        ("REFUSED BY A SAFETY RULE -- the asset was right, the moment was not",
         plan.unsafe(), ""),
        ("NO CANDIDATE QUALIFIED -- tag these better, or lower --min-score",
         plan.rejected(), ""),
        ("RECORDED ONLY", plan.marker_only(), ""),
    ):
        if not entries:
            continue
        add(_THIN)
        add(f"{heading} ({len(entries)})")
        add(_THIN)
        if note:
            add(f"  {note}")
        for placement in entries[:limit]:
            add(f"  [{placement.start:8.2f}s] {placement.kind:<16}")
            add(f"      why : {placement.reason[:160]}")
            best = placement.best
            if best is not None and best.filename:
                add(f"      near: {best.filename} ({best.score:.2f}) "
                    f"{best.rejected[:80]}")
        if len(entries) > limit:
            add(f"  ... and {len(entries) - limit} more.")
        add("")

    placed = plan.placed()
    add(_THIN)
    add(f"PLACED ({len(placed)})")
    add(_THIN)
    if not placed:
        add("  Nothing. Every placeholder is a marker.")
    for placement in placed[:limit]:
        payload = placement.payload
        detail = []
        if payload.get("gain_db") is not None:
            detail.append(f"{payload['gain_db']:g} dB")
        if payload.get("loops", 1) > 1:
            detail.append(f"{payload['loops']}x loop")
        if payload.get("ducked_under"):
            detail.append(f"ducked under {payload['ducked_under']} line(s)")
        if payload.get("zone"):
            detail.append(str(payload["zone"]).replace("_", " "))
        add(f"  [{placement.start:8.2f}s] {placement.kind:<16} "
            f"{placement.asset_filename[:34]:<34} {placement.track}")
        add(f"      {'; '.join(detail) if detail else ''}".rstrip())
        if placement.notes:
            add(f"      note: {placement.notes[:150]}")
    if len(placed) > limit:
        add(f"  ... and {len(placed) - limit} more.")
    add("")

    add(_THIN)
    add("OPERATION PLAN")
    add(_THIN)
    add(f"  operations : {plan.operation_count}")
    for name, count in sorted(stats["by_operation"].items()):
        add(f"      {name:<20} {count}")
    add(f"  dry run    : "
        f"{'passed' if plan.dry_run_passed else 'not run / FAILED'}")
    add(f"  executed   : {plan.executed}")
    add(f"  on scratch : {plan.on_scratch}")
    if plan.dry_run_error:
        add(f"  error      : {plan.dry_run_error.get('error')}")
        if plan.dry_run_error.get("hint"):
            add(f"  hint       : {plan.dry_run_error['hint']}")
    if plan.explanation:
        add("")
        add("  What it would do:")
        for line in plan.explanation[:limit]:
            add(f"    {line}")
    add("")

    add(_RULE)
    add("Nothing here has been applied. Assets are placed on tracks this plan "
        "adds, never")
    add("on V1 or A1, so the rough cut underneath is untouched and the pass "
        "can be undone")
    add("by deleting those tracks and the markers.")
    add(_RULE)
    return "\n".join(lines)


def render_missing(plan: AssetPlacementPlan, *, limit: int = 40) -> str:
    """A shopping list, grouped by what to go and find.

    Grouped by kind rather than listed by moment on purpose: "you have no
    whooshes" is one errand, and printing it once per placeholder turns a
    two-line answer into a page.
    """
    missing = plan.missing()
    lines = [
        f"{len(missing)} placeholder(s) in '{plan.sequence_name}' had no "
        f"candidate asset:",
    ]
    if not missing:
        lines.append("  Nothing missing. Every placeholder found a candidate.")
        return "\n".join(lines)

    grouped: dict = {}
    for placement in missing:
        grouped.setdefault(placement.kind, []).append(placement)

    lines.append("")
    for kind, entries in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        requirement = REQUIREMENTS.get(kind)
        want = requirement.label if requirement else kind
        folders = ", ".join(
            f"assets/{name}/" for name in (
                requirement.categories if requirement else ()
            )
        )
        times = ", ".join(f"{p.start:.1f}s" for p in entries[:6])
        more = f" (+{len(entries) - 6} more)" if len(entries) > 6 else ""
        lines.append(f"  {kind}  x{len(entries)}")
        lines.append(f"      wanted : {want}")
        if folders:
            lines.append(f"      put in : {folders}")
        if requirement is not None and requirement.tags:
            lines.append(
                f"      tag as : {', '.join(requirement.tags[:6])}"
            )
        if requirement is not None and requirement.needs_loop:
            lines.append("      must be loopable (name it *_loop, or set "
                         "loopable in a sidecar)")
        lines.append(f"      needed : {times}{more}")
        lines.append("")
    return "\n".join(lines)


def render_deferred(plan: AssetPlacementPlan, *, limit: int = 60) -> str:
    """Everything that placed nothing, with the rule that stopped it."""
    held = plan.deferred()
    lines = [
        f"{len(held)} placeholder(s) in '{plan.sequence_name}' placed nothing:",
    ]
    if not held:
        lines.append("  Nothing. Every placeholder was filled.")
        return "\n".join(lines)

    by_status: dict = {}
    for placement in held:
        by_status[placement.status] = by_status.get(placement.status, 0) + 1
    lines.append("  by outcome: " + ", ".join(
        f"{status} x{count}"
        for status, count in sorted(by_status.items(), key=lambda kv: -kv[1])
    ))
    by_risk: dict = {}
    for placement in held:
        for risk in placement.risks:
            by_risk[risk] = by_risk.get(risk, 0) + 1
    if by_risk:
        lines.append("  by reason : " + ", ".join(
            f"{risk} x{count}"
            for risk, count in sorted(by_risk.items(), key=lambda kv: -kv[1])
        ))
    lines.append("")

    for placement in sorted(held, key=lambda p: p.start)[:limit]:
        lines.append(
            f"  [{placement.start:8.2f}s] {placement.kind:<16} "
            f"{placement.status}"
        )
        lines.append(f"      {placement.reason[:160]}")
        best = placement.best
        if best is not None and best.filename and best.rejected:
            lines.append(
                f"      closest: {best.filename} -- {best.rejected[:120]}"
            )
    if len(held) > limit:
        lines.append(f"  ... and {len(held) - limit} more.")
    return "\n".join(lines)


def render_library(library: AssetLibrary, *, style: str = "",
                   limit: int = 60) -> str:
    """What the library holds, and which placeholder kinds it can serve."""
    stats = library.stats()
    lines = [
        f"Asset library at {library.root or '(not set)'}",
        f"  {stats['total']} file(s): {stats['usable']} usable, "
        f"{stats['needs_review']} needing review, {stats['missing']} missing",
        f"  {stats['with_sidecar']} with a sidecar, "
        f"{stats['with_duration']} with a known duration",
        "",
    ]
    if stats["by_category"]:
        lines.append("  by category:")
        for name, count in sorted(stats["by_category"].items()):
            lines.append(f"    {name:<12} {count}")
        lines.append("")

    lines.append("  what each placeholder kind can draw on"
                 + (f" in {style}" if style else "") + ":")
    for kind, entry in coverage(library, style=style).items():
        mark = " " if entry["candidates"] else "!"
        lines.append(
            f"   {mark} {kind:<16} {entry['candidates']:>3} candidate(s), "
            f"{entry['well_tagged']:>3} well tagged   {entry['label']}"
        )
    lines.append("")

    for warning in library.warnings[:limit]:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)


def render_validation(library: AssetLibrary, *, limit: int = 60) -> str:
    """Everything wrong with the library, and what to do about it."""
    problems = library.needing_review() + library.missing()
    lines = [f"Validating {len(library)} asset(s) in {library.root}:", ""]

    if not problems and not library.warnings and not library.skipped:
        lines.append("  No problems found.")
        return "\n".join(lines)

    if library.needing_review():
        lines.append(f"  NEEDS REVIEW ({len(library.needing_review())}) "
                     "-- indexed, never placed automatically")
        for item in library.needing_review()[:limit]:
            lines.append(f"    {item.filename}")
            lines.append(f"      {item.review_reason[:160]}")
        lines.append("")

    if library.missing():
        lines.append(f"  MISSING FROM DISK ({len(library.missing())}) "
                     "-- indexed before, gone now")
        for item in library.missing()[:limit]:
            lines.append(f"    {item.filename}  ({item.path})")
        lines.append("")

    unsafe = [
        item for item in library.items
        if not item.safe_for_auto and not item.needs_review
    ]
    if unsafe:
        lines.append(f"  HELD BACK BY CHOICE ({len(unsafe)}) "
                     "-- safe_for_auto is false")
        for item in unsafe[:limit]:
            lines.append(f"    {item.filename}")
        lines.append("")

    if library.skipped:
        lines.append(f"  SKIPPED ({len(library.skipped)}) "
                     "-- not a supported asset type")
        for entry in library.skipped[:limit]:
            lines.append(f"    {Path(entry.get('path', '')).name}: "
                         f"{entry.get('reason', '')}")
        lines.append("")

    for warning in library.warnings[:limit]:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)


def render_asset(library: AssetLibrary, item) -> str:
    """Everything known about one asset, including where its tags came from."""
    lines = [
        f"{item.filename}",
        f"  id         : {item.asset_id}",
        f"  path       : {item.path}",
        f"  media      : {item.media_type}",
        f"  category   : {item.category}",
        f"  duration   : "
        + (f"{item.duration:.3f}s" if item.duration is not None else "unknown"),
    ]
    if item.effective_duration is not None and item.effective_duration != item.duration:
        lines.append(f"  after trim : {item.effective_duration:.3f}s")
    lines.extend([
        f"  intensity  : {item.intensity}",
        f"  loopable   : {item.loopable}",
        f"  usable     : {item.usable}",
    ])
    if item.bpm is not None:
        lines.append(f"  bpm        : {item.bpm:g}")
    if item.volume_adjust_db is not None:
        lines.append(f"  volume     : {item.volume_adjust_db:+g} dB")
    if item.preferred_styles:
        lines.append(f"  prefers    : {', '.join(item.preferred_styles)}")
    if item.avoid_styles:
        lines.append(f"  avoids     : {', '.join(item.avoid_styles)}")
    if item.license_notes:
        lines.append(f"  licence    : {item.license_notes[:160]}")
    if item.needs_review:
        lines.append(f"  REVIEW     : {item.review_reason[:200]}")
    if item.missing:
        lines.append("  MISSING    : the file is not on disk")

    lines.append("")
    lines.append("  tags (and where they came from):")
    for tag in sorted(item.tags, key=lambda t: (-t.confidence, t.name)):
        lines.append(f"    {tag.name:<20} {tag.source:<9} {tag.confidence:.2f}")
    if not item.tags:
        lines.append("    (none)")

    lines.append("")
    lines.append("  placeholder kinds this could serve:")
    served = [
        kind for kind, requirement in REQUIREMENTS.items()
        if item.category in requirement.categories
        and item.media_type in requirement.media
        and not (requirement.needs_loop and not item.loopable)
    ]
    lines.append("    " + (", ".join(served) if served else "(none)"))
    return "\n".join(lines)


def write(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target

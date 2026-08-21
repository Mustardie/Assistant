"""The human-readable layered-edit report.

Organised by **layer**, because that is how a person decides what to keep: "the
captions are too dense" and "the punch-ins are wrong" are separate judgements
with separate fixes, and a flat list of 90 operations supports neither.

The density block comes first. It is the one number that answers the question
this session exists to address — *does this feel intentionally styled, or
randomly over-edited?* — and it is stated against the style's own ceilings so
the reader can see how much headroom was left rather than just an absolute
figure.

Deferred items come before planned ones inside each layer, for the same reason
the critic report leads with what it could not fix: what the system declined to
do is where the disagreements are, and a reader who stops halfway should have
seen it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from editing.style.presets import StylePreset
from editing.style.schema import LAYERS, LayerItem, LayeredEditPlan

_RULE = "=" * 78
_THIN = "-" * 78


def render(
    plan: LayeredEditPlan,
    *,
    style: Optional[StylePreset] = None,
    limit: int = 30,
) -> str:
    lines: list[str] = []
    add = lines.append
    density = plan.density()
    stats = plan.stats()
    preset = plan.preset or (style.to_dict() if style else {})

    add(_RULE)
    add(f"LAYERED EDIT -- {plan.sequence_name or 'rough cut'}")
    add(_RULE)
    add(f"style     : {plan.style}"
        + (f"  ({preset.get('label')})" if preset.get("label") else ""))
    add(f"generated : {plan.generated_at}")
    add(f"runtime   : {density['cut_duration']:.1f}s "
        f"({density['minutes']:.1f} min)")
    add("")

    # -- density, first ---------------------------------------------------
    add(_THIN)
    add("DENSITY")
    add(_THIN)
    for label, actual, ceiling in (
        ("active edits", density["edits_per_minute"],
         preset.get("max_edits_per_minute")),
        ("captions", density["captions_per_minute"],
         preset.get("max_captions_per_minute")),
        ("zooms", density["zooms_per_minute"],
         preset.get("max_zooms_per_minute")),
    ):
        room = ""
        if isinstance(ceiling, (int, float)) and ceiling:
            room = f"  ({actual / ceiling:.0%} of the ceiling)"
        elif ceiling == 0:
            room = "  (this style allows none)"
        add(f"  {label:<14} {actual:>6.2f} / min"
            + (f"   ceiling {ceiling:g}" if ceiling is not None else "")
            + room)
    add(f"  {'markers':<14} {density['markers_per_minute']:>6.2f} / min"
        "   (annotations are not capped)")
    add("")
    add(f"  planned {stats['planned']}, deferred {stats['deferred']}, "
        f"rejected {stats['rejected']}")
    add(f"  {stats['convertible']} item(s) become operations, "
        f"{stats['marker_only']} of them markers")
    add("")

    for warning in plan.warnings:
        add(f"  ! {warning}")
    if plan.warnings:
        add("")

    # -- per layer ---------------------------------------------------------
    for name in LAYERS:
        items = plan.layer(name)
        if not items:
            continue
        planned = [item for item in items if item.status == "planned"]
        held = [item for item in items if item.status != "planned"]

        add(_THIN)
        add(f"{name.upper()} LAYER -- {len(planned)} planned, {len(held)} held back")
        add(_THIN)

        for item in sorted(held, key=lambda i: i.start)[:limit]:
            add(f"  ? [{item.start:8.2f}s] {item.kind:<18} {item.priority:.2f}")
            add(f"      wanted : {item.reason[:150]}")
            add(f"      held   : {item.status_reason[:150]}")
        if len(held) > limit:
            add(f"  ... and {len(held) - limit} more held back.")
        if held and planned:
            add("")

        for item in sorted(planned, key=lambda i: i.start)[:limit]:
            add(f"  + [{item.start:8.2f}s] {item.kind:<18} "
                f"{_detail(item)[:44]}")
            add(f"      why    : {item.reason[:150]}")
            if item.premiere_ops:
                add("      ops    : " + ", ".join(
                    str(op.get("op")) for op in item.premiere_ops
                ) + ("   (marker only)" if item.is_marker_only else ""))
            if item.notes:
                add(f"      note   : {item.notes[:150]}")
        if len(planned) > limit:
            add(f"  ... and {len(planned) - limit} more planned.")
        add("")

    # -- the plan -----------------------------------------------------------
    add(_THIN)
    add("OPERATION PLAN")
    add(_THIN)
    add(f"  sequence   : {plan.sequence_name}")
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
    add("Nothing in this report has been applied. This pass adds only: it "
        "cannot trim,")
    add("retime or remove a clip, so the rough cut's layout is untouched "
        "underneath it.")
    add(_RULE)
    return "\n".join(lines)


def render_density(plan: LayeredEditPlan) -> str:
    """The short form: is this over-edited, and where?

    What ``layers show-density`` prints. Per-minute buckets rather than a
    single average, because an average hides the case this is meant to catch --
    a calm episode with one frantic minute in the middle.
    """
    density = plan.density()
    preset = plan.preset or {}
    lines = [
        f"'{plan.sequence_name}' in {plan.style} -- "
        f"{density['cut_duration']:.1f}s, {density['planned']} planned item(s)",
        f"  active edits : {density['edits_per_minute']:.2f}/min "
        f"(ceiling {preset.get('max_edits_per_minute', '?')})",
        f"  captions     : {density['captions_per_minute']:.2f}/min "
        f"(ceiling {preset.get('max_captions_per_minute', '?')})",
        f"  zooms        : {density['zooms_per_minute']:.2f}/min "
        f"(ceiling {preset.get('max_zooms_per_minute', '?')})",
        f"  markers      : {density['markers_per_minute']:.2f}/min",
        "",
    ]

    buckets = _per_minute_buckets(plan)
    if buckets:
        lines.append("  minute   active  captions  zooms  markers")
        for minute, counts in buckets:
            bar = "#" * min(30, counts["active"])
            lines.append(
                f"  {minute:>5}   {counts['active']:>6}  "
                f"{counts['captions']:>8}  {counts['zooms']:>5}  "
                f"{counts['markers']:>7}  {bar}"
            )
        lines.append("")

    lines.append("  by layer:")
    for layer, count in sorted(density["by_layer"].items()):
        lines.append(f"    {layer:<10} {count}")
    return "\n".join(lines)


def _per_minute_buckets(plan: LayeredEditPlan) -> list[tuple]:
    """Planned items grouped into whole minutes of the cut."""
    if plan.cut_duration <= 0:
        return []
    total = int(plan.cut_duration // 60) + 1
    buckets = [
        {"active": 0, "captions": 0, "zooms": 0, "markers": 0}
        for _ in range(total)
    ]
    for item in plan.planned():
        index = min(total - 1, int(item.start // 60))
        if item.is_active:
            buckets[index]["active"] += 1
        if item.is_caption:
            buckets[index]["captions"] += 1
        if item.is_zoom:
            buckets[index]["zooms"] += 1
        if item.is_marker_only:
            buckets[index]["markers"] += 1
    return list(enumerate(buckets))


def render_deferred(plan: LayeredEditPlan, *, limit: int = 60) -> str:
    """Everything the style held back, worst-first, with the reason.

    The view for deciding whether the style is too tight. Every line names the
    ceiling that stopped it, so the fix is always visible from the output.
    """
    held = sorted(
        plan.deferred() + plan.rejected(),
        key=lambda item: (-item.priority, item.start),
    )
    lines = [
        f"{len(held)} item(s) held back by the {plan.style} style "
        f"in '{plan.sequence_name}':",
    ]
    if not held:
        lines.append("  Nothing. Every candidate fitted inside this style.")
        return "\n".join(lines)

    by_risk: dict = {}
    for item in held:
        key = item.risks[0] if item.risks else "unspecified"
        by_risk[key] = by_risk.get(key, 0) + 1
    lines.append("  by reason: " + ", ".join(
        f"{risk} x{count}"
        for risk, count in sorted(by_risk.items(), key=lambda kv: -kv[1])
    ))
    lines.append("")

    for item in held[:limit]:
        lines.append(
            f"  [{item.start:8.2f}s] {item.layer:<9} {item.kind:<18} "
            f"p={item.priority:.2f}"
        )
        lines.append(f"      wanted: {item.reason[:150]}")
        lines.append(f"      held  : {item.status_reason[:150]}")
    if len(held) > limit:
        lines.append(f"  ... and {len(held) - limit} more.")
    return "\n".join(lines)


def _detail(item: LayerItem) -> str:
    payload = item.payload or {}
    if payload.get("text"):
        return f'"{payload["text"]}"'
    if payload.get("scale"):
        return f"-> {payload['scale']:g}%"
    if payload.get("placeholder"):
        return str(payload["placeholder"])
    return ""


def write(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target

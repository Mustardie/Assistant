"""The human-readable conform report.

Written for one question: *did the decisions become anything?* Every other
report in this system describes intent. This one has to make the difference
between intent and result impossible to miss, so it is organised around
counting: how many captions were decided, how many became operations, how many
of those Premiere accepted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from editing.conform.schema import ConformPlan, DeliveryResult

RULE = "=" * 74
THIN = "-" * 74


def render(plan: ConformPlan, *, report=None, delivery=None,
           limit: int = 40) -> str:
    """The whole pass, as text."""
    lines: list[str] = [
        RULE,
        f"CONFORM  {plan.name}  ->  {plan.sequence_name or '(no sequence)'}",
        RULE,
        f"mode              {plan.config.mode}",
        f"cut duration      {plan.cut_duration:.1f}s",
        f"operations        {plan.operation_count}",
        f"dry run           {'passed' if plan.dry_run_passed else 'FAILED'}",
    ]
    if plan.dry_run_error:
        lines.append(f"                  ! {plan.dry_run_error.get('error')}")

    lines += ["", "TRACK LAYOUT", THIN]
    lines += [f"  {entry}" for entry in plan.layout.describe()]

    lines += ["", "WHAT EACH LAYER CONTRIBUTED", THIN]
    if not plan.contributions:
        lines.append("  nothing: every layer was disabled or empty.")
    for layer, count in sorted(plan.contributions.items()):
        lines.append(f"  {layer:<14} {count:>4} operation(s)")

    lines += ["", "COLOUR", THIN, "  " + plan.color.line()]
    for note in plan.color.evidence[:4]:
        lines.append(f"    - {note}")
    if plan.color.measured:
        measured = plan.color.measured
        lines.append(
            f"    measured luma {measured.get('luma', 0):.0f}/255, "
            f"chroma {measured.get('chroma', 0):.0f} "
            f"over {measured.get('clips', 0)} clip(s)"
        )

    lines += ["", "MUSIC", THIN, "  " + plan.music.line()]
    for note in plan.music.evidence[:4]:
        lines.append(f"    - {note}")

    lines += ["", "MIX", THIN]
    lines.append(
        f"  target {plan.mix.target_lufs:.1f} LUFS, ceiling "
        f"{plan.mix.peak_ceiling_db:.1f} dBTP, "
        f"{'fully measured' if plan.mix.fully_measured else 'partly assumed'}"
    )
    for role, gain in sorted(plan.mix.gains.items()):
        measurement = plan.mix.measurement_for(role)
        detail = (f"measured {measurement.lufs:.1f} LUFS, "
                  f"peak {measurement.peak_db:.1f} dBTP"
                  if measurement else "not measured; default")
        lines.append(f"  {role:<10} {gain:+6.1f} dB   {detail}")
    for note in plan.mix.notes[:6]:
        lines.append(f"    - {note}")
    for warning in plan.mix.warnings[:6]:
        lines.append(f"    ! {warning}")

    applied = [t for t in plan.transitions if t.applied]
    lines += ["", f"TRANSITIONS  ({len(applied)} of "
                  f"{len(plan.transitions)} cuts)", THIN]
    for decision in plan.transitions[:limit]:
        lines.append("  " + decision.line())

    if plan.unconverted:
        lines += ["", f"NOT CONVERTED  ({len(plan.unconverted)})", THIN,
                  "  Decisions from earlier passes that could not become an",
                  "  operation. None of these were dropped silently.", ""]
        for entry in plan.unconverted[:limit]:
            lines.append(
                f"  {entry.get('at', 0.0):7.2f}  {entry.get('kind', '?'):<10} "
                f"{entry.get('reason', ''):<20} {str(entry.get('detail', ''))[:32]}"
            )

    lines += ["", "OPERATIONS BY TYPE", THIN]
    for name, count in plan.counts().items():
        lines.append(f"  {name:<22} {count:>4}")

    if report is not None:
        lines += ["", "EXECUTION", THIN]
        lines.append(f"  mode        {report.mode}")
        lines.append(f"  executed    {report.executed}")
        lines.append(
            f"  applied     {report.operations_succeeded} of "
            f"{report.operations_attempted}"
        )
        if report.refused_reason:
            lines.append(f"  refused     {report.refused_reason}")
        if report.error:
            lines.append(f"  error       {report.error.get('error')}")
            for failure in (report.error.get("detail") or {}).get("failed", [])[:12]:
                lines.append(
                    f"    - {failure.get('op')} #{failure.get('index')}: "
                    f"{str(failure.get('error'))[:60]}"
                )
        for warning in report.warnings[:6]:
            lines.append(f"  ! {warning}")

    if delivery is not None:
        lines += ["", "DELIVERY", THIN, "  " + delivery.line()]
        if delivery.duration:
            lines.append(f"  duration    {delivery.duration:.1f}s")
        if delivery.preset:
            lines.append(f"  preset      {Path(delivery.preset).name}")
        if delivery.error:
            lines.append(f"  error       {delivery.error.get('error')}")
            if delivery.error.get("hint"):
                lines.append(f"              {delivery.error['hint']}")
        for warning in delivery.warnings[:4]:
            lines.append(f"  ! {warning}")

    lines += ["", RULE, _verdict(plan, report, delivery), RULE, ""]
    return "\n".join(lines)


def _verdict(plan: ConformPlan, report, delivery) -> str:
    """One line saying how far this run actually got."""
    if delivery is not None and delivery.delivered:
        return (f"FINISHED  {delivery.output_path} "
                f"({delivery.size_bytes / 1_000_000:.1f} MB)")
    if report is not None and report.executed:
        return (f"EXECUTED  {report.operations_succeeded} operation(s) are on "
                f"'{plan.sequence_name}'. Not yet exported.")
    if report is not None and report.refused_reason:
        return f"REFUSED   {report.refused_reason[:60]}"
    if plan.dry_run_passed:
        return (f"VALIDATED {plan.operation_count} operation(s) are ready to "
                "run. Nothing has been executed.")
    return "BLOCKED   the plan did not validate; nothing can run."


def write(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target

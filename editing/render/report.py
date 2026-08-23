"""What one render produced, said plainly.

Two audiences, one object. ``report.md`` sits in the job folder for whoever
opens it later; ``render show`` prints the same facts in the terminal. Both
come from ``build_report``, so they cannot drift.

The report is organised around what actually goes wrong with a proxy:

1. is there a video, and is it real?
2. how long is it, and does that match what the cut said it would be?
3. **what did the cut ask for that this could not show?**
4. what do I type to watch it, or to do it again differently?

Point three carries the weight. A rough cut by Session 6 has captions,
markers, sound effects and music in it, and a viewer of the proxy is seeing
none of them. A report that listed only what was rendered would quietly
misrepresent the thing being judged.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from editing.render.notes import timecode
from editing.render.schema import RenderJob, RenderReport

_RULE = "=" * 78
_THIN = "-" * 78

#: Seconds of difference between the plan and the finished file worth telling
#: somebody about. Frame-rate conversion costs a few hundredths on every
#: render; the number that means something is around a second.
DRIFT_WORTH_MENTIONING = 0.5

#: True of every proxy this package makes, however well the render went.
#: Stated on each report because the person reading it may not have read the
#: README, and every one of these has surprised somebody.
LIMITATIONS = (
    "This is a proxy, not a delivery render. It is scaled down, encoded fast "
    "and meant to be watched once and thrown away.",
    "Only the V1 assembly is rendered. Captions, cards, sound effects, music "
    "and graphics are planned by other passes and are not in this video.",
    "Every cut is a hard cut. Transitions, fades and dissolves are not "
    "represented.",
    "Speed changes are applied with setpts and atempo, which is not how "
    "Premiere retimes -- expect the timing to be close, not identical.",
    "Markers are not in the video. The review notes carry the same "
    "information in a form you can read while watching.",
    "Nothing here has touched Premiere, and rendering does not execute "
    "anything.",
)


def build_report(job: RenderJob) -> RenderReport:
    """Everything worth saying about one render, gathered."""
    result = job.result
    report = RenderReport(
        job_id=job.job_id,
        status=job.status,
        output_path=job.output_path if (result and result.rendered) else "",
        notes_path=job.notes_path,
        rendered=bool(result and result.rendered),
        mock=bool(result and result.mock),
        from_cache=bool(result and result.from_cache),
        stats={**job.stats(), **(result.stats() if result else {})},
        config=job.config.to_dict(),
        segments=[segment.to_dict() for segment in job.segments[:400]],
        inputs=[item.to_dict() for item in job.inputs],
        unsupported=list(job.unsupported),
        warnings=list(job.warnings) + (list(result.warnings) if result else []),
        limitations=list(LIMITATIONS),
        failure=(job.failure.to_dict() if job.failure else
                 (result.failure.to_dict()
                  if result and result.failure else None)),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    report.next_commands = _next_commands(job)
    return report


def _next_commands(job: RenderJob) -> list[str]:
    """The shortest path forward from wherever this render got to."""
    out: list[str] = []
    rendered = bool(job.result and job.result.rendered)
    if rendered:
        out.append(f"python -m editing.cli render open {job.job_id}")
        out.append(f"python -m editing.cli render open {job.job_id} --notes")
    elif job.failure is not None and job.failure.hint:
        out.append(job.failure.hint)

    # The re-render command has to name the plan the way *this* job found it.
    # A job rendered from a file lives nowhere `--name` can reach, and
    # printing a command that silently renders a different cut is worse than
    # printing none.
    if job.plan_path and not _is_named_plan(job):
        source = f"from-plan \"{job.plan_path}\""
    else:
        source = f"roughcut --name {job.plan_name}"
    out.append(
        f"python -m editing.cli render {source} "
        f"--quality {job.config.quality} --height {job.config.height} --force"
    )
    out.append(f"python -m editing.cli render show {job.job_id}")
    if rendered:
        out.append("python -m editing.cli feedback start")
    return out


def _is_named_plan(job: RenderJob) -> bool:
    """True when this plan is one ``roughcut --name`` would find.

    The pipeline renders ``roughcut/<name>.json`` and records that path, so a
    plan whose file is named after the job's plan name is reachable that way;
    anything else came from somewhere ``--name`` cannot look.
    """
    return Path(job.plan_path).name == f"{job.plan_name}.json" \
        and Path(job.plan_path).parent.name == "roughcut"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_markdown(job: RenderJob, report: Optional[RenderReport] = None
                    ) -> str:
    """``report.md`` -- the one that lives in the job folder."""
    report = report or build_report(job)
    stats = report.stats
    lines: list[str] = []
    add = lines.append

    add(f"# Render {job.job_id}")
    add("")
    add(f"- Status: **{job.status}**")
    if report.mock:
        add("- **MOCK RENDER -- no video was produced.** The file below is a "
            "placeholder written by the mock runner. Nothing in it is "
            "watchable, and no frame of the cut has been encoded.")
    if report.from_cache:
        add("- Reused an earlier render: nothing changed in the cut, the "
            "sources or the settings.")
    if report.output_path:
        add(f"- Video: `{report.output_path}`")
    add(f"- Notes: `{report.notes_path}`")
    add(f"- Length: {timecode(stats.get('duration', 0))} "
        f"from {stats.get('source_duration', 0):.0f}s of source")
    add(f"- Clips: {stats.get('segments', 0)}"
        + (f", {stats.get('speed_changes', 0)} with a speed change"
           if stats.get("speed_changes") else ""))
    if stats.get("size_mb"):
        add(f"- Size: {stats['size_mb']} MB "
            f"({stats.get('resolution', '')} @ {stats.get('fps', 0)}fps)")
    if stats.get("elapsed"):
        add(f"- Took: {stats['elapsed']:.0f}s"
            + (f" ({stats['realtime_factor']}x realtime)"
               if stats.get("realtime_factor") else ""))
    add("")

    # Only when it is worth a person's attention. Frame-rate conversion puts a
    # few hundredths of a second on every render, and printing that every time
    # would train the reader to skip the line that matters.
    drift = stats.get("duration_drift", 0)
    if abs(drift) > DRIFT_WORTH_MENTIONING:
        add(f"> The finished file is {drift:+.2f}s away from what the cut "
            "predicted. A second or so is normal with speed changes and "
            "frame-rate conversion; more than that is worth understanding "
            "before trusting timings read off this proxy.")
        add("")

    if report.failure:
        add("## What went wrong")
        add("")
        add(f"**{report.failure.get('message', '')}**")
        add("")
        if report.failure.get("hint"):
            add(f"Fix: {report.failure['hint']}")
            add("")
        if report.failure.get("stderr"):
            add("```")
            add(str(report.failure["stderr"])[-1500:])
            add("```")
            add("")

    if report.unsupported:
        add("## What this render could not show")
        add("")
        add("The cut asks for these, and a flat proxy has nowhere to put "
            "them. They are still in the plan; they are just not in the "
            "video you are about to watch.")
        add("")
        for item in report.unsupported:
            add(f"- {item}")
        add("")

    if report.inputs:
        add("## Sources")
        add("")
        for item in report.inputs:
            mark = "" if item.get("usable") else "  **MISSING**"
            add(f"- `{Path(item['path']).name}` -- "
                f"{item.get('segments', 0)} clip(s)"
                + (f", {item['duration']:.0f}s" if item.get("duration") else "")
                + ("" if item.get("has_audio", True) else ", no audio track")
                + mark)
        add("")

    if report.warnings:
        add(f"## Warnings ({len(report.warnings)})")
        add("")
        for warning in report.warnings[:40]:
            add(f"- {warning}")
        if len(report.warnings) > 40:
            add(f"- ... and {len(report.warnings) - 40} more.")
        add("")

    add("## Next")
    add("")
    for command in report.next_commands:
        add(f"- `{command}`")
    add("")

    add("## Limitations")
    add("")
    for limitation in report.limitations:
        add(f"- {limitation}")
    add("")
    return "\n".join(lines) + "\n"


def render_text(job: RenderJob, report: Optional[RenderReport] = None) -> str:
    """The terminal view. Same facts, narrower."""
    report = report or build_report(job)
    stats = report.stats
    lines: list[str] = []
    add = lines.append

    add(_RULE)
    add(f"RENDER -- {job.job_id}")
    add(_RULE)
    add(f"status     : {job.status}"
        + ("   (reused an earlier render)" if report.from_cache else ""))
    add(f"cut        : {job.sequence_name or job.plan_name}")
    add(f"video      : {report.output_path or '(none produced)'}")
    add(f"notes      : {report.notes_path}")
    add(f"length     : {timecode(stats.get('duration', 0))} "
        f"in {stats.get('segments', 0)} clip(s)")
    add(f"settings   : {job.config.quality} / "
        f"{job.config.width}x{job.config.height} @ {job.config.fps:g}fps / "
        f"{job.config.resolved_encoder}"
        + ("" if job.config.include_audio else " / NO AUDIO"))
    if stats.get("size_mb"):
        add(f"file       : {stats['size_mb']} MB"
            + (f", measured {timecode(stats.get('measured_duration', 0))}"
               if stats.get("measured_duration") else ""))
    if stats.get("elapsed"):
        add(f"took       : {stats['elapsed']:.0f}s"
            + (f"  ({stats['realtime_factor']}x realtime)"
               if stats.get("realtime_factor") else ""))
    add("")

    if report.mock:
        add(_THIN)
        add("MOCK RENDER")
        add(_THIN)
        add("  No video was produced. The file above is a placeholder written")
        add("  by the mock runner -- nothing has been encoded and nothing is")
        add("  watchable. Use the real backend for anything you intend to")
        add("  look at.")
        add("")

    if report.failure:
        add(_THIN)
        add("FAILED")
        add(_THIN)
        add(f"  {report.failure.get('message', '')}")
        if report.failure.get("hint"):
            add(f"  fix : {report.failure['hint']}")
        if report.failure.get("path"):
            add(f"  file: {report.failure['path']}")
        if report.failure.get("stderr"):
            add(f"  ffmpeg: {str(report.failure['stderr'])[-300:]}")
        add("")

    if report.unsupported:
        add(_THIN)
        add("NOT IN THIS VIDEO")
        add(_THIN)
        for item in report.unsupported:
            add(f"  - {item}")
        add("")

    missing = [item for item in report.inputs if not item.get("usable")]
    if missing:
        add(_THIN)
        add(f"MISSING SOURCES ({len(missing)})")
        add(_THIN)
        for item in missing:
            add(f"  x {item['path']}")
        add("")

    if report.warnings:
        add(_THIN)
        add(f"WARNINGS ({len(report.warnings)})")
        add(_THIN)
        for warning in report.warnings[:20]:
            add(f"  ! {warning[:150]}")
        if len(report.warnings) > 20:
            add(f"  ... and {len(report.warnings) - 20} more "
                "(report.md has them all).")
        add("")

    add(_THIN)
    add("NEXT")
    add(_THIN)
    for command in report.next_commands:
        add(f"  {command}")
    add("")
    add(_RULE)
    return "\n".join(lines)


def render_job_list(jobs, *, limit: int = 40) -> str:
    """``render list`` -- one line each, newest first."""
    lines = [_RULE, f"RENDERS ({len(jobs)})", _RULE, ""]
    if not jobs:
        lines.append("  Nothing rendered yet. Try:")
        lines.append("    python -m editing.cli render roughcut")
        return "\n".join(lines)
    for job in jobs[:limit]:
        lines.append(f"  {job.line()}")
    if len(jobs) > limit:
        lines.append(f"  ... {len(jobs) - limit} more")
    lines.append("")
    lines.append("  render show <job_id>   for one in full")
    lines.append("  render open <job_id>   to watch it")
    return "\n".join(lines)


def write_report(job: RenderJob) -> Path:
    """Write ``report.md`` into the job folder. Returns the path."""
    from editing.render import store

    target = store.report_path(job.output_dir)
    return store.write_text(target, render_markdown(job))

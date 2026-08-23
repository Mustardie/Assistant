"""The file you write on while you watch.

Everything else in this package exists to produce a video. This produces the
other half of the loop: a markdown file, timestamped to match the render, with
a blank line under every section for the thing you thought at that moment.

The design is one idea. **Reviewing a cut is a fast, low-effort activity, and
anything that interrupts it costs the review.** So:

* the timecodes match the proxy exactly, because they are computed from the
  same segment list the render was built from;
* every section already says which source file it came from and why the
  system kept it, so "why is this here" never means opening another file;
* the answers are shorthand -- ``cut grind``, ``strong payoff`` -- because a
  person pausing a video will type two words and not a sentence.

The shorthand labels are deliberately the same vocabulary the Session 9
feedback collector reasons about, so notes written here can be typed straight
back in as ratings without translation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from editing.render.schema import RenderJob, RenderSegment

#: The shorthand. Two words each, and each one maps to something a later pass
#: can act on -- these are not decoration, they are the vocabulary.
SHORTCUTS = (
    ("good moment", "keep this, it works"),
    ("too slow", "the pacing sags here"),
    ("confusing", "a viewer would not follow this"),
    ("bad cut", "the join itself is wrong"),
    ("keep setup", "this pays off later, do not trim it"),
    ("cut grind", "repetitive work nobody needs to watch"),
    ("needs caption", "something on screen needs explaining"),
    ("needs music", "this stretch is bare"),
    ("wrong hook", "this is not what should open the video"),
    ("strong payoff", "the moment the episode was building to"),
)

#: What each section asks. Kept short: four options and a free line.
VERDICTS = "keep / cut / shorten / extend"


def timecode(seconds: float) -> str:
    """``mm:ss`` -- or ``h:mm:ss`` once a cut passes an hour."""
    total = max(0, int(round(float(seconds))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def render_notes(
    job: RenderJob,
    *,
    interval: float = 0.0,
    max_sections: int = 300,
) -> str:
    """The review notes for one job.

    ``interval`` of 0 writes one section per segment, which is what you want
    for a rough cut: the questions worth asking are about the cuts. A positive
    interval writes fixed-length sections instead, which is better for a long
    cut made of few, long clips.
    """
    segments = list(job.segments)
    lines: list[str] = []
    add = lines.append

    add("# Review Notes")
    add("")
    video = Path(job.output_path).name if job.output_path else "render.mp4"
    add(f"Video: {video}")
    if job.output_path:
        add(f"Path: {job.output_path}")
    add(f"Length: {timecode(job.duration)} "
        f"({len(segments)} clip(s) from {job.stats()['sources']} file(s))")
    if job.sequence_name:
        add(f"Cut: {job.sequence_name}")
    add("")

    if job.result is not None and job.result.mock:
        add("> **This render is a MOCK.** No video was produced -- the file "
            "beside these notes is a placeholder. Nothing below has been "
            "watched by anybody, including the machine.")
        add("")

    add("Write in the blanks while it plays. Two words is enough.")
    add("")
    add("Shorthand:")
    for label, meaning in SHORTCUTS:
        add(f"- `{label}` -- {meaning}")
    add("")
    add("---")
    add("")

    sections = (
        _interval_sections(segments, interval, job.duration)
        if interval > 0 else _segment_sections(segments)
    )
    for section in sections[:max_sections]:
        lines.extend(section)
    if len(sections) > max_sections:
        add(f"_({len(sections) - max_sections} more section(s) not written: "
            f"the cut has more clips than the {max_sections}-section limit.)_")
        add("")

    lines.extend(_overall_section())
    lines.extend(_footer(job))
    return "\n".join(lines) + "\n"


def _segment_sections(segments: Sequence[RenderSegment]) -> list[list[str]]:
    """One section per clip. The default, because cuts are what to judge."""
    out: list[list[str]] = []
    for segment in segments:
        lines = [
            f"## {timecode(segment.timeline_in)}-"
            f"{timecode(segment.timeline_out)}"
            f"  ({segment.duration:.1f}s)",
            "",
            f"- Source: `{Path(segment.source_path).name}` "
            f"{segment.source_in:.1f}-{segment.source_out:.1f}s"
            + (f" @ {segment.speed:g}x" if segment.has_speed_change else ""),
            f"- Kept because: {segment.keep_reason}"
            + ("  (protected -- a hold said leave this alone)"
               if segment.protected else ""),
        ]
        if not segment.audio_enabled:
            lines.append("- Audio: muted")
        if segment.recommendation_ids:
            lines.append(
                "- From: " + ", ".join(segment.recommendation_ids[:4])
                + (" ..." if len(segment.recommendation_ids) > 4 else ""))
        lines += [
            f"- {VERDICTS}:",
            "- Notes:",
            "",
        ]
        out.append(lines)
    return out


def _interval_sections(
    segments: Sequence[RenderSegment], interval: float, duration: float
) -> list[list[str]]:
    """Fixed-length sections, listing whatever plays inside each one."""
    out: list[list[str]] = []
    if duration <= 0:
        return out
    start = 0.0
    while start < duration - 1e-6:
        end = min(start + interval, duration)
        inside = [
            segment for segment in segments
            if segment.timeline_out > start + 1e-6
            and segment.timeline_in < end - 1e-6
        ]
        lines = [f"## {timecode(start)}-{timecode(end)}", ""]
        for segment in inside[:6]:
            lines.append(
                f"- {timecode(segment.timeline_in)} "
                f"`{Path(segment.source_path).name}` "
                f"{segment.source_in:.1f}-{segment.source_out:.1f}s "
                f"({segment.keep_reason})"
            )
        if len(inside) > 6:
            lines.append(f"- ... and {len(inside) - 6} more clip(s)")
        lines += [f"- {VERDICTS}:", "- Notes:", ""]
        out.append(lines)
        start = end
    return out


def _overall_section() -> list[str]:
    """The five questions worth asking once, at the end.

    They are the ones the retention layer cannot answer from the outside:
    every one of them is about how it felt, and the whole point of rendering a
    proxy is that somebody can now say.
    """
    return [
        "---",
        "",
        "## Overall",
        "",
        "- Does the opening earn the next 30 seconds?",
        "- Where did you first get bored?",
        "- What was the best moment, and is it in the right place?",
        "- Anything you did not understand?",
        "- Would you publish this cut, or is it not there yet?",
        "",
    ]


def _footer(job: RenderJob) -> list[str]:
    """How to get these notes back into the system.

    The Session 9 collector already knows how to take a rating against a time
    range or a record ID, so the last thing the notes say is the exact command
    -- writing an opinion into a markdown file that nothing reads would make
    this whole file decoration.
    """
    return [
        "---",
        "",
        "## Feeding this back",
        "",
        "Ratings against a time range or a clip go into the feedback log:",
        "",
        "```",
        "python -m editing.cli feedback start",
        "python -m editing.cli feedback rate 120-155 bad --reason boring "
        "--correction \"cut this shorter\"",
        "python -m editing.cli feedback queue --limit 20",
        "```",
        "",
        "To re-render after changing something:",
        "",
        "```",
        f"python -m editing.cli render roughcut --name {job.plan_name}",
        "```",
        "",
    ]

"""Rendering a result as SRT and as readable text.

Both are for humans and for other tools; the JSON is what this system reads.
That ordering matters for one decision in here: **SRT is written to the
subtitle spec, not to whatever is convenient.** Comma decimal separator, 1-based
indices, blank line between cues, CRLF-tolerant. A player that rejects the file
is a worse outcome than a slightly awkward line break, and Premiere is stricter
than most.

The other decision worth stating: **nothing here re-times anything.** Cues come
out exactly where the model put them. A renderer that nudged overlapping cues
apart would put captions on the wrong frames and be invisible until someone
watched the export.
"""
from __future__ import annotations

from typing import Sequence

from editing.transcribe.schema import TranscriptSegment, TranscriptionResult

#: Subtitle convention: a cue shorter than this is hard to read. Reported, not
#: fixed -- see the module docstring on re-timing.
MIN_READABLE_SECONDS = 0.5


def srt_timestamp(seconds: float) -> str:
    """``HH:MM:SS,mmm`` -- comma, three decimals, as the SRT spec requires."""
    total = max(0.0, float(seconds or 0.0))
    hours, remainder = divmod(int(total), 3600)
    minutes, secs = divmod(remainder, 60)
    milliseconds = int(round((total - int(total)) * 1000))
    # Rounding 1.9999 up to a full second must not produce ",1000".
    if milliseconds >= 1000:
        milliseconds = 0
        secs += 1
        if secs >= 60:
            secs = 0
            minutes += 1
            if minutes >= 60:
                minutes = 0
                hours += 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def vtt_timestamp(seconds: float) -> str:
    """``HH:MM:SS.mmm`` -- WebVTT uses a full stop where SRT uses a comma."""
    return srt_timestamp(seconds).replace(",", ".")


def render_srt(segments: Sequence[TranscriptSegment]) -> str:
    """Standard SRT. 1-based indices, blank line between cues."""
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        text = " ".join(segment.text.split())
        if not text:
            continue
        blocks.append(
            f"{index}\n"
            f"{srt_timestamp(segment.start)} --> {srt_timestamp(segment.end)}\n"
            f"{text}\n"
        )
    return "\n".join(blocks)


def render_vtt(segments: Sequence[TranscriptSegment]) -> str:
    lines = ["WEBVTT", ""]
    for segment in segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        lines.append(
            f"{vtt_timestamp(segment.start)} --> {vtt_timestamp(segment.end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def render_txt(
    result: TranscriptionResult, *, timestamps: bool = True
) -> str:
    """Readable text, with a header saying what produced it.

    The header is not decoration. A ``.txt`` transcript gets pasted into
    documents and issues, and one produced by the mock backend that arrived
    somewhere without its provenance would be indistinguishable from a real
    one.
    """
    lines: list[str] = []
    add = lines.append

    add(f"# {result.source_path}")
    add(f"# {result.backend} / {result.model} / {result.device or 'cpu'}"
        f"  language={result.language or '?'}")
    add(f"# {len(result.segments)} segment(s), {result.word_count} word(s), "
        f"{result.duration:.0f}s of media")
    if result.mock:
        add("#")
        add("# *** MOCK TRANSCRIPT -- every line below is fabricated. ***")
    add("")

    for segment in result.segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        if timestamps:
            add(f"[{_clock(segment.start)}] {text}")
        else:
            add(text)
    return "\n".join(lines) + "\n"


def _clock(seconds: float) -> str:
    total = max(0.0, float(seconds or 0.0))
    return f"{int(total // 60):02d}:{total % 60:05.2f}"


def render_report(result: TranscriptionResult, *, limit: int = 20) -> str:
    """The human summary the CLI prints, limits first."""
    lines: list[str] = []
    add = lines.append
    stats = result.stats()
    rule = "=" * 78
    thin = "-" * 78

    add(rule)
    add(f"TRANSCRIPTION -- {result.job_id or '(no job id)'}")
    add(rule)
    add("")
    add(f"  file      : {result.source_path}")
    add(f"  backend   : {result.backend} / {result.model}"
        f"  ({result.device or '?'}/{result.compute_type or '?'})")
    add(f"  language  : {result.language or 'unknown'}"
        + (f" (p={result.language_probability:.2f})"
           if result.language_probability else ""))
    add(f"  media     : {result.duration:.1f}s")
    add(f"  speech    : {stats['speech_seconds']:.1f}s "
        f"({stats['speech_share']:.0%} of runtime)")
    add(f"  segments  : {stats['segments']}  "
        f"({stats['low_confidence_segments']} low confidence, "
        f"{stats['dropped_segments']} dropped)")
    add(f"  words     : {stats['words']}")
    add(f"  confidence: {stats['mean_confidence']:.2f} mean")
    if result.cached:
        add("  took      : nothing, this came from the cache")
    else:
        speed = stats["realtime_factor"]
        estimate = ""
        if speed > 0:
            # The question people actually have is about their real footage,
            # not about this clip.
            estimate = f"  -- a 40-minute episode would take ~{40 / speed:.0f} min"
        add(f"  took      : {result.elapsed:.1f}s "
            f"({speed:.1f}x realtime){estimate}")
    add("")

    if result.mock:
        add(thin)
        add("*** MOCK BACKEND -- every line below was fabricated without a")
        add("*** speech model. Nothing derived from it means anything.")
        add(thin)
        add("")

    if result.warnings:
        add("WORTH KNOWING")
        add(thin)
        for warning in result.warnings:
            for line in _wrap(f"! {warning}"):
                add(line)
        add("")

    add("TRANSCRIPT")
    add(thin)
    for segment in result.segments[:limit]:
        mark = "?" if segment.is_low_confidence else " "
        add(f" {mark}[{_clock(segment.start)}-{_clock(segment.end)}] "
            f"{' '.join(segment.text.split())[:58]}")
    if len(result.segments) > limit:
        add(f"  ... {len(result.segments) - limit} more")
    return "\n".join(lines)


def _wrap(text: str, width: int = 76, indent: str = "  ") -> list[str]:
    words = str(text or "").split()
    lines: list[str] = []
    current = indent
    for word in words:
        if len(current) + len(word) + 1 > width and current.strip():
            lines.append(current.rstrip())
            current = indent + "  "
        current += word + " "
    if current.strip():
        lines.append(current.rstrip())
    return lines

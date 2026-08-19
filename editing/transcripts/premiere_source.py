"""Premiere Speech to Text / Transcript as a transcript source.

Premiere's transcription lives in the Text panel's Transcript tab (the same
data newer builds call Text-Based Editing). Adobe ships no documented
ExtendScript API for reading it back, so this module:

1. asks the running Premiere what routes it actually exposes
   (``transcript.caps``),
2. tries them for a specific asset (``transcript.read``),
3. and when nothing is reachable, returns a structured *unavailable* answer
   naming the manual export path -- never a fabricated transcript.

Where Premiere has run speech analysis, the result is stored in the project
item's XMP as one marker **per word**. Those are grouped into readable lines
here rather than in ExtendScript, because ``group_word_markers`` is pure and
therefore directly testable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from editing.schema import TranscriptEntry, Transcript

logger = logging.getLogger("nova.editing.transcripts.premiere")

#: Premiere's internal tick rate; the fallback when XMP omits the scale.
PREMIERE_TICKS_PER_SECOND = 254016000000.0

MANUAL_EXPORT_HINT = (
    "In Premiere: Text panel > Transcript tab > the ... menu > Export > "
    "'Export transcript' (.txt) or 'Export captions' (.srt). Then run "
    "`python -m editing.cli transcript import --file <path> --for <clip>`."
)


@dataclass
class TranscriptSupport:
    """What this Premiere install can actually be asked for."""

    available: bool = False
    readable: bool = False
    premiere_version: str = ""
    apis: dict = field(default_factory=dict)
    note: str = ""
    manual_export: str = MANUAL_EXPORT_HINT

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "readable": self.readable,
            "premiere_version": self.premiere_version,
            "apis": dict(self.apis),
            "note": self.note,
            "manual_export": self.manual_export,
        }


def probe_support(bridge=None) -> TranscriptSupport:
    """Measure the transcript routes on the running Premiere.

    ``available=False`` means we could not ask (Premiere closed, panel shut).
    ``available=True, readable=False`` means we asked and the answer was no --
    a different and more useful fact, because it is final for this build.
    """
    bridge, errors = _resolve_bridge(bridge)
    if bridge is None:
        return TranscriptSupport(note=errors)

    health = bridge.health()
    if not health.get("connected"):
        return TranscriptSupport(
            note="Premiere bridge not reachable, so its transcript could not be "
                 "requested. Open Premiere with the Nova Premiere Bridge panel, "
                 "or import a transcript file instead."
        )

    try:
        from premiere.errors import PremiereError
    except ImportError:  # pragma: no cover
        PremiereError = Exception  # type: ignore[assignment]

    try:
        report = bridge.call("transcript.caps", {}) or {}
    except PremiereError as exc:
        # An older panel build without the transcript module lands here. That
        # is a real, reportable answer rather than a crash.
        return TranscriptSupport(
            available=False,
            note=f"This Premiere Bridge panel has no transcript support ({exc}). "
                 "Reinstall it with `python -m premiere.install` and restart "
                 "Premiere, or import a transcript file instead.",
        )

    return TranscriptSupport(
        available=True,
        readable=bool(report.get("readable")),
        premiere_version=str(report.get("version") or ""),
        apis=dict(report.get("apis") or {}),
        note=str(report.get("note") or ""),
        manual_export=str(report.get("manual_export") or MANUAL_EXPORT_HINT),
    )


def _resolve_bridge(bridge):
    if bridge is not None:
        return bridge, ""
    try:
        from premiere.bridge import bridge as default_bridge
    except ImportError as exc:  # pragma: no cover
        return None, f"Premiere layer unavailable: {exc}"
    return default_bridge, ""


# ---------------------------------------------------------------------------
# Word marker grouping
# ---------------------------------------------------------------------------

#: Punctuation that ends a spoken line, so grouping breaks after it.
_SENTENCE_END = (".", "!", "?")


def group_word_markers(
    markers: list[dict],
    *,
    scale: float = PREMIERE_TICKS_PER_SECOND,
    max_gap: float = 0.65,
    max_chars: int = 90,
    max_duration: float = 8.0,
) -> list[TranscriptEntry]:
    """Turn per-word speech markers into readable timed lines.

    A line is broken when any of these is true: the speaker changes, the pause
    before the next word exceeds ``max_gap``, the line would exceed
    ``max_chars`` or ``max_duration``, or the previous word ended a sentence.
    Those thresholds are chosen so the result looks like subtitle lines, which
    is the granularity alignment and editing both want.

    ``confidence`` on the result is the mean of the words' probabilities, so a
    line assembled from words the recogniser was unsure about is visibly less
    trustworthy downstream.
    """
    divisor = float(scale) if scale else PREMIERE_TICKS_PER_SECOND
    if divisor <= 0:
        divisor = PREMIERE_TICKS_PER_SECOND

    words = []
    for marker in markers or []:
        text = str(marker.get("text") or "").strip()
        if not text:
            continue
        start = _number(marker.get("start")) / divisor
        duration = max(0.0, _number(marker.get("duration")) / divisor)
        words.append({
            "start": max(0.0, start),
            "end": max(0.0, start) + duration,
            "text": text,
            "speaker": str(marker.get("speaker") or "").strip(),
            "probability": _number(marker.get("probability"), 1.0),
        })
    words.sort(key=lambda word: (word["start"], word["end"]))

    entries: list[TranscriptEntry] = []
    current: Optional[dict] = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        text = " ".join(current["words"]).strip()
        if text:
            scores = current["scores"]
            entries.append(TranscriptEntry(
                start=current["start"],
                end=max(current["start"], current["end"]),
                text=text,
                speaker=current["speaker"],
                confidence=max(0.0, min(1.0, sum(scores) / len(scores)))
                if scores else 1.0,
                source_ref=current["source_ref"],
            ))
        current = None

    for word in words:
        if current is not None:
            gap = word["start"] - current["end"]
            joined_length = len(" ".join(current["words"])) + 1 + len(word["text"])
            ends_sentence = current["words"][-1].endswith(_SENTENCE_END)
            if (
                word["speaker"] != current["speaker"]
                or gap > max_gap
                or joined_length > max_chars
                or (word["end"] - current["start"]) > max_duration
                or ends_sentence
            ):
                flush()

        if current is None:
            current = {
                "start": word["start"],
                "end": word["end"],
                "speaker": word["speaker"],
                "words": [],
                "scores": [],
                "source_ref": "",
            }
        current["words"].append(word["text"])
        current["scores"].append(word["probability"])
        current["end"] = max(current["end"], word["end"])

    flush()
    return entries


def _number(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


# ---------------------------------------------------------------------------
# Pulling
# ---------------------------------------------------------------------------

@dataclass
class PullResult:
    """Outcome of asking Premiere for one asset's transcript.

    Deliberately not an exception on the "no transcript" path: the caller
    usually wants to carry on with the other assets and report at the end which
    ones need a manual export.
    """

    found: bool
    transcript: Optional[Transcript] = None
    method: str = ""
    checked: list = field(default_factory=list)
    note: str = ""
    manual_export: str = MANUAL_EXPORT_HINT

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "method": self.method,
            "checked": list(self.checked),
            "note": self.note,
            "manual_export": self.manual_export,
            "entries": len(self.transcript) if self.transcript else 0,
        }


def pull(asset, *, bridge=None, reference: Optional[str] = None) -> PullResult:
    """Ask Premiere for ``asset``'s transcript.

    ``reference`` overrides what is sent to Premiere to identify the item;
    by default the media path is used, which ``project.findAsset`` matches
    exactly before it falls back to looser name matching.
    """
    bridge, error = _resolve_bridge(bridge)
    if bridge is None:
        return PullResult(found=False, note=error)

    try:
        from premiere.errors import PremiereError
    except ImportError:  # pragma: no cover
        PremiereError = Exception  # type: ignore[assignment]

    if not bridge.health().get("connected"):
        return PullResult(
            found=False,
            note="Premiere is not reachable, so its transcript could not be read.",
        )

    target = reference or getattr(asset, "path", "") or getattr(asset, "filename", "")
    try:
        report = bridge.call("transcript.read", {"asset": str(target)}) or {}
    except PremiereError as exc:
        return PullResult(
            found=False,
            note=f"Premiere could not read a transcript for this item: {exc}",
        )

    if not report.get("found"):
        return PullResult(
            found=False,
            checked=list(report.get("checked") or []),
            note=str(report.get("note") or "Premiere has no transcript for this item."),
            manual_export=str(report.get("manual_export") or MANUAL_EXPORT_HINT),
        )

    method = str(report.get("method") or "")
    entries = group_word_markers(
        report.get("markers") or [],
        # A caption-track read already reports seconds; only the XMP route is
        # in ticks. Trusting the host's scale blindly would divide seconds by
        # 254 billion and silently produce a transcript at time zero.
        scale=_number(report.get("scale"), PREMIERE_TICKS_PER_SECOND) or 1.0,
    )
    if not entries:
        return PullResult(
            found=False,
            method=method,
            checked=list(report.get("checked") or []),
            note="Premiere returned transcript markers but none carried usable text.",
        )

    transcript = Transcript(
        asset_id=getattr(asset, "asset_id", ""),
        source="premiere",
        source_path=str(report.get("path") or getattr(asset, "path", "")),
        entries=entries,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        note=f"Premiere Speech to Text via {method}",
    )
    return PullResult(
        found=True,
        transcript=transcript,
        method=method,
        checked=list(report.get("checked") or []),
        note=str(report.get("note") or ""),
    )

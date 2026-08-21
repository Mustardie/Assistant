"""Getting feedback out of here in a shape something else can read.

Three formats, for three genuinely different readers:

``jsonl``
    One record per line, each tagged with a ``kind``. The default, because it
    is what a dataset builder wants: streamable, appendable, and it can hold
    the feedback, the preference signals and the training signals in one file
    without any of them having to be nested inside another.
``json``
    One object with the parts as keys, plus the session it came from. For a
    person, or for anything that would rather load the whole thing at once.
``csv``
    A flat table of the feedback itself, for a spreadsheet. Deliberately lossy
    and it says so in the manifest: corrections collapse to one column and the
    signals are not included at all, because a rectangle is the wrong shape for
    them.

**Every export carries its provenance.** The session, the run, the filters, the
counts and a checksum go into a manifest written beside the file. An export
whose provenance is not recorded is what makes a dataset unreproducible six
months later, and feeding a dataset builder is the entire purpose of this layer.

**Every export carries ``NOT_MEASURED``.** It travels with the data rather than
staying behind in the report, because the file is the thing that gets emailed.
"""
from __future__ import annotations

import csv
import io
import json
import time
from typing import Iterable, Optional, Sequence

from editing.errors import EditingError
from editing.feedback.schema import (
    EXPORT_FORMATS, EXPORT_PARTS, FeedbackExport, FeedbackItem, NOT_MEASURED,
    PreferenceSignal, ReviewQueue, TrainingSignal, coerce_many, coerce_one,
)

#: The columns of a CSV export, in order. Fixed rather than derived from the
#: first row, so a session where nobody wrote a correction still produces the
#: same table as one where everybody did.
CSV_COLUMNS = (
    "feedback_id", "created_at", "session_id", "run_id", "prompt_id",
    "target_type", "target_id", "artifact", "resolved", "start", "end",
    "rating", "polarity", "categories", "note", "correction_action",
    "correction_text", "priority", "confidence", "usable_for_training",
    "needs_follow_up", "summary",
)


def build(
    *,
    parts: Iterable[str] = ("feedback",),
    fmt: str = "jsonl",
    items: Sequence[FeedbackItem] = (),
    preferences: Sequence[PreferenceSignal] = (),
    training: Sequence[TrainingSignal] = (),
    queue: Optional[ReviewQueue] = None,
    session: Optional[dict] = None,
    filters: Optional[dict] = None,
) -> tuple[str, FeedbackExport]:
    """Render an export and the manifest that describes it.

    Returns ``(body, record)``. The record's ``path``, ``checksum`` and
    ``bytes_written`` are filled in by ``store.write_export`` once it knows
    where the bytes landed.
    """
    chosen_format = coerce_one(fmt, EXPORT_FORMATS, "")
    if not chosen_format:
        raise EditingError(
            f"'{fmt}' is not an export format this layer writes",
            hint="Formats: " + ", ".join(EXPORT_FORMATS),
        )
    chosen_parts = coerce_many(list(parts), EXPORT_PARTS, limit=4) or ["feedback"]

    counts = {
        "feedback": len(items) if "feedback" in chosen_parts else 0,
        "preferences": len(preferences) if "preferences" in chosen_parts else 0,
        "training": len(training) if "training" in chosen_parts else 0,
        "queue": (
            len(queue.prompts) if queue is not None and "queue" in chosen_parts
            else 0
        ),
    }
    notes = ""
    if chosen_format == "csv":
        chosen_parts = ["feedback"]
        counts = {"feedback": len(items), "preferences": 0, "training": 0,
                  "queue": 0}
        notes = (
            "CSV is lossy: one row per rating, corrections flattened to two "
            "columns, and preference and training signals omitted entirely. "
            "Use jsonl for anything that will be trained on."
        )

    if chosen_format == "jsonl":
        body = _as_jsonl(chosen_parts, items, preferences, training, queue,
                         session)
    elif chosen_format == "json":
        body = _as_json(chosen_parts, items, preferences, training, queue,
                        session)
    else:
        body = _as_csv(items)

    record = FeedbackExport(
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        session_id=str((session or {}).get("session_id", "")),
        run_id=str((session or {}).get("run_id", "")),
        format=chosen_format,
        parts=chosen_parts,
        counts=counts,
        filters=dict(filters or {}),
        notes=notes,
    )
    return body, record


def _as_jsonl(
    parts: Sequence[str],
    items: Sequence[FeedbackItem],
    preferences: Sequence[PreferenceSignal],
    training: Sequence[TrainingSignal],
    queue: Optional[ReviewQueue],
    session: Optional[dict],
) -> str:
    """One record per line, each tagged with its ``kind``.

    The header line is a record too, rather than a comment: a reader that
    filters on ``kind`` skips it for free, and a format where line one has to
    be special-cased is a format people get wrong.
    """
    lines = [
        json.dumps({
            "kind": "header",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "basis": NOT_MEASURED,
            "parts": list(parts),
            "session": session or {},
        }, ensure_ascii=False, default=str)
    ]
    if "queue" in parts and queue is not None:
        for prompt in queue.prompts:
            lines.append(json.dumps(
                {"kind": "prompt", **prompt.to_dict()},
                ensure_ascii=False, default=str))
    if "feedback" in parts:
        for item in items:
            lines.append(json.dumps(
                {"kind": "feedback", **item.to_dict()},
                ensure_ascii=False, default=str))
    if "preferences" in parts:
        for signal in preferences:
            lines.append(json.dumps(
                {"kind": "preference", **signal.to_dict()},
                ensure_ascii=False, default=str))
    if "training" in parts:
        for signal in training:
            lines.append(json.dumps(
                {"kind": "training", **signal.to_dict()},
                ensure_ascii=False, default=str))
    return "\n".join(lines) + "\n"


def _as_json(
    parts: Sequence[str],
    items: Sequence[FeedbackItem],
    preferences: Sequence[PreferenceSignal],
    training: Sequence[TrainingSignal],
    queue: Optional[ReviewQueue],
    session: Optional[dict],
) -> str:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "basis": NOT_MEASURED,
        "parts": list(parts),
        "session": session or {},
    }
    if "queue" in parts and queue is not None:
        payload["queue"] = queue.to_dict()
    if "feedback" in parts:
        payload["feedback"] = [item.to_dict() for item in items]
    if "preferences" in parts:
        payload["preferences"] = [signal.to_dict() for signal in preferences]
    if "training" in parts:
        payload["training"] = [signal.to_dict() for signal in training]
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"


def _as_csv(items: Sequence[FeedbackItem]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=list(CSV_COLUMNS), extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for item in items:
        correction = item.correction
        writer.writerow({
            "feedback_id": item.feedback_id,
            "created_at": item.created_at,
            "session_id": item.session_id,
            "run_id": item.run_id,
            "prompt_id": item.prompt_id,
            "target_type": item.target.target_type,
            "target_id": item.target.target_id,
            "artifact": item.target.artifact,
            "resolved": item.target.resolved,
            "start": round(item.target.start, 3),
            "end": round(item.target.end, 3),
            "rating": item.rating.rating,
            "polarity": item.polarity,
            "categories": "|".join(item.categories),
            "note": item.note.replace("\n", " "),
            "correction_action": correction.action if correction else "",
            "correction_text": (
                correction.text.replace("\n", " ") if correction else ""),
            "priority": round(item.priority, 3),
            "confidence": round(item.confidence, 3),
            "usable_for_training": item.usable_for_training,
            "needs_follow_up": item.needs_follow_up,
            "summary": item.summary.replace("\n", " "),
        })
    return buffer.getvalue()


def default_filename(fmt: str, parts: Sequence[str]) -> str:
    stem = "-".join(sorted(parts)) or "feedback"
    return f"{stem}.{fmt}"

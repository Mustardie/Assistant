"""The finished artifact: identifying the edited sequence and rendering it.

The pipeline used to end at "edit plan generated successfully". Every artifact
it produced was a description of a video. This module is where it ends at a
file instead.

Three jobs, and the third is the one that is easy to get wrong:

1. **Identify** the finished sequence. Not "whatever is open" -- the sequence
   the run's own plans built, confirmed to exist in the project by asking
   Premiere for its sequence list.
2. **Export** it, through ``sequence.export`` and therefore through the same
   catalog and bridge as every other operation.
3. **Tell the truth about whether it finished.** Premiere has two render
   routes: a direct export that blocks until the file is written, and Adobe
   Media Encoder, which returns the instant the job is queued. Reporting the
   second as a completed render would be the single most misleading thing this
   system could do, so a queued render is polled for its file and a render that
   never produces one is reported as a failure with the reason.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from editing.conform.schema import DeliveryResult, now

logger = logging.getLogger("nova.editing.conform.deliver")

#: How long to wait for a Media Encoder render before giving up. Generous:
#: an eight-minute 4K episode takes a while, and the alternative to waiting is
#: reporting a file that is not there.
DEFAULT_WAIT = 900.0

#: How long the file has to stop growing before it counts as finished. AME
#: writes the container header early, so "the file exists" is not the same as
#: "the render is done".
SETTLE_SECONDS = 4.0
POLL_SECONDS = 2.0


def find_sequence(bridge, wanted: str = "") -> tuple:
    """``(name, note)`` for the sequence to deliver.

    An explicit name is verified rather than trusted -- exporting the wrong
    sequence produces a plausible-looking file of the wrong video, which is
    worse than an error.
    """
    try:
        info = bridge.call("sequence.list") or {}
    except Exception as exc:  # noqa: BLE001 - a failed lookup is an answer
        return "", f"could not read the sequence list: {exc}"

    entries = info.get("sequences") if isinstance(info, dict) else info
    names = [str(entry.get("name")) for entry in (entries or [])
             if isinstance(entry, dict) and entry.get("name")]
    if not names:
        return "", "the project contains no sequences"

    if wanted:
        for name in names:
            if name == wanted:
                return name, ""
        return "", (
            f"no sequence named '{wanted}' exists in the project. "
            f"It has: {', '.join(names[:8])}"
        )
    if len(names) == 1:
        return names[0], ""
    return "", (
        "the project has several sequences and none was named, so there is no "
        f"way to know which one is the edit: {', '.join(names[:8])}"
    )


def deliver(
    *,
    bridge,
    sequence_name: str,
    output_path: str,
    preset: str = "",
    wait: float = DEFAULT_WAIT,
    overwrite: bool = True,
    poll: float = POLL_SECONDS,
) -> DeliveryResult:
    """Render one sequence to one file, and report honestly what happened."""
    started = time.time()
    result = DeliveryResult(
        sequence_name=sequence_name,
        requested_path=str(output_path),
        finished_at=now(),
    )

    name, note = find_sequence(bridge, sequence_name)
    if not name:
        result.error = {
            "code": "sequence_not_found",
            "error": "There is no finished sequence to export.",
            "hint": note,
        }
        return result
    result.sequence_name = name

    # Absolute, always.
    #
    # Premiere runs with its own working directory, so a relative path handed
    # across the bridge resolves somewhere nobody intended -- the direct export
    # fails, the call falls through to Media Encoder, and the result is a
    # render that reports "started" and never produces a file. The failure
    # looked like a Media Encoder problem and was a path problem.
    target = Path(output_path).expanduser().resolve()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.error = {
            "code": "output_unwritable",
            "error": f"Could not create {target.parent}: {exc}",
        }
        return result
    if target.exists() and overwrite:
        try:
            target.unlink()
        except OSError as exc:
            result.error = {
                "code": "output_locked",
                "error": f"{target} exists and could not be replaced: {exc}",
                "hint": "Close anything playing the file, then retry.",
            }
            return result

    params = {
        "sequence": name,
        "path": str(target),
        "overwrite": overwrite,
    }
    if preset:
        params["preset"] = preset

    try:
        response = bridge.call("sequence.export", params,
                               timeout=max(120.0, wait)) or {}
    except Exception as exc:  # noqa: BLE001 - a failed export is a result
        to_dict = getattr(exc, "to_dict", None)
        result.error = (
            to_dict() if callable(to_dict)
            else {"code": "export_failed", "error": str(exc)}
        )
        result.waited = round(time.time() - started, 2)
        return result

    result.started = bool(response.get("started"))
    result.complete = bool(response.get("complete"))
    result.method = str(response.get("method") or "")
    result.preset = str(response.get("preset") or preset)
    result.output_path = str(response.get("path") or target)

    if not result.complete:
        # Media Encoder route: the call returned before the render did, so the
        # only trustworthy signal is the file itself.
        result.warnings.append(
            "Adobe Media Encoder was handed the render, so this waited for the "
            "file rather than for the call to return."
        )
        _wait_for_file(Path(result.output_path), wait=wait, poll=poll)

    _stat(result)
    result.waited = round(time.time() - started, 2)

    if not result.delivered:
        result.error = result.error or {
            "code": "no_output",
            "error": (
                f"The export reported {'success' if result.started else 'nothing'} "
                f"but no file was written to {result.output_path}."
            ),
            "hint": (
                "Check Adobe Media Encoder's queue for a failed job, and that "
                "the preset matches the sequence's frame size and rate."
                if result.method == "media_encoder" else
                "Check that the export preset is valid for this sequence."
            ),
        }
    else:
        result.complete = True
    return result


def _wait_for_file(path: Path, *, wait: float, poll: float) -> None:
    """Block until the file exists and has stopped growing, or time runs out."""
    deadline = time.time() + max(0.0, wait)
    last_size = -1
    stable_since = 0.0
    while time.time() < deadline:
        if path.exists():
            size = path.stat().st_size
            if size > 0 and size == last_size:
                if stable_since and time.time() - stable_since >= SETTLE_SECONDS:
                    return
                stable_since = stable_since or time.time()
            else:
                stable_since = 0.0
            last_size = size
        time.sleep(max(0.25, poll))


def _stat(result: DeliveryResult) -> None:
    path = Path(result.output_path or result.requested_path)
    result.exists = path.is_file()
    if not result.exists:
        return
    result.size_bytes = path.stat().st_size
    result.duration = _probe_duration(path)


def _probe_duration(path: Path) -> float:
    """The rendered file's real duration, so the report can compare it to the cut."""
    try:
        from editing import ffmpeg as ff

        return float(ff.probe(path).get("duration") or 0.0)
    except Exception:  # noqa: BLE001 - a missing duration is not a failure
        return 0.0


def default_output_path(root: str | os.PathLike, sequence_name: str,
                        *, extension: str = ".mp4") -> Path:
    """Where a finished edit goes when nobody said. Always absolute."""
    safe = "".join(
        ch if ch.isalnum() or ch in "-_ " else "_" for ch in (sequence_name or "edit")
    ).strip() or "edit"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return (Path(root).expanduser().resolve()
            / "delivered" / f"{safe}-{stamp}{extension}")

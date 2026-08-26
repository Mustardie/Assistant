"""Looking at what was actually produced.

Every critique in this system until now judged a *description* of the edit.
The frames the critic looked at were pulled straight from the source files at
the times the cut mapped to, which means they showed the footage as it was
recorded: no captions, no grade, no callouts, no freeze frames. The critic was
asked "is this edit good" and shown the raw material.

This module closes that. Once the conform pass has executed, the edit exists in
Premiere, and Premiere can render its own program monitor -- which is the only
picture in this system that contains what the editor actually did. So:

    executed sequence -> frame.export at chosen times -> the same critic

The frames come back through ``frame.export``, an operation that has been in
the catalog since the beginning and whose whole stated purpose was to let the
editing model *look at* a frame instead of reasoning blind. It has simply never
had anything worth looking at before.

Premiere's scriptable frame export is not in the public API and moves between
versions -- on Premiere 25 it is simply gone, and the host says so rather than
inventing a result. So there is a second route, and it is arguably the better
one: once the edit has been **delivered**, the rendered file *is* the finished
timeline, and FFmpeg can read any frame of it. Same frames, same critic, one
render behind.

What this never does is fall back to the *source* footage. Source frames are
what the review pass already looks at, they contain none of the edit, and
presenting them as a verification of the edit would be the one dishonest thing
this module could do.

Two honest limits, stated rather than hidden:

* Neither route exists before the edit has been executed, and the render route
  additionally needs a delivered file. Both absences are reported, not
  papered over.
* A still cannot show a fade, a transition or a mix. Anything time-varying is
  sampled at a point, so a caption's fade-in reads as an opacity, not a fade.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger("nova.editing.conform.verify")

#: How many frames to pull when nobody says. Enough to cover the moments the
#: pass actually changed, few enough that a verification is not a render.
DEFAULT_FRAME_COUNT = 12

#: Operations whose result is visible in a still, and therefore worth
#: photographing. A gain change is real and invisible, so it is not here.
VISIBLE_OPS = (
    "text.create", "graphic.shape", "graphic.image", "clip.freeze",
    "color.grade", "transition.apply", "animate",
)


@dataclass
class VerifiedFrame:
    """One frame of the finished edit, and what was supposed to be there."""

    at: float = 0.0
    path: str = ""
    exported: bool = False
    #: Which planned operations claim this moment.
    expects: list[str] = field(default_factory=list)
    note: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationResult:
    """What the finished timeline actually looks like."""

    sequence_name: str = ""
    frames: list[VerifiedFrame] = field(default_factory=list)
    #: Where the pictures came from: the delivered render, or the program
    #: monitor. Never the source footage -- see the module docstring.
    source: str = ""
    supported: bool = True
    #: Why frame export could not run, when it could not.
    note: str = ""
    elapsed: float = 0.0

    @property
    def exported(self) -> list[VerifiedFrame]:
        return [frame for frame in self.frames if frame.exported]

    @property
    def usable(self) -> bool:
        return bool(self.exported)

    def line(self) -> str:
        if not self.supported:
            return f"not verified: {self.note}"
        return (f"{len(self.exported)} of {len(self.frames)} frame(s) of "
                f"'{self.sequence_name}' read from "
                f"{self.source or 'nowhere'}")

    def to_dict(self) -> dict:
        return {
            "sequence_name": self.sequence_name,
            "source": self.source,
            "frames": [frame.to_dict() for frame in self.frames],
            "supported": self.supported,
            "note": self.note,
            "elapsed": round(self.elapsed, 2),
            "exported": len(self.exported),
            "usable": self.usable,
        }


def moments_of_interest(plan, *, limit: int = DEFAULT_FRAME_COUNT) -> list[tuple]:
    """``(time, [what is expected there])``, most interesting first.

    Chosen from the plan's own operations rather than spread evenly: the point
    of this pass is to check that what the editor *did* is there, so the times
    worth photographing are the times it did something visible.
    """
    claims: dict = {}
    for op in getattr(plan, "ops", ()) or ():
        name = op.get("op")
        if name not in VISIBLE_OPS:
            continue
        at = op.get("time")
        if at is None:
            at = (op.get("clip") or {}).get("at")
        if at is None and name in ("color.grade",):
            # A grade covers the whole programme; sample it in the middle,
            # where a still is least likely to land on a transition.
            at = round(float(getattr(plan, "cut_duration", 0.0)) / 2.0, 3)
        if at is None:
            continue
        key = round(float(at) + _offset_for(name, op), 2)
        label = f"{name}: {str(op.get('note', ''))[:60]}".strip()
        claims.setdefault(key, []).append(label)

    ordered = sorted(claims.items(), key=lambda item: (-len(item[1]), item[0]))
    return [(at, labels) for at, labels in ordered[:limit]]


def _offset_for(name: str, op: dict) -> float:
    """How far into an operation to sample.

    A caption photographed at its own start time lands on the first frame of
    its fade-in, where it is transparent. A transition photographed at the cut
    lands mid-dissolve. Sampling a little way in is the difference between
    verifying an edit and photographing the moment before it.
    """
    if name in ("text.create", "graphic.shape", "graphic.image"):
        duration = float(op.get("duration") or 0.0)
        return min(0.6, duration / 2.0) if duration else 0.3
    if name == "transition.apply":
        return float(op.get("duration") or 0.5) / 2.0
    if name == "animate":
        return float(op.get("duration") or 0.0) * 0.9
    return 0.0


def verify(
    plan,
    *,
    bridge=None,
    output_dir: str | Path,
    limit: int = DEFAULT_FRAME_COUNT,
    activate: bool = True,
    rendered: str | Path = "",
    ffmpeg: str = "ffmpeg",
) -> VerificationResult:
    """Frames of the finished edit, from Premiere or from the render.

    ``rendered`` is preferred when it exists: a delivered file contains
    exactly what a viewer would see, needs no Premiere API that may have been
    removed, and cannot be confused with source footage. The program monitor
    is the fallback for when the edit has been executed but not yet delivered.
    Never raises.
    """
    if rendered and Path(rendered).is_file():
        return _verify_from_render(plan, Path(rendered), output_dir,
                                   limit=limit, ffmpeg=ffmpeg)
    if bridge is None:
        result = VerificationResult(
            sequence_name=getattr(plan, "sequence_name", ""),
        )
        result.supported = False
        result.note = (
            "there is no rendered file to read and no Premiere connection to "
            "photograph the timeline with."
        )
        return result
    return _verify_from_premiere(plan, bridge=bridge, output_dir=output_dir,
                                 limit=limit, activate=activate)


def _verify_from_render(plan, rendered: Path, output_dir: str | Path, *,
                        limit: int, ffmpeg: str) -> VerificationResult:
    """Pull the same moments out of the delivered video with FFmpeg."""
    from editing import ffmpeg as ff

    started = time.time()
    result = VerificationResult(
        sequence_name=getattr(plan, "sequence_name", ""),
        source="rendered_file",
    )
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)

    moments = moments_of_interest(plan, limit=limit)
    if not moments:
        result.note = (
            "the conform plan changed nothing that would show in a still."
        )
        result.elapsed = time.time() - started
        return result

    for index, (at, expects) in enumerate(moments):
        target = folder / f"verify_{index:02d}_{at:07.2f}.png".replace(" ", "0")
        frame = VerifiedFrame(at=at, path=str(target), expects=expects,
                              note=f"read from {rendered.name}")
        try:
            ff.extract_frame(rendered, at, target, ffmpeg=ffmpeg)
            frame.exported = target.is_file()
            if not frame.exported:
                frame.error = "ffmpeg wrote no file for this time"
        except Exception as exc:  # noqa: BLE001 - one failure is not fatal
            frame.error = str(exc)[:300]
        result.frames.append(frame)

    if not result.exported:
        result.note = (
            f"no frame could be read out of {rendered}; the file may be "
            "incomplete."
        )
    result.elapsed = time.time() - started
    return result


def _verify_from_premiere(
    plan, *, bridge, output_dir: str | Path, limit: int, activate: bool,
) -> VerificationResult:
    """Export frames of the executed sequence from the program monitor."""
    started = time.time()
    result = VerificationResult(
        sequence_name=getattr(plan, "sequence_name", ""),
        source="premiere_program_monitor",
    )
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)

    moments = moments_of_interest(plan, limit=limit)
    if not moments:
        result.note = (
            "the conform plan changed nothing that would show in a still, so "
            "there is nothing for a frame to verify."
        )
        result.elapsed = time.time() - started
        return result

    if activate and result.sequence_name:
        try:
            bridge.call("sequence.activate", {"name": result.sequence_name})
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            result.supported = False
            result.note = f"could not activate '{result.sequence_name}': {exc}"
            result.elapsed = time.time() - started
            return result

    for index, (at, expects) in enumerate(moments):
        target = folder / f"verify_{index:02d}_{at:07.2f}.png".replace(" ", "0")
        frame = VerifiedFrame(at=at, path=str(target), expects=expects)
        try:
            bridge.call("frame.export", {"time": at, "path": str(target)})
            frame.exported = target.is_file()
            if not frame.exported:
                frame.error = "the host reported success but wrote no file"
        except Exception as exc:  # noqa: BLE001 - one failure is not fatal
            frame.error = str(exc)[:300]
            code = getattr(exc, "code", "")
            if code == "unsupported":
                result.supported = False
                result.note = (
                    "this Premiere build exposes no scriptable frame export, "
                    "so the finished timeline cannot be photographed. "
                    f"{getattr(exc, 'alternative', '')}"
                )
                result.frames.append(frame)
                break
        result.frames.append(frame)

    if result.supported and not result.exported:
        result.note = (
            "every frame export failed; the critic has nothing of the finished "
            "edit to look at."
        )
    result.elapsed = time.time() - started
    return result


def critic_frames(result: VerificationResult) -> list[dict]:
    """The exported frames in the shape the critic's prompt builder expects.

    Deliberately the same shape the source-frame path produces, so the critic
    itself needs no knowledge of where a frame came from -- only the caller
    decides whether it is judging the material or the edit.
    """
    return [
        {
            "path": frame.path,
            "at": frame.at,
            "sequence_time": frame.at,
            "source": result.source,
            "expects": list(frame.expects),
        }
        for frame in result.exported
    ]

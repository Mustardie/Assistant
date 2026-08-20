"""Review frames from the rough cut.

After a cut exists, the next useful question is "does it actually look right?".
This module produces the evidence for answering that — one representative frame
per clip, each tied back to the recommendation and segment that put the clip in
the cut — and writes a manifest a later critic pass can read without knowing
anything about how the cut was built.

**Frames come from the source files, not from Premiere.** The rough cut is an
assembly of source ranges with known in/out points, so a frame at sequence time
*t* is a frame at a computable source time — and pulling it with FFmpeg needs
neither Premiere open nor a render. It also means review frames can be exported
from a plan that was never executed, which is what makes them useful for
checking a cut *before* committing to it.

Nothing here judges the cut. It prepares the input for something that will.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from editing import ffmpeg as ff
from editing.config import EditingConfig
from editing.errors import ToolMissingError
from editing.roughcut.schema import ClipPlacement, RoughCutPlan

#: Where in each clip the representative frame is taken from. A third of the
#: way in avoids both the incoming cut and any trailing motion blur, and is
#: past whatever the previous clip's action was still resolving.
DEFAULT_POSITION = 0.34

#: Long edge of an exported review frame. Larger than an analysis frame: a
#: human is looking at these, and readability of the HUD is the point.
DEFAULT_WIDTH = 960


#: Why a frame was chosen. A critic reads this to know what it is looking at:
#: "is this framed well" is a different question at a cut point than in the
#: middle of a punch-in.
FRAME_KINDS = (
    "clip_sample",      # the representative frame, a third of the way in
    "clip_start",       # just after the incoming cut
    "clip_end",         # just before the outgoing cut
    "marker",           # at a planned marker
    "zoom",             # inside a punch-in or push-in
    "speed_change",     # inside a retimed clip
    "text_placeholder", # where text/caption/callout was proposed
    "high_priority",    # a moment the planner ranked highly
    "sanity",           # a random probe inside a long stretch
)


@dataclass
class ReviewFrame:
    """One exported frame, with everything needed to trace it back.

    The trailing fields are *context*: what was being said, heard and seen at
    this moment, and what the cut did to it. They are denormalised onto the
    frame rather than left as IDs because the consumer is a vision model
    looking at one picture at a time -- it has no way to follow a reference,
    and a critic that does not know a clip was sped to 2x will misread the
    motion blur it is looking at.
    """

    frame_id: str
    placement_id: str
    path: str
    sequence_time: float
    source_time: float
    source_file: str
    asset_id: str
    keep_reason: str
    speed: float = 1.0
    protected: bool = False
    recommendation_ids: list[str] = field(default_factory=list)
    segment_ids: list[str] = field(default_factory=list)
    #: Markers landing inside this clip, so a reviewer sees what was intended.
    marker_names: list[str] = field(default_factory=list)
    note: str = ""

    # -- context, filled in by ``editing.critic.frames`` ------------------
    #: The sequence this frame belongs to, so a frame record reads alone.
    sequence_name: str = ""
    #: One of ``FRAME_KINDS``.
    frame_kind: str = "clip_sample"
    #: Why this frame was sampled, in a phrase.
    reason: str = ""
    #: What is being said at (or just around) this moment.
    transcript: str = ""
    #: Audio events overlapping this moment: ``{type, start, end, confidence}``.
    audio_events: list[dict] = field(default_factory=list)
    audio_types: list[str] = field(default_factory=list)
    #: Visual events overlapping this moment, by ID plus a flattened summary.
    visual_event_ids: list[str] = field(default_factory=list)
    environment: str = ""
    actions: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    threats: list[str] = field(default_factory=list)
    importance: str = ""
    #: HUD/screen state read by the vision layer, e.g. ``["low_health"]``.
    ui_flags: list[str] = field(default_factory=list)
    #: Edits the rough cut applies at this moment: ``{kind, detail, ...}``.
    applied_edits: list[dict] = field(default_factory=list)
    #: Highest recommendation priority covering this moment, 0 when none.
    priority: float = 0.0
    #: How long the clip this frame came from runs on the timeline. Carried on
    #: the frame because "is this stretch too long" is unanswerable from a
    #: still, and the critic is asked that question.
    clip_duration: float = 0.0

    @property
    def has_zoom(self) -> bool:
        return any(edit.get("kind") == "zoom" for edit in self.applied_edits)

    @property
    def has_text(self) -> bool:
        return any(
            edit.get("kind") in ("text", "caption", "callout")
            for edit in self.applied_edits
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["sequence_time"] = round(self.sequence_time, 3)
        data["source_time"] = round(self.source_time, 3)
        data["priority"] = round(self.priority, 3)
        data["clip_duration"] = round(self.clip_duration, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewFrame":
        kind = str(data.get("frame_kind") or "clip_sample")
        return cls(
            frame_id=str(data.get("frame_id") or ""),
            placement_id=str(data.get("placement_id") or ""),
            path=str(data.get("path") or ""),
            sequence_time=float(data.get("sequence_time") or 0.0),
            source_time=float(data.get("source_time") or 0.0),
            source_file=str(data.get("source_file") or ""),
            asset_id=str(data.get("asset_id") or ""),
            keep_reason=str(data.get("keep_reason") or "unknown"),
            speed=float(data.get("speed") or 1.0),
            protected=bool(data.get("protected")),
            recommendation_ids=[str(x) for x in (data.get("recommendation_ids") or [])],
            segment_ids=[str(x) for x in (data.get("segment_ids") or [])],
            marker_names=[str(x) for x in (data.get("marker_names") or [])],
            note=str(data.get("note") or ""),
            sequence_name=str(data.get("sequence_name") or ""),
            frame_kind=kind if kind in FRAME_KINDS else "clip_sample",
            reason=str(data.get("reason") or ""),
            transcript=str(data.get("transcript") or ""),
            audio_events=[
                dict(entry) for entry in (data.get("audio_events") or [])
                if isinstance(entry, dict)
            ],
            audio_types=[str(x) for x in (data.get("audio_types") or [])],
            visual_event_ids=[str(x) for x in (data.get("visual_event_ids") or [])],
            environment=str(data.get("environment") or ""),
            actions=[str(x) for x in (data.get("actions") or [])],
            entities=[str(x) for x in (data.get("entities") or [])],
            threats=[str(x) for x in (data.get("threats") or [])],
            importance=str(data.get("importance") or ""),
            ui_flags=[str(x) for x in (data.get("ui_flags") or [])],
            applied_edits=[
                dict(entry) for entry in (data.get("applied_edits") or [])
                if isinstance(entry, dict)
            ],
            priority=float(data.get("priority") or 0.0),
            clip_duration=float(data.get("clip_duration") or 0.0),
        )


@dataclass
class ReviewSet:
    """Every review frame for one rough cut, plus how it was produced."""

    sequence_name: str = ""
    frames: list[ReviewFrame] = field(default_factory=list)
    generated_at: str = ""
    cut_duration: float = 0.0
    warnings: list[str] = field(default_factory=list)
    #: True when frames were actually written; False for a dry listing.
    exported: bool = False

    def __len__(self) -> int:
        return len(self.frames)

    def frame(self, frame_id: str) -> Optional[ReviewFrame]:
        for frame in self.frames:
            if frame.frame_id == frame_id:
                return frame
        return None

    def stats(self) -> dict:
        by_reason: dict = {}
        by_kind: dict = {}
        for frame in self.frames:
            by_reason[frame.keep_reason] = by_reason.get(frame.keep_reason, 0) + 1
            by_kind[frame.frame_kind] = by_kind.get(frame.frame_kind, 0) + 1
        return {
            "frames": len(self.frames),
            "cut_duration": round(self.cut_duration, 2),
            "by_keep_reason": by_reason,
            "by_frame_kind": by_kind,
            "with_recommendations": sum(
                1 for frame in self.frames if frame.recommendation_ids
            ),
            "with_applied_edits": sum(
                1 for frame in self.frames if frame.applied_edits
            ),
        }

    def to_dict(self) -> dict:
        return {
            "sequence_name": self.sequence_name,
            "generated_at": self.generated_at,
            "exported": self.exported,
            "stats": self.stats(),
            "warnings": list(self.warnings),
            "frames": [frame.to_dict() for frame in self.frames],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewSet":
        return cls(
            sequence_name=str(data.get("sequence_name") or ""),
            frames=[ReviewFrame.from_dict(f) for f in (data.get("frames") or [])],
            generated_at=str(data.get("generated_at") or ""),
            cut_duration=float((data.get("stats") or {}).get("cut_duration") or 0.0),
            warnings=[str(w) for w in (data.get("warnings") or [])],
            exported=bool(data.get("exported")),
        )


def plan_frames(
    plan: RoughCutPlan, *, position: float = DEFAULT_POSITION
) -> list[ReviewFrame]:
    """Decide which frames to export, without touching the filesystem.

    Pure, so the choice of frames is testable on its own and a caller can list
    what *would* be exported before spending the extraction time.
    """
    position = max(0.0, min(1.0, float(position)))
    frames: list[ReviewFrame] = []

    for placement in plan.placements:
        if placement.source_duration <= 0:
            continue
        source_time = placement.source_in + placement.source_duration * position
        sequence_time = placement.source_to_sequence(source_time)
        if sequence_time is None:
            sequence_time = placement.sequence_midpoint

        inside = [
            marker.name for marker in plan.markers
            if placement.sequence_start <= marker.time < placement.sequence_end
        ]
        frames.append(ReviewFrame(
            frame_id=f"rf_{placement.placement_id[2:]}",
            placement_id=placement.placement_id,
            path="",
            sequence_time=sequence_time,
            source_time=source_time,
            source_file=placement.source_file,
            asset_id=placement.asset_id,
            keep_reason=placement.keep_reason,
            speed=placement.speed,
            protected=placement.protected,
            recommendation_ids=list(placement.recommendation_ids),
            segment_ids=list(placement.segment_ids),
            marker_names=sorted(set(inside)),
            note=placement.notes,
            sequence_name=plan.sequence_name,
            frame_kind="clip_sample",
            reason="representative frame for the clip",
            clip_duration=placement.sequence_duration,
        ))
    return frames


def export_frames(
    plan: RoughCutPlan,
    config: EditingConfig,
    *,
    position: float = DEFAULT_POSITION,
    width: int = DEFAULT_WIDTH,
    out_dir: Optional[Path] = None,
    write_manifest: bool = True,
    frames: Optional[Sequence[ReviewFrame]] = None,
) -> ReviewSet:
    """Extract the planned frames and write the manifest.

    Frames that cannot be extracted are dropped from the set with a warning
    rather than failing the export -- one unreadable source in a long cut
    should not cost the whole review.

    ``frames`` overrides the default one-per-clip plan. That is the seam the
    critic uses: ``editing.critic.frames`` decides *which* moments deserve a
    look and attaches the context, and this function stays the single place
    that talks to FFmpeg and writes the manifest.
    """
    directory = Path(out_dir) if out_dir is not None else (
        config.review_dir / _slugify(plan.sequence_name)
    )
    directory.mkdir(parents=True, exist_ok=True)

    review = ReviewSet(
        sequence_name=plan.sequence_name,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        cut_duration=plan.total_duration,
    )
    planned = (
        list(frames) if frames is not None
        else plan_frames(plan, position=position)
    )
    if not planned:
        review.warnings.append("The plan contains no clips, so there is "
                               "nothing to review.")
        return review

    for index, frame in enumerate(planned):
        if not frame.sequence_name:
            frame.sequence_name = plan.sequence_name
        target = directory / (
            f"{index:03d}_{frame.frame_kind}_{frame.sequence_time:08.2f}.jpg"
        )
        try:
            written = ff.extract_frame(
                frame.source_file, frame.source_time, target,
                width=width, quality=3, ffmpeg=config.ffmpeg,
            )
        except ToolMissingError as exc:
            review.warnings.append(f"{exc.message}. {exc.hint}")
            break
        except Exception as exc:  # noqa: BLE001 - one bad clip is not fatal
            review.warnings.append(
                f"Could not export a frame for {frame.placement_id}: {exc}"
            )
            continue

        if written is None:
            review.warnings.append(
                f"No frame at {frame.source_time:.2f}s of "
                f"{Path(frame.source_file).name}."
            )
            continue

        frame.path = str(written)
        review.frames.append(frame)

    review.exported = bool(review.frames)
    if write_manifest:
        write_review(review, directory / "review.json")
    return review


def write_review(review: ReviewSet, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(review.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def load_review(path: str | Path) -> ReviewSet:
    return ReviewSet.from_dict(
        json.loads(Path(path).read_text(encoding="utf-8"))
    )


def _slugify(name: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in str(name)
    )
    return safe.strip("_").lower() or "roughcut"

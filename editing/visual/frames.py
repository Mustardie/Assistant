"""Getting the planned frames out of a file, and the motion signal in.

The sampling planner decides *which* timestamps matter; this module actually
produces them. It sits between the pure planner and the model client so both of
those stay free of subprocess handling.

Two design choices worth stating:

**Frames are extracted per window, not per file.** A 40-minute recording plans
to well over a thousand frames; writing them all up front costs gigabytes of
JPEG and throws most of it away when the cache already holds the answers.
Extracting a window's frames immediately before that window is analysed means a
fully-cached re-run touches ffmpeg zero times.

**Frames are deleted after use by default.** They are reproducible from the
source file and the sampling config, so keeping them is a debugging option
(``keep_frames``), not the normal path.

The motion probe is cached on the file fingerprint plus the probe settings, so
the expensive whole-file scan happens once per recording rather than once per
run.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Sequence

from editing import ffmpeg as ff
from editing.cache import Cache
from editing.config import EditingConfig, SamplingConfig
from editing.errors import ToolMissingError
from editing.fingerprint import Fingerprint
from editing.visual.sampling import MotionPoint, SampleWindow

logger = logging.getLogger("nova.editing.visual.frames")


@dataclass
class ExtractedFrames:
    """The frames actually written for one window.

    ``times`` and ``paths`` stay paired and can be shorter than the window
    asked for: a timestamp past the last decodable frame yields nothing, which
    is normal at the end of a file and must not fail the run.
    """

    window: SampleWindow
    times: list[float] = field(default_factory=list)
    paths: list[Path] = field(default_factory=list)
    directory: Optional[Path] = None

    def __len__(self) -> int:
        return len(self.paths)

    @property
    def ok(self) -> bool:
        return bool(self.paths)

    def cleanup(self) -> None:
        """Remove this window's frame directory. Safe to call twice."""
        if self.directory is None:
            return
        shutil.rmtree(self.directory, ignore_errors=True)
        self.directory = None


class FrameSource(Protocol):
    """What the analyzer needs from a frame provider.

    Narrow on purpose: tests substitute a stub that returns fixture images
    without FFmpeg, and the analyzer cannot tell the difference.
    """

    def extract(self, path: str, window: SampleWindow) -> ExtractedFrames: ...


class FFmpegFrameSource:
    """Real frame extraction, one JPEG per planned timestamp."""

    def __init__(
        self,
        config: EditingConfig,
        sampling: SamplingConfig,
        *,
        root: Optional[Path] = None,
        keep_frames: bool = False,
    ):
        self.config = config
        self.sampling = sampling.validated()
        self.root = Path(root) if root is not None else config.frames_dir
        self.keep_frames = keep_frames

    def extract(self, path: str, window: SampleWindow) -> ExtractedFrames:
        directory = self.root / f"w{window.index:05d}_{window.start:09.3f}"
        pairs = ff.extract_frames(
            path,
            window.frame_times,
            directory,
            width=self.sampling.frame_width,
            quality=self.sampling.frame_quality,
            ffmpeg=self.config.ffmpeg,
            prefix="f",
        )
        if not pairs:
            logger.warning(
                "No frames could be extracted for %.2f-%.2fs of %s",
                window.start, window.end, Path(path).name,
            )
            shutil.rmtree(directory, ignore_errors=True)
            return ExtractedFrames(window=window, directory=None)

        return ExtractedFrames(
            window=window,
            times=[time for time, _ in pairs],
            paths=[frame for _, frame in pairs],
            # Handing back no directory when frames are kept means cleanup()
            # becomes a no-op, which is exactly what --keep-frames should do.
            directory=None if self.keep_frames else directory,
        )


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

def probe_motion(
    asset_path: str,
    *,
    config: EditingConfig,
    sampling: SamplingConfig,
    cache: Optional[Cache] = None,
    mark: Optional[Fingerprint] = None,
    enabled: bool = True,
) -> list[MotionPoint]:
    """The motion signal for one file, cached on its fingerprint.

    Returns ``[]`` when motion data is unavailable for any reason other than a
    missing FFmpeg. That is a deliberate soft failure: without motion the
    planner falls back to uniform windows, which is less targeted but still
    covers the whole recording. A missing FFmpeg is re-raised because nothing
    else in the pipeline will work either.
    """
    if not enabled:
        return []

    sampling = sampling.validated()
    key = None
    if cache is not None and mark is not None:
        key = cache.key(
            "motion",
            file=mark.cache_key_part(),
            interval=sampling.motion_probe_interval,
            method="keyframe_scene_score",
        )
        cached = cache.get("motion", key)
        if cached is not None:
            return [
                MotionPoint(time=float(point[0]), score=float(point[1]))
                for point in cached
            ]

    try:
        samples = ff.motion_samples(
            asset_path,
            ffmpeg=config.ffmpeg,
            keyframes_only=True,
            interval=sampling.motion_probe_interval,
        )
    except ToolMissingError:
        raise
    except Exception as exc:  # noqa: BLE001 - motion is an optimisation
        logger.warning("Motion probe failed for %s: %s", asset_path, exc)
        return []

    points = [MotionPoint(time=sample.time, score=sample.score) for sample in samples]

    if cache is not None and key is not None:
        # Stored as pairs rather than dicts: a two-hour recording produces tens
        # of thousands of readings and the key names would dominate the file.
        cache.put(
            "motion", key,
            [[round(point.time, 3), round(point.score, 4)] for point in points],
            meta={"path": asset_path, "samples": len(points)},
        )
    return points


def motion_stats(points: Sequence[MotionPoint], threshold: float) -> dict:
    """Summary of a motion scan, for the CLI and for debugging a sampling plan."""
    if not points:
        return {"samples": 0, "above_threshold": 0, "peak": 0.0, "mean": 0.0}
    scores = [point.score for point in points]
    return {
        "samples": len(points),
        "above_threshold": sum(1 for score in scores if score >= threshold),
        "peak": round(max(scores), 4),
        "mean": round(sum(scores) / len(scores), 4),
    }

"""Turning a media file into ``VisualEvent`` records.

The orchestration, in order, for one file:

1. probe motion (cached on the file fingerprint),
2. plan sampling windows from the duration and that motion,
3. for each window: cache lookup -> extract frames -> ask the model -> coerce
   the answer through the schema -> cache the result,
4. return the events, in time order.

Three rules this module exists to enforce:

**Nothing uncoerced escapes.** Whatever the model says goes through
``VisualEvent.from_dict``, so an event always has a valid environment, a valid
importance, a confidence in 0..1 and a suggested range inside its own window.
The model's original wording is preserved in the ``raw_*`` fields.

**Failures are recorded, not swallowed and not fatal.** A window whose model
call fails becomes an event carrying ``error`` with zero confidence, so the
timeline shows an honest hole rather than silently skipping five seconds of
footage. Failed windows are never cached -- a server that was down at 3am must
not poison the results forever.

**A fully cached re-run costs nothing.** No frames are extracted and no model
call is made for a window that is already in the cache, which is what makes
re-running after a config tweak practical.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from editing.cache import Cache
from editing.config import EditingConfig, SamplingConfig
from editing.errors import EditingError, ModelError
from editing.fingerprint import Fingerprint, fingerprint
from editing.schema import MediaAsset, TimeRange, VisualEvent, short_hash
from editing.visual import prompt as prompt_module
from editing.visual.frames import (
    ExtractedFrames, FFmpegFrameSource, FrameSource, motion_stats, probe_motion,
)
from editing.visual.qwen import MockVisionModel, VisionModel, build_model
from editing.visual.sampling import (
    MotionPoint, SampleWindow, coverage_gaps, plan_summary, plan_windows,
)

logger = logging.getLogger("nova.editing.visual.analyzer")

#: Called as ``progress(done, total, event)`` after each window resolves.
ProgressHook = Callable[[int, int, VisualEvent], None]


@dataclass
class AnalysisResult:
    """Everything one file's analysis produced, including how it went."""

    asset_id: str
    source_file: str
    events: list[VisualEvent] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    motion: dict = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0
    failures: int = 0
    gaps: list = field(default_factory=list)
    elapsed: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.events) and self.failures < len(self.events)

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "source_file": self.source_file,
            "event_count": len(self.events),
            "plan": dict(self.plan),
            "motion": dict(self.motion),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "failures": self.failures,
            "gaps": list(self.gaps),
            "elapsed": round(self.elapsed, 2),
            "warnings": list(self.warnings),
            "events": [event.to_dict() for event in self.events],
        }


class VisualAnalyzer:
    """Analyses media files into visual events."""

    def __init__(
        self,
        config: EditingConfig,
        sampling: SamplingConfig,
        *,
        cache: Optional[Cache] = None,
        model: Optional[VisionModel] = None,
        frame_source: Optional[FrameSource] = None,
        keep_frames: bool = False,
        use_motion: bool = True,
    ):
        self.config = config
        self.sampling = sampling.validated()
        self.cache = cache
        self.model = model if model is not None else build_model(config)
        self.keep_frames = keep_frames
        self.use_motion = use_motion
        self._frame_source = frame_source

    # -- naming ---------------------------------------------------------

    @property
    def model_name(self) -> str:
        """The name that goes into cache keys and event provenance.

        Taken from the model object rather than the config so a mock run can
        never share cache entries with a real one.
        """
        return getattr(self.model, "name", None) or self.config.vision_model

    def frame_source_for(self, asset: MediaAsset) -> FrameSource:
        if self._frame_source is not None:
            return self._frame_source
        return FFmpegFrameSource(
            self.config,
            self.sampling,
            root=self.config.frames_dir / asset.asset_id,
            keep_frames=self.keep_frames,
        )

    # -- analysis -------------------------------------------------------

    def analyze_asset(
        self,
        asset: MediaAsset,
        *,
        mark: Optional[Fingerprint] = None,
        progress: Optional[ProgressHook] = None,
        max_windows: Optional[int] = None,
    ) -> AnalysisResult:
        """Analyse one file into visual events."""
        started = time.time()
        result = AnalysisResult(asset_id=asset.asset_id, source_file=asset.path)

        if asset.duration <= 0:
            result.warnings.append(
                f"{asset.filename} has no readable duration"
                + (f" ({asset.probe_error})" if asset.probe_error else "")
                + "; nothing to analyse."
            )
            result.elapsed = time.time() - started
            return result

        mark = mark or self._mark_for(asset)

        motion = probe_motion(
            asset.path,
            config=self.config,
            sampling=self.sampling,
            cache=self.cache,
            mark=mark,
            enabled=self.use_motion,
        )
        result.motion = motion_stats(motion, self.sampling.motion_threshold)
        if self.use_motion and not motion:
            result.warnings.append(
                "No motion signal available, so sampling is uniform: fast "
                "moments get no extra frames."
            )

        windows = plan_windows(asset.duration, self.sampling, motion=motion)
        if max_windows is not None:
            windows = windows[: max(0, int(max_windows))]
        result.plan = plan_summary(windows, asset.duration)
        result.gaps = coverage_gaps(windows, asset.duration)
        if result.gaps:
            result.warnings.append(
                f"{len(result.gaps)} stretch(es) of the recording are not "
                "covered by any window."
            )

        if not windows:
            result.elapsed = time.time() - started
            return result

        events = self._run_windows(asset, windows, mark, result, progress)
        events.sort(key=lambda event: (event.start, event.end))
        result.events = events
        result.elapsed = time.time() - started
        return result

    def analyze_assets(
        self,
        assets: Sequence[MediaAsset],
        *,
        progress: Optional[ProgressHook] = None,
        max_windows: Optional[int] = None,
    ) -> list[AnalysisResult]:
        """Analyse several files, one after another.

        Sequential by design: the concurrency that helps is *within* a file
        (see ``vision_concurrency``), and analysing two files at once would
        double frame extraction I/O against the same disk for no gain.
        """
        return [
            self.analyze_asset(asset, progress=progress, max_windows=max_windows)
            for asset in assets
        ]

    # -- per-window -----------------------------------------------------

    def _run_windows(
        self,
        asset: MediaAsset,
        windows: Sequence[SampleWindow],
        mark: Optional[Fingerprint],
        result: AnalysisResult,
        progress: Optional[ProgressHook],
    ) -> list[VisualEvent]:
        source = self.frame_source_for(asset)
        total = len(windows)
        done = 0
        events: list[VisualEvent] = []

        def handle(window: SampleWindow) -> VisualEvent:
            return self._analyze_window(asset, window, mark, source, result)

        workers = max(1, int(self.config.vision_concurrency))
        if workers > 1:
            # Frame extraction is I/O bound and the model call is network
            # bound, so threads (not processes) are the right tool; the cache's
            # writes are atomic, which is what makes this safe.
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for event in pool.map(handle, windows):
                    events.append(event)
                    done += 1
                    if progress is not None:
                        progress(done, total, event)
        else:
            for window in windows:
                event = handle(window)
                events.append(event)
                done += 1
                if progress is not None:
                    progress(done, total, event)

        return events

    def _analyze_window(
        self,
        asset: MediaAsset,
        window: SampleWindow,
        mark: Optional[Fingerprint],
        source: FrameSource,
        result: AnalysisResult,
    ) -> VisualEvent:
        key = self._cache_key(asset, window, mark)

        if key is not None and self.cache is not None:
            cached = self.cache.get("visual", key)
            if cached is not None:
                result.cache_hits += 1
                return VisualEvent.from_dict(cached)

        result.cache_misses += 1

        frames: Optional[ExtractedFrames] = None
        try:
            frames = source.extract(asset.path, window)
            if not frames.ok:
                return self._failed_event(
                    asset, window,
                    "No frames could be extracted for this window.",
                )

            answer = self.model.analyze(
                frames.paths,
                system=prompt_module.SYSTEM_PROMPT,
                user=prompt_module.build_user_prompt(
                    window_start=window.start,
                    window_end=window.end,
                    frame_times=frames.times,
                    source_name=asset.filename,
                    sampling=self.sampling,
                ),
            )
            event = self._build_event(asset, window, frames, answer)

        except ModelError as exc:
            logger.warning(
                "Vision model failed on %.2f-%.2fs of %s: %s",
                window.start, window.end, asset.filename, exc.message,
            )
            return self._failed_event(asset, window, exc.message)
        except EditingError as exc:
            logger.warning(
                "Window %.2f-%.2fs of %s failed: %s",
                window.start, window.end, asset.filename, exc.message,
            )
            return self._failed_event(asset, window, exc.message)
        except Exception as exc:  # noqa: BLE001 - one window must not kill a run
            logger.exception("Unexpected failure analysing a window of %s", asset.path)
            return self._failed_event(asset, window, f"{type(exc).__name__}: {exc}")
        finally:
            if frames is not None:
                frames.cleanup()

        if key is not None and self.cache is not None:
            self.cache.put(
                "visual", key, event.to_dict(),
                meta={
                    "asset_id": asset.asset_id,
                    "path": asset.path,
                    "window": [round(window.start, 3), round(window.end, 3)],
                    "model": self.model_name,
                },
            )
        return event

    # -- keys and construction -----------------------------------------

    def _cache_key(
        self,
        asset: MediaAsset,
        window: SampleWindow,
        mark: Optional[Fingerprint],
    ) -> Optional[str]:
        """Key for one window's analysis, or None when it cannot be trusted.

        Without a fingerprint there is nothing tying a cached answer to the
        file's current content, so caching is skipped rather than risked.
        """
        if self.cache is None or mark is None:
            return None
        return self.cache.key(
            "visual",
            file=mark.cache_key_part(),
            model=self.model_name,
            sampling=self.sampling.cache_key_part(),
            window=window.cache_key_part(),
        )

    def _build_event(
        self,
        asset: MediaAsset,
        window: SampleWindow,
        frames: ExtractedFrames,
        answer: dict,
    ) -> VisualEvent:
        """Coerce a model answer into a schema-valid event.

        The window's own times overwrite anything the model said about start
        and end: the model is being asked what it sees, not when it saw it, and
        a hallucinated timestamp would corrupt the whole timeline.
        """
        payload = dict(answer or {})
        payload.update({
            "event_id": self._event_id(asset, window),
            "source_file": asset.path,
            "asset_id": asset.asset_id,
            "start": window.start,
            "end": window.end,
            "model": self.model_name,
            "frame_times": list(frames.times),
            "dense": window.dense,
            "motion_score": window.motion_score,
        })
        event = VisualEvent.from_dict(payload)
        event.suggested_range = self._clamp_range(event.suggested_range, window)
        return event

    @staticmethod
    def _clamp_range(candidate: TimeRange, window: SampleWindow) -> TimeRange:
        """Keep the suggested range inside the window it describes.

        Models routinely answer with 0-8 (window-relative) or with a range
        drifting past the window. Clamping is the honest fix; a range outside
        its own window would cut footage the model never looked at.
        """
        start = max(window.start, min(candidate.start, window.end))
        end = max(start, min(candidate.end, window.end))
        if end - start < 0.05:
            return TimeRange(start=window.start, end=window.end)
        return TimeRange(start=start, end=end)

    @staticmethod
    def _event_id(asset: MediaAsset, window: SampleWindow) -> str:
        return "e_" + short_hash(asset.asset_id, round(window.start, 3),
                                 round(window.end, 3))

    def _failed_event(
        self, asset: MediaAsset, window: SampleWindow, reason: str
    ) -> VisualEvent:
        """A visible hole in the timeline, rather than a missing five seconds."""
        return VisualEvent(
            event_id=self._event_id(asset, window),
            source_file=asset.path,
            asset_id=asset.asset_id,
            start=window.start,
            end=window.end,
            confidence=0.0,
            environment="unknown",
            actions=["unknown"],
            importance="boring",
            suggested_range=TimeRange(start=window.start, end=window.end),
            notes=f"Not analysed: {reason}",
            model=self.model_name,
            frame_times=list(window.frame_times),
            dense=window.dense,
            motion_score=window.motion_score,
            error=reason,
        )

    @staticmethod
    def _mark_for(asset: MediaAsset) -> Optional[Fingerprint]:
        try:
            return fingerprint(asset.path)
        except Exception:  # noqa: BLE001 - offline media is a normal state
            logger.debug("No fingerprint for %s; caching disabled", asset.path)
            return None


def build_analyzer(
    config: EditingConfig,
    sampling: SamplingConfig,
    *,
    cache: Optional[Cache] = None,
    model: Optional[VisionModel] = None,
    **kwargs,
) -> VisualAnalyzer:
    """Convenience constructor used by the CLI and the pipeline."""
    return VisualAnalyzer(config, sampling, cache=cache, model=model, **kwargs)


__all__ = [
    "AnalysisResult", "VisualAnalyzer", "build_analyzer", "MockVisionModel",
    "MotionPoint", "ProgressHook",
]

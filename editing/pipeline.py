"""The orchestration each CLI command calls into.

The CLI is argument parsing and printing; everything it actually *does* is
here, so the same steps can be driven from a script, a test, or later from the
agent loop without going through a command line.

One session object (``Pipeline``) holds the config, cache and model so a
command like ``run`` can do discovery, transcripts, analysis and alignment
without rebuilding any of them, and so cache statistics accumulate across the
whole run rather than resetting per step.

Intermediate results are written to disk at each stage (``assets.json``,
``visual/<asset_id>.json``, ``transcripts/<asset_id>.json``) rather than only
at the end. A four-hour analysis that is interrupted must not lose everything,
and each file is independently inspectable when the output looks wrong.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from editing import align
from editing.cache import Cache, build_cache
from editing.audio.analyzer import AudioAnalyzer, AudioResult
from editing.config import AudioConfig, EditingConfig, SamplingConfig
from editing.discovery import discover
from editing.errors import EditingError, FootageError
from editing.fingerprint import fingerprint
from editing.premiere_link import ProjectSnapshot
from editing.recommend import report as report_module
from editing.recommend.planner import PlannerOptions, plan_recommendations
from editing.recommend.premiere_plan import DraftPlan, build_and_dry_run
from editing.recommend.schema import RecommendationSet
from editing.schema import (
    AudioEvent, MediaAsset, StructureTimeline, VisualEvent,
)
from editing.transcripts import store as transcript_store
from editing.visual.analyzer import AnalysisResult, VisualAnalyzer
from editing.visual.qwen import build_model

logger = logging.getLogger("nova.editing.pipeline")

#: ``say(message)`` -- how the pipeline reports progress. The CLI passes print.
Reporter = Callable[[str], None]


def _quiet(_message: str) -> None:
    return None


@dataclass
class Pipeline:
    """One editing-structure session."""

    config: EditingConfig
    sampling: SamplingConfig
    audio: AudioConfig = field(default_factory=AudioConfig)
    cache: Optional[Cache] = None
    say: Reporter = _quiet
    bridge: object = None
    model: object = None
    #: Injected in tests to avoid FFmpeg; None means the real reader.
    audio_source: object = None

    assets: list[MediaAsset] = field(default_factory=list)
    project: Optional[ProjectSnapshot] = None

    def __post_init__(self) -> None:
        self.sampling = self.sampling.validated()
        self.audio = self.audio.validated()
        if self.cache is None:
            self.cache = build_cache(self.config)
        self.config.ensure_dirs()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(
        self,
        *,
        folder: Optional[str] = None,
        files: Optional[Sequence[str]] = None,
        recursive: bool = True,
        use_premiere: Optional[bool] = None,
        save: bool = True,
    ) -> list[MediaAsset]:
        """Find footage, probe it, map it to Premiere, and remember it."""
        assets, project = discover(
            config=self.config,
            folder=folder,
            files=files,
            cache=self.cache,
            recursive=recursive,
            use_premiere=use_premiere,
            bridge=self.bridge,
        )
        self.assets = assets
        self.project = project

        if project.available:
            matched = sum(1 for asset in assets if asset.premiere.matched)
            self.say(
                f"Premiere: {project.project_name or 'project open'}, "
                f"{matched}/{len(assets)} file(s) mapped to project items."
            )
        elif project.note:
            self.say(f"Premiere: {project.note}")

        for asset in assets:
            if asset.probe_error:
                self.say(f"  ! {asset.filename}: {asset.probe_error}")

        if save:
            self.write_assets()
        return assets

    def write_assets(self) -> Path:
        target = self.config.assets_file
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "project": self.project.to_dict() if self.project else {},
            "count": len(self.assets),
            "assets": [asset.to_dict() for asset in self.assets],
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    def load_assets(self) -> list[MediaAsset]:
        """Reload the last discovery, so later commands need no arguments."""
        target = self.config.assets_file
        if not target.exists():
            raise FootageError(
                "No footage has been discovered yet",
                hint="Run `python -m editing.cli discover --folder <path>` first.",
            )
        document = json.loads(target.read_text(encoding="utf-8"))
        self.assets = [
            MediaAsset.from_dict(entry) for entry in (document.get("assets") or [])
        ]
        return self.assets

    def ensure_assets(self, **kwargs) -> list[MediaAsset]:
        """Discover if arguments were given, otherwise reuse the last scan."""
        if kwargs.get("folder") or kwargs.get("files"):
            return self.discover(**kwargs)
        if self.assets:
            return self.assets
        try:
            return self.load_assets()
        except FootageError:
            return self.discover(**kwargs)

    def select(self, assets: Sequence[MediaAsset], pattern: str) -> list[MediaAsset]:
        """Narrow a list by asset id, filename or path substring."""
        if not pattern:
            return list(assets)
        needle = pattern.strip().lower()
        exact = [
            asset for asset in assets
            if asset.asset_id == pattern or asset.filename.lower() == needle
        ]
        if exact:
            return exact
        return [
            asset for asset in assets
            if needle in asset.filename.lower() or needle in asset.path.lower()
        ]

    # ------------------------------------------------------------------
    # Transcripts
    # ------------------------------------------------------------------

    def transcripts(
        self,
        assets: Optional[Sequence[MediaAsset]] = None,
        *,
        use_premiere: Optional[bool] = None,
        use_sidecar: bool = True,
        refresh: bool = False,
    ) -> dict:
        """Resolve every asset's transcript. Returns ``{asset_id: resolution}``."""
        assets = list(assets if assets is not None else self.assets)
        out: dict = {}
        for asset in assets:
            resolution = transcript_store.resolve(
                self.config, asset,
                cache=self.cache,
                bridge=self.bridge,
                use_premiere=use_premiere,
                use_sidecar=use_sidecar,
                refresh=refresh,
            )
            out[asset.asset_id] = resolution
            if resolution.found:
                stale = " (stale)" if resolution.stale else ""
                self.say(
                    f"  {asset.filename}: {len(resolution.transcript)} line(s) "
                    f"from {resolution.origin}{stale}"
                )
            else:
                self.say(f"  {asset.filename}: no transcript. {resolution.note}")
        return out

    def import_transcript(self, asset: MediaAsset, path: str):
        """Import one external transcript file for one asset."""
        transcript = transcript_store.import_file(
            self.config, asset, path, cache=self.cache
        )
        self.say(
            f"Imported {len(transcript)} line(s) from {Path(path).name} "
            f"for {asset.filename} (format: {transcript.source})."
        )
        return transcript

    # ------------------------------------------------------------------
    # Visual analysis
    # ------------------------------------------------------------------

    def analyzer(self, **kwargs) -> VisualAnalyzer:
        if self.model is None:
            self.model = build_model(self.config)
        return VisualAnalyzer(
            self.config, self.sampling,
            cache=self.cache, model=self.model, **kwargs,
        )

    def analyze(
        self,
        assets: Optional[Sequence[MediaAsset]] = None,
        *,
        keep_frames: bool = False,
        use_motion: bool = True,
        max_windows: Optional[int] = None,
        show_progress: bool = True,
    ) -> dict:
        """Analyse assets into visual events. Returns ``{asset_id: result}``."""
        assets = list(assets if assets is not None else self.assets)
        analyzer = self.analyzer(keep_frames=keep_frames, use_motion=use_motion)
        results: dict = {}

        for asset in assets:
            self.say(f"Analysing {asset.filename} ({asset.duration:.0f}s)...")
            hook = self._progress_hook(asset) if show_progress else None
            result = analyzer.analyze_asset(
                asset, progress=hook, max_windows=max_windows
            )
            results[asset.asset_id] = result
            self.write_events(asset, result)

            self.say(
                f"  {len(result.events)} event(s) in {result.elapsed:.1f}s "
                f"({result.cache_hits} cached, {result.cache_misses} analysed"
                + (f", {result.failures} failed" if result.failures else "")
                + ")"
            )
            for warning in result.warnings:
                self.say(f"  ! {warning}")
        return results

    def _progress_hook(self, asset: MediaAsset):
        def hook(done: int, total: int, event: VisualEvent) -> None:
            # Report sparsely: a 400-window file would otherwise print 400
            # lines and bury the warnings that matter.
            if done == total or done % 25 == 0:
                self.say(f"    {done}/{total} windows ({event.end:.0f}s)")
        return hook

    def write_events(self, asset: MediaAsset, result: AnalysisResult) -> Path:
        target = self.config.visual_dir / f"{asset.asset_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_events(self, asset: MediaAsset) -> list[VisualEvent]:
        """Read an asset's saved events. Empty when it has not been analysed."""
        target = self.config.visual_dir / f"{asset.asset_id}.json"
        if not target.exists():
            return []
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable events file %s: %s", target, exc)
            return []
        return [
            VisualEvent.from_dict(entry) for entry in (document.get("events") or [])
        ]

    # ------------------------------------------------------------------
    # Audio
    # ------------------------------------------------------------------

    def audio_analyzer(self) -> AudioAnalyzer:
        return AudioAnalyzer(
            self.config, self.audio, cache=self.cache, source=self.audio_source
        )

    def analyze_audio(
        self,
        assets: Optional[Sequence[MediaAsset]] = None,
        *,
        transcripts: Optional[dict] = None,
        refresh: bool = False,
    ) -> dict:
        """Analyse audio into events. Returns ``{asset_id: AudioResult}``."""
        assets = list(assets if assets is not None else self.assets)
        if transcripts is None:
            resolved = self.transcripts(assets)
            transcripts = {
                asset_id: resolution.transcript
                for asset_id, resolution in resolved.items()
                if resolution.found
            }

        analyzer = self.audio_analyzer()
        results: dict = {}
        for asset in assets:
            result = analyzer.analyze_asset(
                asset,
                transcript=transcripts.get(asset.asset_id),
                refresh=refresh,
            )
            results[asset.asset_id] = result
            self.write_audio(asset, result)
            self.say(
                f"  {asset.filename}: {len(result.events)} audio event(s)"
                + (" (cached)" if result.cached else "")
            )
            for warning in result.warnings:
                self.say(f"    ! {warning}")
        return results

    def write_audio(self, asset: MediaAsset, result: AudioResult):
        target = self.config.audio_dir / f"{asset.asset_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_audio_events(self, asset: MediaAsset) -> list:
        """Read an asset's saved audio events. Empty when never analysed."""
        target = self.config.audio_dir / f"{asset.asset_id}.json"
        if not target.exists():
            return []
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable audio file %s: %s", target, exc)
            return []
        return [
            AudioEvent.from_dict(event) for event in (document.get("events") or [])
        ]

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    def timeline(
        self,
        assets: Optional[Sequence[MediaAsset]] = None,
        *,
        merge_similar: bool = True,
        max_segment_seconds: float = 30.0,
        usable_threshold: float = align.DEFAULT_USABLE_THRESHOLD,
        use_premiere: Optional[bool] = None,
        refresh_transcripts: bool = False,
    ) -> StructureTimeline:
        """Build the combined structure timeline from what is already on disk.

        Deliberately does not run analysis: building a timeline should be
        instant and repeatable while tuning merge and threshold settings. Files
        with no saved events simply contribute their transcript, and say so in
        the timeline's warnings.
        """
        assets = list(assets if assets is not None else self.assets)
        events_by_asset: dict = {}
        audio_by_asset: dict = {}
        transcripts: dict = {}
        sources: dict = {}
        warnings: list[str] = []

        resolutions = self.transcripts(
            assets, use_premiere=use_premiere, refresh=refresh_transcripts
        )

        for asset in assets:
            events = self.load_events(asset)
            events_by_asset[asset.asset_id] = events
            if not events:
                warnings.append(
                    f"{asset.filename}: no visual analysis on disk. Run "
                    "`analyze` for it."
                )
            audio_by_asset[asset.asset_id] = self.load_audio_events(asset)

            resolution = resolutions.get(asset.asset_id)
            if resolution is not None:
                sources[asset.asset_id] = resolution.to_dict()
                if resolution.found:
                    transcripts[asset.asset_id] = resolution.transcript

        return align.build_timeline(
            assets,
            events_by_asset,
            transcripts,
            audio_by_asset=audio_by_asset,
            sampling=self.sampling,
            model=self.config.vision_model,
            transcript_sources=sources,
            warnings=warnings,
            merge_similar=merge_similar,
            max_segment_seconds=max_segment_seconds,
            usable_threshold=usable_threshold,
        )

    def write_timeline(
        self, timeline: StructureTimeline, *, name: str = "structure"
    ) -> Path:
        self.config.timelines_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.timelines_dir / f"{name}.json"
        target.write_text(
            json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_timeline(self, *, name: str = "structure") -> StructureTimeline:
        target = self.config.timelines_dir / f"{name}.json"
        if not target.exists():
            raise EditingError(
                f"No timeline named '{name}' has been built yet",
                hint="Run `python -m editing.cli timeline` first.",
            )
        return StructureTimeline.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    # ------------------------------------------------------------------
    # Recommendations
    # ------------------------------------------------------------------

    def recommend(
        self,
        timeline: Optional[StructureTimeline] = None,
        *,
        options: Optional[PlannerOptions] = None,
        name: str = "structure",
        save: bool = True,
    ) -> RecommendationSet:
        """Run the layered planner over a timeline."""
        if timeline is None:
            timeline = self.load_timeline(name=name)
        recommendations = plan_recommendations(timeline, options=options)

        stats = recommendations.stats()
        self.say(
            f"{stats['total']} recommendation(s): {stats['accepted']} accepted, "
            f"{len(recommendations.removed())} removed or softened by the "
            "safety pass."
        )
        for warning in recommendations.warnings:
            self.say(f"  ! {warning}")

        if save:
            self.write_recommendations(recommendations, name=name)
        return recommendations

    def write_recommendations(
        self, recommendations: RecommendationSet, *, name: str = "structure"
    ) -> Path:
        self.config.recommendations_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.recommendations_dir / f"{name}.json"
        target.write_text(
            json.dumps(recommendations.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_recommendations(self, *, name: str = "structure") -> RecommendationSet:
        target = self.config.recommendations_dir / f"{name}.json"
        if not target.exists():
            raise EditingError(
                f"No recommendations named '{name}' have been generated yet",
                hint="Run `python -m editing.cli recommend` first.",
            )
        return RecommendationSet.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    # ------------------------------------------------------------------
    # Draft Premiere plan (never executed)
    # ------------------------------------------------------------------

    def draft_plan(
        self,
        recommendations: Optional[RecommendationSet] = None,
        *,
        name: str = "structure",
        save: bool = True,
    ) -> DraftPlan:
        """Convert accepted recommendations and validate them offline.

        Executes nothing. The validation runs against ``premiere.validator``
        at a fixed frame rate, so it needs neither Premiere nor the bridge.
        """
        if recommendations is None:
            recommendations = self.load_recommendations(name=name)

        paths = {asset.asset_id: asset.path for asset in self.assets}
        if not paths:
            try:
                paths = {
                    asset.asset_id: asset.path for asset in self.load_assets()
                }
            except FootageError:
                paths = {}

        draft = build_and_dry_run(recommendations, asset_paths=paths)
        self.say(
            f"Draft plan: {draft.operation_count} operation(s), dry run "
            + ("valid" if draft.valid else "INVALID")
            + f", {len(draft.not_convertible)} recommendation(s) kept without ops."
        )
        if draft.validation_error:
            self.say(f"  ! {draft.validation_error.get('error')}")
        for warning in draft.warnings:
            self.say(f"  ! {warning}")

        if save:
            self.write_plan(draft, name=name)
        return draft

    def write_plan(self, draft: DraftPlan, *, name: str = "structure") -> Path:
        self.config.plans_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.plans_dir / f"{name}.json"
        target.write_text(
            json.dumps(draft.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def write_report(
        self,
        recommendations: RecommendationSet,
        *,
        timeline: Optional[StructureTimeline] = None,
        draft: Optional[DraftPlan] = None,
        name: str = "structure",
        limit: int = 25,
    ) -> Path:
        text = report_module.render(
            recommendations, timeline=timeline, draft=draft, limit=limit
        )
        self.config.recommendations_dir.mkdir(parents=True, exist_ok=True)
        return report_module.write(
            self.config.recommendations_dir / f"{name}.txt", text
        )

    # ------------------------------------------------------------------
    # Whole run
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        folder: Optional[str] = None,
        files: Optional[Sequence[str]] = None,
        recursive: bool = True,
        keep_frames: bool = False,
        use_motion: bool = True,
        max_windows: Optional[int] = None,
        use_premiere: Optional[bool] = None,
        **timeline_kwargs,
    ) -> StructureTimeline:
        """Discovery -> transcripts -> analysis -> timeline, in one call."""
        assets = self.ensure_assets(
            folder=folder, files=files, recursive=recursive,
            use_premiere=use_premiere,
        )
        if not assets:
            raise FootageError(
                "No media files found",
                hint="Check the folder contains video files.",
            )
        self.say(f"Found {len(assets)} file(s).")

        self.say("Resolving transcripts...")
        resolved = self.transcripts(assets, use_premiere=use_premiere)

        self.say("Analysing audio...")
        self.analyze_audio(assets, transcripts={
            asset_id: resolution.transcript
            for asset_id, resolution in resolved.items() if resolution.found
        })

        self.analyze(
            assets,
            keep_frames=keep_frames,
            use_motion=use_motion,
            max_windows=max_windows,
        )

        self.say("Building structure timeline...")
        timeline = self.timeline(assets, use_premiere=use_premiere, **timeline_kwargs)
        return timeline

    def run_full(
        self,
        *,
        planner_options: Optional[PlannerOptions] = None,
        **run_kwargs,
    ) -> tuple[StructureTimeline, RecommendationSet, DraftPlan]:
        """Everything: structure, recommendations, and a validated draft plan.

        Still executes nothing -- the draft is validated and written, and
        applying it stays a separate, human decision.
        """
        timeline = self.run(**run_kwargs)
        self.write_timeline(timeline)
        self.say("Planning recommendations...")
        recommendations = self.recommend(timeline, options=planner_options)
        self.say("Building draft Premiere plan...")
        draft = self.draft_plan(recommendations)
        self.write_report(recommendations, timeline=timeline, draft=draft)
        return timeline, recommendations, draft


def build_pipeline(
    config: EditingConfig,
    sampling: SamplingConfig,
    audio: Optional[AudioConfig] = None,
    *,
    say: Reporter = _quiet,
    use_cache: bool = True,
    bridge=None,
    model=None,
    audio_source=None,
) -> Pipeline:
    return Pipeline(
        config=config,
        sampling=sampling,
        audio=(audio or AudioConfig()).validated(),
        cache=build_cache(config, enabled=use_cache),
        say=say,
        bridge=bridge,
        model=model,
        audio_source=audio_source,
    )

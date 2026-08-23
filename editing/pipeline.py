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
from editing.critic import (
    critic as critic_module, execute as critic_execute, frames as critic_frames,
    plan as critic_plan, report as critic_report, revise as critic_revise,
)
from editing.critic.schema import (
    CriticReport, RevisionPlan, RevisionSet,
)
from editing.discovery import discover
from editing.episode import memory as episode_memory_module
from editing.episode import plan as episode_plan_module
from editing.episode import report as episode_report
from editing.episode.schema import EpisodeMemory, EpisodeRetentionPlan
from editing.errors import EditingError, FootageError
from editing.feedback import collect as feedback_collect
from editing.feedback import export as feedback_export
from editing.feedback import queue as feedback_queue_module
from editing.feedback import report as feedback_report
from editing.feedback import signals as feedback_signals
from editing.feedback import store as feedback_store
from editing.feedback import targets as feedback_targets
from editing.feedback import training as feedback_training
from editing.feedback.schema import (
    FeedbackItem, FeedbackSession, PreferenceSignal, ReviewQueue,
    TrainingSignal,
)
from editing.fingerprint import fingerprint
from editing.premiere_link import ProjectSnapshot
from editing.recommend import report as report_module
from editing.recommend.planner import PlannerOptions, plan_recommendations
from editing.recommend.premiere_plan import DraftPlan, build_and_dry_run
from editing.recommend.schema import RecommendationSet
from editing.render import notes as render_notes
from editing.render import report as render_report
from editing.render import run as render_run
from editing.render import runner as render_runner
from editing.render import store as render_store
from editing.render.schema import RenderConfig, RenderJob, RenderResult
from editing.roughcut import execute as roughcut_execute, review as review_module
from editing.assets import compile as asset_compile
from editing.assets import execute as asset_execute
from editing.assets import indexer as asset_indexer
from editing.assets import library as asset_library
from editing.assets import report as asset_report
from editing.assets.schema import AssetLibrary, AssetPlacementPlan
from editing.style import compile as style_compile, execute as style_execute
from editing.style import presets as style_presets, report as style_report
from editing.style.schema import LayeredEditPlan
from editing.roughcut.build import RoughCutOptions, build_rough_cut
from editing.roughcut.schema import ExecutionReport, RoughCutPlan
from editing.schema import (
    AudioEvent, MediaAsset, StructureTimeline, VisualEvent,
)
from editing.transcribe import run as transcribe_run
from editing.transcribe import store as transcribe_store
from editing.transcribe.backends import check as transcribe_check
from editing.transcribe.schema import (
    TranscriptionBatch, TranscriptionConfig, TranscriptionJob,
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

    # ------------------------------------------------------------------
    # Producing transcripts (Session 10A)
    # ------------------------------------------------------------------
    #
    # ``transcripts()`` above *resolves* a transcript from the durable store,
    # Premiere or a sidecar. These methods *make* one. They are separate verbs
    # on purpose: resolution must stay cheap and side-effect free, while
    # transcription loads a model and can take minutes.

    def transcription_config(self, **overrides) -> TranscriptionConfig:
        """Transcription settings from the environment, overridden by kwargs."""
        base = TranscriptionConfig.from_env()
        clean = {k: v for k, v in overrides.items() if v is not None}
        if clean:
            from dataclasses import replace
            base = replace(base, **clean)
        return base.validated()

    def transcribe_status(
        self, settings: Optional[TranscriptionConfig] = None
    ) -> dict:
        """Whether transcription could run right now, without loading a model."""
        return transcribe_check(settings or self.transcription_config())

    def transcribe_file(
        self,
        path: str,
        *,
        settings: Optional[TranscriptionConfig] = None,
        asset: Optional[MediaAsset] = None,
        force: bool = False,
        extract_audio: bool = False,
        publish: bool = True,
    ) -> TranscriptionJob:
        """Transcribe one media file and publish it for the rest of the layer."""
        return transcribe_run.transcribe_file(
            self.config, path,
            settings=settings or self.transcription_config(),
            cache=self.cache,
            asset=asset,
            force=force,
            extract_audio=extract_audio,
            publish=publish,
            say=self.say,
        )

    def transcribe_folder(
        self,
        folder: str,
        *,
        settings: Optional[TranscriptionConfig] = None,
        assets: Optional[Sequence[MediaAsset]] = None,
        recursive: bool = True,
        force: bool = False,
        extract_audio: bool = False,
        skip_existing: bool = True,
        publish: bool = True,
        limit: int = 0,
    ) -> TranscriptionBatch:
        """Transcribe a folder. One bad file never costs the rest of the batch."""
        return transcribe_run.transcribe_folder(
            self.config, folder,
            settings=settings or self.transcription_config(),
            cache=self.cache,
            assets=assets,
            recursive=recursive,
            force=force,
            extract_audio=extract_audio,
            skip_existing=skip_existing,
            publish=publish,
            limit=limit,
            say=self.say,
        )

    def transcribe_assets(
        self,
        assets: Sequence[MediaAsset],
        *,
        settings: Optional[TranscriptionConfig] = None,
        force: bool = False,
        only_missing: bool = True,
    ) -> TranscriptionBatch:
        """Transcribe exactly the clips a run is about to analyse.

        The seam the auto pipeline uses. ``only_missing`` is what makes
        ``--transcribe`` safe to leave on: a second run over the same footage
        transcribes nothing and costs nothing.
        """
        wanted = list(assets)
        if only_missing and not force:
            outstanding = transcribe_run.missing_transcripts(
                self.config, wanted)
            if not outstanding:
                self.say("Every clip already has a current transcript.")
        # Every asset is passed through rather than pre-filtered, and
        # ``skip_existing`` does the same check one layer down. That is what
        # makes the batch summary say "3 skipped" instead of "no media files
        # found", which is the difference between an accurate report and one
        # that looks like discovery broke.
        root = str(Path(assets[0].path).parent) if assets else ""
        return self.transcribe_folder(
            root,
            settings=settings,
            assets=wanted,
            force=force,
            skip_existing=only_missing and not force,
        )

    def transcription_jobs(self, *, limit: int = 100) -> list[TranscriptionJob]:
        return transcribe_store.list_jobs(self.config, limit=limit)

    def transcription_job(self, job_id: str) -> TranscriptionJob:
        return transcribe_store.load_job(self.config, job_id)

    def transcription_result(self, job_id: str):
        return transcribe_store.load_result(self.config, job_id)

    def export_transcription(
        self, job_id: str, out: str, *, fmt: str = "srt"
    ) -> Path:
        return transcribe_store.export_job(self.config, job_id, out, fmt=fmt)

    def clear_transcription_cache(self) -> int:
        return transcribe_store.clear_cache(self.cache)

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
    # Rough cut
    # ------------------------------------------------------------------

    def rough_cut(
        self,
        *,
        timeline: Optional[StructureTimeline] = None,
        recommendations: Optional[RecommendationSet] = None,
        options: Optional[RoughCutOptions] = None,
        name: str = "structure",
        validate: bool = True,
        save: bool = True,
    ) -> RoughCutPlan:
        """Build a rough cut plan from the timeline and recommendations."""
        if timeline is None:
            timeline = self.load_timeline(name=name)
        if recommendations is None:
            recommendations = self.load_recommendations(name=name)
        assets = self.assets or self._assets_or_empty()

        plan = build_rough_cut(
            timeline, recommendations,
            assets=assets, options=options, validate=validate,
        )

        self.say(
            f"Rough cut '{plan.sequence_name}': {len(plan.placements)} clip(s), "
            f"{plan.total_duration:.1f}s from {plan.source_duration:.1f}s of "
            f"footage, {plan.operation_count} operation(s)."
        )
        if validate:
            self.say(
                "  dry run: " + ("passed" if plan.dry_run_passed else "FAILED")
            )
            if plan.dry_run_error:
                self.say(f"  ! {plan.dry_run_error.get('error')}")
        for warning in plan.warnings:
            self.say(f"  ! {warning}")

        if save:
            self.write_rough_cut(plan, name=name)
        return plan

    def _assets_or_empty(self) -> list:
        try:
            return self.load_assets()
        except FootageError:
            return []

    def write_rough_cut(self, plan: RoughCutPlan, *, name: str = "structure") -> Path:
        self.config.roughcut_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.roughcut_dir / f"{name}.json"
        target.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_rough_cut(self, *, name: str = "structure") -> RoughCutPlan:
        target = self.config.roughcut_dir / f"{name}.json"
        if not target.exists():
            raise EditingError(
                f"No rough cut named '{name}' has been built yet",
                hint="Run `python -m editing.cli roughcut build` first.",
            )
        return RoughCutPlan.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def run_rough_cut(
        self,
        plan: Optional[RoughCutPlan] = None,
        *,
        mode: str = "dry_run",
        allow_active_sequence: bool = False,
        name: str = "structure",
        engine=None,
        save: bool = True,
    ) -> ExecutionReport:
        """Carry a rough cut plan out to the depth ``mode`` allows."""
        if plan is None:
            plan = self.load_rough_cut(name=name)

        report = roughcut_execute.run(
            plan,
            mode=mode,
            bridge=self.bridge,
            engine=engine,
            allow_active_sequence=allow_active_sequence,
        )

        if report.refused_reason:
            self.say(f"Refused: {report.refused_reason}")
        elif report.executed:
            self.say(
                f"Executed {report.operations_succeeded}/"
                f"{report.operations_attempted} operation(s) on "
                f"'{plan.sequence_name}'."
            )
        elif mode == "dry_run":
            self.say(
                "Dry run " + ("passed" if report.dry_run_passed else "FAILED")
                + f" ({plan.operation_count} operation(s)); nothing was executed."
            )
        if report.error:
            self.say(f"  ! {report.error.get('error')}")

        if save:
            self.write_execution_report(report, name=name)
            self.write_rough_cut(plan, name=name)
        return report

    def write_execution_report(
        self, report: ExecutionReport, *, name: str = "structure"
    ) -> Path:
        self.config.roughcut_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.roughcut_dir / f"{name}.execution.json"
        target.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_execution_report(self, *, name: str = "structure") -> ExecutionReport:
        target = self.config.roughcut_dir / f"{name}.execution.json"
        if not target.exists():
            raise EditingError(
                f"No execution report named '{name}' exists yet",
                hint="Run `python -m editing.cli roughcut dry-run` or "
                     "`roughcut execute` first.",
            )
        return ExecutionReport.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def review_frames(
        self,
        plan: Optional[RoughCutPlan] = None,
        *,
        name: str = "structure",
        position: float = review_module.DEFAULT_POSITION,
        width: int = review_module.DEFAULT_WIDTH,
        coverage: bool = True,
        coverage_options=None,
        timeline: Optional[StructureTimeline] = None,
        recommendations: Optional[RecommendationSet] = None,
    ):
        """Export the review frames for a cut, with their context attached.

        ``coverage=True`` (the default) plans frames by rule -- cut points,
        markers, zooms, speed changes, placeholders, high-priority moments and
        sanity probes -- which is what the critic pass needs. ``coverage=False``
        falls back to Session 3's one representative frame per clip, which is
        the right answer when a person just wants to flick through the cut.
        """
        if plan is None:
            plan = self.load_rough_cut(name=name)

        frames = None
        if coverage:
            if timeline is None:
                timeline = self._timeline_or_none(name)
            if recommendations is None:
                recommendations = self._recommendations_or_none(name)
            frames = critic_frames.plan_coverage_frames(
                plan,
                timeline=timeline,
                recommendations=recommendations,
                options=coverage_options,
            )
            if timeline is None:
                self.say(
                    "  ! No timeline was found, so frames carry no transcript, "
                    "audio or visual context. The critic will be judging "
                    "pictures with no idea what is happening in them."
                )

        review = review_module.export_frames(
            plan, self.config, position=position, width=width, frames=frames
        )
        self.say(
            f"{len(review)} review frame(s) exported for "
            f"'{plan.sequence_name}'."
        )
        for warning in review.warnings:
            self.say(f"  ! {warning}")
        return review

    def _timeline_or_none(self, name: str) -> Optional[StructureTimeline]:
        try:
            return self.load_timeline(name=name)
        except EditingError:
            return None

    def _recommendations_or_none(self, name: str) -> Optional[RecommendationSet]:
        try:
            return self.load_recommendations(name=name)
        except EditingError:
            return None

    def review_manifest_path(self, plan: RoughCutPlan) -> Path:
        """Where ``export_frames`` writes the manifest for this cut."""
        return (
            self.config.review_dir
            / review_module._slugify(plan.sequence_name)
            / "review.json"
        )

    def load_review(self, *, name: str = "structure", plan=None):
        """The review manifest for a cut, or a clear error saying to make one."""
        if plan is None:
            plan = self.load_rough_cut(name=name)
        target = self.review_manifest_path(plan)
        if not target.exists():
            raise EditingError(
                f"No review frames have been exported for "
                f"'{plan.sequence_name}' yet",
                hint="Run `python -m editing.cli review export-frames` first.",
                detail={"expected": str(target)},
            )
        return review_module.load_review(target)

    # ------------------------------------------------------------------
    # Critic and revisions
    # ------------------------------------------------------------------

    def critic(self, *, model=None) -> critic_module.VisualCritic:
        return critic_module.build_critic(
            self.config, cache=self.cache, model=model
        )

    def critique(
        self,
        review=None,
        *,
        name: str = "structure",
        model=None,
        limit: int = 0,
        save: bool = True,
    ) -> CriticReport:
        """Run the visual critic over the exported review frames."""
        if review is None:
            review = self.load_review(name=name)

        critic = self.critic(model=model)
        self.say(
            f"Critiquing {len(review.frames)} frame(s) with "
            f"{critic.model_name}..."
        )

        def progress(done: int, count: int, frame) -> None:
            if count and (done == count or done % 10 == 0):
                self.say(f"  {done}/{count} frames")

        report = critic.critique(review, progress=progress, limit=limit)
        stats = report.stats()
        self.say(
            f"{stats['findings']} finding(s) across "
            f"{stats['frames_with_findings']} frame(s); "
            f"{stats['frames_clean']} frame(s) clean."
        )
        for warning in report.warnings[:10]:
            self.say(f"  ! {warning}")

        if save:
            self.write_critique(report, name=name)
        return report

    def write_critique(
        self, report: CriticReport, *, name: str = "structure"
    ) -> Path:
        self.config.critic_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.critic_dir / f"{name}.critique.json"
        target.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_critique(self, *, name: str = "structure") -> CriticReport:
        target = self.config.critic_dir / f"{name}.critique.json"
        if not target.exists():
            raise EditingError(
                f"No critique named '{name}' has been run yet",
                hint="Run `python -m editing.cli review critique` first.",
            )
        return CriticReport.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def revise(
        self,
        *,
        name: str = "structure",
        critique: Optional[CriticReport] = None,
        review=None,
        roughcut: Optional[RoughCutPlan] = None,
        options=None,
        plan_options: Optional[dict] = None,
        save: bool = True,
    ):
        """Findings -> revisions -> one validated revision plan."""
        if roughcut is None:
            roughcut = self.load_rough_cut(name=name)
        if critique is None:
            critique = self.load_critique(name=name)
        if review is None:
            review = self.load_review(name=name, plan=roughcut)

        durations = {
            asset.asset_id: asset.duration
            for asset in (self.assets or self._assets_or_empty())
        }
        revisions = critic_revise.build_revisions(
            critique, review, roughcut,
            recommendations=self._recommendations_or_none(name),
            asset_durations=durations,
            options=options,
        )

        plan = critic_plan.build_revision_plan(
            revisions, roughcut,
            roughcut_executed=self._roughcut_was_executed(name, roughcut),
            **(plan_options or {}),
        )
        critic_execute.dry_run(plan)

        stats = revisions.stats()
        self.say(
            f"{stats['total']} revision(s): {stats['accepted']} accepted, "
            f"{stats['needs_human_review']} kept for a human."
        )
        self.say(
            f"Revision plan: {plan.operation_count} operation(s), dry run "
            + ("passed" if plan.dry_run_passed else "FAILED") + "."
        )
        if plan.dry_run_error:
            self.say(f"  ! {plan.dry_run_error.get('error')}")
        for warning in revisions.warnings + plan.warnings:
            self.say(f"  ! {warning}")

        if save:
            self.write_revisions(revisions, name=name)
            self.write_revision_plan(plan, name=name)
            self.write_revision_report(
                revisions, critique=critique, plan=plan, name=name
            )
        return revisions, plan

    def _roughcut_was_executed(self, name: str, roughcut: RoughCutPlan) -> bool:
        """Whether this cut is actually in Premiere, per the execution report."""
        try:
            report = self.load_execution_report(name=name)
        except EditingError:
            return False
        return bool(
            report.executed and report.sequence_name == roughcut.sequence_name
        )

    def write_revisions(
        self, revisions: RevisionSet, *, name: str = "structure"
    ) -> Path:
        self.config.critic_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.critic_dir / f"{name}.revisions.json"
        target.write_text(
            json.dumps(revisions.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_revisions(self, *, name: str = "structure") -> RevisionSet:
        target = self.config.critic_dir / f"{name}.revisions.json"
        if not target.exists():
            raise EditingError(
                f"No revisions named '{name}' have been planned yet",
                hint="Run `python -m editing.cli review plan` first.",
            )
        return RevisionSet.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def write_revision_plan(
        self, plan: RevisionPlan, *, name: str = "structure"
    ) -> Path:
        self.config.critic_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.critic_dir / f"{name}.revision-plan.json"
        target.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_revision_plan(self, *, name: str = "structure") -> RevisionPlan:
        target = self.config.critic_dir / f"{name}.revision-plan.json"
        if not target.exists():
            raise EditingError(
                f"No revision plan named '{name}' has been built yet",
                hint="Run `python -m editing.cli review plan` first.",
            )
        return RevisionPlan.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def write_revision_report(
        self,
        revisions: RevisionSet,
        *,
        critique: Optional[CriticReport] = None,
        plan: Optional[RevisionPlan] = None,
        name: str = "structure",
        limit: int = 40,
    ) -> Path:
        """The human-readable revision report.

        Written beside the rough cut's report, never over it -- the baseline
        being judged has to survive the judgement.
        """
        text = critic_report.render(
            revisions, critique=critique, plan=plan, limit=limit
        )
        self.config.critic_dir.mkdir(parents=True, exist_ok=True)
        return critic_report.write(
            self.config.critic_dir / f"{name}.revisions.txt", text
        )

    def run_revisions(
        self,
        plan: Optional[RevisionPlan] = None,
        *,
        mode: str = "dry_run",
        name: str = "structure",
        roughcut: Optional[RoughCutPlan] = None,
        allow_active_sequence: bool = False,
        engine=None,
        save: bool = True,
    ) -> ExecutionReport:
        """Carry a revision plan out to the depth ``mode`` allows."""
        if plan is None:
            plan = self.load_revision_plan(name=name)
        if roughcut is None:
            try:
                roughcut = self.load_rough_cut(name=name)
            except EditingError:
                roughcut = None

        report = critic_execute.run(
            plan,
            mode=mode,
            roughcut=roughcut,
            bridge=self.bridge,
            engine=engine,
            allow_active_sequence=allow_active_sequence,
        )

        if report.refused_reason:
            self.say(f"Refused: {report.refused_reason}")
        elif report.executed:
            self.say(
                f"Applied {report.operations_succeeded}/"
                f"{report.operations_attempted} revision operation(s) to "
                f"'{plan.sequence_name}'."
            )
        elif mode == "dry_run":
            self.say(
                "Dry run " + ("passed" if report.dry_run_passed else "FAILED")
                + f" ({plan.operation_count} operation(s)); nothing was applied."
            )
        if report.error:
            self.say(f"  ! {report.error.get('error')}")

        if save:
            self.write_revision_execution(report, name=name)
            self.write_revision_plan(plan, name=name)
        return report

    def write_revision_execution(
        self, report: ExecutionReport, *, name: str = "structure"
    ) -> Path:
        self.config.critic_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.critic_dir / f"{name}.revision-execution.json"
        target.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_revision_execution(
        self, *, name: str = "structure"
    ) -> ExecutionReport:
        target = self.config.critic_dir / f"{name}.revision-execution.json"
        if not target.exists():
            raise EditingError(
                f"No revision execution report named '{name}' exists yet",
                hint="Run `python -m editing.cli review dry-run` or "
                     "`review execute --yes` first.",
            )
        return ExecutionReport.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    # ------------------------------------------------------------------
    # Style and layers
    # ------------------------------------------------------------------

    def layers(
        self,
        *,
        name: str = "structure",
        style=None,
        timeline: Optional[StructureTimeline] = None,
        recommendations: Optional[RecommendationSet] = None,
        roughcut: Optional[RoughCutPlan] = None,
        revisions=None,
        options=None,
        save: bool = True,
    ) -> LayeredEditPlan:
        """Compile a layered, styled edit from the rough cut, and dry-run it."""
        if roughcut is None:
            roughcut = self.load_rough_cut(name=name)
        if timeline is None:
            timeline = self.load_timeline(name=name)
        if recommendations is None:
            recommendations = self.load_recommendations(name=name)
        if revisions is None:
            revisions = self._revisions_or_none(name)

        preset = style if style is not None else style_presets.get()
        plan = style_compile.compile_layers(
            timeline, roughcut,
            style=preset,
            recommendations=recommendations,
            revisions=revisions,
            options=options,
            roughcut_executed=self._roughcut_was_executed(name, roughcut),
        )
        style_execute.dry_run(plan)

        density = plan.density()
        stats = plan.stats()
        self.say(
            f"Layered edit '{plan.sequence_name}' in {plan.style}: "
            f"{stats['planned']} item(s) planned, {stats['deferred']} held "
            f"back, {plan.operation_count} operation(s)."
        )
        self.say(
            f"  density: {density['edits_per_minute']:.2f} active edit(s)/min "
            f"(ceiling {preset.max_edits_per_minute:g}), "
            f"{density['captions_per_minute']:.2f} caption(s)/min "
            f"(ceiling {preset.max_captions_per_minute:g})."
        )
        self.say(
            "  dry run: " + ("passed" if plan.dry_run_passed else "FAILED")
        )
        if plan.dry_run_error:
            self.say(f"  ! {plan.dry_run_error.get('error')}")
        for warning in plan.warnings:
            self.say(f"  ! {warning}")

        if save:
            self.write_layers(plan, name=name)
            self.write_layers_report(plan, name=name)
        return plan

    def _revisions_or_none(self, name: str):
        try:
            return self.load_revisions(name=name)
        except EditingError:
            return None

    def write_layers(
        self, plan: LayeredEditPlan, *, name: str = "structure"
    ) -> Path:
        self.config.layers_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.layers_dir / f"{name}.json"
        target.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_layers(self, *, name: str = "structure") -> LayeredEditPlan:
        target = self.config.layers_dir / f"{name}.json"
        if not target.exists():
            raise EditingError(
                f"No layered edit named '{name}' has been built yet",
                hint="Run `python -m editing.cli layers build --style "
                     "<preset>` first.",
            )
        return LayeredEditPlan.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def write_layers_report(
        self, plan: LayeredEditPlan, *, name: str = "structure", limit: int = 30
    ) -> Path:
        """The human-readable layered report, beside the rough cut's own.

        Never over it: a style pass is one interpretation of a cut, and the
        cut it interprets has to survive being interpreted.
        """
        text = style_report.render(plan, limit=limit)
        self.config.layers_dir.mkdir(parents=True, exist_ok=True)
        return style_report.write(
            self.config.layers_dir / f"{name}.txt", text
        )

    def run_layers(
        self,
        plan: Optional[LayeredEditPlan] = None,
        *,
        mode: str = "dry_run",
        name: str = "structure",
        roughcut: Optional[RoughCutPlan] = None,
        allow_active_sequence: bool = False,
        engine=None,
        save: bool = True,
    ) -> ExecutionReport:
        """Carry a layered plan out to the depth ``mode`` allows."""
        if plan is None:
            plan = self.load_layers(name=name)
        if roughcut is None:
            try:
                roughcut = self.load_rough_cut(name=name)
            except EditingError:
                roughcut = None

        report = style_execute.run(
            plan,
            mode=mode,
            roughcut=roughcut,
            bridge=self.bridge,
            engine=engine,
            allow_active_sequence=allow_active_sequence,
        )

        if report.refused_reason:
            self.say(f"Refused: {report.refused_reason}")
        elif report.executed:
            self.say(
                f"Applied {report.operations_succeeded}/"
                f"{report.operations_attempted} layer operation(s) to "
                f"'{plan.sequence_name}'."
            )
        elif mode == "dry_run":
            self.say(
                "Dry run " + ("passed" if report.dry_run_passed else "FAILED")
                + f" ({plan.operation_count} operation(s)); nothing was applied."
            )
        if report.error:
            self.say(f"  ! {report.error.get('error')}")

        if save:
            self.write_layers_execution(report, name=name)
            self.write_layers(plan, name=name)
        return report

    def write_layers_execution(
        self, report: ExecutionReport, *, name: str = "structure"
    ) -> Path:
        self.config.layers_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.layers_dir / f"{name}.execution.json"
        target.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_layers_execution(
        self, *, name: str = "structure"
    ) -> ExecutionReport:
        target = self.config.layers_dir / f"{name}.execution.json"
        if not target.exists():
            raise EditingError(
                f"No layer execution report named '{name}' exists yet",
                hint="Run `python -m editing.cli layers dry-run` or "
                     "`layers execute --yes` first.",
            )
        return ExecutionReport.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def init_assets(self, *, root: Optional[str] = None) -> dict:
        """Create the asset folder structure. Never overwrites anything."""
        target = asset_library.resolve_root(self.config, root)
        result = asset_library.initialise(target)
        self.say(
            f"Asset library at {target}: {len(result['created'])} folder(s) "
            f"created, {len(result['existing'])} already there."
        )
        for path in result["docs"]:
            self.say(f"  wrote {Path(path).name}")
        return result

    def index_assets(
        self,
        *,
        root: Optional[str] = None,
        probe_durations: bool = True,
        reuse: bool = True,
        previous=None,
        save: bool = True,
    ) -> AssetLibrary:
        """Scan the asset folders into an index.

        ``previous`` seeds the scan with an earlier index so unchanged files
        keep their measured durations without paying for ffprobe again. An
        automated run passes the *shared* index here, because its own output
        directory is per-run and would otherwise re-probe the whole library
        every time.
        """
        if previous is None and reuse:
            try:
                previous = self.load_asset_library(root=root)
            except EditingError:
                previous = None

        library = asset_indexer.index_library(
            self.config, root=root, previous=previous,
            probe_durations=probe_durations, say=self.say,
        )
        for warning in library.warnings:
            self.say(f"  ! {warning}")
        if save:
            self.write_asset_library(library, root=root)
        return library

    def write_asset_library(
        self, library: AssetLibrary, *, root: Optional[str] = None
    ) -> Path:
        target = asset_library.index_path(self.config, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(library.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_asset_library(self, *, root: Optional[str] = None) -> AssetLibrary:
        target = asset_library.index_path(self.config, root)
        if not target.exists():
            raise EditingError(
                "The asset library has not been indexed yet",
                hint="Run `python -m editing.cli assets index` first "
                     "(`assets init` creates the folders).",
                detail={"expected": str(target)},
            )
        return AssetLibrary.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def asset_library_or_empty(
        self, *, root: Optional[str] = None
    ) -> AssetLibrary:
        """The index, or an empty library rather than an error.

        An empty library is a valid input that produces a complete plan of
        markers, so the planning path should not require anyone to have run
        the indexer first.
        """
        try:
            return self.load_asset_library(root=root)
        except EditingError:
            return AssetLibrary(
                root=str(asset_library.resolve_root(self.config, root)),
                warnings=[
                    "No asset index exists yet, so this ran against an empty "
                    "library. Run `assets index` once the folders have files "
                    "in them."
                ],
            )

    def asset_plan(
        self,
        *,
        name: str = "structure",
        root: Optional[str] = None,
        library=None,
        layers=None,
        style=None,
        timeline: Optional[StructureTimeline] = None,
        revisions=None,
        options=None,
        limits=None,
        save: bool = True,
    ) -> AssetPlacementPlan:
        """Resolve every layer placeholder against the asset library."""
        if layers is None:
            layers = self.load_layers(name=name)
        if library is None:
            library = self.asset_library_or_empty(root=root)
        if timeline is None:
            timeline = self._timeline_or_none(name)
        if revisions is None:
            revisions = self._revisions_or_none(name)

        preset = style
        if preset is None:
            from editing.style import presets as style_presets

            preset = style_presets.get(layers.style or None)

        plan = asset_compile.compile_assets(
            layers, library,
            style=preset,
            timeline=timeline,
            revisions=revisions,
            options=options,
            limits=limits,
            roughcut_executed=layers.roughcut_executed,
        )
        asset_execute.dry_run(plan)

        stats = plan.stats()
        self.say(
            f"Asset placement for '{plan.sequence_name}' [{plan.style}]: "
            f"{stats['placed']} placed, {stats['missing']} missing, "
            f"{stats['rejected']} rejected, {stats['unsafe']} unsafe."
        )
        self.say(
            f"  {plan.operation_count} operation(s), dry run "
            + ("passed" if plan.dry_run_passed else "FAILED") + "."
        )
        if plan.dry_run_error:
            self.say(f"  ! {plan.dry_run_error.get('error')}")
        for warning in plan.warnings:
            self.say(f"  ! {warning}")

        if save:
            self.write_asset_plan(plan, name=name)
            self.write_asset_report(plan, library=library, name=name)
        return plan

    def write_asset_plan(
        self, plan: AssetPlacementPlan, *, name: str = "structure"
    ) -> Path:
        self.config.asset_library_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.asset_library_dir / f"{name}.placement.json"
        target.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_asset_plan(self, *, name: str = "structure") -> AssetPlacementPlan:
        target = self.config.asset_library_dir / f"{name}.placement.json"
        if not target.exists():
            raise EditingError(
                f"No asset placement plan named '{name}' has been built yet",
                hint="Run `python -m editing.cli assets plan` first.",
            )
        return AssetPlacementPlan.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def write_asset_report(
        self,
        plan: AssetPlacementPlan,
        *,
        library=None,
        name: str = "structure",
        limit: int = 30,
    ) -> Path:
        """The human-readable asset report, beside the layered one.

        Never over it: an asset pass is one more interpretation of the same
        cut, and the passes it builds on have to survive it.
        """
        text = asset_report.render(plan, library=library, limit=limit)
        self.config.asset_library_dir.mkdir(parents=True, exist_ok=True)
        return asset_report.write(
            self.config.asset_library_dir / f"{name}.placement.txt", text
        )

    def run_assets(
        self,
        plan: Optional[AssetPlacementPlan] = None,
        *,
        mode: str = "dry_run",
        name: str = "structure",
        roughcut: Optional[RoughCutPlan] = None,
        allow_active_sequence: bool = False,
        engine=None,
        save: bool = True,
    ) -> ExecutionReport:
        """Carry an asset placement plan out to the depth ``mode`` allows."""
        if plan is None:
            plan = self.load_asset_plan(name=name)
        if roughcut is None:
            try:
                roughcut = self.load_rough_cut(name=name)
            except EditingError:
                roughcut = None

        report = asset_execute.run(
            plan,
            mode=mode,
            roughcut=roughcut,
            bridge=self.bridge,
            engine=engine,
            allow_active_sequence=allow_active_sequence,
        )

        if report.refused_reason:
            self.say(f"Refused: {report.refused_reason}")
        elif report.executed:
            self.say(
                f"Placed {report.operations_succeeded}/"
                f"{report.operations_attempted} asset operation(s) on "
                f"'{plan.sequence_name}'."
            )
        elif mode == "dry_run":
            self.say(
                "Dry run " + ("passed" if report.dry_run_passed else "FAILED")
                + f" ({plan.operation_count} operation(s)); nothing was placed."
            )
        if report.error:
            self.say(f"  ! {report.error.get('error')}")

        if save:
            self.write_asset_execution(report, name=name)
            self.write_asset_plan(plan, name=name)
        return report

    def write_asset_execution(
        self, report: ExecutionReport, *, name: str = "structure"
    ) -> Path:
        self.config.asset_library_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.asset_library_dir / f"{name}.placement-execution.json"
        target.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_asset_execution(
        self, *, name: str = "structure"
    ) -> ExecutionReport:
        target = self.config.asset_library_dir / f"{name}.placement-execution.json"
        if not target.exists():
            raise EditingError(
                f"No asset execution report named '{name}' exists yet",
                hint="Run `python -m editing.cli assets dry-run` or "
                     "`assets execute --yes` first.",
            )
        return ExecutionReport.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    # ------------------------------------------------------------------
    # Episode memory and retention plan (Session 8)
    # ------------------------------------------------------------------
    #
    # Neither of these touches Premiere, and neither has a dry run or an
    # execute, because there is nothing to execute: the layer produces records
    # a later pass may read. That is why there is no ``run_episode``.

    def episode_memory(
        self,
        *,
        name: str = "structure",
        timeline: Optional[StructureTimeline] = None,
        roughcut: Optional[RoughCutPlan] = None,
        recommendations: Optional[RecommendationSet] = None,
        layers=None,
        asset_plan=None,
        use_roughcut: bool = True,
        save: bool = True,
    ) -> EpisodeMemory:
        """Read one episode off the timeline and, when there is one, the cut.

        Every optional input is loaded opportunistically and its absence is
        recorded rather than worked around: a memory built without a rough cut
        is in a different timebase from one built with, and a caller that
        cannot tell which would put markers in the wrong places.
        """
        if timeline is None:
            timeline = self.load_timeline(name=name)
        if roughcut is None and use_roughcut:
            roughcut = self._rough_cut_or_none(name)
        if recommendations is None:
            recommendations = self._recommendations_or_none(name)
        if layers is None:
            layers = self._layers_or_none(name)
        if asset_plan is None:
            asset_plan = self._asset_plan_or_none(name)

        memory = episode_memory_module.build(
            timeline,
            roughcut=roughcut,
            recommendations=recommendations,
            layers=layers,
            asset_plan=asset_plan,
            name=name,
        )
        stats = memory.stats()
        objective = memory.main_objective
        self.say(
            f"Episode memory for '{name}': {stats['beats']} beat(s) "
            f"({stats['labelled_beats']} named), {stats['open_loops']} open "
            f"loop(s) of which {stats['resolved_loops']} resolve, "
            f"{stats['callbacks']} callback opportunity(ies)."
        )
        self.say(
            "  objective: "
            + (f"{objective.text[:60]} ({objective.status})" if objective
               else "none stated or inferable")
        )
        self.say(f"  timebase : {memory.timebase}")
        for warning in memory.warnings:
            self.say(f"  ! {warning}")

        if save:
            self.write_episode_memory(memory, name=name)
            self.write_episode_report(memory, name=name)
        return memory

    def _rough_cut_or_none(self, name: str) -> Optional[RoughCutPlan]:
        try:
            return self.load_rough_cut(name=name)
        except EditingError:
            return None

    def _layers_or_none(self, name: str):
        try:
            return self.load_layers(name=name)
        except EditingError:
            return None

    def _asset_plan_or_none(self, name: str):
        try:
            return self.load_asset_plan(name=name)
        except EditingError:
            return None

    def write_episode_memory(
        self, memory: EpisodeMemory, *, name: str = "structure"
    ) -> Path:
        self.config.episode_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.episode_dir / f"{name}.memory.json"
        target.write_text(
            json.dumps(memory.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_episode_memory(self, *, name: str = "structure") -> EpisodeMemory:
        target = self.config.episode_dir / f"{name}.memory.json"
        if not target.exists():
            raise EditingError(
                f"No episode memory named '{name}' has been built yet",
                hint="Run `python -m editing.cli episode build-memory` first.",
            )
        return EpisodeMemory.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def write_episode_report(
        self, memory: EpisodeMemory, *, name: str = "structure",
        limit: int = 60,
    ) -> Path:
        self.config.episode_dir.mkdir(parents=True, exist_ok=True)
        return episode_report.write(
            self.config.episode_dir / f"{name}.memory.txt",
            episode_report.render_memory(memory, limit=limit),
        )

    def retention_plan(
        self,
        *,
        name: str = "structure",
        memory: Optional[EpisodeMemory] = None,
        timeline: Optional[StructureTimeline] = None,
        roughcut: Optional[RoughCutPlan] = None,
        hook_limit: int = 5,
        save: bool = True,
    ) -> EpisodeRetentionPlan:
        """Risks, hooks, a peak, an ending and the suggestions that follow.

        The timeline is reloaded even when a memory is passed in, because the
        risk detectors read the slots underneath the beats -- measured silence,
        motion and what was said across a cut do not survive the merge into a
        beat list.
        """
        if memory is None:
            memory = self.load_episode_memory(name=name)
        if timeline is None:
            timeline = self.load_timeline(name=name)
        if roughcut is None and memory.timebase == "roughcut":
            roughcut = self._rough_cut_or_none(name)

        plan = episode_plan_module.build(
            memory, timeline=timeline, roughcut=roughcut, hook_limit=hook_limit,
        )
        stats = plan.stats()
        self.say(
            f"Retention plan for '{name}': {stats['risks']} risk zone(s) "
            f"({stats['high_severity']} high), {stats['hooks']} hook "
            f"candidate(s), {stats['suggestions']} suggestion(s)."
        )
        self.say(
            f"  {stats['auto_safe']} suggestion(s) are safe to apply; "
            f"{stats['marker_only']} are markers for a person."
        )
        self.say(
            "  climax: "
            + (f"{plan.climax.start:.1f}s" if plan.climax
               else "no single moment stands out")
        )
        for warning in plan.warnings:
            self.say(f"  ! {warning}")

        if save:
            self.write_retention_plan(plan, name=name)
            self.write_retention_report(plan, memory=memory, name=name)
        return plan

    def write_retention_plan(
        self, plan: EpisodeRetentionPlan, *, name: str = "structure"
    ) -> Path:
        self.config.episode_dir.mkdir(parents=True, exist_ok=True)
        target = self.config.episode_dir / f"{name}.retention.json"
        target.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    def load_retention_plan(
        self, *, name: str = "structure"
    ) -> EpisodeRetentionPlan:
        target = self.config.episode_dir / f"{name}.retention.json"
        if not target.exists():
            raise EditingError(
                f"No retention plan named '{name}' has been built yet",
                hint="Run `python -m editing.cli episode plan-retention` "
                     "first.",
            )
        return EpisodeRetentionPlan.from_dict(
            json.loads(target.read_text(encoding="utf-8"))
        )

    def write_retention_report(
        self,
        plan: EpisodeRetentionPlan,
        *,
        memory: Optional[EpisodeMemory] = None,
        name: str = "structure",
        limit: int = 60,
    ) -> Path:
        self.config.episode_dir.mkdir(parents=True, exist_ok=True)
        return episode_report.write(
            self.config.episode_dir / f"{name}.retention.txt",
            episode_report.render_plan(plan, memory=memory, limit=limit),
        )

    def retention_suggestions_for(
        self, stage: str, *, name: str = "structure",
        plan: Optional[EpisodeRetentionPlan] = None,
        safe_only: bool = False,
    ) -> list:
        """The seam Sessions 3, 5 and 6 will read.

        A filter over records with no Premiere operations in them, on purpose:
        a later pass decides what an operation looks like, this one only says
        what it wants. Nothing consumes it yet -- the seam exists so the next
        session does not have to reshape this artifact to use it.
        """
        if plan is None:
            plan = self.load_retention_plan(name=name)
        wanted = plan.suggestions_for(stage)
        return [item for item in wanted if item.auto_safe] if safe_only \
            else wanted

    # ------------------------------------------------------------------
    # Feedback collection and the human review loop (Session 9)
    # ------------------------------------------------------------------
    #
    # Nothing here executes anything, and nothing here trains anything. It
    # reads the artifacts the earlier passes wrote, asks a person about the
    # decisions in them, and appends what they said to a log that is never
    # rewritten. The only thing this layer produces that a later session will
    # consume is ``exports/`` -- everything else is for the person reviewing.

    def feedback_artifacts(
        self, *, name: str = "structure", run_id: str = "", style: str = "",
    ) -> feedback_targets.Artifacts:
        """Load whatever the earlier passes left, and record what is missing.

        Every load is opportunistic. Reviewing a rough cut before the critic
        has ever run is a reasonable thing to want, so an absent artifact is
        recorded in ``sources`` rather than raised -- the queue then says which
        passes it could not ask about, instead of implying they were fine.
        """
        artifacts = feedback_targets.Artifacts(
            name=name,
            style=style,
            run_id=run_id,
            artifact_root=str(self.config.output_dir),
        )
        for field_name, loader in (
            ("timeline", lambda: self.load_timeline(name=name)),
            ("recommendations", lambda: self.load_recommendations(name=name)),
            ("roughcut", lambda: self.load_rough_cut(name=name)),
            ("critique", lambda: self.load_critique(name=name)),
            ("revisions", lambda: self.load_revisions(name=name)),
            ("layers", lambda: self.load_layers(name=name)),
            ("asset_plan", lambda: self.load_asset_plan(name=name)),
            ("memory", lambda: self.load_episode_memory(name=name)),
            ("retention", lambda: self.load_retention_plan(name=name)),
        ):
            try:
                setattr(artifacts, field_name, loader())
            except EditingError:
                continue          # absent, and ``sources`` will say so
            except (OSError, ValueError, TypeError, KeyError) as error:
                # Present but unreadable is a different problem from absent,
                # and the reviewer needs to know which one they have.
                artifacts.warnings.append(
                    f"{field_name} exists but could not be read: {error}"
                )
        if not artifacts.style and artifacts.layers is not None:
            artifacts.style = getattr(artifacts.layers, "style", "")
        return artifacts

    def feedback_start(
        self,
        *,
        name: str = "structure",
        run_id: str = "",
        session_id: str = "",
        title: str = "",
        notes: str = "",
        limit: int = feedback_queue_module.DEFAULT_LIMIT,
        build_queue: bool = True,
        force: bool = False,
        artifacts: Optional[feedback_targets.Artifacts] = None,
    ) -> tuple[FeedbackSession, Optional[ReviewQueue]]:
        """Open a review session and, unless told not to, build its queue."""
        if artifacts is None:
            artifacts = self.feedback_artifacts(name=name, run_id=run_id)
        session = feedback_collect.start_session(
            self.config, artifacts, run_id=run_id, session_id=session_id,
            title=title, notes=notes, force=force,
        )
        self.say(f"Feedback session {session.session_id} started.")
        self.say(f"  reviewing : {session.sequence_name or '(no rough cut)'} "
                 f"({session.duration:.0f}s, timebase {session.timebase})")
        for warning in session.warnings:
            self.say(f"  ! {warning}")

        queue = None
        if build_queue:
            queue = self.feedback_queue(
                session=session, artifacts=artifacts, limit=limit)
        self.say(
            "  saved to  : "
            f"{feedback_store.session_dir(self.config, session.session_id)}"
        )
        return session, queue

    def feedback_queue(
        self,
        *,
        session: Optional[FeedbackSession] = None,
        session_id: str = "",
        name: str = "structure",
        artifacts: Optional[feedback_targets.Artifacts] = None,
        limit: int = feedback_queue_module.DEFAULT_LIMIT,
        categories: Sequence[str] = (),
        sources: Sequence[str] = (),
        include_positive: bool = True,
        save: bool = True,
    ) -> ReviewQueue:
        """Build the review queue and mark what already has feedback.

        Regenerating a queue is always allowed -- it holds no feedback -- and
        the previous one is kept beside it, because a rating references a
        prompt ID and that question has to stay readable afterwards.
        """
        if session is None:
            session = feedback_store.resolve_session(self.config, session_id)
        if artifacts is None:
            artifacts = self.feedback_artifacts(
                name=session.name or name, run_id=session.run_id,
                style=session.style,
            )
        queue = feedback_queue_module.build(
            artifacts,
            session_id=session.session_id,
            run_id=session.run_id,
            limit=limit,
            categories=categories,
            sources=sources,
            include_positive=include_positive,
        )
        feedback_collect.mark_answered(
            queue,
            feedback_store.read_current(self.config, session.session_id),
        )
        stats = queue.stats()
        self.say(
            f"Review queue: {stats['prompts']} question(s) from "
            f"{stats['candidates']} candidate(s), in {stats['groups']} group(s)."
        )
        for warning in queue.warnings:
            self.say(f"  ! {warning}")
        if save:
            feedback_store.write_queue(self.config, session.session_id, queue)
        return queue

    def feedback_session(
        self, session_id: str = "", *, run_id: str = ""
    ) -> FeedbackSession:
        return feedback_store.resolve_session(
            self.config, session_id, run_id=run_id)

    def feedback_items(
        self, session: FeedbackSession, *, current_only: bool = True
    ) -> list[FeedbackItem]:
        return (
            feedback_store.read_current(self.config, session.session_id)
            if current_only
            else feedback_store.read_all(self.config, session.session_id)
        )

    def feedback_signals(
        self, session: FeedbackSession, *, current_only: bool = True
    ) -> tuple[list[PreferenceSignal], list[TrainingSignal]]:
        """Preference and training signals for one session.

        Derived on every call rather than stored, so they can never disagree
        with the log they came from. Neither is applied to anything.
        """
        items = self.feedback_items(session, current_only=current_only)
        preferences = feedback_signals.extract(items, style=session.style)
        queue = feedback_store.queue_or_none(self.config, session.session_id)
        prompts = {
            prompt.prompt_id: prompt
            for prompt in (queue.prompts if queue else ())
        }
        # A prompt may have been superseded by a later queue; look those up
        # individually so an answered question is never lost to a regenerate.
        for item in items:
            if item.prompt_id and item.prompt_id not in prompts:
                found = feedback_store.find_prompt(
                    self.config, session.session_id, item.prompt_id)
                if found is not None:
                    prompts[item.prompt_id] = found
        training = feedback_training.extract(
            items, prompts=prompts, timebase=session.timebase)
        return preferences, training

    def feedback_summary(
        self, session: FeedbackSession, *, save: bool = True
    ) -> dict:
        """The whole picture of a session, as data. Regenerated every time."""
        history = feedback_store.read_all(self.config, session.session_id)
        current = feedback_store.current_of(history)
        preferences, training = self.feedback_signals(session)
        summary = feedback_report.build_summary(
            session,
            history=history,
            current=current,
            preferences=preferences,
            training=training,
            queue=feedback_store.queue_or_none(
                self.config, session.session_id),
            problems=feedback_store.read_problems(
                self.config, session.session_id),
        )
        if save:
            feedback_collect.refresh_counts(self.config, session)
            summary["session"] = session.to_dict()
            feedback_store.write_summary(
                self.config, session.session_id, summary)
            feedback_store.write_report(
                self.config, session.session_id,
                feedback_report.render_report(summary),
            )
        return summary

    def feedback_export(
        self,
        session: FeedbackSession,
        *,
        parts: Sequence[str] = ("feedback", "preferences", "training"),
        fmt: str = "jsonl",
        out: Optional[str] = None,
        current_only: bool = True,
        training_only: bool = False,
    ) -> tuple[Path, object]:
        """Write an export and its manifest. Never overwrites an earlier one."""
        items = self.feedback_items(session, current_only=current_only)
        if training_only:
            items = [item for item in items if item.usable_for_training]
        preferences, training = self.feedback_signals(
            session, current_only=current_only)
        body, record = feedback_export.build(
            parts=parts,
            fmt=fmt,
            items=items,
            preferences=preferences,
            training=training,
            queue=feedback_store.queue_or_none(
                self.config, session.session_id),
            session=session.to_dict(),
            filters={
                "current_only": bool(current_only),
                "training_only": bool(training_only),
            },
        )
        # The session always keeps its own copy and manifest, so an export
        # written somewhere else is still recorded where the review lives.
        kept, record = feedback_store.write_export(
            self.config, session.session_id, body=body, record=record,
            filename=feedback_export.default_filename(
                record.format, record.parts),
        )
        target = kept
        if out:
            target = Path(out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
            record.path = str(target)
        self.say(f"Exported {record.total_rows} row(s) to {target}")
        return target, record

    def feedback_estimate(self, *, name: str = "structure") -> dict:
        """How much would be worth reviewing, without starting a session.

        The auto report calls this so it can say "34 things are worth looking
        at" without creating a review nobody asked for.
        """
        return feedback_queue_module.estimate(
            self.feedback_artifacts(name=name))

    # ------------------------------------------------------------------
    # Proxy renders
    # ------------------------------------------------------------------
    #
    # The iteration loop. Everything above produces plans; this turns one into
    # a video somebody can watch, without Premiere and without executing
    # anything. It reads the rough cut and writes only under
    # ``render/jobs/<job_id>/``.

    def render_config(self, **overrides) -> RenderConfig:
        """Render settings from the environment, overridden by kwargs."""
        base = RenderConfig.from_env()
        clean = {k: v for k, v in overrides.items() if v is not None}
        if clean:
            from dataclasses import replace
            base = replace(base, **clean)
        return base.validated()

    def render_status(self, settings: Optional[RenderConfig] = None) -> dict:
        """Whether a render could run right now. Runs nothing."""
        settings = settings or self.render_config()
        health = render_runner.check(self.config, backend=settings.backend)
        health["config_warnings"] = settings.warnings
        health.update(render_store.usage(self.config))
        return health

    def render_roughcut(
        self,
        *,
        name: str = "structure",
        plan: Optional[RoughCutPlan] = None,
        settings: Optional[RenderConfig] = None,
        runner=None,
        force: bool = False,
        dry_run: bool = False,
        muted_placements: Optional[Sequence[str]] = None,
    ) -> RenderJob:
        """Render the rough cut this pipeline's output directory holds."""
        if plan is None:
            plan = self.load_rough_cut(name=name)
        return render_run.render_plan(
            self.config, plan,
            settings=settings or self.render_config(),
            plan_name=name,
            plan_path=str(self.config.roughcut_dir / f"{name}.json"),
            runner=runner,
            force=force,
            dry_run=dry_run,
            muted_placements=muted_placements,
            say=self.say,
        )

    def render_plan_file(
        self,
        path: str,
        *,
        settings: Optional[RenderConfig] = None,
        runner=None,
        force: bool = False,
        dry_run: bool = False,
    ) -> RenderJob:
        """Render a rough cut plan from any JSON file on disk."""
        return render_run.render_from_file(
            self.config, path,
            settings=settings or self.render_config(),
            runner=runner, force=force, dry_run=dry_run, say=self.say,
        )

    def render_jobs(self, *, limit: int = 50) -> list[RenderJob]:
        return render_store.list_jobs(self.config, limit=limit)

    def render_job(self, job_id: str = "") -> RenderJob:
        """One render by ID, or the most recent one."""
        return render_store.resolve_job(self.config, job_id)

    def render_result(self, job_id: str) -> RenderResult:
        return render_store.load_result(self.config, job_id)

    def render_report(self, job_id: str = "", *, save: bool = True):
        """The report for one render, regenerated from the job on disk."""
        job = self.render_job(job_id)
        report = render_report.build_report(job)
        if save and job.output_dir:
            render_report.write_report(job)
        return job, report

    def render_notes(self, job_id: str = "") -> Path:
        """Rewrite one render's review notes. Returns the path.

        Separate from the render so a person who has scribbled over the notes
        and wants a clean copy does not have to re-encode the video to get one.
        """
        job = self.render_job(job_id)
        return render_store.write_text(
            render_store.notes_path(job.output_dir),
            render_notes.render_notes(job, interval=job.config.notes_interval),
        )

    def clean_renders(
        self, *, job_id: str = "", temp_only: bool = False,
        keep_latest: int = 0,
    ) -> dict:
        return render_store.clean(
            self.config, job_id=job_id, temp_only=temp_only,
            keep_latest=keep_latest,
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

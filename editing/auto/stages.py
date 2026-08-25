"""The pipeline, as a table.

Twenty-three stages, each declared with what it needs, what it produces, and what
happens when it fails. Keeping it as data rather than as a function with
twenty-three sections buys three things:

* ``auto status`` can describe a stage that has not run yet — its
  requirements, its artifacts, the command to run it by hand;
* checkpoint invalidation is derivable, because each stage names the config
  fields it actually depends on;
* the dependency order is auditable in one screen instead of inferred from
  control flow.

**Not every stage is critical.** The review pass needs FFmpeg and a model
server, and neither is guaranteed. So the four review stages are marked
non-critical: if frames cannot be exported or the critic cannot be reached, the
run says so, marks the downstream review stages blocked, and **keeps going** to
the style and asset passes. A missing critic costs you the critic, not the run.

The runner functions live here too, one per stage, each a thin adapter over the
pipeline method that already does the work. They exist to normalise the return
into ``(outputs, summary, warnings)`` and to raise ``StageBlocked`` where a
missing tool is a foreseeable, explainable condition rather than a bug.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from editing.auto.schema import AutoStage
from editing.errors import EditingError, ModelError, ToolMissingError

#: Raised by a stage runner when the stage cannot proceed for a reason the user
#: can act on. Carries the same fields ``AutoFailure`` needs.
class StageBlocked(Exception):
    """A stage cannot run, and a person can see why and fix it."""

    def __init__(
        self,
        what: str,
        why: str,
        *,
        code: str = "stage_blocked",
        next_command: str = "",
        detail: Optional[dict] = None,
    ):
        super().__init__(why)
        self.what = what
        self.why = why
        self.code = code
        self.next_command = next_command
        self.detail = detail or {}


#: Every stage, in run order.
STAGES = (
    AutoStage(
        name="doctor",
        summary="Check FFmpeg, the vision model and Premiere",
        config_keys=("mock", "no_premiere"),
        resumable=False,          # cheap, and its answer can change any minute
        critical=False,           # informational: a missing tool blocks later
        manual_command="python -m editing.cli doctor",
    ),
    AutoStage(
        name="discover",
        summary="Find the footage and probe it",
        requires=("doctor",),
        config_keys=("footage_folder", "recursive", "no_premiere"),
        artifacts=("assets.json",),
        manual_command="python -m editing.cli discover --folder <folder>",
    ),
    AutoStage(
        name="transcribe",
        summary="Local Whisper speech to text over the discovered clips",
        requires=("discover",),
        config_keys=("footage_folder", "transcribe_model",
                     "transcribe_language", "transcribe_backend",
                     "recursive"),
        # Not resumable, and cheap anyway: the runner skips any clip that
        # already has a current transcript, so re-reaching this stage on a
        # resume costs a fingerprint check per file rather than a model load.
        resumable=False,
        critical=False,
        manual_command="python -m editing.cli transcribe folder <folder>",
    ),
    AutoStage(
        name="analyze",
        summary="Transcripts, audio events, Qwen3-VL vision, combined timeline",
        requires=("discover",),
        config_keys=("footage_folder", "max_windows", "mock", "use_motion",
                     "keep_frames", "no_premiere"),
        artifacts=("timelines/structure.json",),
        requires_model=True,
        requires_ffmpeg=True,
        manual_command="python -m editing.cli run --folder <folder>",
    ),
    AutoStage(
        name="recommend",
        summary="Six recommendation layers and the safety pass",
        requires=("analyze",),
        config_keys=("name",),
        artifacts=("recommendations/structure.json",),
        manual_command="python -m editing.cli recommend",
    ),
    AutoStage(
        name="director_plan",
        summary="A model reads the whole episode and decides what the cut is",
        # Needs the story layer to be worth running, but must come *before*
        # the rough cut it chooses the ranges for -- so it depends on
        # recommendations and reads the episode memory opportunistically.
        requires=("recommend",),
        config_keys=("name", "style", "director_mode", "director_model",
                     "director_backend", "style_guide", "target_duration"),
        artifacts=("director/structure.plan.json",),
        requires_model=True,
        critical=False,
        manual_command="python -m editing.cli director plan",
    ),
    AutoStage(
        name="roughcut_build",
        summary="Select ranges and lay out the scratch sequence",
        requires=("recommend",),
        config_keys=("name", "director", "director_mode"),
        artifacts=("roughcut/structure.json",),
        manual_command="python -m editing.cli roughcut build",
    ),
    AutoStage(
        name="roughcut_dry_run",
        summary="Validate the rough cut plan offline",
        requires=("roughcut_build",),
        config_keys=("name",),
        resumable=False,          # cheap, and it gates an execution
        manual_command="python -m editing.cli roughcut dry-run",
    ),
    AutoStage(
        name="review_export_frames",
        summary="Export the frames the critic will look at",
        requires=("roughcut_build",),
        config_keys=("name",),
        requires_ffmpeg=True,
        critical=False,
        manual_command="python -m editing.cli review export-frames",
    ),
    AutoStage(
        name="review_critique",
        summary="Qwen3-VL looks at the frames",
        requires=("review_export_frames",),
        config_keys=("name", "mock"),
        artifacts=("critic/structure.critique.json",),
        requires_model=True,
        critical=False,
        manual_command="python -m editing.cli review critique",
    ),
    AutoStage(
        name="review_plan",
        summary="Findings become revisions, and a revision plan",
        requires=("review_critique",),
        config_keys=("name",),
        artifacts=("critic/structure.revisions.json",
                   "critic/structure.revision-plan.json"),
        critical=False,
        manual_command="python -m editing.cli review plan",
    ),
    AutoStage(
        name="review_dry_run",
        summary="Validate the revision plan offline",
        requires=("review_plan",),
        config_keys=("name",),
        resumable=False,
        critical=False,
        manual_command="python -m editing.cli review dry-run",
    ),
    AutoStage(
        name="layers_build",
        summary="Apply the style: captions, emphasis, audio cues, cards",
        requires=("roughcut_build",),
        config_keys=("name", "style", "markers_only"),
        artifacts=("layers/structure.json",),
        manual_command="python -m editing.cli layers build --style <preset>",
    ),
    AutoStage(
        name="layers_dry_run",
        summary="Validate the layered plan offline",
        requires=("layers_build",),
        config_keys=("name", "style", "markers_only"),
        resumable=False,
        manual_command="python -m editing.cli layers dry-run",
    ),
    AutoStage(
        name="assets_index",
        summary="Scan the local asset library",
        requires=("doctor",),
        config_keys=("asset_library",),
        artifacts=("assets/library.json",),
        requires_assets=True,
        critical=False,
        manual_command="python -m editing.cli assets index",
    ),
    AutoStage(
        name="assets_plan",
        summary="Match every placeholder against the library",
        requires=("layers_build", "assets_index"),
        config_keys=("name", "style", "asset_library", "markers_only"),
        artifacts=("assets/structure.placement.json",),
        manual_command="python -m editing.cli assets plan",
    ),
    AutoStage(
        name="assets_dry_run",
        summary="Validate the asset placement plan offline",
        requires=("assets_plan",),
        config_keys=("name", "style", "asset_library", "markers_only"),
        resumable=False,
        manual_command="python -m editing.cli assets dry-run",
    ),
    AutoStage(
        name="episode_memory",
        summary="Read the story off the cut: beats, objectives, loops",
        requires=("roughcut_build",),
        config_keys=("name", "style"),
        artifacts=("episode/structure.memory.json",),
        critical=False,
        manual_command="python -m editing.cli episode build-memory",
    ),
    AutoStage(
        name="retention_plan",
        summary="Risks, hook candidates, a peak, an ending, suggestions",
        requires=("episode_memory",),
        config_keys=("name", "style"),
        artifacts=("episode/structure.retention.json",),
        critical=False,
        manual_command="python -m editing.cli episode plan-retention",
    ),
    AutoStage(
        name="retention_cut",
        summary="Reshape the cut around the retention findings",
        # Needs the retention plan it consumes and the cut it reshapes.
        requires=("retention_plan", "roughcut_build"),
        config_keys=("name", "style", "retention_mode", "cold_open",
                     "max_cold_open_seconds", "dead_air_aggressiveness",
                     "director", "director_mode"),
        artifacts=("retention/structure.plan.json",),
        critical=False,
        manual_command="python -m editing.cli retention plan",
    ),
    AutoStage(
        name="caption_polish",
        summary="Choose the few lines worth putting on screen",
        # Reads the cut this run produced, so it waits for the retention pass
        # when there is one. ``retention_cut`` is non-critical and often
        # skipped, so the requirement is the rough cut and the runner reads
        # the retention variant when it exists.
        requires=("roughcut_build",),
        config_keys=("name", "style", "captions", "max_captions_per_minute",
                     "max_caption_seconds", "max_caption_words",
                     "min_caption_confidence", "require_caption_confidence",
                     "retention_cut", "retention_mode"),
        artifacts=("polish/structure.captions.json",),
        critical=False,
        manual_command="python -m editing.cli polish captions "
                       "--captions key_moments",
    ),
    AutoStage(
        name="audio_polish",
        summary="Mark the few moments that earn a sound",
        requires=("roughcut_build",),
        config_keys=("name", "style", "audio_polish", "max_sfx_per_minute",
                     "music_bed", "ducking", "asset_library",
                     "retention_cut", "retention_mode"),
        artifacts=("polish/structure.audio.json",),
        critical=False,
        manual_command="python -m editing.cli polish audio "
                       "--audio-polish placeholders",
    ),
    AutoStage(
        name="visual_plan",
        summary="Find the moments that earn visual emphasis, and refuse most",
        # After the polish passes because a callout over a caption is a
        # refusal it can only make once the captions exist.
        requires=("roughcut_build",),
        config_keys=("name", "style", "visual_layer",
                     "max_effects_per_minute", "max_callouts_per_minute",
                     "allow_freeze_frames", "allow_callouts", "allow_replays",
                     "allow_screen_shake", "captions", "audio_polish",
                     "retention_cut", "retention_mode"),
        artifacts=("visuals/structure.visuals.json",),
        critical=False,
        manual_command="python -m editing.cli visuals plan "
                       "--visual-layer balanced",
    ),
    AutoStage(
        name="final_edit_plan",
        summary="Compose the cut, the captions, the sound and the visuals",
        requires=("visual_plan",),
        config_keys=("name", "style", "visual_mode",
                     "export_premiere_visual_plan"),
        artifacts=("visuals/structure.final.json",),
        critical=False,
        manual_command="python -m editing.cli visuals report --latest",
    ),
    AutoStage(
        name="render_proxy",
        summary="Render a watchable proxy of the rough cut with FFmpeg",
        requires=("roughcut_build",),
        config_keys=("name", "render_quality", "render_height"),
        # Not resumable, and cheap to re-reach anyway: the renderer keys on
        # the cut, the sources and the settings, so a resume over an unchanged
        # cut hands back the existing video in a fraction of a second. Naming
        # artifacts here instead would mean guessing the job ID, which is
        # derived from that key and therefore not knowable in advance.
        resumable=False,
        requires_ffmpeg=True,
        critical=False,
        manual_command="python -m editing.cli render roughcut",
    ),
    AutoStage(
        name="reliability_gates",
        summary="Check whether this run produced something usable",
        # Requires only the cut: a run whose render was blocked still deserves
        # its checks, and the render gates report ``skipped`` rather than
        # inventing a judgement about a video nobody made.
        requires=("roughcut_build",),
        config_keys=(),
        # Always re-run: the checks read files that can change between two
        # looks, and a cached "everything passed" over a deleted video is the
        # one answer this stage must never give.
        resumable=False,
        critical=False,
        manual_command="python -m editing.cli auto show-checks "
                       "--run <run_id>",
    ),
    AutoStage(
        name="feedback_start",
        summary="Open a review session over everything this run produced",
        requires=("roughcut_build",),
        config_keys=("name", "style"),
        # Not resumable, and deliberately idempotent instead: the runner
        # reuses this run's existing session rather than opening a second one,
        # so a resume adds to yesterday's review instead of splitting it.
        resumable=False,
        critical=False,
        manual_command="python -m editing.cli feedback start --run <run_id>",
    ),
    AutoStage(
        name="feedback_queue",
        summary="Work out what is actually worth a human looking at",
        requires=("feedback_start",),
        config_keys=("name", "style"),
        resumable=False,          # cheap, and it should reflect a resume
        critical=False,
        manual_command="python -m editing.cli feedback queue --run <run_id>",
    ),
    AutoStage(
        name="feedback_report",
        summary="Write the review session's summary and report",
        requires=("feedback_queue",),
        config_keys=("name",),
        resumable=False,
        critical=False,
        manual_command="python -m editing.cli feedback report --run <run_id>",
    ),
    AutoStage(
        name="review_package",
        summary="Gather the run into one review folder with an index",
        requires=("roughcut_build",),
        config_keys=(),
        # Always rebuilt, like the report: it is a *view* over everything
        # else, and a cached view is a view of something that has changed.
        resumable=False,
        critical=False,
        manual_command="python -m editing.cli review package --run <run_id>",
    ),
    AutoStage(
        name="report",
        summary="Write the JSON and human-readable run reports",
        requires=("roughcut_build",),
        config_keys=(),
        resumable=False,          # always regenerated: it summarises the rest
        critical=False,
        manual_command="python -m editing.cli auto report --run <run_id>",
    ),
)

BY_NAME = {stage.name: stage for stage in STAGES}

#: The transcription stage, on its own. Opt-in via ``--transcribe``, because
#: it loads a speech model -- but unlike the feedback stages it produces a file
#: the rest of the pipeline reads, so the report nags when it was skipped and
#: nothing else supplied a transcript.
TRANSCRIBE_STAGES = ("transcribe",)

#: Stages the review pass owns. Skipped as a group by ``--skip-review``, and
#: blocked as a group when FFmpeg or the model is unavailable.
REVIEW_STAGES = (
    "review_export_frames", "review_critique", "review_plan", "review_dry_run",
)

#: Stages the asset pass owns.
ASSET_STAGES = ("assets_index", "assets_plan", "assets_dry_run")

#: Stages the episode pass owns. Skipped as a group by ``--skip-episode``.
#: Neither executes anything, so neither has a dry run or a gate.
EPISODE_STAGES = ("episode_memory", "retention_plan")

#: The director pass, on its own. Opt-in via ``--director``: it needs a model
#: endpoint, and a pipeline that silently required one would stop working on
#: every machine that has not set one up.
DIRECTOR_STAGES = ("director_plan",)

#: The retention wiring, on its own. Opt-in via ``--retention-cut``: it
#: reshapes the episode, and reshaping somebody's episode is not a default.
RETENTION_STAGES = ("retention_cut",)

#: The proxy render, on its own. Opt-in via ``--render-proxy``: it is the
#: only stage that produces a file measured in hundreds of megabytes, and a
#: pipeline that quietly filled a disk would be a bad neighbour.
RENDER_STAGES = ("render_proxy",)

#: Stages the feedback collector owns. These are opt-*in* -- ``--feedback`` --
#: rather than opt-out, because they start something a person has to finish.
FEEDBACK_STAGES = ("feedback_start", "feedback_queue", "feedback_report")

#: The caption polish, on its own. Opt-in via ``--captions``: putting text on
#: somebody's video is not a default, and the mode says how much.
CAPTION_STAGES = ("caption_polish",)

#: The audio polish, on its own. Opt-in via ``--audio-polish``.
AUDIO_STAGES = ("audio_polish",)

#: The creative visual layer and the composer it feeds. Opt-in via
#: ``--visual-layer``: deciding where somebody's video should zoom, flash and
#: point at things is the least default-able thing in this system.
VISUAL_STAGES = ("visual_plan", "final_edit_plan")

#: The review package. Opt-*out* -- the only late addition that is on by
#: default -- because it creates nothing new, costs a fraction of a second,
#: and is the difference between a run somebody can inspect and forty files
#: they have to learn the layout of.
REVIEW_STAGES_PACKAGE = ("review_package",)


def stage(name: str) -> AutoStage:
    found = BY_NAME.get(name)
    if found is None:
        raise EditingError(
            f"Unknown stage '{name}'",
            hint="Stages, in order: " + ", ".join(s.name for s in STAGES),
        )
    return found


def dependents(name: str) -> list[str]:
    """Every stage that transitively depends on ``name``.

    Used when something invalidates a stage -- a config change, or an execution
    that changes what the later plans should say -- so the invalidation
    propagates instead of leaving a half-fresh run.
    """
    out: list[str] = []
    frontier = {name}
    for candidate in STAGES:
        if candidate.name in frontier:
            continue
        if set(candidate.requires) & frontier:
            frontier.add(candidate.name)
            out.append(candidate.name)
    return out


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------
#
# Each takes ``(pipeline, run, context)`` and returns
# ``(outputs, summary, warnings)``. ``context`` carries anything a later stage
# needs from an earlier one within a single process; nothing in it is required
# for a resume, because a resumed run reloads from artifacts instead.


def run_doctor(pipeline, run, context) -> tuple:
    """What is actually available. Never fails the run."""
    from editing import ffmpeg as ff
    from editing.visual import qwen

    config = pipeline.config
    ffmpeg_ok = ff.have_tool(config.ffmpeg)
    ffprobe_ok = ff.have_tool(config.ffprobe)
    vision = (
        {"reachable": True, "backend": "mock",
         "note": "mock backend: no real analysis is performed"}
        if run.mock else qwen.health(config)
    )

    premiere = {"connected": False, "note": "--no-premiere was set"}
    if not run.no_premiere:
        premiere = _premiere_health(pipeline)

    context["doctor"] = {
        "ffmpeg": ffmpeg_ok, "ffprobe": ffprobe_ok,
        "vision_reachable": bool(vision.get("reachable")),
        "premiere_connected": bool(premiere.get("connected")),
    }

    warnings: list[str] = []
    if not (ffmpeg_ok and ffprobe_ok):
        warnings.append(
            "FFmpeg is not on PATH. Visual analysis and review frames need it; "
            "the audio layer degrades to transcript markers only."
        )
    if not vision.get("reachable"):
        reason = vision.get("error") or "no reason given"
        warnings.append(
            f"The vision model is not reachable ({reason}). Use --mock to "
            "exercise the pipeline without it."
        )
    if not run.no_premiere and not premiere.get("connected"):
        warnings.append(
            "Premiere is not reachable, so nothing can be executed. Planning "
            "and dry runs are unaffected."
        )

    summary = {
        "ffmpeg": ffmpeg_ok, "ffprobe": ffprobe_ok,
        "vision": vision, "premiere": premiere, "mock": run.mock,
    }
    return [], summary, warnings


def _premiere_health(pipeline) -> dict:
    try:
        from premiere.bridge import bridge as default_bridge
    except ImportError:  # pragma: no cover - premiere always ships here
        return {"connected": False, "note": "the premiere package is missing"}
    transport = pipeline.bridge if pipeline.bridge is not None else default_bridge
    try:
        health = transport.health() or {}
    except Exception as exc:  # noqa: BLE001 - an unreachable host is a fact
        return {"connected": False, "error": str(exc)[:200]}
    return dict(health)


def run_discover(pipeline, run, context) -> tuple:
    assets = pipeline.discover(
        folder=run.footage_folder or None,
        recursive=run.recursive,
        use_premiere=(False if run.no_premiere else None),
    )
    if not assets:
        raise StageBlocked(
            "found no footage",
            f"there are no video files in {run.footage_folder!r}.",
            code="no_footage",
            next_command="python -m editing.cli discover --folder <folder>",
        )
    context["assets"] = assets
    return (
        [str(pipeline.config.assets_file)],
        {
            "files": len(assets),
            "total_seconds": round(sum(a.duration for a in assets), 2),
            "names": [a.filename for a in assets][:20],
        },
        [f"{a.filename}: {a.probe_error}" for a in assets if a.probe_error],
    )


def run_transcribe(pipeline, run, context) -> tuple:
    """Produce transcripts for the clips this run is about to analyse.

    Non-critical on purpose. A missing speech model, or one clip that will not
    decode, costs the story layer and not the run -- analysis, the rough cut,
    the style pass and the asset pass all work without a word of dialogue.
    They are just less good, and the report says so.
    """
    from editing.transcribe.schema import TranscriptionConfig

    # ``discover`` seeds the context, but on a resume it is satisfied from a
    # checkpoint and never runs -- so fall back to the discovery on disk, the
    # same way ``analyze`` re-derives its own assets.
    assets = context.get("assets")
    if not assets:
        try:
            assets = pipeline.ensure_assets(
                folder=run.footage_folder or None,
                recursive=run.recursive,
                use_premiere=(False if run.no_premiere else None),
            )
        except EditingError:
            assets = []
    if not assets:
        raise StageBlocked(
            "there is nothing to transcribe",
            "discovery found no media files.",
            code="no_assets",
            next_command="python -m editing.cli discover --folder <folder>",
        )

    settings = pipeline.transcription_config(
        model=run.transcribe_model or None,
        language=run.transcribe_language or None,
        backend=run.transcribe_backend or None,
    )
    health = pipeline.transcribe_status(settings)
    if not health.get("ready"):
        raise StageBlocked(
            "could not transcribe the footage",
            "faster-whisper is not installed, so no transcript can be made. "
            "The run continues without one; the story and retention layers "
            "will have nothing to read.",
            code="whisper_missing",
            next_command=str(health.get("hint") or "pip install faster-whisper"),
            detail={"health": health},
        )

    try:
        batch = pipeline.transcribe_assets(assets, settings=settings)
    except ToolMissingError as exc:
        raise StageBlocked(
            "could not transcribe the footage",
            f"{exc.message}.",
            code="whisper_missing",
            next_command="pip install faster-whisper, then: "
                         "python -m editing.cli auto resume --run <run_id>",
        ) from None

    context["transcription"] = batch
    stats = batch.stats()
    warnings = list(batch.warnings)
    for job in batch.failed:
        if job.failure is not None:
            warnings.append(
                f"{Path(job.source_path).name}: {job.failure.message}")
    return (
        [str(pipeline.config.transcripts_dir)],
        {
            "backend": settings.backend,
            "model": settings.model,
            "device": health.get("resolved_device", "?"),
            # Loud on purpose: a run whose transcripts were fabricated must
            # never look like a run that heard the footage.
            "mock": settings.backend == "mock",
            "files": stats["files"],
            "transcribed": stats["done"],
            "cached": stats["cached"],
            "skipped": stats["skipped"],
            "failed": stats["failed"],
            "words": stats["words"],
        },
        warnings,
    )


def run_analyze(pipeline, run, context) -> tuple:
    """Transcripts, audio, vision and the combined timeline."""
    try:
        timeline = pipeline.run(
            folder=run.footage_folder or None,
            recursive=run.recursive,
            keep_frames=run.keep_frames,
            use_motion=run.use_motion,
            max_windows=run.max_windows,
            use_premiere=(False if run.no_premiere else None),
        )
    except ToolMissingError as exc:
        raise StageBlocked(
            "could not analyse the footage",
            f"{exc.message}. Visual analysis extracts frames with FFmpeg.",
            code="ffmpeg_missing",
            next_command="Install FFmpeg, put it on PATH, then: "
                         "python -m editing.cli auto resume --run <run_id>",
        ) from None
    except ModelError as exc:
        raise StageBlocked(
            "could not reach the vision model",
            exc.message,
            code="model_unreachable",
            next_command="Start the model server, or re-run with --mock: "
                         "python -m editing.cli auto resume --run <run_id>",
            detail={"hint": exc.hint},
        ) from None

    target = pipeline.write_timeline(timeline, name=run.name)
    stats = timeline.stats()
    context["timeline"] = timeline

    # Words, counted here rather than taken from the transcription stage.
    # A transcript can arrive three ways -- Whisper, Premiere, or an .srt
    # sitting beside the footage -- and only this stage sees all three. The
    # reliability checks read this, so "no transcript" means no words in the
    # timeline rather than "the Whisper stage did not run".
    words = sum(len(segment.said.split()) for segment in timeline.segments)
    return (
        [str(target)],
        {
            "segments": stats["segments"],
            "usable_segments": stats["usable_segments"],
            "covered_seconds": stats["covered_seconds"],
            "by_importance": stats["by_importance"],
            "segments_with_speech": stats["segments_with_speech"],
            "transcript_words": words,
            "model": timeline.model,
        },
        list(timeline.warnings),
    )


def run_recommend(pipeline, run, context) -> tuple:
    recommendations = pipeline.recommend(
        context.get("timeline"), name=run.name
    )
    stats = recommendations.stats()
    context["recommendations"] = recommendations
    return (
        [str(pipeline.config.recommendations_dir / f"{run.name}.json")],
        {
            "total": stats["total"],
            "accepted": stats["accepted"],
            "actionable": stats["actionable"],
            "by_category": stats["by_category"],
        },
        list(recommendations.warnings),
    )


def run_director_plan(pipeline, run, context) -> tuple:
    """Ask the director what the cut is, and check every answer.

    Non-critical, like the review pass and for the same reason: a machine with
    no model endpoint still produces every plan, and losing this one costs the
    story-aware selection rather than the run. The rough cut stage then falls
    back to the thresholds and says in its own summary that it did.
    """
    from editing.director.schema import DirectorConfig

    settings = pipeline.director_config(
        backend=run.director_backend or None,
        model=run.director_model or None,
        mode=run.director_mode or None,
        style=run.style or None,
        target_duration=(run.target_duration or None),
    )
    health = pipeline.director_status(settings)
    if not health.get("ready"):
        raise StageBlocked(
            "could not run the director pass",
            f"the director model is not reachable "
            f"({health.get('error') or 'no reason given'}). The run continues "
            "and the rough cut will be chosen by the rule-based selector.",
            code="director_unreachable",
            next_command=str(health.get("hint")
                             or "python -m editing.cli director status"),
            detail={"health": health},
        )

    plan = pipeline.director_plan(
        name=run.name, settings=settings,
        style_guide_path=run.style_guide or "",
    )
    context["director_plan"] = plan

    if plan.failure is not None:
        raise StageBlocked(
            "the director pass produced no usable cut",
            plan.failure.message,
            code=plan.failure.code,
            next_command=plan.failure.hint
            or "python -m editing.cli director report",
            detail={"stage": plan.failure.stage},
        )

    stats = plan.stats()
    return (
        [str(pipeline.config.director_dir / f"{run.name}.plan.json")],
        {
            "backend": plan.backend,
            "model": plan.model,
            # Loud, for the same reason the transcription stage is: a cut
            # chosen by four fixed rules must never read as a directed one.
            "mock": plan.mock,
            "cached": plan.cached,
            "mode": plan.mode,
            "style_guide": plan.style_guide.name,
            "decisions": stats["decisions"],
            "accepted": stats["accepted"],
            "rejected": stats["rejected"],
            "modified": stats["modified"],
            "needs_human_review": stats["needs_human_review"],
            "ranges": stats["ranges"],
            "cut_duration": stats["cut_duration"],
        },
        list(plan.warnings),
    )


def run_roughcut_build(pipeline, run, context) -> tuple:
    from editing.roughcut.build import RoughCutOptions

    # The director stage is non-critical, so it may have been skipped,
    # blocked or have failed -- in every one of those cases this falls back to
    # the thresholds and the summary below says which selector actually ran.
    #
    # The test is ``ranges``, not ``is not None``. A blocked director stage
    # still leaves its plan in the context (the rejections are worth keeping),
    # and a plan with no ranges produces a threshold cut -- so keying on the
    # object's existence made the report say "hybrid" over a cut the
    # thresholds had chosen entirely, which is the one outcome this whole
    # layer is not allowed to produce.
    director = context.get("director_plan")
    if director is None and run.director:
        director = pipeline.director_plan_or_none(name=run.name)
    usable = director is not None and bool(director.ranges)
    mode = run.director_mode if (run.director and usable) else "heuristic"

    plan = pipeline.rough_cut(
        timeline=context.get("timeline"),
        recommendations=context.get("recommendations"),
        name=run.name,
        options=RoughCutOptions(mode=mode),
        director_plan=director,
        # Validated here so the saved plan carries ``dry_run_passed``. The
        # dry-run stage re-validates but writes nothing: rewriting the plan
        # would change its fingerprint and invalidate this stage's own
        # checkpoint, so every resume would rebuild the cut for no reason.
        validate=True,
    )
    if not plan.placements:
        raise StageBlocked(
            "built an empty rough cut",
            "nothing in the timeline scored high enough to keep.",
            code="empty_cut",
            next_command="python -m editing.cli roughcut build "
                         "--keep-threshold 0.2",
        )
    stats = plan.stats()
    context["roughcut"] = plan
    return (
        [str(pipeline.config.roughcut_dir / f"{run.name}.json")],
        {
            "sequence": plan.sequence_name,
            "clips": stats["placements"],
            "cut_duration": stats["cut_duration"],
            "source_duration": stats["source_duration"],
            "markers": stats["markers"],
            "operations": stats["operations"],
            "unconverted": stats["unconverted"],
            # Which selector actually chose the ranges. Not what was asked
            # for -- what happened.
            "selection": mode,
        },
        list(plan.warnings),
    )


def run_roughcut_dry_run(pipeline, run, context) -> tuple:
    plan = pipeline.load_rough_cut(name=run.name)
    # This stage writes nothing at all, deliberately.
    #
    # ``save=False`` because ``run_*`` would otherwise write the pass's
    # *execution report*, which is the same file a real execution writes: a dry
    # run after an execution would overwrite "executed: true" with "false", and
    # every later gate would believe the sequence had never been built.
    #
    # And the plan is not rewritten either, because that would change its
    # fingerprint and invalidate the *build* stage's checkpoint -- so every
    # resume would rebuild a plan that had not changed. The build stage already
    # validated and saved it.
    report = pipeline.run_rough_cut(
        plan, mode="dry_run", name=run.name, save=False
    )
    context["roughcut"] = plan
    if not plan.dry_run_passed:
        error = plan.dry_run_error or {}
        raise StageBlocked(
            "could not validate the rough cut plan",
            error.get("error", "the plan did not validate."),
            code=str(error.get("code") or "dry_run_failed"),
            next_command="python -m editing.cli roughcut dry-run",
            detail={"hint": error.get("hint", "")},
        )
    return (
        [str(pipeline.config.roughcut_dir / f"{run.name}.json")],
        {
            "dry_run_passed": True,
            "operations": plan.operation_count,
            "executed": report.executed,
        },
        list(report.warnings),
    )


def run_review_export_frames(pipeline, run, context) -> tuple:
    plan = context.get("roughcut") or pipeline.load_rough_cut(name=run.name)
    review = pipeline.review_frames(plan, name=run.name)
    if not review.frames:
        raise StageBlocked(
            "exported no review frames",
            "; ".join(review.warnings[:2])
            or "no frame could be extracted from the source files.",
            code="no_frames",
            next_command="python -m editing.cli review export-frames --list",
        )
    context["review"] = review
    return (
        [str(pipeline.review_manifest_path(plan))],
        {"frames": len(review), **review.stats()},
        list(review.warnings),
    )


def run_review_critique(pipeline, run, context) -> tuple:
    try:
        report = pipeline.critique(context.get("review"), name=run.name)
    except ModelError as exc:
        raise StageBlocked(
            "could not reach the critic model",
            exc.message,
            code="model_unreachable",
            next_command="Start the model server, or re-run with --mock.",
            detail={"hint": exc.hint},
        ) from None

    stats = report.stats()
    context["critique"] = report
    return (
        [str(pipeline.config.critic_dir / f"{run.name}.critique.json")],
        {
            "mock": report.mock,
            "model": report.model,
            "frames_examined": stats["frames_examined"],
            "findings": stats["findings"],
            "by_issue": stats["by_issue"],
            "by_severity": stats["by_severity"],
        },
        list(report.warnings),
    )


def run_review_plan(pipeline, run, context) -> tuple:
    revisions, plan = pipeline.revise(
        name=run.name,
        critique=context.get("critique"),
        review=context.get("review"),
        roughcut=context.get("roughcut"),
    )
    stats = revisions.stats()
    context["revisions"] = revisions
    context["revision_plan"] = plan
    return (
        [
            str(pipeline.config.critic_dir / f"{run.name}.revisions.json"),
            str(pipeline.config.critic_dir / f"{run.name}.revision-plan.json"),
        ],
        {
            "revisions": stats["total"],
            "accepted": stats["accepted"],
            "needs_human_review": stats["needs_human_review"],
            "operations": plan.operation_count,
            "mock": revisions.mock,
        },
        list(revisions.warnings) + list(plan.warnings),
    )


def run_review_dry_run(pipeline, run, context) -> tuple:
    plan = context.get("revision_plan") or pipeline.load_revision_plan(
        name=run.name
    )
    # This stage writes nothing at all, deliberately.
    #
    # ``save=False`` because ``run_*`` would otherwise write the pass's
    # *execution report*, which is the same file a real execution writes: a dry
    # run after an execution would overwrite "executed: true" with "false", and
    # every later gate would believe the sequence had never been built.
    #
    # And the plan is not rewritten either, because that would change its
    # fingerprint and invalidate the *build* stage's checkpoint -- so every
    # resume would rebuild a plan that had not changed. The build stage already
    # validated and saved it.
    report = pipeline.run_revisions(
        plan, mode="dry_run", name=run.name, save=False
    )
    context["revision_plan"] = plan

    if not plan.dry_run_passed:
        error = plan.dry_run_error or {}
        if error.get("code") == "empty_plan":
            # Not a failure: the critic found nothing it could fix safely,
            # which is a normal and common outcome.
            return (
                [],
                {"dry_run_passed": False, "operations": 0, "empty": True},
                ["The critic found nothing that converts into an operation, "
                 "so there is no revision plan to execute."],
            )
        raise StageBlocked(
            "could not validate the revision plan",
            error.get("error", "the plan did not validate."),
            code=str(error.get("code") or "dry_run_failed"),
            next_command="python -m editing.cli review dry-run",
            detail={"hint": error.get("hint", "")},
        )
    return (
        [str(pipeline.config.critic_dir / f"{run.name}.revision-plan.json")],
        {"dry_run_passed": True, "operations": plan.operation_count,
         "executed": report.executed},
        list(report.warnings),
    )


def run_layers_build(pipeline, run, context) -> tuple:
    from editing.style import presets as style_presets
    from editing.style.compile import CompileOptions

    style = style_presets.get(run.style)
    plan = pipeline.layers(
        name=run.name,
        style=style,
        timeline=context.get("timeline"),
        recommendations=context.get("recommendations"),
        roughcut=context.get("roughcut"),
        revisions=context.get("revisions"),
        options=CompileOptions(markers_only=run.markers_only),
    )
    density = plan.density()
    stats = plan.stats()
    context["layers"] = plan
    return (
        [str(pipeline.config.layers_dir / f"{run.name}.json")],
        {
            "style": plan.style,
            "planned": stats["planned"],
            "deferred": stats["deferred"],
            "operations": stats["operations"],
            "marker_only": stats["marker_only"],
            "edits_per_minute": density["edits_per_minute"],
            "captions_per_minute": density["captions_per_minute"],
            "zooms_per_minute": density["zooms_per_minute"],
            "by_layer": density["by_layer"],
        },
        list(plan.warnings),
    )


def run_layers_dry_run(pipeline, run, context) -> tuple:
    plan = context.get("layers") or pipeline.load_layers(name=run.name)
    # This stage writes nothing at all, deliberately.
    #
    # ``save=False`` because ``run_*`` would otherwise write the pass's
    # *execution report*, which is the same file a real execution writes: a dry
    # run after an execution would overwrite "executed: true" with "false", and
    # every later gate would believe the sequence had never been built.
    #
    # And the plan is not rewritten either, because that would change its
    # fingerprint and invalidate the *build* stage's checkpoint -- so every
    # resume would rebuild a plan that had not changed. The build stage already
    # validated and saved it.
    report = pipeline.run_layers(
        plan, mode="dry_run", name=run.name, save=False
    )
    context["layers"] = plan

    if not plan.dry_run_passed:
        error = plan.dry_run_error or {}
        if error.get("code") == "empty_plan":
            return (
                [],
                {"dry_run_passed": False, "operations": 0, "empty": True},
                ["This style planned nothing that converts into an operation. "
                 "Run `layers show-deferred` to see what it held back."],
            )
        raise StageBlocked(
            "could not validate the layered plan",
            error.get("error", "the plan did not validate."),
            code=str(error.get("code") or "dry_run_failed"),
            next_command="python -m editing.cli layers dry-run",
            detail={"hint": error.get("hint", "")},
        )
    return (
        [str(pipeline.config.layers_dir / f"{run.name}.json")],
        {"dry_run_passed": True, "operations": plan.operation_count,
         "executed": report.executed},
        list(report.warnings),
    )


def run_assets_index(pipeline, run, context) -> tuple:
    from editing.assets import library as asset_library

    root = run.asset_library or None
    target = asset_library.resolve_root(pipeline.config, root)

    # A missing library folder is *not* a blocker. The asset pass with an empty
    # library is the single most useful thing this pipeline produces for
    # somebody who has no sounds yet: it is the shopping list. Blocking here
    # would withhold exactly that.
    library = pipeline.index_assets(
        root=root, previous=context.get("shared_asset_library")
    )
    stats = library.stats()
    context["asset_library"] = library

    warnings = list(library.warnings)
    if not Path(target).exists():
        warnings.insert(0, (
            f"There is no asset library at {target}, so this ran against an "
            "empty one. Every placeholder will be reported as a missing "
            "asset, which is a useful shopping list. Create the folders with: "
            "python -m editing.cli assets init"
            + (f" --root {root}" if root else "")
        ))
    return (
        [str(asset_library.index_path(pipeline.config, root))],
        {
            "root": library.root,
            "total": stats["total"],
            "usable": stats["usable"],
            "needs_review": stats["needs_review"],
            "by_category": stats["by_category"],
            "library_exists": Path(target).exists(),
        },
        warnings,
    )


def run_assets_plan(pipeline, run, context) -> tuple:
    from editing.assets.compile import AssetOptions

    plan = pipeline.asset_plan(
        name=run.name,
        root=run.asset_library or None,
        library=context.get("asset_library"),
        layers=context.get("layers"),
        timeline=context.get("timeline"),
        revisions=context.get("revisions"),
        options=AssetOptions(markers_only=run.markers_only),
    )
    stats = plan.stats()
    context["asset_plan"] = plan
    return (
        [str(pipeline.config.asset_library_dir / f"{run.name}.placement.json")],
        {
            "placeholders": stats["placeholders"],
            "placed": stats["placed"],
            "missing": stats["missing"],
            "rejected": stats["rejected"],
            "unsafe": stats["unsafe"],
            "marker_only": stats["marker_only"],
            "distinct_assets": stats["distinct_assets"],
            "operations": stats["operations"],
        },
        list(plan.warnings),
    )


def run_assets_dry_run(pipeline, run, context) -> tuple:
    plan = context.get("asset_plan") or pipeline.load_asset_plan(name=run.name)
    # This stage writes nothing at all, deliberately.
    #
    # ``save=False`` because ``run_*`` would otherwise write the pass's
    # *execution report*, which is the same file a real execution writes: a dry
    # run after an execution would overwrite "executed: true" with "false", and
    # every later gate would believe the sequence had never been built.
    #
    # And the plan is not rewritten either, because that would change its
    # fingerprint and invalidate the *build* stage's checkpoint -- so every
    # resume would rebuild a plan that had not changed. The build stage already
    # validated and saved it.
    report = pipeline.run_assets(
        plan, mode="dry_run", name=run.name, save=False
    )
    context["asset_plan"] = plan

    if not plan.dry_run_passed:
        error = plan.dry_run_error or {}
        if error.get("code") == "empty_plan":
            return (
                [],
                {"dry_run_passed": False, "operations": 0, "empty": True},
                ["Nothing in the layered edit needed an asset, so there is no "
                 "placement plan to execute."],
            )
        raise StageBlocked(
            "could not validate the asset placement plan",
            error.get("error", "the plan did not validate."),
            code=str(error.get("code") or "dry_run_failed"),
            next_command="python -m editing.cli assets dry-run",
            detail={"hint": error.get("hint", "")},
        )
    return (
        [str(pipeline.config.asset_library_dir
             / f"{run.name}.placement.json")],
        {"dry_run_passed": True, "operations": plan.operation_count,
         "executed": report.executed},
        list(report.warnings),
    )


#: Stage name -> runner. ``report`` is absent on purpose: the orchestrator
#: writes it after every other stage has settled, because it summarises them.
RUNNERS = {
    "doctor": run_doctor,
    "discover": run_discover,
    "transcribe": run_transcribe,
    "analyze": run_analyze,
    "recommend": run_recommend,
    "director_plan": run_director_plan,
    "roughcut_build": run_roughcut_build,
    "roughcut_dry_run": run_roughcut_dry_run,
    "review_export_frames": run_review_export_frames,
    "review_critique": run_review_critique,
    "review_plan": run_review_plan,
    "review_dry_run": run_review_dry_run,
    "layers_build": run_layers_build,
    "layers_dry_run": run_layers_dry_run,
    "assets_index": run_assets_index,
    "assets_plan": run_assets_plan,
    "assets_dry_run": run_assets_dry_run,
}


def run_episode_memory(pipeline, run, context) -> tuple:
    """Read the story off the cut. Touches nothing and executes nothing."""
    memory = pipeline.episode_memory(
        name=run.name,
        timeline=context.get("timeline"),
        roughcut=context.get("roughcut"),
        recommendations=context.get("recommendations"),
        layers=context.get("layers"),
        asset_plan=context.get("asset_plan"),
    )
    context["episode_memory"] = memory
    stats = memory.stats()
    objective = memory.main_objective
    return (
        [str(pipeline.config.episode_dir / f"{run.name}.memory.json")],
        {
            "timebase": memory.timebase,
            "beats": stats["beats"],
            "labelled_beats": stats["labelled_beats"],
            "open_loops": stats["open_loops"],
            "resolved_loops": stats["resolved_loops"],
            "callbacks": stats["callbacks"],
            "objective": (objective.text[:80] if objective else None),
            "objective_status": (objective.status if objective else "none"),
        },
        list(memory.warnings),
    )


def run_retention_plan(pipeline, run, context) -> tuple:
    """Risks, hooks, a peak, an ending and the suggestions that follow."""
    plan = pipeline.retention_plan(
        name=run.name,
        memory=context.get("episode_memory"),
        timeline=context.get("timeline"),
        roughcut=context.get("roughcut"),
    )
    context["retention_plan"] = plan
    stats = plan.stats()
    return (
        [str(pipeline.config.episode_dir / f"{run.name}.retention.json")],
        {
            "risks": stats["risks"],
            "high_severity": stats["high_severity"],
            "hooks": stats["hooks"],
            "has_climax": stats["has_climax"],
            "has_ending": stats["has_ending"],
            "suggestions": stats["suggestions"],
            "auto_safe": stats["auto_safe"],
            "marker_only": stats["marker_only"],
        },
        list(plan.warnings),
    )


RUNNERS["episode_memory"] = run_episode_memory
RUNNERS["retention_plan"] = run_retention_plan


# ---------------------------------------------------------------------------
# Feedback (Session 9)
# ---------------------------------------------------------------------------
#
# None of these executes anything, trains anything, or blocks anything. They
# are non-critical and opt-in, and a failure in any of them costs the review
# and not the run.

def run_feedback_start(pipeline, run, context) -> tuple:
    """Open a review session for this run, or reuse the one it already has.

    Idempotent on purpose. ``feedback_start`` is not resumable, so a resumed
    run reaches it again -- and creating a second session would split one
    review across two logs with no way to tell which was current.
    """
    from editing.feedback import store as feedback_store

    run_id = str(context.get("run_id") or "")
    existing = feedback_store.latest_session(
        pipeline.config, run_id=run_id, open_only=True)
    if existing is not None:
        context["feedback_session"] = existing
        return (
            [str(feedback_store.session_dir(
                pipeline.config, existing.session_id))],
            {"session_id": existing.session_id, "reused": True,
             "items": (existing.counts or {}).get("items", 0)},
            [],
        )

    session, _queue = pipeline.feedback_start(
        name=run.name,
        run_id=run_id,
        title=f"auto run {run_id}",
        build_queue=False,        # the next stage owns the queue
    )
    context["feedback_session"] = session
    return (
        [str(feedback_store.session_dir(pipeline.config, session.session_id))],
        {"session_id": session.session_id, "reused": False,
         "timebase": session.timebase,
         "reviewed_without": len(
             [k for k, v in session.sources.items() if not v]),
         },
        list(session.warnings),
    )


def run_feedback_queue(pipeline, run, context) -> tuple:
    """Build the review queue from everything this run produced."""
    from editing.feedback import store as feedback_store

    session = context.get("feedback_session")
    if session is None:
        session = feedback_store.resolve_session(
            pipeline.config, run_id=str(context.get("run_id") or ""))
    queue = pipeline.feedback_queue(session=session)
    context["feedback_queue"] = queue
    stats = queue.stats()
    return (
        [str(feedback_store.queue_path(pipeline.config, session.session_id))],
        {
            "session_id": session.session_id,
            "questions": stats["prompts"],
            "worth_reviewing": stats["candidates"],
            "groups": stats["groups"],
            "high_impact": stats["by_flag"].get("high_impact", 0),
            "uncertain": stats["by_flag"].get("uncertain", 0),
            "risky_automatic": stats["by_flag"].get("risky_automatic", 0),
            "structural": stats["by_flag"].get("structural", 0),
            "retention_risk": stats["by_flag"].get("retention_risk", 0),
        },
        list(queue.warnings),
    )


def run_feedback_report(pipeline, run, context) -> tuple:
    """Write the session's summary and report, empty though they will be."""
    from editing.feedback import store as feedback_store

    session = context.get("feedback_session")
    if session is None:
        session = feedback_store.resolve_session(
            pipeline.config, run_id=str(context.get("run_id") or ""))
    summary = pipeline.feedback_summary(session)
    counts = summary.get("counts", {})
    coverage = summary.get("coverage", {})
    return (
        [str(feedback_store.report_path(pipeline.config, session.session_id))],
        {
            "session_id": session.session_id,
            "items": counts.get("items", 0),
            "answered": coverage.get("answered", 0),
            "queued": coverage.get("queued", 0),
        },
        [],
    )


def run_retention_cut(pipeline, run, context) -> tuple:
    """Wire the retention findings into the cut.

    Non-critical: a run with no usable retention plan still produced every
    other plan, and the rough cut it would have reshaped is untouched. The
    render stage then renders whichever cut exists, and the report says which.
    """
    settings = pipeline.retention_config(
        mode=run.retention_mode or None,
        cold_open=(None if run.cold_open else False),
        max_cold_open_seconds=(run.max_cold_open_seconds or None),
        dead_air_aggressiveness=(run.dead_air_aggressiveness or None),
        style=run.style or None,
    )

    plan, cut = pipeline.retention_cut(
        name=run.name,
        settings=settings,
        timeline=context.get("timeline"),
        memory=context.get("episode_memory"),
        retention=context.get("retention_plan"),
        roughcut=context.get("roughcut"),
        recommendations=context.get("recommendations"),
        director_plan=context.get("director_plan"),
    )
    context["retention_cut_plan"] = plan
    if cut is not None:
        context["retention_roughcut"] = cut

    if plan.failure is not None:
        raise StageBlocked(
            "could not wire the retention findings into the cut",
            plan.failure.message,
            code=plan.failure.code,
            next_command=plan.failure.hint
            or "python -m editing.cli retention plan --mode report_only",
            detail={"stage": plan.failure.stage},
        )

    stats = plan.stats()
    cold = plan.cold_open
    return (
        [str(pipeline.config.retention_dir / f"{run.name}.plan.json")],
        {
            "mode": plan.mode,
            "base": plan.base,
            # Loud on purpose: a run whose cut was *not* reshaped must never
            # read as one that was.
            "applied": plan.applied,
            "cold_open": cold.chosen,
            "cold_open_type": cold.hook_type if cold.chosen else "",
            "cold_open_seconds": stats["cold_open_seconds"],
            "zones_compressed": stats["zones_compressed"],
            "seconds_removed": stats["seconds_removed"],
            "setups_protected": stats["setups_protected"],
            "payoffs_protected": stats["payoffs_protected"],
            "dead_air_cut": stats["dead_air_cut"],
            "refused": stats["rejected"],
            "unresolved": stats["unresolved_warnings"],
            "cut_duration": stats["cut_duration"],
            "base_duration": stats["base_duration"],
        },
        list(plan.warnings),
    )


def run_render_proxy(pipeline, run, context) -> tuple:
    """Render the rough cut to a proxy MP4. Touches no host application.

    Non-critical, like the review pass and for the same reason: a machine with
    no FFmpeg still produced every plan, and losing the video costs the
    watching, not the run.

    The renderer's own cache does the work a checkpoint would: it keys on the
    cut, the source files and the settings, so reaching this stage again after
    a resume hands back the existing video instead of re-encoding it.
    """
    from editing.errors import ToolMissingError as _ToolMissing
    from editing.render.schema import INSTALL_HINT

    settings = pipeline.render_config(
        quality=run.render_quality or None,
        height=(run.render_height or None),
    )
    health = pipeline.render_status(settings)
    if not health.get("ready"):
        raise StageBlocked(
            "could not render a proxy",
            "FFmpeg is not installed, so no video could be made. Every plan "
            "this run produced is unaffected.",
            code="ffmpeg_missing",
            next_command=str(health.get("hint") or INSTALL_HINT),
            detail={"health": health},
        )

    # The retention cut when there is one, because that is the cut this run
    # actually produced. Rendering the pre-retention cut while the report says
    # a cold open was chosen would show a video that does not match its own
    # description.
    roughcut = context.get("retention_roughcut") or context.get("roughcut")
    try:
        job = pipeline.render_roughcut(
            name=run.name, plan=roughcut, settings=settings)
    except _ToolMissing as exc:
        raise StageBlocked(
            "could not render a proxy",
            f"{exc.message}.",
            code="ffmpeg_missing",
            next_command=INSTALL_HINT,
        ) from None

    context["render"] = job
    if job.failure is not None:
        raise StageBlocked(
            "could not render a proxy",
            job.failure.message,
            code=job.failure.code,
            next_command=job.failure.hint
            or f"python -m editing.cli render show {job.job_id}",
            detail={"job_id": job.job_id, "stage": job.failure.stage},
        )

    # Captions are never burned into a proxy -- the render joins pre-encoded
    # segments, and adding text would mean re-encoding the joined file. The
    # sidecar beside the video is the honest alternative, and it costs nothing.
    sidecar = ""
    caption_plan = context.get("caption_plan")
    if caption_plan is not None and job.output_path:
        written = pipeline.caption_sidecar_beside(
            job.output_path, name=run.name, plan=caption_plan)
        sidecar = str(written) if written is not None else ""

    # And the visual marker file, for the same reason: no treatment in that
    # plan is in the video either, and a file beside it is the honest way to
    # see where each one would land.
    visual_markers = ""
    visuals = context.get("visual_plan")
    final = context.get("final_edit")
    if visuals is not None and final is not None and job.output_path:
        written = pipeline.visual_markers_beside(
            job.output_path, name=run.name, visuals=visuals, final=final)
        visual_markers = str(written) if written is not None else ""

    result = job.result
    return (
        [job.output_path, job.notes_path],
        {
            "job_id": job.job_id,
            "video": job.output_path,
            "notes": job.notes_path,
            "subtitles": sidecar,
            "visual_markers": visual_markers,
            "clips": len(job.segments),
            "duration": job.duration,
            "quality": settings.quality,
            "height": settings.height,
            # Loud, for the same reason the transcription stage is: a run
            # whose "render" is a placeholder must never read as a run you
            # could watch.
            "mock": bool(result and result.mock),
            "rendered": bool(result and result.rendered),
            "cached": bool(result and result.from_cache),
            "size_mb": result.size_mb if result else 0.0,
            "elapsed": round(result.elapsed, 1) if result else 0.0,
            "not_shown": len(job.unsupported),
        },
        list(job.warnings),
    )


# ---------------------------------------------------------------------------
# Polish, checks and the review package (Session 11)
# ---------------------------------------------------------------------------
#
# None of these executes anything, needs a tool, or changes a frame of the cut.
# All four are non-critical: losing the captions costs the captions.


def _polish_cut(pipeline, run, context):
    """The cut this run actually produced, and what to call it.

    The retention variant when there is one, because that is what the render
    stage renders and what a person will watch. Planning captions against the
    pre-retention cut while the video is the reshaped one would put every
    caption at the wrong moment -- and it would do it silently, which is the
    worst kind of wrong this pass could be.
    """
    cut = context.get("retention_roughcut")
    if cut is None and run.retention_cut:
        cut = pipeline.retention_roughcut_or_none(name=run.name)
    if cut is not None:
        return cut, "retention"
    cut = context.get("roughcut") or pipeline.load_rough_cut(name=run.name)
    return cut, "roughcut"


def run_caption_polish(pipeline, run, context) -> tuple:
    """Decide which spoken lines earn a caption."""
    from editing.style import presets as style_presets

    style = style_presets.get(run.style)
    settings = pipeline.caption_config(
        style, mode=run.captions,
        max_per_minute=(run.max_captions_per_minute or None),
        max_seconds=(run.max_caption_seconds or None),
        max_words=(run.max_caption_words or None),
        min_confidence=(run.min_caption_confidence or None),
        require_confidence=(run.require_caption_confidence or None),
    )
    cut, base = _polish_cut(pipeline, run, context)

    plan = pipeline.polish_captions(
        name=run.name,
        timeline=context.get("timeline"),
        cut=cut,
        style=style,
        settings=settings,
        memory=context.get("episode_memory"),
    )
    context["caption_plan"] = plan

    stats = plan.stats()
    return (
        [str(pipeline.config.polish_dir / f"{run.name}.captions.json")],
        {
            "mode": plan.mode,
            "base": base,
            "considered": stats["considered"],
            "accepted": stats["accepted"],
            "rejected": stats["rejected"],
            "captions_per_minute": stats["captions_per_minute"],
            "ceiling": settings.max_per_minute,
            "longest_seconds": stats["longest_seconds"],
            "by_moment": stats["by_moment"],
            "by_reject_reason": stats["by_reject_reason"],
            # Loud on purpose, for the same reason the render stage's mock
            # flag is: a video with no captions in it must never read as a
            # video with captions in it.
            "burned_in": plan.burned_in,
            "sidecar": plan.sidecar_path,
        },
        list(plan.warnings),
    )


def run_audio_polish(pipeline, run, context) -> tuple:
    """Decide which moments earn a sound, and whether anything can play it."""
    from editing.style import presets as style_presets

    style = style_presets.get(run.style)
    settings = pipeline.audio_polish_config(
        style, mode=run.audio_polish,
        max_sfx_per_minute=(run.max_sfx_per_minute or None),
        music_bed=run.music_bed,
        ducking=run.ducking,
    )
    cut, base = _polish_cut(pipeline, run, context)

    library = None
    if settings.uses_library:
        library = context.get("asset_library") or pipeline.asset_library_or_empty(
            root=run.asset_library or None)

    plan = pipeline.polish_audio(
        name=run.name,
        timeline=context.get("timeline"),
        cut=cut,
        style=style,
        settings=settings,
        library=library,
    )
    context["audio_plan"] = plan

    stats = plan.stats()
    return (
        [str(pipeline.config.polish_dir / f"{run.name}.audio.json")],
        {
            "mode": plan.mode,
            "base": base,
            "considered": stats["considered"],
            "accepted": stats["accepted"],
            "rejected": stats["rejected"],
            "placed": stats["placed"],
            "placeholders": stats["placeholders"],
            "missing_assets": stats["missing_assets"],
            "effects": stats["effects"],
            "sfx_per_minute": stats["sfx_per_minute"],
            "ceiling": settings.max_sfx_per_minute,
            "by_kind": stats["by_kind"],
            "by_reject_reason": stats["by_reject_reason"],
            # Same reason as above: a plan of notes must never read as sound.
            "plays_anything": bool(plan.placed),
        },
        list(plan.warnings),
    )


def run_visual_plan(pipeline, run, context) -> tuple:
    """Find the moments that earn visual emphasis, and refuse most of them.

    Non-critical, like every other polish pass: losing the visual layer costs
    the visual layer. The cut, the captions and the sound are untouched by it.
    """
    from editing.style import presets as style_presets

    style = style_presets.get(run.style)
    settings = pipeline.visual_config(
        style, layer=run.visual_layer, mode=run.visual_mode,
        max_effects_per_minute=(run.max_effects_per_minute or None),
        max_callouts_per_minute=(run.max_callouts_per_minute or None),
        allow_freeze_frames=run.allow_freeze_frames,
        allow_callouts=run.allow_callouts,
        allow_replays=run.allow_replays,
        allow_screen_shake=run.allow_screen_shake,
    )
    cut, base = _polish_cut(pipeline, run, context)

    visuals, final = pipeline.plan_visuals(
        name=run.name,
        timeline=context.get("timeline"),
        cut=cut,
        style=style,
        settings=settings,
        director_plan=context.get("director_plan"),
        retention_plan=context.get("retention_cut_plan"),
        caption_plan=context.get("caption_plan"),
        audio_plan=context.get("audio_plan"),
        memory=context.get("episode_memory"),
        retention_findings=context.get("retention_plan"),
        base=base,
        run_id=str(context.get("run_id") or ""),
    )
    context["visual_plan"] = visuals
    context["final_edit"] = final

    stats = visuals.stats()
    return (
        [str(pipeline.config.visuals_dir / f"{run.name}.visuals.json")],
        {
            "layer": visuals.layer,
            "base": base,
            "moments": stats["moments"],
            "considered": stats["considered"],
            "accepted": stats["accepted"],
            "rejected": stats["rejected"],
            "lowered": stats["lowered"],
            "untreated_moments": stats["untreated_moments"],
            "effects_per_minute": stats["effects_per_minute"],
            "callouts_per_minute": stats["callouts_per_minute"],
            "ceiling": settings.max_effects_per_minute,
            "by_family": stats["by_family"],
            "by_effect": stats["by_effect"],
            "by_moment_kind": stats["by_moment_kind"],
            "by_reject_reason": stats["by_reject_reason"],
            "placeholder_only": stats["placeholder_only"],
            # Loud on purpose, for the same reason the caption stage's
            # ``burned_in`` is: a plan of intentions must never read as a
            # video with effects in it.
            "rendered": False,
        },
        list(visuals.warnings),
    )


def run_final_edit_plan(pipeline, run, context) -> tuple:
    """Compose the cut, the captions, the sound and the visuals into one plan.

    The composer already ran inside ``visual_plan`` -- they share a pass
    because the composer needs the plan object rather than the file. This
    stage exists to report on it separately and to write the Premiere
    operation plan when the run asked for one it would not otherwise get.
    """
    from editing.visuals import store as visuals_store

    final = context.get("final_edit")
    if final is None:
        final = pipeline.final_edit_or_none(name=run.name)
    if final is None:
        raise StageBlocked(
            "there is no final edit plan to report on",
            "the visual pass produced no plan, so there is nothing to "
            "compose.",
            code="no_visual_plan",
            next_command="python -m editing.cli visuals plan "
                         "--visual-layer balanced",
        )

    outputs = [str(visuals_store.final_path(pipeline.config, run.name))]

    # ``--export-premiere-visual-plan`` is the switch for somebody who planned
    # in a mode that does not build one but wants it anyway. It still executes
    # nothing.
    premiere = final.execution.premiere
    if premiere is None and run.export_premiere_visual_plan:
        premiere = pipeline.export_visual_premiere_plan(
            name=run.name, visuals=context.get("visual_plan"))
        final.execution.premiere = premiere
        visuals_store.save_final(pipeline.config, final, name=run.name)
    if premiere is not None:
        outputs.append(
            str(visuals_store.premiere_path(pipeline.config, run.name)))

    stats = final.stats()
    return (
        outputs,
        {
            "mode": final.mode,
            "segments": stats["segments"],
            "busy_segments": stats["busy_segments"],
            "untouched_segments": stats["untouched_segments"],
            "visual_treatments": stats["visual_treatments"],
            "captions": stats["captions"],
            "audio_cues": stats["audio_cues"],
            "premiere_operations": (
                premiere.operation_count if premiere else 0),
            "premiere_unsupported": len(premiere.unsupported) if premiere else 0,
            "premiere_dry_run_passed": (
                bool(premiere.dry_run_passed) if premiere else False),
            "preview_burnable": stats["execution_preview_burnable"],
            "placeholder_only": stats["execution_placeholder_only"],
            # Same reason as above, and the field the report reads to refuse
            # to claim otherwise.
            "executed": False,
        },
        list(final.warnings),
    )


def run_reliability_gates(pipeline, run, context) -> tuple:
    """Check whether this run produced something usable.

    Never fails the run, whatever it finds. A gate that says the output is
    unusable has said the most useful thing it can; stopping the pipeline on
    top of that would only cost the report that explains it.
    """
    from editing.reliability import report as gate_report
    from editing.reliability import run as reliability_run

    state = context.get("run_state")
    if state is None:
        raise StageBlocked(
            "could not run the reliability checks",
            "this stage needs the run's own state and did not get it.",
            code="no_run_state",
            next_command="python -m editing.cli auto show-checks "
                         "--run <run_id>",
        )

    # The shared config, not the pipeline's: this writes into the run folder
    # rather than among the run's artifacts, and the pipeline's config is
    # already scoped one level inside it.
    config = context.get("shared_config") or pipeline.config

    report, _inputs = reliability_run.check_run(
        config, state,
        caption_plan=context.get("caption_plan"),
        audio_plan=context.get("audio_plan"),
    )
    context["gate_report"] = report

    outputs: list = []
    target = reliability_run.report_path(config, state.run_id)
    if target is not None:
        import json

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2,
                       default=str),
            encoding="utf-8",
        )
        text = target.with_suffix(".txt")
        text.write_text(gate_report.render(report), encoding="utf-8")
        outputs = [str(target), str(text)]

    stats = report.stats()
    return (
        outputs,
        {
            "status": stats["status"],
            "usable": stats["usable"],
            "passed": stats["passed"],
            "warned": stats["warned"],
            "failed": stats["failed"],
            "skipped": stats["skipped"],
            "blocking": stats["blocking"],
            "failures": [r.name for r in report.failures],
            "warnings": [r.name for r in report.warnings],
        },
        [f"{r.name}: {r.reason}" for r in report.failures + report.warnings],
    )


def run_review_package(pipeline, run, context) -> tuple:
    """Gather everything this run produced into one folder with an index."""
    from editing.review import build as review_build

    state = context.get("run_state")
    if state is None:
        raise StageBlocked(
            "could not build the review package",
            "this stage needs the run's own state and did not get it.",
            code="no_run_state",
            next_command="python -m editing.cli review package --run <run_id>",
        )

    # Shared config, for the same reason the checks use it: the package lives
    # beside the run's artifacts, not inside them.
    config = context.get("shared_config") or pipeline.config

    package, written = review_build.write_package(
        config, state,
        checks=context.get("gate_report"),
        caption_plan=context.get("caption_plan"),
        audio_plan=context.get("audio_plan"),
        visual_plan=context.get("visual_plan"),
        final_edit=context.get("final_edit"),
    )
    context["review_package"] = package

    stats = package.stats()
    from editing.review import store as review_store

    return (
        [str(path) for path in written],
        {
            "folder": package.folder,
            "index": str(review_store.index_path(config, state.run_id)),
            "items": stats["items"],
            "present": stats["present"],
            "has_video": stats["has_video"],
            "watch_for": stats["watch_for"],
            "weak_points": stats["weak_points"],
            "decisions_needed": stats["decisions_needed"],
            "checks_status": stats["checks_status"],
        },
        [],
    )


RUNNERS["retention_cut"] = run_retention_cut
RUNNERS["caption_polish"] = run_caption_polish
RUNNERS["audio_polish"] = run_audio_polish
RUNNERS["visual_plan"] = run_visual_plan
RUNNERS["final_edit_plan"] = run_final_edit_plan
RUNNERS["render_proxy"] = run_render_proxy
RUNNERS["reliability_gates"] = run_reliability_gates
RUNNERS["feedback_start"] = run_feedback_start
RUNNERS["feedback_queue"] = run_feedback_queue
RUNNERS["feedback_report"] = run_feedback_report
RUNNERS["review_package"] = run_review_package

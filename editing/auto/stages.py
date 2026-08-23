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
        name="roughcut_build",
        summary="Select ranges and lay out the scratch sequence",
        requires=("recommend",),
        config_keys=("name",),
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

#: The proxy render, on its own. Opt-in via ``--render-proxy``: it is the
#: only stage that produces a file measured in hundreds of megabytes, and a
#: pipeline that quietly filled a disk would be a bad neighbour.
RENDER_STAGES = ("render_proxy",)

#: Stages the feedback collector owns. These are opt-*in* -- ``--feedback`` --
#: rather than opt-out, because they start something a person has to finish.
FEEDBACK_STAGES = ("feedback_start", "feedback_queue", "feedback_report")


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
    return (
        [str(target)],
        {
            "segments": stats["segments"],
            "usable_segments": stats["usable_segments"],
            "covered_seconds": stats["covered_seconds"],
            "by_importance": stats["by_importance"],
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


def run_roughcut_build(pipeline, run, context) -> tuple:
    plan = pipeline.rough_cut(
        timeline=context.get("timeline"),
        recommendations=context.get("recommendations"),
        name=run.name,
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

    roughcut = context.get("roughcut")
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

    result = job.result
    return (
        [job.output_path, job.notes_path],
        {
            "job_id": job.job_id,
            "video": job.output_path,
            "notes": job.notes_path,
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


RUNNERS["render_proxy"] = run_render_proxy
RUNNERS["feedback_start"] = run_feedback_start
RUNNERS["feedback_queue"] = run_feedback_queue
RUNNERS["feedback_report"] = run_feedback_report

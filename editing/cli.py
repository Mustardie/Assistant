"""Command line for the editing structure layer.

    python -m editing.cli <command> [options]

Commands, in the order a session normally uses them::

    discover    find footage and map it to the open Premiere project
    transcript  status | pull | import   -- Premiere Speech to Text, or a file
    transcribe  file | folder | status | show | export | clear-cache
                -- local faster-whisper speech to text, no cloud, no upload
    analyze     run Qwen3-VL over sampled windows
    timeline    combine events and transcripts into the structure timeline
    show        print a built timeline (table or JSON)
    export      write a built timeline somewhere else
    plan        preview sampling cost without analysing anything
    cache       info | clear
    doctor      check FFmpeg, the model server and Premiere
    roughcut    build | dry-run | execute | placements | unconverted | report
    review      export-frames | critique | plan | dry-run | execute --yes |
                report | show-issues   -- the critic pass over a rough cut
                package | summary | open-latest   -- and the review folder a
                finished run leaves behind: one index, five questions
    polish      captions | audio | show-rejected | show-missing
                -- key-moment captions and restrained sound. Plans only
    visuals     plan | report | show-accepted | show-rejected |
                export-premiere-plan | show-final
                -- the creative visual layer: which moments earn emphasis,
                which are refused, and what Premiere could do about it. Draws
                nothing and executes nothing
    style       list | show <preset>   -- the editing styles available
    layers      build | report | export | dry-run | execute --yes |
                show-deferred | show-density   -- a styled, layered edit
    assets      init | index | list | show | validate | report | match |
                plan | dry-run | execute --yes | show-missing | show-deferred
                -- a local sound/graphic library, and placing from it
    auto        run | status | list-runs | resume | report | show-gates |
                execute-stage <stage> --yes | clean | explain-failure |
                show-checks | batch | list-batches | batch-report
                -- the whole pipeline, checkpointed, with gated execution,
                over one folder or over every folder under a root
    episode     build-memory | plan-retention | report | show-beats |
                show-risks | show-hooks | show-open-loops | show-callbacks |
                export   -- the story the footage tells, and where it sags
    feedback    start | queue | show | rate | note | correct | list |
                report | export | stats   -- structured human review of an
                edit, appended to a log that is never rewritten
    render      roughcut | from-plan | show | list | report | notes | open |
                clean   -- a watchable proxy MP4 of a rough cut, made with
                FFmpeg. No Premiere, and nothing is executed
    retention   plan | report | show-cold-open | show-compression |
                show-protected | show-rejected | compare | render
                -- wires the retention findings into the cut itself: a cold
                open, compressed sag, protected setups, harder dead air
    director    build-context | plan | report | show-decisions |
                show-rejected | show-style | compare-heuristic | render |
                status | clear-cache   -- a model reads the whole episode and
                decides what the cut is; deterministic rules check every
                answer

Every command accepts ``--json`` and then prints one machine-readable object on
stdout and nothing else, so this is usable as a subprocess. Progress messages
go to stderr, which keeps them out of a piped ``--json`` result.

Errors exit non-zero with a JSON object carrying ``code`` and ``hint``, so a
caller can branch on the failure kind rather than parsing English.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from editing import align
from editing.config import AudioConfig, SamplingConfig, load_config
from editing.errors import EditingError
from editing.pipeline import Pipeline, build_pipeline
from editing.schema import StructureTimeline
from editing.recommend import report as report_module
from editing.recommend.planner import PlannerOptions
from editing.critic import report as critic_report
from editing.critic.frames import CoverageOptions
from editing.critic.revise import RevisionOptions
from editing.roughcut import review as review_module
from editing.roughcut.build import RoughCutOptions
from editing.assets import report as assets_report
from editing.conform import execute as conform_execute
from editing.conform import report as conform_report
from editing.conform.schema import COLOR_LOOKS, CONFORM_MODES
from editing.auto import gates as auto_gates
from editing.auto import report as auto_report
from editing.auto import store as auto_store
from editing.episode import report as episode_report
from editing.episode import schema as episode_schema
from editing.feedback import collect as feedback_collect
from editing.feedback import queue as feedback_queue_module
from editing.feedback import report as feedback_report
from editing.feedback import schema as feedback_schema
from editing.feedback import store as feedback_store
from editing.auto.runner import AutoRunner
from editing.auto.schema import AutoRunConfig
from editing.assets.compile import AssetOptions
from editing.assets.place import PlacementLimits
from editing.style import presets as style_presets, report as layers_report
from editing.style.compile import CompileOptions
from editing.retention import compare as retention_compare
from editing.retention import report as retention_report
from editing.retention import schema as retention_schema
from editing.retention import store as retention_store
from editing.director import compare as director_compare
from editing.director import report as director_report
from editing.director import schema as director_schema
from editing.director import store as director_store
from editing.director import style_guide as director_style_guide
from editing.render import report as render_report
from editing.render import schema as render_schema
from editing.render import store as render_store
from editing.transcribe import formats as transcribe_formats
from editing.transcribe import schema as transcribe_schema
from editing.transcribe import store as transcribe_store
from editing.transcripts import premiere_source
from editing.visual import qwen

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _emit(payload: dict) -> None:
    """The one place stdout is written for ``--json``."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def _note(message: str) -> None:
    """Human progress output. stderr so it never pollutes ``--json``."""
    print(message, file=sys.stderr)


def _reporter(args) -> object:
    return (lambda message: None) if args.quiet else _note


# ---------------------------------------------------------------------------
# Argument plumbing
# ---------------------------------------------------------------------------

def _sampling_from(args) -> SamplingConfig:
    """Sampling config from the environment, overridden by any CLI flags."""
    base = SamplingConfig.from_env()
    overrides = {
        "window_seconds": getattr(args, "window_seconds", None),
        "window_overlap": getattr(args, "window_overlap", None),
        "frames_per_window": getattr(args, "frames", None),
        "dense_frames_per_window": getattr(args, "dense_frames", None),
        "motion_threshold": getattr(args, "motion_threshold", None),
        "dense_window_seconds": getattr(args, "dense_window_seconds", None),
        "max_windows": getattr(args, "max_windows_config", None),
        "frame_width": getattr(args, "frame_width", None),
    }
    clean = {key: value for key, value in overrides.items() if value is not None}
    if clean:
        from dataclasses import replace
        base = replace(base, **clean)
    return base.validated()


def _audio_from(args) -> AudioConfig:
    """Audio config from the environment, overridden by any CLI flags."""
    base = AudioConfig.from_env()
    overrides = {
        "silence_threshold_db": getattr(args, "silence_db", None),
        "min_silence_seconds": getattr(args, "min_silence", None),
        "long_pause_seconds": getattr(args, "long_pause", None),
        "spike_delta_db": getattr(args, "spike_db", None),
        "sample_interval": getattr(args, "audio_interval", None),
    }
    clean = {key: value for key, value in overrides.items() if value is not None}
    if clean:
        from dataclasses import replace
        base = replace(base, **clean)
    return base.validated()


def _pipeline(args) -> Pipeline:
    config, sampling, audio = load_config(
        sampling=_sampling_from(args),
        audio=_audio_from(args),
        output_dir=Path(args.output_dir) if getattr(args, "output_dir", None) else None,
        vision_backend=getattr(args, "backend", None),
        vision_model=getattr(args, "model", None),
        vision_base_url=getattr(args, "base_url", None),
        use_premiere=(False if getattr(args, "no_premiere", False) else None),
    )
    return build_pipeline(
        config, sampling, audio,
        say=_reporter(args),
        use_cache=not getattr(args, "no_cache", False),
    )


def _run_scoped_pipeline(args) -> Pipeline:
    """A pipeline scoped to one auto run's artifacts, if ``--run`` was given.

    Used by ``feedback`` and ``render``, which are the two commands that read
    or write things an auto run produces. Each run is hermetic, so its review
    and its proxy live beside the plans they are about; without a run, the
    shared output directory is used, which is what the stage-by-stage commands
    write to.
    """
    pipeline = _pipeline(args)
    run_id = getattr(args, "run", "") or ""
    if not run_id:
        return pipeline

    directory = auto_store.run_dir(pipeline.config, run_id)
    if not directory.exists():
        raise EditingError(
            f"No auto run called '{run_id}'",
            hint="List them with `python -m editing.cli auto list-runs`, or "
                 "drop --run to use the shared output directory.",
            detail={"path": str(directory)},
        )
    return build_pipeline(
        auto_store.run_config(pipeline.config, run_id),
        _sampling_from(args), _audio_from(args),
        say=_reporter(args),
        use_cache=not getattr(args, "no_cache", False),
    )


def _assets_for(pipeline: Pipeline, args):
    """The assets a command should act on, honouring --folder/--file/--only."""
    assets = pipeline.ensure_assets(
        folder=getattr(args, "folder", None),
        files=getattr(args, "file", None) or None,
        recursive=not getattr(args, "no_recursive", False),
        use_premiere=(False if getattr(args, "no_premiere", False) else None),
    )
    only = getattr(args, "only", None)
    if only:
        selected = pipeline.select(assets, only)
        if not selected:
            raise EditingError(
                f"No discovered footage matches '{only}'",
                hint="Run `discover` to see what is available, or check the name.",
                detail={"available": [asset.filename for asset in assets][:20]},
            )
        return selected
    return assets


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_discover(args) -> int:
    pipeline = _pipeline(args)
    assets = pipeline.discover(
        folder=args.folder,
        files=args.file or None,
        recursive=not args.no_recursive,
        use_premiere=(False if args.no_premiere else None),
    )
    target = pipeline.write_assets()

    if args.json:
        _emit({
            "success": True,
            "count": len(assets),
            "written": str(target),
            "project": pipeline.project.to_dict() if pipeline.project else {},
            "assets": [asset.to_dict() for asset in assets],
        })
        return EXIT_OK

    if not assets:
        _note("No media files found.")
        return EXIT_OK

    print(f"{len(assets)} file(s):")
    for asset in assets:
        premiere = (
            f"Premiere: {asset.premiere.item_name or 'yes'}"
            + (f" [{asset.premiere.bin}]" if asset.premiere.bin else "")
            if asset.premiere.matched else "not in project"
        )
        print(
            f"  {asset.filename}\n"
            f"      {asset.duration:8.1f}s  {asset.resolution or '?':>10}  "
            f"{asset.fps or 0:5.2f}fps  audio={'yes' if asset.has_audio else 'no':<3}  "
            f"{premiere}\n"
            f"      id={asset.asset_id}  {asset.path}"
        )
    print(f"\nWritten to {target}")
    return EXIT_OK


def cmd_transcript(args) -> int:
    pipeline = _pipeline(args)

    if args.transcript_command == "status":
        support = premiere_source.probe_support(pipeline.bridge)
        assets = _assets_for(pipeline, args)
        resolutions = {
            asset.asset_id: pipeline.transcripts([asset], use_premiere=False)
            [asset.asset_id]
            for asset in assets
        }
        if args.json:
            _emit({
                "success": True,
                "premiere_support": support.to_dict(),
                "assets": {
                    asset.filename: resolutions[asset.asset_id].to_dict()
                    for asset in assets
                },
            })
            return EXIT_OK

        print("Premiere Speech to Text support:")
        print(f"  reachable : {support.available}")
        print(f"  readable  : {support.readable}")
        if support.premiere_version:
            print(f"  version   : {support.premiere_version}")
        if support.note:
            print(f"  note      : {support.note}")
        print(f"  manual    : {support.manual_export}")
        print("\nPer file:")
        for asset in assets:
            resolution = resolutions[asset.asset_id]
            state = (
                f"{len(resolution.transcript)} line(s) from {resolution.origin}"
                if resolution.found else "none"
            )
            print(f"  {asset.filename}: {state}")
            if not resolution.found and resolution.note:
                print(f"      {resolution.note}")
        return EXIT_OK

    if args.transcript_command == "pull":
        assets = _assets_for(pipeline, args)
        resolutions = pipeline.transcripts(
            assets, use_premiere=True, refresh=args.refresh
        )
        found = sum(1 for r in resolutions.values() if r.found)
        if args.json:
            _emit({
                "success": True,
                "found": found,
                "total": len(assets),
                "results": {
                    asset.filename: resolutions[asset.asset_id].to_dict()
                    for asset in assets
                },
            })
            return EXIT_OK
        print(f"{found}/{len(assets)} file(s) have a transcript.")
        for asset in assets:
            resolution = resolutions[asset.asset_id]
            if not resolution.found:
                print(f"  {asset.filename}: {resolution.note}")
        return EXIT_OK

    if args.transcript_command == "import":
        assets = _assets_for(pipeline, args)
        target = pipeline.select(assets, args.for_) if args.for_ else assets
        if len(target) != 1:
            raise EditingError(
                "An import needs exactly one target file",
                hint="Pass --for with the clip's name, e.g. --for session_01.mp4",
                detail={"matched": [asset.filename for asset in target][:20]},
            )
        transcript = pipeline.import_transcript(target[0], args.file_path)
        if args.json:
            _emit({
                "success": True,
                "asset": target[0].filename,
                "asset_id": target[0].asset_id,
                "source": transcript.source,
                "entries": len(transcript),
                "duration": round(transcript.duration, 2),
            })
        return EXIT_OK

    raise EditingError("Unknown transcript command")


def cmd_analyze(args) -> int:
    pipeline = _pipeline(args)
    assets = _assets_for(pipeline, args)

    results = pipeline.analyze(
        assets,
        keep_frames=args.keep_frames,
        use_motion=not args.no_motion,
        max_windows=args.max_windows,
        show_progress=not args.quiet,
    )

    if args.json:
        _emit({
            "success": True,
            "assets": {
                asset.filename: results[asset.asset_id].to_dict()
                for asset in assets
            },
            "cache": pipeline.cache.stats.to_dict() if pipeline.cache else {},
        })
        return EXIT_OK

    total_events = sum(len(result.events) for result in results.values())
    failures = sum(result.failures for result in results.values())
    print(f"{total_events} visual event(s) across {len(assets)} file(s).")
    if failures:
        print(f"{failures} window(s) could not be analysed; see the events files.")
    print(f"Events written to {pipeline.config.visual_dir}")
    return EXIT_OK


def cmd_audio(args) -> int:
    pipeline = _pipeline(args)
    assets = _assets_for(pipeline, args)
    results = pipeline.analyze_audio(assets, refresh=args.refresh)

    if args.json:
        _emit({
            "success": True,
            "assets": {
                asset.filename: results[asset.asset_id].to_dict()
                for asset in assets
            },
        })
        return EXIT_OK

    total = sum(len(result.events) for result in results.values())
    print(f"{total} audio event(s) across {len(assets)} file(s).")
    for asset in assets:
        result = results[asset.asset_id]
        summary = result.summary()
        if summary["by_type"]:
            print(f"  {asset.filename}: " + ", ".join(
                f"{name}={count}"
                for name, count in sorted(summary["by_type"].items())
            ))
    print(f"Written to {pipeline.config.audio_dir}")
    return EXIT_OK


def cmd_attach(args) -> int:
    """Fold audio events into the timeline without re-analysing anything.

    ``timeline`` already attaches whatever audio is on disk, so this is the
    same operation named for the step it performs -- useful after running
    ``audio`` on a timeline that was built before the audio existed.
    """
    pipeline = _pipeline(args)
    assets = _assets_for(pipeline, args)

    missing = [
        asset.filename for asset in assets if not pipeline.load_audio_events(asset)
    ]
    timeline = pipeline.timeline(assets, use_premiere=False)
    target = pipeline.write_timeline(timeline, name=args.name)

    attached = sum(len(segment.audio_events) for segment in timeline.segments)
    covered = sum(1 for segment in timeline.segments if segment.audio_events)

    if args.json:
        _emit({
            "success": True,
            "written": str(target),
            "segments": len(timeline.segments),
            "segments_with_audio": covered,
            "audio_event_links": attached,
            "files_without_audio_analysis": missing,
        })
        return EXIT_OK

    print(
        f"{attached} audio event link(s) across {covered}/{len(timeline.segments)} "
        f"segment(s)."
    )
    for name in missing:
        print(f"  ! {name} has no audio analysis yet -- run `audio` for it.")
    print(f"Written to {target}")
    return EXIT_OK


def cmd_recommend(args) -> int:
    pipeline = _pipeline(args)
    timeline = pipeline.load_timeline(name=args.name)
    recommendations = pipeline.recommend(
        timeline,
        options=PlannerOptions(
            budget_seconds=args.budget_seconds,
            min_repeat_gap=args.repeat_gap,
            skip_safety=args.no_safety,
        ),
        name=args.name,
    )
    draft = None
    if args.with_plan:
        draft = pipeline.draft_plan(recommendations, name=args.name)
    report_path = pipeline.write_report(
        recommendations, timeline=timeline, draft=draft, name=args.name
    )

    if args.json:
        _emit({"success": True, "report": str(report_path),
               **recommendations.to_dict()})
        return EXIT_OK

    print(report_module.render(
        recommendations, timeline=timeline, draft=draft, limit=args.limit
    ))
    print(f"\nWritten to {pipeline.config.recommendations_dir}")
    return EXIT_OK


def cmd_top(args) -> int:
    pipeline = _pipeline(args)
    timeline = pipeline.load_timeline(name=args.name)
    if args.json:
        _emit({
            "success": True,
            "moments": [
                segment.to_dict()
                for segment in timeline.highlights(args.limit)
            ],
        })
        return EXIT_OK
    print(report_module.render_top_moments(timeline, limit=args.limit))
    return EXIT_OK


def cmd_reactions(args) -> int:
    """Moments the audio layer made interesting on its own."""
    pipeline = _pipeline(args)
    timeline = pipeline.load_timeline(name=args.name)
    reacting = [
        segment for segment in timeline.segments
        if segment.audio_reaction is not None
    ]
    reacting.sort(key=lambda s: s.audio_reaction.confidence, reverse=True)

    if args.json:
        _emit({
            "success": True,
            "count": len(reacting),
            "moments": [
                {
                    "start": round(segment.start, 3),
                    "end": round(segment.end, 3),
                    "reaction": segment.audio_reaction.to_dict(),
                    "importance": segment.importance,
                    "said": segment.said,
                    "usefulness": round(segment.usefulness, 3),
                }
                for segment in reacting[: args.limit]
            ],
        })
        return EXIT_OK

    if not reacting:
        print("No audio reaction moments. Run `audio` if you have not yet.")
        return EXIT_OK
    print(f"{len(reacting)} audio reaction moment(s):")
    for segment in reacting[: args.limit]:
        reaction = segment.audio_reaction
        print(
            f"  [{segment.start:8.2f}-{segment.end:8.2f}] {reaction.type:<18}"
            f" {reaction.detection:<18} conf={reaction.confidence:.2f}"
        )
        print(f"      picture: {segment.importance}; "
              + (f'said "{segment.said[:50]}"' if segment.has_speech else "silent"))
    return EXIT_OK


def cmd_removed(args) -> int:
    """What the safety pass took out, and why."""
    pipeline = _pipeline(args)
    recommendations = pipeline.load_recommendations(name=args.name)
    removed = recommendations.removed()

    if args.json:
        _emit({
            "success": True,
            "count": len(removed),
            "removed": [entry.to_dict() for entry in removed],
        })
        return EXIT_OK

    if not removed:
        print("The safety pass removed nothing.")
        return EXIT_OK
    print(f"{len(removed)} recommendation(s) removed or softened:")
    for entry in removed[: args.limit]:
        print(f"  [{entry.status:<10}] {entry.start:8.2f}s {entry.category:<16} "
              f"{entry.reason[:44]}")
        print(f"      why: {entry.status_reason}")
    return EXIT_OK


def cmd_draft(args) -> int:
    """Build the draft Premiere plan and dry-run it. Executes nothing."""
    pipeline = _pipeline(args)
    recommendations = pipeline.load_recommendations(name=args.name)
    draft = pipeline.draft_plan(recommendations, name=args.name)

    if args.json:
        _emit({"success": True, **draft.to_dict()})
        return EXIT_OK

    print(f"Draft Premiere plan ({args.name})")
    print(f"  operations     : {draft.operation_count}")
    print(f"  dry run        : {'valid' if draft.valid else 'INVALID'}")
    print(f"  executed       : {draft.executed}  <- nothing has been applied")
    print(f"  kept as recs   : {len(draft.not_convertible)}")
    if draft.validation_error:
        print(f"  error          : {draft.validation_error.get('error')}")
        if draft.validation_error.get("hint"):
            print(f"  hint           : {draft.validation_error['hint']}")
    for line in draft.explanation[: args.limit]:
        print(f"    {line}")
    if draft.not_convertible:
        print("\n  Kept as recommendations (no operation yet):")
        seen: dict = {}
        for entry in draft.not_convertible:
            seen.setdefault(entry["category"], entry["reason"])
        for category, reason in sorted(seen.items()):
            print(f"    {category:<18} {reason}")
    print(f"\nWritten to {pipeline.config.plans_dir / (args.name + '.json')}")
    return EXIT_OK


def _roughcut_options(args) -> RoughCutOptions:
    """Selection settings from whichever command asked for them.

    Read with ``getattr`` and defaults rather than by attribute: this is
    called from ``roughcut``, which declares every flag, and from ``director``,
    which declares the handful that make sense there. Requiring all of them
    would mean either duplicating the whole flag set onto a second command
    group or crashing on the ones it left out.
    """
    defaults = RoughCutOptions()

    def value(name: str, fallback):
        found = getattr(args, name, None)
        return fallback if found is None else found

    return RoughCutOptions(
        sequence_name=value("sequence", defaults.sequence_name),
        keep_threshold=value("keep_threshold", defaults.keep_threshold),
        filler_speed=value("filler_speed", defaults.filler_speed),
        handle=value("handle", defaults.handle),
        drop_filler=bool(getattr(args, "drop_filler", False)),
        allow_zooms=not getattr(args, "no_zooms", False),
        preset=value("preset", "") or "",
        mode=value("mode", defaults.mode),
    )


def cmd_conform(args) -> int:
    """Build, validate, execute and deliver the finished edit.

    The one command group where "it worked" means a file exists. Every other
    pass in this CLI ends at a plan; this one is judged on frames.
    """
    pipeline = _pipeline(args)
    command = args.conform_command

    # -- build ----------------------------------------------------------
    if command == "build":
        style = style_presets.get(args.style) if args.style else style_presets.get()
        config = pipeline.conform_config(
            style=style,
            mode=args.mode,
            color_look=args.color or "",
            color_strength=args.color_strength,
            music_library=args.music_library or "",
            target_lufs=args.target_lufs,
            max_transitions=args.max_transitions,
            captions=not args.no_captions,
            sound=not args.no_sound,
            music=not args.no_music,
            visuals=not args.no_visuals,
            color=not args.no_color,
            transitions=not args.no_transitions,
        )
        plan = pipeline.conform(name=args.name, config=config, style=style)
        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK
        print(conform_report.render(plan, limit=args.limit))
        return EXIT_OK

    # -- dry-run --------------------------------------------------------
    if command == "dry-run":
        plan = pipeline.load_conform(name=args.name)
        report = pipeline.run_conform(plan, mode="dry_run", name=args.name)
        if args.json:
            _emit({"success": True, "report": report.to_dict(),
                   "dry_run_passed": plan.dry_run_passed,
                   "explanation": plan.explanation})
            return EXIT_OK
        print(f"Dry run: {'PASSED' if plan.dry_run_passed else 'FAILED'}")
        print(f"  operations : {plan.operation_count}")
        print(f"  executed   : {report.executed}  <- nothing has been applied")
        if plan.dry_run_error:
            print(f"  error      : {plan.dry_run_error.get('error')}")
            if plan.dry_run_error.get("hint"):
                print(f"  hint       : {plan.dry_run_error['hint']}")
        for line in plan.explanation[: args.limit]:
            print(f"    {line}")
        return EXIT_OK

    # -- execute --------------------------------------------------------
    if command == "execute":
        plan = pipeline.load_conform(name=args.name)
        if not args.yes:
            raise EditingError(
                "Execution needs an explicit confirmation",
                hint="This writes to Premiere. Re-run with --yes once you "
                     "have read the dry run.",
            )
        report = pipeline.run_conform(plan, mode="execute", name=args.name)
        if args.json:
            _emit({"success": report.executed,
                   "by_layer": conform_execute.executed_by_layer(report, plan),
                   **report.to_dict()})
            return EXIT_OK if report.executed or report.refused_reason \
                else EXIT_ERROR

        print(f"Conform execution ({plan.sequence_name})")
        print(f"  dry run    : {'passed' if report.dry_run_passed else 'FAILED'}")
        print(f"  on scratch : {report.on_scratch}")
        print(f"  executed   : {report.executed}")
        print(f"  operations : {report.operations_succeeded}/"
              f"{report.operations_attempted}")
        for name, counts in conform_execute.executed_by_layer(
            report, plan
        ).items():
            failed = f", {counts['failed']} failed" if counts["failed"] else ""
            print(f"    {name:<22} {counts['ok']} applied{failed}")
        if report.refused_reason:
            print(f"  refused    : {report.refused_reason}")
        if report.error:
            print(f"  error      : {report.error.get('error')}")
            for failure in (report.error.get("detail") or {}).get("failed", [])[:10]:
                print(f"    - {failure.get('op')} #{failure.get('index')}: "
                      f"{str(failure.get('error'))[:70]}")
        return EXIT_OK if report.executed or report.refused_reason else EXIT_ERROR

    # -- report ---------------------------------------------------------
    if command == "report":
        plan = pipeline.load_conform(name=args.name)
        delivery = pipeline.delivery_or_none(name=args.name)
        if args.json:
            _emit({"success": True, "plan": plan.to_dict(),
                   "delivery": delivery.to_dict() if delivery else None})
            return EXIT_OK
        print(conform_report.render(plan, delivery=delivery, limit=args.limit))
        return EXIT_OK

    # -- operations -----------------------------------------------------
    if command == "operations":
        plan = pipeline.load_conform(name=args.name)
        if args.json:
            _emit({"success": True, "ops": plan.ops})
            return EXIT_OK
        print(f"{plan.operation_count} operation(s) for '{plan.sequence_name}':")
        for index, op in enumerate(plan.ops[: args.limit]):
            note = str(op.get("note", ""))[:52]
            where = op.get("time", op.get("track", ""))
            print(f"  {index:>4}  {op.get('op', '?'):<20} {str(where):<10} {note}")
        if plan.operation_count > args.limit:
            print(f"  ... {plan.operation_count - args.limit} more")
        return EXIT_OK

    # -- verify ---------------------------------------------------------
    if command == "verify":
        plan = pipeline.load_conform(name=args.name)
        result = pipeline.verify_conform(
            name=args.name, plan=plan, limit=args.limit,
            critique=args.critique,
        )
        if args.json:
            _emit({"success": result.usable, **result.to_dict()})
            return EXIT_OK if result.usable else EXIT_ERROR
        print(f"Verification of '{result.sequence_name}'")
        print(f"  source     : {result.source or '-'}")
        print(f"  frames     : {len(result.exported)}/{len(result.frames)}")
        if result.note:
            print(f"  note       : {result.note}")
        for frame in result.frames:
            mark = "+" if frame.exported else "-"
            print(f"  {mark} {frame.at:7.2f}  {frame.path}")
            for claim in frame.expects[:3]:
                print(f"        expects: {claim}")
            if frame.error:
                print(f"        ! {frame.error}")
        return EXIT_OK if result.usable else EXIT_ERROR

    # -- unconverted ----------------------------------------------------
    if command == "unconverted":
        plan = pipeline.load_conform(name=args.name)
        if args.json:
            _emit({"success": True, "unconverted": plan.unconverted})
            return EXIT_OK
        print(f"{len(plan.unconverted)} decision(s) did not become an operation:")
        for entry in plan.unconverted[: args.limit]:
            print(f"  {entry.get('at', 0.0):7.2f}  {entry.get('kind', '?'):<10} "
                  f"{entry.get('reason', ''):<22} {entry.get('detail', '')}")
        return EXIT_OK

    raise EditingError(f"Unknown conform command '{command}'")


def cmd_deliver(args) -> int:
    """Render the finished sequence to a file."""
    pipeline = _pipeline(args)
    result = pipeline.deliver(
        name=args.name,
        sequence=args.sequence or "",
        output=args.output or "",
        preset=args.preset or "",
        wait=args.wait,
    )
    if args.json:
        _emit({"success": result.delivered, **result.to_dict()})
        return EXIT_OK if result.delivered else EXIT_ERROR

    print(f"Delivery of '{result.sequence_name}'")
    print(f"  method     : {result.method or '-'}")
    print(f"  path       : {result.output_path or result.requested_path}")
    print(f"  exists     : {result.exists}")
    if result.exists:
        print(f"  size       : {result.size_bytes / 1_000_000:.1f} MB")
        print(f"  duration   : {result.duration:.1f}s")
    print(f"  waited     : {result.waited:.1f}s")
    if result.error:
        print(f"  error      : {result.error.get('error')}")
        if result.error.get("hint"):
            print(f"  hint       : {result.error['hint']}")
    for warning in result.warnings:
        print(f"  ! {warning}")
    return EXIT_OK if result.delivered else EXIT_ERROR


def cmd_roughcut(args) -> int:
    pipeline = _pipeline(args)
    command = args.roughcut_command

    # -- build ----------------------------------------------------------
    if command == "build":
        plan = pipeline.rough_cut(
            options=_roughcut_options(args),
            name=args.name,
            validate=not args.plan_only,
        )
        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK
        _print_roughcut(plan, limit=args.limit)
        if args.plan_only:
            print("\nplan-only: NOT validated. Run `roughcut dry-run` next.")
        return EXIT_OK

    # -- dry-run --------------------------------------------------------
    if command == "dry-run":
        plan = pipeline.load_rough_cut(name=args.name)
        report = pipeline.run_rough_cut(plan, mode="dry_run", name=args.name)
        if args.json:
            _emit({"success": True, "report": report.to_dict(),
                   "dry_run_passed": plan.dry_run_passed,
                   "explanation": plan.explanation})
            return EXIT_OK
        print(f"Dry run: {'PASSED' if plan.dry_run_passed else 'FAILED'}")
        print(f"  operations : {plan.operation_count}")
        print(f"  executed   : {report.executed}  <- nothing has been applied")
        if plan.dry_run_error:
            print(f"  error      : {plan.dry_run_error.get('error')}")
            if plan.dry_run_error.get("hint"):
                print(f"  hint       : {plan.dry_run_error['hint']}")
        for line in plan.explanation[: args.limit]:
            print(f"    {line}")
        return EXIT_OK

    # -- execute --------------------------------------------------------
    if command == "execute":
        plan = pipeline.load_rough_cut(name=args.name)
        if not args.yes:
            raise EditingError(
                "Execution needs an explicit confirmation",
                hint="This writes to Premiere. Re-run with --yes once you have "
                     "read the dry run.",
            )
        report = pipeline.run_rough_cut(
            plan,
            mode="execute_on_scratch",
            allow_active_sequence=args.allow_active_sequence,
            name=args.name,
        )
        if args.json:
            _emit({"success": report.ok, **report.to_dict()})
            return EXIT_OK if report.ok or report.refused_reason else EXIT_ERROR

        print(f"Rough cut execution ({plan.sequence_name})")
        print(f"  dry run    : {'passed' if report.dry_run_passed else 'FAILED'}")
        print(f"  on scratch : {report.on_scratch}")
        print(f"  executed   : {report.executed}")
        print(f"  operations : {report.operations_succeeded}/"
              f"{report.operations_attempted}")
        if report.refused_reason:
            print(f"  refused    : {report.refused_reason}")
        if report.error:
            print(f"  error      : {report.error.get('error')}")
        return EXIT_OK if report.executed or report.refused_reason else EXIT_ERROR

    # -- placements -----------------------------------------------------
    if command == "placements":
        plan = pipeline.load_rough_cut(name=args.name)
        if args.json:
            _emit({"success": True, "sequence": plan.sequence_name,
                   "placements": [p.to_dict() for p in plan.placements]})
            return EXIT_OK
        print(f"{len(plan.placements)} clip(s) in '{plan.sequence_name}':")
        for placement in plan.placements:
            flags = []
            if placement.protected:
                flags.append("protected")
            if placement.speed != 1.0:
                flags.append(f"{placement.speed:g}x")
            print(
                f"  [{placement.sequence_start:8.2f}-{placement.sequence_end:8.2f}] "
                f"<- {placement.source_in:8.2f}-{placement.source_out:8.2f}  "
                f"{placement.keep_reason:<14} {' '.join(flags)}"
            )
            print(f"      {placement.placement_id}  "
                  f"recs={len(placement.recommendation_ids)} "
                  f"segments={len(placement.segment_ids)}")
        return EXIT_OK

    # -- unconverted ----------------------------------------------------
    if command == "unconverted":
        plan = pipeline.load_rough_cut(name=args.name)
        if args.json:
            _emit({"success": True, "count": len(plan.unconverted),
                   "unconverted": [u.to_dict() for u in plan.unconverted]})
            return EXIT_OK
        if not plan.unconverted:
            print("Everything accepted was converted into the cut.")
            return EXIT_OK
        print(f"{len(plan.unconverted)} recommendation(s) not in the cut:")
        for entry in plan.unconverted[: args.limit]:
            print(f"  {entry.start:8.2f}s {entry.category:<16} "
                  f"{entry.recommendation_id}")
            print(f"      {entry.reason}")
        return EXIT_OK

    # -- report ---------------------------------------------------------
    if command == "report":
        report = pipeline.load_execution_report(name=args.name)
        if args.json:
            _emit({"success": True, **report.to_dict()})
            return EXIT_OK
        print(f"Execution report ({args.name})")
        for label, value in (
            ("mode", report.mode), ("executed", report.executed),
            ("on scratch", report.on_scratch),
            ("dry run passed", report.dry_run_passed),
            ("sequence", report.sequence_name),
            ("operations", f"{report.operations_succeeded}/"
                           f"{report.operations_attempted}"),
            ("elapsed", f"{report.elapsed:.2f}s"),
        ):
            print(f"  {label:<15}: {value}")
        if report.refused_reason:
            print(f"  refused        : {report.refused_reason}")
        if report.error:
            print(f"  error          : {report.error.get('error')}")
        return EXIT_OK

    raise EditingError("Unknown roughcut command")


def _coverage_options(args) -> Optional[CoverageOptions]:
    """Coverage options from the flags, or None for "simple, one per clip"."""
    if getattr(args, "simple", False):
        return None
    return CoverageOptions(
        cut_points=not getattr(args, "no_cut_points", False),
        markers=not getattr(args, "no_markers", False),
        zooms=not getattr(args, "no_zooms", False),
        speed_changes=not getattr(args, "no_speed", False),
        text_placeholders=not getattr(args, "no_text", False),
        high_priority=not getattr(args, "no_priority", False),
        sanity=not getattr(args, "no_sanity", False),
        max_frames=getattr(args, "max_frames", None) or 120,
    )


def _revision_options(args) -> RevisionOptions:
    return RevisionOptions(
        allow_timing=not getattr(args, "no_timing", False),
        allow_zoom_edits=not getattr(args, "no_zoom_edits", False),
        min_confidence=getattr(args, "min_confidence", None) or 0.60,
    )


def cmd_review(args) -> int:
    """The critic pass, and the review package a finished run produces."""
    command = getattr(args, "review_command", None) or "export-frames"

    # The three package commands are about a *run* rather than about the
    # critic, so they are handled before the pipeline is built: they read a
    # run folder and never need a model, a bridge or a cache.
    if command in ("package", "summary", "open-latest"):
        return _review_package_command(args, command)

    pipeline = _pipeline(args)

    # -- export-frames ---------------------------------------------------
    if command == "export-frames":
        return _review_export(args, pipeline)

    # -- critique --------------------------------------------------------
    if command == "critique":
        review = pipeline.load_review(name=args.name)
        report = pipeline.critique(
            review, name=args.name, limit=args.max_frames or 0
        )
        if args.json:
            _emit({"success": True, **report.to_dict()})
            return EXIT_OK
        stats = report.stats()
        print(f"Critique of '{report.sequence_name}' with {report.model}")
        if report.mock:
            print("  *** MOCK CRITIC -- metadata only, no picture examined ***")
        print(f"  frames examined : {stats['frames_examined']}")
        print(f"  clean           : {stats['frames_clean']}")
        print(f"  failed          : {stats['frames_failed']}")
        print(f"  findings        : {stats['findings']}")
        for issue, count in sorted(
            stats["by_issue"].items(), key=lambda kv: -kv[1]
        ):
            print(f"    {issue:<26} {count}")
        for warning in report.warnings[: args.limit]:
            print(f"  ! {warning}")
        print(f"\nWritten to "
              f"{pipeline.config.critic_dir / (args.name + '.critique.json')}")
        return EXIT_OK

    # -- plan -------------------------------------------------------------
    if command == "plan":
        revisions, plan = pipeline.revise(
            name=args.name,
            options=_revision_options(args),
            plan_options={"mark_deferred": not args.no_review_markers},
        )
        if args.json:
            _emit({"success": True, "revisions": revisions.to_dict(),
                   "plan": plan.to_dict()})
            return EXIT_OK
        _print_revision_plan(revisions, plan, limit=args.limit)
        print(f"\nWritten to {pipeline.config.critic_dir}")
        return EXIT_OK

    # -- dry-run ----------------------------------------------------------
    if command == "dry-run":
        plan = pipeline.load_revision_plan(name=args.name)
        report = pipeline.run_revisions(plan, mode="dry_run", name=args.name)
        if args.json:
            _emit({"success": True, "report": report.to_dict(),
                   "dry_run_passed": plan.dry_run_passed,
                   "explanation": plan.explanation})
            return EXIT_OK
        print(f"Revision dry run: "
              f"{'PASSED' if plan.dry_run_passed else 'FAILED'}")
        print(f"  operations : {plan.operation_count}")
        print(f"  executed   : {report.executed}  <- nothing has been applied")
        if plan.dry_run_error:
            print(f"  error      : {plan.dry_run_error.get('error')}")
            if plan.dry_run_error.get("hint"):
                print(f"  hint       : {plan.dry_run_error['hint']}")
        for line in plan.explanation[: args.limit]:
            print(f"    {line}")
        for warning in plan.warnings:
            print(f"  ! {warning}")
        return EXIT_OK

    # -- execute ----------------------------------------------------------
    if command == "execute":
        plan = pipeline.load_revision_plan(name=args.name)
        if not args.yes:
            raise EditingError(
                "Applying revisions needs an explicit confirmation",
                hint="This writes to the rough cut sequence in Premiere. "
                     "Re-run with --yes once you have read the dry run and "
                     "the revision report.",
            )
        report = pipeline.run_revisions(
            plan,
            mode="execute",
            name=args.name,
            allow_active_sequence=args.allow_active_sequence,
        )
        if args.json:
            _emit({"success": report.ok, **report.to_dict()})
            return EXIT_OK if report.ok or report.refused_reason else EXIT_ERROR

        print(f"Revision execution ({plan.sequence_name})")
        print(f"  dry run    : {'passed' if report.dry_run_passed else 'FAILED'}")
        print(f"  on scratch : {report.on_scratch}")
        print(f"  executed   : {report.executed}")
        print(f"  operations : {report.operations_succeeded}/"
              f"{report.operations_attempted}")
        if report.refused_reason:
            print(f"  refused    : {report.refused_reason}")
        if report.error:
            print(f"  error      : {report.error.get('error')}")
        return EXIT_OK if report.executed or report.refused_reason else EXIT_ERROR

    # -- report -----------------------------------------------------------
    if command == "report":
        revisions = pipeline.load_revisions(name=args.name)
        critique = None
        plan = None
        try:
            critique = pipeline.load_critique(name=args.name)
        except EditingError:
            pass
        try:
            plan = pipeline.load_revision_plan(name=args.name)
        except EditingError:
            pass
        if args.json:
            _emit({
                "success": True,
                "revisions": revisions.to_dict(),
                "critique": critique.to_dict() if critique else None,
                "plan": plan.to_dict() if plan else None,
            })
            return EXIT_OK
        print(critic_report.render(
            revisions, critique=critique, plan=plan, limit=args.limit
        ))
        return EXIT_OK

    # -- show-issues -------------------------------------------------------
    if command == "show-issues":
        revisions = pipeline.load_revisions(name=args.name)
        if args.json:
            entries = revisions.ranked()
            if args.severity:
                from editing.critic.schema import SEVERITY_ORDER
                floor = SEVERITY_ORDER.get(args.severity, 0)
                entries = [
                    entry for entry in entries
                    if SEVERITY_ORDER.get(entry.severity, 0) >= floor
                ]
            _emit({"success": True, "count": len(entries),
                   "mock": revisions.mock,
                   "issues": [entry.to_dict() for entry in entries]})
            return EXIT_OK
        print(critic_report.render_issues(
            revisions, limit=args.limit, severity=args.severity or ""
        ))
        return EXIT_OK

    raise EditingError("Unknown review command")


def _review_export(args, pipeline) -> int:
    """``review export-frames``: choose the moments, then extract them."""
    plan = pipeline.load_rough_cut(name=args.name)
    coverage = not getattr(args, "simple", False)

    if args.list:
        if coverage:
            frames = pipeline_coverage_preview(pipeline, plan, args)
        else:
            frames = review_module.plan_frames(plan, position=args.position)
        if args.json:
            _emit({"success": True, "exported": False,
                   "count": len(frames),
                   "frames": [f.to_dict() for f in frames]})
            return EXIT_OK
        print(f"{len(frames)} frame(s) would be exported:")
        for frame in frames[: args.limit]:
            print(f"  seq {frame.sequence_time:8.2f}s <- src "
                  f"{frame.source_time:8.2f}s  {frame.frame_kind:<16} "
                  f"{frame.reason[:44]}")
        return EXIT_OK

    review = pipeline.review_frames(
        plan, name=args.name, position=args.position, width=args.width,
        coverage=coverage, coverage_options=_coverage_options(args),
    )
    if args.json:
        _emit({"success": True, **review.to_dict()})
        return EXIT_OK

    print(f"{len(review)} review frame(s) for '{plan.sequence_name}'.")
    for kind, count in sorted(review.stats()["by_frame_kind"].items()):
        print(f"    {kind:<18} {count}")
    for frame in review.frames[: args.limit]:
        print(f"  seq {frame.sequence_time:8.2f}s  {frame.frame_kind:<16} "
              f"{Path(frame.path).name}")
        if frame.marker_names:
            print(f"      markers: {', '.join(frame.marker_names)}")
        if frame.applied_edits:
            print("      edits  : " + ", ".join(
                str(edit.get("kind")) for edit in frame.applied_edits
            ))
    for warning in review.warnings:
        print(f"  ! {warning}")
    print(f"\nWritten to {pipeline.config.review_dir}")
    return EXIT_OK


def pipeline_coverage_preview(pipeline, plan, args):
    """The coverage frame list, without extracting anything."""
    from editing.critic import frames as critic_frames

    return critic_frames.plan_coverage_frames(
        plan,
        timeline=pipeline._timeline_or_none(args.name),
        recommendations=pipeline._recommendations_or_none(args.name),
        options=_coverage_options(args),
    )


def _print_revision_plan(revisions, plan, *, limit: int = 30) -> None:
    stats = revisions.stats()
    print(f"Revisions for '{revisions.sequence_name}'")
    if revisions.mock:
        print("  *** MOCK CRITIC -- metadata only, no picture examined ***")
    print(f"  findings turned into revisions : {stats['total']}")
    print(f"  accepted (will be applied)     : {stats['accepted']}")
    print(f"  kept for a human               : "
          f"{stats['needs_human_review']}")
    print(f"  rejected                       : {stats['rejected']}")
    print(f"  operations                     : {plan.operation_count}")
    print(f"  dry run                        : "
          f"{'passed' if plan.dry_run_passed else 'FAILED'}")
    print(f"  executed                       : {plan.executed}"
          f"  <- nothing has been applied")

    accepted = revisions.accepted()
    if accepted:
        print("\n  Would be applied:")
        for revision in accepted[:limit]:
            print(f"    [{revision.start:8.2f}s] {revision.issue:<24} "
                  f"-> {revision.suggested_fix}")
            if revision.fix_detail:
                print(f"        {revision.fix_detail[:100]}")

    deferred = revisions.needing_human()
    if deferred:
        print("\n  Kept as recommendations (NOT fixed automatically):")
        for revision in deferred[:limit]:
            print(f"    [{revision.start:8.2f}s] {revision.issue:<24} "
                  f"{revision.severity:<6} {revision.confidence:.0%}")
            print(f"        {revision.status_reason[:110]}")

    if plan.dry_run_error:
        print(f"\n  ! {plan.dry_run_error.get('error')}")
    for warning in revisions.warnings + plan.warnings:
        print(f"  ! {warning}")


def _review_package_command(args, command: str) -> int:
    """``review package`` / ``summary`` / ``open-latest``.

    All three read a finished run and none of them needs the analysis
    pipeline, so they share one path and build only what they use.
    """
    from editing.reliability import run as reliability_run
    from editing.review import build as review_build
    from editing.review import index as review_index
    from editing.review import store as review_store

    runner = _auto_runner(args)
    config = runner.config

    # -- package: rebuild it from what is on disk now ---------------------
    if command == "package":
        state = runner.resolve(args.run)
        checks = None
        if not args.no_checks:
            checks, _inputs = reliability_run.check_run(config, state)
        package, written = review_build.write_package(
            config, state, checks=checks)

        if args.json:
            _emit({"success": True, "written": [str(p) for p in written],
                   **package.to_dict()})
            return EXIT_OK
        print(review_index.render_summary(package))
        print()
        print(f"  Index written to {review_store.index_path(config, state.run_id)}")
        return EXIT_OK

    # -- summary and open-latest ------------------------------------------
    run_id = args.run or ""
    if not run_id:
        run_id = review_store.latest_with_package(config) or ""
    if not run_id:
        # No package anywhere. Fall back to the most recent run and say what
        # to type, rather than reporting an absence as an error.
        latest = runner.latest_run_id()
        if not latest:
            raise EditingError(
                "No automated runs exist yet",
                hint="Start one with `auto run --folder <folder> "
                     "--style <preset>`.",
            )
        raise EditingError(
            f"No run has a review package yet (most recent run: {latest})",
            hint=f"Build one with `review package --run {latest}`.",
            detail={"latest_run": latest},
        )

    package = review_store.package_or_none(config, run_id)
    if package is None:
        raise EditingError(
            f"Run '{run_id}' has no review package",
            hint=f"Build one with `review package --run {run_id}`.",
        )

    index = review_store.index_path(config, run_id)
    if command == "summary":
        if args.json:
            _emit({"success": True, **package.to_dict()})
            return EXIT_OK
        print(review_index.render_summary(package))
        return EXIT_OK

    # -- open-latest -------------------------------------------------------
    opened = False
    if not args.print_only:
        opened = _open_on_desktop(index)
    if args.json:
        _emit({"success": True, "run_id": run_id, "index": str(index),
               "opened": opened, "folder": package.folder})
        return EXIT_OK

    print(f"Review index for {run_id}")
    print(f"  {index}")
    if opened:
        print("  (opened)")
        return EXIT_OK
    if index.exists():
        print()
        print(index.read_text(encoding="utf-8"))
    return EXIT_OK


def _open_on_desktop(path) -> bool:
    """Hand a file to the desktop. False when that is not possible here.

    Never raises and never blocks: a headless machine, an SSH session and a
    locked-down desktop all return False, and the caller prints the file
    instead. Failing to open a Markdown file is not an error worth an exit
    code.
    """
    from pathlib import Path as _Path

    target = _Path(path)
    if not target.exists():
        return False
    try:
        import os
        import subprocess
        import sys

        if sys.platform.startswith("win"):
            os.startfile(str(target))  # noqa: S606 - a local file this wrote
            return True
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(  # noqa: S603 - fixed argv, path from this system
            [opener, str(target)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001 - not opening a file is not a failure
        return False


def cmd_style(args) -> int:
    """Inspect the editing styles. Reads nothing and writes nothing."""
    command = args.style_command

    if command == "list":
        presets = [style_presets.get(name) for name in style_presets.names()]
        if args.json:
            _emit({"success": True, "default": style_presets.DEFAULT_PRESET,
                   "presets": [preset.to_dict() for preset in presets]})
            return EXIT_OK
        print(f"{len(presets)} style preset(s) "
              f"(default: {style_presets.DEFAULT_PRESET}):\n")
        for preset in presets:
            print("  " + preset.summary())
        print("\n  Every number in a preset is a ceiling, never a target: a "
              "style can only\n  make the edit quieter than the evidence "
              "justifies, never busier.")
        return EXIT_OK

    if command == "show":
        preset = style_presets.get(args.preset)
        if args.json:
            _emit({"success": True, **preset.to_dict()})
            return EXIT_OK
        print(f"{preset.name} -- {preset.label}")
        print(f"\n{preset.description}\n")
        for heading, rows in (
            ("pacing", (
                ("pacing", preset.pacing),
                ("max edits / minute", preset.max_edits_per_minute),
                ("min edit spacing", f"{preset.min_edit_spacing:g}s"),
                ("dead air tolerated", f"{preset.dead_air_tolerance:g}s"),
                ("trim aggression", preset.trim_aggression),
            )),
            ("text", (
                ("max captions / minute", preset.max_captions_per_minute),
                ("min caption spacing", f"{preset.min_caption_spacing:g}s"),
                ("max words per caption", preset.max_caption_words),
                ("min line score", preset.caption_min_priority),
                ("zones", ", ".join(preset.text_zones)),
                ("draws real text", preset.allow_real_text),
            )),
            ("visual emphasis", (
                ("max punch scale", f"{preset.max_zoom_scale:g}%"),
                ("max push scale", f"{preset.max_push_scale:g}%"),
                ("max zooms / minute", preset.max_zooms_per_minute),
                ("zoom protected clips", preset.zoom_protected_clips),
                ("zoom retimed clips", preset.zoom_retimed_clips),
            )),
            ("cards", (
                ("title cards", preset.title_cards),
                ("chapter cards", preset.chapter_cards),
                ("card duration", f"{preset.card_duration:g}s"),
                ("min section length", f"{preset.min_section_seconds:g}s"),
            )),
            ("audio", (
                ("placeholders", ", ".join(sorted(preset.audio_kinds)) or "none"),
                ("real audio ops", preset.allow_audio_ops),
            )),
            ("safety", (
                ("min confidence", preset.min_confidence),
                ("min stack spacing", f"{preset.min_stack_spacing:g}s"),
                ("marker prefix", preset.marker_prefix or "(none)"),
            )),
        ):
            print(f"  {heading}")
            for label, value in rows:
                print(f"    {label:<24}: {value}")
            print()
        if preset.preferred_kinds:
            print("  prefers  : " + ", ".join(sorted(preset.preferred_kinds)))
        if preset.limited_kinds:
            print("  limits   : " + ", ".join(
                f"{kind} <= {rate:g}/min"
                for kind, rate in sorted(preset.limited_kinds.items())
            ))
        if preset.forbidden_kinds:
            print("  never    : " + ", ".join(sorted(preset.forbidden_kinds)))
        problems = preset.problems()
        if problems:
            print("\n  PROBLEMS:")
            for problem in problems:
                print(f"    ! {problem}")
        return EXIT_OK

    raise EditingError("Unknown style command")


def _compile_options(args) -> CompileOptions:
    return CompileOptions(
        include_base=not getattr(args, "no_base", False),
        use_critic=not getattr(args, "no_critic", False),
        markers_only=getattr(args, "markers_only", False),
        max_operations=getattr(args, "max_operations", None) or 400,
    )


def _style_from(args):
    """The preset named on the command line, with any inline overrides."""
    overrides = {}
    for flag, field in (
        ("max_edits_per_minute", "max_edits_per_minute"),
        ("max_captions_per_minute", "max_captions_per_minute"),
        ("max_zooms_per_minute", "max_zooms_per_minute"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            overrides[field] = value
    if getattr(args, "no_text", False):
        overrides["max_captions_per_minute"] = 0.0
    if getattr(args, "no_zooms", False):
        overrides["max_zoom_scale"] = 100.0
        overrides["max_zooms_per_minute"] = 0.0
    return style_presets.get(getattr(args, "style", None), **overrides)


def cmd_layers(args) -> int:
    """Build, inspect, validate and (explicitly) apply a styled layered edit."""
    pipeline = _pipeline(args)
    command = args.layers_command

    # -- build ------------------------------------------------------------
    if command == "build":
        plan = pipeline.layers(
            name=args.name,
            style=_style_from(args),
            options=_compile_options(args),
        )
        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK
        _print_layers(plan, limit=args.limit)
        print(f"\nWritten to {pipeline.config.layers_dir}")
        return EXIT_OK

    # -- report -----------------------------------------------------------
    if command == "report":
        plan = pipeline.load_layers(name=args.name)
        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK
        print(layers_report.render(plan, limit=args.limit))
        return EXIT_OK

    # -- export -----------------------------------------------------------
    if command == "export":
        plan = pipeline.load_layers(name=args.name)
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.suffix.lower() == ".txt":
            layers_report.write(target, layers_report.render(plan, limit=200))
        else:
            target.write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if args.json:
            _emit({"success": True, "written": str(target),
                   "items": len(plan), "operations": plan.operation_count})
            return EXIT_OK
        print(f"Wrote {len(plan)} layer item(s) to {target}")
        return EXIT_OK

    # -- dry-run ----------------------------------------------------------
    if command == "dry-run":
        plan = pipeline.load_layers(name=args.name)
        report = pipeline.run_layers(plan, mode="dry_run", name=args.name)
        if args.json:
            _emit({"success": True, "report": report.to_dict(),
                   "dry_run_passed": plan.dry_run_passed,
                   "explanation": plan.explanation})
            return EXIT_OK
        print(f"Layer dry run: "
              f"{'PASSED' if plan.dry_run_passed else 'FAILED'}")
        print(f"  style      : {plan.style}")
        print(f"  operations : {plan.operation_count}")
        print(f"  executed   : {report.executed}  <- nothing has been applied")
        if plan.dry_run_error:
            print(f"  error      : {plan.dry_run_error.get('error')}")
            if plan.dry_run_error.get("hint"):
                print(f"  hint       : {plan.dry_run_error['hint']}")
        for line in plan.explanation[: args.limit]:
            print(f"    {line}")
        for warning in plan.warnings:
            print(f"  ! {warning}")
        return EXIT_OK

    # -- execute ----------------------------------------------------------
    if command == "execute":
        plan = pipeline.load_layers(name=args.name)
        if not args.yes:
            raise EditingError(
                "Applying a layered edit needs an explicit confirmation",
                hint="This writes to the rough cut sequence in Premiere. "
                     "Re-run with --yes once you have read the dry run and "
                     "`layers report`.",
            )
        report = pipeline.run_layers(
            plan,
            mode="execute",
            name=args.name,
            allow_active_sequence=args.allow_active_sequence,
        )
        if args.json:
            _emit({"success": report.ok, **report.to_dict()})
            return EXIT_OK if report.ok or report.refused_reason else EXIT_ERROR

        print(f"Layered edit execution ({plan.sequence_name}, {plan.style})")
        print(f"  dry run    : {'passed' if report.dry_run_passed else 'FAILED'}")
        print(f"  on scratch : {report.on_scratch}")
        print(f"  executed   : {report.executed}")
        print(f"  operations : {report.operations_succeeded}/"
              f"{report.operations_attempted}")
        if report.refused_reason:
            print(f"  refused    : {report.refused_reason}")
        if report.error:
            print(f"  error      : {report.error.get('error')}")
        return EXIT_OK if report.executed or report.refused_reason else EXIT_ERROR

    # -- show-deferred ----------------------------------------------------
    if command == "show-deferred":
        plan = pipeline.load_layers(name=args.name)
        held = plan.deferred() + plan.rejected()
        if args.json:
            _emit({"success": True, "count": len(held), "style": plan.style,
                   "deferred": [item.to_dict() for item in held]})
            return EXIT_OK
        print(layers_report.render_deferred(plan, limit=args.limit))
        return EXIT_OK

    # -- show-density -----------------------------------------------------
    if command == "show-density":
        plan = pipeline.load_layers(name=args.name)
        if args.json:
            _emit({"success": True, "style": plan.style, **plan.density()})
            return EXIT_OK
        print(layers_report.render_density(plan))
        return EXIT_OK

    raise EditingError("Unknown layers command")


def _print_layers(plan, *, limit: int = 20) -> None:
    density = plan.density()
    stats = plan.stats()
    preset = plan.preset or {}

    print(f"Layered edit: {plan.sequence_name}  [{plan.style}]")
    print(f"  {stats['planned']} item(s) planned, {stats['deferred']} held "
          f"back, {stats['rejected']} rejected")
    print(f"  {stats['convertible']} become operations "
          f"({stats['marker_only']} of them markers)")
    print(f"  density    : {density['edits_per_minute']:.2f} edits/min "
          f"(<= {preset.get('max_edits_per_minute', '?')}), "
          f"{density['captions_per_minute']:.2f} captions/min "
          f"(<= {preset.get('max_captions_per_minute', '?')})")
    print(f"  dry run    : {'passed' if plan.dry_run_passed else 'not run'}")
    print(f"  on scratch : {plan.on_scratch}  (nothing has been applied)")

    by_layer = density["by_layer"]
    if by_layer:
        print("\n  Planned by layer:")
        for layer, count in sorted(by_layer.items()):
            print(f"    {layer:<10} {count}")

    interesting = [
        item for item in plan.planned()
        if item.layer not in ("base",)
    ]
    if interesting:
        print("\n  Timeline:")
        for item in sorted(interesting, key=lambda i: i.start)[:limit]:
            detail = (item.payload.get("text")
                      or item.payload.get("placeholder") or "")
            flag = " (marker)" if item.is_marker_only else ""
            print(f"    [{item.start:8.2f}] {item.layer:<9} {item.kind:<18} "
                  f"{str(detail)[:32]}{flag}")
        if len(interesting) > limit:
            print(f"    ... and {len(interesting) - limit} more.")

    if plan.dry_run_error:
        print(f"\n  ! {plan.dry_run_error.get('error')}")
    for warning in plan.warnings:
        print(f"  ! {warning}")


def _asset_options(args) -> AssetOptions:
    tracks = {}
    for role, flag in (("sfx", "sfx_track"), ("music", "music_track"),
                       ("visual", "visual_track")):
        value = getattr(args, flag, None)
        if value:
            tracks[role] = value.strip().upper()
    return AssetOptions(
        min_score=getattr(args, "min_score", None) or 0.5,
        allow_unsafe=getattr(args, "allow_unsafe", False),
        markers_only=getattr(args, "markers_only", False),
        use_critic=not getattr(args, "no_critic", False),
        max_operations=getattr(args, "max_operations", None) or 500,
        tracks=tracks or None,
    )


def _asset_limits(args) -> PlacementLimits:
    limits = PlacementLimits()
    for flag, field in (
        ("min_sfx_gap", "min_sfx_gap"),
        ("max_sfx_per_minute", "max_sfx_per_minute"),
        ("max_concurrent_audio", "max_concurrent_audio"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            setattr(limits, field, value)
    if getattr(args, "allow_music_over_speech", False):
        limits.require_ducking_over_speech = False
    return limits


def cmd_assets(args) -> int:
    """The local asset library, and placing from it."""
    pipeline = _pipeline(args)
    command = args.assets_command
    root = getattr(args, "root", None)

    # -- init -------------------------------------------------------------
    if command == "init":
        result = pipeline.init_assets(root=root)
        if args.json:
            _emit({"success": True, **result})
            return EXIT_OK
        print(f"Asset library at {result['root']}")
        for path in result["created"]:
            print(f"  created  {path}")
        for path in result["existing"]:
            print(f"  existing {path}")
        for path in result["docs"]:
            print(f"  wrote    {path}")
        print("\n  Put local files in those folders, then run "
              "`assets index`.\n  Nothing is downloaded and nothing in the "
              "library is ever modified.")
        return EXIT_OK

    # -- index ------------------------------------------------------------
    if command == "index":
        library = pipeline.index_assets(
            root=root,
            probe_durations=not args.no_probe,
            reuse=not args.rebuild,
        )
        if args.json:
            _emit({"success": True, **library.to_dict()})
            return EXIT_OK
        stats = library.stats()
        print(f"Indexed {stats['total']} asset(s) under {library.root}")
        for label, key in (
            ("usable", "usable"), ("need review", "needs_review"),
            ("missing", "missing"), ("with sidecar", "with_sidecar"),
            ("with duration", "with_duration"), ("skipped", "skipped"),
        ):
            print(f"  {label:<14}: {stats[key]}")
        for name, count in sorted(stats["by_category"].items()):
            print(f"    {name:<12} {count}")
        for warning in library.warnings[: args.limit]:
            print(f"  ! {warning}")
        return EXIT_OK

    # -- list -------------------------------------------------------------
    if command == "list":
        library = pipeline.asset_library_or_empty(root=root)
        items = library.find(args.filter or "")
        if args.category:
            items = [item for item in items if item.category == args.category]
        if args.json:
            _emit({"success": True, "count": len(items),
                   "root": library.root,
                   "assets": [item.to_dict() for item in items]})
            return EXIT_OK
        print(f"{len(items)} asset(s) in {library.root or '(no library)'}:")
        for item in items[: args.limit]:
            print("  " + item.summary())
        if len(items) > args.limit:
            print(f"  ... and {len(items) - args.limit} more.")
        if not items:
            print("  (nothing indexed -- run `assets init` then `assets index`)")
        return EXIT_OK

    # -- show -------------------------------------------------------------
    if command == "show":
        library = pipeline.asset_library_or_empty(root=root)
        matches = library.find(args.asset)
        if not matches:
            raise EditingError(
                f"No asset matches '{args.asset}'",
                hint="Run `assets list` to see what is indexed.",
            )
        item = matches[0]
        if args.json:
            _emit({"success": True, **item.to_dict()})
            return EXIT_OK
        print(assets_report.render_asset(library, item))
        if len(matches) > 1:
            print(f"\n  ({len(matches) - 1} other asset(s) also matched "
                  f"'{args.asset}')")
        return EXIT_OK

    # -- validate ---------------------------------------------------------
    if command == "validate":
        library = pipeline.asset_library_or_empty(root=root)
        problems = library.needing_review() + library.missing()
        if args.json:
            _emit({"success": True, "root": library.root,
                   "problems": len(problems),
                   "needs_review": [i.to_dict() for i in library.needing_review()],
                   "missing": [i.to_dict() for i in library.missing()],
                   "skipped": list(library.skipped),
                   "warnings": list(library.warnings)})
            return EXIT_OK
        print(assets_report.render_validation(library, limit=args.limit))
        return EXIT_OK

    # -- report -----------------------------------------------------------
    if command == "report":
        library = pipeline.asset_library_or_empty(root=root)
        if args.json:
            from editing.assets.match import coverage

            _emit({"success": True, "root": library.root,
                   "stats": library.stats(),
                   "coverage": coverage(library, style=args.style or "")})
            return EXIT_OK
        print(assets_report.render_library(
            library, style=args.style or "", limit=args.limit
        ))
        return EXIT_OK

    # -- match ------------------------------------------------------------
    if command == "match":
        from editing.assets.match import rank_candidates, requirement_for

        library = pipeline.asset_library_or_empty(root=root)
        requirement = requirement_for(args.kind)
        if requirement is None:
            raise EditingError(
                f"'{args.kind}' is not a placeholder kind an asset can fill",
                hint="Try one of: "
                     + ", ".join(sorted(__import__(
                         "editing.assets.match", fromlist=["REQUIREMENTS"]
                     ).REQUIREMENTS)),
            )
        matches = rank_candidates(
            args.kind, library, style=args.style or "",
            slot_duration=args.slot or 0.0, min_score=args.min_score or 0.5,
        )
        if args.json:
            _emit({"success": True, "kind": args.kind,
                   "label": requirement.label,
                   "count": len(matches),
                   "matches": [m.to_dict() for m in matches]})
            return EXIT_OK
        print(f"{args.kind} wants {requirement.label}.")
        print(f"{len(matches)} candidate(s) in the library:\n")
        for match in matches[: args.limit]:
            mark = "+" if match.accepted else "-"
            print(f"  {mark} {match.score:.2f}  {match.filename}")
            if match.rejected:
                print(f"        ruled out: {match.rejected[:110]}")
            for why, delta in match.reasons:
                print(f"        {delta:+.2f}  {why[:100]}")
        if not matches:
            print("  (nothing in the library is even the right category)")
        return EXIT_OK

    # -- plan -------------------------------------------------------------
    if command == "plan":
        plan = pipeline.asset_plan(
            name=args.name, root=root,
            options=_asset_options(args), limits=_asset_limits(args),
        )
        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK
        _print_asset_plan(plan, limit=args.limit)
        print(f"\nWritten to {pipeline.config.asset_library_dir}")
        return EXIT_OK

    # -- dry-run ----------------------------------------------------------
    if command == "dry-run":
        plan = pipeline.load_asset_plan(name=args.name)
        report = pipeline.run_assets(plan, mode="dry_run", name=args.name)
        if args.json:
            _emit({"success": True, "report": report.to_dict(),
                   "dry_run_passed": plan.dry_run_passed,
                   "explanation": plan.explanation})
            return EXIT_OK
        print(f"Asset dry run: "
              f"{'PASSED' if plan.dry_run_passed else 'FAILED'}")
        print(f"  operations : {plan.operation_count}")
        print(f"  executed   : {report.executed}  <- nothing has been placed")
        if plan.dry_run_error:
            print(f"  error      : {plan.dry_run_error.get('error')}")
            if plan.dry_run_error.get("hint"):
                print(f"  hint       : {plan.dry_run_error['hint']}")
        for line in plan.explanation[: args.limit]:
            print(f"    {line}")
        for warning in plan.warnings:
            print(f"  ! {warning}")
        return EXIT_OK

    # -- execute ----------------------------------------------------------
    if command == "execute":
        plan = pipeline.load_asset_plan(name=args.name)
        if not args.yes:
            raise EditingError(
                "Placing assets needs an explicit confirmation",
                hint="This imports media and writes clips into the rough cut "
                     "sequence. Re-run with --yes once you have read the dry "
                     "run and `assets report`.",
            )
        report = pipeline.run_assets(
            plan, mode="execute", name=args.name,
            allow_active_sequence=args.allow_active_sequence,
        )
        if args.json:
            _emit({"success": report.ok, **report.to_dict()})
            return EXIT_OK if report.ok or report.refused_reason else EXIT_ERROR

        print(f"Asset placement ({plan.sequence_name}, {plan.style})")
        print(f"  dry run    : {'passed' if report.dry_run_passed else 'FAILED'}")
        print(f"  on scratch : {report.on_scratch}")
        print(f"  executed   : {report.executed}")
        print(f"  operations : {report.operations_succeeded}/"
              f"{report.operations_attempted}")
        if report.refused_reason:
            print(f"  refused    : {report.refused_reason}")
        if report.error:
            print(f"  error      : {report.error.get('error')}")
        return EXIT_OK if report.executed or report.refused_reason else EXIT_ERROR

    # -- show-missing -----------------------------------------------------
    if command == "show-missing":
        plan = pipeline.load_asset_plan(name=args.name)
        if args.json:
            _emit({"success": True, "count": len(plan.missing()),
                   "missing": [p.to_dict() for p in plan.missing()]})
            return EXIT_OK
        print(assets_report.render_missing(plan, limit=args.limit))
        return EXIT_OK

    # -- show-deferred ----------------------------------------------------
    if command == "show-deferred":
        plan = pipeline.load_asset_plan(name=args.name)
        if args.json:
            _emit({"success": True, "count": len(plan.deferred()),
                   "deferred": [p.to_dict() for p in plan.deferred()]})
            return EXIT_OK
        print(assets_report.render_deferred(plan, limit=args.limit))
        return EXIT_OK

    raise EditingError("Unknown assets command")


def _print_asset_plan(plan, *, limit: int = 20) -> None:
    stats = plan.stats()
    print(f"Asset placement: {plan.sequence_name}  [{plan.style}]")
    print(f"  library    : {plan.library_root or '(none)'}"
          f"  ({(plan.library_stats or {}).get('usable', 0)} usable)")
    print(f"  tracks     : " + ", ".join(
        f"{role}={name}" for role, name in sorted(plan.tracks.items())
    ))
    print(f"  placed     : {stats['placed']} "
          f"({stats['distinct_assets']} distinct asset(s))")
    print(f"  missing    : {stats['missing']}")
    print(f"  rejected   : {stats['rejected']}")
    print(f"  unsafe     : {stats['unsafe']}")
    print(f"  marker only: {stats['marker_only']}")
    print(f"  operations : {plan.operation_count}")
    print(f"  dry run    : {'passed' if plan.dry_run_passed else 'not run'}")
    print(f"  on scratch : {plan.on_scratch}  (nothing has been placed)")

    placed = plan.placed()
    if placed:
        print("\n  Placed:")
        for placement in placed[:limit]:
            print(f"    [{placement.start:8.2f}] {placement.kind:<16} "
                  f"{placement.asset_filename[:32]:<32} {placement.track}")

    held = plan.deferred()
    if held:
        print("\n  Not placed:")
        for placement in held[:limit]:
            print(f"    [{placement.start:8.2f}] {placement.kind:<16} "
                  f"{placement.status:<12} {placement.reason[:60]}")
        if len(held) > limit:
            print(f"    ... and {len(held) - limit} more.")

    if plan.dry_run_error:
        print(f"\n  ! {plan.dry_run_error.get('error')}")
    for warning in plan.warnings:
        print(f"  ! {warning}")


def _auto_runner(args) -> AutoRunner:
    """An orchestrator wired to the same config every other command uses."""
    config, sampling, audio = load_config(
        sampling=_sampling_from(args),
        audio=_audio_from(args),
        output_dir=Path(args.output_dir) if getattr(args, "output_dir", None)
        else None,
        vision_backend=("mock" if getattr(args, "mock", False)
                        else getattr(args, "backend", None)),
        vision_model=getattr(args, "model", None),
        vision_base_url=getattr(args, "base_url", None),
        use_premiere=(False if getattr(args, "no_premiere", False) else None),
    )
    return AutoRunner(
        config, sampling=sampling, audio=audio, say=_reporter(args)
    )


def _auto_config(args) -> AutoRunConfig:
    return AutoRunConfig(
        footage_folder=str(args.folder or ""),
        style=args.style,
        name=args.name,
        asset_library=str(getattr(args, "asset_library", "") or ""),
        mock=getattr(args, "mock", False),
        no_premiere=getattr(args, "no_premiere", False),
        markers_only=getattr(args, "markers_only", False),
        max_windows=getattr(args, "max_windows", None),
        recursive=not getattr(args, "no_recursive", False),
        keep_frames=getattr(args, "keep_frames", False),
        use_motion=not getattr(args, "no_motion", False),
        skip_review=getattr(args, "skip_review", False),
        skip_assets=getattr(args, "skip_assets", False),
        skip_episode=getattr(args, "skip_episode", False),
        feedback=getattr(args, "feedback", False),
        transcribe=getattr(args, "transcribe", False),
        transcribe_model=getattr(args, "transcribe_model", "") or "",
        transcribe_language=getattr(args, "transcribe_language", "") or "",
        transcribe_backend=getattr(args, "transcribe_backend", "") or "",
        director=getattr(args, "director", False),
        director_mode=getattr(args, "director_mode", "") or "hybrid",
        director_model=getattr(args, "director_model", "") or "",
        director_backend=getattr(args, "director_backend", "") or "",
        style_guide=getattr(args, "style_guide", "") or "",
        target_duration=float(getattr(args, "target_duration", 0.0) or 0.0),
        retention_cut=getattr(args, "retention_cut", False),
        retention_mode=getattr(args, "retention_mode", "") or "report_only",
        cold_open=not getattr(args, "no_cold_open", False),
        max_cold_open_seconds=float(
            getattr(args, "max_cold_open_seconds", 0.0) or 0.0),
        dead_air_aggressiveness=getattr(
            args, "dead_air_aggressiveness", "") or "",
        render_proxy=getattr(args, "render_proxy", False),
        render_quality=getattr(args, "render_quality", "") or "",
        render_height=int(getattr(args, "render_height", 0) or 0),
        captions=getattr(args, "captions", "") or "off",
        max_captions_per_minute=float(
            getattr(args, "max_captions_per_minute", 0.0) or 0.0),
        max_caption_seconds=float(
            getattr(args, "max_caption_seconds", 0.0) or 0.0),
        max_caption_words=int(getattr(args, "max_caption_words", 0) or 0),
        min_caption_confidence=float(
            getattr(args, "min_caption_confidence", 0.0) or 0.0),
        require_caption_confidence=getattr(
            args, "require_caption_confidence", False),
        audio_polish=getattr(args, "audio_polish", "") or "off",
        max_sfx_per_minute=float(
            getattr(args, "max_sfx_per_minute", 0.0) or 0.0),
        music_bed=not getattr(args, "no_music_bed", False),
        ducking=not getattr(args, "no_ducking", False),
        review_package=not getattr(args, "no_review_package", False),
        visual_layer=getattr(args, "visual_layer", "") or "off",
        visual_mode=getattr(args, "visual_mode", "") or "plan_only",
        max_effects_per_minute=float(
            getattr(args, "max_effects_per_minute", 0.0) or 0.0),
        max_callouts_per_minute=float(
            getattr(args, "max_callouts_per_minute", 0.0) or 0.0),
        allow_freeze_frames=not getattr(args, "no_freeze_frames", False),
        allow_callouts=not getattr(args, "no_callouts", False),
        allow_replays=not getattr(args, "no_replays", False),
        allow_screen_shake=getattr(args, "allow_screen_shake", False),
        export_premiere_visual_plan=getattr(
            args, "export_premiere_visual_plan", False),
        conform=getattr(args, "conform", "") or "full",
        color_look=getattr(args, "color_look", "") or "",
        music_library=str(getattr(args, "music_library", "") or ""),
        target_lufs=float(getattr(args, "target_lufs", -14.0) or -14.0),
        max_transitions=int(getattr(args, "max_transitions", 6) or 6),
        deliver=getattr(args, "deliver", False),
        deliver_output=str(getattr(args, "deliver_output", "") or ""),
        deliver_preset=str(getattr(args, "deliver_preset", "") or ""),
    )


def cmd_auto(args) -> int:
    """The whole pipeline: plan everything, execute nothing without --yes."""
    runner = _auto_runner(args)
    command = args.auto_command

    # -- run --------------------------------------------------------------
    if command == "run":
        if not args.folder:
            raise EditingError(
                "auto run needs a footage folder",
                hint="python -m editing.cli auto run --folder <folder> "
                     "--style <preset>",
            )
        state = runner.start(
            _auto_config(args), force_new_run=args.force_new_run
        )
        _note(f"Run {state.run_id}")
        state = runner.run(state)

        if args.json:
            _emit({"success": state.status != "failed", **state.to_dict()})
            return EXIT_OK if state.status != "failed" else EXIT_ERROR
        print(auto_report.render_status(state))
        print(f"\n  Report: {auto_store.report_paths(runner.config, state.run_id)[1]}")
        _print_next(state)
        return EXIT_OK if state.status != "failed" else EXIT_ERROR

    # -- resume -----------------------------------------------------------
    if command == "resume":
        state = runner.resolve(args.run)
        if args.style and args.style != state.config.style:
            # Restyling in place. The style is one of the config fields the
            # layer and asset stages fingerprint, so changing it here
            # invalidates exactly those checkpoints and leaves the analysis
            # alone -- no flag to remember, and no new run folder.
            from dataclasses import replace as _replace

            _note(f"Restyling {state.config.style} -> {args.style}")
            state.config = _replace(state.config, style=args.style)
            auto_store.write_config(runner.config, state.run_id, state.config)
        _note(f"Resuming {state.run_id}")
        state = runner.resume(state, refresh=args.refresh or ())

        if args.json:
            _emit({"success": state.status != "failed", **state.to_dict()})
            return EXIT_OK if state.status != "failed" else EXIT_ERROR
        print(auto_report.render_status(state))
        _print_next(state)
        return EXIT_OK if state.status != "failed" else EXIT_ERROR

    # -- status -----------------------------------------------------------
    if command == "status":
        state = runner.resolve(args.run)
        if args.json:
            _emit({"success": True, **state.to_dict()})
            return EXIT_OK
        print(auto_report.render_status(state))
        return EXIT_OK

    # -- list-runs --------------------------------------------------------
    if command == "list-runs":
        runs = auto_store.list_runs(runner.config, limit=args.limit)
        if args.json:
            _emit({"success": True, "count": len(runs), "runs": runs})
            return EXIT_OK
        if not runs:
            print("No automated runs yet. Start one with:")
            print("  python -m editing.cli auto run --folder <folder> "
                  "--style <preset>")
            return EXIT_OK
        print(f"{len(runs)} run(s), newest first:\n")
        for entry in runs:
            modes = " mock" if entry.get("mock") else ""
            print(f"  {entry['run_id']:<40} {entry.get('status', '?'):<9}"
                  f"{modes}")
            print(f"      style {entry.get('style', '?')}   "
                  f"passed {entry.get('passed', 0)}  "
                  f"failed {entry.get('failed', 0)}  "
                  f"blocked {entry.get('blocked', 0)}  "
                  f"executed {entry.get('gates_executed', 0)}")
            if entry.get("folder"):
                print(f"      {entry['folder']}")
        return EXIT_OK

    # -- report -----------------------------------------------------------
    if command == "report":
        state = runner.resolve(args.run)
        state.gates = auto_gates.compute_gates(runner.config, state)
        report = auto_report.build_report(runner.config, state)
        if args.json:
            _emit({"success": True, **report.to_dict()})
            return EXIT_OK
        print(auto_report.render(state, report))
        return EXIT_OK

    # -- show-gates -------------------------------------------------------
    if command == "show-gates":
        state = runner.resolve(args.run)
        gates = auto_gates.compute_gates(runner.config, state)
        state.gates = gates
        auto_store.save(runner.config, state)

        if args.json:
            _emit({"success": True, "run_id": state.run_id,
                   "gates": [gate.to_dict() for gate in gates]})
            return EXIT_OK
        print(f"Execution gates for {state.run_id}\n")
        print("  Nothing below has run unless it says EXECUTED. Each gate is")
        print("  a separate decision and needs its own --yes.\n")
        for gate in gates:
            print(gate.render())
            print()
        return EXIT_OK

    # -- finish -----------------------------------------------------------
    if command == "finish":
        state = runner.resolve(args.run)
        result = auto_gates.finish(
            runner.config, state,
            yes=args.yes, again=args.again,
            deliver=not args.no_deliver,
            output=args.output or "", preset=args.preset or "",
            say=_reporter(args),
        )
        if args.json:
            _emit(result)
            return EXIT_OK if result.get("success") else EXIT_ERROR

        print(f"Finish ({state.run_id})")
        for step in result.get("steps", []):
            print(f"  {step['stage']:<10} executed={step.get('executed')} "
                  f"{step.get('operations_succeeded', 0)}/"
                  f"{step.get('operations_attempted', 0)}")
        delivery = result.get("delivery")
        if delivery:
            print(f"  delivered  {delivery.get('delivered')}  "
                  f"{delivery.get('output_path')}")
            if delivery.get("size_bytes"):
                print(f"  size       "
                      f"{delivery['size_bytes'] / 1_000_000:.1f} MB, "
                      f"{delivery.get('duration', 0):.1f}s")
        if result.get("refused_reason"):
            print(f"  REFUSED    {result['refused_reason']}")
        return EXIT_OK if result.get("success") else EXIT_ERROR

    # -- execute-stage ----------------------------------------------------
    if command == "execute-stage":
        state = runner.resolve(args.run)
        result = auto_gates.execute(
            runner.config, state, args.stage,
            yes=args.yes,
            allow_active_sequence=args.allow_active_sequence,
            again=getattr(args, "again", False),
            say=_reporter(args),
        )
        if args.json:
            _emit(result)
            return EXIT_OK if result.get("success") else EXIT_ERROR

        print(f"Execute {args.stage} ({state.run_id})")
        if result.get("refused_reason"):
            print(f"  REFUSED  : {result['refused_reason']}")
        else:
            print(f"  sequence : {result.get('sequence')}")
            print(f"  executed : {result.get('executed')}")
            print(f"  operations: {result.get('operations_succeeded')}/"
                  f"{result.get('operations_attempted')}")
            if result.get("error"):
                print(f"  error    : {result['error'].get('error')}")
        if result.get("next_command"):
            print(f"  next     : {result['next_command']}")
        return EXIT_OK if result.get("success") else EXIT_ERROR

    # -- show-checks ------------------------------------------------------
    if command == "show-checks":
        from editing.reliability import report as gate_report
        from editing.reliability import run as reliability_run
        from editing.reliability.schema import GateReport

        state = runner.resolve(args.run)
        report = None
        if not args.rebuild:
            stored = reliability_run.report_path(runner.config, state.run_id)
            if stored is not None and stored.exists():
                try:
                    report = GateReport.from_dict(
                        json.loads(stored.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    report = None
        if report is None:
            # Re-evaluating is always safe: the checks read files and stage
            # summaries and change nothing.
            report, _inputs = reliability_run.check_run(runner.config, state)

        if args.json:
            _emit({"success": True, **report.to_dict()})
            return EXIT_OK
        print(gate_report.render(report))
        return EXIT_OK

    # -- batch ------------------------------------------------------------
    if command == "batch":
        from editing.batch import report as batch_report
        from editing.batch import run as batch_run
        from editing.batch.schema import BatchConfig

        batch = BatchConfig(
            root=str(args.root),
            style=args.style,
            name=args.name,
            limit=int(args.limit or 0),
            only_new=args.only_new,
            resume=args.resume,
            force=args.force,
            dry_run=args.dry_run,
            recursive=not args.no_recursive,
            director=args.director,
            retention_cut=args.retention_cut,
            render_proxy=args.render_proxy,
            no_premiere=getattr(args, "no_premiere", False),
            mock=args.mock,
            transcribe=args.transcribe,
            captions=args.captions,
            audio_polish=args.audio_polish,
            visual_layer=args.visual_layer,
            visual_mode=args.visual_mode,
        )
        summary = batch_run.run_batch(
            runner.config, batch, runner=runner, say=_reporter(args))

        if args.json:
            _emit({"success": not summary.failed, **summary.to_dict()})
            # A batch with failures still exits 0: the useful outcome is the
            # folders that worked, and the summary names the ones that did not.
            return EXIT_OK
        print(batch_report.render(summary, limit=args.limit or 60))
        return EXIT_OK

    # -- list-batches -----------------------------------------------------
    if command == "list-batches":
        from editing.batch import store as batch_store

        batches = batch_store.list_batches(runner.config, limit=args.limit)
        if args.json:
            _emit({"success": True, "count": len(batches),
                   "batches": batches})
            return EXIT_OK
        if not batches:
            print("No batches yet. Start one with:")
            print("  python -m editing.cli auto batch --root <folder> "
                  "--dry-run")
            return EXIT_OK
        print(f"{len(batches)} batch(es), newest first:\n")
        for entry in batches:
            print(f"  {entry['batch_id']:<44} {entry.get('status', '?')}")
            print(f"      {entry.get('completed', 0)} completed, "
                  f"{entry.get('failed', 0)} failed, "
                  f"{entry.get('skipped', 0)} skipped   "
                  f"style {entry.get('style', '?')}")
            if entry.get("root"):
                print(f"      {entry['root']}")
        return EXIT_OK

    # -- batch-report -----------------------------------------------------
    if command == "batch-report":
        from editing.batch import report as batch_report
        from editing.batch import store as batch_store

        batch_id = args.batch or batch_store.latest_batch_id(runner.config)
        if not batch_id:
            raise EditingError(
                "No batches exist yet",
                hint="Start one with `auto batch --root <folder> --dry-run`.",
            )
        summary = batch_store.load(runner.config, batch_id)
        if args.json:
            _emit({"success": True, **summary.to_dict()})
            return EXIT_OK
        print(batch_report.render(summary, limit=args.limit))
        return EXIT_OK

    # -- explain-failure --------------------------------------------------
    if command == "explain-failure":
        state = runner.resolve(args.run)
        if args.json:
            failed = state.of_status("failed")
            blocked = state.of_status("blocked")
            _emit({
                "success": True, "run_id": state.run_id,
                "status": state.status,
                "failed": [r.to_dict() for r in failed],
                "blocked": [r.to_dict() for r in blocked],
            })
            return EXIT_OK
        print(auto_report.render_failure(state))
        return EXIT_OK

    # -- clean ------------------------------------------------------------
    if command == "clean":
        result = auto_store.clean(
            runner.config, run_id=args.run,
            failed_only=not args.all, dry_run=not args.yes,
        )
        if args.json:
            _emit({"success": True, **result})
            return EXIT_OK
        if result["dry_run"]:
            print("Dry run -- nothing was deleted. Add --yes to remove.\n")
        for entry in result["removed"]:
            verb = "would remove" if result["dry_run"] else "removed"
            print(f"  {verb} {entry['run_id']}  ({entry['status']})")
        for entry in result["kept"]:
            print(f"  kept    {entry['run_id']}  -- {entry['reason']}")
        if not result["removed"] and not result["kept"]:
            print("  Nothing to clean.")
        return EXIT_OK

    raise EditingError("Unknown auto command")


def _print_next(state) -> None:
    """The two or three lines a person needs after a run."""
    failure = state.first_failure()
    if failure is not None and failure.failure is not None:
        print()
        print(failure.failure.render())
        return

    ready = [gate for gate in state.gates if gate.ready]
    blocked = [gate for gate in state.gates if not gate.ready
               and not gate.executed]
    print()
    if ready:
        print(f"  {len(ready)} gate(s) ready to execute. Each needs its own "
              "--yes:")
        for gate in ready:
            print(f"    {gate.command}")
    elif blocked:
        print(f"  No gate is ready yet. Why: {blocked[0].blocked_reason[:150]}")
    print(f"  Full report: python -m editing.cli auto report "
          f"--run {state.run_id}")


def _print_roughcut(plan, *, limit: int = 30) -> None:
    stats = plan.stats()
    print(f"Rough cut: {plan.sequence_name}")
    print(f"  {stats['placements']} clip(s), {stats['cut_duration']}s from "
          f"{stats['source_duration']}s of footage")
    print(f"  {stats['protected']} protected, {stats['sped_up']} sped up, "
          f"{stats['markers']} marker(s)")
    print(f"  {stats['operations']} operation(s), "
          f"{stats['unconverted']} recommendation(s) unconverted")
    print(f"  dry run    : {'passed' if plan.dry_run_passed else 'not run'}")
    print(f"  on scratch : {plan.on_scratch}  (nothing has been applied)")

    if plan.placements:
        print("\n  Assembly:")
        for placement in plan.placements[:limit]:
            flags = " ".join(filter(None, [
                "protected" if placement.protected else "",
                f"{placement.speed:g}x" if placement.speed != 1.0 else "",
            ]))
            print(f"    [{placement.sequence_start:7.2f}-"
                  f"{placement.sequence_end:7.2f}] "
                  f"{placement.keep_reason:<14} {flags}")

    if plan.warnings:
        print("\n  Warnings:")
        for warning in plan.warnings:
            print(f"    ! {warning}")


def cmd_polish(args) -> int:
    """Caption and audio polish: plan them, read them, see what was refused."""
    from editing.polish import report as polish_report

    command = args.polish_command
    pipeline = _run_scoped_pipeline(args)
    style = style_presets.get(args.style) if args.style else style_presets.get()

    # -- captions ---------------------------------------------------------
    if command == "captions":
        if args.report:
            plan = pipeline.load_caption_plan(name=args.name)
        else:
            settings = pipeline.caption_config(
                style, mode=args.captions,
                max_per_minute=(args.max_captions_per_minute or None),
                max_seconds=(args.max_caption_seconds or None),
                max_words=(args.max_caption_words or None),
                min_confidence=(args.min_caption_confidence or None),
                require_confidence=(args.require_caption_confidence or None),
            )
            plan = pipeline.polish_captions(
                name=args.name, style=style, settings=settings)

        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK
        print(polish_report.render_captions(plan, limit=args.limit))
        return EXIT_OK

    # -- audio ------------------------------------------------------------
    if command == "audio":
        if args.report:
            plan = pipeline.load_audio_plan(name=args.name)
        else:
            settings = pipeline.audio_polish_config(
                style, mode=args.audio_polish,
                max_sfx_per_minute=(args.max_sfx_per_minute or None),
                music_bed=(False if args.no_music_bed else None),
                ducking=(False if args.no_ducking else None),
            )
            plan = pipeline.polish_audio(
                name=args.name, style=style, settings=settings)

        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK
        print(polish_report.render_audio(plan, limit=args.limit))
        return EXIT_OK

    # -- show-rejected ----------------------------------------------------
    if command == "show-rejected":
        plan = pipeline.load_caption_plan(name=args.name)
        rejected = plan.rejected
        if args.reason:
            rejected = [d for d in rejected if d.reject_reason == args.reason]
        if args.json:
            _emit({"success": True, "count": len(rejected),
                   "rejected": [d.to_dict() for d in rejected[:args.limit]]})
            return EXIT_OK
        print(f"{len(rejected)} line(s) were refused a caption:\n")
        for decision in rejected[:args.limit]:
            print(f"  {decision.line()}")
        if len(rejected) > args.limit:
            print(f"  ... and {len(rejected) - args.limit} more.")
        print("\n  by reason:")
        for code, count in sorted(
            plan.by_reject_reason().items(), key=lambda kv: -kv[1]
        ):
            print(f"    {count:>4}  {code}")
        return EXIT_OK

    # -- show-missing -----------------------------------------------------
    if command == "show-missing":
        plan = pipeline.load_audio_plan(name=args.name)
        shopping = plan.shopping_list()
        if args.json:
            _emit({"success": True, "count": len(shopping),
                   "missing": shopping})
            return EXIT_OK
        if not shopping:
            print("Nothing is missing: every planned cue has a file behind "
                  "it, or the plan is placeholders-only by design.")
            return EXIT_OK
        print(f"{len(shopping)} sound(s) to go and find:\n")
        for entry in shopping[:args.limit]:
            print(f"  {entry['count']:>3} x {entry['placeholder']}")
            print(f"        kind {entry['kind']}, first needed at "
                  f"{entry['first_at']:.0f}s")
        return EXIT_OK

    raise EditingError("Unknown polish command")


def cmd_visuals(args) -> int:
    """The creative visual layer: plan it, read it, see what was refused."""
    from editing.visuals import report as visual_report
    from editing.visuals import schema as visual_schema

    command = args.visuals_command
    if getattr(args, "latest", False) and not getattr(args, "run", ""):
        args.run = _auto_runner(args).latest_run_id() or ""
    pipeline = _run_scoped_pipeline(args)
    style = style_presets.get(args.style) if args.style else style_presets.get()

    # -- plan -------------------------------------------------------------
    if command == "plan":
        settings = pipeline.visual_config(
            style, layer=args.visual_layer, mode=args.visual_mode,
            max_effects_per_minute=(args.max_effects_per_minute or None),
            max_callouts_per_minute=(args.max_callouts_per_minute or None),
            allow_freeze_frames=(False if args.no_freeze_frames else None),
            allow_callouts=(False if args.no_callouts else None),
            allow_replays=(False if args.no_replays else None),
            allow_screen_shake=(True if args.allow_screen_shake else None),
        )
        visuals, final = pipeline.plan_visuals(
            name=args.name, style=style, settings=settings,
            run_id=getattr(args, "run", "") or "")
        if args.export_premiere_visual_plan and \
                final.execution.premiere is None:
            pipeline.export_visual_premiere_plan(
                name=args.name, visuals=visuals)

        if args.json:
            _emit({"success": True, "visuals": visuals.to_dict(),
                   "final": final.to_dict()})
            return EXIT_OK
        print(visual_report.render(visuals, limit=args.limit))
        print()
        print(f"  Written to {pipeline.config.visuals_dir}")
        return EXIT_OK

    # -- report -----------------------------------------------------------
    if command == "report":
        visuals = pipeline.load_visual_plan(name=args.name)
        if args.json:
            from editing.visuals import store as visuals_store

            built = visual_report.build_report(
                visuals,
                premiere=visuals_store.premiere_or_none(
                    pipeline.config, name=args.name))
            _emit({"success": True, **built.to_dict()})
            return EXIT_OK
        print(visual_report.render(visuals, limit=args.limit))
        return EXIT_OK

    # -- show-final -------------------------------------------------------
    if command == "show-final":
        final = pipeline.load_final_edit(name=args.name)
        if args.json:
            _emit({"success": True, **final.to_dict()})
            return EXIT_OK
        print(visual_report.render_final(final, limit=args.limit))
        return EXIT_OK

    # -- show-accepted ----------------------------------------------------
    if command == "show-accepted":
        visuals = pipeline.load_visual_plan(name=args.name)
        accepted = sorted(visuals.accepted, key=lambda t: t.start)
        if args.effect:
            accepted = [t for t in accepted if t.effect == args.effect]
        if args.json:
            _emit({"success": True, "count": len(accepted),
                   "accepted": [t.to_dict() for t in accepted[:args.limit]]})
            return EXIT_OK
        print(f"{len(accepted)} treatment(s) planned:\n")
        for treatment in accepted[:args.limit]:
            print(f"  {treatment.line()}")
            for note in treatment.safety_notes[:2]:
                print(f"      note: {note[:120]}")
        if len(accepted) > args.limit:
            print(f"  ... and {len(accepted) - args.limit} more.")
        print("\n  by effect:")
        for effect, count in sorted(visuals.by_effect().items(),
                                    key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {effect}")
        print(f"\n  {visual_schema.NOT_RENDERED}")
        return EXIT_OK

    # -- show-rejected ----------------------------------------------------
    if command == "show-rejected":
        visuals = pipeline.load_visual_plan(name=args.name)
        rejected = sorted(visuals.rejected, key=lambda t: t.start)
        if args.reason:
            rejected = [t for t in rejected if t.reject_reason == args.reason]
        if args.json:
            _emit({"success": True, "count": len(rejected),
                   "rejected": [t.to_dict() for t in rejected[:args.limit]]})
            return EXIT_OK
        print(f"{len(rejected)} treatment(s) were refused:\n")
        for treatment in rejected[:args.limit]:
            print(f"  {treatment.line()}")
        if len(rejected) > args.limit:
            print(f"  ... and {len(rejected) - args.limit} more.")
        print("\n  by reason:")
        for code, count in sorted(visuals.by_reject_reason().items(),
                                  key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {code:<26} "
                  f"{visual_report.REASONS.get(code, '')}")
        return EXIT_OK

    # -- export-premiere-plan ---------------------------------------------
    if command == "export-premiere-plan":
        plan = pipeline.export_visual_premiere_plan(name=args.name)
        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK

        from editing.visuals import store as visuals_store

        print(f"Premiere visual plan for '{args.name}'")
        print(f"  operations  : {plan.operation_count}")
        print(f"  treatments  : "
              f"{len({e.treatment_id for e in plan.operations})}")
        print(f"  unsupported : {len(plan.unsupported)}")
        print(f"  dry run     : "
              f"{'passed' if plan.dry_run_passed else 'NOT passed'}")
        if plan.dry_run_error:
            print(f"  error       : {plan.dry_run_error.get('error')}")
            if plan.dry_run_error.get("hint"):
                print(f"  hint        : {plan.dry_run_error['hint']}")
        if plan.by_op():
            print("\n  by operation:")
            for name, count in sorted(plan.by_op().items(),
                                      key=lambda kv: -kv[1]):
                print(f"    {count:>4}  {name}")
        if plan.unsupported:
            print("\n  cannot be expressed:")
            for entry in plan.unsupported[:args.limit]:
                print(f"    {entry.effect} at {entry.start:.1f}s")
                print(f"      why : {entry.reason[:140]}")
                print(f"      else: {entry.alternative[:140]}")
        for warning in plan.warnings:
            print(f"\n  ! {warning}")
        print(f"\n  Written to "
              f"{visuals_store.premiere_path(pipeline.config, args.name)}")
        print("  NOTHING HAS BEEN EXECUTED. This is a plan.")
        return EXIT_OK

    raise EditingError("Unknown visuals command")


def cmd_episode(args) -> int:
    """Build and inspect the episode memory and the retention plan.

    Nine subcommands, none of which touches Premiere. This layer plans; it
    executes nothing, so there is no ``dry-run`` and no ``--yes`` here.
    """
    pipeline = _pipeline(args)
    command = args.episode_command
    name = getattr(args, "name", "structure")

    if command == "build-memory":
        memory = pipeline.episode_memory(
            name=name,
            use_roughcut=not getattr(args, "no_roughcut", False),
            save=not getattr(args, "no_save", False),
        )
        if args.json:
            _emit({"success": True, **memory.to_dict()})
            return EXIT_OK
        print(episode_report.render_memory(memory, limit=args.limit))
        return EXIT_OK

    if command == "plan-retention":
        memory = pipeline.load_episode_memory(name=name)
        plan = pipeline.retention_plan(
            name=name, memory=memory, hook_limit=args.hooks,
            save=not getattr(args, "no_save", False),
        )
        if args.json:
            _emit({"success": True, **plan.to_dict()})
            return EXIT_OK
        print(episode_report.render_plan(plan, memory=memory, limit=args.limit))
        return EXIT_OK

    if command == "report":
        memory = pipeline.load_episode_memory(name=name)
        plan = _retention_or_none(pipeline, name)
        if args.json:
            _emit({
                "success": True,
                "memory": memory.to_dict(),
                "retention": plan.to_dict() if plan else None,
            })
            return EXIT_OK
        print(episode_report.render_memory(memory, limit=args.limit))
        if plan is None:
            _note("No retention plan yet. Run `episode plan-retention`.")
            return EXIT_OK
        print()
        print(episode_report.render_plan(plan, memory=memory, limit=args.limit))
        return EXIT_OK

    if command == "show-beats":
        memory = pipeline.load_episode_memory(name=name)
        if args.json:
            _emit({
                "success": True,
                "timebase": memory.timebase,
                "beats": [
                    beat.to_dict() for beat in memory.beats
                    if not args.kind or beat.kind == args.kind
                ],
            })
            return EXIT_OK
        print(episode_report.render_beats(
            memory, kind=args.kind or "", limit=args.limit))
        return EXIT_OK

    if command == "show-risks":
        plan = pipeline.load_retention_plan(name=name)
        if args.json:
            _emit({
                "success": True,
                "basis": episode_schema.NOT_ANALYTICS,
                "risks": [
                    zone.to_dict() for zone in plan.risks
                    if not args.severity or zone.severity == args.severity
                ],
            })
            return EXIT_OK
        print(episode_report.render_risks(
            plan, severity=args.severity or "", limit=args.limit))
        return EXIT_OK

    if command == "show-hooks":
        plan = pipeline.load_retention_plan(name=name)
        if args.json:
            _emit({
                "success": True,
                "hooks": [hook.to_dict() for hook in plan.top_hooks(args.limit)],
            })
            return EXIT_OK
        print(episode_report.render_hooks(plan, limit=args.limit))
        return EXIT_OK

    if command == "show-open-loops":
        memory = pipeline.load_episode_memory(name=name)
        loops = [
            loop for loop in memory.open_loops
            if not args.unresolved or not loop.resolved
        ]
        if args.json:
            _emit({"success": True,
                   "open_loops": [loop.to_dict() for loop in loops]})
            return EXIT_OK
        print(episode_report.render_open_loops(
            memory, unresolved_only=args.unresolved, limit=args.limit))
        return EXIT_OK

    if command == "show-callbacks":
        memory = pipeline.load_episode_memory(name=name)
        if args.json:
            _emit({
                "success": True,
                "callbacks": [item.to_dict() for item in memory.callbacks],
            })
            return EXIT_OK
        print(episode_report.render_callbacks(memory, limit=args.limit))
        return EXIT_OK

    if command == "export":
        memory = pipeline.load_episode_memory(name=name)
        plan = _retention_or_none(pipeline, name)
        payload = {
            "memory": memory.to_dict(),
            "retention": plan.to_dict() if plan else None,
        }
        if args.suggestions_for:
            if plan is None:
                raise EditingError(
                    "No retention plan to take suggestions from",
                    hint="Run `python -m editing.cli episode plan-retention` "
                         "first.",
                )
            wanted = plan.suggestions_for(args.suggestions_for)
            if args.safe_only:
                wanted = [item for item in wanted if item.auto_safe]
            payload = {
                "basis": episode_schema.NOT_ANALYTICS,
                "timebase": plan.timebase,
                "sequence_name": plan.sequence_name,
                "downstream": args.suggestions_for,
                "safe_only": bool(args.safe_only),
                "suggestions": [item.to_dict() for item in wanted],
            }
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if args.json:
            _emit({"success": True, "path": str(target)})
            return EXIT_OK
        print(f"Wrote {target}")
        return EXIT_OK

    raise EditingError("Unknown episode command")


def cmd_transcribe(args) -> int:
    """Produce transcripts locally with faster-whisper.

    Six subcommands. Nothing here touches Premiere, and nothing leaves the
    machine -- the model runs locally through CTranslate2.
    """
    pipeline = _pipeline(args)
    command = args.transcribe_command

    settings = pipeline.transcription_config(
        backend=getattr(args, "backend_name", None),
        model=getattr(args, "model_size", None),
        device=getattr(args, "device", None),
        compute_type=getattr(args, "compute_type", None),
        language=getattr(args, "language", None),
        beam_size=getattr(args, "beam_size", None),
        vad_filter=(False if getattr(args, "no_vad", False) else None),
        word_timestamps=_word_timestamps_from(args),
        initial_prompt=getattr(args, "prompt", None),
        use_cache=(False if getattr(args, "no_cache", False) else None),
    )

    if command == "status":
        health = pipeline.transcribe_status(settings)
        jobs = pipeline.transcription_jobs(limit=args.limit)
        if args.json:
            _emit({
                "success": True,
                "health": health,
                "settings": settings.to_dict(),
                "jobs": [job.to_dict() for job in jobs],
            })
            return EXIT_OK
        print(_render_transcribe_status(health, settings, jobs))
        return EXIT_OK

    if command == "file":
        job = pipeline.transcribe_file(
            args.path,
            settings=settings,
            force=getattr(args, "force", False),
            extract_audio=getattr(args, "extract_audio", False),
            publish=not getattr(args, "no_publish", False),
        )
        if args.json:
            _emit({"success": True, **job.to_dict()})
            return EXIT_OK
        if job.result is not None:
            print(transcribe_formats.render_report(job.result, limit=args.limit))
            print()
        print(f"Job {job.job_id} [{job.status}] -> {job.output_dir}")
        return EXIT_OK

    if command == "folder":
        batch = pipeline.transcribe_folder(
            args.path,
            settings=settings,
            recursive=not getattr(args, "no_recursive", False),
            force=getattr(args, "force", False),
            extract_audio=getattr(args, "extract_audio", False),
            skip_existing=not getattr(args, "redo_existing", False),
            publish=not getattr(args, "no_publish", False),
            limit=getattr(args, "max_files", 0) or 0,
        )
        if args.json:
            _emit({"success": True, **batch.to_dict()})
            return EXIT_OK
        print(_render_batch(batch, limit=args.limit))
        # A batch with failures still exits 0: the useful outcome is the files
        # that worked, and the summary names the ones that did not.
        return EXIT_OK

    if command == "show":
        job = pipeline.transcription_job(args.job_id)
        result = pipeline.transcription_result(args.job_id)
        if args.json:
            _emit({"success": True, "job": job.to_dict(),
                   **result.to_dict()})
            return EXIT_OK
        print(transcribe_formats.render_report(result, limit=args.limit))
        return EXIT_OK

    if command == "export":
        target = pipeline.export_transcription(
            args.job_id, args.out, fmt=args.format)
        if args.json:
            _emit({"success": True, "path": str(target),
                   "format": args.format})
            return EXIT_OK
        print(f"Wrote {target}")
        return EXIT_OK

    if command == "clear-cache":
        if not getattr(args, "yes", False):
            raise EditingError(
                "Clearing the transcription cache needs --yes",
                hint="Every cached transcript would have to be produced again, "
                     "which on a folder of episodes is hours. Re-run with "
                     "`transcribe clear-cache --yes` if that is what you want.",
            )
        removed = pipeline.clear_transcription_cache()
        if args.json:
            _emit({"success": True, "removed": removed})
            return EXIT_OK
        print(f"Removed {removed} cached transcription(s).")
        print("Durable transcripts in transcripts/<asset_id>.json are "
              "untouched.")
        return EXIT_OK

    raise EditingError("Unknown transcribe command")


def _word_timestamps_from(args):
    """``--word-timestamps`` / ``--no-word-timestamps``, or leave the default."""
    if getattr(args, "no_word_timestamps", False):
        return False
    if getattr(args, "word_timestamps", False):
        return True
    return None


def _render_transcribe_status(health: dict, settings, jobs) -> str:
    lines = ["=" * 78, "TRANSCRIPTION", "=" * 78, ""]
    ready = health.get("ready")
    lines.append(f"  backend   : {health.get('backend')} "
                 f"{'[ready]' if ready else '[NOT INSTALLED]'}")
    lines.append(f"  model     : {settings.model}")
    lines.append(f"  device    : {settings.device} "
                 f"-> {health.get('resolved_device', '?')}"
                 f"   (cuda available: {health.get('cuda')})")
    lines.append(f"  language  : {settings.language or 'auto-detect'}")
    lines.append(f"  words     : {settings.word_timestamps}"
                 f"   vad: {settings.vad_filter}")
    lines.append("")
    if not ready:
        lines.append("  This cannot run yet. To install:")
        lines.append(f"    {health.get('hint') or transcribe_schema.INSTALL_HINT}")
        lines.append("")
    for warning in health.get("config_warnings", []):
        lines.append(f"  ! {warning}")
    if health.get("config_warnings"):
        lines.append("")

    lines.append("-" * 78)
    lines.append(f"JOBS ({len(jobs)})")
    lines.append("-" * 78)
    if not jobs:
        lines.append("  Nothing transcribed yet. Try:")
        lines.append("    python -m editing.cli transcribe file <clip.mp4>")
    for job in jobs:
        lines.append(f"  {job.job_id[:34]:<34} {job.line()}")
    return "\n".join(lines)


def _render_batch(batch, *, limit: int = 40) -> str:
    stats = batch.stats()
    lines = ["=" * 78, f"TRANSCRIBED -- {batch.root}", "=" * 78, ""]
    lines.append(f"  {stats['files']} file(s): {stats['done']} done, "
                 f"{stats['cached']} from cache, {stats['skipped']} skipped, "
                 f"{stats['failed']} failed")
    lines.append(f"  {stats['segments']} segment(s), {stats['words']} word(s) "
                 f"across {stats['media_seconds']:.0f}s of media")
    lines.append(f"  took {stats['elapsed']:.0f}s")
    lines.append("")
    lines.append("-" * 78)
    for job in batch.jobs[:limit]:
        lines.append(f"  {job.line()}")
    if len(batch.jobs) > limit:
        lines.append(f"  ... {len(batch.jobs) - limit} more")

    if batch.failed:
        lines.append("")
        lines.append("-" * 78)
        lines.append(f"FAILED ({len(batch.failed)})")
        lines.append("-" * 78)
        for job in batch.failed:
            if job.failure is None:
                continue
            lines.append(f"  {Path(job.source_path).name}")
            lines.append(f"    why : {job.failure.message}")
            if job.failure.hint:
                lines.append(f"    fix : {job.failure.hint}")
    for warning in batch.warnings:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)


def cmd_retention(args) -> int:
    """Wire the retention findings into the cut.

    Eight subcommands. None touches Premiere, executes anything, or modifies
    the cut it reads -- a retention cut is written as a variant under its own
    name.
    """
    pipeline = _run_scoped_pipeline(args)
    command = args.retention_command
    name = getattr(args, "name", "structure") or "structure"

    settings = pipeline.retention_config(
        mode=getattr(args, "mode", None),
        cold_open=(False if getattr(args, "no_cold_open", False) else None),
        max_cold_open_seconds=getattr(args, "max_cold_open_seconds", None),
        min_cold_open_seconds=getattr(args, "min_cold_open_seconds", None),
        duplicate_policy=getattr(args, "duplicate_policy", None),
        allow_duplicate_footage=(
            True if getattr(args, "allow_duplicates", False) else None),
        compress_sag=(
            False if getattr(args, "no_compress", False) else None),
        grind_speed=getattr(args, "grind_speed", None),
        dead_air_aggressiveness=getattr(args, "dead_air", None),
        max_ordinary_silence=getattr(args, "max_silence", None),
        kill_dead_air=(
            False if getattr(args, "keep_dead_air", False) else None),
        protect_setups=(
            False if getattr(args, "no_protect", False) else None),
        max_compression_share=getattr(args, "max_compression", None),
        target_duration=getattr(args, "target", None),
        max_duration=getattr(args, "max_duration", None),
        style=getattr(args, "style", None),
    )

    if command == "plan":
        plan, cut = pipeline.retention_cut(
            name=name, settings=settings, options=_roughcut_options(args))
        if args.json:
            _emit({
                "success": plan.ok,
                "applied": plan.applied,
                "cut": cut.to_dict() if cut is not None else None,
                **plan.to_dict(),
            })
        else:
            print(retention_report.render(plan))
        return EXIT_OK if plan.ok else EXIT_ERROR

    if command == "report":
        plan = pipeline.load_retention_cut_plan(name=name)
        if args.json:
            _emit({"success": plan.ok, **plan.to_dict()})
            return EXIT_OK
        print(retention_report.render(plan))
        return EXIT_OK

    if command == "show-cold-open":
        plan = pipeline.load_retention_cut_plan(name=name)
        if args.json:
            _emit({"success": True, **plan.cold_open.to_dict()})
            return EXIT_OK
        print(retention_report.render_cold_open(plan))
        return EXIT_OK

    if command == "show-compression":
        plan = pipeline.load_retention_cut_plan(name=name)
        if args.json:
            _emit({"success": True, **plan.sag.to_dict()})
            return EXIT_OK
        print(retention_report.render_compression(plan, limit=args.limit))
        return EXIT_OK

    if command == "show-protected":
        plan = pipeline.load_retention_cut_plan(name=name)
        if args.json:
            _emit({
                "success": True,
                "setups": [item.to_dict() for item in plan.setups],
                "payoffs": [item.to_dict() for item in plan.payoffs],
                "unresolved": plan.unresolved_warnings,
            })
            return EXIT_OK
        print(retention_report.render_protected(plan))
        return EXIT_OK

    if command == "show-rejected":
        plan = pipeline.load_retention_cut_plan(name=name)
        if args.json:
            _emit({
                "success": True,
                "count": len(plan.rejected),
                "rejected": [item.to_dict() for item in plan.rejected],
            })
            return EXIT_OK
        print(retention_report.render_rejected(plan, limit=args.limit))
        return EXIT_OK

    if command == "compare":
        comparison = pipeline.compare_retention(
            name=name, options=_roughcut_options(args))
        if args.json:
            _emit({"success": True, **comparison.to_dict()})
            return EXIT_OK
        print(retention_compare.render(comparison))
        return EXIT_OK

    if command == "render":
        # The retention cut, through the Session 10B renderer. Two verbs on
        # purpose: reshaping the cut and rendering it are separate decisions,
        # and you should be able to re-render without re-deciding.
        cut = pipeline.retention_roughcut_or_none(name=name)
        if cut is None:
            raise EditingError(
                f"There is no retention cut for '{name}' to render",
                hint="Build one with `python -m editing.cli retention plan "
                     "--mode retention`. In report-only mode nothing is "
                     "written, because nothing changed.",
            )
        render_settings = pipeline.render_config(
            quality=getattr(args, "quality", None),
            height=getattr(args, "height", None),
            backend=("mock" if getattr(args, "mock", False) else None),
        )
        job = pipeline.render_roughcut(
            name=name, plan=cut, settings=render_settings,
            force=getattr(args, "force", False),
        )
        if args.json:
            _emit({"success": job.ok, "clips": len(cut.placements),
                   **job.to_dict()})
        else:
            print(render_report.render_text(job))
        return EXIT_OK if job.ok else EXIT_ERROR

    raise EditingError("Unknown retention command")


def cmd_director(args) -> int:
    """A model reads the whole episode and decides what the cut is.

    Ten subcommands. None of them touches Premiere or executes anything: the
    pass produces decisions, deterministic rules check them, and the rough cut
    builder turns what survives into ranges.
    """
    pipeline = _run_scoped_pipeline(args)
    command = args.director_command
    name = getattr(args, "name", "structure") or "structure"

    settings = pipeline.director_config(
        backend=getattr(args, "backend_name", None),
        model=getattr(args, "model_name", None),
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
        temperature=getattr(args, "temperature", None),
        mode=getattr(args, "mode", None),
        style=getattr(args, "style", None),
        target_duration=getattr(args, "target", None),
        max_duration=getattr(args, "max_duration", None),
        max_segments=getattr(args, "max_segments", None),
        max_context_chars=getattr(args, "context_chars", None),
        max_output_tokens=getattr(args, "max_tokens", None),
        use_cache=(False if getattr(args, "no_cache", False) else None),
    )

    if command == "status":
        health = pipeline.director_status(settings)
        if args.json:
            _emit({"success": True, "health": health,
                   "settings": settings.to_dict()})
            return EXIT_OK
        print(_render_director_status(health, settings))
        return EXIT_OK

    if command == "show-style":
        guide = pipeline.style_guide(getattr(args, "style_guide", "") or "")
        if args.json:
            _emit({"success": True, **guide.to_dict()})
            return EXIT_OK
        print(director_style_guide.describe(guide))
        return EXIT_OK

    if command == "clear-cache":
        if not getattr(args, "yes", False):
            raise EditingError(
                "Clearing the director cache needs --yes",
                hint="Every cached answer would have to be asked for again, "
                     "which costs a model call per episode. Re-run with "
                     "`director clear-cache --yes` if that is what you want.",
            )
        removed = pipeline.clear_director_cache()
        if args.json:
            _emit({"success": True, "removed": removed})
            return EXIT_OK
        print(f"Removed {removed} cached director answer(s).")
        return EXIT_OK

    if command == "build-context":
        context = pipeline.director_context(
            name=name, settings=settings,
            style_guide_path=getattr(args, "style_guide", "") or "",
        )
        if args.json:
            _emit({"success": True, **context.to_dict()})
            return EXIT_OK
        print(director_report.render_context_summary(context))
        if getattr(args, "show_prompt", False):
            from editing.director import prompt as director_prompt
            print(director_prompt.build(context, settings).user)
        else:
            print(f"  Written to "
                  f"{director_store.context_path(pipeline.config, name)}")
            print("  Add --show-prompt to print what the model would be sent.")
        return EXIT_OK

    if command == "plan":
        plan = pipeline.director_plan(
            name=name, settings=settings,
            style_guide_path=getattr(args, "style_guide", "") or "",
            force=getattr(args, "force", False),
        )
        if args.json:
            _emit({"success": plan.ok, **plan.to_dict()})
        else:
            print(director_report.render(plan))
        return EXIT_OK if plan.ok else EXIT_ERROR

    if command == "report":
        plan = pipeline.load_director_plan(name=name)
        if args.json:
            _emit({"success": plan.ok, **plan.to_dict()})
            return EXIT_OK
        print(director_report.render(plan))
        return EXIT_OK

    if command in ("show-decisions", "show-rejected"):
        plan = pipeline.load_director_plan(name=name)
        rejected = command == "show-rejected"
        pool = plan.rejected if rejected else plan.accepted
        if args.json:
            _emit({"success": True, "count": len(pool),
                   "decisions": [entry.to_dict() for entry in pool]})
            return EXIT_OK
        print(director_report.render_decisions(
            plan, limit=args.limit,
            action=getattr(args, "action", "") or "",
            rejected=rejected,
        ))
        return EXIT_OK

    if command == "compare-heuristic":
        payload = pipeline.compare_director(
            name=name, options=_roughcut_options(args))
        if args.json:
            _emit({"success": True, **payload})
            return EXIT_OK
        print(director_compare.render(payload))
        return EXIT_OK

    if command == "render":
        # Build the cut from the director plan, then hand it to the Session
        # 10B renderer. Two separate verbs on purpose: the plan is a set of
        # decisions and the render is a video, and conflating them would mean
        # you could not re-render without re-deciding.
        options = _roughcut_options(args)
        options.mode = getattr(args, "mode", None) or "director"
        cut = pipeline.rough_cut(
            name=name, options=options, validate=False,
            director_plan=pipeline.director_plan_or_none(name=name),
        )
        render_settings = pipeline.render_config(
            quality=getattr(args, "quality", None),
            height=getattr(args, "height", None),
            backend=("mock" if getattr(args, "mock", False) else None),
        )
        job = pipeline.render_roughcut(
            name=name, plan=cut, settings=render_settings,
            force=getattr(args, "force", False),
        )
        if args.json:
            _emit({"success": job.ok, "mode": options.mode,
                   "clips": len(cut.placements), **job.to_dict()})
        else:
            print(render_report.render_text(job))
        return EXIT_OK if job.ok else EXIT_ERROR

    raise EditingError("Unknown director command")


def _render_director_status(health: dict, settings) -> str:
    lines = ["=" * 78, "DIRECTOR PASS", "=" * 78, ""]
    ready = health.get("ready")
    lines.append(f"  backend   : {health.get('backend')} "
                 f"{'[ready]' if ready else '[NOT REACHABLE]'}")
    lines.append(f"  model     : {settings.model}")
    lines.append(f"  endpoint  : {settings.base_url}")
    lines.append(f"  mode      : {settings.mode}")
    lines.append(f"  context   : up to {settings.max_segments} range(s), "
                 f"{settings.max_context_chars} characters")
    if settings.target_duration or settings.max_duration:
        lines.append(f"  runtime   : target {settings.target_duration:.0f}s, "
                     f"max {settings.max_duration:.0f}s")
    lines.append("")
    if not ready:
        lines.append("  A director pass cannot run yet.")
        if health.get("error"):
            lines.append(f"    error: {health['error']}")
        if health.get("hint"):
            for line in str(health["hint"]).split(". "):
                if line.strip():
                    lines.append(f"    {line.strip()}")
        lines.append("")
    if health.get("warning"):
        lines.append(f"  ! {health['warning']}")
        lines.append("")
    for warning in health.get("config_warnings", []):
        lines.append(f"  ! {warning}")
    if health.get("config_warnings"):
        lines.append("")
    lines.append("  Any OpenAI-compatible endpoint works: vLLM, LM Studio,")
    lines.append("  llama.cpp-server, or a hosted API. Set")
    lines.append("  EDITING_DIRECTOR_BASE_URL, EDITING_DIRECTOR_MODEL and")
    lines.append("  EDITING_DIRECTOR_API_KEY, or pass --base-url/--model.")
    return "\n".join(lines)


def cmd_render(args) -> int:
    """Render a rough cut to a watchable proxy with FFmpeg.

    Eight subcommands. None of them touches Premiere, executes an operation,
    or writes anything outside ``data/editing/render/``.
    """
    pipeline = _run_scoped_pipeline(args)
    command = args.render_command

    settings = pipeline.render_config(
        backend=("mock" if getattr(args, "mock", False) else None),
        quality=getattr(args, "quality", None),
        height=getattr(args, "height", None),
        fps=getattr(args, "fps", None),
        video_encoder=getattr(args, "encoder", None),
        crf=getattr(args, "crf", None),
        scale_mode=getattr(args, "scale_mode", None),
        include_audio=(False if getattr(args, "no_audio", False) else None),
        max_seconds=getattr(args, "max_seconds", None),
        max_segments=getattr(args, "max_segments", None),
        keep_temp=(True if getattr(args, "keep_temp", False) else None),
        use_cache=(False if getattr(args, "no_cache", False) else None),
        notes_interval=getattr(args, "notes_interval", None),
    )

    if command == "status":
        health = pipeline.render_status(settings)
        if args.json:
            _emit({"success": True, "health": health,
                   "settings": settings.to_dict()})
            return EXIT_OK
        print(_render_render_status(health, settings))
        return EXIT_OK

    if command in ("roughcut", "from-plan"):
        if command == "from-plan":
            job = pipeline.render_plan_file(
                args.path, settings=settings,
                force=getattr(args, "force", False),
                dry_run=getattr(args, "dry_run", False),
            )
        elif getattr(args, "plan", ""):
            job = pipeline.render_plan_file(
                args.plan, settings=settings,
                force=getattr(args, "force", False),
                dry_run=getattr(args, "dry_run", False),
            )
        else:
            job = pipeline.render_roughcut(
                name=args.name, settings=settings,
                force=getattr(args, "force", False),
                dry_run=getattr(args, "dry_run", False),
            )
        if args.json:
            _emit({"success": job.ok, **job.to_dict()})
        else:
            print(render_report.render_text(job))
        # A failed render exits non-zero: it is the one command here that can
        # be part of a script, and "there is no video" must be detectable
        # without parsing the report.
        return EXIT_OK if job.ok else EXIT_ERROR

    if command == "list":
        jobs = pipeline.render_jobs(limit=args.limit)
        if args.json:
            _emit({"success": True, "count": len(jobs),
                   "usage": render_store.usage(pipeline.config),
                   "jobs": [job.to_dict() for job in jobs]})
            return EXIT_OK
        print(render_report.render_job_list(jobs, limit=args.limit))
        return EXIT_OK

    if command == "show":
        job = pipeline.render_job(getattr(args, "job_id", "") or "")
        if args.json:
            _emit({"success": job.ok, **job.to_dict()})
            return EXIT_OK
        print(render_report.render_text(job))
        return EXIT_OK

    if command == "report":
        job, report = pipeline.render_report(
            getattr(args, "job_id", "") or "",
            save=not getattr(args, "no_save", False),
        )
        if args.json:
            _emit({"success": True, **report.to_dict()})
            return EXIT_OK
        print(render_report.render_markdown(job, report))
        return EXIT_OK

    if command == "notes":
        target = pipeline.render_notes(getattr(args, "job_id", "") or "")
        if args.json:
            _emit({"success": True, "path": str(target)})
            return EXIT_OK
        print(f"Wrote {target}")
        return EXIT_OK

    if command == "open":
        job = pipeline.render_job(getattr(args, "job_id", "") or "")
        wants_notes = getattr(args, "notes", False)
        if not wants_notes and not job.rendered:
            # A mocked or planned job has a file where the video would be, and
            # handing it to a player would be the one thing this package
            # promises never to do: present something that is not a render as
            # though it were one.
            raise EditingError(
                f"Render '{job.job_id}' produced no video to open "
                f"(status: {job.status})",
                hint=("The mock backend writes a placeholder, not a video. "
                      "Re-run without --mock."
                      if job.status == "mocked" else
                      f"`render show {job.job_id}` says what happened. "
                      f"`render open {job.job_id} --notes` opens the review "
                      "notes, which exist either way."),
                detail={"status": job.status, "path": job.output_path},
            )
        target = Path(job.notes_path if wants_notes else job.output_path)
        if not target.exists():
            raise EditingError(
                f"Render '{job.job_id}' has no file to open at {target}",
                hint="It may have failed, or been rendered with --dry-run. "
                     f"`render show {job.job_id}` says which.",
                detail={"path": str(target), "status": job.status},
            )
        opened = _open_file(target)
        if args.json:
            _emit({"success": True, "path": str(target), "opened": opened})
            return EXIT_OK
        print(str(target) if opened
              else f"Could not open it here. The file is at:\n  {target}")
        return EXIT_OK

    if command == "clean":
        if not getattr(args, "yes", False):
            raise EditingError(
                "Deleting renders needs --yes",
                hint="Re-run with `render clean --yes`. Add --temp-only to "
                     "drop just the per-clip intermediates and keep every "
                     "video.",
            )
        result = pipeline.clean_renders(
            job_id=getattr(args, "job", "") or "",
            temp_only=getattr(args, "temp_only", False),
            keep_latest=getattr(args, "keep_latest", 0) or 0,
        )
        if args.json:
            _emit({"success": True, **result})
            return EXIT_OK
        freed = result["freed_bytes"] / (1024 * 1024)
        what = "intermediate(s)" if result["temp_only"] else "render(s)"
        print(f"Cleaned {len(result['removed'])} {what}, freeing "
              f"{freed:.0f} MB.")
        return EXIT_OK

    raise EditingError("Unknown render command")


def _open_file(path: Path) -> bool:
    """Hand a file to whatever the desktop uses. False when there is none.

    Deliberately best-effort: on a headless machine or over SSH there is no
    player, and printing the path is a better outcome than an exception.
    """
    import subprocess
    import sys as _sys

    try:
        if _sys.platform.startswith("win"):
            import os
            os.startfile(str(path))  # noqa: S606 - a file this tool wrote
            return True
        opener = "open" if _sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(  # noqa: S603
            [opener, str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001 - no desktop is a normal condition
        return False


def _render_render_status(health: dict, settings) -> str:
    lines = ["=" * 78, "PROXY RENDERING", "=" * 78, ""]
    ready = health.get("ready")
    lines.append(f"  backend   : {health.get('backend')} "
                 f"{'[ready]' if ready else '[FFMPEG NOT FOUND]'}")
    lines.append(f"  version   : {health.get('version') or '(unknown)'}")
    lines.append(f"  quality   : {settings.quality} "
                 f"(crf {settings.crf}, preset {settings.preset})")
    lines.append(f"  output    : {settings.width}x{settings.height} @ "
                 f"{settings.fps:g}fps, {settings.resolved_encoder}")
    lines.append(f"  audio     : "
                 f"{'on' if settings.include_audio else 'OFF'}"
                 f"   ({settings.audio_encoder} {settings.audio_bitrate})")
    lines.append("")
    if not ready:
        lines.append("  Rendering cannot run yet. To fix:")
        lines.append(f"    {health.get('hint') or render_schema.INSTALL_HINT}")
        lines.append("")
    for warning in health.get("config_warnings", []):
        lines.append(f"  ! {warning}")
    if health.get("config_warnings"):
        lines.append("")
    lines.append("-" * 78)
    lines.append("ON DISK")
    lines.append("-" * 78)
    lines.append(f"  {health.get('jobs', 0)} render(s) in {health.get('root')}")
    lines.append(f"  {health.get('total_bytes', 0) / (1024 * 1024):.0f} MB "
                 f"total, of which "
                 f"{health.get('temp_bytes', 0) / (1024 * 1024):.0f} MB is "
                 "intermediates")
    if health.get("temp_bytes"):
        lines.append("  Drop the intermediates with "
                     "`render clean --temp-only --yes`.")
    return "\n".join(lines)


def cmd_feedback(args) -> int:
    """Collect structured human review of an edit.

    Ten subcommands, none of which touches Premiere, trains anything, or
    rewrites a single line of an existing log. Everything that records
    something appends; everything else reads or derives.
    """
    pipeline = _run_scoped_pipeline(args)
    command = args.feedback_command
    name = getattr(args, "name", "structure")
    session_id = getattr(args, "session", "") or ""

    if command == "start":
        session, queue = pipeline.feedback_start(
            name=name,
            run_id=getattr(args, "run", "") or "",
            session_id=getattr(args, "id", "") or "",
            title=getattr(args, "title", "") or "",
            notes=getattr(args, "notes", "") or "",
            limit=args.limit,
            build_queue=not getattr(args, "no_queue", False),
            force=getattr(args, "force", False),
        )
        if args.json:
            _emit({
                "success": True,
                "session": session.to_dict(),
                "queue": queue.to_dict() if queue else None,
            })
            return EXIT_OK
        print(f"Feedback session: {session.session_id}")
        print(f"  folder: "
              f"{feedback_store.session_dir(pipeline.config, session.session_id)}")
        if queue is not None:
            print()
            print(feedback_report.render_queue(queue, limit=args.limit))
            print()
            print("Rate the first one with:")
            first = queue.prompts[0] if queue.prompts else None
            if first is not None:
                print(f"  python -m editing.cli feedback rate "
                      f"{first.prompt_id} good --reason {first.category}")
        return EXIT_OK

    if command == "queue":
        session = pipeline.feedback_session(
            session_id, run_id=getattr(args, "run", "") or "")
        saved = feedback_store.queue_or_none(
            pipeline.config, session.session_id)
        # A different --limit means a different *queue*, not a different slice
        # of one: the selection is what makes a short queue worth reading, so
        # asking for twelve questions has to re-select rather than print twelve
        # of the twenty that were chosen for a bigger budget.
        wants_rebuild = (
            getattr(args, "regenerate", False)
            or saved is None
            or saved.limit != args.limit
            or bool(_split(getattr(args, "category", "")))
            or bool(_split(getattr(args, "source", "")))
            or getattr(args, "no_positive", False)
        )
        if wants_rebuild:
            queue = pipeline.feedback_queue(
                session=session,
                limit=args.limit,
                categories=_split(getattr(args, "category", "")),
                sources=_split(getattr(args, "source", "")),
                include_positive=not getattr(args, "no_positive", False),
            )
        else:
            queue = saved
            feedback_collect.mark_answered(
                queue, pipeline.feedback_items(session))
        if getattr(args, "unanswered", False):
            queue.prompts = [p for p in queue.prompts if not p.answered]
        if args.json:
            _emit({"success": True, **queue.to_dict()})
            return EXIT_OK
        print(feedback_report.render_queue(queue, limit=args.limit))
        return EXIT_OK

    if command == "show":
        session = pipeline.feedback_session(session_id)
        history = feedback_store.read_all(pipeline.config, session.session_id)
        prompt = feedback_store.find_prompt(
            pipeline.config, session.session_id, args.id)
        if prompt is not None:
            about = [
                item for item in history
                if item.prompt_id == prompt.prompt_id
                or item.target.key() == prompt.target.key()
            ]
            if args.json:
                _emit({
                    "success": True,
                    "prompt": prompt.to_dict(),
                    "feedback": [item.to_dict() for item in about],
                })
                return EXIT_OK
            print(feedback_report.render_prompt(prompt, history=about))
            return EXIT_OK

        item = feedback_store.find_item(history, args.id)
        if item is None:
            raise EditingError(
                f"Nothing in session '{session.session_id}' is called "
                f"'{args.id}'",
                hint="Pass a prompt ID from `feedback queue` or a feedback ID "
                     "from `feedback list`.",
            )
        related = feedback_store.history_of(history, item.target.key())
        if args.json:
            _emit({
                "success": True,
                "feedback": item.to_dict(),
                "history": [entry.to_dict() for entry in related],
            })
            return EXIT_OK
        print(feedback_report.render_item(item, history=related))
        return EXIT_OK

    if command in ("rate", "note", "correct"):
        session = pipeline.feedback_session(
            session_id, run_id=getattr(args, "run", "") or "")
        artifacts = pipeline.feedback_artifacts(
            name=session.name or name, run_id=session.run_id,
            style=session.style,
        )
        strict = not getattr(args, "allow_unknown", False)

        if command == "rate":
            item = feedback_collect.rate(
                pipeline.config, session, artifacts, args.id, args.rating,
                reasons=_split(getattr(args, "reason", "")),
                note=getattr(args, "note", "") or "",
                correction=getattr(args, "correction", "") or "",
                correction_action=getattr(args, "action", "") or "",
                correction_seconds=getattr(args, "seconds", None),
                priority=getattr(args, "priority", None),
                confidence=getattr(args, "confidence", None),
                target_type=getattr(args, "target_type", "") or "",
                usable_for_training=(
                    False if getattr(args, "no_training", False) else None),
                needs_follow_up=getattr(args, "follow_up", False),
                strict=strict,
            )
        elif command == "note":
            item = feedback_collect.add_note(
                pipeline.config, session, artifacts, args.id, args.text,
                reasons=_split(getattr(args, "reason", "")),
                target_type=getattr(args, "target_type", "") or "",
                strict=strict,
            )
        else:
            item = feedback_collect.add_correction(
                pipeline.config, session, artifacts, args.id, args.text,
                action=getattr(args, "action", "") or "",
                seconds=getattr(args, "seconds", None),
                start=getattr(args, "start", 0.0) or 0.0,
                end=getattr(args, "end", 0.0) or 0.0,
                target_type=getattr(args, "target_type", "") or "",
                strict=strict,
            )

        feedback_collect.refresh_counts(pipeline.config, session)
        if args.json:
            _emit({"success": True, **item.to_dict()})
            return EXIT_OK
        print(f"Recorded {item.feedback_id}")
        print(f"  {item.summary}")
        if item.supersedes:
            print(f"  replaces {item.supersedes} (both stay in the log)")
        if not item.usable_for_training:
            print(f"  not training material: {item.training_note}")
        if item.needs_follow_up:
            print(f"  needs follow-up: {item.follow_up_note}")
        return EXIT_OK

    if command == "list":
        if getattr(args, "sessions", False):
            sessions = feedback_store.list_sessions(
                pipeline.config, limit=args.limit)
            if args.json:
                _emit({
                    "success": True,
                    "sessions": [item.to_dict() for item in sessions],
                })
                return EXIT_OK
            print(feedback_report.render_sessions(sessions))
            return EXIT_OK

        session = pipeline.feedback_session(session_id)
        items = pipeline.feedback_items(
            session, current_only=not getattr(args, "history", False))
        items = feedback_collect.filtered(
            items,
            ratings=_split(getattr(args, "rating", "")),
            categories=_split(getattr(args, "category", "")),
            target_types=_split(getattr(args, "target_type", "")),
            needs_follow_up=getattr(args, "follow_up", False),
            training_only=getattr(args, "training_only", False),
        )
        if args.json:
            _emit({
                "success": True,
                "session_id": session.session_id,
                "feedback": [item.to_dict() for item in items],
            })
            return EXIT_OK
        print(feedback_report.render_list(items, limit=args.limit))
        return EXIT_OK

    if command == "report":
        session = pipeline.feedback_session(
            session_id, run_id=getattr(args, "run", "") or "")
        summary = pipeline.feedback_summary(
            session, save=not getattr(args, "no_save", False))
        if args.json:
            _emit({"success": True, **summary})
            return EXIT_OK
        print(feedback_report.render_report(summary, limit=args.limit))
        if not getattr(args, "no_save", False):
            _note(f"Wrote "
                  f"{feedback_store.report_path(pipeline.config, session.session_id)}")
        return EXIT_OK

    if command == "stats":
        session = pipeline.feedback_session(session_id)
        summary = pipeline.feedback_summary(session, save=False)
        if args.json:
            _emit({
                "success": True,
                "session_id": session.session_id,
                "counts": summary.get("counts", {}),
                "coverage": summary.get("coverage", {}),
                "preferences": {
                    key: value
                    for key, value in summary.get("preferences", {}).items()
                    if key != "items"
                },
                "training": {
                    key: value
                    for key, value in summary.get("training", {}).items()
                    if key != "items"
                },
                "basis": feedback_schema.NOT_MEASURED,
            })
            return EXIT_OK
        print(feedback_report.render_stats(summary))
        if getattr(args, "preferences", False):
            print()
            signals, _training = pipeline.feedback_signals(session)
            print(feedback_report.render_preferences(signals, limit=args.limit))
        return EXIT_OK

    if command == "export":
        session = pipeline.feedback_session(
            session_id, run_id=getattr(args, "run", "") or "")
        parts = _split(getattr(args, "include", "")) or [
            "feedback", "preferences", "training"]
        target, record = pipeline.feedback_export(
            session,
            parts=parts,
            fmt=args.format,
            out=getattr(args, "out", "") or None,
            current_only=not getattr(args, "history", False),
            training_only=getattr(args, "training_only", False),
        )
        if args.json:
            _emit({"success": True, "path": str(target), **record.to_dict()})
            return EXIT_OK
        print(f"Wrote {target}")
        print(f"  {record.total_rows} row(s): "
              + ", ".join(f"{key} {value}"
                          for key, value in record.counts.items() if value))
        print(f"  checksum {record.checksum}")
        if record.notes:
            print(f"  note: {record.notes}")
        return EXIT_OK

    raise EditingError("Unknown feedback command")


def _split(value) -> list:
    """``--reason pacing,story`` or a repeated flag, as a flat list."""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for item in items:
        for piece in str(item).replace(";", ",").split(","):
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out


def _retention_or_none(pipeline: Pipeline, name: str):
    try:
        return pipeline.load_retention_plan(name=name)
    except EditingError:
        return None


def cmd_timeline(args) -> int:
    pipeline = _pipeline(args)
    assets = _assets_for(pipeline, args)

    timeline = pipeline.timeline(
        assets,
        merge_similar=not args.no_merge,
        max_segment_seconds=args.max_segment_seconds,
        usable_threshold=args.threshold,
        refresh_transcripts=args.refresh_transcripts,
    )
    target = pipeline.write_timeline(timeline, name=args.name)

    if args.json:
        _emit({"success": True, "written": str(target), **timeline.to_dict()})
        return EXIT_OK

    _print_timeline(timeline, limit=args.limit)
    print(f"\nWritten to {target}")
    return EXIT_OK


def cmd_show(args) -> int:
    pipeline = _pipeline(args)
    timeline = pipeline.load_timeline(name=args.name)
    if args.json:
        _emit({"success": True, **timeline.to_dict()})
        return EXIT_OK
    _print_timeline(timeline, limit=args.limit, highlights_only=args.highlights)
    return EXIT_OK


def cmd_export(args) -> int:
    """Write a built artefact to a path of your choosing."""
    pipeline = _pipeline(args)
    target = Path(args.out).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)

    if args.what == "timeline":
        timeline = pipeline.load_timeline(name=args.name)
        target.write_text(
            json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary = {"segments": len(timeline.segments), "stats": timeline.stats()}
        human = f"Wrote {len(timeline.segments)} segment(s) to {target}"

    elif args.what == "recommendations":
        recommendations = pipeline.load_recommendations(name=args.name)
        target.write_text(
            json.dumps(recommendations.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary = {"recommendations": len(recommendations),
                   "stats": recommendations.stats()}
        human = f"Wrote {len(recommendations)} recommendation(s) to {target}"

    elif args.what == "report":
        recommendations = pipeline.load_recommendations(name=args.name)
        # The report is richer with the timeline and plan alongside it, but
        # both are optional -- exporting a report must not fail because the
        # draft plan has not been built.
        timeline = None
        draft = None
        try:
            timeline = pipeline.load_timeline(name=args.name)
        except EditingError:
            pass
        plan_path = pipeline.config.plans_dir / f"{args.name}.json"
        if plan_path.exists():
            from editing.recommend.premiere_plan import DraftPlan

            stored = json.loads(plan_path.read_text(encoding="utf-8"))
            draft = DraftPlan(
                ops=list(stored.get("plan", {}).get("ops") or []),
                not_convertible=list(stored.get("not_convertible") or []),
                no_op=list(stored.get("no_op") or []),
                valid=bool(stored.get("valid")),
                validation_error=stored.get("validation_error"),
                explanation=list(stored.get("explanation") or []),
                generated_at=str(stored.get("generated_at") or ""),
            )
        text = report_module.render(recommendations, timeline=timeline, draft=draft)
        target.write_text(text, encoding="utf-8")
        summary = {"recommendations": len(recommendations), "characters": len(text)}
        human = f"Wrote the report to {target}"

    else:   # plan
        plan_path = pipeline.config.plans_dir / f"{args.name}.json"
        if not plan_path.exists():
            raise EditingError(
                f"No draft plan named '{args.name}' has been built yet",
                hint="Run `python -m editing.cli draft` first.",
            )
        stored = json.loads(plan_path.read_text(encoding="utf-8"))
        target.write_text(
            json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary = {"operations": stored.get("operation_count", 0),
                   "valid": stored.get("valid"), "executed": stored.get("executed")}
        human = f"Wrote the draft plan to {target}"

    if args.json:
        _emit({"success": True, "what": args.what, "written": str(target), **summary})
    else:
        print(human)
    return EXIT_OK


def cmd_plan(args) -> int:
    """Show what an analysis would cost, without running it."""
    from editing.visual.sampling import plan_summary, plan_windows

    pipeline = _pipeline(args)
    assets = _assets_for(pipeline, args)

    plans = {}
    for asset in assets:
        windows = plan_windows(asset.duration, pipeline.sampling)
        plans[asset.filename] = plan_summary(windows, asset.duration)

    if args.json:
        _emit({
            "success": True,
            "sampling": pipeline.sampling.to_dict(),
            "plans": plans,
            "totals": {
                "windows": sum(plan["windows"] for plan in plans.values()),
                "frames": sum(plan["frames"] for plan in plans.values()),
            },
        })
        return EXIT_OK

    print("Sampling plan (before motion analysis, which adds dense windows):")
    for name, plan in plans.items():
        print(
            f"  {name}: {plan['windows']} window(s), {plan['frames']} frame(s), "
            f"~{plan['seconds_per_window']}s each"
        )
    print(
        f"\nTotal: {sum(p['windows'] for p in plans.values())} model call(s), "
        f"{sum(p['frames'] for p in plans.values())} frame(s)."
    )
    return EXIT_OK


def cmd_cache(args) -> int:
    pipeline = _pipeline(args)
    cache = pipeline.cache

    if args.cache_command == "clear":
        removed = cache.clear(args.kind)
        if args.json:
            _emit({"success": True, "removed": removed, "kind": args.kind or "all"})
        else:
            print(f"Removed {removed} cache entr{'y' if removed == 1 else 'ies'}.")
        return EXIT_OK

    info = cache.info()
    if args.json:
        _emit({"success": True, **info})
        return EXIT_OK
    print(f"Cache: {info['root']}")
    print(f"  {info['total_entries']} entries, {info['total_bytes'] / 1e6:.1f} MB")
    for kind, bucket in sorted(info["kinds"].items()):
        print(f"    {kind:<12} {bucket['entries']:>6} entries  "
              f"{bucket['bytes'] / 1e6:>7.1f} MB")
    return EXIT_OK


def cmd_doctor(args) -> int:
    """Check the three external things this layer depends on."""
    from editing import ffmpeg as ff

    pipeline = _pipeline(args)
    config = pipeline.config

    report = {
        "ffmpeg": {"path": config.ffmpeg, "found": ff.have_tool(config.ffmpeg)},
        "ffprobe": {"path": config.ffprobe, "found": ff.have_tool(config.ffprobe)},
        "vision": qwen.health(config),
        # ``--no-premiere`` means never talk to Premiere, and a probe is
        # talking to it. Reporting what a live host says under that flag made
        # the doctor's answer depend on whether the user happened to have
        # Premiere open, which is the opposite of what the flag promises.
        "premiere_transcript": (
            premiere_source.probe_support(pipeline.bridge).to_dict()
            if config.use_premiere else
            {"available": False, "readable": False,
             "note": "--no-premiere was set, so Premiere was not asked."}
        ),
        "output_dir": str(config.output_dir),
        "model": config.vision_model,
        "backend": config.vision_backend,
    }

    if args.json:
        _emit({"success": True, **report})
        return EXIT_OK

    print("FFmpeg")
    for tool in ("ffmpeg", "ffprobe"):
        state = "found" if report[tool]["found"] else "NOT FOUND"
        print(f"  {tool:<9} {state:<10} ({report[tool]['path']})")
    if not (report["ffmpeg"]["found"] and report["ffprobe"]["found"]):
        print("  Install FFmpeg and put it on PATH, or set EDITING_FFMPEG/"
              "EDITING_FFPROBE.")

    print(f"\nVision model ({config.vision_backend}: {config.vision_model})")
    vision = report["vision"]
    print(f"  reachable  {vision.get('reachable')}")
    if vision.get("error"):
        print(f"  error      {vision['error']}")
    if vision.get("hint"):
        print(f"  hint       {vision['hint']}")

    print("\nPremiere transcript access")
    support = report["premiere_transcript"]
    print(f"  reachable  {support['available']}")
    print(f"  readable   {support['readable']}")
    if support.get("note"):
        print(f"  note       {support['note']}")

    print(f"\nOutputs: {config.output_dir}")
    return EXIT_OK


def cmd_run(args) -> int:
    pipeline = _pipeline(args)
    if args.recommend:
        timeline, recommendations, draft = pipeline.run_full(
            planner_options=PlannerOptions(
                budget_seconds=args.budget_seconds,
                min_repeat_gap=args.repeat_gap,
                skip_safety=args.no_safety,
            ),
            folder=args.folder,
            files=args.file or None,
            recursive=not args.no_recursive,
            keep_frames=args.keep_frames,
            use_motion=not args.no_motion,
            max_windows=args.max_windows,
            use_premiere=(False if args.no_premiere else None),
            merge_similar=not args.no_merge,
            max_segment_seconds=args.max_segment_seconds,
            usable_threshold=args.threshold,
        )
        if args.json:
            _emit({
                "success": True,
                "timeline": timeline.to_dict(),
                "recommendations": recommendations.to_dict(),
                "draft_plan": draft.to_dict(),
            })
            return EXIT_OK
        print(report_module.render(
            recommendations, timeline=timeline, draft=draft, limit=args.limit
        ))
        return EXIT_OK

    timeline = pipeline.run(
        folder=args.folder,
        files=args.file or None,
        recursive=not args.no_recursive,
        keep_frames=args.keep_frames,
        use_motion=not args.no_motion,
        max_windows=args.max_windows,
        use_premiere=(False if args.no_premiere else None),
        merge_similar=not args.no_merge,
        max_segment_seconds=args.max_segment_seconds,
        usable_threshold=args.threshold,
    )
    target = pipeline.write_timeline(timeline, name=args.name)
    if args.json:
        _emit({"success": True, "written": str(target), **timeline.to_dict()})
        return EXIT_OK
    _print_timeline(timeline, limit=args.limit)
    print(f"\nWritten to {target}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def _print_timeline(
    timeline: StructureTimeline, *, limit: int = 60, highlights_only: bool = False
) -> None:
    stats = timeline.stats()
    print(
        f"{stats['segments']} segment(s) over {stats['assets']} file(s), "
        f"{stats['covered_seconds']:.0f}s covered, "
        f"{stats['usable_segments']} flagged usable."
    )
    if stats["by_importance"]:
        print("  importance: " + ", ".join(
            f"{name}={count}" for name, count in sorted(stats["by_importance"].items())
        ))
    if stats["by_alignment"]:
        print("  alignment : " + ", ".join(
            f"{name}={count}" for name, count in sorted(stats["by_alignment"].items())
        ))

    segments = timeline.highlights(limit) if highlights_only else timeline.segments
    shown = segments[:limit]
    if shown:
        label = "Highlights" if highlights_only else "Segments"
        print(f"\n{label} (showing {len(shown)} of {len(segments)}):")
        current_asset = None
        for segment in shown:
            if segment.asset_id != current_asset:
                current_asset = segment.asset_id
                print(f"\n  {Path(segment.source_file).name}")
            print(f"    {segment.summary()}")

    if timeline.warnings:
        print(f"\nWarnings ({len(timeline.warnings)}):")
        for warning in timeline.warnings[:12]:
            print(f"  ! {warning}")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _add_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--folder", help="footage folder to scan")
    parser.add_argument("--file", action="append", default=[],
                        help="a specific media file (repeatable)")
    parser.add_argument("--only", help="act only on files matching this name/id")
    parser.add_argument("--no-recursive", action="store_true",
                        help="do not descend into sub-folders")


def _add_sampling(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("sampling")
    group.add_argument("--window-seconds", type=float,
                       help="length of one analysis window (default 8)")
    group.add_argument("--window-overlap", type=float,
                       help="overlap between windows in seconds (default 0.5)")
    group.add_argument("--frames", type=int,
                       help="frames per normal window (default 3)")
    group.add_argument("--dense-frames", type=int,
                       help="frames per high-motion window (default 5)")
    group.add_argument("--dense-window-seconds", type=float,
                       help="window length inside high-motion stretches (default 4)")
    group.add_argument("--motion-threshold", type=float,
                       help="scene-change score that triggers dense sampling "
                            "(0-1, default 0.30)")
    group.add_argument("--max-windows-config", type=int, metavar="N",
                       help="ceiling on windows per file (default 400)")
    group.add_argument("--frame-width", type=int,
                       help="extracted frame width in pixels (default 768)")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true",
                        help="print one JSON object on stdout and nothing else")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="suppress progress output")
    parser.add_argument("--output-dir", help="where outputs are written "
                                             "(default data/editing)")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore and do not write the cache")
    parser.add_argument("--no-premiere", action="store_true",
                        help="never talk to Premiere")
    parser.add_argument("--verbose", action="store_true", help="debug logging")


def _add_review_export_args(parser: argparse.ArgumentParser) -> None:
    """Flags shared by ``review`` and ``review export-frames``.

    Defined once and applied to both so the bare ``review`` command keeps
    working exactly as Session 3 documented it.
    """
    parser.add_argument("--name", default="structure")
    parser.add_argument("--position", type=float, default=0.34,
                        help="where in each clip to sample in --simple mode, "
                             "0-1 (default 0.34)")
    parser.add_argument("--width", type=int, default=960,
                        help="exported frame width (default 960)")
    parser.add_argument("--list", action="store_true",
                        help="list what would be exported without extracting")
    parser.add_argument("--simple", action="store_true",
                        help="one representative frame per clip, with no "
                             "coverage rules and no context attached")
    parser.add_argument("--max-frames", type=int, default=120,
                        help="ceiling on frames exported (default 120)")
    group = parser.add_argument_group("coverage rules")
    group.add_argument("--no-cut-points", action="store_true")
    group.add_argument("--no-markers", action="store_true")
    group.add_argument("--no-zooms", action="store_true")
    group.add_argument("--no-speed", action="store_true")
    group.add_argument("--no-text", action="store_true")
    group.add_argument("--no-priority", action="store_true")
    group.add_argument("--no-sanity", action="store_true")
    parser.add_argument("--limit", type=int, default=40)


def _add_polish(parser: argparse.ArgumentParser) -> None:
    """Caption and audio polish flags, shared by ``auto run`` and ``batch``.

    Defined once so the two cannot drift: a batch that spelled ``--captions``
    differently from a single run would be a batch nobody could reproduce by
    hand.
    """
    from editing.polish import schema as polish_schema

    group = parser.add_argument_group("polish")
    group.add_argument(
        "--captions", default="off", choices=list(polish_schema.CAPTION_MODES),
        help="key_moments captions only the moments that carry the episode; "
             "dense is close to subtitles and is never a default")
    group.add_argument(
        "--max-captions-per-minute", dest="max_captions_per_minute",
        type=float, default=0.0,
        help="ceiling on captions a minute (0 = the style's own number)")
    group.add_argument(
        "--max-caption-seconds", dest="max_caption_seconds", type=float,
        default=0.0, help="longest a caption may stay up (0 = the style's)")
    group.add_argument(
        "--max-caption-words", dest="max_caption_words", type=int, default=0,
        help="words a caption may carry (0 = the style's own number)")
    group.add_argument(
        "--min-caption-confidence", dest="min_caption_confidence", type=float,
        default=0.0,
        help="ASR confidence a line needs to be captioned (0 = default 0.6)")
    group.add_argument(
        "--require-caption-confidence", dest="require_caption_confidence",
        action="store_true",
        help="refuse lines from a transcript that carries no confidence "
             "figures at all")
    group.add_argument(
        "--audio-polish", dest="audio_polish", default="off",
        choices=list(polish_schema.AUDIO_POLISH_MODES),
        help="placeholders marks where sound belongs and needs no library; "
             "assets matches against the local one and reports what is "
             "missing")
    group.add_argument(
        "--max-sfx-per-minute", dest="max_sfx_per_minute", type=float,
        default=0.0,
        help="ceiling on effects a minute (0 = the style's own number)")
    group.add_argument(
        "--no-music-bed", dest="no_music_bed", action="store_true",
        help="never lay a music or ambience bed")
    group.add_argument(
        "--no-ducking", dest="no_ducking", action="store_true",
        help="do not ask for the bed to duck under speech")


def _add_conform(parser: argparse.ArgumentParser) -> None:
    """Conform and delivery flags, shared by ``auto run`` and ``batch``.

    Defined once for the same reason ``_add_visuals`` is: two spellings of the
    same switch is a run nobody can reproduce by hand.
    """
    group = parser.add_argument_group("conform and delivery")
    group.add_argument(
        "--conform", dest="conform", default="full",
        choices=list(CONFORM_MODES),
        help="how much of the finished edit to build as real operations. "
             "off leaves every decision as a plan, which is what this "
             "pipeline did before the conform pass existed")
    group.add_argument(
        "--color-look", dest="color_look", default="",
        choices=[""] + sorted(COLOR_LOOKS),
        help="force a colour treatment instead of letting the footage decide")
    group.add_argument(
        "--music-library", dest="music_library", default="",
        help="folder of music this edit may choose a bed from")
    group.add_argument(
        "--target-lufs", dest="target_lufs", type=float, default=-14.0,
        help="loudness the finished mix is aimed at (default -14, which is "
             "what streaming platforms normalise to)")
    group.add_argument(
        "--max-transitions", dest="max_transitions", type=int, default=6,
        help="ceiling on deliberate transitions across the episode")
    group.add_argument(
        "--deliver", dest="deliver", action="store_true",
        help="render the finished sequence to a file at the end of the run "
             "(off by default: it renders a whole episode through Premiere)")
    group.add_argument(
        "--deliver-output", dest="deliver_output", default="",
        help="where the finished file goes (default under delivered/)")
    group.add_argument(
        "--deliver-preset", dest="deliver_preset", default="",
        help="an .epr export preset (default: a match-source H.264)")


def _add_visuals(parser: argparse.ArgumentParser) -> None:
    """Visual layer flags, shared by ``auto run``, ``batch`` and ``visuals``.

    Defined once so the three cannot drift: a run that spelled
    ``--visual-layer`` differently from a batch would be a run nobody could
    reproduce by hand.
    """
    from editing.visuals import schema as visual_schema

    group = parser.add_argument_group("visual layer")
    group.add_argument(
        "--visual-layer", dest="visual_layer", default="off",
        choices=list(visual_schema.VISUAL_LAYERS),
        help="balanced is the intended setting; high lets one moment carry "
             "three effects and is the one that reads as over-edited")
    group.add_argument(
        "--visual-mode", dest="visual_mode", default="plan_only",
        choices=list(visual_schema.COMPOSER_MODES),
        help="plan_only (default) composes a final edit plan; proxy_preview "
             "adds a marker file; premiere_plan adds an operation plan. None "
             "of them executes anything")
    group.add_argument(
        "--max-effects-per-minute", dest="max_effects_per_minute", type=float,
        default=0.0,
        help="ceiling on picture-changing effects (0 = the style's own)")
    group.add_argument(
        "--max-callouts-per-minute", dest="max_callouts_per_minute",
        type=float, default=0.0,
        help="ceiling on arrows, circles and boxes (0 = the style's own)")
    # Each family has both spellings. On by default, so ``--no-*`` is the one
    # that changes anything -- but ``--allow-*`` is what a person reaches for
    # when turning a feature on, and a flag that exists in somebody's head and
    # not in the parser produces an error message instead of a run. The pairs
    # are mutually exclusive so ``--allow-callouts --no-callouts`` is a usage
    # error rather than a silent winner.
    freeze = group.add_mutually_exclusive_group()
    freeze.add_argument(
        "--no-freeze-frames", dest="no_freeze_frames", action="store_true",
        help="never plan a freeze frame")
    freeze.add_argument(
        "--allow-freeze-frames", dest="no_freeze_frames",
        action="store_false",
        help="permit freeze frames (the default)")

    callouts = group.add_mutually_exclusive_group()
    callouts.add_argument(
        "--no-callouts", dest="no_callouts", action="store_true",
        help="never plan an arrow, a circle or a box")
    callouts.add_argument(
        "--allow-callouts", dest="no_callouts", action="store_false",
        help="permit callouts (the default)")

    replays = group.add_mutually_exclusive_group()
    replays.add_argument(
        "--no-replays", dest="no_replays", action="store_true",
        help="never plan a replay marker")
    replays.add_argument(
        "--allow-replays", dest="no_replays", action="store_false",
        help="permit replay markers (the default)")
    group.add_argument(
        "--allow-screen-shake", dest="allow_screen_shake",
        action="store_true",
        help="permit screen shake. Off by default: it is the effect most "
             "often refused by the safety pass and the most annoying one "
             "when it is not")
    group.add_argument(
        "--export-premiere-visual-plan", dest="export_premiere_visual_plan",
        action="store_true",
        help="write the Premiere visual operation plan even in a mode that "
             "would not. It still executes nothing")


def _add_model(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("model")
    group.add_argument("--backend", choices=sorted(set(qwen.BACKENDS) | {"mock"}),
                       help="how to reach the vision model (default openai)")
    group.add_argument("--model", help="model name (default Qwen3-VL-8B-Instruct)")
    group.add_argument("--base-url", help="model server base URL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m editing.cli",
        description="Editing Brain V1 -- footage structure and edit "
                    "recommendations. Produces a machine-readable timeline of "
                    "what happens in Minecraft footage, what is said and heard "
                    "over it, and which edits would be worth making. Proposes "
                    "only: it never applies an edit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Everything at once, planning only:\n"
               "  python -m editing.cli auto run --folder D:/Footage/ep12 "
               "--style cinematic_minecraft\n"
               "  python -m editing.cli auto show-gates\n"
               "  python -m editing.cli auto execute-stage roughcut --yes\n"
               "\nOr try it with no GPU, no model server and no Premiere:\n"
               "  python -m editing.cli auto run --folder D:/Footage/ep12 "
               "--mock --no-premiere\n"
               "\nStage by stage:\n"
               "  python -m editing.cli doctor\n"
               "  python -m editing.cli discover --folder D:/Footage/ep12\n"
               "  python -m editing.cli transcript status\n"
               "  python -m editing.cli audio\n"
               "  python -m editing.cli analyze\n"
               "  python -m editing.cli timeline\n"
               "  python -m editing.cli recommend --with-plan\n"
               "  python -m editing.cli roughcut build\n"
               "  python -m editing.cli roughcut dry-run\n"
               "  python -m editing.cli roughcut execute --yes\n"
               "  python -m editing.cli review export-frames\n"
               "  python -m editing.cli review critique\n"
               "  python -m editing.cli review plan\n"
               "  python -m editing.cli review dry-run\n"
               "  python -m editing.cli review execute --yes\n"
               "  python -m editing.cli style list\n"
               "  python -m editing.cli layers build --style fast_funny\n"
               "  python -m editing.cli layers dry-run\n"
               "  python -m editing.cli layers execute --yes\n"
               "  python -m editing.cli assets init\n"
               "  python -m editing.cli assets index\n"
               "  python -m editing.cli assets plan\n"
               "  python -m editing.cli assets dry-run\n"
               "  python -m editing.cli assets execute --yes\n"
               "\nOr all of it at once:\n"
               "  python -m editing.cli run --folder D:/Footage/ep12 --recommend\n"
               "\nNothing here applies an edit. `draft` validates a Premiere\n"
               "plan offline; running it stays a separate, human decision.\n",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- discover -------------------------------------------------------
    discover = subparsers.add_parser(
        "discover", help="find footage and map it to the Premiere project")
    _add_selection(discover)
    _add_common(discover)
    discover.set_defaults(func=cmd_discover)

    # -- transcript -----------------------------------------------------
    transcript = subparsers.add_parser(
        "transcript", help="Premiere Speech to Text, or import a transcript file")
    transcript_subs = transcript.add_subparsers(
        dest="transcript_command", required=True)

    status = transcript_subs.add_parser(
        "status", help="what transcript data is available, per file")
    _add_selection(status)
    _add_common(status)
    status.set_defaults(func=cmd_transcript)

    pull = transcript_subs.add_parser(
        "pull", help="read transcripts from Premiere where it has them")
    pull.add_argument("--refresh", action="store_true",
                      help="re-read even if one is already stored")
    _add_selection(pull)
    _add_common(pull)
    pull.set_defaults(func=cmd_transcript)

    imported = transcript_subs.add_parser(
        "import", help="import an .srt/.vtt/.txt/.json/.csv transcript")
    imported.add_argument("--file", dest="file_path", required=True,
                          help="the transcript file to import")
    imported.add_argument("--for", dest="for_", required=True,
                          help="which clip it belongs to (name or asset id)")
    imported.add_argument("--folder", help="footage folder, if not yet discovered")
    imported.add_argument("--only", help=argparse.SUPPRESS)
    imported.add_argument("--no-recursive", action="store_true",
                          help=argparse.SUPPRESS)
    _add_common(imported)
    imported.set_defaults(func=cmd_transcript, file=[])

    # -- analyze --------------------------------------------------------
    analyze = subparsers.add_parser(
        "analyze", help="run the vision model over sampled windows")
    analyze.add_argument("--keep-frames", action="store_true",
                         help="keep extracted frames for inspection")
    analyze.add_argument("--no-motion", action="store_true",
                         help="skip the motion scan; sample uniformly")
    analyze.add_argument("--max-windows", type=int,
                         help="analyse at most N windows per file (for a trial run)")
    _add_selection(analyze)
    _add_sampling(analyze)
    _add_model(analyze)
    _add_common(analyze)
    analyze.set_defaults(func=cmd_analyze)

    # -- timeline -------------------------------------------------------
    timeline = subparsers.add_parser(
        "timeline", help="combine visual events and transcripts")
    timeline.add_argument("--name", default="structure",
                          help="timeline file name (default structure)")
    timeline.add_argument("--no-merge", action="store_true",
                          help="one segment per window; do not merge similar ones")
    timeline.add_argument("--max-segment-seconds", type=float, default=30.0,
                          help="cap on a merged segment (default 30)")
    timeline.add_argument("--threshold", type=float,
                          default=align.DEFAULT_USABLE_THRESHOLD,
                          help="usefulness score to flag a segment usable "
                               f"(default {align.DEFAULT_USABLE_THRESHOLD})")
    timeline.add_argument("--refresh-transcripts", action="store_true",
                          help="re-resolve transcripts instead of using stored ones")
    timeline.add_argument("--limit", type=int, default=60,
                          help="segments to print (default 60)")
    _add_selection(timeline)
    _add_common(timeline)
    timeline.set_defaults(func=cmd_timeline)

    # -- audio ----------------------------------------------------------
    audio = subparsers.add_parser(
        "audio", help="detect audio events (silence, spikes, reactions)")
    audio.add_argument("--refresh", action="store_true",
                       help="re-analyse even if a cached result exists")
    audio.add_argument("--silence-db", type=float,
                       help="dBFS floor for silence (default -45)")
    audio.add_argument("--min-silence", type=float,
                       help="shortest silence worth reporting (default 0.8s)")
    audio.add_argument("--long-pause", type=float,
                       help="transcript gap that counts as dead air (default 2.5s)")
    audio.add_argument("--spike-db", type=float,
                       help="dB above baseline that counts as a spike (default 8)")
    audio.add_argument("--audio-interval", type=float,
                       help="loudness sample spacing in seconds (default 0.25)")
    _add_selection(audio)
    _add_common(audio)
    audio.set_defaults(func=cmd_audio)

    # -- attach ---------------------------------------------------------
    attach = subparsers.add_parser(
        "attach",
        help="fold analysed audio events into the timeline (rebuilds it)")
    attach.add_argument("--name", default="structure")
    _add_selection(attach)
    _add_common(attach)
    attach.set_defaults(func=cmd_attach)

    # -- recommend ------------------------------------------------------
    recommend = subparsers.add_parser(
        "recommend", help="generate layered edit recommendations")
    recommend.add_argument("--name", default="structure")
    recommend.add_argument("--limit", type=int, default=25)
    recommend.add_argument(
        "--budget-seconds", type=float, default=20.0,
        help="seconds of footage per permitted active edit; higher is calmer "
             "(default 20)")
    recommend.add_argument(
        "--repeat-gap", type=float, default=12.0,
        help="minimum seconds between two edits of the same kind (default 12)")
    recommend.add_argument(
        "--no-safety", action="store_true",
        help="skip the anti-trash pass -- for inspecting raw proposals only; "
             "the result must not be used to build a plan")
    recommend.add_argument("--with-plan", action="store_true",
                           help="also build and dry-run the draft Premiere plan")
    _add_common(recommend)
    recommend.set_defaults(func=cmd_recommend)

    # -- top / reactions / removed --------------------------------------
    top = subparsers.add_parser("top", help="the moments most worth using")
    top.add_argument("--name", default="structure")
    top.add_argument("--limit", type=int, default=20)
    _add_common(top)
    top.set_defaults(func=cmd_top)

    reactions = subparsers.add_parser(
        "reactions", help="moments the audio made interesting")
    reactions.add_argument("--name", default="structure")
    reactions.add_argument("--limit", type=int, default=20)
    _add_common(reactions)
    reactions.set_defaults(func=cmd_reactions)

    removed = subparsers.add_parser(
        "removed", help="what the safety pass took out, and why")
    removed.add_argument("--name", default="structure")
    removed.add_argument("--limit", type=int, default=40)
    _add_common(removed)
    removed.set_defaults(func=cmd_removed)

    # -- draft ----------------------------------------------------------
    draft = subparsers.add_parser(
        "draft",
        help="build the draft Premiere plan and dry-run it (executes nothing)")
    draft.add_argument("--name", default="structure")
    draft.add_argument("--limit", type=int, default=30)
    _add_common(draft)
    draft.set_defaults(func=cmd_draft)

    # -- roughcut -------------------------------------------------------
    roughcut = subparsers.add_parser(
        "roughcut",
        help="build, validate and (explicitly) execute a rough cut sequence")
    roughcut_subs = roughcut.add_subparsers(
        dest="roughcut_command", required=True)

    rc_build = roughcut_subs.add_parser(
        "build", help="select ranges and build the operation plan")
    rc_build.add_argument("--name", default="structure")
    rc_build.add_argument("--sequence", default="Nova Rough Cut",
                          help="scratch sequence name")
    rc_build.add_argument("--keep-threshold", type=float, default=0.40,
                          help="usefulness below which a segment is dropped "
                               "(default 0.40)")
    rc_build.add_argument("--filler-speed", type=float, default=2.0,
                          help="playback rate for dull silent footage "
                               "(default 2.0)")
    rc_build.add_argument("--handle", type=float, default=0.25,
                          help="seconds of handle either side of a kept range")
    rc_build.add_argument("--drop-filler", action="store_true",
                          help="drop low-value footage instead of speeding it up")
    rc_build.add_argument("--no-zooms", action="store_true",
                          help="markers only; do not animate Motion > Scale")
    rc_build.add_argument("--preset", help="a .sqpreset path for the sequence")
    rc_build.add_argument("--plan-only", action="store_true",
                          help="build the operations without validating them")
    rc_build.add_argument("--limit", type=int, default=30)
    _add_common(rc_build)
    rc_build.set_defaults(func=cmd_roughcut)

    rc_dry = roughcut_subs.add_parser(
        "dry-run", help="validate the plan offline (executes nothing)")
    rc_dry.add_argument("--name", default="structure")
    rc_dry.add_argument("--limit", type=int, default=40)
    _add_common(rc_dry)
    rc_dry.set_defaults(func=cmd_roughcut)

    rc_exec = roughcut_subs.add_parser(
        "execute",
        help="run the plan on a scratch sequence -- needs --yes")
    rc_exec.add_argument("--name", default="structure")
    rc_exec.add_argument("--yes", action="store_true",
                         help="required: confirms you have read the dry run")
    rc_exec.add_argument(
        "--allow-active-sequence", action="store_true",
        help="permit editing the sequence currently open instead of a scratch "
             "one. Off by default, and rarely what you want.")
    rc_exec.add_argument("--limit", type=int, default=40)
    _add_common(rc_exec)
    rc_exec.set_defaults(func=cmd_roughcut)

    rc_place = roughcut_subs.add_parser(
        "placements", help="what lands where on the sequence")
    rc_place.add_argument("--name", default="structure")
    rc_place.add_argument("--limit", type=int, default=60)
    _add_common(rc_place)
    rc_place.set_defaults(func=cmd_roughcut)

    rc_unconv = roughcut_subs.add_parser(
        "unconverted", help="recommendations that did not make the cut, and why")
    rc_unconv.add_argument("--name", default="structure")
    rc_unconv.add_argument("--limit", type=int, default=40)
    _add_common(rc_unconv)
    rc_unconv.set_defaults(func=cmd_roughcut)

    rc_report = roughcut_subs.add_parser(
        "report", help="the last execution report")
    rc_report.add_argument("--name", default="structure")
    rc_report.add_argument("--limit", type=int, default=40)
    _add_common(rc_report)
    rc_report.set_defaults(func=cmd_roughcut)

    # -- conform --------------------------------------------------------
    # The pass that makes everything above real. Its subcommands mirror the
    # rough cut's on purpose: build, dry-run, execute, and then one more that
    # none of the others have -- a finished file.
    conform = subparsers.add_parser(
        "conform",
        help="turn every decision into real Premiere operations")
    conform_subs = conform.add_subparsers(dest="conform_command", required=True)

    cf_build = conform_subs.add_parser(
        "build", help="compose captions, sound, music, visuals, colour and mix")
    cf_build.add_argument("--name", default="structure")
    cf_build.add_argument("--style", default="",
                          help="style preset; also seeds the colour look")
    cf_build.add_argument("--mode", default="full",
                          choices=list(CONFORM_MODES),
                          help="how much of the edit to build (default full)")
    cf_build.add_argument("--color", default="",
                          help="force a colour look: "
                               + ", ".join(sorted(COLOR_LOOKS)))
    cf_build.add_argument("--color-strength", type=float, default=1.0,
                          help="0-1; scales the look towards neutral")
    cf_build.add_argument("--music-library", default="",
                          help="folder of music this edit may choose from")
    cf_build.add_argument("--target-lufs", type=float, default=-14.0,
                          help="loudness target for the finished mix")
    cf_build.add_argument("--max-transitions", type=int, default=6)
    cf_build.add_argument("--no-captions", action="store_true")
    cf_build.add_argument("--no-sound", action="store_true")
    cf_build.add_argument("--no-music", action="store_true")
    cf_build.add_argument("--no-visuals", action="store_true")
    cf_build.add_argument("--no-color", action="store_true")
    cf_build.add_argument("--no-transitions", action="store_true")
    cf_build.add_argument("--limit", type=int, default=40)
    _add_common(cf_build)
    cf_build.set_defaults(func=cmd_conform)

    cf_dry = conform_subs.add_parser(
        "dry-run", help="validate the plan offline (executes nothing)")
    cf_dry.add_argument("--name", default="structure")
    cf_dry.add_argument("--limit", type=int, default=40)
    _add_common(cf_dry)
    cf_dry.set_defaults(func=cmd_conform)

    cf_exec = conform_subs.add_parser(
        "execute", help="apply the plan to the sequence -- needs --yes")
    cf_exec.add_argument("--name", default="structure")
    cf_exec.add_argument("--yes", action="store_true",
                         help="required: confirms you have read the dry run")
    cf_exec.add_argument("--limit", type=int, default=40)
    _add_common(cf_exec)
    cf_exec.set_defaults(func=cmd_conform)

    cf_report = conform_subs.add_parser(
        "report", help="what was built, executed and delivered")
    cf_report.add_argument("--name", default="structure")
    cf_report.add_argument("--limit", type=int, default=40)
    _add_common(cf_report)
    cf_report.set_defaults(func=cmd_conform)

    cf_ops = conform_subs.add_parser(
        "operations", help="every operation the plan will send")
    cf_ops.add_argument("--name", default="structure")
    cf_ops.add_argument("--limit", type=int, default=80)
    _add_common(cf_ops)
    cf_ops.set_defaults(func=cmd_conform)

    cf_verify = conform_subs.add_parser(
        "verify",
        help="photograph the finished edit and (optionally) critique it")
    cf_verify.add_argument("--name", default="structure")
    cf_verify.add_argument(
        "--critique", action="store_true",
        help="run the vision critic over the frames. Unlike the review pass, "
             "these are frames of the EDIT rather than of the raw footage")
    cf_verify.add_argument("--limit", type=int, default=12,
                           help="how many moments to photograph")
    _add_common(cf_verify)
    cf_verify.set_defaults(func=cmd_conform)

    cf_unconv = conform_subs.add_parser(
        "unconverted", help="decisions that could not become an operation")
    cf_unconv.add_argument("--name", default="structure")
    cf_unconv.add_argument("--limit", type=int, default=40)
    _add_common(cf_unconv)
    cf_unconv.set_defaults(func=cmd_conform)

    # -- deliver --------------------------------------------------------
    deliver_cmd = subparsers.add_parser(
        "deliver",
        help="render the finished sequence to a video file")
    deliver_cmd.add_argument("--name", default="structure")
    deliver_cmd.add_argument("--sequence", default="",
                             help="sequence to export; defaults to the one "
                                  "the conform plan targeted")
    deliver_cmd.add_argument("--output", default="",
                             help="output file; defaults to "
                                  "data/editing/delivered/<sequence>-<time>.mp4")
    deliver_cmd.add_argument("--preset", default="",
                             help="an .epr path; defaults to a match-source "
                                  "H.264 preset the host finds")
    deliver_cmd.add_argument("--wait", type=float, default=900.0,
                             help="seconds to wait for a Media Encoder render")
    _add_common(deliver_cmd)
    deliver_cmd.set_defaults(func=cmd_deliver)

    # -- review ---------------------------------------------------------
    # A subcommand group, but the bare `review` still works and means
    # `review export-frames` -- that is what Session 3 documented, and
    # breaking it to add subcommands would be a bad trade.
    review = subparsers.add_parser(
        "review",
        help="the critic pass: review frames, critique, revise, apply")
    _add_review_export_args(review)
    _add_common(review)
    review.set_defaults(func=cmd_review, review_command="export-frames")
    review_subs = review.add_subparsers(dest="review_command", required=False)

    rv_frames = review_subs.add_parser(
        "export-frames",
        help="choose the moments worth reviewing and extract them")
    _add_review_export_args(rv_frames)
    _add_common(rv_frames)
    rv_frames.set_defaults(func=cmd_review)

    rv_crit = review_subs.add_parser(
        "critique", help="run the visual critic over the exported frames")
    rv_crit.add_argument("--name", default="structure")
    rv_crit.add_argument("--max-frames", type=int, default=0,
                         help="critique at most this many frames (0 = all)")
    rv_crit.add_argument("--limit", type=int, default=40)
    _add_model(rv_crit)
    _add_common(rv_crit)
    rv_crit.set_defaults(func=cmd_review)

    rv_plan = review_subs.add_parser(
        "plan", help="turn findings into revisions and a validated plan")
    rv_plan.add_argument("--name", default="structure")
    rv_plan.add_argument("--no-timing", action="store_true",
                         help="propose no trims or hold extensions, so the "
                              "cut's timing is left exactly as it was")
    rv_plan.add_argument("--no-zoom-edits", action="store_true",
                         help="never change a zoom; report them instead")
    rv_plan.add_argument("--no-review-markers", action="store_true",
                         help="do not put REVIEW markers on the timeline for "
                              "findings that could not be fixed")
    rv_plan.add_argument("--min-confidence", type=float, default=0.60,
                         help="critic confidence needed to change the edit "
                              "automatically (default 0.60)")
    rv_plan.add_argument("--limit", type=int, default=30)
    _add_common(rv_plan)
    rv_plan.set_defaults(func=cmd_review)

    rv_dry = review_subs.add_parser(
        "dry-run", help="validate the revision plan offline (applies nothing)")
    rv_dry.add_argument("--name", default="structure")
    rv_dry.add_argument("--limit", type=int, default=40)
    _add_common(rv_dry)
    rv_dry.set_defaults(func=cmd_review)

    rv_exec = review_subs.add_parser(
        "execute", help="apply the revisions to the rough cut -- needs --yes")
    rv_exec.add_argument("--name", default="structure")
    rv_exec.add_argument("--yes", action="store_true",
                         help="required: confirms you have read the dry run")
    rv_exec.add_argument(
        "--allow-active-sequence", action="store_true",
        help="permit revising a sequence this system cannot prove is the "
             "rough cut's scratch one. Off by default.")
    rv_exec.add_argument("--limit", type=int, default=40)
    _add_common(rv_exec)
    rv_exec.set_defaults(func=cmd_review)

    rv_report = review_subs.add_parser(
        "report", help="the full revision report, including what it could not fix")
    rv_report.add_argument("--name", default="structure")
    rv_report.add_argument("--limit", type=int, default=40)
    _add_common(rv_report)
    rv_report.set_defaults(func=cmd_review)

    rv_package = review_subs.add_parser(
        "package",
        help="gather one run into a review folder with an index. Rebuilds it "
             "from what is on disk now")
    rv_package.add_argument("--run", help="run id (default: the most recent)")
    rv_package.add_argument(
        "--no-checks", dest="no_checks", action="store_true",
        help="skip the reliability checks rather than running them fresh")
    rv_package.add_argument("--limit", type=int, default=40)
    _add_common(rv_package)
    rv_package.set_defaults(func=cmd_review)

    rv_summary = review_subs.add_parser(
        "summary",
        help="what one run produced, what to watch for, and what needs you")
    rv_summary.add_argument("--run", help="run id")
    rv_summary.add_argument(
        "--latest", action="store_true",
        help="the most recent run with a review package (the default when no "
             "--run is given)")
    rv_summary.add_argument("--limit", type=int, default=40)
    _add_common(rv_summary)
    rv_summary.set_defaults(func=cmd_review)

    rv_open = review_subs.add_parser(
        "open-latest",
        help="print the newest review index, and open it if the desktop can")
    rv_open.add_argument("--run", help="run id (default: the most recent)")
    rv_open.add_argument(
        "--print", dest="print_only", action="store_true",
        help="print the index rather than handing it to the desktop")
    rv_open.add_argument("--limit", type=int, default=40)
    _add_common(rv_open)
    rv_open.set_defaults(func=cmd_review)

    rv_issues = review_subs.add_parser(
        "show-issues", help="one line per issue, worst first")
    rv_issues.add_argument("--name", default="structure")
    rv_issues.add_argument("--severity", choices=["low", "medium", "high"],
                           help="only this severity and above")
    rv_issues.add_argument("--limit", type=int, default=40)
    _add_common(rv_issues)
    rv_issues.set_defaults(func=cmd_review)

    # -- style ----------------------------------------------------------
    style = subparsers.add_parser(
        "style", help="the editing style presets available")
    style_subs = style.add_subparsers(dest="style_command", required=True)

    st_list = style_subs.add_parser("list", help="every preset, one line each")
    _add_common(st_list)
    st_list.set_defaults(func=cmd_style)

    st_show = style_subs.add_parser(
        "show", help="every number in one preset, and why")
    st_show.add_argument("preset", help="preset name")
    _add_common(st_show)
    st_show.set_defaults(func=cmd_style)

    # -- layers ---------------------------------------------------------
    layers = subparsers.add_parser(
        "layers",
        help="build, inspect and (explicitly) apply a styled layered edit")
    layers_subs = layers.add_subparsers(dest="layers_command", required=True)

    ly_build = layers_subs.add_parser(
        "build", help="compile the layers for a style and dry-run them")
    ly_build.add_argument("--name", default="structure")
    ly_build.add_argument(
        "--style", default=style_presets.DEFAULT_PRESET,
        choices=style_presets.names(),
        help=f"which preset to apply (default {style_presets.DEFAULT_PRESET})")
    ly_build.add_argument("--markers-only", action="store_true",
                          help="draw and scale nothing; record every choice "
                               "as a marker instead")
    ly_build.add_argument("--no-text", action="store_true",
                          help="plan no captions or cards at all")
    ly_build.add_argument("--no-zooms", action="store_true",
                          help="never scale the picture, whatever the style says")
    ly_build.add_argument("--no-base", action="store_true",
                          help="omit the rough cut's own clips from the plan")
    ly_build.add_argument("--no-critic", action="store_true",
                          help="ignore critic findings when placing text and "
                               "emphasis")
    ly_build.add_argument("--max-edits-per-minute", type=float,
                          help="override the style's edit ceiling")
    ly_build.add_argument("--max-captions-per-minute", type=float,
                          help="override the style's caption ceiling")
    ly_build.add_argument("--max-zooms-per-minute", type=float,
                          help="override the style's zoom ceiling")
    ly_build.add_argument("--max-operations", type=int, default=400)
    ly_build.add_argument("--limit", type=int, default=20)
    _add_common(ly_build)
    ly_build.set_defaults(func=cmd_layers)

    ly_report = layers_subs.add_parser(
        "report", help="the full layered report, layer by layer")
    ly_report.add_argument("--name", default="structure")
    ly_report.add_argument("--limit", type=int, default=30)
    _add_common(ly_report)
    ly_report.set_defaults(func=cmd_layers)

    ly_export = layers_subs.add_parser(
        "export", help="write the layered plan somewhere of your choosing")
    ly_export.add_argument("--name", default="structure")
    ly_export.add_argument("--out", required=True,
                           help="destination path (.json, or .txt for the report)")
    _add_common(ly_export)
    ly_export.set_defaults(func=cmd_layers)

    ly_dry = layers_subs.add_parser(
        "dry-run", help="validate the layered plan offline (applies nothing)")
    ly_dry.add_argument("--name", default="structure")
    ly_dry.add_argument("--limit", type=int, default=40)
    _add_common(ly_dry)
    ly_dry.set_defaults(func=cmd_layers)

    ly_exec = layers_subs.add_parser(
        "execute", help="apply the layers to the rough cut -- needs --yes")
    ly_exec.add_argument("--name", default="structure")
    ly_exec.add_argument("--yes", action="store_true",
                         help="required: confirms you have read the dry run")
    ly_exec.add_argument(
        "--allow-active-sequence", action="store_true",
        help="permit styling a sequence this system cannot prove is the "
             "rough cut's scratch one. Off by default.")
    ly_exec.add_argument("--limit", type=int, default=40)
    _add_common(ly_exec)
    ly_exec.set_defaults(func=cmd_layers)

    ly_def = layers_subs.add_parser(
        "show-deferred", help="what the style held back, and why")
    ly_def.add_argument("--name", default="structure")
    ly_def.add_argument("--limit", type=int, default=60)
    _add_common(ly_def)
    ly_def.set_defaults(func=cmd_layers)

    ly_den = layers_subs.add_parser(
        "show-density", help="edits per minute, against the style's ceilings")
    ly_den.add_argument("--name", default="structure")
    ly_den.add_argument("--limit", type=int, default=40)
    _add_common(ly_den)
    ly_den.set_defaults(func=cmd_layers)

    # -- assets ---------------------------------------------------------
    assets = subparsers.add_parser(
        "assets",
        help="the local sound/graphic library, and placing from it")
    assets_subs = assets.add_subparsers(dest="assets_command", required=True)

    def _add_root(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--root",
            help="asset library root (default <model dir>/assets)")

    as_init = assets_subs.add_parser(
        "init", help="create the asset folders and their documentation")
    _add_root(as_init)
    _add_common(as_init)
    as_init.set_defaults(func=cmd_assets)

    as_index = assets_subs.add_parser(
        "index", help="scan the asset folders into an index")
    _add_root(as_index)
    as_index.add_argument("--no-probe", action="store_true",
                          help="skip ffprobe; durations stay unknown")
    as_index.add_argument("--rebuild", action="store_true",
                          help="ignore the previous index and re-probe everything")
    as_index.add_argument("--limit", type=int, default=20)
    _add_common(as_index)
    as_index.set_defaults(func=cmd_assets)

    as_list = assets_subs.add_parser("list", help="what is in the library")
    _add_root(as_list)
    as_list.add_argument("--filter", help="substring of id, filename or tag")
    as_list.add_argument("--category", choices=list(
        __import__("editing.assets.schema", fromlist=["CATEGORIES"]).CATEGORIES))
    as_list.add_argument("--limit", type=int, default=60)
    _add_common(as_list)
    as_list.set_defaults(func=cmd_assets)

    as_show = assets_subs.add_parser(
        "show", help="everything known about one asset")
    as_show.add_argument("asset", help="asset id, filename or tag")
    _add_root(as_show)
    as_show.add_argument("--limit", type=int, default=40)
    _add_common(as_show)
    as_show.set_defaults(func=cmd_assets)

    as_validate = assets_subs.add_parser(
        "validate", help="what is wrong with the library, and how to fix it")
    _add_root(as_validate)
    as_validate.add_argument("--limit", type=int, default=40)
    _add_common(as_validate)
    as_validate.set_defaults(func=cmd_assets)

    as_report = assets_subs.add_parser(
        "report", help="library contents and what each placeholder can draw on")
    _add_root(as_report)
    as_report.add_argument("--style", choices=style_presets.names())
    as_report.add_argument("--limit", type=int, default=40)
    _add_common(as_report)
    as_report.set_defaults(func=cmd_assets)

    as_match = assets_subs.add_parser(
        "match", help="rank the library against one placeholder kind")
    as_match.add_argument("kind", help="e.g. impact_sfx, whoosh, tension_bed")
    _add_root(as_match)
    as_match.add_argument("--style", choices=style_presets.names())
    as_match.add_argument("--slot", type=float,
                          help="slot length in seconds, for duration fit")
    as_match.add_argument("--min-score", type=float, default=0.5)
    as_match.add_argument("--limit", type=int, default=15)
    _add_common(as_match)
    as_match.set_defaults(func=cmd_assets)

    as_plan = assets_subs.add_parser(
        "plan", help="resolve every layer placeholder against the library")
    as_plan.add_argument("--name", default="structure")
    _add_root(as_plan)
    as_plan.add_argument("--markers-only", action="store_true",
                         help="match but place nothing; record every choice")
    as_plan.add_argument("--allow-unsafe", action="store_true",
                         help="include assets whose sidecar says "
                              "safe_for_auto: false")
    as_plan.add_argument("--no-critic", action="store_true",
                         help="ignore critic findings when placing graphics")
    as_plan.add_argument("--min-score", type=float, default=0.5,
                         help="match score needed to place anything "
                              "(default 0.50)")
    as_plan.add_argument("--min-sfx-gap", type=float,
                         help="seconds between two placed one-shots")
    as_plan.add_argument("--max-sfx-per-minute", type=float,
                         help="ceiling on placed one-shots per minute")
    as_plan.add_argument("--max-concurrent-audio", type=int,
                         help="how many asset clips may sound at once")
    as_plan.add_argument("--allow-music-over-speech", action="store_true",
                         help="place beds over dialogue even without ducking")
    as_plan.add_argument("--sfx-track", help="audio track for one-shots "
                                             "(default A2)")
    as_plan.add_argument("--music-track", help="audio track for beds "
                                               "(default A3)")
    as_plan.add_argument("--visual-track", help="video track for graphics "
                                                "(default V3)")
    as_plan.add_argument("--max-operations", type=int, default=500)
    as_plan.add_argument("--limit", type=int, default=20)
    _add_common(as_plan)
    as_plan.set_defaults(func=cmd_assets)

    as_dry = assets_subs.add_parser(
        "dry-run", help="validate the asset plan offline (places nothing)")
    as_dry.add_argument("--name", default="structure")
    as_dry.add_argument("--limit", type=int, default=40)
    _add_common(as_dry)
    as_dry.set_defaults(func=cmd_assets)

    as_exec = assets_subs.add_parser(
        "execute", help="place the assets -- needs --yes")
    as_exec.add_argument("--name", default="structure")
    as_exec.add_argument("--yes", action="store_true",
                         help="required: confirms you have read the dry run")
    as_exec.add_argument(
        "--allow-active-sequence", action="store_true",
        help="permit placing on a sequence this system cannot prove is the "
             "rough cut's scratch one. Off by default.")
    as_exec.add_argument("--limit", type=int, default=40)
    _add_common(as_exec)
    as_exec.set_defaults(func=cmd_assets)

    as_missing = assets_subs.add_parser(
        "show-missing", help="a shopping list of what the library lacks")
    as_missing.add_argument("--name", default="structure")
    as_missing.add_argument("--limit", type=int, default=40)
    _add_common(as_missing)
    as_missing.set_defaults(func=cmd_assets)

    as_deferred = assets_subs.add_parser(
        "show-deferred", help="every placeholder that placed nothing, and why")
    as_deferred.add_argument("--name", default="structure")
    as_deferred.add_argument("--limit", type=int, default=60)
    _add_common(as_deferred)
    as_deferred.set_defaults(func=cmd_assets)

    # -- auto -----------------------------------------------------------
    auto = subparsers.add_parser(
        "auto",
        help="run the whole pipeline with checkpoints and gated execution")
    auto_subs = auto.add_subparsers(dest="auto_command", required=True)

    def _add_run_ref(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--run", help="run id (default: the most recent run)")

    au_run = auto_subs.add_parser(
        "run", help="plan the whole edit. Executes nothing.")
    au_run.add_argument("--folder", help="footage folder", required=False)
    au_run.add_argument("--style", default=style_presets.DEFAULT_PRESET,
                        choices=style_presets.names())
    au_run.add_argument("--name", default="structure")
    au_run.add_argument("--asset-library",
                        help="asset library root (default <model dir>/assets)")
    au_run.add_argument("--mock", action="store_true",
                        help="mock the vision model and the critic: no GPU, "
                             "no model server")
    au_run.add_argument("--markers-only", action="store_true",
                        help="style and asset passes record every choice "
                             "instead of drawing or playing it")
    au_run.add_argument("--skip-review", action="store_true",
                        help="skip the critic pass entirely")
    au_run.add_argument("--skip-episode", action="store_true",
                        help="skip the episode memory and retention planner")
    au_run.add_argument(
        "--transcribe", action="store_true",
        help="produce transcripts with local Whisper before analysing "
             "(off by default: it loads a speech model)")
    au_run.add_argument(
        "--transcribe-model", default="",
        help="whisper size for that stage (default small)")
    au_run.add_argument(
        "--transcribe-language", default="",
        help="ISO code, e.g. en. Omit to auto-detect")
    au_run.add_argument(
        "--transcribe-backend", default="",
        choices=["", "faster_whisper", "mock"],
        help="mock fabricates transcripts and stamps every artifact as fake; "
             "for exercising the pipeline, never for an edit")
    au_run.add_argument(
        "--director", action="store_true",
        help="have a model read the whole episode and choose the cut "
             "(off by default: it needs a model endpoint)")
    au_run.add_argument(
        "--director-mode", dest="director_mode", default="hybrid",
        choices=["director", "hybrid"],
        help="hybrid fills what the director did not mention from the "
             "rule-based selector (default)")
    au_run.add_argument(
        "--director-model", dest="director_model", default="",
        help="model name at the director endpoint")
    au_run.add_argument(
        "--director-backend", dest="director_backend", default="",
        choices=["", "openai", "mock"],
        help="mock decides by fixed rule and stamps every artifact as such")
    au_run.add_argument(
        "--style-guide", dest="style_guide", default="",
        help="a prose file of your editing rules, for the director")
    au_run.add_argument(
        "--target-duration", dest="target_duration", type=float, default=0.0,
        help="runtime the director should aim at, in seconds")
    au_run.add_argument(
        "--retention-cut", dest="retention_cut", action="store_true",
        help="reshape the cut around the retention findings: a cold open, "
             "compressed sag, protected setups, harder dead air")
    au_run.add_argument(
        "--retention-mode", dest="retention_mode", default="report_only",
        choices=list(retention_schema.MODES),
        help="report_only decides everything and changes nothing (default)")
    au_run.add_argument(
        "--no-cold-open", dest="no_cold_open", action="store_true",
        help="leave the opening where it is")
    au_run.add_argument(
        "--max-cold-open-seconds", dest="max_cold_open_seconds", type=float,
        default=0.0, help="ceiling on the opening, in seconds")
    au_run.add_argument(
        "--dead-air-aggressiveness", dest="dead_air_aggressiveness",
        default="", choices=[""] + list(retention_schema.AGGRESSIVENESS),
        help="how hard ordinary silence is cut")
    au_run.add_argument(
        "--render-proxy", dest="render_proxy", action="store_true",
        help="render a watchable proxy MP4 of the rough cut with FFmpeg "
             "(off by default: minutes of CPU and hundreds of MB)")
    au_run.add_argument(
        "--render-quality", dest="render_quality", default="",
        choices=[""] + list(render_schema.QUALITIES),
        help="quality preset for that render (default proxy)")
    au_run.add_argument(
        "--render-height", dest="render_height", type=int, default=0,
        help="output height for that render (default 720)")
    au_run.add_argument(
        "--feedback", action="store_true",
        help="open a review session and build its queue at the end of the run "
             "(off by default: it starts a review a person has to finish)")
    au_run.add_argument(
        "--no-review-package", dest="no_review_package", action="store_true",
        help="do not gather this run into a review folder. On by default: it "
             "creates nothing new and is what makes a run inspectable")
    _add_polish(au_run)
    _add_visuals(au_run)
    _add_conform(au_run)
    au_run.add_argument("--skip-assets", action="store_true",
                        help="skip the asset pass entirely")
    au_run.add_argument("--force-new-run", action="store_true",
                        help="start a fresh run even if one already exists "
                             "for this footage and style")
    au_run.add_argument("--max-windows", type=int,
                        help="cap analysis windows per file")
    au_run.add_argument("--no-recursive", action="store_true")
    au_run.add_argument("--no-motion", action="store_true")
    au_run.add_argument("--keep-frames", action="store_true")
    au_run.add_argument("--limit", type=int, default=40)
    _add_model(au_run)
    _add_common(au_run)
    au_run.set_defaults(func=cmd_auto)

    au_resume = auto_subs.add_parser(
        "resume", help="continue a run, retrying failed and blocked stages")
    _add_run_ref(au_resume)
    au_resume.add_argument(
        "--style", choices=style_presets.names(),
        help="restyle this run in place: rebuilds the style and asset passes "
             "and reuses the analysis")
    au_resume.add_argument(
        "--refresh", action="append",
        help="re-run this stage and everything after it, ignoring checkpoints")
    au_resume.add_argument("--limit", type=int, default=40)
    _add_model(au_resume)
    _add_common(au_resume)
    au_resume.set_defaults(func=cmd_auto)

    au_status = auto_subs.add_parser(
        "status", help="one line per stage, and where the run stands")
    _add_run_ref(au_status)
    au_status.add_argument("--limit", type=int, default=40)
    _add_common(au_status)
    au_status.set_defaults(func=cmd_auto)

    au_list = auto_subs.add_parser(
        "list-runs", help="recent runs, newest first")
    au_list.add_argument("--limit", type=int, default=25)
    _add_common(au_list)
    au_list.set_defaults(func=cmd_auto)

    au_rep = auto_subs.add_parser(
        "report", help="the full run report, including what it did not do")
    _add_run_ref(au_rep)
    au_rep.add_argument("--limit", type=int, default=40)
    _add_common(au_rep)
    au_rep.set_defaults(func=cmd_auto)

    au_gates = auto_subs.add_parser(
        "show-gates", help="what could be executed, and what is blocking it")
    _add_run_ref(au_gates)
    au_gates.add_argument("--limit", type=int, default=40)
    _add_common(au_gates)
    au_gates.set_defaults(func=cmd_auto)

    au_exec = auto_subs.add_parser(
        "execute-stage",
        help="execute exactly one gated stage against Premiere -- needs --yes")
    au_exec.add_argument("stage", choices=auto_gates.gate_names())
    au_exec.add_argument(
        "--again", action="store_true",
        help="re-run a stage that has already been executed. Only for a sequence that has been rebuilt or deleted since -- otherwise this places a second copy of everything")
    _add_run_ref(au_exec)
    au_exec.add_argument("--yes", action="store_true",
                         help="required: confirms you have read the dry run")
    au_exec.add_argument(
        "--allow-active-sequence", action="store_true",
        help="permit editing a sequence this system cannot prove is the "
             "rough cut's scratch one. Off by default.")
    au_exec.add_argument("--limit", type=int, default=40)
    _add_common(au_exec)
    au_exec.set_defaults(func=cmd_auto)

    au_finish = auto_subs.add_parser(
        "finish",
        help="build the sequence, conform it and export a video -- needs --yes")
    _add_run_ref(au_finish)
    au_finish.add_argument(
        "--yes", action="store_true",
        help="required: this writes to Premiere and renders a video")
    au_finish.add_argument(
        "--again", action="store_true",
        help="re-run stages that have already been executed. Only for a "
             "sequence that has been rebuilt or deleted since")
    au_finish.add_argument(
        "--no-deliver", action="store_true",
        help="stop at the finished timeline instead of exporting a file")
    au_finish.add_argument("--output", default="",
                           help="where the finished video goes")
    au_finish.add_argument("--preset", default="",
                           help="an .epr export preset")
    _add_common(au_finish)
    au_finish.set_defaults(func=cmd_auto)

    au_checks = auto_subs.add_parser(
        "show-checks",
        help="the reliability checks: what passed, warned and failed")
    _add_run_ref(au_checks)
    au_checks.add_argument(
        "--rebuild", action="store_true",
        help="re-evaluate the checks now rather than reading the run's stored "
             "answers")
    au_checks.add_argument("--limit", type=int, default=40)
    _add_common(au_checks)
    au_checks.set_defaults(func=cmd_auto)

    au_batch = auto_subs.add_parser(
        "batch",
        help="run every footage folder under a root, one after the other")
    au_batch.add_argument("--root", required=True,
                          help="folder holding your episode folders")
    au_batch.add_argument("--style", default=style_presets.DEFAULT_PRESET,
                          choices=style_presets.names())
    au_batch.add_argument("--name", default="structure")
    au_batch.add_argument(
        "--limit", type=int, default=0,
        help="process at most this many folders (0 = all of them)")
    au_batch.add_argument(
        "--only-new", dest="only_new", action="store_true",
        help="only folders that have never been run at all")
    au_batch.add_argument(
        "--resume", action="store_true",
        help="continue runs that did not finish, instead of skipping them")
    au_batch.add_argument(
        "--force", action="store_true",
        help="run folders that already completed. Each gets a new run folder; "
             "nothing is ever overwritten")
    au_batch.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="say what would happen and create nothing")
    au_batch.add_argument("--no-recursive", action="store_true")
    au_batch.add_argument("--mock", action="store_true",
                          help="mock the vision model and the critic")
    au_batch.add_argument("--transcribe", action="store_true")
    au_batch.add_argument(
        "--director", action="store_true",
        help="have a model choose each cut (needs a model endpoint)")
    au_batch.add_argument(
        "--retention-cut", dest="retention_cut", action="store_true",
        help="reshape each cut around its retention findings")
    au_batch.add_argument(
        "--render-proxy", dest="render_proxy", action="store_true",
        help="render a watchable proxy for each folder")
    _add_polish(au_batch)
    _add_visuals(au_batch)
    _add_conform(au_batch)
    _add_model(au_batch)
    _add_common(au_batch)
    au_batch.set_defaults(func=cmd_auto)

    au_batches = auto_subs.add_parser(
        "list-batches", help="recent batches, newest first")
    au_batches.add_argument("--limit", type=int, default=25)
    _add_common(au_batches)
    au_batches.set_defaults(func=cmd_auto)

    au_batch_report = auto_subs.add_parser(
        "batch-report", help="the full summary for one batch")
    au_batch_report.add_argument(
        "--batch", help="batch id (default: the most recent batch)")
    au_batch_report.add_argument("--limit", type=int, default=60)
    _add_common(au_batch_report)
    au_batch_report.set_defaults(func=cmd_auto)

    au_why = auto_subs.add_parser(
        "explain-failure",
        help="every failed and blocked stage, with the command for each")
    _add_run_ref(au_why)
    au_why.add_argument("--limit", type=int, default=40)
    _add_common(au_why)
    au_why.set_defaults(func=cmd_auto)

    au_clean = auto_subs.add_parser(
        "clean", help="remove incomplete runs. Dry run unless --yes.")
    _add_run_ref(au_clean)
    au_clean.add_argument("--all", action="store_true",
                          help="also remove completed runs and runs that have "
                               "executed against Premiere")
    au_clean.add_argument("--yes", action="store_true",
                          help="required: actually delete")
    au_clean.add_argument("--limit", type=int, default=40)
    _add_common(au_clean)
    au_clean.set_defaults(func=cmd_auto)

    # -- polish ----------------------------------------------------------
    polish = subparsers.add_parser(
        "polish",
        help="key-moment captions and restrained sound. Plans only; nothing "
             "is drawn, played or executed",
    )
    polish_subs = polish.add_subparsers(dest="polish_command", required=True)

    def _add_polish_ref(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--name", default="structure")
        parser.add_argument(
            "--run", help="read and write inside this auto run's artifacts")
        parser.add_argument(
            "--style", choices=style_presets.names(),
            help="style preset whose caption and sound numbers to use")
        parser.add_argument("--limit", type=int, default=40)

    po_cap = polish_subs.add_parser(
        "captions", help="choose the few lines worth putting on screen")
    _add_polish_ref(po_cap)
    po_cap.add_argument(
        "--report", action="store_true",
        help="print the existing plan rather than building a new one")
    _add_polish(po_cap)
    _add_common(po_cap)
    po_cap.set_defaults(func=cmd_polish)

    po_audio = polish_subs.add_parser(
        "audio", help="mark the few moments that earn a sound")
    _add_polish_ref(po_audio)
    po_audio.add_argument(
        "--report", action="store_true",
        help="print the existing plan rather than building a new one")
    _add_polish(po_audio)
    _add_common(po_audio)
    po_audio.set_defaults(func=cmd_polish)

    po_rej = polish_subs.add_parser(
        "show-rejected",
        help="every line that was refused a caption, and the rule that "
             "refused it")
    _add_polish_ref(po_rej)
    po_rej.add_argument(
        "--reason", help="only this refusal code (see the plan's summary)")
    _add_common(po_rej)
    po_rej.set_defaults(func=cmd_polish)

    po_miss = polish_subs.add_parser(
        "show-missing",
        help="the sounds a plan asked for and could not find: a shopping list")
    _add_polish_ref(po_miss)
    _add_common(po_miss)
    po_miss.set_defaults(func=cmd_polish)

    # -- visuals ---------------------------------------------------------
    visuals = subparsers.add_parser(
        "visuals",
        help="the creative visual layer: which moments earn emphasis, which "
             "are refused, and what Premiere could do about it. Draws nothing",
    )
    visuals_subs = visuals.add_subparsers(
        dest="visuals_command", required=True)

    def _add_visuals_ref(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--name", default="structure")
        parser.add_argument(
            "--run", help="read and write inside this auto run's artifacts")
        parser.add_argument(
            "--latest", action="store_true",
            help="use the most recent auto run (the default when no --run is "
                 "given)")
        parser.add_argument(
            "--style", choices=style_presets.names(),
            help="style preset whose visual taste to use")
        parser.add_argument("--limit", type=int, default=40)

    vi_plan = visuals_subs.add_parser(
        "plan", help="find the moments that earn emphasis, and refuse most")
    _add_visuals_ref(vi_plan)
    _add_visuals(vi_plan)
    _add_common(vi_plan)
    vi_plan.set_defaults(func=cmd_visuals)

    vi_report = visuals_subs.add_parser(
        "report", help="the full visual report, including what it refused")
    _add_visuals_ref(vi_report)
    _add_common(vi_report)
    vi_report.set_defaults(func=cmd_visuals)

    vi_final = visuals_subs.add_parser(
        "show-final",
        help="the final edit plan: the cut, the captions, the sound and the "
             "visuals, clip by clip")
    _add_visuals_ref(vi_final)
    _add_common(vi_final)
    vi_final.set_defaults(func=cmd_visuals)

    vi_acc = visuals_subs.add_parser(
        "show-accepted", help="every treatment that survived every rule")
    _add_visuals_ref(vi_acc)
    vi_acc.add_argument("--effect", help="only this effect type")
    _add_common(vi_acc)
    vi_acc.set_defaults(func=cmd_visuals)

    vi_rej = visuals_subs.add_parser(
        "show-rejected",
        help="every treatment that was refused, and the rule that refused it")
    _add_visuals_ref(vi_rej)
    vi_rej.add_argument(
        "--reason", help="only this refusal code (see the plan's summary)")
    _add_common(vi_rej)
    vi_rej.set_defaults(func=cmd_visuals)

    vi_export = visuals_subs.add_parser(
        "export-premiere-plan",
        help="the operations Premiere could run, validated offline. Executes "
             "nothing")
    _add_visuals_ref(vi_export)
    _add_common(vi_export)
    vi_export.set_defaults(func=cmd_visuals)

    # -- episode ---------------------------------------------------------
    episode = subparsers.add_parser(
        "episode",
        help="episode memory and the retention planner (plans only, never "
             "executes)",
    )
    episode_subs = episode.add_subparsers(
        dest="episode_command", required=True)

    def _add_episode_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--name", default="structure", help="which timeline to read")
        parser.add_argument(
            "--limit", type=int, default=40,
            help="how many items to print")
        _add_common(parser)

    ep_mem = episode_subs.add_parser(
        "build-memory",
        help="read the story off the timeline: beats, objectives, loops, "
             "callbacks",
    )
    ep_mem.add_argument(
        "--no-roughcut", action="store_true",
        help="ignore any rough cut and use the raw timeline ordering (the "
             "times are then synthetic, not sequence time)")
    ep_mem.add_argument(
        "--no-save", action="store_true", help="print without writing files")
    _add_episode_common(ep_mem)

    ep_plan = episode_subs.add_parser(
        "plan-retention",
        help="risks, hook candidates, a peak, an ending and the suggestions",
    )
    ep_plan.add_argument(
        "--hooks", type=int, default=5,
        help="how many hook candidates to keep")
    ep_plan.add_argument(
        "--no-save", action="store_true", help="print without writing files")
    _add_episode_common(ep_plan)

    ep_report = episode_subs.add_parser(
        "report", help="the memory and the retention plan, in full")
    _add_episode_common(ep_report)

    ep_beats = episode_subs.add_parser(
        "show-beats", help="every detected story beat")
    ep_beats.add_argument(
        "--kind", default="", help="only beats of this kind")
    _add_episode_common(ep_beats)

    ep_risks = episode_subs.add_parser(
        "show-risks", help="the retention risk zones, worst first")
    ep_risks.add_argument(
        "--severity", default="", choices=["", "low", "medium", "high"],
        help="only risks at this severity")
    _add_episode_common(ep_risks)

    ep_hooks = episode_subs.add_parser(
        "show-hooks", help="candidate openings, with their scores broken out")
    _add_episode_common(ep_hooks)

    ep_loops = episode_subs.add_parser(
        "show-open-loops", help="questions the episode raises, and their fate")
    ep_loops.add_argument(
        "--unresolved", action="store_true",
        help="only the ones nothing answers")
    _add_episode_common(ep_loops)

    ep_calls = episode_subs.add_parser(
        "show-callbacks", help="places the episode refers back to itself")
    _add_episode_common(ep_calls)

    ep_export = episode_subs.add_parser(
        "export", help="write the episode artifacts, or one stage's suggestions")
    ep_export.add_argument("out", help="where to write the JSON")
    ep_export.add_argument(
        "--suggestions-for", default="",
        choices=["", "roughcut", "style", "assets", "human"],
        help="export only the suggestions one downstream pass could act on")
    ep_export.add_argument(
        "--safe-only", action="store_true",
        help="with --suggestions-for, only the ones safe to apply")
    _add_episode_common(ep_export)
    ep_export.set_defaults(func=cmd_episode)

    # Not ``parser`` as the loop variable: this function returns a local of
    # that name, and rebinding it here handed the caller a subparser.
    for episode_parser in (ep_mem, ep_plan, ep_report, ep_beats, ep_risks,
                           ep_hooks, ep_loops, ep_calls):
        episode_parser.set_defaults(func=cmd_episode)


    # -- transcribe -------------------------------------------------------
    transcribe = subparsers.add_parser(
        "transcribe",
        help="local speech to text with faster-whisper (no cloud, no upload)",
    )
    transcribe_subs = transcribe.add_subparsers(
        dest="transcribe_command", required=True)

    def _add_whisper_args(parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group("whisper")
        group.add_argument(
            "--model", dest="model_size", default=None,
            help="whisper size or a local model path (default small). "
                 "Sizes: " + ", ".join(transcribe_schema.KNOWN_MODELS[:8]))
        group.add_argument(
            "--device", default=None,
            choices=list(transcribe_schema.DEVICES),
            help="auto picks CUDA when it is genuinely usable (default auto)")
        group.add_argument(
            "--compute-type", dest="compute_type", default=None,
            choices=list(transcribe_schema.COMPUTE_TYPES),
            help="auto is float16 on CUDA, int8 on CPU")
        group.add_argument(
            "--language", default=None,
            help="ISO code, e.g. en. Omit to auto-detect")
        group.add_argument(
            "--beam-size", dest="beam_size", type=int, default=None)
        group.add_argument(
            "--word-timestamps", action="store_true",
            help="per-word timing (on by default)")
        group.add_argument(
            "--no-word-timestamps", action="store_true",
            help="skip per-word timing; roughly 10-15%% quicker")
        group.add_argument(
            "--no-vad", action="store_true",
            help="decode silence too (Whisper hallucinates into it)")
        group.add_argument(
            "--prompt", default=None,
            help="vocabulary hint, e.g. \"Minecraft, creeper, nether, "
                 "netherite, enderman\"")
        group.add_argument(
            "--backend", dest="backend_name", default=None,
            choices=list(transcribe_schema.BACKENDS),
            help="mock fabricates text and says so; never use it for an edit")
        group.add_argument(
            "--extract-audio", action="store_true",
            help="convert to WAV with FFmpeg first, instead of decoding the "
                 "container directly")
        group.add_argument(
            "--force", action="store_true",
            help="ignore the cache and re-transcribe")
        group.add_argument(
            "--no-publish", action="store_true",
            help="write the job folder but do not make this the asset's "
                 "transcript")

    tr_file = transcribe_subs.add_parser(
        "file", help="transcribe one media file")
    tr_file.add_argument("path", help="the video or audio file")
    _add_whisper_args(tr_file)
    tr_file.add_argument("--limit", type=int, default=20,
                         help="transcript lines to print")
    _add_common(tr_file)

    tr_folder = transcribe_subs.add_parser(
        "folder", help="transcribe every media file in a folder")
    tr_folder.add_argument("path", help="the folder of clips")
    _add_whisper_args(tr_folder)
    tr_folder.add_argument("--no-recursive", action="store_true")
    tr_folder.add_argument(
        "--redo-existing", action="store_true",
        help="transcribe clips that already have a current transcript")
    tr_folder.add_argument(
        "--max-files", type=int, default=0,
        help="stop after this many files (0 = no limit)")
    tr_folder.add_argument("--limit", type=int, default=40,
                           help="rows to print")
    _add_common(tr_folder)

    tr_status = transcribe_subs.add_parser(
        "status", help="is transcription installed, and what has it produced")
    _add_whisper_args(tr_status)
    tr_status.add_argument("--limit", type=int, default=20)
    _add_common(tr_status)

    tr_show = transcribe_subs.add_parser(
        "show", help="one transcription job in full")
    tr_show.add_argument("job_id")
    tr_show.add_argument("--limit", type=int, default=60)
    _add_common(tr_show)

    tr_export = transcribe_subs.add_parser(
        "export", help="write a job's transcript somewhere of your choosing")
    tr_export.add_argument("job_id")
    tr_export.add_argument("--out", required=True, help="destination path")
    tr_export.add_argument(
        "--format", default="srt", choices=["srt", "vtt", "txt", "json"])
    _add_common(tr_export)

    tr_clear = transcribe_subs.add_parser(
        "clear-cache", help="drop every cached transcription")
    tr_clear.add_argument(
        "--yes", action="store_true",
        help="required: re-transcribing a folder of episodes is hours")
    _add_common(tr_clear)

    for transcribe_parser in (tr_file, tr_folder, tr_status, tr_show,
                              tr_export, tr_clear):
        transcribe_parser.set_defaults(func=cmd_transcribe)

    # -- retention --------------------------------------------------------
    retention = subparsers.add_parser(
        "retention",
        help="wire the retention findings into the cut: a cold open, "
             "compressed sag, protected setups, harder dead air",
    )
    retention_subs = retention.add_subparsers(
        dest="retention_command", required=True)

    def _add_retention_args(parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group("retention")
        group.add_argument(
            "--mode", default=None, choices=list(retention_schema.MODES),
            help="report_only decides everything and changes nothing "
                 "(default); retention applies on the rule-based cut; "
                 "director_retention on the director's")
        group.add_argument("--no-cold-open", dest="no_cold_open",
                           action="store_true",
                           help="leave the opening where it is")
        group.add_argument(
            "--max-cold-open-seconds", dest="max_cold_open_seconds",
            type=float, default=None, help="ceiling on the opening (default 20)")
        group.add_argument(
            "--min-cold-open-seconds", dest="min_cold_open_seconds",
            type=float, default=None, help="floor on the opening (default 5)")
        group.add_argument(
            "--duplicate-policy", dest="duplicate_policy", default=None,
            choices=list(retention_schema.DUPLICATE_POLICIES),
            help="what happens to the footage the opening was lifted from "
                 "(default remove)")
        group.add_argument(
            "--allow-duplicates", dest="allow_duplicates",
            action="store_true",
            help="let the opening play twice, as a deliberate teaser")
        group.add_argument("--no-compress", dest="no_compress",
                           action="store_true",
                           help="mark sagging stretches instead of cutting")
        group.add_argument(
            "--grind-speed", dest="grind_speed", type=float, default=None,
            help="playback rate for a sped-up sag (default 2.0)")
        group.add_argument(
            "--dead-air-aggressiveness", dest="dead_air", default=None,
            choices=list(retention_schema.AGGRESSIVENESS),
            help="how hard ordinary silence is cut (default medium)")
        group.add_argument(
            "--max-silence", dest="max_silence", type=float, default=None,
            help="seconds of purposeless silence tolerated, overriding the "
                 "aggressiveness setting")
        group.add_argument("--keep-dead-air", dest="keep_dead_air",
                           action="store_true",
                           help="do not touch silence at all")
        group.add_argument("--no-protect", dest="no_protect",
                           action="store_true",
                           help="do not protect setups (rarely what you want)")
        group.add_argument(
            "--max-compression", dest="max_compression", type=float,
            default=None,
            help="ceiling on how much of the cut compression may remove, 0..1")
        group.add_argument("--target", type=float, default=None,
                           help="runtime to aim at, in seconds")
        group.add_argument("--max-duration", dest="max_duration", type=float,
                           default=None, help="hard maximum runtime")
        group.add_argument("--style", default=None,
                           choices=style_presets.names())

    rt_plan = retention_subs.add_parser(
        "plan", help="decide the retention edits, and apply them if asked")
    rt_plan.add_argument("--name", default="structure")
    _add_retention_args(rt_plan)
    _add_common(rt_plan)

    rt_report = retention_subs.add_parser(
        "report", help="the full report for the current retention cut")
    rt_report.add_argument("--name", default="structure")
    _add_common(rt_report)

    rt_cold = retention_subs.add_parser(
        "show-cold-open", help="the opening, and every candidate refused")
    rt_cold.add_argument("--name", default="structure")
    _add_common(rt_cold)

    rt_comp = retention_subs.add_parser(
        "show-compression", help="every risk zone and what happened to it")
    rt_comp.add_argument("--name", default="structure")
    rt_comp.add_argument("--limit", type=int, default=40)
    _add_common(rt_comp)

    rt_prot = retention_subs.add_parser(
        "show-protected", help="what nothing may touch, and why")
    rt_prot.add_argument("--name", default="structure")
    _add_common(rt_prot)

    rt_rej = retention_subs.add_parser(
        "show-rejected", help="every refused retention action, with the rule")
    rt_rej.add_argument("--name", default="structure")
    rt_rej.add_argument("--limit", type=int, default=60)
    _add_common(rt_rej)

    rt_cmp = retention_subs.add_parser(
        "compare", help="the retention cut against the cut it was built from")
    rt_cmp.add_argument("--name", default="structure")
    rt_cmp.add_argument("--keep-threshold", type=float, default=None)
    rt_cmp.add_argument("--filler-speed", type=float, default=None)
    rt_cmp.add_argument("--handle", type=float, default=None)
    rt_cmp.add_argument("--drop-filler", action="store_true")
    _add_common(rt_cmp)

    rt_render = retention_subs.add_parser(
        "render", help="render the retention cut to a proxy")
    rt_render.add_argument("--name", default="structure")
    rt_render.add_argument("--quality", default=None,
                           choices=list(render_schema.QUALITIES))
    rt_render.add_argument("--height", type=int, default=None)
    rt_render.add_argument("--force", action="store_true")
    rt_render.add_argument("--mock", action="store_true",
                           help="write a placeholder instead of a video")
    _add_common(rt_render)

    for retention_parser in (rt_plan, rt_report, rt_cold, rt_comp, rt_prot,
                             rt_rej, rt_cmp, rt_render):
        retention_parser.add_argument(
            "--run", default="",
            help="scope to one auto run's artifacts")
        retention_parser.set_defaults(func=cmd_retention)

    # -- director ---------------------------------------------------------
    director = subparsers.add_parser(
        "director",
        help="a model reads the whole episode and decides what the cut is "
             "(proposes only: deterministic rules check every answer)",
    )
    director_subs = director.add_subparsers(
        dest="director_command", required=True)

    def _add_director_model_args(parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group("director model")
        group.add_argument(
            "--backend", dest="backend_name", default=None,
            choices=list(director_schema.BACKENDS),
            help="mock decides by fixed rule and says so; never an editorial "
                 "judgement")
        group.add_argument(
            "--model", dest="model_name", default=None,
            help="model name at the endpoint (default qwen2.5-14b-instruct)")
        group.add_argument(
            "--base-url", dest="base_url", default=None,
            help="any OpenAI-compatible endpoint, ending in /v1")
        group.add_argument(
            "--api-key", dest="api_key", default=None,
            help="only if the endpoint needs one")
        group.add_argument("--temperature", type=float, default=None)
        group.add_argument(
            "--max-tokens", dest="max_tokens", type=int, default=None,
            help="ceiling on the answer length")

    def _add_director_shape_args(parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group("the cut")
        group.add_argument(
            "--style-guide", dest="style_guide", default="",
            help="a prose file of your editing rules. Omit for the built-in "
                 "guide; `director show-style` prints whichever is in use")
        group.add_argument(
            "--style", default=None, choices=style_presets.names(),
            help="tell the director what the style pass will add on top")
        group.add_argument(
            "--target", type=float, default=None,
            help="target runtime in seconds")
        group.add_argument(
            "--max-duration", dest="max_duration", type=float, default=None,
            help="hard maximum runtime in seconds")
        group.add_argument(
            "--max-segments", dest="max_segments", type=int, default=None,
            help="ceiling on candidate ranges shown to the model")
        group.add_argument(
            "--context-chars", dest="context_chars", type=int, default=None,
            help="ceiling on the size of the brief, in characters")

    dr_context = director_subs.add_parser(
        "build-context", help="what the director would be shown, and nothing "
                              "else. Calls no model")
    dr_context.add_argument("--name", default="structure")
    dr_context.add_argument("--show-prompt", dest="show_prompt",
                            action="store_true",
                            help="print the whole prompt as it would be sent")
    _add_director_model_args(dr_context)
    _add_director_shape_args(dr_context)
    _add_common(dr_context)

    dr_plan = director_subs.add_parser(
        "plan", help="ask the director, check every answer, write the plan")
    dr_plan.add_argument("--name", default="structure")
    dr_plan.add_argument(
        "--mode", default=None, choices=list(director_schema.MODES),
        help="director uses only its decisions; hybrid fills the gaps from "
             "the rule-based selector (default director)")
    dr_plan.add_argument("--force", action="store_true",
                         help="ignore the cache and ask again")
    _add_director_model_args(dr_plan)
    _add_director_shape_args(dr_plan)
    _add_common(dr_plan)

    dr_report = director_subs.add_parser(
        "report", help="the full report for the current plan")
    dr_report.add_argument("--name", default="structure")
    _add_common(dr_report)

    dr_show = director_subs.add_parser(
        "show-decisions", help="every accepted decision, in full")
    dr_show.add_argument("--name", default="structure")
    dr_show.add_argument("--limit", type=int, default=40)
    dr_show.add_argument("--action", default="",
                         choices=[""] + list(director_schema.ACTIONS),
                         help="only decisions asking for this")
    _add_common(dr_show)

    dr_rejected = director_subs.add_parser(
        "show-rejected", help="what the rules refused, and which rule")
    dr_rejected.add_argument("--name", default="structure")
    dr_rejected.add_argument("--limit", type=int, default=40)
    dr_rejected.add_argument("--action", default="",
                             choices=[""] + list(director_schema.ACTIONS))
    _add_common(dr_rejected)

    dr_style = director_subs.add_parser(
        "show-style", help="the style guide in force, and where it came from")
    dr_style.add_argument("--style-guide", dest="style_guide", default="")
    _add_common(dr_style)

    dr_compare = director_subs.add_parser(
        "compare-heuristic",
        help="the director cut against the cut the thresholds make")
    dr_compare.add_argument("--name", default="structure")
    dr_compare.add_argument("--keep-threshold", type=float, default=None)
    dr_compare.add_argument("--filler-speed", type=float, default=None)
    dr_compare.add_argument("--handle", type=float, default=None)
    dr_compare.add_argument("--drop-filler", action="store_true")
    _add_common(dr_compare)

    dr_render = director_subs.add_parser(
        "render", help="build the director cut and render it to a proxy")
    dr_render.add_argument("--name", default="structure")
    dr_render.add_argument(
        "--mode", default=None, choices=list(director_schema.MODES),
        help="which selector builds the cut (default director)")
    dr_render.add_argument("--quality", default=None,
                           choices=list(render_schema.QUALITIES))
    dr_render.add_argument("--height", type=int, default=None)
    dr_render.add_argument("--force", action="store_true")
    dr_render.add_argument("--mock", action="store_true",
                           help="render a placeholder instead of a video")
    dr_render.add_argument("--keep-threshold", type=float, default=None)
    dr_render.add_argument("--filler-speed", type=float, default=None)
    dr_render.add_argument("--handle", type=float, default=None)
    dr_render.add_argument("--drop-filler", action="store_true")
    _add_common(dr_render)

    dr_status = director_subs.add_parser(
        "status", help="is a director model reachable, and with what settings")
    _add_director_model_args(dr_status)
    _add_director_shape_args(dr_status)
    _add_common(dr_status)

    dr_clear = director_subs.add_parser(
        "clear-cache", help="drop every cached director answer")
    dr_clear.add_argument("--yes", action="store_true",
                          help="required: each answer costs a model call")
    _add_common(dr_clear)

    for director_parser in (dr_context, dr_plan, dr_report, dr_show,
                            dr_rejected, dr_style, dr_compare, dr_render,
                            dr_status, dr_clear):
        director_parser.add_argument(
            "--run", default="",
            help="scope to one auto run's artifacts")
        director_parser.set_defaults(func=cmd_director)

    # -- render -----------------------------------------------------------
    render = subparsers.add_parser(
        "render",
        help="render a rough cut to a watchable proxy MP4 with FFmpeg "
             "(no Premiere, executes nothing)",
    )
    render_subs = render.add_subparsers(dest="render_command", required=True)

    def _add_render_args(parser: argparse.ArgumentParser) -> None:
        group = parser.add_argument_group("render settings")
        group.add_argument(
            "--quality", default=None,
            choices=list(render_schema.QUALITIES),
            help="proxy is the default: fast, 720p, good enough to judge "
                 "pacing. draft is quicker and looks it")
        group.add_argument(
            "--height", type=int, default=None,
            help="output height (default 720). 0 keeps each source's own "
                 "size, which cannot be joined if they differ")
        group.add_argument(
            "--fps", type=float, default=None,
            help="one frame rate for the whole render (default 30)")
        group.add_argument(
            "--encoder", default=None,
            choices=list(render_schema.VIDEO_ENCODERS),
            help="auto is libx264. A hardware encoder is much faster and "
                 "softer; it falls back if this FFmpeg lacks it")
        group.add_argument("--crf", type=int, default=None,
                           help="override the quality preset's CRF")
        group.add_argument(
            "--scale-mode", dest="scale_mode", default=None,
            choices=list(render_schema.SCALE_MODES),
            help="how a differently-shaped source is fitted (default pad)")
        group.add_argument("--no-audio", action="store_true",
                           help="render silent (you will judge it wrong)")
        group.add_argument(
            "--max-seconds", dest="max_seconds", type=float, default=None,
            help="render only the first N seconds of the cut")
        group.add_argument(
            "--max-segments", dest="max_segments", type=int, default=None,
            help="refuse a cut with more clips than this (default 600)")
        group.add_argument(
            "--notes-interval", dest="notes_interval", type=float,
            default=None,
            help="seconds per review-note section (default: one per clip)")
        group.add_argument("--keep-temp", dest="keep_temp",
                           action="store_true",
                           help="keep the per-clip intermediates")
        group.add_argument("--force", action="store_true",
                           help="re-render even if nothing changed")
        # ``--no-cache`` is not declared here: ``_add_common`` already has it,
        # and turning off the analysis cache and the render cache with one
        # flag is what a person means by it.
        group.add_argument(
            "--dry-run", dest="dry_run", action="store_true",
            help="build every FFmpeg command and run none of them")
        group.add_argument(
            "--mock", action="store_true",
            help="write a placeholder instead of a video, and say so "
                 "everywhere. For exercising the pipeline, never for watching")

    rn_rough = render_subs.add_parser(
        "roughcut", help="render the current rough cut")
    rn_rough.add_argument("--name", default="structure",
                          help="which rough cut (default: structure)")
    rn_rough.add_argument("--plan", default="",
                          help="render a plan file instead of the named one")
    _add_render_args(rn_rough)
    _add_common(rn_rough)

    rn_plan = render_subs.add_parser(
        "from-plan", help="render a rough cut plan from a JSON file")
    rn_plan.add_argument("path", help="the plan written by `roughcut build`")
    _add_render_args(rn_plan)
    _add_common(rn_plan)

    rn_status = render_subs.add_parser(
        "status", help="is FFmpeg there, and what have renders used")
    _add_render_args(rn_status)
    _add_common(rn_status)

    rn_list = render_subs.add_parser("list", help="every render, newest first")
    rn_list.add_argument("--limit", type=int, default=20)
    _add_common(rn_list)

    rn_show = render_subs.add_parser(
        "show", help="one render in full (default: the most recent)")
    rn_show.add_argument("job_id", nargs="?", default="")
    _add_common(rn_show)

    rn_report = render_subs.add_parser(
        "report", help="the render's report.md, regenerated")
    rn_report.add_argument("job_id", nargs="?", default="")
    rn_report.add_argument("--no-save", action="store_true",
                           help="print it without rewriting report.md")
    _add_common(rn_report)

    rn_notes = render_subs.add_parser(
        "notes", help="rewrite a render's review notes, blank again")
    rn_notes.add_argument("job_id", nargs="?", default="")
    _add_common(rn_notes)

    rn_open = render_subs.add_parser(
        "open", help="open the render in whatever plays video here")
    rn_open.add_argument("job_id", nargs="?", default="")
    rn_open.add_argument("--notes", action="store_true",
                         help="open the review notes instead of the video")
    _add_common(rn_open)

    rn_clean = render_subs.add_parser(
        "clean", help="delete renders, or just their intermediates")
    rn_clean.add_argument("--job", default="", help="only this one")
    rn_clean.add_argument("--temp-only", dest="temp_only",
                          action="store_true",
                          help="keep the videos, drop the per-clip files")
    rn_clean.add_argument("--keep-latest", dest="keep_latest", type=int,
                          default=0, help="keep the N most recent renders")
    rn_clean.add_argument("--yes", action="store_true",
                          help="required: this deletes files")
    _add_common(rn_clean)

    for render_parser in (rn_rough, rn_plan, rn_status, rn_list, rn_show,
                          rn_report, rn_notes, rn_open, rn_clean):
        # On every subcommand rather than only the ones that render: an auto
        # run's proxy lives inside that run's artifacts, so finding it again
        # needs the same scoping that making it did.
        render_parser.add_argument(
            "--run", default="",
            help="scope to one auto run's artifacts and its renders")
        render_parser.set_defaults(func=cmd_render)

    # -- feedback ---------------------------------------------------------
    feedback = subparsers.add_parser(
        "feedback",
        help="structured human review of an edit (collects only: it trains "
             "nothing and executes nothing)",
    )
    feedback_subs = feedback.add_subparsers(
        dest="feedback_command", required=True)

    def _add_feedback_common(parser: argparse.ArgumentParser) -> None:
        # On every subcommand rather than only the ones that create things:
        # an auto run's review lives inside that run's artifacts, so reading it
        # back needs the same scoping that writing it did.
        if not any(action.dest == "run" for action in parser._actions):
            parser.add_argument(
                "--run", default="",
                help="scope to one auto run's artifacts and its review")
        parser.add_argument(
            "--session", default="",
            help="which feedback session (default: the most recent)")
        parser.add_argument(
            "--limit", type=int, default=feedback_queue_module.DEFAULT_LIMIT,
            help="how many items to print")
        _add_common(parser)

    def _add_target_flags(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--target-type", default="", choices=[""] + list(
                feedback_schema.TARGET_TYPES),
            help="narrow the ID search to one kind of record")
        parser.add_argument(
            "--allow-unknown", action="store_true",
            help="keep the feedback even when the ID matches no record; it is "
                 "flagged unresolved rather than refused")

    fb_start = feedback_subs.add_parser(
        "start",
        help="open a review session over the current artifacts and build its "
             "queue",
    )
    fb_start.add_argument("--run", default="",
                          help="the auto run this review is about")
    fb_start.add_argument("--name", default="structure",
                          help="which timeline to review")
    fb_start.add_argument("--id", default="",
                          help="session ID to use (default: generated)")
    fb_start.add_argument("--title", default="",
                          help="what this sitting is about")
    fb_start.add_argument("--notes", default="")
    fb_start.add_argument("--no-queue", action="store_true",
                          help="open the session without building a queue")
    fb_start.add_argument(
        "--force", action="store_true",
        help="reuse an existing session folder, but only while it is empty; "
             "feedback is never overwritten")
    _add_feedback_common(fb_start)

    fb_queue = feedback_subs.add_parser(
        "queue", help="what is worth reviewing, ranked and grouped")
    fb_queue.add_argument("--run", default="")
    fb_queue.add_argument("--name", default="structure")
    fb_queue.add_argument(
        "--category", default=None, action="append",
        help="only questions in this reason category (repeatable)")
    fb_queue.add_argument(
        "--source", default=None, action="append",
        help="only questions from this pass: "
             + ", ".join(feedback_schema.PROMPT_SOURCES))
    fb_queue.add_argument("--regenerate", action="store_true",
                          help="rebuild the queue rather than reading it back")
    fb_queue.add_argument("--unanswered", action="store_true",
                          help="hide questions that already have feedback")
    fb_queue.add_argument("--no-positive", action="store_true",
                          help="leave out the sample of good decisions")
    _add_feedback_common(fb_queue)

    fb_show = feedback_subs.add_parser(
        "show", help="one queue question, or one recorded rating, in full")
    fb_show.add_argument("id", help="a prompt ID or a feedback ID")
    _add_feedback_common(fb_show)

    fb_rate = feedback_subs.add_parser(
        "rate", help="record a rating against a decision")
    fb_rate.add_argument(
        "id",
        help="a prompt ID, a record ID, a range like 120-155, or 'whole'")
    fb_rate.add_argument("rating", help="one of: " + ", ".join(
        feedback_schema.RATINGS))
    fb_rate.add_argument(
        "--reason", default=None, action="append",
        help="a reason category (repeatable): "
             + ", ".join(feedback_schema.REASON_CATEGORIES))
    fb_rate.add_argument("--note", default="", help="anything worth saying")
    fb_rate.add_argument("--correction", default="",
                         help="what you would have done instead")
    fb_rate.add_argument(
        "--action", default="", choices=[""] + list(
            feedback_schema.CORRECTION_ACTIONS),
        help="the correction's action, if the text should not be guessed from")
    fb_rate.add_argument("--seconds", type=float, default=None,
                         help="how much, for a correction that has a size")
    fb_rate.add_argument("--priority", type=float, default=None,
                         help="how much this matters to you, 0-1")
    fb_rate.add_argument("--confidence", type=float, default=None,
                         help="how sure you are, 0-1 (default 0.7)")
    fb_rate.add_argument("--no-training", action="store_true",
                         help="keep this out of any future training data")
    fb_rate.add_argument("--follow-up", action="store_true",
                         help="mark it as needing another look")
    fb_rate.add_argument("--run", default="")
    _add_target_flags(fb_rate)
    _add_feedback_common(fb_rate)

    fb_note = feedback_subs.add_parser(
        "note", help="attach an observation with no verdict")
    fb_note.add_argument("id")
    fb_note.add_argument("text")
    fb_note.add_argument("--reason", default=None, action="append")
    fb_note.add_argument("--run", default="")
    _add_target_flags(fb_note)
    _add_feedback_common(fb_note)

    fb_correct = feedback_subs.add_parser(
        "correct", help="say what you would have done instead")
    fb_correct.add_argument("id")
    fb_correct.add_argument("text", help='for example: "cut this shorter"')
    fb_correct.add_argument(
        "--action", default="", choices=[""] + list(
            feedback_schema.CORRECTION_ACTIONS))
    fb_correct.add_argument("--seconds", type=float, default=None,
                            help="how much, signed")
    fb_correct.add_argument("--start", type=float, default=0.0)
    fb_correct.add_argument("--end", type=float, default=0.0)
    fb_correct.add_argument("--run", default="")
    _add_target_flags(fb_correct)
    _add_feedback_common(fb_correct)

    fb_list = feedback_subs.add_parser(
        "list", help="what has been said, or which sessions exist")
    fb_list.add_argument("--sessions", action="store_true",
                         help="list sessions instead of feedback")
    fb_list.add_argument("--history", action="store_true",
                         help="include superseded ratings")
    fb_list.add_argument("--rating", default=None, action="append")
    fb_list.add_argument("--category", default=None, action="append")
    fb_list.add_argument("--target-type", default=None, action="append")
    fb_list.add_argument("--follow-up", action="store_true",
                         help="only the ones still needing something")
    fb_list.add_argument("--training-only", action="store_true",
                         help="only the ones usable as training material")
    _add_feedback_common(fb_list)

    fb_report = feedback_subs.add_parser(
        "report", help="the session report: limits, what was said, signals")
    fb_report.add_argument("--run", default="")
    fb_report.add_argument("--no-save", action="store_true",
                           help="print without writing summary.json/report.md")
    _add_feedback_common(fb_report)

    fb_stats = feedback_subs.add_parser(
        "stats", help="the numbers, without the prose")
    fb_stats.add_argument("--preferences", action="store_true",
                          help="also print the preference signals")
    _add_feedback_common(fb_stats)

    fb_export = feedback_subs.add_parser(
        "export", help="write the feedback out for a dataset builder")
    fb_export.add_argument("out", nargs="?", default="",
                           help="where to write it (default: inside the "
                                "session's exports/ folder)")
    fb_export.add_argument(
        "--format", default="jsonl",
        choices=list(feedback_schema.EXPORT_FORMATS),
        help="jsonl for a dataset builder, json for a person, csv for a "
             "spreadsheet (lossy)")
    fb_export.add_argument(
        "--include", default=None, action="append",
        help="which parts (repeatable): "
             + ", ".join(feedback_schema.EXPORT_PARTS))
    fb_export.add_argument("--history", action="store_true",
                           help="include superseded ratings")
    fb_export.add_argument("--training-only", action="store_true",
                           help="only ratings usable as training material")
    fb_export.add_argument("--run", default="")
    _add_feedback_common(fb_export)

    for feedback_parser in (fb_start, fb_queue, fb_show, fb_rate, fb_note,
                            fb_correct, fb_list, fb_report, fb_stats,
                            fb_export):
        feedback_parser.set_defaults(func=cmd_feedback)

    # -- show / export --------------------------------------------------
    show = subparsers.add_parser("show", help="print a built timeline")
    show.add_argument("--name", default="structure")
    show.add_argument("--limit", type=int, default=60)
    show.add_argument("--highlights", action="store_true",
                      help="only the segments flagged usable, best first")
    _add_common(show)
    show.set_defaults(func=cmd_show)

    export = subparsers.add_parser(
        "export", help="write a built artefact to a path of your choosing")
    export.add_argument(
        "what", nargs="?", default="timeline",
        choices=["timeline", "recommendations", "report", "plan"],
        help="what to export (default: timeline)")
    export.add_argument("--name", default="structure")
    export.add_argument("--out", required=True,
                        help="destination path (.json, or .txt for the report)")
    _add_common(export)
    export.set_defaults(func=cmd_export)

    # -- plan -----------------------------------------------------------
    plan = subparsers.add_parser(
        "plan", help="preview sampling cost without analysing")
    _add_selection(plan)
    _add_sampling(plan)
    _add_common(plan)
    plan.set_defaults(func=cmd_plan)

    # -- cache ----------------------------------------------------------
    cache = subparsers.add_parser("cache", help="inspect or clear the cache")
    cache_subs = cache.add_subparsers(dest="cache_command", required=True)
    cache_info = cache_subs.add_parser("info", help="entry counts and size")
    _add_common(cache_info)
    cache_info.set_defaults(func=cmd_cache, kind=None)
    cache_clear = cache_subs.add_parser("clear", help="delete cache entries")
    cache_clear.add_argument(
        "--kind", choices=["visual", "transcript", "probe", "motion"],
        help="only this kind (default: everything)")
    _add_common(cache_clear)
    cache_clear.set_defaults(func=cmd_cache)

    # -- doctor ---------------------------------------------------------
    doctor = subparsers.add_parser(
        "doctor", help="check FFmpeg, the model server and Premiere")
    _add_model(doctor)
    _add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    # -- run ------------------------------------------------------------
    run = subparsers.add_parser(
        "run", help="discover, transcribe, analyse and build the timeline")
    run.add_argument("--name", default="structure")
    run.add_argument("--keep-frames", action="store_true")
    run.add_argument("--no-motion", action="store_true")
    run.add_argument("--max-windows", type=int)
    run.add_argument("--no-merge", action="store_true")
    run.add_argument("--max-segment-seconds", type=float, default=30.0)
    run.add_argument("--threshold", type=float,
                     default=align.DEFAULT_USABLE_THRESHOLD)
    run.add_argument("--limit", type=int, default=60)
    run.add_argument("--recommend", action="store_true",
                     help="also plan recommendations and dry-run a draft plan")
    run.add_argument("--budget-seconds", type=float, default=20.0)
    run.add_argument("--repeat-gap", type=float, default=12.0)
    run.add_argument("--no-safety", action="store_true", help=argparse.SUPPRESS)
    _add_selection(run)
    _add_sampling(run)
    _add_model(run)
    _add_common(run)
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        return args.func(args)
    except EditingError as exc:
        if getattr(args, "json", False):
            _emit(exc.to_dict())
        else:
            _note(f"error: {exc.message}")
            if exc.hint:
                _note(f"  {exc.hint}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        _note("\nInterrupted. Completed work is cached and will be reused.")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

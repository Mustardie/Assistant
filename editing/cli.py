"""Command line for the editing structure layer.

    python -m editing.cli <command> [options]

Commands, in the order a session normally uses them::

    discover    find footage and map it to the open Premiere project
    transcript  status | pull | import   -- Premiere Speech to Text, or a file
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
    style       list | show <preset>   -- the editing styles available
    layers      build | report | export | dry-run | execute --yes |
                show-deferred | show-density   -- a styled, layered edit

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
from editing.style import presets as style_presets, report as layers_report
from editing.style.compile import CompileOptions
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
    return RoughCutOptions(
        sequence_name=args.sequence,
        keep_threshold=args.keep_threshold,
        filler_speed=args.filler_speed,
        handle=args.handle,
        drop_filler=args.drop_filler,
        allow_zooms=not args.no_zooms,
        preset=args.preset or "",
    )


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
    """The critic pass: frames, critique, revisions, dry run, execution."""
    command = getattr(args, "review_command", None) or "export-frames"
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
        "premiere_transcript":
            premiere_source.probe_support(pipeline.bridge).to_dict(),
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
        epilog="Typical first run:\n"
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

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
from editing.config import SamplingConfig, load_config
from editing.errors import EditingError
from editing.pipeline import Pipeline, build_pipeline
from editing.schema import StructureTimeline
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


def _pipeline(args) -> Pipeline:
    config, sampling = load_config(
        sampling=_sampling_from(args),
        output_dir=Path(args.output_dir) if getattr(args, "output_dir", None) else None,
        vision_backend=getattr(args, "backend", None),
        vision_model=getattr(args, "model", None),
        vision_base_url=getattr(args, "base_url", None),
        use_premiere=(False if getattr(args, "no_premiere", False) else None),
    )
    return build_pipeline(
        config, sampling,
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
    pipeline = _pipeline(args)
    timeline = pipeline.load_timeline(name=args.name)
    target = Path(args.out).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(timeline.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.json:
        _emit({"success": True, "written": str(target), "stats": timeline.stats()})
    else:
        print(f"Wrote {len(timeline.segments)} segment(s) to {target}")
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


def _add_model(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("model")
    group.add_argument("--backend", choices=sorted(set(qwen.BACKENDS) | {"mock"}),
                       help="how to reach the vision model (default openai)")
    group.add_argument("--model", help="model name (default Qwen3-VL-8B-Instruct)")
    group.add_argument("--base-url", help="model server base URL")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m editing.cli",
        description="Editing Brain V1 -- footage structure layer. Produces a "
                    "machine-readable timeline of what happens in Minecraft "
                    "footage and what is said over it. Makes no edits.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Typical first run:\n"
               "  python -m editing.cli doctor\n"
               "  python -m editing.cli discover --folder D:/Footage/ep12\n"
               "  python -m editing.cli transcript status\n"
               "  python -m editing.cli analyze\n"
               "  python -m editing.cli timeline\n",
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

    # -- show / export --------------------------------------------------
    show = subparsers.add_parser("show", help="print a built timeline")
    show.add_argument("--name", default="structure")
    show.add_argument("--limit", type=int, default=60)
    show.add_argument("--highlights", action="store_true",
                      help="only the segments flagged usable, best first")
    _add_common(show)
    show.set_defaults(func=cmd_show)

    export = subparsers.add_parser("export", help="write a timeline to a path")
    export.add_argument("--name", default="structure")
    export.add_argument("--out", required=True, help="destination .json path")
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

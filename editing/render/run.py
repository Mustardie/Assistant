"""One rough cut, rendered.

The order of operations, and why each step is where it is:

1. **Convert** the plan into segments. Pure, and first, so a plan that cannot
   be rendered fails before anything expensive happens.
2. **Check FFmpeg.** Before probing, because probing needs ffprobe -- and a
   missing binary should produce one clear sentence with the install command,
   not a probe failure per source file.
3. **Measure the sources.** Content hashes for the cache key, and a probe for
   the one fact the encoder actually needs: whether each file has an audio
   track.
4. **Key and look in the cache.** The job folder for this key holding a
   finished video *is* the cache entry.
5. **Build every command**, and write them out, before running any of them.
   That is what makes ``--dry-run`` free and ``ffmpeg_commands.json`` an
   honest record.
6. **Encode, then join.** Segment by segment into ``temp/``, then a stream
   copy into ``render.mp4``.
7. **Verify.** Probe the finished file and compare it to what the cut said it
   would be. "The encoder exited 0" is not the same claim as "the video is the
   length it should be".
8. **Write the notes and the report**, then clear the intermediates.

Two things this never does: touch the source footage, and touch Premiere.

A failure at any step leaves the job folder behind with the segments, the
commands and the failure record in it. The intermediates are deliberately kept
after a failure too -- the question that follows a failed join is always
"which segment is wrong", and answering it should not cost another render.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from editing.config import EditingConfig
from editing.errors import EditingError
from editing.render import commands as commands_module
from editing.render import convert, notes as notes_module, report as report_module
from editing.render import sources as sources_module
from editing.render import store
from editing.render.runner import build_runner
from editing.render.schema import (
    INSTALL_HINT, RenderConfig, RenderFailure, RenderInput, RenderJob,
    RenderResult, RenderSegment, job_id_for, now,
)
from editing.roughcut.schema import RoughCutPlan

logger = logging.getLogger("nova.editing.render.run")

Reporter = Callable[[str], None]


def _quiet(_message: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def render_plan(
    config: EditingConfig,
    plan: RoughCutPlan,
    *,
    settings: Optional[RenderConfig] = None,
    plan_name: str = "structure",
    plan_path: str = "",
    runner=None,
    force: bool = False,
    dry_run: bool = False,
    muted_placements: Optional[Iterable[str]] = None,
    source_overrides: Optional[dict] = None,
    say: Reporter = _quiet,
) -> RenderJob:
    """Render one rough cut to a proxy. Returns the job, whatever happened.

    Never raises for anything a person can act on. A missing FFmpeg, a
    disconnected drive, a plan with no clips and a segment FFmpeg refused are
    all *results*: a job with a failure record, a hint, and a folder full of
    everything needed to work out why.
    """
    started = time.time()
    settings = (settings or RenderConfig.from_env()).validated()
    runner = runner or build_runner(config, backend=settings.backend)

    conversion = convert.to_segments(
        plan,
        include_audio=settings.include_audio,
        muted_placements=muted_placements,
        max_seconds=settings.max_seconds,
        source_overrides=source_overrides,
    )
    job = RenderJob(
        status="running",
        config=settings,
        plan_name=plan_name or "structure",
        plan_path=str(plan_path or ""),
        sequence_name=plan.sequence_name,
        segments=conversion.segments,
        created_at=now(),
        started_at=now(),
        warnings=list(settings.warnings) + list(conversion.warnings),
        unsupported=list(conversion.unsupported),
    )

    if not job.segments:
        return _fail(
            config, job, started,
            stage="empty_plan",
            code="nothing_to_render",
            message="This rough cut has no clips to render.",
            hint="Build one first: python -m editing.cli roughcut build",
            recoverable=False,
            plan_hash=sources_module.plan_fingerprint(plan),
        )

    if len(job.segments) > settings.max_segments:
        return _fail(
            config, job, started,
            stage="config",
            code="too_many_segments",
            message=f"The cut has {len(job.segments)} clips, and the limit is "
                    f"{settings.max_segments}.",
            hint="Raise it with --max-segments if that is really what you "
                 "want; a cut this long is usually a selection problem.",
            recoverable=True,
            plan_hash=sources_module.plan_fingerprint(plan),
        )

    if not runner.available():
        job.inputs, source_warnings = sources_module.describe_inputs(
            job.segments, runner=None, probe=False)
        job.warnings.extend(source_warnings)
        return _fail(
            config, job, started,
            stage="missing_ffmpeg",
            code="ffmpeg_missing",
            message="FFmpeg is not installed, or is not on PATH.",
            hint=INSTALL_HINT,
            recoverable=True,
            plan_hash=sources_module.plan_fingerprint(plan),
        )

    say(f"[render] measuring {len(job.segments)} clip(s)...")
    job.inputs, source_warnings = sources_module.describe_inputs(
        job.segments, runner=runner)
    job.warnings.extend(source_warnings)
    job.warnings.extend(sources_module.check_ranges(job.segments, job.inputs))

    missing = job.missing_inputs
    if missing:
        return _fail(
            config, job, started,
            stage="missing_source",
            code="source_missing",
            message=f"{len(missing)} source file(s) named by this cut are not "
                    "on disk.",
            hint="Reconnect the drive holding the footage, or re-run "
                 "discovery and rebuild the rough cut from where it lives "
                 "now.",
            path=missing[0].path,
            recoverable=True,
            plan_hash=sources_module.plan_fingerprint(plan),
            detail={"missing": [item.path for item in missing][:20]},
        )

    settings, encoder_warnings = _resolve_encoder(settings, runner)
    job.config = settings
    job.warnings.extend(encoder_warnings)

    plan_hash = sources_module.plan_fingerprint(plan)
    version = runner.version()
    job.cache_key = sources_module.render_cache_key(
        segments=job.segments, inputs=job.inputs, config=settings,
        plan_hash=plan_hash, ffmpeg_version=version,
    )
    job.job_id = job_id_for(job.plan_name, job.cache_key)

    if settings.use_cache and not force and not dry_run:
        reused = store.cached_job(config, job.job_id, job.cache_key)
        if reused is not None:
            say(f"[render] reusing {reused.output_path}")
            return _finish_cached(config, reused, job)

    directory = store.prepare_job_dir(config, job.job_id)
    job.output_dir = str(directory)
    job.commands = _build_commands(job, directory, ffmpeg=_binary(runner))
    store.write_job(config, job)

    if dry_run:
        job.status = "planned"
        job.ended_at = now()
        job.elapsed = round(time.time() - started, 3)
        job.result = RenderResult(
            job_id=job.job_id, status="planned",
            output_path="", rendered=False,
            segments=len(job.segments),
            planned_duration=job.duration,
            encoder=settings.resolved_encoder,
            ffmpeg_version=version,
            cache_key=job.cache_key,
            created_at=now(),
            warnings=["planned only: no FFmpeg command was run"],
        )
        _write_side_files(config, job)
        store.write_job(config, job)
        say(f"[render] planned {len(job.commands)} command(s); ran none.")
        return job

    return _execute(config, job, runner, started, say=say)


def render_from_file(
    config: EditingConfig,
    path: str | Path,
    *,
    settings: Optional[RenderConfig] = None,
    runner=None,
    force: bool = False,
    dry_run: bool = False,
    say: Reporter = _quiet,
) -> RenderJob:
    """Render a rough cut plan read from a JSON file on disk.

    The seam for rendering a plan that did not come from this machine's
    ``roughcut`` directory -- an exported plan, one from an auto run's
    artifacts, or one somebody edited by hand.
    """
    target = Path(path).expanduser()
    if not target.exists():
        raise EditingError(
            f"No rough cut plan at '{target}'",
            hint="Build one with `python -m editing.cli roughcut build`, or "
                 "point at a plan under data/editing/roughcut/.",
            detail={"path": str(target)},
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise EditingError(
            f"'{target.name}' is not a readable rough cut plan",
            hint="It should be the JSON written by `roughcut build`.",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise EditingError(
            f"'{target.name}' does not contain a rough cut plan",
            hint="Expected a JSON object with a 'placements' list.",
            detail={"path": str(target)},
        )

    plan = RoughCutPlan.from_dict(payload)
    return render_plan(
        config, plan,
        settings=settings,
        plan_name=target.stem or "structure",
        plan_path=str(target),
        runner=runner, force=force, dry_run=dry_run, say=say,
    )


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def _build_commands(
    job: RenderJob, directory: Path, *, ffmpeg: str = "ffmpeg"
) -> list[list[str]]:
    """Every invocation this job will run, in order.

    Built up front rather than lazily so ``--dry-run`` and
    ``ffmpeg_commands.json`` show exactly what a real run does -- a record
    reconstructed after the fact is a record that can be wrong.
    """
    audio_by_path = {item.path: item.has_audio for item in job.inputs}
    temp = store.temp_dir(directory)
    out: list[list[str]] = []
    for segment in job.segments:
        out.append(commands_module.segment_command(
            segment,
            temp / f"{segment.segment_id}.{job.config.container}",
            job.config,
            source_has_audio=audio_by_path.get(segment.source_path, True),
            ffmpeg=ffmpeg,
        ))
    out.append(commands_module.concat_command(
        directory / store.CONCAT_FILE,
        store.output_path(directory, job.config),
        job.config,
        ffmpeg=ffmpeg,
    ))
    return out


def _binary(runner) -> str:
    """The FFmpeg the commands should name.

    Taken from the runner rather than defaulted, because a user with
    ``EDITING_FFMPEG`` pointed at a specific build needs *that* path in
    ``ffmpeg_commands.json`` -- the file is meant to be pasted into a shell,
    and a command naming a binary that is not on PATH cannot be.
    """
    return str(getattr(runner, "ffmpeg", "ffmpeg") or "ffmpeg")


def _execute(
    config: EditingConfig,
    job: RenderJob,
    runner,
    started: float,
    *,
    say: Reporter = _quiet,
) -> RenderJob:
    """Run the commands, join the result, and verify what came out."""
    directory = Path(job.output_dir)
    temp = store.temp_dir(directory)
    log = store.log_path(directory)
    segment_paths: list[Path] = []
    ran = 0

    total = len(job.segments)
    for index, segment in enumerate(job.segments):
        command = job.commands[index]
        target = Path(command[-1])
        say(f"[render] {index + 1}/{total}  "
            f"{Path(segment.source_path).name} "
            f"{segment.source_in:.1f}-{segment.source_out:.1f}s")
        result = runner.run(
            command, timeout=job.config.segment_timeout, log_path=log)
        ran += 1
        if not result.ok or not _has_bytes(target):
            return _fail(
                config, job, started,
                stage="encode_segment",
                code="segment_failed",
                message=f"FFmpeg could not encode clip {index + 1} of "
                        f"{total} ({Path(segment.source_path).name} "
                        f"{segment.source_in:.1f}-{segment.source_out:.1f}s).",
                hint="The clip may use a codec this FFmpeg build cannot "
                     "decode, or the range may be past the end of the file. "
                     "The exact command is in ffmpeg_commands.json and the "
                     "error is in logs/ffmpeg.log.",
                path=segment.source_path,
                command=result.command,
                stderr=result.stderr,
                commands_run=ran,
                keep_temp=True,
            )
        segment_paths.append(target)

    # Sorted by filename as well as appended in order: the segment IDs lead
    # with their index, so both orderings agree and a future refactor that
    # reorders one of them cannot silently reorder the cut.
    list_path = directory / store.CONCAT_FILE
    store.write_text(
        list_path, commands_module.concat_list_text(segment_paths))

    say(f"[render] joining {len(segment_paths)} clip(s)...")
    output = store.output_path(directory, job.config)
    concat = job.commands[-1]
    result = runner.run(concat, timeout=job.config.concat_timeout,
                        log_path=log)
    ran += 1

    if not result.ok or not _has_bytes(output):
        # The fallback: re-encode the join. Stream copy is what *should* work
        # here -- the segments were normalised for exactly that -- so needing
        # this means one of them came out different, and that is worth saying
        # rather than quietly paying for a second encode.
        job.warnings.append(
            "Joining by stream copy failed, so the segments were re-encoded "
            "to join them. That is slower and slightly softer; the FFmpeg "
            "error is in logs/ffmpeg.log."
        )
        say("[render] stream copy failed; re-encoding the join...")
        retry = commands_module.concat_command(
            list_path, output, job.config, reencode=True,
            ffmpeg=_binary(runner))
        job.commands.append(retry)
        result = runner.run(retry, timeout=job.config.concat_timeout,
                            log_path=log)
        ran += 1
        if not result.ok or not _has_bytes(output):
            return _fail(
                config, job, started,
                stage="concat",
                code="concat_failed",
                message="The clips encoded, but would not join into one file.",
                hint="The per-clip files are still in temp/ -- play a few to "
                     "find the odd one out. logs/ffmpeg.log has the error.",
                path=str(output),
                command=result.command,
                stderr=result.stderr,
                commands_run=ran,
                keep_temp=True,
            )

    return _succeed(config, job, runner, started, ran, output, say=say)


def _succeed(
    config: EditingConfig,
    job: RenderJob,
    runner,
    started: float,
    commands_run: int,
    output: Path,
    *,
    say: Reporter = _quiet,
) -> RenderJob:
    """Verify, describe and tidy up after a render that worked."""
    mock = getattr(runner, "name", "") == "mock"
    probe = {}
    try:
        probe = runner.probe(output) or {}
    except Exception as exc:  # noqa: BLE001 - verification is best-effort
        job.warnings.append(
            f"The render finished but could not be probed ({exc}); its "
            "measured length is unknown."
        )

    size = output.stat().st_size if output.exists() else 0
    result = RenderResult(
        job_id=job.job_id,
        status="mocked" if mock else "rendered",
        output_path=str(output),
        # The one claim that matters, and it is never true for a mock.
        rendered=bool(size) and not mock,
        mock=mock,
        segments=len(job.segments),
        planned_duration=job.duration,
        measured_duration=float(probe.get("duration") or 0.0),
        size_bytes=size,
        width=int(probe.get("width") or 0),
        height=int(probe.get("height") or 0),
        fps=float(probe.get("fps") or 0.0),
        has_audio=bool(probe.get("has_audio", False)),
        encoder=job.config.resolved_encoder,
        ffmpeg_version=runner.version(),
        commands_run=commands_run,
        elapsed=round(time.time() - started, 3),
        created_at=now(),
        cache_key=job.cache_key,
    )
    if mock:
        result.warnings.append(
            "MOCK RENDER -- the mock runner wrote a placeholder and no video "
            "was produced. Nothing here is watchable."
        )

    drift = result.duration_drift
    if abs(drift) > 1.5:
        job.warnings.append(
            f"The finished file is {drift:+.2f}s away from the "
            f"{job.duration:.1f}s the cut predicted. Check any speed change, "
            "and whether the sources share a frame rate."
        )
    if job.config.include_audio and not result.has_audio and not mock \
            and probe:
        job.warnings.append(
            "The finished file has no audio stream, though audio was asked "
            "for. Every source may be silent -- logs/ffmpeg.log will say."
        )

    job.status = result.status
    job.result = result
    job.ended_at = now()
    job.elapsed = result.elapsed

    _write_side_files(config, job)
    if not job.config.keep_temp:
        freed = store.clear_temp(job.output_dir)
        if freed:
            logger.debug("Cleared %s bytes of render intermediates", freed)
    result.artifacts = store.collect_artifacts(job)
    store.write_job(config, job)

    say(f"[render] {'placeholder' if mock else 'done'}: {output}")
    return job


def _finish_cached(
    config: EditingConfig, reused: RenderJob, fresh: RenderJob
) -> RenderJob:
    """Hand back a previous render, marked as reused.

    The notes and the report are rewritten from the reused job rather than
    trusted to still be there: somebody who deleted ``review_notes.md``
    because it was full of last week's opinions should get a clean one back,
    not a cache hit with a missing file.
    """
    reused.status = "cached"
    if reused.result is not None:
        reused.result.from_cache = True
        reused.result.status = "cached"
    _write_side_files(config, reused)
    if reused.result is not None:
        reused.result.artifacts = store.collect_artifacts(reused)
    store.write_job(config, reused)
    return reused


def _write_side_files(config: EditingConfig, job: RenderJob) -> None:
    """The review notes and the report. Never fails a render.

    A proxy that rendered and could not write its notes is still a proxy, and
    losing it to a locked markdown file would be absurd.
    """
    try:
        store.write_text(
            store.notes_path(job.output_dir),
            notes_module.render_notes(
                job, interval=job.config.notes_interval),
        )
    except OSError as exc:
        job.warnings.append(f"Could not write the review notes: {exc}")
    try:
        report_module.write_report(job)
    except OSError as exc:
        job.warnings.append(f"Could not write report.md: {exc}")


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------

def _fail(
    config: EditingConfig,
    job: RenderJob,
    started: float,
    *,
    stage: str,
    code: str,
    message: str,
    hint: str = "",
    path: str = "",
    recoverable: bool = True,
    command: Optional[Sequence[str]] = None,
    stderr: str = "",
    commands_run: int = 0,
    keep_temp: bool = False,
    plan_hash: str = "",
    detail: Optional[dict] = None,
) -> RenderJob:
    """Record a failure, write everything about it, and return the job.

    The job folder is written even for a failure that happened before a
    ``job_id`` existed -- one is derived from whatever is known, because a
    failure with nowhere to live is a failure nobody can debug.
    """
    failure = RenderFailure(
        stage=stage, code=code, message=message, hint=hint, path=path,
        recoverable=recoverable,
        command=[str(part) for part in (command or ())],
        stderr=stderr[-2000:],
        detail=dict(detail or {}),
    )
    job.status = "failed"
    job.failure = failure
    job.ended_at = now()
    job.elapsed = round(time.time() - started, 3)

    if not job.cache_key:
        job.cache_key = sources_module.render_cache_key(
            segments=job.segments, inputs=job.inputs, config=job.config,
            plan_hash=plan_hash,
        )
    if not job.job_id:
        job.job_id = job_id_for(job.plan_name, job.cache_key)

    job.result = RenderResult(
        job_id=job.job_id,
        status="failed",
        rendered=False,
        segments=len(job.segments),
        planned_duration=job.duration,
        commands_run=commands_run,
        elapsed=job.elapsed,
        created_at=now(),
        cache_key=job.cache_key,
        failure=failure,
        warnings=list(job.warnings),
    )
    try:
        store.write_job(config, job)
        _write_side_files(config, job)
        if not keep_temp and not job.config.keep_temp:
            store.clear_temp(job.output_dir)
        job.result.artifacts = store.collect_artifacts(job)
        store.write_job(config, job)
    except OSError as exc:  # pragma: no cover - a failure while recording one
        logger.warning("Could not record the render failure: %s", exc)
    return job


def _resolve_encoder(
    settings: RenderConfig, runner
) -> tuple[RenderConfig, list[str]]:
    """Fall back to software encoding when the chosen encoder is not there.

    The realistic case: somebody sets ``--encoder h264_nvenc`` on a machine
    whose FFmpeg was built without NVENC. FFmpeg's own error for that is
    "Unknown encoder", four minutes into a render; falling back with a warning
    produces a proxy.
    """
    from dataclasses import replace

    wanted = settings.resolved_encoder
    try:
        available = runner.encoders()
    except Exception:  # noqa: BLE001 - probing capability must not fail a run
        return settings, []
    if not available or wanted in available:
        return settings, []
    return (
        replace(settings, video_encoder="libx264").validated(),
        [f"'{wanted}' is not in this FFmpeg build, so libx264 was used "
         "instead. It is slower and looks slightly better."],
    )


def _has_bytes(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False

"""Transcribing one file, and transcribing a folder without losing the folder.

Two entry points, and the difference between them is entirely about failure.

``transcribe_file`` raises. One file, one answer, and a caller who asked for a
specific thing deserves to hear precisely why it did not happen.

``transcribe_folder`` never raises for a file. Thirty clips where two are
corrupt is an ordinary afternoon, and the useful output is twenty-eight
transcripts plus an exact account of the two. A batch that aborted on file
three would waste the forty minutes it had already spent.

## The order of attempts

1. **Cache.** Keyed on content hash plus every setting that changes a word.
2. **Direct decode.** faster-whisper reads most containers itself; handing it
   the video path is the fast path and the normal one.
3. **Extract, then decode.** Only after a direct decode fails. FFmpeg writes a
   16 kHz mono WAV into the cache -- never beside the footage -- and the decode
   is retried against that. This is why a missing FFmpeg is not fatal on its
   own: most files never reach step 3.

## What "skipped" means

A file with a valid, current transcript already in the durable store is
*skipped*, not re-transcribed. That is what makes ``auto run --transcribe``
safe to leave on: the second run over a folder costs nothing. ``--force``
overrides it, and a source whose content hash changed is never skipped, because
the fingerprint says the audio is not the audio that transcript was made from.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from editing.cache import Cache
from editing.config import EditingConfig
from editing.errors import EditingError, ToolMissingError
from editing.fingerprint import Fingerprint, asset_id_for, fingerprint
from editing.schema import MediaAsset
from editing.transcribe import audio as audio_module
from editing.transcribe import store as store_module
from editing.transcribe.backends import build_backend
from editing.transcribe.schema import (
    TranscriptionBatch, TranscriptionConfig, TranscriptionFailure,
    TranscriptionJob, TranscriptionResult, job_id_for, now,
)

logger = logging.getLogger("nova.editing.transcribe.run")

Reporter = Callable[[str], None]


def _quiet(_message: str) -> None:
    return None


def _asset_for(path: Path, mark: Fingerprint) -> MediaAsset:
    """A minimal asset for a path, when discovery has not been run.

    ``duration`` is left at zero deliberately: it is only used to clamp
    transcript entries, and clamping to a *guess* would silently truncate the
    last line of every transcript. The backend reports the real duration, and
    ``publish`` uses the asset's only when it is genuine.
    """
    return MediaAsset(
        asset_id=mark.asset_id,
        path=str(path),
        filename=path.name,
        duration=0.0,
    )


# ---------------------------------------------------------------------------
# One file
# ---------------------------------------------------------------------------

def transcribe_file(
    config: EditingConfig,
    path: str | Path,
    *,
    settings: Optional[TranscriptionConfig] = None,
    cache: Optional[Cache] = None,
    asset: Optional[MediaAsset] = None,
    backend=None,
    force: bool = False,
    extract_audio: bool = False,
    publish: bool = True,
    write_files: bool = True,
    say: Reporter = _quiet,
    progress=None,
) -> TranscriptionJob:
    """Transcribe one file. Raises for anything that stopped it.

    ``backend`` is passed in by the batch path so one model load serves a whole
    folder; on its own this builds one and lets it go afterwards.
    """
    started = time.time()
    resolved = (settings or TranscriptionConfig.from_env()).validated()
    source = audio_module.check_readable(path)
    mark = fingerprint(source)
    media_asset = asset or _asset_for(source, mark)

    cache = cache if cache is not None else Cache(root=config.cache_dir)
    key = store_module.cache_key(cache, mark, resolved)
    job = TranscriptionJob(
        job_id=job_id_for(media_asset.asset_id, str(source), key),
        source_path=str(source),
        asset_id=media_asset.asset_id,
        status="running",
        config=resolved,
        created_at=now(),
        started_at=now(),
        cache_key=key,
    )

    result: Optional[TranscriptionResult] = None
    if not force:
        result = store_module.cached_result(cache, key, settings=resolved)
        if result is not None:
            job.status = "cached"
            say(f"  {source.name}: reusing cached transcript "
                f"({len(result)} segment(s))")

    if result is None:
        result = _decode(
            source, resolved,
            config=config, cache=cache, mark=mark, backend=backend,
            extract_audio=extract_audio, say=say, progress=progress,
        )
        job.status = "done"
        if resolved.use_cache and not result.is_empty:
            store_module.store_result(
                cache, key, result, mark=mark, settings=resolved)

    result.job_id = job.job_id
    result.asset_id = media_asset.asset_id
    job.result = result
    job.warnings = list(result.warnings)
    job.elapsed = round(time.time() - started, 3)
    job.ended_at = now()

    if write_files:
        store_module.write_job(config, job, result)
    if publish and not result.is_empty:
        store_module.publish(
            config, result, asset=media_asset, mark=mark, cache=cache)

    stats = result.stats()
    say(f"  {source.name}: {stats['segments']} segment(s), "
        f"{stats['words']} word(s), {stats['speech_share']:.0%} speech"
        + ("  [MOCK]" if result.mock else ""))
    for warning in result.warnings:
        say(f"    ! {warning}")
    return job


def _decode(
    source: Path,
    settings: TranscriptionConfig,
    *,
    config: EditingConfig,
    cache: Cache,
    mark: Fingerprint,
    backend=None,
    extract_audio: bool = False,
    say: Reporter = _quiet,
    progress=None,
) -> TranscriptionResult:
    """Direct decode, then extraction as a fallback. Raises on both failing."""
    engine = backend if backend is not None else build_backend(settings)
    media, extracted = audio_module.prepare(
        source,
        cache_dir=store_module.audio_cache_dir(config),
        force_extract=extract_audio,
        fingerprint_key=mark.content_hash,
        ffmpeg=config.ffmpeg,
    )

    try:
        return engine.transcribe(media, config=settings, progress=progress)
    except ToolMissingError:
        # faster-whisper itself is missing; extraction cannot help.
        raise
    except EditingError as first:
        if extracted:
            raise
        say(f"  {source.name}: direct decode failed, extracting audio first")
        logger.info("Direct decode of %s failed (%s); extracting",
                    source, first.message)
        try:
            media, _ = audio_module.prepare(
                source,
                cache_dir=store_module.audio_cache_dir(config),
                force_extract=True,
                fingerprint_key=mark.content_hash,
                ffmpeg=config.ffmpeg,
            )
        except EditingError as second:
            # Both paths failed. The first error is the more informative one,
            # so it leads, and the extraction failure is attached rather than
            # replacing it.
            raise EditingError(
                f"Could not decode '{source.name}', with or without "
                "extracting the audio first",
                hint=second.hint or first.hint,
                detail={"direct": first.message, "extraction": second.message,
                        "path": str(source)},
            ) from second
        return engine.transcribe(media, config=settings, progress=progress)


# ---------------------------------------------------------------------------
# A folder
# ---------------------------------------------------------------------------

def transcribe_folder(
    config: EditingConfig,
    root: str | Path,
    *,
    settings: Optional[TranscriptionConfig] = None,
    cache: Optional[Cache] = None,
    assets: Optional[Sequence[MediaAsset]] = None,
    recursive: bool = True,
    force: bool = False,
    extract_audio: bool = False,
    publish: bool = True,
    skip_existing: bool = True,
    limit: int = 0,
    say: Reporter = _quiet,
) -> TranscriptionBatch:
    """Transcribe every media file under ``root``. Never raises for a file.

    ``assets`` lets a caller pass what discovery already found, so an auto run
    transcribes exactly the clips it is about to analyse rather than everything
    that happens to be in the folder.
    """
    resolved = (settings or TranscriptionConfig.from_env()).validated()
    cache = cache if cache is not None else Cache(root=config.cache_dir)

    if assets is not None:
        files = [Path(asset.path) for asset in assets]
        by_path = {str(Path(a.path)).lower(): a for a in assets}
    else:
        files = audio_module.find_media(root, recursive=recursive)
        by_path = {}

    if limit > 0:
        files = files[:limit]

    batch = TranscriptionBatch(
        batch_id=store_module.batch_id_for(root),
        root=str(root),
        created_at=now(),
        config=resolved,
    )
    if not files:
        batch.warnings.append(
            f"no media files found under '{root}'. Supported extensions: "
            + ", ".join(audio_module.MEDIA_EXTENSIONS)
        )
        batch.finished_at = now()
        store_module.write_batch(config, batch)
        say(f"No media files found under {root}")
        return batch

    batch.warnings.extend(resolved.warnings)
    say(f"Transcribing {len(files)} file(s) with {resolved.backend}/"
        f"{resolved.model}...")

    # One backend for the whole batch: loading a Whisper model takes seconds
    # and hundreds of megabytes, and paying that per file would dominate the
    # runtime of a folder of short clips.
    engine = build_backend(resolved)
    try:
        for index, media in enumerate(files, start=1):
            say(f"[{index}/{len(files)}] {media.name}")
            asset = by_path.get(str(media).lower())
            batch.jobs.append(_one_of_batch(
                config, media,
                settings=resolved, cache=cache, asset=asset, backend=engine,
                force=force, extract_audio=extract_audio, publish=publish,
                skip_existing=skip_existing, say=say,
            ))
    finally:
        engine.close()

    batch.finished_at = now()
    store_module.write_batch(config, batch)
    _say_summary(batch, say)
    return batch


def _one_of_batch(
    config: EditingConfig,
    media: Path,
    *,
    settings: TranscriptionConfig,
    cache: Cache,
    asset: Optional[MediaAsset],
    backend,
    force: bool,
    extract_audio: bool,
    publish: bool,
    skip_existing: bool,
    say: Reporter,
) -> TranscriptionJob:
    """One file inside a batch, with every failure turned into a record."""
    try:
        if skip_existing and not force:
            existing = _existing_transcript(config, media, asset)
            if existing is not None:
                say(f"  {media.name}: already has a transcript "
                    f"({existing} line(s)); skipping")
                return TranscriptionJob(
                    job_id=f"skipped-{asset_id_for(media)[:10]}",
                    source_path=str(media),
                    asset_id=asset.asset_id if asset else asset_id_for(media),
                    status="skipped",
                    config=settings,
                    created_at=now(),
                    ended_at=now(),
                    warnings=[
                        f"a current transcript with {existing} line(s) already "
                        "exists; --force re-transcribes"
                    ],
                )

        return transcribe_file(
            config, media,
            settings=settings, cache=cache, asset=asset, backend=backend,
            force=force, extract_audio=extract_audio, publish=publish,
            say=say,
        )
    except EditingError as exc:
        say(f"  {media.name}: FAILED -- {exc.message}")
        return _failed_job(media, asset, settings, _failure_from(exc, media))
    except Exception as exc:  # noqa: BLE001 - a bug is still a batch outcome
        logger.exception("Unexpected failure transcribing %s", media)
        say(f"  {media.name}: FAILED -- {type(exc).__name__}: {exc}")
        return _failed_job(media, asset, settings, TranscriptionFailure(
            stage="unknown",
            code="internal_error",
            message=f"{type(exc).__name__}: {exc}",
            hint="This is a bug in the transcription layer rather than a "
                 "problem with the file.",
            path=str(media),
        ))


def _existing_transcript(
    config: EditingConfig, media: Path, asset: Optional[MediaAsset]
) -> Optional[int]:
    """Line count of a current durable transcript for this file, or ``None``.

    Stale transcripts do not count. A transcript made from different audio is
    exactly the thing that should be redone, and treating it as present would
    make the staleness invisible for the rest of the run.
    """
    from editing.transcripts import store as transcript_store

    asset_id = asset.asset_id if asset else asset_id_for(media)
    try:
        mark = fingerprint(media)
    except EditingError:
        return None
    stored, stale = transcript_store.load(config, asset_id, mark=mark)
    if stored is None or not len(stored) or stale:
        return None
    return len(stored)


def _failed_job(
    media: Path,
    asset: Optional[MediaAsset],
    settings: TranscriptionConfig,
    failure: TranscriptionFailure,
) -> TranscriptionJob:
    return TranscriptionJob(
        job_id=f"failed-{asset_id_for(media)[:10]}",
        source_path=str(media),
        asset_id=asset.asset_id if asset else asset_id_for(media),
        status="failed",
        config=settings,
        created_at=now(),
        ended_at=now(),
        failure=failure,
        warnings=[failure.message],
    )


#: Error text -> the stage it belongs to. Matched on the message because the
#: underlying libraries raise a dozen exception types for the same few real
#: problems, and the *fix* is what the stage is for.
_STAGE_MARKERS = (
    ("faster-whisper is not installed", "missing_backend"),
    ("ffmpeg is needed", "missing_ffmpeg"),
    ("is not installed or not on path", "missing_ffmpeg"),
    ("could not extract audio", "extract_audio"),
    ("could not load whisper model", "load_model"),
    ("does not exist", "read_media"),
    ("is not a media file", "read_media"),
    ("is empty", "read_media"),
    ("could not be read", "read_media"),
    ("produced no usable lines", "empty"),
    ("could not decode", "decode"),
    ("could not read", "decode"),
)


def _failure_from(exc: EditingError, media: Path) -> TranscriptionFailure:
    lowered = str(exc.message or "").lower()
    stage = "unknown"
    for marker, name in _STAGE_MARKERS:
        if marker in lowered:
            stage = name
            break
    return TranscriptionFailure(
        stage=stage,
        code=getattr(exc, "code", "transcription_failed"),
        message=exc.message,
        hint=exc.hint,
        path=str(media),
        # A file that is not media, or is empty, will not become one by being
        # retried; everything else plausibly works after installing something.
        recoverable=stage not in ("read_media",),
        detail=exc.detail if isinstance(exc.detail, dict) else {},
    )


def _say_summary(batch: TranscriptionBatch, say: Reporter) -> None:
    stats = batch.stats()
    say("")
    say(f"Transcribed {stats['done']} file(s), reused {stats['cached']}, "
        f"skipped {stats['skipped']}, failed {stats['failed']}.")
    if stats["segments"]:
        say(f"  {stats['segments']} segment(s), {stats['words']} word(s) "
            f"across {stats['media_seconds']:.0f}s of media "
            f"in {stats['elapsed']:.0f}s.")
    for job in batch.failed:
        if job.failure is not None:
            say(f"  x {Path(job.source_path).name}: {job.failure.message}")
            if job.failure.hint:
                say(f"      {job.failure.hint}")
    for warning in batch.warnings:
        say(f"  ! {warning}")


# ---------------------------------------------------------------------------
# Asking whether it is worth running
# ---------------------------------------------------------------------------

def missing_transcripts(
    config: EditingConfig, assets: Sequence[MediaAsset]
) -> list[MediaAsset]:
    """The assets with no current transcript. What the auto stage acts on."""
    out: list[MediaAsset] = []
    for asset in assets:
        if _existing_transcript(config, Path(asset.path), asset) is None:
            out.append(asset)
    return out

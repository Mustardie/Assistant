"""Where a transcription lands, and how a repeat run avoids paying twice.

## The job folder

``data/editing/transcripts/<job_id>/``:

```
transcript.json   the result in full: segments, words, probabilities
transcript.srt    subtitles, standard SRT
transcript.txt    readable, with a provenance header
metadata.json     the job: config, timings, device, cache key
warnings.json     everything the run wanted to say, in one place
```

The job ID is derived from the **cache key**, not from a timestamp. So the same
file transcribed twice with the same settings lands in the same folder and
overwrites it -- it is the same answer -- while changing the model or the
language produces a different folder. A timestamped ID would leave a trail of
identical folders and make ``transcribe show`` a guessing game.

``transcript.json`` carries both a ``segments`` list and an ``entries`` list.
The first is the rich form; the second is the shape ``editing.schema.Transcript``
consumes. Both are keys the existing normaliser already looks for, so this file
parses with ``transcripts.normalize.parse_json`` and no bridge.

## Two stores, and the durable one wins

The job folder is the *record of a transcription*. ``transcripts/<asset_id>.json``
-- written by ``editing.transcripts.store`` -- is the *durable transcript for an
asset*, which is what ``resolve()`` reads and therefore what the whole pipeline
sees. Producing a transcript writes both, and that second write is the actual
seam into the rest of the system.

## The cache

Keyed on the file's fingerprint plus everything about the configuration that
changes a word of output. Because the fingerprint carries a content hash, a
re-encoded or re-exported file misses correctly rather than serving a
transcript of the old audio.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from editing.cache import Cache
from editing.config import EditingConfig
from editing.errors import EditingError
from editing.fingerprint import Fingerprint
from editing.transcribe.schema import (
    TranscriptionBatch, TranscriptionCacheEntry, TranscriptionConfig,
    TranscriptionJob, TranscriptionResult,
)

logger = logging.getLogger("nova.editing.transcribe.store")

RESULT_FILE = "transcript.json"
SRT_FILE = "transcript.srt"
TXT_FILE = "transcript.txt"
METADATA_FILE = "metadata.json"
WARNINGS_FILE = "warnings.json"
BATCH_FILE = "batch.json"

#: Cache namespace. Separate from the ``transcript`` kind the Premiere/sidecar
#: path uses, because these are different products of different inputs.
CACHE_KIND = "transcribe"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def jobs_root(config: EditingConfig) -> Path:
    return config.transcripts_dir


def job_dir(config: EditingConfig, job_id: str) -> Path:
    return jobs_root(config) / job_id


def batches_root(config: EditingConfig) -> Path:
    return config.transcripts_dir / "_batches"


def audio_cache_dir(config: EditingConfig) -> Path:
    return config.cache_dir / "transcribe"


def list_jobs(config: EditingConfig, *, limit: int = 100) -> list[TranscriptionJob]:
    """Every job on disk, newest first, unreadable ones skipped.

    Directories only: ``transcripts/`` also holds ``<asset_id>.json`` durable
    transcripts as *files*, and those are a different thing.
    """
    root = jobs_root(config)
    if not root.exists():
        return []
    found: list[tuple[float, TranscriptionJob]] = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name.startswith("_"):
            continue
        target = directory / METADATA_FILE
        if not target.exists():
            continue
        try:
            job = TranscriptionJob.from_dict(_read_json(target))
        except (ValueError, OSError):
            continue
        try:
            stamp = target.stat().st_mtime
        except OSError:
            stamp = 0.0
        found.append((stamp, job))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [job for _stamp, job in found[:limit]]


def load_job(config: EditingConfig, job_id: str) -> TranscriptionJob:
    target = job_dir(config, job_id) / METADATA_FILE
    if not target.exists():
        raise EditingError(
            f"No transcription job called '{job_id}'",
            hint="List them with `python -m editing.cli transcribe status`.",
            detail={"path": str(target)},
        )
    return TranscriptionJob.from_dict(_read_json(target))


def load_result(config: EditingConfig, job_id: str) -> TranscriptionResult:
    target = job_dir(config, job_id) / RESULT_FILE
    if not target.exists():
        raise EditingError(
            f"Job '{job_id}' has no transcript on disk",
            hint="Re-run it with `transcribe file <path> --force`.",
            detail={"path": str(target)},
        )
    return TranscriptionResult.from_dict(_read_json(target))


def find_job_for(
    config: EditingConfig, source_path: str | Path
) -> Optional[TranscriptionJob]:
    """The most recent job for one source file, whatever settings it used."""
    wanted = str(Path(source_path).expanduser()).lower()
    for job in list_jobs(config):
        if job.source_path.lower() == wanted:
            return job
    return None


# ---------------------------------------------------------------------------
# Writing a job
# ---------------------------------------------------------------------------

def write_job(
    config: EditingConfig,
    job: TranscriptionJob,
    result: Optional[TranscriptionResult] = None,
    *,
    write_srt: bool = True,
    write_txt: bool = True,
) -> Path:
    """Write everything one job produced. Returns the folder.

    A failed job still gets a folder with ``metadata.json`` and
    ``warnings.json`` in it. "Why did clip_07 fail" has to be answerable
    afterwards, and a failure that leaves nothing on disk is a failure nobody
    can debug.
    """
    from editing.transcribe import formats

    directory = job_dir(config, job.job_id)
    directory.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(directory)

    if result is not None:
        _write_json(directory / RESULT_FILE, result.to_dict())
        if write_srt:
            (directory / SRT_FILE).write_text(
                formats.render_srt(result.segments), encoding="utf-8")
        if write_txt:
            (directory / TXT_FILE).write_text(
                formats.render_txt(result), encoding="utf-8")

    _write_json(directory / METADATA_FILE, job.to_dict())
    _write_json(directory / WARNINGS_FILE, {
        "job_id": job.job_id,
        "source_path": job.source_path,
        "status": job.status,
        "warnings": list(job.warnings) + (
            list(result.warnings) if result else []),
        "segment_warnings": [
            {"index": segment.index, "start": round(segment.start, 3),
             "warnings": segment.warnings}
            for segment in (result.segments if result else [])
            if segment.warnings
        ][:200],
        "failure": job.failure.to_dict() if job.failure else None,
    })
    return directory


def export_job(
    config: EditingConfig,
    job_id: str,
    destination: str | Path,
    *,
    fmt: str = "srt",
) -> Path:
    """Copy one job's output somewhere the user chose."""
    from editing.transcribe import formats

    result = load_result(config, job_id)
    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)

    chosen = (fmt or "srt").lower().lstrip(".")
    if chosen == "srt":
        target.write_text(formats.render_srt(result.segments), encoding="utf-8")
    elif chosen == "vtt":
        target.write_text(formats.render_vtt(result.segments), encoding="utf-8")
    elif chosen == "txt":
        target.write_text(formats.render_txt(result), encoding="utf-8")
    elif chosen == "json":
        _write_json(target, result.to_dict())
    else:
        raise EditingError(
            f"'{fmt}' is not an export format for a transcript",
            hint="Formats: srt, vtt, txt, json.",
        )
    return target


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------

def write_batch(config: EditingConfig, batch: TranscriptionBatch) -> Path:
    directory = batches_root(config)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{batch.batch_id}.json"
    _write_json(target, batch.to_dict())
    return target


def list_batches(
    config: EditingConfig, *, limit: int = 20
) -> list[TranscriptionBatch]:
    directory = batches_root(config)
    if not directory.exists():
        return []
    out: list[TranscriptionBatch] = []
    for path in sorted(directory.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(TranscriptionBatch.from_dict(_read_json(path)))
        except (ValueError, OSError):
            continue
    return out


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

def cache_key(
    cache: Cache, mark: Fingerprint, settings: TranscriptionConfig
) -> str:
    """The key for one file under one configuration.

    ``mark.cache_key_part()`` carries the content hash, so a re-encode or a
    re-export misses correctly instead of serving a transcript of the old
    audio. Everything from the config that changes a word of output is in
    ``cache_key_part``; nothing that does not, is.
    """
    return cache.key(
        CACHE_KIND,
        file=mark.cache_key_part(),
        settings=settings.cache_key_part(),
    )


def cached_result(
    cache: Cache, key: str, *, settings: TranscriptionConfig
) -> Optional[TranscriptionResult]:
    """A stored result for this key, or ``None``.

    A stored entry that will not parse is a miss, not a crash: a half-written
    entry from an interrupted run should cost one re-transcription and nothing
    else.
    """
    if not settings.use_cache:
        return None
    payload = cache.get(CACHE_KIND, key)
    if not payload:
        return None
    try:
        result = TranscriptionResult.from_dict(payload)
    except Exception as exc:  # noqa: BLE001 - a bad entry is a miss, never a crash
        logger.warning("Ignoring unusable cached transcription: %s", exc)
        return None
    if result.is_empty:
        # An empty transcript is rarely the right answer and is cheap to
        # retry; caching it would make one bad run permanent.
        return None
    result.cached = True
    return result


def store_result(
    cache: Cache,
    key: str,
    result: TranscriptionResult,
    *,
    mark: Fingerprint,
    settings: TranscriptionConfig,
) -> TranscriptionCacheEntry:
    """Cache a result and describe what was cached."""
    entry = TranscriptionCacheEntry.describe(
        key, fingerprint=mark, config=settings, result=result)
    cache.put(
        CACHE_KIND, key, result.to_dict(),
        meta={
            "source_path": result.source_path,
            "model": result.model,
            "segments": len(result),
            "entry": entry.to_dict(),
        },
    )
    return entry


def clear_cache(cache: Cache) -> int:
    """Drop every cached transcription. Returns how many entries went."""
    return cache.clear(CACHE_KIND)


# ---------------------------------------------------------------------------
# The seam into the rest of the system
# ---------------------------------------------------------------------------

def publish(
    config: EditingConfig,
    result: TranscriptionResult,
    *,
    asset,
    mark: Optional[Fingerprint] = None,
    cache: Optional[Cache] = None,
) -> Path:
    """Write the durable transcript every other pass reads.

    This is the actual integration point. Everything else this package writes
    is a record of *how* the transcript was made; this is the transcript, in
    the place ``transcripts.store.resolve()`` looks first.

    Entries go through ``normalize_entries`` on the way, so a machine
    transcript gets the same sorting, zero-length repair and duplicate merging
    that an imported SRT does. Skipping it would make Whisper output the one
    transcript source in the system that had not been normalised.
    """
    from editing.transcripts import normalize, store as transcript_store

    transcript = result.as_transcript(asset_id=asset.asset_id)
    transcript.entries = normalize.normalize_entries(
        transcript.entries, max_duration=asset.duration or None
    )
    if not transcript.entries:
        raise EditingError(
            f"Transcription of '{Path(result.source_path).name}' produced no "
            "usable lines",
            hint="The file may have no speech, or the audio track may be "
                 "silent. `transcribe show <job_id>` has the detail.",
            detail={"path": result.source_path,
                    "segments": len(result.segments)},
        )
    return transcript_store.save(config, transcript, mark=mark, cache=cache)


# ---------------------------------------------------------------------------
# JSON, in one place
# ---------------------------------------------------------------------------

def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    # Via a temp file in the same directory, so an interrupted write cannot
    # leave a half-written transcript where a valid one used to be.
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def batch_id_for(root: str | Path) -> str:
    from editing.schema import short_hash
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{short_hash(str(root), length=6)}"

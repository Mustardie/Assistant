"""Where a render lands, and how a repeat render avoids paying twice.

## The job folder

``data/editing/render/jobs/<job_id>/``::

    config.json           the settings, exactly as validated
    segments.json         every source range, in play order
    ffmpeg_commands.json  every invocation, in order, as it was run
    render.mp4            the proxy
    review_notes.md       timestamped sections to write on while watching
    report.md             what was produced and what could not be represented
    result.json           the machine-readable result
    logs/ffmpeg.log       stdout and stderr from every invocation
    temp/                 per-segment intermediates, deleted on success

The job ID is derived from the **cache key**, not from a timestamp. So the
same cut rendered twice with the same settings lands in the same folder, and
that folder holding a finished video *is* the cache entry. There is no second
place for the cache and the output to disagree, which is the failure mode a
separate cache index would introduce.

## Temp files

The per-segment files are the whole render, twice over -- a ten-minute proxy
leaves about the same again in ``temp/``. They are deleted after a successful
concat unless ``--keep-temp`` is passed, and they are deliberately *kept*
after a failure: the usual question after a failed join is "which segment is
broken", and answering it should not require rendering everything again.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Iterable, Optional

from editing.config import EditingConfig
from editing.errors import EditingError
from editing.render.schema import (
    RenderArtifact, RenderConfig, RenderJob, RenderResult, RenderSegment,
)

logger = logging.getLogger("nova.editing.render.store")

CONFIG_FILE = "config.json"
SEGMENTS_FILE = "segments.json"
COMMANDS_FILE = "ffmpeg_commands.json"
RESULT_FILE = "result.json"
JOB_FILE = "job.json"
NOTES_FILE = "review_notes.md"
REPORT_FILE = "report.md"
CONCAT_FILE = "concat.txt"
LOG_NAME = "ffmpeg.log"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def render_root(config: EditingConfig) -> Path:
    return config.render_dir


def jobs_root(config: EditingConfig) -> Path:
    return config.render_dir / "jobs"


def job_dir(config: EditingConfig, job_id: str) -> Path:
    return jobs_root(config) / job_id


def temp_dir(directory: str | Path) -> Path:
    return Path(directory) / "temp"


def logs_dir(directory: str | Path) -> Path:
    return Path(directory) / "logs"


def log_path(directory: str | Path) -> Path:
    return logs_dir(directory) / LOG_NAME


def output_path(directory: str | Path, config: RenderConfig) -> Path:
    return Path(directory) / f"render.{config.container}"


def notes_path(directory: str | Path) -> Path:
    return Path(directory) / NOTES_FILE


def report_path(directory: str | Path) -> Path:
    return Path(directory) / REPORT_FILE


def prepare_job_dir(config: EditingConfig, job_id: str) -> Path:
    """The folder for one job, with its subfolders, created."""
    directory = job_dir(config, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    temp_dir(directory).mkdir(parents=True, exist_ok=True)
    logs_dir(directory).mkdir(parents=True, exist_ok=True)
    return directory


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def list_jobs(config: EditingConfig, *, limit: int = 50) -> list[RenderJob]:
    """Every render on disk, newest first, unreadable ones skipped."""
    root = jobs_root(config)
    if not root.exists():
        return []
    found: list[tuple[float, RenderJob]] = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name.startswith("_"):
            continue
        target = directory / JOB_FILE
        if not target.exists():
            continue
        try:
            job = _load_job_file(target)
        except (ValueError, OSError):
            continue
        try:
            stamp = target.stat().st_mtime
        except OSError:
            stamp = 0.0
        found.append((stamp, job))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [job for _stamp, job in found[:limit]]


def load_job(config: EditingConfig, job_id: str) -> RenderJob:
    target = job_dir(config, job_id) / JOB_FILE
    if not target.exists():
        raise EditingError(
            f"No render job called '{job_id}'",
            hint="List them with `python -m editing.cli render list`.",
            detail={"path": str(target)},
        )
    return _load_job_file(target)


def latest_job(config: EditingConfig) -> Optional[RenderJob]:
    jobs = list_jobs(config, limit=1)
    return jobs[0] if jobs else None


def resolve_job(config: EditingConfig, job_id: str = "") -> RenderJob:
    """One job by ID, or the most recent one. Raises when there is neither."""
    if job_id:
        return load_job(config, job_id)
    job = latest_job(config)
    if job is None:
        raise EditingError(
            "Nothing has been rendered yet",
            hint="Render the current rough cut with "
                 "`python -m editing.cli render roughcut`.",
            detail={"looked_in": str(jobs_root(config))},
        )
    return job


def load_result(config: EditingConfig, job_id: str) -> RenderResult:
    target = job_dir(config, job_id) / RESULT_FILE
    if not target.exists():
        raise EditingError(
            f"Render job '{job_id}' has no result on disk",
            hint="Re-run it with `render roughcut --force`.",
            detail={"path": str(target)},
        )
    return RenderResult.from_dict(_read_json(target))


def _load_job_file(target: Path) -> RenderJob:
    """A job, with its segments read back from their own file.

    ``job.json`` deliberately does not embed the segments: a four-hundred
    segment cut makes the job record unreadable, and ``segments.json`` is the
    file a person actually opens when they want to know what got rendered.
    """
    job = RenderJob.from_dict(_read_json(target))
    segments_file = target.parent / SEGMENTS_FILE
    if segments_file.exists():
        try:
            payload = _read_json(segments_file)
        except (ValueError, OSError):
            payload = {}
        job.segments = [
            RenderSegment.from_dict(item)
            for item in (payload.get("segments") or [])
            if isinstance(item, dict)
        ]
    commands_file = target.parent / COMMANDS_FILE
    if commands_file.exists():
        try:
            payload = _read_json(commands_file)
        except (ValueError, OSError):
            payload = {}
        job.commands = [
            [str(part) for part in command]
            for command in (payload.get("commands") or [])
            if isinstance(command, (list, tuple))
        ]
    return job


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def write_job(config: EditingConfig, job: RenderJob) -> Path:
    """Write everything one job knows about itself. Returns the folder.

    A *failed* job gets the same treatment as a successful one: the config,
    the segments it wanted, the commands it built and the failure record.
    "Why did this not render" has to be answerable afterwards, and a failure
    that leaves nothing on disk is a failure nobody can debug.

    Deliberately does *not* call ``prepare_job_dir``: this is called once more
    after a successful render has cleared its intermediates, and recreating
    ``temp/`` there would leave an empty folder behind every single time.
    """
    directory = job_dir(config, job.job_id)
    directory.mkdir(parents=True, exist_ok=True)
    job.output_dir = str(directory)

    _write_json(directory / CONFIG_FILE, {
        "job_id": job.job_id,
        "plan_name": job.plan_name,
        "plan_path": job.plan_path,
        "sequence_name": job.sequence_name,
        "cache_key": job.cache_key,
        "created_at": job.created_at,
        "config": job.config.to_dict(),
        "config_warnings": job.config.validated().warnings,
    })
    _write_json(directory / SEGMENTS_FILE, {
        "job_id": job.job_id,
        "count": len(job.segments),
        "duration": job.duration,
        "segments": [segment.to_dict() for segment in job.segments],
    })
    _write_json(directory / COMMANDS_FILE, {
        "job_id": job.job_id,
        "count": len(job.commands),
        "note": "Every FFmpeg invocation this job ran, in order. Paste one "
                "into a shell to reproduce a single segment.",
        "commands": [list(command) for command in job.commands],
    })
    _write_json(directory / JOB_FILE, job.to_dict())
    if job.result is not None:
        _write_json(directory / RESULT_FILE, job.result.to_dict())
    return directory


def collect_artifacts(job: RenderJob) -> list[RenderArtifact]:
    """Every file this job produced, described.

    Built by looking at the disk rather than by remembering what was written,
    so an artifact that failed to save is reported missing instead of being
    listed as present.
    """
    directory = Path(job.output_dir)
    described = [
        (output_path(directory, job.config), "video",
         "the proxy render -- watch this"),
        (notes_path(directory), "notes",
         "timestamped sections to write your review into"),
        (report_path(directory), "report",
         "what was rendered, and what could not be"),
        (directory / RESULT_FILE, "json", "the machine-readable result"),
        (directory / SEGMENTS_FILE, "json",
         "every source range, in play order"),
        (directory / COMMANDS_FILE, "commands",
         "every FFmpeg invocation, in order"),
        (directory / CONFIG_FILE, "json", "the settings this used"),
        (log_path(directory), "log", "FFmpeg's own output"),
    ]
    return [
        RenderArtifact.describe(path, kind=kind, description=description)
        for path, kind, description in described
        if path.exists()
    ]


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

def cached_job(
    config: EditingConfig, job_id: str, cache_key: str
) -> Optional[RenderJob]:
    """A previous, complete render for this exact key, or ``None``.

    Three conditions, all of which have to hold: the job record parses, its
    key matches, and the video it names is still on disk with bytes in it.
    The third is the one that matters -- a job folder whose ``render.mp4``
    somebody deleted to free space must miss, not hand back a path to nothing.
    """
    directory = job_dir(config, job_id)
    target = directory / JOB_FILE
    if not target.exists():
        return None
    try:
        job = _load_job_file(target)
    except (ValueError, OSError) as exc:
        logger.warning("Ignoring unreadable render job %s: %s", job_id, exc)
        return None
    if job.cache_key != cache_key:
        return None
    if job.result is None or not job.result.rendered:
        return None
    video = output_path(directory, job.config)
    try:
        if not video.exists() or video.stat().st_size <= 0:
            return None
    except OSError:
        return None
    return job


def clear_temp(directory: str | Path) -> int:
    """Delete the per-segment intermediates. Returns bytes reclaimed.

    Never raises: a locked temp file on Windows (a player still holding one
    open) must not turn a finished render into a failure.
    """
    target = temp_dir(directory)
    if not target.exists():
        return 0
    freed = 0
    for path in sorted(target.rglob("*"), reverse=True):
        try:
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        except OSError as exc:
            logger.debug("Could not remove %s: %s", path, exc)
    try:
        target.rmdir()
    except OSError:
        pass
    return freed


def temp_size(directory: str | Path) -> int:
    target = temp_dir(directory)
    if not target.exists():
        return 0
    total = 0
    for path in target.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def remove_job(config: EditingConfig, job_id: str) -> int:
    """Delete one job folder entirely. Returns bytes reclaimed."""
    directory = job_dir(config, job_id)
    if not directory.exists():
        return 0
    freed = _folder_size(directory)
    shutil.rmtree(directory, ignore_errors=True)
    return freed


def clean(
    config: EditingConfig,
    *,
    job_id: str = "",
    temp_only: bool = False,
    keep_latest: int = 0,
) -> dict:
    """Remove renders, or just their intermediates.

    ``temp_only`` is the safe default a person reaches for when a drive is
    full: the proxies stay watchable and the intermediates -- which are the
    larger half and are pure derivation -- go.
    """
    removed: list[str] = []
    freed = 0

    if job_id:
        directory = job_dir(config, job_id)
        if not directory.exists():
            raise EditingError(
                f"No render job called '{job_id}'",
                hint="List them with `python -m editing.cli render list`.",
            )
        if temp_only:
            freed += clear_temp(directory)
        else:
            freed += remove_job(config, job_id)
        removed.append(job_id)
        return {"removed": removed, "freed_bytes": freed,
                "temp_only": temp_only}

    jobs = list_jobs(config, limit=1000)
    for index, job in enumerate(jobs):
        if keep_latest and index < keep_latest:
            if temp_only:
                freed += clear_temp(job_dir(config, job.job_id))
            continue
        if temp_only:
            freed += clear_temp(job_dir(config, job.job_id))
        else:
            freed += remove_job(config, job.job_id)
        removed.append(job.job_id)
    return {"removed": removed, "freed_bytes": freed, "temp_only": temp_only}


def _folder_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def usage(config: EditingConfig) -> dict:
    """How much disk the renders are using, and where."""
    root = jobs_root(config)
    jobs = list_jobs(config, limit=1000)
    return {
        "root": str(root),
        "jobs": len(jobs),
        "total_bytes": _folder_size(root) if root.exists() else 0,
        "temp_bytes": sum(
            temp_size(job_dir(config, job.job_id)) for job in jobs),
    }


# ---------------------------------------------------------------------------
# JSON, in one place
# ---------------------------------------------------------------------------

def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    # Via a temp file in the same directory, so an interrupted write cannot
    # leave a half-written record where a valid one used to be.
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def write_text(path: str | Path, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target

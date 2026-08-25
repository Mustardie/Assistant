"""What a batch is, as data.

A batch is a list of footage folders and one configuration, run one after the
other. It is deliberately not a scheduler: no concurrency, no retries, no
dependency graph. Twenty folders processed in order, each producing its own
hermetic run, is a thing whose failure modes a person can reason about at nine
in the morning; a work queue is not.

## Four rules

**One failure does not stop the batch.** A folder that fails is recorded with
its reason and the batch moves on. The single most useful property of an
overnight run is that it is still going in the morning.

**Nothing is ever overwritten.** A completed run for the same footage and style
is *skipped* by default and gets a new, separately-timestamped run when
``--force`` says so. There is no path through this package that writes over a
finished run's artifacts.

**Every folder ends in a named state.** ``completed``, ``failed``, ``skipped``,
``planned`` -- and each carries why. A batch summary where something silently
did not happen would be worse than no summary.

**A dry run creates nothing.** It reports exactly which folders it would
process, which it would skip and why, and it does not make a run folder.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from editing.schema import _slug, as_float, as_text_list, short_hash

#: What happened to one folder.
#:
#: ``pending``    not reached yet
#: ``planned``    a dry run: this is what would have happened
#: ``running``    started, not finished (what a killed batch leaves behind)
#: ``completed``  the run finished, whatever its own stages did
#: ``failed``     the run could not be made or raised
#: ``skipped``    deliberately not run, with a reason
ENTRY_STATUSES = (
    "pending", "planned", "running", "completed", "failed", "skipped",
)

#: Why a folder was skipped. Closed, so a summary can group them.
SKIP_REASONS = (
    "already_completed",
    "already_failed",
    "limit_reached",
    "no_video_files",
    "not_new",
    "excluded",
    "unknown",
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def coerce_one(value: Any, allowed, default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


@dataclass
class BatchConfig:
    """What a batch was asked to do.

    The run-shaping fields mirror ``AutoRunConfig`` rather than wrapping it,
    because a batch applies *one* configuration to many folders: the folder is
    the only thing that varies, and making the whole run config per-entry would
    invite a batch where two folders were edited differently for reasons
    nobody recorded.
    """

    root: str = ""
    style: str = "minimal_clean"
    name: str = "structure"

    #: Process at most this many folders. 0 means all of them.
    limit: int = 0
    #: Only folders that have never been run at all.
    only_new: bool = False
    #: Continue incomplete runs instead of starting new ones.
    resume: bool = False
    #: Run again even where a completed run exists. Never overwrites: a forced
    #: run gets its own new run folder.
    force: bool = False
    #: Report what would happen and create nothing.
    dry_run: bool = False
    #: Descend into sub-folders looking for footage.
    recursive: bool = True

    # -- what each run does ------------------------------------------------
    director: bool = False
    retention_cut: bool = False
    render_proxy: bool = False
    no_premiere: bool = True
    mock: bool = False
    transcribe: bool = False
    captions: str = "off"
    audio_polish: str = "off"
    visual_layer: str = "off"
    visual_mode: str = "plan_only"

    created_at: str = ""
    schema_version: int = 1

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.dry_run:
            out.append(
                "this was a dry run: nothing was created and no folder was "
                "processed."
            )
        if self.force:
            out.append(
                "--force was set, so folders with a completed run were run "
                "again. Each one got a new run folder; nothing was "
                "overwritten."
            )
        if not self.no_premiere:
            out.append(
                "this batch did not set --no-premiere. No stage executes "
                "anything on its own, but every run will try to reach "
                "Premiere in its doctor stage."
            )
        if self.mock:
            out.append(
                "--mock was set, so the vision model and the critic were "
                "faked in every run. Nothing here analysed a picture."
            )
        return out

    def to_dict(self) -> dict:
        data = asdict(self)
        data["warnings"] = self.warnings
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "BatchConfig":
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**clean)


def batch_id_for(config: BatchConfig, *, when: Optional[float] = None) -> str:
    """A batch ID that is readable, sortable and effectively unique.

    Same shape as a run ID and for the same reasons: the timestamp sorts, the
    root hash groups batches over the same library, and the style is the field
    people tell two of them apart by.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(when or time.time()))
    digest = short_hash(str(config.root or "noroot"), length=6)
    return f"batch-{stamp}-{digest}-{_slug(config.style) or 'style'}"


@dataclass
class BatchEntry:
    """One folder, and what became of it."""

    folder: str = ""
    label: str = ""
    status: str = "pending"
    #: The run this folder produced, when it produced one.
    run_id: str = ""
    run_status: str = ""
    #: Why it was skipped or failed, in plain English.
    reason: str = ""
    #: The named skip reason, from ``SKIP_REASONS``.
    skip_reason: str = ""
    video_files: int = 0

    started_at: str = ""
    ended_at: str = ""
    elapsed: float = 0.0

    #: What the run itself reported.
    stages_passed: int = 0
    stages_failed: int = 0
    stages_blocked: int = 0
    #: Reliability gate status for the run, when checks were run.
    checks_status: str = ""
    checks_blocking: int = 0

    report_path: str = ""
    review_index: str = ""
    video_path: str = ""
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def ok(self) -> bool:
        return self.status in ("completed", "planned", "skipped")

    @property
    def produced_an_edit(self) -> bool:
        return self.status == "completed" and self.stages_failed == 0

    def line(self) -> str:
        mark = {"completed": "+", "failed": "x", "skipped": ".",
                "planned": "?", "running": ">", "pending": " "}.get(
                    self.status, "?")
        tail = self.reason or (
            f"run {self.run_id} [{self.run_status}]" if self.run_id else "")
        return f"{mark} {self.label[:44]:<44} {self.status:<10} {tail[:60]}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ok"] = self.ok
        data["elapsed"] = round(self.elapsed, 2)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "BatchEntry":
        data = data or {}
        return cls(
            folder=_text(data.get("folder"), 500),
            label=_text(data.get("label"), 200),
            status=coerce_one(data.get("status"), ENTRY_STATUSES, "pending"),
            run_id=_text(data.get("run_id"), 120),
            run_status=_text(data.get("run_status"), 40),
            reason=_text(data.get("reason"), 600),
            skip_reason=coerce_one(
                data.get("skip_reason"), SKIP_REASONS, ""),
            video_files=int(as_float(data.get("video_files"))),
            started_at=_text(data.get("started_at"), 40),
            ended_at=_text(data.get("ended_at"), 40),
            elapsed=as_float(data.get("elapsed")),
            stages_passed=int(as_float(data.get("stages_passed"))),
            stages_failed=int(as_float(data.get("stages_failed"))),
            stages_blocked=int(as_float(data.get("stages_blocked"))),
            checks_status=_text(data.get("checks_status"), 20),
            checks_blocking=int(as_float(data.get("checks_blocking"))),
            report_path=_text(data.get("report_path"), 500),
            review_index=_text(data.get("review_index"), 500),
            video_path=_text(data.get("video_path"), 500),
            warnings=as_text_list(data.get("warnings"), limit=40),
        )


@dataclass
class BatchSummary:
    """The whole batch, as one durable record.

    Rewritten after every folder, so a batch killed halfway leaves a summary
    describing what actually happened rather than a mystery.
    """

    batch_id: str = ""
    config: BatchConfig = field(default_factory=BatchConfig)
    entries: list[BatchEntry] = field(default_factory=list)
    status: str = "running"
    started_at: str = ""
    ended_at: str = ""
    elapsed: float = 0.0
    folder: str = ""
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.entries)

    def entry(self, folder: str) -> Optional[BatchEntry]:
        for item in self.entries:
            if item.folder == folder:
                return item
        return None

    def of_status(self, *statuses: str) -> list[BatchEntry]:
        wanted = set(statuses)
        return [item for item in self.entries if item.status in wanted]

    @property
    def completed(self) -> list[BatchEntry]:
        return self.of_status("completed")

    @property
    def failed(self) -> list[BatchEntry]:
        return self.of_status("failed")

    @property
    def skipped(self) -> list[BatchEntry]:
        return self.of_status("skipped")

    def stats(self) -> dict:
        by_status: dict = {}
        for entry in self.entries:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
        by_skip: dict = {}
        for entry in self.skipped:
            key = entry.skip_reason or "unknown"
            by_skip[key] = by_skip.get(key, 0) + 1
        return {
            "folders": len(self.entries),
            "completed": len(self.completed),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "planned": len(self.of_status("planned")),
            "videos": sum(1 for e in self.entries if e.video_path),
            "with_blocking_checks": sum(
                1 for e in self.entries if e.checks_blocking),
            "elapsed": round(self.elapsed, 1),
            "by_status": by_status,
            "by_skip_reason": by_skip,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "folder": self.folder,
            "config": self.config.to_dict(),
            "stats": self.stats(),
            "warnings": list(self.warnings),
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchSummary":
        data = data or {}
        return cls(
            batch_id=_text(data.get("batch_id"), 120),
            config=BatchConfig.from_dict(data.get("config")),
            entries=[
                BatchEntry.from_dict(item)
                for item in (data.get("entries") or [])
                if isinstance(item, dict)
            ],
            status=_text(data.get("status"), 40) or "running",
            started_at=_text(data.get("started_at"), 40),
            ended_at=_text(data.get("ended_at"), 40),
            elapsed=as_float((data.get("stats") or {}).get("elapsed")),
            folder=_text(data.get("folder"), 500),
            warnings=as_text_list(data.get("warnings"), limit=80),
        )


@dataclass
class BatchCandidate:
    """One folder discovery found, before anything decides what to do with it."""

    folder: str = ""
    label: str = ""
    video_files: int = 0
    total_bytes: int = 0
    #: Runs that already exist over this folder, newest first.
    existing_runs: list = field(default_factory=list)

    @property
    def completed_run(self) -> str:
        for entry in self.existing_runs:
            if entry.get("status") == "complete":
                return entry.get("run_id", "")
        return ""

    @property
    def incomplete_run(self) -> str:
        for entry in self.existing_runs:
            if entry.get("status") in ("failed", "blocked", "running"):
                return entry.get("run_id", "")
        return ""

    def to_dict(self) -> dict:
        return {
            "folder": self.folder,
            "label": self.label,
            "video_files": self.video_files,
            "total_bytes": self.total_bytes,
            "existing_runs": list(self.existing_runs),
            "completed_run": self.completed_run,
            "incomplete_run": self.incomplete_run,
        }

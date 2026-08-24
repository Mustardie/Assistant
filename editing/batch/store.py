"""Where a batch lives on disk.

``data/editing/auto/batches/<batch_id>/``::

    summary.json    every folder and what became of it, rewritten as it goes
    summary.txt     the readable version
    batch.log       one line per folder, appended

Beside the runs rather than inside one, because a batch is *about* runs and
outlives any of them. The runs themselves are ordinary run folders: a batch
creates nothing a single ``auto run`` would not have created, which is what
makes every other command in this system work unchanged on a batch's output.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from editing.batch.schema import BatchSummary
from editing.config import EditingConfig
from editing.errors import EditingError

logger = logging.getLogger("nova.editing.batch.store")

SUMMARY_NAME = "summary.json"
REPORT_NAME = "summary.txt"
LOG_NAME = "batch.log"


def batches_root(config: EditingConfig) -> Path:
    return config.output_dir / "auto" / "batches"


def batch_dir(config: EditingConfig, batch_id: str) -> Path:
    return batches_root(config) / batch_id


def summary_path(config: EditingConfig, batch_id: str) -> Path:
    return batch_dir(config, batch_id) / SUMMARY_NAME


def report_path(config: EditingConfig, batch_id: str) -> Path:
    return batch_dir(config, batch_id) / REPORT_NAME


def log_path(config: EditingConfig, batch_id: str) -> Path:
    return batch_dir(config, batch_id) / LOG_NAME


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save(config: EditingConfig, summary: BatchSummary) -> Path:
    """Write the summary atomically.

    Atomic because this is rewritten after every folder, including the one
    somebody interrupts: a half-written summary would turn a resumable batch
    into a mystery.
    """
    import os

    target = summary_path(config, summary.batch_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    summary.folder = str(batch_dir(config, summary.batch_id).resolve())
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(summary.to_dict(), ensure_ascii=False, indent=2,
                   default=str),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def save_report(config: EditingConfig, batch_id: str, text: str) -> Path:
    target = report_path(config, batch_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def append_log(config: EditingConfig, batch_id: str, message: str) -> None:
    """One line into the batch's log. Never raises."""
    target = log_path(config, batch_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load(config: EditingConfig, batch_id: str) -> BatchSummary:
    target = summary_path(config, batch_id)
    if not target.exists():
        raise EditingError(
            f"No batch called '{batch_id}'",
            hint="List them with `python -m editing.cli auto list-batches`.",
            detail={"path": str(target)},
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditingError(
            f"Batch '{batch_id}' has a corrupted summary: {exc}",
            hint="The runs it made are unaffected; list them with "
                 "`auto list-runs`.",
            detail={"path": str(target)},
        ) from None
    return BatchSummary.from_dict(document)


def load_or_none(
    config: EditingConfig, batch_id: str
) -> Optional[BatchSummary]:
    try:
        return load(config, batch_id)
    except EditingError:
        return None


def list_batches(config: EditingConfig, *, limit: int = 25) -> list[dict]:
    """Recent batches, newest first, without fully loading each one."""
    root = batches_root(config)
    if not root.exists():
        return []

    out: list[dict] = []
    for directory in sorted(root.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        summary = load_or_none(config, directory.name)
        if summary is None:
            out.append({"batch_id": directory.name, "status": "unreadable",
                        "folder": str(directory)})
        else:
            stats = summary.stats()
            out.append({
                "batch_id": summary.batch_id or directory.name,
                "status": summary.status,
                "root": summary.config.root,
                "style": summary.config.style,
                "started_at": summary.started_at,
                "folders": stats["folders"],
                "completed": stats["completed"],
                "failed": stats["failed"],
                "skipped": stats["skipped"],
                "folder": str(directory),
            })
        if len(out) >= limit:
            break
    return out


def latest_batch_id(config: EditingConfig) -> Optional[str]:
    entries = list_batches(config, limit=1)
    return entries[0]["batch_id"] if entries else None

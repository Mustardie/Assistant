"""Where a review package lives.

Inside the run it is about::

    data/editing/auto/runs/<run_id>/review/
        review_index.md     the thing to open
        package.json        the same content, for a script
        checks.json         the reliability gates
        checks.txt          the readable version of those
        <item>.txt/.json    a copy of every small report the run produced

Inside the run folder rather than in a shared directory because a review is
*about one run*, and a shared folder of packages would immediately raise the
question of which run each one described. Deleting a run deletes its review
with it, which is the right coupling: the package is a view over artifacts that
no longer exist.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from editing.auto import store as auto_store
from editing.config import EditingConfig
from editing.errors import EditingError
from editing.review.schema import ReviewPackage

logger = logging.getLogger("nova.editing.review.store")

PACKAGE_DIR = "review"
INDEX_NAME = "review_index.md"
PACKAGE_NAME = "package.json"
CHECKS_NAME = "checks.json"


def package_dir(config: EditingConfig, run_id: str) -> Path:
    return auto_store.run_dir(config, run_id) / PACKAGE_DIR


def index_path(config: EditingConfig, run_id: str) -> Path:
    return package_dir(config, run_id) / INDEX_NAME


def package_path(config: EditingConfig, run_id: str) -> Path:
    return package_dir(config, run_id) / PACKAGE_NAME


def checks_path(config: EditingConfig, run_id: str) -> Path:
    return package_dir(config, run_id) / CHECKS_NAME


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save_package(config: EditingConfig, package: ReviewPackage) -> Path:
    target = package_path(config, package.run_id)
    _write_json(target, package.to_dict())
    return target


def save_index(config: EditingConfig, run_id: str, text: str) -> Path:
    return save_text(index_path(config, run_id), text)


def save_checks(config: EditingConfig, run_id: str, report) -> Path:
    target = checks_path(config, run_id)
    _write_json(target, report.to_dict())
    return target


def save_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_package(config: EditingConfig, run_id: str) -> ReviewPackage:
    target = package_path(config, run_id)
    if not target.exists():
        raise EditingError(
            f"Run '{run_id}' has no review package",
            hint=f"Build one with `python -m editing.cli review package "
                 f"--run {run_id}`.",
            detail={"path": str(target)},
        )
    return ReviewPackage.from_dict(_read_json(target))


def package_or_none(
    config: EditingConfig, run_id: str
) -> Optional[ReviewPackage]:
    try:
        return load_package(config, run_id)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable review package for %s: %s", run_id, exc)
        return None


def latest_with_package(
    config: EditingConfig, *, limit: int = 25
) -> Optional[str]:
    """The most recent run that has a review package, if any."""
    for entry in auto_store.list_runs(config, limit=limit):
        run_id = entry.get("run_id") or ""
        if run_id and package_path(config, run_id).exists():
            return run_id
    return None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}

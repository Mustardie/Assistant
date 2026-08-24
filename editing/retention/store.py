"""Where a retention cut lives.

``data/editing/retention/``::

    <name>.plan.json       every decision, accepted and rejected
    <name>.plan.txt        the readable report
    <name>.compare.json    retention cut against the cut it was built from
    <name>.roughcut.json   the resulting cut, in the ordinary rough-cut shape

The last one is the point of the split. A retention cut is a *variant*: it goes
in its own file under its own name, and ``roughcut/<name>.json`` -- the cut it
was built from -- is never touched. Rejecting the retention pass costs you
nothing, which is what makes it safe to try.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from editing.config import EditingConfig
from editing.errors import EditingError
from editing.retention.schema import RetentionCutComparison, RetentionCutPlan
from editing.roughcut.schema import RoughCutPlan

logger = logging.getLogger("nova.editing.retention.store")


def plan_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.retention_dir / f"{name}.plan.json"


def report_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.retention_dir / f"{name}.plan.txt"


def compare_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.retention_dir / f"{name}.compare.json"


def roughcut_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.retention_dir / f"{name}.roughcut.json"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save_plan(config: EditingConfig, plan: RetentionCutPlan, *,
              name: str = "structure") -> Path:
    target = plan_path(config, name)
    _write_json(target, plan.to_dict())
    return target


def save_report(config: EditingConfig, text: str, *,
                name: str = "structure") -> Path:
    target = report_path(config, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def save_comparison(config: EditingConfig, comparison: RetentionCutComparison,
                    *, name: str = "structure") -> Path:
    target = compare_path(config, name)
    _write_json(target, comparison.to_dict())
    return target


def save_roughcut(config: EditingConfig, cut: RoughCutPlan, *,
                  name: str = "structure") -> Path:
    """The resulting cut, in its own file.

    Deliberately not ``roughcut/<name>.json``: that is the cut this was built
    from, and overwriting it would mean disagreeing with the retention pass
    cost you the cut it was arguing with.
    """
    target = roughcut_path(config, name)
    _write_json(target, cut.to_dict())
    return target


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_plan(config: EditingConfig, *,
              name: str = "structure") -> RetentionCutPlan:
    target = plan_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No retention cut plan for '{name}'",
            hint="Build one with `python -m editing.cli retention plan`.",
            detail={"path": str(target)},
        )
    return RetentionCutPlan.from_dict(_read_json(target))


def load_roughcut(config: EditingConfig, *,
                  name: str = "structure") -> RoughCutPlan:
    target = roughcut_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No retention cut for '{name}'",
            hint="Build one with `python -m editing.cli retention plan "
                 "--mode retention`. In report-only mode no cut is written, "
                 "because nothing changed.",
            detail={"path": str(target)},
        )
    return RoughCutPlan.from_dict(_read_json(target))


def load_comparison(config: EditingConfig, *,
                    name: str = "structure") -> RetentionCutComparison:
    target = compare_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No retention comparison for '{name}'",
            hint="Build one with `python -m editing.cli retention compare`.",
            detail={"path": str(target)},
        )
    return RetentionCutComparison.from_dict(_read_json(target))


def plan_or_none(config: EditingConfig, *,
                 name: str = "structure") -> Optional[RetentionCutPlan]:
    """The plan if there is a readable one. Never raises."""
    try:
        return load_plan(config, name=name)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable retention plan for %s: %s", name, exc)
        return None


def roughcut_or_none(config: EditingConfig, *,
                     name: str = "structure") -> Optional[RoughCutPlan]:
    try:
        return load_roughcut(config, name=name)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable retention cut for %s: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# JSON, in one place
# ---------------------------------------------------------------------------

def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}

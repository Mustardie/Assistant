"""Where a visual plan lives.

``data/editing/visuals/``::

    <name>.visuals.json      every moment and every treatment, refusals included
    <name>.visuals.txt       the readable visual report
    <name>.visuals.md        the sidecar marker file, for watching the proxy
    <name>.final.json        the FinalEditPlan: cut, captions, sound, visuals
    <name>.final.txt         the readable final-edit report
    <name>.premiere.json     the Premiere operation plan, validated offline
    <name>.compare.json      the visual layer against the cut without it

Its own directory, like every other pass: a visual plan is an opinion about a
cut, and re-running it must never be able to overwrite the cut underneath.
Everything here is derived and can be rebuilt in a second.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from editing.config import EditingConfig
from editing.errors import EditingError
from editing.visuals.execution import (
    FinalEditPlan, PremiereVisualOperationPlan, VisualComparisonReport,
)
from editing.visuals.schema import VisualLayerPlan

logger = logging.getLogger("nova.editing.visuals.store")


def plan_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.visuals_dir / f"{name}.visuals.json"


def report_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.visuals_dir / f"{name}.visuals.txt"


def markers_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.visuals_dir / f"{name}.visuals.md"


def final_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.visuals_dir / f"{name}.final.json"


def final_report_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.visuals_dir / f"{name}.final.txt"


def premiere_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.visuals_dir / f"{name}.premiere.json"


def compare_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.visuals_dir / f"{name}.compare.json"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save_plan(config: EditingConfig, plan: VisualLayerPlan, *,
              name: str = "structure") -> Path:
    target = plan_path(config, name)
    _write_json(target, plan.to_dict())
    return target


def save_final(config: EditingConfig, plan: FinalEditPlan, *,
               name: str = "structure") -> Path:
    target = final_path(config, name)
    _write_json(target, plan.to_dict())
    return target


def save_premiere(config: EditingConfig, plan: PremiereVisualOperationPlan, *,
                  name: str = "structure") -> Path:
    target = premiere_path(config, name)
    _write_json(target, plan.to_dict())
    return target


def save_comparison(config: EditingConfig, report: VisualComparisonReport, *,
                    name: str = "structure") -> Path:
    target = compare_path(config, name)
    _write_json(target, report.to_dict())
    return target


def save_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_plan(config: EditingConfig, *,
              name: str = "structure") -> VisualLayerPlan:
    target = plan_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No visual plan for '{name}'",
            hint="Build one with `python -m editing.cli visuals plan "
                 "--visual-layer balanced`.",
            detail={"path": str(target)},
        )
    return VisualLayerPlan.from_dict(_read_json(target))


def load_final(config: EditingConfig, *,
               name: str = "structure") -> FinalEditPlan:
    target = final_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No final edit plan for '{name}'",
            hint="Build one with `python -m editing.cli visuals plan "
                 "--visual-layer balanced`.",
            detail={"path": str(target)},
        )
    return FinalEditPlan.from_dict(_read_json(target))


def load_premiere(config: EditingConfig, *,
                  name: str = "structure") -> PremiereVisualOperationPlan:
    target = premiere_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No Premiere visual plan for '{name}'",
            hint="Build one with `python -m editing.cli visuals "
                 "export-premiere-plan`.",
            detail={"path": str(target)},
        )
    return PremiereVisualOperationPlan.from_dict(_read_json(target))


def plan_or_none(config: EditingConfig, *,
                 name: str = "structure") -> Optional[VisualLayerPlan]:
    """The visual plan if there is a readable one. Never raises."""
    try:
        return load_plan(config, name=name)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable visual plan for %s: %s", name, exc)
        return None


def final_or_none(config: EditingConfig, *,
                  name: str = "structure") -> Optional[FinalEditPlan]:
    try:
        return load_final(config, name=name)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable final edit plan for %s: %s", name, exc)
        return None


def premiere_or_none(
    config: EditingConfig, *, name: str = "structure"
) -> Optional[PremiereVisualOperationPlan]:
    try:
        return load_premiere(config, name=name)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable Premiere visual plan for %s: %s", name, exc)
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

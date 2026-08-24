"""Where a polish pass lives on disk.

``data/editing/polish/``::

    <name>.captions.json    every line considered, accepted and refused
    <name>.captions.txt     the readable caption report
    <name>.captions.srt     the sidecar subtitle file, in sequence time
    <name>.audio.json       every cue considered, accepted and refused
    <name>.audio.txt        the readable audio report

Its own directory, like every other pass: a polish plan is an opinion about a
cut, and re-running it must never be able to overwrite the cut it is about.
Deleting this folder loses nothing that cannot be rebuilt in a second.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from editing.config import EditingConfig
from editing.errors import EditingError
from editing.polish.schema import AudioPolishPlan, CaptionPlan

logger = logging.getLogger("nova.editing.polish.store")


def caption_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.polish_dir / f"{name}.captions.json"


def caption_report_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.polish_dir / f"{name}.captions.txt"


def sidecar_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.polish_dir / f"{name}.captions.srt"


def audio_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.polish_dir / f"{name}.audio.json"


def audio_report_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.polish_dir / f"{name}.audio.txt"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save_captions(config: EditingConfig, plan: CaptionPlan, *,
                  name: str = "structure") -> Path:
    target = caption_path(config, name)
    _write_json(target, plan.to_dict())
    return target


def save_audio(config: EditingConfig, plan: AudioPolishPlan, *,
               name: str = "structure") -> Path:
    target = audio_path(config, name)
    _write_json(target, plan.to_dict())
    return target


def save_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_captions(config: EditingConfig, *,
                  name: str = "structure") -> CaptionPlan:
    target = caption_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No caption plan for '{name}'",
            hint="Build one with `python -m editing.cli polish captions "
                 "--captions key_moments`.",
            detail={"path": str(target)},
        )
    return CaptionPlan.from_dict(_read_json(target))


def load_audio(config: EditingConfig, *,
               name: str = "structure") -> AudioPolishPlan:
    target = audio_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No audio polish plan for '{name}'",
            hint="Build one with `python -m editing.cli polish audio "
                 "--audio-polish placeholders`.",
            detail={"path": str(target)},
        )
    return AudioPolishPlan.from_dict(_read_json(target))


def captions_or_none(config: EditingConfig, *,
                     name: str = "structure") -> Optional[CaptionPlan]:
    """The caption plan if there is a readable one. Never raises."""
    try:
        return load_captions(config, name=name)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable caption plan for %s: %s", name, exc)
        return None


def audio_or_none(config: EditingConfig, *,
                  name: str = "structure") -> Optional[AudioPolishPlan]:
    try:
        return load_audio(config, name=name)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable audio polish plan for %s: %s", name, exc)
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

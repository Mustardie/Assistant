"""Where a director plan lives, and how a repeat run avoids paying twice.

``data/editing/director/``::

    <name>.context.json    what the model was shown, structured
    <name>.prompt.txt      what it was actually sent, verbatim
    <name>.plan.json       every decision, accepted and rejected
    <name>.plan.txt        the readable report
    <name>.compare.json    director cut against heuristic cut

The prompt is written as text rather than only inside the plan JSON because
the first thing to do about bad decisions is read the prompt, and reading it
out of a JSON string field is unpleasant enough that people do not.

## The cache

Keyed on the context fingerprint plus everything about the configuration that
changes what the model would answer. The context fingerprint covers the
footage, the analysis, the story layer and the style guide -- so re-running
after editing the guide correctly misses, and re-running after nothing changed
correctly hits.

A cached response is stored as *text*, not as parsed decisions. The parser and
the safety pass both change as this layer is tuned, and a cache of parsed
output would mean fixing a parser bug did not fix anything already cached.
Storing the raw answer means every rerun re-parses and re-checks -- which is
free, and is the difference between a cache and a fossil.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from editing.cache import Cache
from editing.config import EditingConfig
from editing.errors import EditingError
from editing.director.schema import (
    DirectorConfig, DirectorContext, DirectorPlan, DirectorPrompt,
)

logger = logging.getLogger("nova.editing.director.store")

#: Cache namespace.
CACHE_KIND = "director"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def plan_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.director_dir / f"{name}.plan.json"


def report_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.director_dir / f"{name}.plan.txt"


def context_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.director_dir / f"{name}.context.json"


def prompt_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.director_dir / f"{name}.prompt.txt"


def compare_path(config: EditingConfig, name: str = "structure") -> Path:
    return config.director_dir / f"{name}.compare.json"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def save_context(
    config: EditingConfig, context: DirectorContext, *,
    name: str = "structure",
) -> Path:
    target = context_path(config, name)
    _write_json(target, context.to_dict())
    return target


def save_prompt(
    config: EditingConfig, prompt: DirectorPrompt, *,
    name: str = "structure",
) -> Path:
    """Write the prompt as text. The first thing to read when a plan is odd."""
    target = prompt_path(config, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "=== SYSTEM ===\n" + prompt.system
        + "\n\n=== USER ===\n" + prompt.user + "\n",
        encoding="utf-8",
    )
    return target


def save_plan(
    config: EditingConfig, plan: DirectorPlan, *, name: str = "structure",
) -> Path:
    target = plan_path(config, name)
    _write_json(target, plan.to_dict())
    return target


def save_report(
    config: EditingConfig, text: str, *, name: str = "structure",
) -> Path:
    target = report_path(config, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def save_comparison(
    config: EditingConfig, payload: dict, *, name: str = "structure",
) -> Path:
    target = compare_path(config, name)
    _write_json(target, payload)
    return target


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_plan(
    config: EditingConfig, *, name: str = "structure"
) -> DirectorPlan:
    target = plan_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No director plan for '{name}'",
            hint="Build one with `python -m editing.cli director plan`.",
            detail={"path": str(target)},
        )
    return DirectorPlan.from_dict(_read_json(target))


def load_context(
    config: EditingConfig, *, name: str = "structure"
) -> DirectorContext:
    target = context_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No director context for '{name}'",
            hint="Build one with `python -m editing.cli director "
                 "build-context`.",
            detail={"path": str(target)},
        )
    return DirectorContext.from_dict(_read_json(target))


def load_comparison(config: EditingConfig, *, name: str = "structure") -> dict:
    target = compare_path(config, name)
    if not target.exists():
        raise EditingError(
            f"No director/heuristic comparison for '{name}'",
            hint="Build one with `python -m editing.cli director "
                 "compare-heuristic`.",
            detail={"path": str(target)},
        )
    return _read_json(target)


def plan_or_none(
    config: EditingConfig, *, name: str = "structure"
) -> Optional[DirectorPlan]:
    """The plan if there is a readable one. Never raises.

    Used by the rough cut builder, which must degrade to the heuristic path
    when there is no director plan rather than fail.
    """
    try:
        return load_plan(config, name=name)
    except (EditingError, ValueError, OSError) as exc:
        logger.debug("No usable director plan for %s: %s", name, exc)
        return None


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

def cache_key(
    cache: Cache, context: DirectorContext, settings: DirectorConfig
) -> str:
    """The key for one context under one configuration."""
    return cache.key(
        CACHE_KIND,
        context=context.fingerprint(),
        settings=settings.cache_key_part(),
    )


def cached_response(
    cache: Cache, key: str, *, settings: DirectorConfig
) -> Optional[str]:
    """A stored model answer for this key, or ``None``.

    Text, deliberately: see the module docstring. A stored entry that will not
    parse is a miss rather than a crash.
    """
    if not settings.use_cache:
        return None
    payload = cache.get(CACHE_KIND, key)
    if not isinstance(payload, dict):
        return None
    answer = payload.get("response")
    return answer if isinstance(answer, str) and answer.strip() else None


def store_response(
    cache: Cache, key: str, response: str, *, settings: DirectorConfig,
    context: DirectorContext,
) -> None:
    """Cache one model answer, with enough beside it to audit the entry."""
    cache.put(
        CACHE_KIND, key,
        {"response": response},
        meta={
            "model": settings.model,
            "backend": settings.backend,
            "context": context.fingerprint(),
            "segments": len(context.segments),
            "style_guide": context.style_guide.fingerprint(),
        },
    )


def clear_cache(cache: Cache) -> int:
    """Drop every cached director answer. Returns how many entries went."""
    return cache.clear(CACHE_KIND)


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

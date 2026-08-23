"""One director pass, end to end.

    timeline + story layer -> context -> prompt -> model -> decisions
                           -> deterministic safety -> DirectorPlan -> ranges

Every step is separable and every step is inspectable on disk afterwards. The
order is not negotiable: the context is built before the prompt so the safety
pass can check the answer against the same object the model was shown, and
safety runs before anything becomes a range so no unchecked decision ever
reaches the rough cut builder.

## What happens when the model is not there

A failure at any stage produces a ``DirectorPlan`` carrying a
``DirectorFailure`` and **no ranges**. The caller -- ``pipeline.rough_cut``,
the auto stage, the CLI -- then falls back to the heuristic selector, which is
a worse cut and an honest one. That is the whole degradation story: this layer
is an improvement over the thresholds, never a dependency of them.

## What is never done

Decisions are never invented. Not when the model is unreachable, not when it
answers with prose, not when every decision it made was rejected. A plan with
no decisions in it says so.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional, Sequence

from editing.cache import Cache
from editing.config import EditingConfig
from editing.errors import EditingError, ModelError
from editing.director import backends, context as context_module
from editing.director import parse, prompt as prompt_module, safety
from editing.director import store, style_guide as style_guide_module
from editing.director.schema import (
    DirectorConfig, DirectorContext, DirectorFailure, DirectorPlan,
    DirectorPrompt, DirectorResult, StyleGuide, now,
)
from editing.schema import StructureTimeline

logger = logging.getLogger("nova.editing.director.run")

Reporter = Callable[[str], None]


def _quiet(_message: str) -> None:
    return None


def build_context(
    timeline: StructureTimeline,
    *,
    settings: Optional[DirectorConfig] = None,
    style_guide_path: str = "",
    style_guide_text: Optional[str] = None,
    memory=None,
    retention=None,
    recommendations=None,
    roughcut=None,
    preferences: Optional[Sequence] = None,
    style_preset=None,
    name: str = "structure",
) -> DirectorContext:
    """Everything the director will be told. Calls no model."""
    settings = (settings or DirectorConfig()).validated()
    guide = style_guide_module.load(
        style_guide_path or settings.style_guide_path or None,
        text=style_guide_text,
    )
    return context_module.build(
        timeline,
        config=settings,
        style_guide=guide,
        memory=memory,
        retention=retention,
        recommendations=recommendations,
        roughcut=roughcut,
        preferences=preferences,
        style_preset=style_preset,
        name=name,
    )


def plan(
    config: EditingConfig,
    context: DirectorContext,
    *,
    settings: Optional[DirectorConfig] = None,
    model=None,
    cache: Optional[Cache] = None,
    force: bool = False,
    say: Reporter = _quiet,
) -> DirectorPlan:
    """Ask the director, check the answer, and produce a plan.

    Never raises for anything a person can act on. An unreachable model, an
    unparseable answer and a plan where every decision was rejected are all
    *results*: a plan with a failure record, a hint, and whatever survived.
    """
    started = time.time()
    settings = (settings or DirectorConfig()).validated()

    plan_out = DirectorPlan(
        name=context.name,
        episode_id=context.episode_id,
        mode=settings.mode,
        config=settings,
        style_guide=context.style_guide,
        backend=settings.backend,
        model=settings.model,
        context_fingerprint=context.fingerprint(),
        context_stats=context.stats(),
        sources=dict(context.sources),
        generated_at=now(),
        warnings=list(settings.warnings) + list(context.warnings),
    )

    if context.is_empty:
        return _fail(
            plan_out, started,
            stage="empty_context",
            code="nothing_to_decide",
            message="There are no candidate ranges to decide about.",
            hint="Run `python -m editing.cli run --folder <folder>` to build "
                 "a timeline first.",
            recoverable=False,
        )

    if not settings.runs_model:
        return _fail(
            plan_out, started,
            stage="config",
            code="mode_is_heuristic",
            message="Mode is 'heuristic', so no director pass was run.",
            hint="Use --mode director or --mode hybrid to run one.",
            recoverable=True,
        )

    built = prompt_module.build(context, settings)
    plan_out.prompt = built
    say(f"[director] {len(context.segments)} candidate range(s), "
        f"~{built.approx_tokens} token(s) of context")

    result = _ask(
        config, context, built, settings,
        model=model, cache=cache, force=force, say=say,
    )
    plan_out.mock = result.mock
    plan_out.cached = result.cached
    plan_out.approach = result.approach
    plan_out.model = result.model or settings.model
    plan_out.warnings.extend(result.warnings)

    if result.failure is not None:
        plan_out.failure = result.failure
        plan_out.elapsed = round(time.time() - started, 3)
        return plan_out

    say(f"[director] {len(result.decisions)} decision(s); checking them")
    decisions, ranges, review = safety.review(
        result.decisions, context, config=settings)
    plan_out.decisions = decisions
    plan_out.ranges = ranges
    plan_out.safety = review
    plan_out.warnings.extend(review.warnings)
    for entry in result.discarded:
        plan_out.warnings.append(
            f"discarded a decision the model produced: {entry.get('why', '')}")

    if not ranges:
        plan_out.failure = DirectorFailure(
            stage="safety",
            code="everything_rejected",
            message="Every decision that would have put footage in the cut "
                    "was rejected by the safety pass.",
            hint="`director show-rejected` lists each one and the check that "
                 "refused it. The heuristic selector still works: build a "
                 "rough cut without --director.",
            recoverable=True,
            detail={"by_check": review.by_check()},
        )

    plan_out.elapsed = round(time.time() - started, 3)
    say(f"[director] {review.accepted} accepted, {review.rejected} rejected, "
        f"{review.modified} modified -> {plan_out.cut_duration:.0f}s cut")
    return plan_out


def _ask(
    config: EditingConfig,
    context: DirectorContext,
    built: DirectorPrompt,
    settings: DirectorConfig,
    *,
    model=None,
    cache: Optional[Cache] = None,
    force: bool = False,
    say: Reporter = _quiet,
) -> DirectorResult:
    """One model call, cached, parsed. Failures come back as a result."""
    started = time.time()
    client = model or backends.build_model(settings)
    is_mock = getattr(client, "name", "") == "mock-director" \
        or settings.backend == "mock"

    result = DirectorResult(
        backend=settings.backend,
        model=getattr(client, "name", settings.model),
        mock=is_mock,
    )

    answer: Optional[str] = None
    if cache is not None and settings.use_cache and not force:
        key = store.cache_key(cache, context, settings)
        answer = store.cached_response(cache, key, settings=settings)
        if answer is not None:
            result.cached = True
            say("[director] reusing the cached answer for this episode")

    if answer is None:
        say(f"[director] asking {result.model}...")
        try:
            answer = client.complete(system=built.system, user=built.user)
        except ModelError as exc:
            result.failure = DirectorFailure(
                stage="no_backend" if "reach" in exc.message.lower()
                else "model",
                code=exc.code,
                message=exc.message,
                hint=exc.hint or "Use --backend mock to exercise the pipeline "
                                 "without a model.",
                recoverable=True,
                detail=dict(exc.detail or {}) if isinstance(exc.detail, dict)
                else {},
            )
            result.elapsed = round(time.time() - started, 3)
            return result
        except Exception as exc:  # noqa: BLE001 - a bug is still a result
            result.failure = DirectorFailure(
                stage="model",
                code="director_call_failed",
                message=f"{type(exc).__name__}: {exc}"[:500],
                hint="This is unexpected. The prompt is saved beside the plan "
                     "if you want to try it by hand.",
                recoverable=True,
            )
            result.elapsed = round(time.time() - started, 3)
            return result

        if cache is not None and settings.use_cache and not is_mock:
            store.store_response(
                cache, store.cache_key(cache, context, settings), answer,
                settings=settings, context=context,
            )

    result.raw_response = answer
    result.elapsed = round(time.time() - started, 3)

    try:
        decisions, approach, discarded, warnings = parse.parse_response(
            answer, context, config=settings)
    except ModelError as exc:
        result.failure = DirectorFailure(
            stage=("no_decisions" if "no usable decisions" in exc.message
                   else "invalid_json"),
            code=exc.code,
            message=exc.message,
            hint=exc.hint,
            recoverable=True,
            response_excerpt=answer[:2000],
            detail=dict(exc.detail or {}) if isinstance(exc.detail, dict)
            else {},
        )
        return result

    result.decisions = decisions
    result.approach = approach
    result.discarded = discarded
    result.warnings.extend(warnings)
    if is_mock:
        result.warnings.append(
            "MOCK DIRECTOR -- these decisions come from four fixed rules, not "
            "from a model. Nothing here is an editorial judgement."
        )
    return result


def _fail(
    plan_out: DirectorPlan,
    started: float,
    *,
    stage: str,
    code: str,
    message: str,
    hint: str = "",
    recoverable: bool = True,
) -> DirectorPlan:
    plan_out.failure = DirectorFailure(
        stage=stage, code=code, message=message, hint=hint,
        recoverable=recoverable,
    )
    plan_out.elapsed = round(time.time() - started, 3)
    return plan_out


def persist(
    config: EditingConfig,
    plan_out: DirectorPlan,
    context: Optional[DirectorContext] = None,
    *,
    name: str = "structure",
) -> dict:
    """Write everything one pass produced. Returns the paths.

    A failed pass is written too, prompt included: "why did it decide that" is
    only answerable with the prompt in hand, and a failure that leaves nothing
    on disk is a failure nobody can debug.
    """
    from editing.director import report as report_module

    written: dict = {}
    if context is not None:
        written["context"] = str(store.save_context(config, context, name=name))
    if plan_out.prompt is not None:
        written["prompt"] = str(
            store.save_prompt(config, plan_out.prompt, name=name))
    written["plan"] = str(store.save_plan(config, plan_out, name=name))
    written["report"] = str(store.save_report(
        config, report_module.render(plan_out), name=name))
    return written

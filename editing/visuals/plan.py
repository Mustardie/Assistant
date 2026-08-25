"""One visual pass: detect, propose, check, thin.

    moments -> candidates -> safety -> density -> VisualLayerPlan

The order is the design. Detection reads only what the earlier passes recorded;
proposal offers what the library and the style permit; the safety rules refuse
or soften; and the density ceilings take what is left down to something a
person would actually watch.

**A treatment is refused strongest-first, not earliest-first.** Candidates are
ranked before the budget is applied, so what survives a full ceiling is what
was argued for best rather than what happened to come first in the episode.
The alternative produces an edit with all its effects in the opening minute.

**Everything refused stays in the plan.** Including the ones the style never
offered: when a moment earns nothing, the report has to be able to say whether
that was the safety pass, the budget, or the style simply not doing that.
"""
from __future__ import annotations

import logging
from typing import Sequence

from editing.roughcut.schema import RoughCutPlan
from editing.schema import StructureTimeline
from editing.visuals import moments as moments_module
from editing.visuals import safety as safety_module
from editing.visuals import treatments as treatments_module
from editing.visuals.schema import (
    NOT_MEASURED, NOT_RENDERED, VisualConfig, VisualLayerPlan, VisualMoment,
    VisualTreatment, family_of, now, treatment_id_for,
)

logger = logging.getLogger("nova.editing.visuals.plan")


def build_visual_plan(
    timeline: StructureTimeline,
    cut: RoughCutPlan,
    style,
    config: VisualConfig,
    *,
    director_plan=None,
    retention_plan=None,
    caption_plan=None,
    audio_plan=None,
    memory=None,
    retention_findings=None,
    base: str = "roughcut",
    name: str = "structure",
) -> VisualLayerPlan:
    """Every moment found and every treatment considered, in one object."""
    config = config.validated()
    plan = VisualLayerPlan(
        name=name,
        layer=config.layer,
        config=config,
        style=getattr(style, "name", "") or "",
        base=base,
        sequence_name=cut.sequence_name,
        cut_duration=round(cut.total_duration, 3),
        generated_at=now(),
        warnings=list(config.warnings),
    )

    if not config.enabled:
        plan.safety_notes.append(
            "No moment was considered: the visual layer is off for this run.")
        return plan

    plan.moments = moments_module.detect_moments(
        timeline, cut,
        director_plan=director_plan,
        retention_plan=retention_plan,
        caption_plan=caption_plan,
        audio_plan=audio_plan,
        memory=memory,
        retention_findings=retention_findings,
    )
    if not plan.moments:
        plan.warnings.append(
            "no moment in this cut carried enough evidence for a visual "
            "treatment. That is a normal result on footage with no "
            "transcript, no director pass and nothing on screen the vision "
            "pass could name."
        )
        return plan

    allowed = treatments_module.allowed_effects(style, config)
    context = _episode_context(memory, caption_plan)
    captions = _caption_windows(caption_plan)
    hook_polish = _hook_polish(caption_plan, audio_plan, retention_plan)
    importance = _importance_by_moment(plan.moments, timeline)

    candidates = _propose_all(plan, style, config, allowed, context)
    _apply_safety(plan, candidates, config, style, cut, captions,
                  hook_polish, importance)
    _apply_budget(plan, config)
    _finish(plan)
    return plan


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------

def _propose_all(plan, style, config, allowed, context) -> list:
    """Every candidate the library offers, plus a record of what it refused.

    The second half matters as much as the first. A moment the style never
    offers anything for is a moment that earned nothing, and "why did this
    death get no treatment" has to be answerable without reading the style
    tables.
    """
    out: list = []
    for moment in plan.moments:
        proposed = treatments_module.propose(
            moment, style, config, allowed=allowed, context=context)
        if proposed:
            out.extend((moment, candidate) for candidate in proposed)
            continue

        offered = treatments_module.MOMENT_EFFECTS.get(moment.kind, ())
        blocked = [effect for effect in offered if effect not in allowed]
        if blocked:
            allowed_by_config, why = config.allows(blocked[0])
            plan.treatments.append(_refused(
                moment, blocked[0],
                "layer_forbids" if not allowed_by_config else "style_forbids",
                why or (f"the {plan.style} style does not use "
                        f"{blocked[0].replace('_', ' ')}"),
            ))
        elif offered:
            plan.treatments.append(_refused(
                moment, offered[0], "no_evidence",
                "the library offered this and there was nothing to fill it "
                "with -- no text that was said, no entity that was named",
            ))
        else:
            plan.treatments.append(_refused(
                moment, "label_tag", "no_evidence",
                f"nothing in the library suits a "
                f"{moment.kind.replace('_', ' ')}",
            ))
    return out


def _refused(moment: VisualMoment, effect: str, code: str,
             detail: str) -> VisualTreatment:
    """A treatment that never became a candidate, recorded anyway."""
    return VisualTreatment(
        treatment_id=treatment_id_for(effect, moment.start, moment.moment_id),
        moment_id=moment.moment_id,
        source_id=moment.source_id,
        source_type=moment.source_type,
        moment_kind=moment.kind,
        effect=effect,
        intensity="subtle",
        start=moment.start,
        end=moment.start,
        placement_id=moment.placement_id,
        priority=moment.importance,
        reason=f"considered for the {moment.kind.replace('_', ' ')} here",
        evidence=list(moment.evidence)[:6],
        accepted=False,
        reject_reason=code,
        reject_detail=detail,
    )


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def _apply_safety(plan, candidates, config, style, cut, captions,
                  hook_polish, importance) -> None:
    """Every candidate through every rule, strongest first.

    Ranked before checking rather than after, because the spacing rule reads
    what has already been kept: checking a weak candidate first would let it
    claim the space a strong one needed.
    """
    ranked = sorted(
        candidates,
        key=lambda pair: (-pair[1].priority, pair[1].candidate_id),
    )
    kept: list[VisualTreatment] = []
    counts: dict = {}

    for moment, candidate in ranked:
        treatment = _from_candidate(moment, candidate, config)
        placement = cut.placement_at(treatment.start)
        safety_module.check_all(
            treatment, moment, config,
            style=style,
            placement=placement,
            captions=captions,
            kept=kept,
            effect_counts=counts,
            hook_polish=hook_polish,
            segment_importance=importance.get(moment.moment_id, ""),
        )
        plan.treatments.append(treatment)
        if treatment.accepted:
            kept.append(treatment)
            counts[treatment.effect] = counts.get(treatment.effect, 0) + 1


def _from_candidate(moment: VisualMoment, candidate, config) -> VisualTreatment:
    from editing.visuals.preview import target_for

    start = round(moment.start + candidate.offset, 3)
    duration = min(candidate.duration, config.max_effect_seconds)
    return VisualTreatment(
        treatment_id=candidate.candidate_id,
        moment_id=moment.moment_id,
        source_id=moment.source_id,
        source_type=moment.source_type,
        moment_kind=moment.kind,
        effect=candidate.effect,
        intensity=candidate.intensity,
        start=start,
        end=round(start + duration, 3),
        placement_id=moment.placement_id,
        easing=str(candidate.payload.get("easing") or "ease_out"),
        priority=candidate.priority,
        reason=candidate.reason,
        evidence=list(moment.evidence)[:8],
        target_output=target_for(candidate.effect),
        payload=dict(candidate.payload),
    )


# ---------------------------------------------------------------------------
# Density
# ---------------------------------------------------------------------------

def _apply_budget(plan: VisualLayerPlan, config: VisualConfig) -> None:
    """Thin the accepted set until it fits the ceilings.

    Two budgets, because "how many effects" and "how many arrows" are
    different questions and an arrow is the one that most quickly reads as
    try-hard. Markers count against neither: they change no frame.
    """
    accepted = [t for t in plan.treatments if t.accepted]
    if not accepted:
        return

    minutes = max(plan.cut_duration, 1.0) / 60.0
    effects_allowed = _budget(
        config.max_effects_per_minute, minutes, plan, "effects")
    callouts_allowed = _budget(
        config.max_callouts_per_minute, minutes, plan, "callouts")
    effects_allowed = min(effects_allowed, config.max_total)

    ranked = sorted(accepted, key=lambda t: (-t.priority, t.start))
    effects = 0
    callouts = 0

    for treatment in ranked:
        if not treatment.counts_against_density:
            continue
        if family_of(treatment.effect) == "callout":
            if callouts >= callouts_allowed:
                _reject(treatment, "density_limit",
                        f"the callout budget for this cut is "
                        f"{callouts_allowed} "
                        f"({config.max_callouts_per_minute:.1f} a minute over "
                        f"{plan.cut_duration:.0f}s), and stronger moments "
                        "filled it")
                continue
            callouts += 1
        if effects >= effects_allowed:
            if family_of(treatment.effect) == "callout":
                callouts -= 1
            _reject(treatment, "density_limit",
                    f"the effect budget for this cut is {effects_allowed} "
                    f"({config.max_effects_per_minute:.1f} a minute over "
                    f"{plan.cut_duration:.0f}s), and stronger moments filled "
                    "it")
            continue
        effects += 1

    kept = len([t for t in plan.treatments if t.accepted])
    if kept < len(accepted):
        plan.warnings.append(
            f"{len(accepted)} treatment(s) cleared every safety rule and "
            f"{kept} fitted the budget. The rest are in the plan with the "
            "rule that refused them."
        )


def _budget(rate: float, minutes: float, plan, label: str) -> int:
    """How many of something this cut may carry.

    A rate that rounds down to nothing on a short cut still allows one, and
    the plan says so -- the same floor the caption and audio budgets use, for
    the same reason: a rate that silently disables a feature is not a limit.
    """
    allowed = int(rate * minutes)
    if rate > 0 and allowed < 1:
        allowed = 1
        plan.safety_notes.append(
            f"the cut is {plan.cut_duration:.0f}s, so {rate:.2f} {label} a "
            "minute rounds down to none. One was allowed, which reads above "
            "the ceiling in the density figure."
        )
    return allowed


def _reject(treatment: VisualTreatment, code: str, detail: str) -> None:
    treatment.accepted = False
    treatment.reject_reason = code
    treatment.reject_detail = detail


# ---------------------------------------------------------------------------
# Context the library needs
# ---------------------------------------------------------------------------

def _episode_context(memory, caption_plan) -> dict:
    """What the cards can legitimately say, gathered from what was recorded.

    Nothing in here is generated. An objective is what somebody stated; a title
    is that objective; a recap is the payoff the episode memory found. When a
    field is missing, the card that needed it is not built -- which is the
    mechanism behind "no card with invented words on it".
    """
    context: dict = {}
    if memory is not None:
        objective = getattr(memory, "main_objective", None)
        if objective is not None and getattr(objective, "text", ""):
            context["objective"] = str(objective.text)[:120]
            context["title"] = str(objective.text)[:120]
        payoffs = getattr(memory, "payoffs", ()) or ()
        if payoffs:
            context["recap"] = str(getattr(payoffs[0], "why", ""))[:120]

    if caption_plan is not None and not context.get("objective"):
        for decision in getattr(caption_plan, "accepted", []) or []:
            if decision.moment == "objective":
                context["objective"] = decision.full_line[:120]
                context["title"] = decision.text[:120]
                break
    return context


def _caption_windows(caption_plan) -> list[tuple]:
    """Every accepted caption as ``(start, end, text)``, in sequence time."""
    if caption_plan is None:
        return []
    return [
        (decision.start, decision.end, decision.text)
        for decision in getattr(caption_plan, "accepted", []) or []
        if decision.start >= 0
    ]


def _hook_polish(caption_plan, audio_plan, retention_plan) -> int:
    """How much the opening already carries.

    A cold open, a caption over it and a sting under it are three things a
    viewer meets in the first few seconds. The fourth is where an opening
    starts feeling like an advert.
    """
    count = 0
    cold = getattr(retention_plan, "cold_open", None)
    window = 0.0
    if cold is not None and getattr(cold, "chosen", False):
        count += 1
        window = max(4.0, float(cold.duration or 0.0))
    if window <= 0:
        window = 8.0

    for decision in getattr(caption_plan, "accepted", []) or []:
        if 0 <= decision.start < window:
            count += 1
    for cue in getattr(audio_plan, "accepted", []) or []:
        if cue.start < window and cue.kind != "music_bed":
            count += 1
    return count


def _importance_by_moment(moments: Sequence[VisualMoment],
                          timeline: StructureTimeline) -> dict:
    """The strongest picture importance under each moment.

    The safety pass needs this to tell "a freeze on a death" from "a freeze
    mid-fight", and looking it up once here is cheaper than walking the
    timeline inside a rule that runs per candidate.
    """
    by_segment = {s.segment_id: s for s in timeline.segments}
    out: dict = {}
    for moment in moments:
        best = ""
        weight = -1.0
        for segment_id in moment.segment_ids:
            segment = by_segment.get(segment_id)
            if segment is None:
                continue
            from editing.schema import IMPORTANCE_WEIGHT

            score = IMPORTANCE_WEIGHT.get(segment.importance, 0.0)
            if score > weight:
                best, weight = segment.importance, score
        out[moment.moment_id] = best
    return out


# ---------------------------------------------------------------------------
# Finishing
# ---------------------------------------------------------------------------

def _finish(plan: VisualLayerPlan) -> None:
    plan.treatments.sort(key=lambda t: (t.start, t.treatment_id))

    placeholders = [t for t in plan.accepted
                    if t.target_output == "placeholder_only"]
    if placeholders:
        plan.safety_notes.append(
            f"{len(placeholders)} accepted treatment(s) are notes rather than "
            "operations: nothing in this system can express them, and the "
            "plan says which and why."
        )
    if plan.lowered:
        plan.safety_notes.append(
            f"{len(plan.lowered)} treatment(s) were softened rather than "
            "refused. Each carries the rule that softened it."
        )

    unnamed = [t for t in plan.accepted
               if t.effect in ("arrow_callout", "circle_highlight",
                               "box_highlight", "entity_callout")]
    if unnamed:
        plan.safety_notes.append(
            f"{len(unnamed)} callout(s) name a target and not a position. "
            "This system knows what is on screen and never where, so every "
            "one of them has to be placed by hand."
        )

    plan.safety_notes.append(NOT_RENDERED)
    plan.safety_notes.append(NOT_MEASURED)

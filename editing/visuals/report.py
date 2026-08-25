"""The visual reports: what was added, what was refused, and what to check.

Organised around the six questions the brief asks the review index to answer,
because they are the ones a person actually has about a layer whose entire job
is restraint:

1. what visual effects were added?
2. why were they added?
3. which moments got no visuals, and why?
4. what is placeholder-only?
5. what can be executed in Premiere later?
6. what might be cringe or overdone?

Point three and point six get the most room. A layer that treated everything it
found would be a different, worse feature, and the untreated list is what
proves it did not.
"""
from __future__ import annotations

from typing import Optional

from editing.visuals.execution import (
    FinalEditPlan, PremiereVisualOperationPlan, VisualReport,
)
from editing.visuals.schema import (
    NOT_MEASURED, NOT_RENDERED, PREVIEW_NOTE, VisualLayerPlan, now,
)

_RULE = "=" * 78
_THIN = "-" * 78

#: Plain-English names for the refusal codes, so a report does not make a
#: reader look them up.
REASONS = {
    "too_close_to_another": "too close to another effect",
    "density_limit": "the effect budget was full",
    "caption_overlap": "a caption is already on screen there",
    "hides_hud": "it would cover or crop the HUD",
    "low_confidence": "the moment was not certain enough",
    "interrupts_action": "it would stop the thing the viewer came for",
    "shake_during_combat": "shaking the frame while there is something to aim at",
    "unknown_target": "nothing named on screen to point at",
    "too_long": "longer than the duration ceiling",
    "repeated_effect": "the same effect too many times",
    "hook_already_polished": "the opening already carries enough",
    "clip_too_short": "the clip cannot carry it",
    "low_transcript_confidence": "the speech behind it was unclear",
    "weak_visual_label": "the vision pass named nothing here",
    "style_forbids": "this style does not use that effect",
    "layer_forbids": "this run turned that effect off",
    "no_evidence": "nothing to fill the effect with",
    "disabled": "the visual layer is off",
}

#: Signs that a plan is doing too much. Not refusals -- every safety rule
#: already passed -- but the things to look at first if it feels over-edited.
OVERDONE_RATE = 3.0
OVERDONE_CALLOUT_RATE = 1.5


def render(plan: VisualLayerPlan, *, limit: int = 40) -> str:
    """The visual plan, readable."""
    lines: list[str] = []
    add = lines.append
    stats = plan.stats()

    add(_RULE)
    add(f"VISUAL LAYER -- {plan.name}")
    add(_RULE)
    add(f"layer       : {plan.layer}")
    add(f"style       : {plan.style}")
    add(f"cut         : {plan.sequence_name or '(unnamed)'} "
        f"({stats['cut_duration']:.0f}s, the {plan.base} cut)")
    add(f"moments     : {stats['moments']} found")
    add(f"considered  : {stats['considered']} treatment(s)")
    add(f"planned     : {stats['accepted']} "
        f"({stats['effects_per_minute']:.2f} a minute, ceiling "
        f"{plan.config.max_effects_per_minute:.2f})")
    add(f"callouts    : {stats['by_family'].get('callout', 0)} "
        f"({stats['callouts_per_minute']:.2f} a minute, ceiling "
        f"{plan.config.max_callouts_per_minute:.2f})")
    add(f"refused     : {stats['rejected']}")
    add(f"softened    : {stats['lowered']}")
    add("")

    # -- 1 and 2: what and why --------------------------------------------
    add(_THIN)
    add("WHAT WOULD BE ON SCREEN")
    add(_THIN)
    if plan.accepted:
        for treatment in sorted(plan.accepted, key=lambda t: t.start)[:limit]:
            detail = str(treatment.payload.get("text")
                         or treatment.payload.get("target") or "")
            add(f"  [{treatment.start:7.2f}-{treatment.end:7.2f}] "
                f"{treatment.effect:<22} {treatment.intensity:<7}"
                + (f' "{detail[:40]}"' if detail else ""))
            add(f"      for  : the {treatment.moment_kind.replace('_', ' ')} "
                f"here ({treatment.source_type})")
            add(f"      why  : {treatment.reason[:140]}")
            for note in treatment.safety_notes[:2]:
                add(f"      note : {note[:140]}")
        if len(plan.accepted) > limit:
            add(f"  ... and {len(plan.accepted) - limit} more.")
    else:
        add("  Nothing. No moment cleared every rule, which is a normal "
            "result")
        add("  for a layer that only treats what the episode earns.")
    add("")

    # -- 3: what got nothing ----------------------------------------------
    untreated = plan.untreated_moments()
    add(_THIN)
    add(f"MOMENTS THAT GOT NOTHING ({len(untreated)})")
    add(_THIN)
    if untreated:
        for moment in untreated[:limit]:
            refusals = [t for t in plan.rejected
                        if t.moment_id == moment.moment_id]
            why = (refusals[0].reject_detail if refusals
                   else "no treatment in the library suited it")
            add(f"  [{moment.start:7.2f}] {moment.kind:<20} "
                f"{moment.label[:44]}")
            add(f"      why  : {why[:140]}")
        if len(untreated) > limit:
            add(f"  ... and {len(untreated) - limit} more.")
    else:
        add("  Every moment found earned something.")
    add("")

    # -- the refusal tally -------------------------------------------------
    add(_THIN)
    add(f"WHAT WAS REFUSED ({stats['rejected']})")
    add(_THIN)
    if stats["by_reject_reason"]:
        for code, count in sorted(
            stats["by_reject_reason"].items(), key=lambda kv: -kv[1]
        ):
            add(f"  {count:>4}  {code:<26} {REASONS.get(code, '')}")
    else:
        add("  Nothing was refused.")
    add("")

    # -- 4 and 5: where each one can go ------------------------------------
    add(_THIN)
    add("WHERE THESE CAN GO")
    add(_THIN)
    targets = stats["by_target"]
    add(f"  Premiere operations : {targets.get('premiere_plan', 0)}")
    add(f"  FFmpeg-only         : {targets.get('ffmpeg_preview', 0)}")
    add(f"  Notes only          : {targets.get('placeholder_only', 0)}")
    add("")
    add(f"  {NOT_RENDERED}")
    add("")

    # -- 6: what might be too much ----------------------------------------
    risks = overdone_risks(plan)
    add(_THIN)
    add("WHAT MIGHT BE OVERDONE")
    add(_THIN)
    for line in risks:
        add(f"  - {line}")
    add("")

    if plan.warnings:
        add(_THIN)
        add(f"WARNINGS ({len(plan.warnings)})")
        add(_THIN)
        for warning in plan.warnings[:20]:
            add(f"  ! {warning}")
        add("")

    add(_THIN)
    add("CHECK BY HAND")
    add(_THIN)
    for line in manual_checks(plan):
        add(f"  - {line}")
    add("")
    add(f"  {NOT_MEASURED}")
    add(_RULE)
    return "\n".join(lines)


def overdone_risks(plan: VisualLayerPlan) -> list[str]:
    """Where this plan is most likely to read as try-hard.

    Nothing here is a refusal -- every safety rule already passed. These are
    the places a person should look first if the result feels over-edited,
    which is a judgement only they can make.
    """
    out: list[str] = []
    stats = plan.stats()

    if not plan.accepted:
        return ["Nothing was planned, so nothing can be overdone."]

    if stats["effects_per_minute"] > OVERDONE_RATE:
        out.append(
            f"{stats['effects_per_minute']:.1f} effects a minute is one every "
            "twenty seconds. Past about three a minute the effects stop "
            "marking moments and start being the edit."
        )
    if stats["callouts_per_minute"] > OVERDONE_CALLOUT_RATE:
        out.append(
            f"{stats['callouts_per_minute']:.1f} callouts a minute. An arrow "
            "is the effect that most quickly reads as try-hard."
        )
    if plan.layer == "high":
        out.append(
            "this plan was made at the 'high' layer, which lets one moment "
            "carry three effects. Watch a minute of it before trusting the "
            "rest."
        )

    repeated = [(effect, count) for effect, count in plan.by_effect().items()
                if count >= 4]
    for effect, count in sorted(repeated, key=lambda pair: -pair[1]):
        out.append(
            f"{effect.replace('_', ' ')} appears {count} times. The fourth "
            "one stops being emphasis and starts being a tic."
        )

    meme = [t for t in plan.accepted
            if t.effect in ("freeze_frame_label", "impact_flash",
                            "zoom_punch", "screen_shake")]
    if len(meme) >= 3:
        out.append(
            f"{len(meme)} of the treatments are the loud kind -- punches, "
            "flashes, freeze labels. That is a choice rather than a mistake, "
            "and it is the choice most likely to age badly."
        )

    picture = [t for t in plan.accepted if t.changes_the_picture]
    if len(picture) >= 6:
        out.append(
            f"{len(picture)} treatment(s) scale or move the picture. Each one "
            "is checked against the HUD individually and none against the "
            "others."
        )

    if not out:
        out.append(
            "Nothing stands out. That is not the same as it being good -- no "
            "number here has watched the video."
        )
    return out


def manual_checks(plan: VisualLayerPlan) -> list[str]:
    """What a person has to settle before trusting this plan."""
    out: list[str] = []
    if not plan.accepted:
        out.append(
            "Nothing was planned. Read the refusal list above: if the reasons "
            "are mostly 'style forbids', the style is doing its job, and if "
            "they are mostly 'low confidence' the earlier passes are the "
            "problem."
        )
        return out

    callouts = [t for t in plan.accepted if t.family == "callout"]
    if callouts:
        out.append(
            f"Place every one of the {len(callouts)} callout(s) by hand. This "
            "system knows what is on screen and never where."
        )
    picture = [t for t in plan.accepted if t.changes_the_picture]
    if picture:
        out.append(
            f"Check the HUD on the {len(picture)} clip(s) whose picture is "
            "scaled. The ceiling is computed for 16:9 and has not looked at "
            "your footage."
        )
    cards = [t for t in plan.accepted if t.family == "card"]
    if cards:
        out.append(
            f"Read the {len(cards)} card(s). Their words come from what was "
            "said or seen; nothing was written for them, which also means "
            "nothing was polished."
        )
    if plan.lowered:
        out.append(
            f"{len(plan.lowered)} treatment(s) were softened rather than "
            "refused. Decide whether the softer version is still worth having."
        )
    out.append(PREVIEW_NOTE)
    return out


# ---------------------------------------------------------------------------
# The structured report
# ---------------------------------------------------------------------------

#: The six questions the review index has to answer about this layer.
QUESTIONS = (
    "What visual effects were added?",
    "Why were they added?",
    "Which moments got no visuals, and why?",
    "What is placeholder-only?",
    "What can be executed in Premiere later?",
    "What might be cringe or overdone?",
)


def build_report(
    plan: VisualLayerPlan,
    *,
    premiere: Optional[PremiereVisualOperationPlan] = None,
    preview=None,
) -> VisualReport:
    """The six questions, each with what this plan actually did.

    ``preview`` sharpens the fourth answer: "what is placeholder-only" is
    incomplete without "and what a proxy could not show you either".
    """
    stats = plan.stats()
    answers: list[str] = []

    if plan.accepted:
        kinds = ", ".join(
            f"{count} x {effect.replace('_', ' ')}"
            for effect, count in sorted(
                plan.by_effect().items(), key=lambda kv: -kv[1])[:6])
        answers.append(
            f"{stats['accepted']} treatment(s) at "
            f"{stats['effects_per_minute']:.2f} a minute: {kinds}."
        )
    elif plan.config.enabled:
        answers.append(
            f"None. {stats['moments']} moment(s) were found and every "
            "treatment was refused; the plan names the rule for each."
        )
    else:
        answers.append("None. The visual layer was off for this run.")

    by_kind = plan.by_moment_kind()
    if by_kind:
        answers.append(
            "Each one is attached to a moment an earlier pass recorded: "
            + ", ".join(f"{count} on a {kind.replace('_', ' ')}"
                        for kind, count in sorted(
                            by_kind.items(), key=lambda kv: -kv[1])[:6])
            + ". Sources: "
            + ", ".join(f"{name} {count}"
                        for name, count in sorted(plan.by_source().items()))
            + "."
        )
    else:
        answers.append("Nothing was added, so nothing needs a reason.")

    untreated = plan.untreated_moments()
    if untreated:
        tally = plan.by_reject_reason()
        answers.append(
            f"{len(untreated)} of {stats['moments']} moment(s) earned nothing. "
            + ", ".join(f"{count} {REASONS.get(code, code)}"
                        for code, count in sorted(
                            tally.items(), key=lambda kv: -kv[1])[:5])
            + "."
        )
    else:
        answers.append(
            "Every moment found earned something, which on a short cut is "
            "normal and on a long one is worth a second look."
        )

    placeholders = [t for t in plan.accepted
                    if t.target_output == "placeholder_only"]
    invisible = len(preview.invisible) if preview is not None else 0
    if placeholders:
        answers.append(
            f"{len(placeholders)} treatment(s) are notes and nothing else: "
            + ", ".join(sorted({t.effect for t in placeholders})[:6])
            + ". Nothing in this system can express them."
            + (f" A further {invisible} can be planned in Premiere and not "
               "shown by FFmpeg in any form." if invisible else "")
        )
    else:
        answers.append(
            "Nothing is placeholder-only: every planned treatment maps onto "
            "an operation or an FFmpeg filter."
            + (f" {invisible} of them cannot be shown by FFmpeg in any form, "
               "and are in the marker file only." if invisible else "")
        )

    if premiere is not None:
        answers.append(
            f"{premiere.operation_count} operation(s) across "
            f"{len({e.treatment_id for e in premiere.operations})} "
            f"treatment(s), validated offline "
            f"({'passed' if premiere.dry_run_passed else 'NOT passed'}). "
            f"{len(premiere.unsupported)} treatment(s) have no catalog "
            "representation. Nothing has been executed."
        )
    else:
        answers.append(
            "No Premiere plan was built for this run. "
            "--visual-mode premiere_plan builds one; it still executes "
            "nothing."
        )

    risks = overdone_risks(plan)
    answers.append(risks[0] if risks else "Nothing stands out.")

    return VisualReport(
        name=plan.name,
        layer=plan.layer,
        style=plan.style,
        stats=stats,
        answers=[{"question": question, "answer": answer}
                 for question, answer in zip(QUESTIONS, answers)],
        overdone_risks=risks,
        manual_checks=manual_checks(plan),
        warnings=list(plan.warnings),
        generated_at=now(),
    )


# ---------------------------------------------------------------------------
# The final edit
# ---------------------------------------------------------------------------

def render_final(final: FinalEditPlan, *, limit: int = 40) -> str:
    """The final edit plan, readable."""
    lines: list[str] = []
    add = lines.append
    stats = final.stats()

    add(_RULE)
    add(f"FINAL EDIT PLAN -- {final.name}")
    add(_RULE)
    add(f"mode        : {final.mode}")
    add(f"style       : {final.style}")
    add(f"cut         : {final.sequence_name or '(unnamed)'} "
        f"({stats['duration']:.0f}s, the {final.base} cut)")
    add(f"segments    : {stats['segments']} "
        f"({stats['busy_segments']} busy, "
        f"{stats['untouched_segments']} untouched)")
    add(f"visuals     : {stats['visual_treatments']} planned, "
        f"{stats['visual_rejected']} refused "
        f"({stats['effects_per_minute']:.2f} a minute)")
    add(f"captions    : {stats['captions']}")
    add(f"audio cues  : {stats['audio_cues']}")
    add(f"executed    : no")
    add("")

    add(_THIN)
    add("THE EDIT, CLIP BY CLIP")
    add(_THIN)
    for segment in final.segments[:limit]:
        flags = " ".join(filter(None, [
            "protected" if segment.protected else "",
            f"{segment.speed:g}x" if segment.speed != 1.0 else "",
            "BUSY" if segment.is_busy else "",
        ]))
        add(f"  [{segment.start:7.2f}-{segment.end:7.2f}] "
            f"{segment.keep_reason:<16} {flags}")
        if segment.treatments:
            add(f"      visuals : {len(segment.treatments)} "
                + ", ".join(_effect_names(final, segment.treatments)[:4]))
        if segment.captions:
            add(f"      captions: {len(segment.captions)}")
        if segment.audio_cues:
            add(f"      audio   : {len(segment.audio_cues)}")
        for note in segment.notes[:2]:
            add(f"      note    : {note[:130]}")
    if len(final.segments) > limit:
        add(f"  ... and {len(final.segments) - limit} more.")
    add("")

    execution = final.execution
    add(_THIN)
    add("WHAT COULD BE DONE WITH THIS")
    add(_THIN)
    if execution.premiere is not None:
        add(f"  Premiere : {execution.premiere.operation_count} operation(s), "
            f"dry run "
            f"{'passed' if execution.premiere.dry_run_passed else 'NOT passed'}")
        add(f"             {len(execution.premiere.unsupported)} treatment(s) "
            "have no catalog representation")
    else:
        add("  Premiere : no plan was built (--visual-mode premiere_plan)")
    if execution.preview is not None:
        preview_stats = execution.preview.stats()
        add(f"  FFmpeg   : {preview_stats['burnable']} of "
            f"{preview_stats['items']} could be burned into a preview render, "
            "and none was")
        add(f"             markers: "
            f"{execution.preview.sidecar_path or '(not written)'}")
    else:
        add("  FFmpeg   : no preview plan was built "
            "(--visual-mode proxy_preview)")
    add(f"  Executed : no. {NOT_RENDERED}")
    add("")

    if final.warnings:
        add(_THIN)
        add(f"WARNINGS ({len(final.warnings)})")
        add(_THIN)
        for warning in final.warnings[:20]:
            add(f"  ! {warning}")
        add("")

    add(_THIN)
    add("CHECK BY HAND")
    add(_THIN)
    for line in final.manual_checks:
        add(f"  - {line}")
    add("")
    add(f"  {NOT_MEASURED}")
    add(_RULE)
    return "\n".join(lines)


def _effect_names(final: FinalEditPlan, treatment_ids) -> list[str]:
    by_id = {t.treatment_id: t.effect for t in final.visuals.accepted}
    return [by_id.get(item, item) for item in treatment_ids]


def render_comparison(report) -> str:
    """The visual layer against the cut without it. Counts only."""
    lines: list[str] = []
    add = lines.append
    stats = report.stats()

    add(_RULE)
    add(f"VISUAL LAYER vs THE BARE CUT -- {report.name}")
    add(_RULE)
    add(f"layer          : {report.layer}")
    add(f"cut            : {report.cut_duration:.0f}s")
    add(f"segments       : {stats['segments']} "
        f"({stats['segments_touched']} treated, "
        f"{stats['segments_untouched']} untouched, "
        f"{stats['segments_busy']} busy)")
    add(f"treatments     : {stats['treatments']} planned, "
        f"{stats['rejected']} refused")
    add(f"density        : {stats['effects_per_minute']:.2f} effects a minute, "
        f"{stats['callouts_per_minute']:.2f} callouts a minute")
    add("")

    if report.by_family:
        add(_THIN)
        add("BY FAMILY")
        add(_THIN)
        for family, count in sorted(report.by_family.items(),
                                    key=lambda kv: -kv[1]):
            add(f"  {count:>4}  {family}")
        add("")

    if report.untreated:
        add(_THIN)
        add(f"FOUND AND LEFT ALONE ({len(report.untreated)})")
        add(_THIN)
        for entry in report.untreated[:30]:
            add(f"  [{entry['at']:7.2f}] {entry['kind']:<20} "
                f"{entry['label'][:40]}")
            add(f"      {entry['why'][:130]}")
        if len(report.untreated) > 30:
            add(f"  ... and {len(report.untreated) - 30} more.")
        add("")

    for note in report.notes:
        add(f"  {note}")
    add(_RULE)
    return "\n".join(lines)

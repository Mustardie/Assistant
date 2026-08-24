"""The polish reports: what was added, what was refused, and what to check.

Both reports are organised around the same three questions, because they are
the ones a person actually has about a pass whose entire job is restraint:

1. how much was added?
2. **what was considered and refused, and by which rule?**
3. what do I have to check by ear or by eye?

Point two gets the most room. A caption pass that placed four captions out of
sixty candidates and a caption pass that is broken both print "4"; only the
refusal list tells them apart.
"""
from __future__ import annotations

from editing.polish.schema import (
    NOT_MEASURED, AudioPolishPlan, CaptionPlan,
)
from editing.polish.sidecar import BURN_IN_NOTE

_RULE = "=" * 78
_THIN = "-" * 78

#: Plain-English names for the refusal codes, so a report does not make a
#: reader look them up.
CAPTION_REASONS = {
    "not_a_key_moment": "not one of the moments this pass captions",
    "boring_explanation": "explains rather than lands",
    "too_long": "the line is a paragraph",
    "too_many_words": "too many words to condense honestly",
    "unclear_transcript": "the transcript could not hear it",
    "low_confidence": "the speech confidence is too low",
    "background_speech": "reads as background rather than commentary",
    "repeated_filler": "filler or an annotation",
    "duplicate_line": "the same text is already captioned",
    "cut_from_the_edit": "the line is not in the cut",
    "blocked_by_ui": "a full-screen menu is open",
    "style_forbids_text": "this style puts no text on screen",
    "no_safe_zone": "no safe place on screen for it",
    "density_limit": "the caption budget was full",
    "too_close_to_another": "too close to another caption",
    "disabled": "captions are off",
}

AUDIO_REASONS = {
    "no_moment": "nothing here to mark",
    "would_cover_speech": "it would land on a spoken word",
    "too_close_to_another": "too close to another cue",
    "density_limit": "the effect budget was full",
    "style_forbids": "this style does not use that cue",
    "bed_not_allowed": "no bed was allowed for this run",
    "no_asset": "nothing in the library fits",
    "clip_too_short": "the clip is too short to carry it",
    "duplicate_cue": "the same cue is already there",
    "disabled": "audio polish is off",
}


def render_captions(plan: CaptionPlan, *, limit: int = 40) -> str:
    """The caption plan, readable."""
    lines: list[str] = []
    add = lines.append
    stats = plan.stats()

    add(_RULE)
    add(f"CAPTION POLISH -- {plan.name}")
    add(_RULE)
    add(f"mode        : {plan.mode}")
    add(f"style       : {plan.style}")
    add(f"cut         : {plan.sequence_name or '(unnamed)'} "
        f"({stats['cut_duration']:.0f}s)")
    add(f"considered  : {stats['considered']} spoken line(s)")
    add(f"captioned   : {stats['accepted']} "
        f"({stats['captions_per_minute']:.2f} a minute, ceiling "
        f"{plan.config.max_per_minute:.2f})")
    add(f"refused     : {stats['rejected']}")
    add("")

    if plan.accepted:
        add(_THIN)
        add("WHAT GOES ON SCREEN")
        add(_THIN)
        for decision in plan.accepted[:limit]:
            add(f"  [{decision.start:7.2f}-{decision.end:7.2f}] "
                f"{decision.moment:<16} \"{decision.text}\"")
            add(f"      why  : {decision.reason[:150]}")
            if decision.condensed:
                add(f"      from : \"{decision.full_line[:100]}\"")
        if len(plan.accepted) > limit:
            add(f"  ... and {len(plan.accepted) - limit} more.")
        add("")
        add(f"  longest caption : {stats['longest_seconds']:.1f}s "
            f"(ceiling {plan.config.max_seconds:.1f}s)")
        add(f"  most words      : {stats['most_words']} "
            f"(ceiling {plan.config.max_words})")
    else:
        add(_THIN)
        add("WHAT GOES ON SCREEN")
        add(_THIN)
        add("  Nothing. No line cleared every rule, which is a normal result "
            "for")
        add("  a pass that only captions the moments carrying the episode.")
    add("")

    add(_THIN)
    add(f"WHAT WAS REFUSED ({stats['rejected']})")
    add(_THIN)
    if stats["by_reject_reason"]:
        for code, count in sorted(
            stats["by_reject_reason"].items(), key=lambda kv: -kv[1]
        ):
            add(f"  {count:>4}  {code:<22} {CAPTION_REASONS.get(code, '')}")
        add("")
        add("  The closest calls:")
        near = sorted(
            (d for d in plan.rejected
             if d.reject_reason in ("density_limit", "too_close_to_another")),
            key=lambda d: -d.priority,
        )
        for decision in near[:8]:
            add(f'    "{decision.text[:44]}" -- {decision.reject_detail[:90]}')
        if not near:
            add("    None: nothing was refused for want of room.")
    else:
        add("  Nothing was refused.")
    add("")

    add(_THIN)
    add("HOW TO SEE THEM")
    add(_THIN)
    add(f"  {BURN_IN_NOTE}")
    if plan.sidecar_path:
        add(f"  sidecar : {plan.sidecar_path}")
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
    for line in caption_checks(plan):
        add(f"  - {line}")
    add("")
    add(f"  {NOT_MEASURED}")
    add(_RULE)
    return "\n".join(lines)


def caption_checks(plan: CaptionPlan) -> list[str]:
    """What a person should look at before trusting this plan."""
    out: list[str] = []
    if not plan.accepted:
        out.append(
            "Nothing was captioned. Read the refusal list above: if the "
            "reasons are mostly 'not a key moment', the footage may simply "
            "not have any, and if they are mostly 'low confidence' the "
            "transcript is the problem."
        )
        return out
    out.append(
        "Read every caption against what is actually said at that moment. "
        "Condensed lines are marked; a condensed line that changes the "
        "meaning is the failure to look for."
    )
    if any(d.condensed for d in plan.accepted):
        out.append(
            "Some captions were condensed to their strongest phrase. The "
            "full line is in the report under each one."
        )
    out.append(
        "Check the placement zone against the HUD in the proxy. This pass "
        "avoids full-screen menus but has not looked at the picture."
    )
    if plan.mode == "dense":
        out.append(
            "This ran in dense mode, which is close to subtitles. Watch a "
            "minute of it before committing to the rest."
        )
    return out


def render_audio(plan: AudioPolishPlan, *, limit: int = 40) -> str:
    """The audio polish plan, readable."""
    lines: list[str] = []
    add = lines.append
    stats = plan.stats()

    add(_RULE)
    add(f"AUDIO POLISH -- {plan.name}")
    add(_RULE)
    add(f"mode        : {plan.mode}")
    add(f"style       : {plan.style}")
    add(f"cut         : {plan.sequence_name or '(unnamed)'} "
        f"({stats['cut_duration']:.0f}s)")
    add(f"considered  : {stats['considered']} cue(s)")
    add(f"planned     : {stats['accepted']} "
        f"({stats['sfx_per_minute']:.2f} effect(s) a minute, ceiling "
        f"{plan.config.max_sfx_per_minute:.2f})")
    add(f"from library: {stats['placed']}")
    add(f"placeholders: {stats['placeholders']}")
    add(f"refused     : {stats['rejected']}")
    add("")

    if plan.accepted:
        add(_THIN)
        add("WHAT WOULD BE HEARD")
        add(_THIN)
        for cue in sorted(plan.accepted, key=lambda c: c.start)[:limit]:
            source = cue.asset_filename or f"[{cue.placeholder or 'nothing'}]"
            add(f"  [{cue.start:7.2f}-{cue.end:7.2f}] {cue.kind:<13} {source}")
            add(f"      for  : {cue.target[:120]}")
            add(f"      why  : {cue.reason[:140]}")
            for note in cue.safety_notes[:3]:
                add(f"      note : {note[:140]}")
        if len(plan.accepted) > limit:
            add(f"  ... and {len(plan.accepted) - limit} more.")
    else:
        add(_THIN)
        add("WHAT WOULD BE HEARD")
        add(_THIN)
        add("  Nothing. Every cue was refused, which the list below explains.")
    add("")

    add(_THIN)
    add(f"WHAT WAS REFUSED ({stats['rejected']})")
    add(_THIN)
    if stats["by_reject_reason"]:
        for code, count in sorted(
            stats["by_reject_reason"].items(), key=lambda kv: -kv[1]
        ):
            add(f"  {count:>4}  {code:<22} {AUDIO_REASONS.get(code, '')}")
    else:
        add("  Nothing was refused.")
    add("")

    shopping = plan.shopping_list()
    if shopping:
        add(_THIN)
        add(f"MISSING ASSETS ({len(shopping)} kind(s))")
        add(_THIN)
        add("  Nothing plays at these moments. This list is what to go and "
            "find:")
        for entry in shopping[:20]:
            add(f"  {entry['count']:>3} x {entry['placeholder'][:50]:<50} "
                f"({entry['kind']})")
        add("")

    if plan.warnings:
        add(_THIN)
        add(f"WARNINGS ({len(plan.warnings)})")
        add(_THIN)
        for warning in plan.warnings[:20]:
            add(f"  ! {warning}")
        add("")

    add(_THIN)
    add("CHECK BY EAR")
    add(_THIN)
    for line in audio_checks(plan):
        add(f"  - {line}")
    add("")
    add(f"  {NOT_MEASURED}")
    add(_RULE)
    return "\n".join(lines)


def audio_checks(plan: AudioPolishPlan) -> list[str]:
    """What a person should listen for before trusting this plan."""
    out: list[str] = []
    if not plan.accepted:
        out.append(
            "Nothing was planned, so there is nothing to listen to. The "
            "refusal list above says why."
        )
        return out
    out.append(
        "Every level in this plan is unset. Nothing has been measured and "
        "nothing has been listened to."
    )
    if plan.placed:
        out.append(
            "The matched files were chosen on tags, folder and duration. "
            "Play each one at its moment before trusting the match."
        )
    if any(cue.kind in ("music_bed", "ambience") for cue in plan.accepted):
        out.append(
            "A bed is tiled rather than crossfaded. If the file does not "
            "loop cleanly the seam is audible."
        )
    if plan.missing:
        out.append(
            f"{len(plan.missing)} cue(s) have no sound behind them. They are "
            "notes about the edit, not something that will play."
        )
    out.append(
        "Nothing in this plan is in the rendered proxy. The proxy is the cut "
        "and its original audio, nothing else."
    )
    return out

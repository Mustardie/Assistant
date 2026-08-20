"""Turning what the critic saw into what the system proposes doing.

This is the module the session's safety rules live in. Everything the critic
reports arrives here as an observation with a confidence; what leaves is either

* an **accepted** revision carrying draft Premiere operations, or
* a revision the system **kept as a recommendation**, with the reason it could
  not be automated.

Nothing is discarded, and nothing is faked. A finding whose fix has no safe
automatic form does not quietly become a marker and get reported as fixed --
it stays a recommendation, and the report says so.

The rules that decide which side a finding lands on:

**Confidence gates action, severity does not.** A high-severity finding the
critic is 40% sure about is a note for a person, not an edit. Severity only
decides how loudly it is reported and whether it earns a marker on the
timeline.

**A fix may only act on something the plan knows is there.** ``reduce_zoom``
requires a zoom in the plan at that moment; ``trim_dead_air`` requires an audio
event confirming dead air; ``extend_hold`` requires source footage past the out
point. Without the premise, the fix is deferred with ``not_verifiable`` rather
than applied hopefully. A critic hallucinating a zoom must not be able to make
the system edit one that never existed.

**Conservative amounts, always.** Zooms reduce to a fixed gentle scale rather
than to whatever the model suggested; holds extend by at most half a second;
trims take at most a second and never leave a clip under a second long. A
revision pass is meant to fix obvious mistakes, not to re-edit.
"""
from __future__ import annotations

import time
from typing import Optional

from editing.critic.schema import (
    SAFE_FIXES, SEVERITY_ORDER, CriticFinding, CriticReport,
    RevisionRecommendation, RevisionSet, revision_id_for,
)
from editing.recommend.schema import RecommendationSet
from editing.roughcut.review import ReviewFrame, ReviewSet
from editing.roughcut.schema import RoughCutPlan

#: A finding below this is never turned into an operation, whatever it says.
#: Small VLMs are confidently wrong often enough that a 50/50 judgement is not
#: a basis for changing someone's edit.
MIN_ACTION_CONFIDENCE = 0.60

#: Timing changes move every later clip, so they need more than the baseline.
MIN_TIMING_CONFIDENCE = 0.70

#: Cutting footage out is the only irreversible-feeling fix here. The brief
#: asks for it only when confidence is high, and this is what "high" means.
MIN_TRIM_CONFIDENCE = 0.80

#: The scale a too-strong zoom is reduced *to*. Fixed rather than derived from
#: the model's suggestion: "make it a bit less" from a VLM is not a number, and
#: 106% is visible as emphasis without softening a 1080p source.
REDUCED_ZOOM_SCALE = 106.0

#: The most a hold may be extended automatically.
MAX_HOLD_EXTENSION = 0.5

#: The most dead air a single revision may trim.
MAX_TRIM_SECONDS = 1.0

#: A trim may never leave a clip shorter than this.
MIN_CLIP_AFTER_TRIM = 1.0

#: Below this, a timing change is not worth the ripple it causes.
MIN_TIMING_DELTA = 0.15

#: Audio events that count as evidence of dead air.
DEAD_AIR_TYPES = frozenset({"silence", "long_pause", "low_energy"})

#: Full-screen UI flags. When one is up, the centre of frame is a menu, so text
#: belongs below it rather than above.
FULLSCREEN_UI = frozenset({
    "inventory_open", "crafting_open", "chest_open", "map_open", "death_screen",
})


class RevisionOptions:
    """Which classes of fix this pass is allowed to propose.

    ``allow_timing`` is separated out because trims and extends are the only
    fixes here that move other clips. Everything else -- zoom changes, markers
    -- leaves the layout exactly as the rough cut computed it, which makes a
    markers-only revision pass strictly safer. Turning timing off is the
    conservative choice and is one flag away.
    """

    def __init__(
        self,
        *,
        allow_timing: bool = True,
        allow_zoom_edits: bool = True,
        min_confidence: float = MIN_ACTION_CONFIDENCE,
        max_hold_extension: float = MAX_HOLD_EXTENSION,
        max_trim: float = MAX_TRIM_SECONDS,
        reduced_zoom_scale: float = REDUCED_ZOOM_SCALE,
        #: Findings at or below this severity are recorded but never marked.
        marker_severity: str = "medium",
    ):
        self.allow_timing = allow_timing
        self.allow_zoom_edits = allow_zoom_edits
        self.min_confidence = max(0.0, min(1.0, float(min_confidence)))
        self.max_hold_extension = max(0.0, float(max_hold_extension))
        self.max_trim = max(0.0, float(max_trim))
        self.reduced_zoom_scale = max(100.0, float(reduced_zoom_scale))
        self.marker_severity = marker_severity

    def to_dict(self) -> dict:
        return {
            "allow_timing": self.allow_timing,
            "allow_zoom_edits": self.allow_zoom_edits,
            "min_confidence": self.min_confidence,
            "max_hold_extension": self.max_hold_extension,
            "max_trim": self.max_trim,
            "reduced_zoom_scale": self.reduced_zoom_scale,
            "marker_severity": self.marker_severity,
        }


def build_revisions(
    critique: CriticReport,
    review: ReviewSet,
    plan: RoughCutPlan,
    *,
    recommendations: Optional[RecommendationSet] = None,
    asset_durations: Optional[dict] = None,
    options: Optional[RevisionOptions] = None,
) -> RevisionSet:
    """One revision per critic finding, decided against the cut it describes."""
    options = options or RevisionOptions()
    asset_durations = asset_durations or {}
    frames = {frame.frame_id: frame for frame in review.frames}
    placements = {p.placement_id: p for p in plan.placements}

    revisions = RevisionSet(
        sequence_name=plan.sequence_name or critique.sequence_name,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        model=critique.model,
        mock=critique.mock,
    )
    if critique.mock:
        revisions.warnings.append(
            "These revisions come from the mock critic, which reads frame "
            "metadata rather than pictures. Do not execute them as if a model "
            "had looked at the cut."
        )

    for finding in critique.findings:
        frame = frames.get(finding.frame_id)
        if frame is None:
            revisions.warnings.append(
                f"Finding {finding.finding_id} names frame "
                f"{finding.frame_id}, which is not in the review manifest; it "
                "was kept but cannot be located on the timeline."
            )
        revisions.revisions.append(_revise(
            finding, frame, plan, placements, options, asset_durations
        ))

    revisions.revisions.sort(key=lambda r: (r.start, r.issue))
    _add_summary_warnings(revisions)
    return revisions


# ---------------------------------------------------------------------------
# One finding
# ---------------------------------------------------------------------------

def _revise(
    finding: CriticFinding,
    frame: Optional[ReviewFrame],
    plan: RoughCutPlan,
    placements: dict,
    options: RevisionOptions,
    asset_durations: dict,
) -> RevisionRecommendation:
    placement = placements.get(finding.placement_id)
    start = finding.sequence_time
    end = start
    if placement is not None:
        end = min(placement.sequence_end, start + 1.0)

    revision = RevisionRecommendation(
        revision_id=revision_id_for(finding.frame_id, finding.issue, start),
        source_recommendation_id=_source_recommendation(finding, frame),
        finding_id=finding.finding_id,
        frame_id=finding.frame_id,
        placement_id=finding.placement_id,
        start=start,
        end=end,
        issue=finding.issue,
        severity=finding.severity,
        confidence=finding.confidence,
        visual_evidence=finding.evidence,
        suggested_fix=finding.suggested_fix,
        notes=finding.notes,
    )
    if frame is not None:
        revision.transcript_evidence = frame.transcript
        revision.audio_evidence = list(frame.audio_types)

    if finding.mock:
        revision.notes = (
            (revision.notes + " | " if revision.notes else "")
            + "mock critic: not a real visual judgement"
        )

    # -- the gates, in order of how cheaply they can refuse ---------------
    if frame is None:
        return revision.defer(
            "The frame this finding refers to is not in the review manifest, "
            "so nothing on the timeline can be located to fix."
        )

    if finding.suggested_fix not in SAFE_FIXES:
        revision.risks.append("not_verifiable")
        return revision.defer(
            f"'{finding.suggested_fix}' has no safe automatic form in this "
            "system; it is a judgement call for an editor."
        )

    if finding.confidence < options.min_confidence:
        revision.risks.append("low_confidence")
        return revision.defer(
            f"The critic was only {finding.confidence:.0%} sure, below the "
            f"{options.min_confidence:.0%} needed to change the edit "
            "automatically."
        )

    handler = _HANDLERS.get(finding.suggested_fix)
    if handler is None:  # pragma: no cover - SAFE_FIXES and _HANDLERS agree
        return revision.defer(
            f"No handler is implemented for '{finding.suggested_fix}'."
        )
    return handler(revision, frame, placement, plan, options, asset_durations)


def _source_recommendation(
    finding: CriticFinding, frame: Optional[ReviewFrame]
) -> str:
    """The Session 2 recommendation this finding is about, when there is one.

    Prefers the recommendation behind the *specific edit* the critic
    complained about over whatever put the clip in the cut: a zoom complaint
    is about the punch-in, not about the payoff that kept the footage.
    """
    if frame is None:
        return ""
    wanted = {
        "zoom_too_strong": "zoom", "hud_hidden": "zoom", "action_hidden": "zoom",
        "remove_edit": "zoom",
        "text_unreadable": "text", "text_placed_badly": "text",
        "caption_covers_gameplay": "caption",
        "callout_needed": "callout", "marker_mismatch": "marker",
    }.get(finding.issue, "")
    if wanted:
        for edit in frame.applied_edits:
            if edit.get("kind") == wanted and edit.get("recommendation_id"):
                return str(edit["recommendation_id"])
    for edit in frame.applied_edits:
        if edit.get("recommendation_id"):
            return str(edit["recommendation_id"])
    return frame.recommendation_ids[0] if frame.recommendation_ids else ""


# ---------------------------------------------------------------------------
# Fix handlers
# ---------------------------------------------------------------------------

def _zoom_edit(frame: ReviewFrame) -> Optional[dict]:
    for edit in frame.applied_edits:
        if edit.get("kind") == "zoom":
            return edit
    return None


def _fix_remove_zoom(
    revision, frame, placement, plan, options, asset_durations
) -> RevisionRecommendation:
    """Drop the zoom entirely, back to an untouched frame."""
    zoom = _zoom_edit(frame)
    if zoom is None:
        revision.risks.append("not_verifiable")
        return revision.defer(
            "The critic asked to remove a zoom, but the plan applies no zoom "
            "at this moment. Nothing was changed."
        )
    if not options.allow_zoom_edits:
        return revision.defer("Zoom edits are disabled for this revision pass.")
    if placement is None:
        return revision.defer(
            "The clip this zoom sits on is not in the plan any more."
        )

    revision.risks.append("removes_an_edit")
    revision.fix_detail = (
        f"Reset Motion > Scale on the clip at {placement.sequence_midpoint:.2f}s, "
        f"clearing the zoom to {zoom.get('to')}%."
    )
    return revision.accept(
        "A zoom is planned here and removing it is a single reversible "
        "property reset.",
        [{
            "op": "property.reset",
            "clip": placement.selector(),
            "component": "Motion",
            "property": "Scale",
            "note": f"revision {revision.revision_id}: remove the "
                    f"{zoom.get('to')}% zoom -- {revision.visual_evidence[:80]}",
        }],
    )


def _fix_reduce_zoom(
    revision, frame, placement, plan, options, asset_durations
) -> RevisionRecommendation:
    """Keep the emphasis, at a scale that cannot crop the HUD.

    Two operations, not one. Animating Scale again on a clip that already has
    Scale keyframes stacks a second animation on the first; resetting before
    re-animating is what makes the result the intended single gentle push
    rather than an unpredictable sum.
    """
    zoom = _zoom_edit(frame)
    if zoom is None:
        revision.risks.append("not_verifiable")
        return revision.defer(
            "The critic called the zoom too strong, but the plan applies no "
            "zoom at this moment. Nothing was changed."
        )
    if not options.allow_zoom_edits:
        return revision.defer("Zoom edits are disabled for this revision pass.")
    if placement is None:
        return revision.defer(
            "The clip this zoom sits on is not in the plan any more."
        )

    current = float(zoom.get("to") or 100.0)
    target = options.reduced_zoom_scale
    if current <= target + 0.5:
        return revision.defer(
            f"The planned zoom is already {current:g}%, which is at or below "
            f"the {target:g}% this fix would reduce it to. Reducing it further "
            "is an editorial choice, not a correction."
        )

    start = float(zoom.get("start") or placement.sequence_start)
    duration = max(0.4, float(zoom.get("duration") or 0.8))
    selector = placement.selector()
    revision.fix_detail = (
        f"Reduce the zoom from {current:g}% to {target:g}% over "
        f"{duration:.2f}s from {start:.2f}s."
    )
    return revision.accept(
        f"The zoom is planned and measurable ({current:g}%), so reducing it to "
        f"a fixed {target:g}% is a bounded, reversible change.",
        [
            {
                "op": "property.reset",
                "clip": selector,
                "component": "Motion",
                "property": "Scale",
                "note": f"revision {revision.revision_id}: clear the "
                        f"{current:g}% zoom before re-animating it smaller",
            },
            {
                "op": "animate",
                "clip": selector,
                "component": "Motion",
                "property": "Scale",
                "from": 100.0,
                "to": round(target, 3),
                "start": round(start, 3),
                "duration": round(duration, 3),
                "easing": "ease_out",
                "relative_to": "sequence",
                "note": f"revision {revision.revision_id}: {current:g}% -> "
                        f"{target:g}% -- {revision.visual_evidence[:80]}",
            },
        ],
    )


def _fix_move_text(
    revision, frame, placement, plan, options, asset_durations
) -> RevisionRecommendation:
    """Re-site a text/caption/callout placeholder away from the busy area.

    No text exists on this timeline: Session 3 converts text categories into
    marker placeholders, and that is what this moves. Saying so plainly matters
    -- a revision that claimed to have repositioned a title would be a lie
    about work that was never done.
    """
    placeholder = next(
        (edit for edit in frame.applied_edits
         if edit.get("kind") in ("text", "caption", "callout")),
        None,
    )
    if placeholder is None:
        revision.risks.append("not_verifiable")
        return revision.defer(
            "No text, caption or callout placeholder is planned at this "
            "moment, so there is nothing to move."
        )

    at = float(placeholder.get("at") or revision.start)
    name = str(placeholder.get("name") or "TEXT")
    zone = _safe_text_zone(frame)
    detail = str(placeholder.get("detail") or "")
    revision.risks.append("annotation_only")
    revision.fix_detail = (
        f"Move the {placeholder.get('kind')} placeholder to the {zone}. "
        "No graphic exists yet; this rewrites the placeholder marker that "
        "tells the editor where it goes."
    )
    return revision.accept(
        "The placeholder is a marker, so re-siting it is a marker rewrite -- "
        "it changes guidance, not picture.",
        [
            {
                "op": "marker.remove",
                "at": round(at, 3),
                "name": name,
                "note": f"revision {revision.revision_id}: replacing the "
                        f"{name} placeholder with a re-sited one",
            },
            {
                "op": "marker.add",
                "time": round(at, 3),
                "name": name,
                "type": "comment",
                "comment": (
                    f"PLACE IN THE {zone.upper()}. "
                    f"Critic: {revision.visual_evidence[:160]} | "
                    f"was: {detail[:200]}"
                )[:500],
                "note": f"revision {revision.revision_id}: re-sited "
                        f"{placeholder.get('kind')} placeholder",
            },
        ],
    )


def _safe_text_zone(frame: ReviewFrame) -> str:
    """Where text can go without covering what matters.

    Minecraft puts the health, hunger and hotbar across the bottom centre and
    the crosshair dead centre, so the upper thirds are the default safe ground.
    A full-screen menu inverts that: the menu owns the middle and the top, and
    the strip under it is what is left.
    """
    flags = set(frame.ui_flags)
    if flags & FULLSCREEN_UI:
        return "lower left third, clear of the open menu"
    if "chat_open" in flags:
        return "upper right third, clear of the chat overlay"
    return "upper left third, clear of the crosshair and the hotbar"


def _fix_color_marker(
    revision, frame, placement, plan, options, asset_durations
) -> RevisionRecommendation:
    """A colour note for the human, because colour is not safely automatable.

    Lumetri is reachable from the catalog, but "this frame is too dark" does
    not carry a number, and guessing an exposure lift would be inventing a
    grade. A marker is the honest conversion.
    """
    direction = {
        "too_dark": "lift the shadows / raise exposure",
        "too_bright": "pull the highlights / lower exposure",
    }.get(revision.issue, "check the grade")
    revision.risks.append("annotation_only")
    revision.fix_detail = (
        f"Leave a COLOR marker at {revision.start:.2f}s suggesting: {direction}."
    )
    return revision.accept(
        "Colour cannot be corrected safely from a single frame, so this "
        "becomes a marker for the grade pass rather than an automatic change.",
        [{
            "op": "marker.add",
            "time": round(revision.start, 3),
            "name": "COLOR",
            "type": "comment",
            "comment": (
                f"{revision.issue}: {direction}. "
                f"Critic ({revision.confidence:.0%}): "
                f"{revision.visual_evidence[:200]}"
            )[:500],
            "note": f"revision {revision.revision_id}: colour suggestion",
        }],
    )


def _fix_callout_marker(
    revision, frame, placement, plan, options, asset_durations
) -> RevisionRecommendation:
    revision.risks.append("annotation_only")
    revision.fix_detail = (
        f"Leave a CALLOUT marker at {revision.start:.2f}s."
    )
    return revision.accept(
        "A callout graphic does not exist yet, so the honest fix is to mark "
        "where one belongs.",
        [{
            "op": "marker.add",
            "time": round(revision.start, 3),
            "name": "CALLOUT",
            "type": "comment",
            "comment": (
                f"The critic says the viewer will miss something here: "
                f"{revision.visual_evidence[:200]}"
                + (f" | entities: {', '.join(frame.entities[:5])}"
                   if frame.entities else "")
            )[:500],
            "note": f"revision {revision.revision_id}: callout suggested",
        }],
    )


def _fix_review_marker(
    revision, frame, placement, plan, options, asset_durations
) -> RevisionRecommendation:
    revision.risks.append("annotation_only")
    revision.fix_detail = (
        f"Leave a REVIEW marker at {revision.start:.2f}s."
    )
    return revision.accept(
        "Nothing here can be fixed automatically, so the fix is to put it in "
        "front of a person at the right moment on the timeline.",
        [{
            "op": "marker.add",
            "time": round(revision.start, 3),
            "name": "REVIEW",
            "type": "comment",
            "comment": _review_comment(revision),
            "note": f"revision {revision.revision_id}: flagged for a human",
        }],
    )


def _review_comment(revision: RevisionRecommendation) -> str:
    parts = [
        f"{revision.issue} ({revision.severity}, "
        f"{revision.confidence:.0%} confident)",
        revision.visual_evidence[:200],
    ]
    if revision.transcript_evidence:
        parts.append(f"said: \"{revision.transcript_evidence[:80]}\"")
    if revision.audio_evidence:
        parts.append("audio: " + ", ".join(revision.audio_evidence[:3]))
    parts.append(f"[{revision.revision_id}]")
    return " | ".join(part for part in parts if part)[:500]


def _fix_extend_hold(
    revision, frame, placement, plan, options, asset_durations
) -> RevisionRecommendation:
    """Give the moment slightly more room, if the footage exists for it."""
    if not options.allow_timing:
        revision.risks.append("changes_timing")
        return revision.defer(
            "Timing changes are disabled for this revision pass "
            "(--no-timing). The finding stands as a recommendation."
        )
    if placement is None:
        return revision.defer("The clip this refers to is not in the plan.")
    if revision.confidence < MIN_TIMING_CONFIDENCE:
        revision.risks.append("low_confidence")
        return revision.defer(
            f"Extending a hold moves every later clip, which needs "
            f"{MIN_TIMING_CONFIDENCE:.0%} confidence; the critic gave "
            f"{revision.confidence:.0%}."
        )
    if placement.speed != 1.0:
        return revision.defer(
            f"The clip is retimed to {placement.speed:g}x, so extending it "
            "would stretch a speed change as well as the hold."
        )

    duration = asset_durations.get(placement.asset_id)
    if not duration:
        revision.risks.append("not_verifiable")
        return revision.defer(
            "The source file's length is unknown here, so there is no way to "
            "confirm footage exists past the out point. Run `discover` so the "
            "asset list is available, or extend it by hand."
        )

    headroom = float(duration) - placement.source_out
    delta = min(options.max_hold_extension, max(0.0, headroom))
    if delta < MIN_TIMING_DELTA:
        return revision.defer(
            f"Only {max(0.0, headroom):.2f}s of footage remains after this "
            "clip's out point -- not enough to extend the hold usefully."
        )

    revision.risks.append("changes_timing")
    revision.fix_detail = (
        f"Extend the clip's out point by {delta:.2f}s, rippling everything "
        "after it later by the same amount."
    )
    return revision.accept(
        f"There is {headroom:.2f}s of source footage past the out point, so "
        f"a {delta:.2f}s extension is a bounded, reversible trim.",
        [{
            "op": "clip.trim",
            "clip": placement.selector(),
            "edge": "out",
            # Negative shortens nothing and extends the edge -- see the
            # catalog: "Positive 'by' shortens, negative extends."
            "by": round(-delta, 3),
            "ripple": True,
            "note": f"revision {revision.revision_id}: hold +{delta:.2f}s -- "
                    f"{revision.visual_evidence[:80]}",
        }],
    )


def _fix_trim_dead_air(
    revision, frame, placement, plan, options, asset_durations
) -> RevisionRecommendation:
    """Take a little dead air off a clip edge, when the audio agrees."""
    if not options.allow_timing:
        revision.risks.append("changes_timing")
        return revision.defer(
            "Timing changes are disabled for this revision pass "
            "(--no-timing). The finding stands as a recommendation."
        )
    if placement is None:
        return revision.defer("The clip this refers to is not in the plan.")
    if revision.confidence < MIN_TRIM_CONFIDENCE:
        revision.risks.append("low_confidence")
        return revision.defer(
            f"Cutting footage needs {MIN_TRIM_CONFIDENCE:.0%} confidence; the "
            f"critic gave {revision.confidence:.0%}. Kept as a recommendation."
        )

    quiet = [
        event for event in frame.audio_events
        if event.get("type") in DEAD_AIR_TYPES
    ]
    if not quiet:
        revision.risks.append("not_verifiable")
        return revision.defer(
            "No silence, long pause or low-energy audio event covers this "
            "moment, so the audio layer does not confirm dead air. The critic "
            "may be reading a still as a pause."
        )
    if frame.frame_kind not in ("clip_start", "clip_end"):
        return revision.defer(
            "The frame is from the middle of a clip, so there is no edge to "
            "trim. Removing dead air from the middle needs a split, which is "
            "a bigger change than this pass makes."
        )

    edge = "in" if frame.frame_kind == "clip_start" else "out"
    quiet_span = max(
        (float(event.get("end", 0.0)) - float(event.get("start", 0.0)))
        for event in quiet
    )
    delta = min(
        options.max_trim,
        quiet_span,
        max(0.0, placement.sequence_duration - MIN_CLIP_AFTER_TRIM),
    )
    if delta < MIN_TIMING_DELTA:
        return revision.defer(
            f"Trimming here would either take less than {MIN_TIMING_DELTA:.2f}s "
            f"or leave the clip under {MIN_CLIP_AFTER_TRIM:.1f}s long."
        )

    revision.risks.append("changes_timing")
    revision.fix_detail = (
        f"Trim {delta:.2f}s off the {edge} edge of the clip, rippling "
        "everything after it earlier by the same amount."
    )
    return revision.accept(
        f"The audio layer records {quiet_span:.2f}s of "
        f"{', '.join(sorted({e.get('type', '') for e in quiet}))} here, and "
        f"the critic is {revision.confidence:.0%} sure, so a {delta:.2f}s trim "
        "is supported by both channels.",
        [{
            "op": "clip.trim",
            "clip": placement.selector(),
            "edge": edge,
            "by": round(delta, 3),
            "ripple": True,
            "note": f"revision {revision.revision_id}: trim {delta:.2f}s of "
                    f"dead air off the {edge} edge",
        }],
    )


_HANDLERS = {
    "remove_zoom": _fix_remove_zoom,
    "reduce_zoom": _fix_reduce_zoom,
    "move_text_placeholder": _fix_move_text,
    "color_marker": _fix_color_marker,
    "callout_marker": _fix_callout_marker,
    "review_marker": _fix_review_marker,
    "extend_hold": _fix_extend_hold,
    "trim_dead_air": _fix_trim_dead_air,
}


def _add_summary_warnings(revisions: RevisionSet) -> None:
    """The things a person needs to be told before reading the list."""
    deferred = revisions.needing_human()
    if deferred:
        severe = [
            entry for entry in deferred
            if SEVERITY_ORDER.get(entry.severity, 0) >= 1
        ]
        revisions.warnings.append(
            f"{len(deferred)} finding(s) could not be fixed automatically"
            + (f", {len(severe)} of them medium or high severity" if severe else "")
            + ". They are kept as recommendations with the reason on each."
        )
    timing = [
        entry for entry in revisions.accepted()
        if "changes_timing" in entry.risks
    ]
    if timing:
        revisions.warnings.append(
            f"{len(timing)} accepted revision(s) change timing and will ripple "
            "later clips. Use --no-timing for a markers-and-zooms-only pass."
        )

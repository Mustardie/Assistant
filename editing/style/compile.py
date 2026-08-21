"""The layer compiler.

    recommendations + rough cut + style preset (+ optional critic findings)
        -> candidate items on seven layers
        -> deduplication
        -> density enforcement
        -> ordered Premiere operations
        -> offline dry run

The individual layers propose enthusiastically; this is the module that says
no. That split is the reason a styled edit can be tuned at all — the caption
builder does not need to know how many captions a style tolerates, and the
style does not need to know how a caption is chosen.

**This layer never changes timing.** Nothing it emits trims, retimes, moves or
removes a clip: it adds a track, scales clips that are already there, writes
audio level keyframes, places overlays and drops markers. That is not an
accident of the current implementation, it is enforced by the operation
allowlist in ``editing.style.execute`` — and it buys a property Session 4 could
not have: because no operation ripples, no marker or overlay can end up
describing a frame that moved out from under it. Every position in the plan is
the position it will have when it runs.

The density rules, in the order they are applied to each candidate:

1. **Style permission.** A forbidden kind never gets further.
2. **Confidence.** Below the style's ``min_confidence``, an item is a note, not
   an edit.
3. **Duplication.** Against the rough cut's existing markers, and against items
   already accepted on this pass.
4. **Spacing.** Per class, the wider of the style's explicit spacing and the
   spacing implied by its per-minute rate.
5. **Window count.** No more than the style's rate in any 60-second window.
6. **Stacking.** Two active edits inside ``min_stack_spacing`` are one too many.

Candidates are considered **most defensible first**, so a ceiling removes the
weakest ideas rather than whichever happened to come last — the same principle
as the Session 2 budget, applied per minute instead of per file.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Sequence

from editing.recommend.schema import RecommendationSet
from editing.roughcut.schema import RoughCutPlan
from editing.roughcut.select import map_to_sequence
from editing.schema import StructureTimeline
from editing.style import audio as audio_layer
from editing.style import captions as caption_layer
from editing.style import cards as card_layer
from editing.style import emphasis as emphasis_layer
from editing.style.captions import OVERLAY_TRACK
from editing.style.presets import DEFAULT_PRESET, StylePreset, get as get_preset
from editing.style.schema import (
    LayerEvidence, LayerItem, LayeredEditPlan, item_id_for,
)

#: Operations emitted, in the order they must run. Markers last so they land at
#: final positions; ``track.add`` first among the mutating ops because the
#: overlay track has to exist before anything is placed on it.
_OP_ORDER = (
    "sequence.activate", "track.add", "animate", "audio.fade", "text.create",
    "marker.add",
)

#: Two markers this close together, with the same name, are the same note.
MARKER_DEDUPE_GAP = 0.5

#: Risks that mean "this style has no room for it", as opposed to "this would
#: be unsafe". A structural item refused for room can still leave a note; one
#: refused for safety must not.
_DENSITY_RISKS = frozenset({
    "over_editing", "text_spam", "style_limited", "stacked",
})

#: Critic issues whose moments should be left alone by the style pass.
CRITIC_BLOCKERS = frozenset({
    "hud_hidden", "action_hidden", "bad_crop", "zoom_too_strong",
    "text_placed_badly", "caption_covers_gameplay", "text_unreadable",
})

#: Recommendation categories the marker layer turns into structure notes. These
#: are the ones Session 2 could never convert and Session 3 leaves as markers.
STRUCTURE_CATEGORIES = {
    "structure_cut": "structure_marker",
    "marker": "structure_marker",
    "beat_marker": "beat_marker",
    "color_adjust": "polish_marker",
    "transition": "polish_marker",
    "freeze_frame": "freeze_frame",
}


@dataclass
class CompileOptions:
    """Knobs that are about this run rather than about the style."""

    #: Include the rough cut's own clips as a ``base`` layer, for completeness.
    include_base: bool = True
    #: Let critic findings block emphasis and text at flagged moments.
    use_critic: bool = True
    #: Turn every convertible item into a marker instead. The safest possible
    #: pass: it draws nothing and scales nothing.
    markers_only: bool = False
    #: Hard ceiling on operations, whatever the density rules allow.
    max_operations: int = 400

    def to_dict(self) -> dict:
        return {
            "include_base": self.include_base,
            "use_critic": self.use_critic,
            "markers_only": self.markers_only,
            "max_operations": self.max_operations,
        }


def compile_layers(
    timeline: StructureTimeline,
    roughcut: RoughCutPlan,
    *,
    style: Optional[StylePreset] = None,
    recommendations: Optional[RecommendationSet] = None,
    revisions=None,
    options: Optional[CompileOptions] = None,
    roughcut_executed: bool = False,
) -> LayeredEditPlan:
    """Build every layer, enforce the style, and emit the operation plan."""
    style = (style or get_preset(DEFAULT_PRESET)).validated()
    options = options or CompileOptions()
    recommendations = recommendations or RecommendationSet()

    plan = LayeredEditPlan(
        sequence_name=roughcut.sequence_name,
        style=style.name,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        on_scratch=roughcut.on_scratch,
        roughcut_executed=roughcut_executed,
        cut_duration=roughcut.total_duration,
        preset=style.to_dict(),
    )
    if not roughcut.placements:
        plan.warnings.append(
            "The rough cut has no clips, so there is nothing to style. Run "
            "`roughcut build` first."
        )
        return plan

    blocked = _blocked_ranges(revisions) if options.use_critic else []
    if blocked:
        plan.warnings.append(
            f"{len(blocked)} moment(s) flagged by the critic are excluded from "
            "emphasis and text in this pass."
        )

    candidates: list[LayerItem] = []
    if options.include_base:
        candidates.extend(_base_layer(roughcut, style))
    candidates.extend(_marker_layer(recommendations, roughcut, style))
    candidates.extend(caption_layer.build_captions(
        timeline, roughcut, style,
        recommendations=recommendations, blocked_ranges=blocked,
    ))
    candidates.extend(emphasis_layer.build_emphasis(
        timeline, roughcut, style,
        recommendations=recommendations, blocked_ranges=blocked,
    ))
    candidates.extend(audio_layer.build_audio(
        timeline, roughcut, style, blocked_ranges=blocked,
    ))
    candidates.extend(card_layer.build_cards(
        timeline, roughcut, style, blocked_ranges=blocked,
    ))

    if options.markers_only:
        for item in candidates:
            _demote_to_marker(
                item, style,
                why="markers-only pass: nothing is drawn or scaled",
            )

    plan.items = _enforce(candidates, roughcut, style, plan)
    plan.ops = _operations(plan, style, options)
    _add_warnings(plan, style, options)
    return plan


# ---------------------------------------------------------------------------
# Layers the compiler owns
# ---------------------------------------------------------------------------

def _base_layer(roughcut: RoughCutPlan, style: StylePreset) -> list[LayerItem]:
    """The rough cut's own clips, as read-only context.

    These carry no operations: the clips are already on the timeline. They are
    here so a layered plan is a complete description of the sequence rather
    than a diff against a file you have to open separately.
    """
    items: list[LayerItem] = []
    for placement in roughcut.placements:
        item = LayerItem(
            item_id=f"li_base_{placement.placement_id}",
            layer="base",
            kind="pacing_marker",
            placement_id=placement.placement_id,
            start=placement.sequence_start,
            end=placement.sequence_end,
            source_start=placement.source_in,
            source_end=placement.source_out,
            asset_id=placement.asset_id,
            style=style.name,
            reason=f"{placement.keep_reason} clip from the rough cut"
                   + (f", retimed to {placement.speed:g}x"
                      if placement.speed != 1.0 else "")
                   + (" (protected hold)" if placement.protected else ""),
            effect="pacing",
            priority=0.5,
            evidence=LayerEvidence(
                segment_ids=list(placement.segment_ids),
                summary=f"{placement.source_in:.2f}-{placement.source_out:.2f}s "
                        f"of {placement.source_file.rsplit('/', 1)[-1]}",
            ),
            payload={
                "keep_reason": placement.keep_reason,
                "speed": placement.speed,
                "protected": placement.protected,
                "placeholder": "existing clip",
            },
            notes="already on the timeline; this layer adds no operation for it",
        )
        items.append(item)
    return items


def _marker_layer(
    recommendations: RecommendationSet,
    roughcut: RoughCutPlan,
    style: StylePreset,
) -> list[LayerItem]:
    """Structure and polish notes from recommendations nothing else realises.

    Session 2 proposed a set of categories that no pass can convert into a real
    edit — colour moves, transitions, beat anchors. Rather than let them
    disappear between sessions, they become notes on the timeline in the
    style's own naming, which is what an editor can actually use them for.
    """
    items: list[LayerItem] = []
    for entry in recommendations.recommendations:
        kind = STRUCTURE_CATEGORIES.get(entry.category)
        if kind is None or not style.allows(kind):
            continue
        if entry.status not in ("accepted", "downgraded"):
            continue

        at = map_to_sequence(roughcut.placements, entry.asset_id, entry.start)
        if at is None:
            continue
        placement = roughcut.placement_at(at)
        layer = "polish" if kind == "polish_marker" else "marker"
        item = LayerItem(
            item_id=item_id_for(kind, at, entry.recommendation_id),
            layer=layer,
            kind=kind,
            recommendation_id=entry.recommendation_id,
            placement_id=placement.placement_id if placement else "",
            start=at,
            end=at,
            source_start=entry.start,
            source_end=entry.end,
            asset_id=entry.asset_id,
            style=style.name,
            reason=entry.reason or f"{entry.category} proposed here",
            effect=entry.effects[0] if entry.effects else "clarity",
            priority=entry.priority,
            risks=list(entry.risks),
            evidence=LayerEvidence(
                visual_event_ids=list(entry.evidence.visual_event_ids),
                transcript_quotes=list(entry.evidence.transcript_quotes),
                audio_event_ids=list(entry.evidence.audio_event_ids),
                audio_types=list(entry.evidence.audio_types),
                segment_ids=list(placement.segment_ids) if placement else [],
                summary=entry.evidence.summary,
            ),
            payload={"placeholder": entry.category, "category": entry.category},
            notes=f"from a {entry.category} recommendation",
        )
        if "placeholder_only" not in item.risks:
            item.risks.append("placeholder_only")
        item.premiere_ops = [{
            "op": "marker.add",
            "time": round(at, 3),
            "name": style.marker_name(kind),
            "type": "comment",
            "comment": (
                f"{entry.category}: {entry.reason} | "
                f"priority {entry.priority:.2f} [{item.item_id}]"
            )[:500],
            "note": f"{kind} [{style.name}]",
        }]
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def _enforce(
    candidates: list[LayerItem],
    roughcut: RoughCutPlan,
    style: StylePreset,
    plan: LayeredEditPlan,
) -> list[LayerItem]:
    """Apply the style's limits, most defensible candidate first."""
    existing = _existing_markers(roughcut)
    accepted: list[LayerItem] = []
    base = [item for item in candidates if item.layer == "base"]
    contenders = [item for item in candidates if item.layer != "base"]

    order = sorted(contenders, key=lambda item: (-item.priority, item.start))
    for item in order:
        reason, risk = _refuse(
            item, accepted, existing, style, plan.cut_duration
        )
        if not reason:
            accepted.append(item)
            continue
        # A section boundary is structure, not decoration. When a card is held
        # back for room rather than for safety, the boundary still gets a
        # marker -- a documentary that silently loses a chapter has lost the
        # thing the style was chosen for, and a marker costs the viewer
        # nothing. Everything else is deferred as normal.
        if item.is_card and risk in _DENSITY_RISKS and not item.is_marker_only:
            _demote_to_marker(item, style, why=reason)
            if risk not in item.risks:
                item.risks.append(risk)
            accepted.append(item)
            continue
        item.defer(reason, risk=risk)

    everything = base + contenders
    everything.sort(key=lambda item: (item.start, item.layer, item.kind))
    return everything


def _refuse(
    item: LayerItem,
    accepted: Sequence[LayerItem],
    existing: Sequence[tuple],
    style: StylePreset,
    duration: float,
) -> tuple:
    """Why this item must not be planned, and the risk to record. Or ``("", "")``."""
    if not style.allows(item.kind):
        return (
            f"the {style.name} style does not use {item.kind}.",
            "style_limited",
        )
    if item.is_active and item.priority < style.min_confidence:
        return (
            f"the evidence scores {item.priority:.0%}, below the "
            f"{style.min_confidence:.0%} this style needs before it changes "
            "the picture or sound.",
            "low_confidence",
        )

    duplicate = _duplicate_of(item, accepted, existing, style)
    if duplicate:
        return duplicate, "repetitive"

    limit = style.limit_for(item.kind)
    if limit is not None:
        refusal = _rate_refusal(
            item, [a for a in accepted if a.kind == item.kind], limit,
            f"the style limits {item.kind} to {limit:g} per minute",
            duration=duration,
        )
        if refusal:
            return refusal, "style_limited"

    if item.is_caption:
        refusal = _rate_refusal(
            item, [a for a in accepted if a.is_caption],
            style.max_captions_per_minute,
            f"the style allows {style.max_captions_per_minute:g} caption(s) "
            "per minute",
            floor=style.min_caption_spacing,
            duration=duration,
        )
        if refusal:
            return refusal, "text_spam"

    if item.is_zoom:
        refusal = _rate_refusal(
            item, [a for a in accepted if a.is_zoom],
            style.max_zooms_per_minute,
            f"the style allows {style.max_zooms_per_minute:g} zoom(s) per minute",
            duration=duration,
        )
        if refusal:
            return refusal, "over_editing"

    if item.is_active:
        refusal = _rate_refusal(
            item, [a for a in accepted if a.is_active],
            style.max_edits_per_minute,
            f"the style allows {style.max_edits_per_minute:g} active edit(s) "
            "per minute",
            floor=style.min_edit_spacing if _same_kind_nearby(
                item, accepted, style.min_edit_spacing
            ) else 0.0,
            duration=duration,
        )
        if refusal:
            return refusal, "over_editing"

        stacked = _stacked(item, accepted, style)
        if stacked:
            return stacked, "stacked"
    return "", ""


def _rate_refusal(
    item: LayerItem,
    peers: Sequence[LayerItem],
    per_minute: float,
    label: str,
    *,
    floor: float = 0.0,
    duration: float = 0.0,
) -> str:
    """Enforce a per-minute ceiling. Returns the reason to refuse, or "".

    Two regimes, because "N per minute" means genuinely different things above
    and below one.

    **At least one a minute** is a *rolling window count*: no more than the
    rate inside any 60 seconds (or inside the whole cut, when the cut is
    shorter than that — otherwise four edits spread across 30 seconds would
    report as eight a minute). Spacing here comes from the style's own
    ``min_edit_spacing`` / ``min_caption_spacing``, because how tightly a style
    clusters is a separate choice from how many it allows: ``fast_funny`` wants
    seven a minute *and* is happy for three to land in the same ten seconds.

    **Below one a minute** cannot be a window count at all — floored, it is
    always zero. It becomes a *whole-cut budget* (``rate x minutes``) plus the
    spacing the rate implies (``60 / rate``). "0.4 zooms a minute" means one
    every 150 seconds, and none at all in a cut too short to have earned one.

    There is deliberately **no "at least one" floor**. Rounding up to one broke
    the ceiling in exactly the case that matters: a 30-second cut was getting a
    zoom against a 0.4-per-minute ceiling, five times its own limit. A ceiling
    a short cut may exceed is not a ceiling.
    """
    if per_minute <= 0:
        return f"{label}, which is none."

    runtime = float(duration) if duration and duration > 0 else 60.0
    nearest = min(
        (abs(item.start - peer.start) for peer in peers), default=None
    )

    if per_minute < 1.0:
        spacing = max(float(floor), 60.0 / per_minute)
        if nearest is not None and nearest < spacing:
            return (
                f"{label}, so they must sit at least {spacing:.0f}s apart; the "
                f"nearest is {nearest:.1f}s away."
            )
        budget = int(per_minute * runtime / 60.0)
        if budget < 1:
            return (
                f"{label}, which works out at less than one across the "
                f"{runtime:.0f}s of this cut."
            )
        if len(peers) >= budget:
            return (
                f"{label}, which allows {budget} across the whole "
                f"{runtime:.0f}s cut; {len(peers)} are already planned."
            )
        return ""

    window = min(60.0, runtime)
    if floor > 0 and nearest is not None and nearest < floor:
        return (
            f"{label}, and this style keeps them {floor:.0f}s apart; the "
            f"nearest is {nearest:.1f}s away."
        )

    allowed = int(per_minute * window / 60.0)
    span = "a minute" if window >= 60.0 else f"{window:.0f}s"
    if allowed < 1:
        return f"{label}, which is less than one across the {span} of this cut."

    # A full minute is a rolling window, so it reaches half a minute either
    # side. A shorter cut *is* the window, so it reaches across all of it.
    reach = window if window < 60.0 else 30.0
    in_window = sum(1 for peer in peers if abs(peer.start - item.start) <= reach)
    if in_window >= allowed:
        return (
            f"{label}, which allows {allowed} across {span}; {in_window} "
            f"already sit that close to {item.start:.1f}s."
        )
    return ""


def _same_kind_nearby(
    item: LayerItem, accepted: Sequence[LayerItem], gap: float
) -> bool:
    return any(
        peer.kind == item.kind and abs(peer.start - item.start) < gap
        for peer in accepted
    )


#: Which sense an edit competes for. Stacking is only a problem within one
#: channel: two things happening to the picture at once fight each other, but
#: an audio fade under a title card is ordinary editing, not noise.
_CHANNEL = {
    "reaction_caption": "picture", "key_phrase": "picture",
    "danger_text": "picture", "title_card": "picture",
    "chapter_card": "picture", "punch_in": "picture",
    "slow_push_in": "picture", "freeze_frame": "picture",
    "audio_fade_in": "sound", "audio_fade_out": "sound",
}


def _stacked(
    item: LayerItem, accepted: Sequence[LayerItem], style: StylePreset
) -> str:
    """Two changes to the same sense, too close together, of different kinds."""
    channel = _CHANNEL.get(item.kind)
    if channel is None:
        return ""
    for peer in accepted:
        if not peer.is_active or peer.kind == item.kind:
            continue
        if _CHANNEL.get(peer.kind) != channel:
            continue
        gap = abs(peer.start - item.start)
        if gap < style.min_stack_spacing:
            return (
                f"a {peer.kind} already sits {gap:.1f}s away and this style "
                f"keeps {channel} edits {style.min_stack_spacing:g}s apart; "
                "stacking them reads as noise."
            )
    return ""


def _duplicate_of(
    item: LayerItem,
    accepted: Sequence[LayerItem],
    existing: Sequence[tuple],
    style: StylePreset,
) -> str:
    """Whether this repeats something already on the timeline or in this pass."""
    name = style.marker_name(item.kind)
    for marker_name, marker_time in existing:
        if marker_name == name and abs(marker_time - item.start) <= MARKER_DEDUPE_GAP:
            return (
                f"the rough cut already placed a '{marker_name}' marker at "
                f"{marker_time:.2f}s; a second one says nothing new."
            )
    for peer in accepted:
        if peer.kind != item.kind:
            continue
        if abs(peer.start - item.start) <= MARKER_DEDUPE_GAP:
            return (
                f"another {item.kind} is already planned at "
                f"{peer.start:.2f}s ({peer.item_id})."
            )
        if item.is_caption and peer.is_caption and peer.overlaps(
            item.start, item.end
        ) > 0:
            return (
                f"a caption is already on screen from {peer.start:.2f}s to "
                f"{peer.end:.2f}s; two at once cannot both be read."
            )
    return ""


def _existing_markers(roughcut: RoughCutPlan) -> list[tuple]:
    """Markers the rough cut already put on the sequence, as (name, time)."""
    return [(marker.name, marker.time) for marker in roughcut.markers]


def _blocked_ranges(revisions) -> list[tuple]:
    """Moments the critic complained about, as ``(start, end, reason)``.

    Everything the critic flagged, whether or not the revision pass could fix
    it: an unfixed complaint is a stronger reason to stay away, not a weaker
    one.
    """
    if revisions is None:
        return []
    out: list[tuple] = []
    for revision in getattr(revisions, "revisions", []) or []:
        if revision.issue not in CRITIC_BLOCKERS:
            continue
        start = max(0.0, revision.start - 0.5)
        end = max(start, revision.end) + 0.5
        out.append((
            start, end,
            f"the critic reported {revision.issue} here "
            f"({revision.confidence:.0%} confident)",
        ))
    return out


def _demote_to_marker(
    item: LayerItem, style: StylePreset, *, why: str = "recorded rather than applied"
) -> None:
    """Replace a real edit with the note describing it.

    ``why`` reaches the marker's own comment, so someone reading the timeline
    learns whether this was a markers-only pass or a density decision rather
    than seeing a note that does not match what happened.
    """
    if not item.premiere_ops:
        return
    if all(str(op.get("op")) == "marker.add" for op in item.premiere_ops):
        return
    label = item.payload.get("text") or item.payload.get("label") or ""
    if not item.payload.get("placeholder"):
        item.payload["placeholder"] = label or item.kind
    if "placeholder_only" not in item.risks:
        item.risks.append("placeholder_only")
    item.notes = (item.notes + " | " if item.notes else "") + why
    op = {
        "op": "marker.add",
        "time": round(item.start, 3),
        "name": style.marker_name(item.kind),
        "type": "comment",
        "comment": (
            f"{item.kind.replace('_', ' ').upper()}"
            + (f': "{label}"' if label else "")
            + f" | {item.reason} | not drawn: {why} [{item.item_id}]"
        )[:500],
        "note": f"{item.kind} recorded as a marker [{style.name}]",
    }
    if item.duration >= 0.25:
        op["duration"] = round(item.duration, 3)
    item.premiere_ops = [op]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _operations(
    plan: LayeredEditPlan, style: StylePreset, options: CompileOptions
) -> list[dict]:
    """Every planned item's operations, in an order that runs cleanly."""
    collected: list[tuple] = []
    for item in plan.items:
        if not item.is_convertible:
            continue
        for op in item.premiere_ops:
            collected.append((item.start, op))

    if not collected:
        plan.warnings.append(
            "Nothing in this pass converts into an operation, so the plan is "
            "empty. The items are still in the report with their reasons."
        )
        return []

    ops: list[dict] = [{
        "op": "sequence.activate",
        "name": plan.sequence_name,
        "note": "Style the rough cut's own scratch sequence. This plan never "
                "creates one.",
    }]
    if any(op.get("op") == "text.create" for _at, op in collected):
        ops.append({
            "op": "track.add",
            "video": 1,
            "note": f"An overlay track for the style pass. Everything this "
                    f"plan draws lands on {OVERLAY_TRACK}, so it can be "
                    f"removed as a unit.",
        })

    rank = {name: index for index, name in enumerate(_OP_ORDER)}
    ordered = sorted(
        collected,
        key=lambda pair: (rank.get(str(pair[1].get("op")), 99), pair[0]),
    )
    ops.extend(dict(op) for _at, op in ordered)

    if options.max_operations and len(ops) > options.max_operations:
        plan.warnings.append(
            f"The plan reached the {options.max_operations}-operation ceiling "
            f"and was cut from {len(ops)}. Raise --max-operations, or tighten "
            "the style."
        )
        ops = ops[: options.max_operations]
    return ops


def _add_warnings(
    plan: LayeredEditPlan, style: StylePreset, options: CompileOptions
) -> None:
    """The things a person needs to know before running this."""
    density = plan.density()

    if not plan.roughcut_executed:
        plan.warnings.append(
            "There is no record of this rough cut having been executed into "
            "Premiere, so the sequence this plan activates may not exist yet. "
            "Run `roughcut execute --yes` first."
        )
    if not plan.on_scratch:
        plan.warnings.append(
            "The rough cut this styles is not marked as being on a scratch "
            "sequence. Executing would edit whatever that sequence is."
        )

    if density["edits_per_minute"] > style.max_edits_per_minute + 0.01:
        plan.warnings.append(
            f"Density came out at {density['edits_per_minute']:.2f} active "
            f"edits per minute against a ceiling of "
            f"{style.max_edits_per_minute:g}. That should not happen -- treat "
            "it as a bug in the enforcement pass, not as a style choice."
        )
    if 0 < plan.cut_duration < 60.0:
        plan.warnings.append(
            f"The cut is only {plan.cut_duration:.0f}s. A per-minute ceiling "
            "that works out at less than one edit across a cut this short "
            "allows none at all, so a restrained style may plan nothing but "
            "markers here. That is the ceiling being honest, not a failure."
        )

    marker_only = sum(1 for item in plan.planned() if item.is_marker_only)
    if marker_only:
        plan.warnings.append(
            f"{marker_only} planned item(s) are markers rather than edits: "
            "either the asset does not exist (music, SFX, callout graphics) or "
            "the placement could not be made safely. `layers show-deferred` "
            "explains each one."
        )

    deferred = plan.deferred()
    if deferred:
        plan.warnings.append(
            f"{len(deferred)} candidate(s) were held back by this style. They "
            "are in the plan with the reason on each; a looser style or a "
            "higher --max-operations would let more through."
        )

    if options.markers_only:
        plan.warnings.append(
            "markers-only pass: nothing will be drawn or scaled. Every item "
            "was recorded as a note."
        )
    if not style.allow_real_text and style.text_allowed:
        plan.warnings.append(
            f"The {style.name} style never draws text; every caption and card "
            "is a marker for the editor to place."
        )

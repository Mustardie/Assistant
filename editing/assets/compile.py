"""The asset placement compiler.

    layered edit plan + asset library + style preset (+ critic findings)
        -> one placement per placeholder
        -> matching, then the mixing safety rules
        -> ordered Premiere operations
        -> offline dry run

Every Session 5 placeholder that could be a sound or a graphic gets exactly one
``AssetPlacement``, and exactly one of five outcomes. Four of them place
nothing, and that is the expected case for most libraries — a plan where
everything is a marker is a correct plan and a useful shopping list, not a
failure.

The order the rules run in is chosen so the *cheapest and most certain*
refusals happen first, and so a placeholder is never charged against a mixing
budget it was never going to reach:

1. **Is this kind asset-backed at all?** A ``silence_hold`` is the absence of
   sound; no asset can satisfy it.
2. **Does the library have anything of this kind?** Zero candidates is
   ``missing`` — a different answer from "candidates existed and none were good
   enough", and a different thing to do about it.
3. **Match.** The best candidate above the score threshold, or ``rejected``
   with every loser's reason kept.
4. **Mixing safety.** Spam, stacking, dialogue, HUD. A good match refused here
   is ``unsafe``, which again is worth distinguishing: the asset is right and
   the moment is wrong.

Placeholders are considered **most defensible first**, so when a spam ceiling
bites it drops the weakest moment rather than whichever came last — the same
principle as the Session 2 budget and the Session 5 density pass.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Sequence

from editing.assets import match as match_module
from editing.assets import place as place_module
from editing.assets.match import NOT_ASSET_BACKED, rank_candidates
from editing.assets.place import (
    ASSET_BIN, BED_KINDS, DEFAULT_TRACKS, HUD_FLAGS, ONE_SHOT_KINDS,
    PROTECTED_TRACKS, PlacementLimits,
)
from editing.assets.schema import (
    AssetLibrary, AssetPlacement, AssetPlacementPlan, placement_id_for,
)
from editing.schema import StructureTimeline
from editing.style.presets import DEFAULT_PRESET, StylePreset, get as get_preset
from editing.style.schema import LayerItem, LayeredEditPlan

#: Operation order. Imports first (media must exist in the project), then
#: tracks (they must exist before anything lands on them), then the clips, then
#: everything that acts *on* a placed clip, then markers last.
_OP_ORDER = (
    "sequence.activate", "project.import", "track.add", "clip.overwrite",
    "graphic.image", "audio.gain", "audio.fade", "audio.duck", "marker.add",
)

#: Critic issues that mean "do not put a graphic here".
CRITIC_VISUAL_BLOCKERS = frozenset({
    "hud_hidden", "action_hidden", "bad_crop", "text_placed_badly",
    "caption_covers_gameplay",
})


@dataclass
class AssetOptions:
    """Knobs about this run rather than about the style or the mix."""

    #: Below this match score, nothing is placed.
    min_score: float = match_module.DEFAULT_MIN_SCORE
    #: Place assets whose sidecar says ``safe_for_auto: false``. Off, loudly.
    allow_unsafe: bool = False
    #: Turn every placement into a marker. The safest possible pass.
    markers_only: bool = False
    #: Let critic findings block graphics at flagged moments.
    use_critic: bool = True
    #: Hard ceiling on operations.
    max_operations: int = 500
    #: Track names. Never V1/A1 -- validated on construction.
    tracks: Optional[dict] = None

    def resolved_tracks(self) -> dict:
        tracks = dict(DEFAULT_TRACKS)
        tracks.update(self.tracks or {})
        for role, name in tracks.items():
            if name in PROTECTED_TRACKS:
                raise ValueError(
                    f"the {role} track cannot be {name}: V1 and A1 belong to "
                    "the rough cut and are never written to"
                )
        return tracks

    def to_dict(self) -> dict:
        return {
            "min_score": self.min_score,
            "allow_unsafe": self.allow_unsafe,
            "markers_only": self.markers_only,
            "use_critic": self.use_critic,
            "max_operations": self.max_operations,
            "tracks": self.resolved_tracks(),
        }


def compile_assets(
    layers: LayeredEditPlan,
    library: AssetLibrary,
    *,
    style: Optional[StylePreset] = None,
    timeline: Optional[StructureTimeline] = None,
    revisions=None,
    options: Optional[AssetOptions] = None,
    limits: Optional[PlacementLimits] = None,
    roughcut_executed: bool = False,
) -> AssetPlacementPlan:
    """Resolve every placeholder in ``layers`` against ``library``."""
    options = options or AssetOptions()
    limits = limits or PlacementLimits()
    style = (style or get_preset(layers.style or DEFAULT_PRESET)).validated()
    tracks = options.resolved_tracks()

    plan = AssetPlacementPlan(
        sequence_name=layers.sequence_name,
        style=style.name,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        on_scratch=layers.on_scratch,
        roughcut_executed=roughcut_executed or layers.roughcut_executed,
        cut_duration=layers.cut_duration,
        library_root=library.root,
        library_stats=library.stats(),
        tracks=dict(tracks),
    )

    candidates = _placeholders(layers)
    slots = _bed_slots(candidates, layers.cut_duration)
    if not candidates:
        plan.warnings.append(
            "The layered edit has no placeholders that an asset could fill. "
            "Run `layers build` with a style that emits audio or callout "
            "placeholders."
        )
        return plan

    speech = _speech_ranges(layers)
    blocked = _blocked_ranges(revisions) if options.use_critic else []
    hud_by_time = _hud_ranges(timeline, layers)

    used: dict = {}
    placed: list[AssetPlacement] = []

    for item in sorted(candidates, key=lambda i: (-i.priority, i.start)):
        placement = _resolve(
            item, library, style, options, limits, tracks,
            speech=speech, blocked=blocked, hud_by_time=hud_by_time,
            used=used, placed=placed, slot_end=slots.get(item.item_id),
        )
        plan.placements.append(placement)
        if placement.is_placed:
            placed.append(placement)
            if placement.library_asset_id:
                used[placement.library_asset_id] = (
                    used.get(placement.library_asset_id, 0) + 1
                )

    plan.placements.sort(key=lambda p: (p.start, p.kind))
    _add_markers(plan, style)
    plan.ops = _operations(plan, options)
    _add_warnings(plan, library, options, limits)
    return plan


def _bed_slots(items: Sequence[LayerItem], cut_duration: float) -> dict:
    """How long each bed placeholder should actually run for.

    Session 5's ``music_start`` is a *point*: "music comes in here". It carries
    no duration, because at the time nothing could be placed and a marker needs
    none. A bed with a zero-length slot can never be placed, so the slot is
    derived here instead -- a bed runs until the next bed-ish cue, or to the end
    of the cut.

    Only zero-length placeholders are given a slot. Anything Session 5 already
    gave a real range (a tension bed under a tense stretch, a silence hold) is
    left exactly as it was: the style pass had a reason for that range and this
    pass is not entitled to overrule it.
    """
    beds = sorted(
        (item for item in items if item.kind in BED_KINDS),
        key=lambda item: item.start,
    )
    starts = [item.start for item in beds]
    out: dict = {}

    for index, item in enumerate(beds):
        if item.end > item.start:
            continue
        following = next(
            (start for start in starts[index + 1:] if start > item.start + 0.5),
            None,
        )
        end = following if following is not None else cut_duration
        if end > item.start:
            out[item.item_id] = end
    return out


def _placeholders(layers: LayeredEditPlan) -> list[LayerItem]:
    """Session 5 items an asset could fill.

    Only *planned* items: something the style already held back should not come
    back through the asset pass, and something the style deferred is not a
    placeholder waiting to be filled.
    """
    return [
        item for item in layers.planned()
        if item.kind not in NOT_ASSET_BACKED
        and match_module.requirement_for(item.kind) is not None
    ]


# ---------------------------------------------------------------------------
# One placeholder
# ---------------------------------------------------------------------------

def _resolve(
    item: LayerItem,
    library: AssetLibrary,
    style: StylePreset,
    options: AssetOptions,
    limits: PlacementLimits,
    tracks: dict,
    *,
    speech: Sequence[dict],
    blocked: Sequence[tuple],
    hud_by_time: Sequence[tuple],
    used: dict,
    placed: Sequence[AssetPlacement],
    slot_end: Optional[float] = None,
) -> AssetPlacement:
    placement = AssetPlacement(
        placement_id=placement_id_for(item.item_id, item.kind, item.start),
        item_id=item.item_id,
        kind=item.kind,
        layer=item.layer,
        start=item.start,
        end=(
            slot_end if slot_end is not None and slot_end > item.start
            else (item.end if item.end > item.start else item.start)
        ),
        style=style.name,
        payload={
            "placeholder": item.payload.get("placeholder") or item.kind,
            "reason": item.reason[:200],
            "intensity": item.intensity,
            "priority": item.priority,
        },
    )

    requirement = match_module.requirement_for(item.kind)
    matches = rank_candidates(
        item.kind, library,
        style=style.name,
        slot_duration=placement.duration,
        used=used,
        min_score=options.min_score,
        allow_unsafe=options.allow_unsafe,
    )
    placement.candidates = matches[:12]

    if not matches:
        wanted = requirement.label
        for article in ("a ", "an "):
            if wanted.startswith(article):
                wanted = wanted[len(article):]
                break
        return placement.refuse(
            "missing",
            f"the library has nothing that could be {requirement.label}: no "
            f"{wanted} in {' or '.join(requirement.categories)}.",
            risk="no_asset",
        )

    chosen = match_module.best_match(matches)
    if chosen is None:
        best = matches[0]
        return placement.refuse(
            "rejected",
            f"{len(matches)} candidate(s) were considered and none qualified. "
            f"Closest was {best.filename}: {best.rejected}",
            risk="low_score",
        )

    asset = library.by_id(chosen.asset_id)
    if asset is None:  # pragma: no cover - ids come from this library
        return placement.refuse(
            "missing", "the chosen asset is not in the library index.",
            risk="no_asset",
        )

    placement.library_asset_id = asset.asset_id
    placement.asset_path = asset.path
    placement.asset_filename = asset.filename
    placement.payload["score"] = round(chosen.score, 3)

    if options.markers_only:
        return placement.refuse(
            "marker_only",
            f"markers-only pass: {asset.filename} matched but nothing was "
            "placed.",
            keep_asset=True,
        )

    # -- mixing safety, before anything is built ---------------------------
    unsafe = _mixing_refusal(placement, item, limits, placed, blocked,
                             hud_by_time, tracks)
    if unsafe:
        reason, risk = unsafe
        return placement.refuse("unsafe", reason, risk=risk)

    if asset.is_audio:
        return place_module.place_audio(
            placement, asset, tracks=tracks, limits=limits,
            speech_ranges=speech, style=style,
        )
    if asset.is_visual or asset.media_type == "mogrt":
        return place_module.place_visual(
            placement, asset, tracks=tracks, limits=limits, style=style,
            hud_flags=_hud_at(hud_by_time, placement.start, placement.end),
        )
    return placement.refuse(
        "rejected",
        f"{asset.filename} is {asset.media_type}, which this system has no way "
        "to place.",
        risk="unsupported_media",
    )


def _track_for(kind: str, tracks: dict) -> str:
    """Which track a placeholder of this kind will land on.

    Duplicated from ``place`` on purpose: the overlap check has to run
    *before* anything is built, and the answer is a one-line consequence of
    the kind.
    """
    if kind in ONE_SHOT_KINDS:
        return tracks.get("sfx", "")
    if kind in BED_KINDS:
        return tracks.get("music", "")
    return tracks.get("visual", "")


def _mixing_refusal(
    placement: AssetPlacement,
    item: LayerItem,
    limits: PlacementLimits,
    placed: Sequence[AssetPlacement],
    blocked: Sequence[tuple],
    hud_by_time: Sequence[tuple],
    tracks: dict,
) -> Optional[tuple]:
    """Whether this moment can take another asset. ``(reason, risk)`` or None."""
    kind = placement.kind

    if kind in ONE_SHOT_KINDS:
        one_shots = [p for p in placed if p.kind in ONE_SHOT_KINDS]
        nearest = min(
            (abs(p.start - placement.start) for p in one_shots), default=None
        )
        if nearest is not None and nearest < limits.min_sfx_gap:
            return (
                f"another one-shot is {nearest:.1f}s away and effects closer "
                f"than {limits.min_sfx_gap:g}s read as spam.",
                "sfx_spam",
            )
        in_minute = sum(
            1 for p in one_shots if abs(p.start - placement.start) <= 30.0
        )
        if in_minute >= limits.max_sfx_per_minute:
            return (
                f"{in_minute} effect(s) are already placed within a minute of "
                f"{placement.start:.1f}s, at the ceiling of "
                f"{limits.max_sfx_per_minute:g} a minute.",
                "sfx_spam",
            )

    # Correctness, not taste: ``clip.overwrite`` destroys whatever is under
    # it, so two placements overlapping on ONE track means the second silently
    # eats the first. Two beds on A3 are the realistic case -- a theme running
    # into a tension bed -- and it would look fine in the plan and be wrong on
    # the timeline.
    target = _track_for(kind, tracks)
    if target:
        for peer in placed:
            if peer.track != target:
                continue
            if peer.end > placement.start and peer.start < max(
                placement.end, placement.start + 0.05
            ):
                return (
                    f"{peer.kind} already occupies {target} from "
                    f"{peer.start:.1f}s to {peer.end:.1f}s, and placing here "
                    "would overwrite it.",
                    "stacked_audio" if target.startswith("A")
                    else "too_many_overlays",
                )

    if kind in ONE_SHOT_KINDS or kind in BED_KINDS:
        concurrent = sum(
            1 for p in placed
            if p.track and not p.track.startswith("V")
            and p.end > placement.start and p.start < max(
                placement.end, placement.start + 0.1
            )
        )
        if concurrent >= limits.max_concurrent_audio:
            return (
                f"{concurrent} asset clip(s) already sound at this moment, at "
                f"the ceiling of {limits.max_concurrent_audio}.",
                "stacked_audio",
            )

    if placement.layer == "caption" or kind in (
        "visual_callout", "callout_label", "title_card", "chapter_card"
    ):
        overlapping = sum(
            1 for p in placed
            if p.track.startswith("V")
            and p.end > placement.start and p.start < placement.end
        )
        if overlapping >= limits.max_concurrent_visual:
            return (
                f"{overlapping} graphic(s) are already on screen here, at the "
                f"ceiling of {limits.max_concurrent_visual}.",
                "too_many_overlays",
            )
        for low, high, why in blocked:
            if placement.end > low and placement.start < high:
                return (why, "hud_risk")

    return None


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def _speech_ranges(layers: LayeredEditPlan) -> list[dict]:
    """Speech in sequence time, from Session 5's ducking placeholder.

    Session 5 computed these and could not use them: ``audio.duck`` needs a bed
    clip and there was none. Placing a bed is what makes them actionable, so
    they are read straight back out of the placeholder that recorded them.
    """
    for item in layers.of_kind("duck_narration"):
        ranges = item.payload.get("under")
        if isinstance(ranges, list) and ranges:
            return [
                {"start": float(entry.get("start", 0.0)),
                 "end": float(entry.get("end", 0.0))}
                for entry in ranges if isinstance(entry, dict)
            ]
    return []


def _blocked_ranges(revisions) -> list[tuple]:
    """Moments the critic said not to cover, as ``(start, end, reason)``."""
    if revisions is None:
        return []
    out: list[tuple] = []
    for revision in getattr(revisions, "revisions", []) or []:
        if revision.issue not in CRITIC_VISUAL_BLOCKERS:
            continue
        start = max(0.0, revision.start - 0.5)
        end = max(start, revision.end) + 0.5
        out.append((
            start, end,
            f"the critic reported {revision.issue} here "
            f"({revision.confidence:.0%} confident), so a graphic would "
            "compound it",
        ))
    return out


def _hud_ranges(
    timeline: Optional[StructureTimeline], layers: LayeredEditPlan
) -> list[tuple]:
    """Where the analysis pass saw a HUD state, in **sequence** time.

    Built from the layer items' own segment IDs rather than from source times,
    because a graphic is placed on the sequence and the mapping between the two
    lives in the rough cut, not here.
    """
    if timeline is None:
        return []
    by_id = {segment.segment_id: segment for segment in timeline.segments}
    out: list[tuple] = []
    for item in layers.items:
        flags: list[str] = []
        for segment_id in item.evidence.segment_ids:
            segment = by_id.get(segment_id)
            if segment is None:
                continue
            for event in segment.events:
                for name in HUD_FLAGS:
                    if getattr(event.ui, name, False) and name not in flags:
                        flags.append(name)
        if flags:
            out.append((item.start, max(item.end, item.start + 0.1), flags))
    return out


def _hud_at(
    hud_by_time: Sequence[tuple], start: float, end: float
) -> list[str]:
    flags: list[str] = []
    for low, high, names in hud_by_time:
        if end > low and start < high:
            for name in names:
                if name not in flags:
                    flags.append(name)
    return flags


# ---------------------------------------------------------------------------
# Markers and operations
# ---------------------------------------------------------------------------

def _add_markers(plan: AssetPlacementPlan, style: StylePreset) -> None:
    """Every placement that put nothing down still leaves a note.

    This is the difference between "the system considered a whoosh here and
    could not find one" and "the system did nothing here". The first is a
    shopping list; the second is invisible.
    """
    for placement in plan.placements:
        if placement.is_placed or placement.premiere_ops:
            continue
        best = placement.best
        detail = ""
        if best is not None and best.filename:
            detail = f" | closest: {best.filename}"
        placement.premiere_ops = [{
            "op": "marker.add",
            "time": round(placement.start, 3),
            "name": style.marker_name(placement.kind),
            "type": "comment",
            "comment": (
                f"{placement.kind.replace('_', ' ').upper()} "
                f"[{placement.status}]: {placement.reason}{detail} "
                f"[{placement.placement_id}]"
            )[:500],
            "note": f"{placement.kind} not placed [{placement.status}]",
        }]


def _operations(plan: AssetPlacementPlan, options: AssetOptions) -> list[dict]:
    """Every placement's operations, in an order that runs cleanly."""
    collected: list[tuple] = []
    for placement in plan.placements:
        for op in placement.premiere_ops:
            collected.append((placement.start, op))

    if not collected:
        plan.warnings.append(
            "Nothing in this pass produced an operation. The placements are "
            "still in the report with their reasons."
        )
        return []

    ops: list[dict] = [{
        "op": "sequence.activate",
        "name": plan.sequence_name,
        "note": "Place assets on the rough cut's own scratch sequence. This "
                "plan never creates one.",
    }]

    paths = plan.assets_used()
    if paths:
        ops.append({
            "op": "project.import",
            "paths": paths,
            "bin": ASSET_BIN,
            "note": f"Import the {len(paths)} asset(s) this pass places, into "
                    f"one bin so they can be found and removed as a group.",
        })

    audio_tracks = sum(
        1 for role, name in plan.tracks.items() if name.startswith("A")
    )
    video_tracks = sum(
        1 for role, name in plan.tracks.items() if name.startswith("V")
    )
    if any(str(op.get("track", "")).startswith("A") for _at, op in collected):
        ops.append({
            "op": "track.add",
            "audio": audio_tracks,
            "note": f"Audio tracks for the asset pass. Everything it places "
                    f"lands above A1, so the rough cut's own audio is never "
                    f"touched.",
        })
    if any(
        str(op.get("track", "")).startswith("V") for _at, op in collected
    ):
        ops.append({
            "op": "track.add",
            "video": video_tracks,
            "note": "A video track for asset graphics, above the assembly and "
                    "above anything the style pass drew.",
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
            f"and was cut from {len(ops)}. Raise --max-operations, or narrow "
            "the style."
        )
        ops = ops[: options.max_operations]
    return ops


def _add_warnings(
    plan: AssetPlacementPlan,
    library: AssetLibrary,
    options: AssetOptions,
    limits: PlacementLimits,
) -> None:
    stats = plan.stats()

    if not library.items:
        plan.warnings.append(
            "The asset library is empty, so every placeholder is reported as a "
            "missing asset. That list is a shopping list: `assets "
            "show-missing` groups it by what to go and find."
        )
    if not plan.roughcut_executed:
        plan.warnings.append(
            "There is no record of this rough cut having been executed into "
            "Premiere, so the sequence this plan activates may not exist yet. "
            "Run `roughcut execute --yes` first."
        )
    if not plan.on_scratch:
        plan.warnings.append(
            "The layered edit this places assets over is not marked as being "
            "on a scratch sequence. Executing would edit whatever that "
            "sequence is."
        )
    if options.allow_unsafe:
        plan.warnings.append(
            "--allow-unsafe was set, so assets whose sidecars say "
            "safe_for_auto: false were eligible. Check what was placed before "
            "executing."
        )
    if options.markers_only:
        plan.warnings.append(
            "markers-only pass: nothing was placed. Every match was recorded "
            "as a note naming the asset it would have used."
        )

    if stats["missing"]:
        plan.warnings.append(
            f"{stats['missing']} placeholder(s) had no candidate asset at all. "
            "`assets show-missing` says what kinds to go and find."
        )
    if stats["unsafe"]:
        plan.warnings.append(
            f"{stats['unsafe']} match(es) were refused by a mixing or HUD "
            "safety rule. The asset was right and the moment was wrong; "
            "`assets show-deferred` explains each."
        )
    if stats["rejected"]:
        plan.warnings.append(
            f"{stats['rejected']} placeholder(s) had candidates and none "
            "qualified. Lower --min-score, tag the files better, or add a "
            "sidecar."
        )
    if library.needing_review():
        plan.warnings.append(
            f"{len(library.needing_review())} asset(s) in the library need "
            "review and were never considered. Run `assets validate`."
        )

"""Turning a chosen asset into Premiere operations.

Two halves: audio onto dedicated audio tracks, graphics onto a dedicated video
track. Both obey the same rule, which is the one that makes this session safe
to run at all:

    **Nothing is ever placed on V1 or A1.**

Those are the rough cut's own tracks. Every asset lands on a track this plan
adds, which means the assembly underneath is untouchable, the whole pass can be
undone by deleting two or three tracks, and a bug here cannot damage the cut.
The executor checks it structurally rather than trusting this module.

The refusals are the interesting part. Each one has a named rule and a reason,
and each one falls back to a marker rather than to silence:

* **SFX spam** — a minimum gap between one-shots, and a per-minute ceiling.
  Three impacts in four seconds is the single most recognisable way an
  automatically-scored edit announces itself.
* **Stacked audio** — a cap on how many asset clips may sound at once. A bed
  plus one effect is a mix; a bed plus four is a mess.
* **Music over dialogue** — a bed that covers speech is refused *unless* the
  plan also ducks it. Session 5 could not duck, because there was no bed clip
  to duck; placing one is what makes ``audio.duck`` genuinely available, and
  this is the module that uses it.
* **HUD risk** — a graphic is never placed where the analysis pass saw an open
  menu or low health, and never in the centre of frame.

Levels are conservative and stated: beds sit well under speech, ambience
further under, one-shots below unity. They are opinions, they are in one table,
and a sidecar's ``volume_adjust_db`` overrides them per file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from editing.assets.schema import AssetItem, AssetPlacement
from editing.style.presets import ZONE_POSITION, StylePreset
from editing.tracks import DEFAULT_LAYOUT

#: Tracks this pass writes to. Never V1/A1: those are the rough cut's.
#:
#: Read off the shared layout in ``editing.tracks`` rather than spelled out
#: here. They used to be spelled out, and drifted: this pass put graphics on
#: V3 while the visual pass put its treatments on the same track, so two
#: independent passes silently overwrote each other's overlays. One table is
#: the fix, and ``overlay`` (V4) is where additional picture belongs -- b-roll,
#: picture-in-picture, facecam and library graphics all being the same kind of
#: thing.
DEFAULT_TRACKS = {
    "sfx": DEFAULT_LAYOUT.sfx,
    "music": DEFAULT_LAYOUT.music,
    "visual": DEFAULT_LAYOUT.overlay,
}

#: Tracks that belong to the rough cut and may never be written to.
PROTECTED_TRACKS = DEFAULT_LAYOUT.protected

#: Bin the library's files are imported into, so they are removable as a group.
ASSET_BIN = "Nova Assets"

#: Default level per category, in dB, before any sidecar adjustment.
#:
#: Opinions, not measurements. A bed at -18 sits under commentary without
#: disappearing; ambience at -26 is felt rather than heard; a one-shot at -8
#: lands without clipping the mix. Every one is overridable per file.
DEFAULT_GAIN_DB = {
    "music": -18.0,
    "ambience": -26.0,
    "sfx": -8.0,
    "transition": -10.0,
}

#: Level a bed drops to while someone is speaking over it.
DUCKED_DB = -30.0

#: Fade lengths, by what is being placed.
BED_FADE = 1.5
SFX_FADE = 0.05

#: A one-shot shorter than this after trimming is a click, not a sound.
MIN_SFX_SECONDS = 0.05

#: How many times a loopable bed may be tiled to fill a slot. Past this, the
#: asset is too short for the job and saying so beats laying down forty copies.
MAX_LOOPS = 12

#: Placeholder kinds that are one-shots, for the spam rules.
ONE_SHOT_KINDS = frozenset({"impact_sfx", "comedic_sfx", "whoosh"})

#: Placeholder kinds that run over a range.
BED_KINDS = frozenset({"tension_bed", "ambience", "music_start", "music_rise"})

#: HUD flags that make any overlay a bad idea at that moment.
HUD_FLAGS = ("inventory_open", "crafting_open", "chest_open", "map_open",
             "death_screen", "low_health")


@dataclass
class PlacementLimits:
    """The safety rules, as numbers.

    Separated from the style preset because these are about *mixing*, not about
    taste: a fast, loud style still should not put three impacts in four
    seconds or bury dialogue under a bed.
    """

    #: Minimum gap between two placed one-shots.
    min_sfx_gap: float = 2.5
    #: Ceiling on placed one-shots in any 60 seconds.
    max_sfx_per_minute: float = 5.0
    #: How many asset clips may overlap at one instant.
    max_concurrent_audio: int = 2
    #: How many asset graphics may overlap at one instant.
    max_concurrent_visual: int = 1
    #: Longest a callout graphic stays on screen.
    max_callout_seconds: float = 2.5
    #: Beds shorter than this are not worth placing.
    min_bed_seconds: float = 3.0
    #: Place music over speech only when the plan also ducks it.
    require_ducking_over_speech: bool = True

    def to_dict(self) -> dict:
        return {
            "min_sfx_gap": self.min_sfx_gap,
            "max_sfx_per_minute": self.max_sfx_per_minute,
            "max_concurrent_audio": self.max_concurrent_audio,
            "max_concurrent_visual": self.max_concurrent_visual,
            "max_callout_seconds": self.max_callout_seconds,
            "min_bed_seconds": self.min_bed_seconds,
            "require_ducking_over_speech": self.require_ducking_over_speech,
        }


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def place_audio(
    placement: AssetPlacement,
    item: AssetItem,
    *,
    tracks: dict,
    limits: PlacementLimits,
    speech_ranges: Sequence[dict] = (),
    style: Optional[StylePreset] = None,
) -> AssetPlacement:
    """Build the operations for one audio asset, or refuse with a reason."""
    one_shot = placement.kind in ONE_SHOT_KINDS
    track = tracks["sfx"] if one_shot else tracks["music"]
    if track in PROTECTED_TRACKS:  # pragma: no cover - guarded at config time
        return placement.refuse(
            "unsafe", f"{track} belongs to the rough cut and is never written to.",
        )

    source_in = max(0.0, item.start_offset)
    available = item.effective_duration
    gain = _gain_for(item)

    if one_shot:
        return _place_one_shot(
            placement, item, track=track, source_in=source_in,
            available=available, gain=gain,
        )
    return _place_bed(
        placement, item, track=track, source_in=source_in,
        available=available, gain=gain, limits=limits,
        speech_ranges=speech_ranges,
    )


def _place_one_shot(
    placement: AssetPlacement,
    item: AssetItem,
    *,
    track: str,
    source_in: float,
    available: Optional[float],
    gain: float,
) -> AssetPlacement:
    """A single hit at a moment. Trimmed to the asset, not to the slot.

    A one-shot's length is a property of the sound, not of the gap the planner
    left it. Stretching an impact to fill four seconds would mean holding a
    tail nobody wants; letting it run its natural length and end is correct.
    """
    length = available if available is not None else 1.0
    if available is not None and length < MIN_SFX_SECONDS:
        return placement.refuse(
            "rejected",
            f"{item.filename} is only {length:.3f}s after trimming, which is a "
            "click rather than a sound.",
            risk="duration_mismatch",
        )

    source_out = source_in + length
    placement.track = track
    placement.payload.update({
        "gain_db": gain,
        "source_in": round(source_in, 3),
        "source_out": round(source_out, 3),
        "length": round(length, 3),
        "one_shot": True,
        "length_known": available is not None,
    })
    placement.end = placement.start + length

    ops: list[dict] = [{
        "op": "clip.overwrite",
        "asset": item.path,
        "track": track,
        "time": round(placement.start, 3),
        "in": round(source_in, 3),
        "out": round(source_out, 3),
        "note": f"{placement.kind}: {item.filename} [{placement.placement_id}]",
    }]
    ops.append(_gain_op(placement, track, gain, item))
    if available is not None and length > SFX_FADE * 4:
        ops.append({
            "op": "audio.fade",
            "clip": {"track": track, "at": round(placement.start + length / 2, 3)},
            "out": SFX_FADE,
            "easing": "ease_out",
            "note": f"tiny tail fade so the cut off the end is not a click "
                    f"[{placement.placement_id}]",
        })

    placement.premiere_ops = ops
    placement.status = "placed"
    placement.reason = (
        f"{item.filename} matched {placement.kind} and fits in one hit at "
        f"{placement.start:.2f}s on {track}."
    )
    if available is None:
        placement.notes = (
            "the asset's length is unknown (no probe, no sidecar), so it is "
            "placed whole and may run longer than expected"
        )
    return placement


def _place_bed(
    placement: AssetPlacement,
    item: AssetItem,
    *,
    track: str,
    source_in: float,
    available: Optional[float],
    gain: float,
    limits: PlacementLimits,
    speech_ranges: Sequence[dict],
) -> AssetPlacement:
    """Music or ambience across a range, looped if it needs to be."""
    slot = placement.duration
    if slot < limits.min_bed_seconds:
        return placement.refuse(
            "rejected",
            f"the slot is only {slot:.1f}s, under the "
            f"{limits.min_bed_seconds:g}s worth placing a bed for.",
            risk="duration_mismatch",
        )

    covered = _speech_inside(speech_ranges, placement.start, placement.end)
    ducking_possible = bool(covered)
    if covered and limits.require_ducking_over_speech and not item.loopable:
        # Ducking writes level keyframes across the clip; on a one-shot bed
        # that has already been trimmed to fit, that is still fine -- the
        # refusal here is only for the case where we cannot cover the range at
        # all, handled below.
        pass

    length = available
    ops: list[dict] = []
    loops = 1

    if length is None:
        # Unknown length: place once and trim to the slot. The clip may end
        # early if the asset is shorter, which is visible in the report rather
        # than silently wrong.
        ops.append(_overwrite(item, track, placement.start, source_in,
                              source_in + slot, placement))
        placement.notes = (
            "the asset's length is unknown, so it is placed once and trimmed "
            "to the slot; if it is shorter than the slot the bed will stop "
            "early"
        )
    elif length >= slot:
        ops.append(_overwrite(item, track, placement.start, source_in,
                              source_in + slot, placement))
    elif item.loopable:
        loops = min(MAX_LOOPS, int(slot // length) + (1 if slot % length else 0))
        if slot / length > MAX_LOOPS:
            return placement.refuse(
                "rejected",
                f"{item.filename} is {length:.1f}s and the slot is "
                f"{slot:.1f}s, which would need more than {MAX_LOOPS} loops. "
                "A longer bed would sound better than tiling this one.",
                risk="duration_mismatch",
            )
        at = placement.start
        remaining = slot
        for _ in range(loops):
            span = min(length, remaining)
            ops.append(_overwrite(item, track, at, source_in,
                                  source_in + span, placement))
            at += span
            remaining -= span
            if remaining <= 0.01:
                break
    elif length >= limits.min_bed_seconds:
        # A track shorter than the section it opens is ordinary: music comes
        # in, plays, and ends. Refusing it would mean a two-minute theme could
        # never open a three-minute cut, which is the common case rather than
        # an edge one. It is placed whole, faded out, and the report says it
        # stops early.
        ops.append(_overwrite(item, track, placement.start, source_in,
                              source_in + length, placement))
        placement.notes = (
            f"{item.filename} is {length:.1f}s and the slot is {slot:.1f}s, so "
            "the music ends before the section does. It does not loop, so it "
            "is placed once and faded out rather than tiled."
        )
        slot = length
    else:
        return placement.refuse(
            "rejected",
            f"{item.filename} is only {length:.1f}s, under the "
            f"{limits.min_bed_seconds:g}s worth placing as a bed.",
            risk="duration_mismatch",
        )

    placement.track = track
    fade = min(BED_FADE, max(0.2, slot / 6.0))
    ops.append({
        "op": "audio.fade",
        "clip": {"track": track, "at": round(placement.start + slot / 2, 3)},
        "in": round(fade, 3),
        "out": round(fade, 3),
        "easing": "ease_in_out",
        "note": f"ease the bed in and out [{placement.placement_id}]",
    })

    if ducking_possible:
        ops.append({
            "op": "audio.duck",
            "clip": {"track": track, "at": round(placement.start + slot / 2, 3)},
            "under": [dict(entry) for entry in covered][:200],
            "base_db": gain,
            "duck_db": min(DUCKED_DB, gain - 8.0),
            "attack": 0.25,
            "release": 0.45,
            "merge_gap": 0.6,
            "note": f"duck under {len(covered)} speech range(s) "
                    f"[{placement.placement_id}]",
        })
        placement.payload["ducked_under"] = len(covered)
    else:
        ops.append(_gain_op(placement, track, gain, item))

    # The record has to match what is actually on the timeline: a track that
    # runs out early shortens the placement, and the concurrency check reads
    # ``end`` to decide what overlaps what.
    placement.end = placement.start + slot
    placement.payload.update({
        "gain_db": gain,
        "loops": loops,
        "slot": round(slot, 3),
        "asset_length": round(length, 3) if length is not None else None,
        "fade": round(fade, 3),
        "one_shot": False,
    })
    placement.premiere_ops = ops
    placement.status = "placed"
    placement.reason = (
        f"{item.filename} covers {slot:.1f}s from {placement.start:.2f}s on "
        f"{track}"
        + (f", looped {loops}x" if loops > 1 else "")
        + (f", ducked under {len(covered)} speech range(s)"
           if ducking_possible else "")
        + "."
    )
    return placement


def _overwrite(
    item: AssetItem,
    track: str,
    at: float,
    source_in: float,
    source_out: float,
    placement: AssetPlacement,
) -> dict:
    return {
        "op": "clip.overwrite",
        "asset": item.path,
        "track": track,
        "time": round(at, 3),
        "in": round(source_in, 3),
        "out": round(source_out, 3),
        "note": f"{placement.kind}: {item.filename} [{placement.placement_id}]",
    }


def _gain_op(
    placement: AssetPlacement, track: str, gain: float, item: AssetItem
) -> dict:
    at = placement.start + max(0.05, placement.duration / 2)
    return {
        "op": "audio.gain",
        "clip": {"track": track, "at": round(at, 3)},
        "db": round(gain, 2),
        "note": f"{item.category} level for {placement.kind} "
                f"[{placement.placement_id}]",
    }


def _gain_for(item: AssetItem) -> float:
    """The level this file is placed at.

    The sidecar wins outright: a person who measured their own file knows more
    than a table of defaults.
    """
    if item.volume_adjust_db is not None:
        return max(-96.0, min(15.0, item.volume_adjust_db))
    return DEFAULT_GAIN_DB.get(item.category, -12.0)


def _speech_inside(
    speech_ranges: Sequence[dict], start: float, end: float
) -> list[dict]:
    """Speech ranges overlapping a slot, clipped to it."""
    out: list[dict] = []
    for entry in speech_ranges:
        low = float(entry.get("start", 0.0))
        high = float(entry.get("end", 0.0))
        if high <= start or low >= end:
            continue
        out.append({
            "start": round(max(low, start), 3),
            "end": round(min(high, end), 3),
        })
    return out


# ---------------------------------------------------------------------------
# Visual
# ---------------------------------------------------------------------------

def place_visual(
    placement: AssetPlacement,
    item: AssetItem,
    *,
    tracks: dict,
    limits: PlacementLimits,
    style: Optional[StylePreset] = None,
    hud_flags: Sequence[str] = (),
) -> AssetPlacement:
    """Build the operations for one graphic, or refuse with a reason."""
    track = tracks["visual"]
    if track in PROTECTED_TRACKS:  # pragma: no cover - guarded at config time
        return placement.refuse(
            "unsafe", f"{track} belongs to the rough cut and is never written to.",
        )

    if item.media_type == "mogrt":
        return placement.refuse(
            "marker_only",
            f"{item.filename} is a Motion Graphics template. Driving one needs "
            "a registered .mogrt and a parameter mapping, which this system "
            "does not have, so the marker names the template for you to drop "
            "in by hand.",
            risk="unsupported_media",
            keep_asset=True,
        )

    if hud_flags:
        return placement.refuse(
            "unsafe",
            f"the analysis pass saw {', '.join(sorted(set(hud_flags))[:3])} "
            "here, so a graphic would cover what the viewer is reading.",
            risk="hud_risk",
        )
    if "hud_risk" in (item.usage_notes or ""):
        return placement.refuse(
            "unsafe",
            f"{item.filename} is marked a HUD risk in its sidecar and is only "
            "ever placed by hand.",
            risk="hud_risk",
        )

    is_card = placement.kind in ("title_card", "chapter_card")
    duration = placement.duration
    if not is_card:
        duration = min(duration or limits.max_callout_seconds,
                       limits.max_callout_seconds)
    duration = max(0.4, duration)

    zone = "center" if is_card else _callout_zone(style)
    if zone is None:
        return placement.refuse(
            "unsafe",
            "this style has no safe zone left for a graphic at this moment.",
            risk="hud_risk",
        )

    placement.track = track
    placement.end = placement.start + duration
    placement.payload.update({
        "zone": zone,
        "position": list(ZONE_POSITION[zone]),
        "seconds": round(duration, 3),
        "media_type": item.media_type,
    })
    placement.premiere_ops = [{
        "op": "graphic.image" if item.media_type == "image" else "clip.overwrite",
        **(
            {
                "path": item.path,
                "track": track,
                "time": round(placement.start, 3),
                "duration": round(duration, 3),
                "position": list(ZONE_POSITION[zone]),
                "opacity": 100.0,
            }
            if item.media_type == "image"
            else {
                "asset": item.path,
                "track": track,
                "time": round(placement.start, 3),
                "in": round(max(0.0, item.start_offset), 3),
                "out": round(max(0.0, item.start_offset) + duration, 3),
            }
        ),
        "note": f"{placement.kind}: {item.filename} ({zone}) "
                f"[{placement.placement_id}]",
    }]
    placement.status = "placed"
    placement.reason = (
        f"{item.filename} placed in the {zone.replace('_', ' ')} for "
        f"{duration:.1f}s from {placement.start:.2f}s on {track}."
    )
    return placement


def _callout_zone(style: Optional[StylePreset]) -> Optional[str]:
    """Where a callout graphic can sit.

    Reuses the style's own text zones, which already exclude the centre of
    frame (the crosshair) and the bottom centre (the hotbar and health). A
    graphic has exactly the same problem as a caption, so it should obey the
    same rule rather than a parallel one that can drift out of step.
    """
    if style is None:
        return "upper_left"
    return style.zone_for("key_phrase")

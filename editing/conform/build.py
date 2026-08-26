"""Composing every decision into one list of operations Premiere can run.

This is the module the whole system was missing. Before it, each pass ended at
its own artifact: an ``.srt`` file, a list of cue notes, a plan of visual
operations nobody executed. Each of those is a *decision*, and a decision that
never reaches a timeline is an opinion.

What happens here is deliberately dull. Nothing new is decided -- the captions
were chosen by the caption pass, the sounds by the audio pass, the treatments
by the visual pass, and colour, music, the mix and the transitions by their
modules next door. This file's only job is to turn all of it into catalog
operations in an order that works, and to record everything it could not
convert with the reason.

## Order is load-bearing

1. ``sequence.activate`` -- fix the target rather than inherit it.
2. ``track.add`` -- every overlay below lands on a track that does not exist
   yet in a sequence created from a single clip.
3. ``project.import`` -- media must be in the project before it can be placed.
4. **transitions**, then **colour** -- both act on the programme clips, and a
   transition changes clip boundaries, so it goes first.
5. **sound**: effects, then the music bed.
6. **picture**: visual treatments, then captions on top of them.
7. **the mix** -- last, because it sets levels on clips the steps above
   created.
8. ``marker.add`` -- last of all, so markers land at final positions.

## What it refuses

A decision that cannot become an operation is appended to ``unconverted`` with
a reason and is never silently dropped. The three real cases are a caption the
cut removed, a sound cue with no file behind it, and a visual treatment the
catalog has no primitive for -- all of which are normal, and all of which a
person is entitled to see.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

from editing.conform import color as color_module
from editing.conform import mix as mix_module
from editing.conform import placement as placement_module
from editing.conform import music as music_module
from editing.conform import transitions as transitions_module
from editing.conform.schema import (
    ConformConfig, ConformPlan, MixDecision, now,
)
from editing.tracks import TrackLayout

logger = logging.getLogger("nova.editing.conform.build")

#: Bins the pass imports into, so everything it added is removable as a group.
SFX_BIN = "Nova Sound"
MUSIC_BIN = "Nova Music"

#: Minimum time a caption stays on screen. Below this it is a flash, not text.
MIN_CAPTION_SECONDS = 0.6

#: Padding added to a caption's end so it does not vanish on the last syllable.
CAPTION_TAIL = 0.25

#: A sound placed within this of the end of the cut would hang off the end.
END_GUARD = 0.05

#: How far from the end of the cut anything on screen has to finish.
#:
#: A frame's worth would be enough to be *legal*; this is larger because the
#: plan's idea of the cut length and Premiere's differ by a frame or two once
#: clip durations are rounded to the sequence frame rate, and because an
#: overlay that ends on the final frame reads as a glitch even when it works.
END_MARGIN = 0.35


def text_of(decision) -> str:
    return str(getattr(decision, "text", "") or "").strip()


def build(
    *,
    rough_cut,
    config: Optional[ConformConfig] = None,
    caption_plan=None,
    audio_plan=None,
    visual_plan=None,
    style=None,
    timeline=None,
    name: str = "structure",
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    frames_dir=None,
    measure_fn=None,
) -> ConformPlan:
    """Every decision the run made, as one executable plan."""
    config = config or ConformConfig()
    layout = config.layout
    plan = ConformPlan(
        name=name,
        sequence_name=getattr(rough_cut, "sequence_name", ""),
        config=config,
        layout=layout,
        cut_duration=float(getattr(rough_cut, "total_duration", 0.0) or 0.0),
        generated_at=now(),
    )
    placements = list(getattr(rough_cut, "placements", ()) or ())

    if config.mode == "off":
        plan.warnings.append(
            "The conform pass is off, so nothing was built. Everything the "
            "earlier passes decided stays as a plan."
        )
        return plan
    if not plan.sequence_name:
        plan.warnings.append(
            "The rough cut has no sequence name, so this plan cannot name its "
            "target and will be refused at execution."
        )

    ops: list[dict] = [{
        "op": "sequence.activate",
        "name": plan.sequence_name,
        "note": "fix the target rather than inherit whatever is open",
    }]
    ops.extend(layout.ensure_ops(existing_video=1, existing_audio=1))

    speech_ranges = _speech_ranges(rough_cut, timeline)

    # -- transitions ------------------------------------------------------
    if config.enabled("transitions"):
        plan.transitions = transitions_module.decide(
            placements,
            track=layout.programme,
            max_transitions=config.max_transitions,
            scene_changes=_scene_changes(timeline, rough_cut),
        )
        transition_ops = transitions_module.transition_ops(plan.transitions)
        _contribute(plan, "transitions", transition_ops)
        ops.extend(transition_ops)
        for decision in plan.transitions:
            if not decision.applied and decision.reject_reason not in (
                "", "ordinary_cut",
            ):
                plan.unconverted.append({
                    "kind": "transition", "at": decision.at,
                    "reason": decision.reject_reason, "detail": decision.reason,
                })

    # -- colour -----------------------------------------------------------
    if config.enabled("color"):
        measurements = [
            color_module.measure_footage(path, ffmpeg=ffmpeg)
            for path in _source_paths(placements)[:3]
        ]
        plan.color = color_module.decide(
            style_name=getattr(style, "name", "") or "",
            style_intent=_style_intent(style),
            requested=config.color_look,
            strength=config.color_strength,
            measurements=[m for m in measurements if m],
        )
        grade_ops = color_module.grade_ops(
            plan.color, layout, clip_count=len(placements)
        )
        _contribute(plan, "color", grade_ops)
        ops.extend(grade_ops)

    # -- sound effects ----------------------------------------------------
    sfx_paths: list[str] = []
    if config.enabled("sound") and audio_plan is not None:
        sfx_ops, sfx_paths = _sound_ops(plan, audio_plan, layout)
        _contribute(plan, "sound", sfx_ops)
        ops.extend(sfx_ops)

    # -- music ------------------------------------------------------------
    if config.enabled("music"):
        plan.music = music_module.plan_bed(
            library_root=config.music_library,
            cut_duration=plan.cut_duration,
            track=layout.music,
            gain_db=config.music_under_dialogue_db,
            speech_ranges=speech_ranges,
            ffprobe=ffprobe,
            ffmpeg=ffmpeg,
        )
        bed_ops = music_module.bed_ops(plan.music, bin_name=MUSIC_BIN)
        _contribute(plan, "music", bed_ops)
        ops.extend(bed_ops)
        if not plan.music.placed and plan.music.reject_reason:
            plan.unconverted.append({
                "kind": "music", "at": 0.0,
                "reason": plan.music.reject_reason,
                "detail": plan.music.reason,
            })

    # -- visual treatments -------------------------------------------------
    if config.enabled("visuals") and visual_plan is not None:
        visual_ops = _visual_ops(plan, visual_plan, layout)
        _contribute(plan, "visuals", visual_ops)
        ops.extend(visual_ops)

    # -- captions ----------------------------------------------------------
    if config.enabled("captions") and caption_plan is not None:
        caption_ops = _caption_ops(
            plan, caption_plan, config, style,
            placements=placements, frames_dir=frames_dir, ffmpeg=ffmpeg,
        )
        _contribute(plan, "captions", caption_ops)
        ops.extend(caption_ops)

    # -- the mix -----------------------------------------------------------
    if config.enabled("sound"):
        plan.mix = mix_module.build_mix(
            dialogue_sources=_source_paths(placements),
            music_path=plan.music.asset_path if plan.music.placed else "",
            sfx_paths=sfx_paths,
            speech_ranges=speech_ranges,
            target_lufs=config.target_lufs,
            peak_ceiling_db=config.peak_ceiling_db,
            music_under_dialogue_db=config.music_under_dialogue_db,
            sfx_under_dialogue_db=config.sfx_under_dialogue_db,
            ffmpeg=ffmpeg,
            measure_fn=measure_fn or mix_module.measure,
        )
        # The music bed's own gain was written from the config default when its
        # operations were built; the measured answer replaces it.
        measured_music = plan.mix.gains.get("music")
        if plan.music.placed and measured_music is not None:
            plan.music.gain_db = measured_music
            for op in ops:
                if (op.get("op") == "audio.gain"
                        and (op.get("clip") or {}).get("track") == layout.music):
                    op["db"] = round(measured_music, 2)
        mix_ops = mix_module.mix_ops(
            plan.mix, layout, sfx_clip_count=len(sfx_paths),
            cut_duration=plan.cut_duration,
            tail_at=_last_clip_midpoint(placements),
        )
        _contribute(plan, "mix", mix_ops)
        ops.extend(mix_ops)
    else:
        plan.mix = MixDecision(
            target_lufs=config.target_lufs,
            peak_ceiling_db=config.peak_ceiling_db,
        )
        plan.mix.warnings.append(
            "Sound is disabled for this pass, so no levels were measured or "
            "set. The timeline's audio is whatever the rough cut left."
        )

    plan.ops = ops
    return plan


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

def _caption_ops(plan: ConformPlan, caption_plan, config: ConformConfig,
                 style, *, placements=(), frames_dir=None,
                 ffmpeg: str = "ffmpeg") -> list[dict]:
    """Accepted captions as styled ``text.create`` overlays.

    ``engine="render"`` rather than ``"auto"``: the rasterised path exists on
    every install, while the MOGRT path needs a template registered in the
    user's Premiere. Asking for the one that always works is what makes a dry
    run that passes here correspond to an execution that can happen.

    The fade is a real opacity animation on the text clip, not a property of
    the text: captions that pop on and off are the single most obvious tell of
    an automated edit.
    """
    ops: list[dict] = []
    track = plan.layout.captions

    accepted = [d for d in getattr(caption_plan, "decisions", ())
                if getattr(d, "accepted", False)]
    index = 0
    for decision in accepted:
        start = float(getattr(decision, "start", -1.0))
        if start < 0:
            plan.unconverted.append({
                "kind": "caption", "at": start,
                "reason": "not_on_the_cut",
                "detail": f'"{decision.text[:60]}" was said in a part of the '
                          "footage this cut does not include",
            })
            continue

        end = float(getattr(decision, "end", start))
        duration = max(MIN_CAPTION_SECONDS, (end - start) + CAPTION_TAIL)

        # Room to the end of the cut, with a margin.
        #
        # Not just "does it fit". The first real episode put a caption at
        # 20.12s of a 20.2s cut, which is a caption on screen for the final
        # two frames -- unreadable, and it landed past the last frame Premiere
        # actually had once clip lengths were rounded to the frame rate. A
        # caption that cannot hold its minimum duration inside the cut is a
        # refusal, not something to squeeze.
        if plan.cut_duration:
            room = plan.cut_duration - END_MARGIN - start
            if room < MIN_CAPTION_SECONDS:
                plan.unconverted.append({
                    "kind": "caption", "at": start, "reason": "no_room",
                    "detail": (
                        f'"{text_of(decision)[:40]}" starts {room:.2f}s before '
                        f"the end of the cut, which is not long enough to read"
                    ),
                })
                continue
            duration = min(duration, room)

        position, zone, why = _caption_position(
            decision, style,
            placements=placements, frames_dir=frames_dir, ffmpeg=ffmpeg,
        )
        text = text_of(decision)
        if not text:
            plan.unconverted.append({
                "kind": "caption", "at": start, "reason": "empty_text",
                "detail": "the caption has no text to put on screen",
            })
            continue

        ops.append({
            "op": "text.create",
            "text": text,
            "track": track,
            "time": round(start, 3),
            "duration": round(duration, 3),
            "position": list(position),
            "engine": "render",
            "font": config.caption_font,
            "size": int(config.caption_size),
            "color": config.caption_color,
            "align": "center",
            "max_width": 0.8,
            "stroke_color": config.caption_stroke_color,
            "stroke_width": float(config.caption_stroke_width),
            "note": (f"{getattr(decision, 'moment', '') or 'caption'} "
                     f"({zone}): {why or getattr(decision, 'reason', '')}"
                     )[:200],
        })

        fade = float(config.caption_fade)
        if fade > 0 and duration > fade * 2.5:
            ops.append({
                "op": "animate",
                "clip": {"track": track, "index": index},
                "property": "Opacity",
                "component": "Opacity",
                "from": 0.0, "to": 100.0,
                "start": 0.0, "duration": round(fade, 3),
                "easing": "sine_out",
                "relative_to": "clip",
                "note": "caption fades up rather than popping on",
            })
            ops.append({
                "op": "animate",
                "clip": {"track": track, "index": index},
                "property": "Opacity",
                "component": "Opacity",
                "from": 100.0, "to": 0.0,
                "start": round(duration - fade, 3), "duration": round(fade, 3),
                "easing": "sine_in",
                "relative_to": "clip",
                "note": "caption fades down",
            })
        index += 1
    return ops


def _caption_position(decision, style, *, placements=(), frames_dir=None,
                      ffmpeg: str = "ffmpeg") -> tuple:
    """``(position, zone, why)`` for one caption.

    The style's safe zone is the prior; the frame decides. A style that says
    "text goes upper centre" is encoding an assumption about where the game
    draws its own interface, and the first real episode this system edited put
    a caption exactly on top of the checkpoint counter. Measuring the frame at
    that moment is the cheapest way to notice.

    Falls back to the style's zone whenever the frame cannot be read, so this
    can improve placement and never worsen it.
    """
    from editing.style.presets import ZONE_POSITION

    preferred = str(getattr(decision, "zone", "") or "")
    fallback = tuple(ZONE_POSITION.get(preferred, (0.5, 0.82)))

    candidates = _candidate_zones(style, ZONE_POSITION)
    if frames_dir is None or not candidates:
        return fallback, preferred or "style default", ""

    frame = _frame_for(decision, placements, frames_dir, ffmpeg=ffmpeg)
    if frame is None:
        return fallback, preferred or "style default", ""

    zone, position, why = placement_module.choose_zone(
        frame, candidates, preferred=preferred, ffmpeg=ffmpeg,
    )
    return tuple(position), zone, why


def _candidate_zones(style, zone_position: dict) -> dict:
    """The zones this style permits, as name -> position.

    Read off the style rather than assumed, so a preset that has ruled a zone
    out cannot be talked back into it by a measurement.
    """
    allowed = getattr(style, "text_zones", None)
    names = [str(name) for name in (allowed or ()) if name in zone_position]
    if not names:
        names = list(zone_position)
    return {name: tuple(zone_position[name]) for name in names}


def _frame_for(decision, placements, frames_dir, *, ffmpeg: str):
    """One still of the source footage at the moment this caption appears."""
    asset_id = str(getattr(decision, "asset_id", "") or "")
    at = float(getattr(decision, "source_start", 0.0))
    source = ""
    for placement in placements:
        if getattr(placement, "asset_id", "") == asset_id:
            source = str(getattr(placement, "source_file", "") or "")
            if source:
                break
    if not source or not Path(source).is_file():
        return None

    folder = Path(frames_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"zone_{asset_id[:12]}_{at:08.3f}.png".replace(".", "_", 1)
    if target.is_file():
        return target
    return placement_module.frame_at(source, at, target, ffmpeg=ffmpeg)


# ---------------------------------------------------------------------------
# Sound
# ---------------------------------------------------------------------------

def _sound_ops(plan: ConformPlan, audio_plan, layout: TrackLayout) -> tuple:
    """Accepted sound cues that name a real file, as placed clips.

    A cue with no file behind it stays a marker. That is the honest form of
    "something belongs here and we do not have it" -- placing silence would
    look like success in every report and sound like nothing at all.
    """
    ops: list[dict] = []
    paths: list[str] = []
    imported: set = set()

    cues = [c for c in getattr(audio_plan, "cues", ())
            if getattr(c, "accepted", False)]
    for cue in cues:
        path = str(getattr(cue, "asset_path", "") or "")
        start = float(getattr(cue, "start", 0.0))
        if not path:
            plan.unconverted.append({
                "kind": "sound", "at": start, "reason": "no_asset",
                "detail": f"{getattr(cue, 'kind', 'cue')}: "
                          f"{getattr(cue, 'placeholder', '') or 'no file matched'}",
            })
            ops.append({
                "op": "marker.add",
                "time": round(start, 3),
                "name": f"SFX: {getattr(cue, 'kind', 'cue')}",
                "comment": (f"{getattr(cue, 'target', '')} | needs: "
                            f"{getattr(cue, 'placeholder', '')}")[:500],
                "type": "comment",
                "note": "sound wanted here, no file available",
            })
            continue
        if not Path(path).is_file():
            plan.unconverted.append({
                "kind": "sound", "at": start, "reason": "file_missing",
                "detail": f"{path} is in the plan but not on disk",
            })
            continue
        if plan.cut_duration and start >= plan.cut_duration - END_GUARD:
            plan.unconverted.append({
                "kind": "sound", "at": start, "reason": "past_the_end",
                "detail": "the cue sits at or beyond the end of the cut",
            })
            continue

        if path not in imported:
            ops.append({
                "op": "project.import",
                "paths": [path],
                "bin": SFX_BIN,
                "note": f"sound: {Path(path).name}",
            })
            imported.add(path)
            paths.append(path)

        end = float(getattr(cue, "end", start))
        length = max(0.05, end - start)
        if plan.cut_duration:
            length = min(length, max(0.05, plan.cut_duration - start))
        ops.append({
            "op": "clip.overwrite",
            "asset": path,
            "track": layout.sfx,
            "time": round(start, 3),
            "in": 0.0,
            "out": round(length, 3),
            "note": (f"{getattr(cue, 'kind', 'cue')}: "
                     f"{getattr(cue, 'target', '')}")[:200],
        })
    return ops, paths


# ---------------------------------------------------------------------------
# Visual treatments
# ---------------------------------------------------------------------------

def _visual_ops(plan: ConformPlan, visual_plan, layout: TrackLayout) -> list[dict]:
    """The visual pass's own operations, retargeted onto this layout.

    Built by ``editing.visuals.premiere`` rather than re-derived here: that
    module already knows how each treatment maps onto the catalog, and a second
    implementation would be a second thing to keep in step.
    """
    try:
        from editing.visuals import premiere as visual_premiere
    except ImportError as exc:  # pragma: no cover - ships together
        plan.warnings.append(f"The visual layer could not be loaded: {exc}")
        return []

    built = visual_premiere.build_premiere_plan(
        visual_plan, name=plan.name, track=layout.treatments,
    )
    for entry in getattr(built, "unsupported", ()):
        plan.unconverted.append({
            "kind": "visual", "at": float(getattr(entry, "start", 0.0)),
            "reason": f"unsupported:{getattr(entry, 'effect', '')}",
            "detail": getattr(entry, "reason", ""),
        })
    return [dict(entry.op) for entry in getattr(built, "operations", ())
            if entry.op]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _contribute(plan: ConformPlan, layer: str, ops: Sequence[dict]) -> None:
    plan.contributions[layer] = len(ops)


def _last_clip_midpoint(placements) -> float:
    """The middle of the final clip, in sequence time.

    Used to name the last clip in a selector. See ``mix.mix_ops`` for why the
    end of the cut is the wrong answer.
    """
    if not placements:
        return 0.0
    last = placements[-1]
    midpoint = getattr(last, "sequence_midpoint", None)
    if midpoint is not None:
        return float(midpoint)
    start = float(getattr(last, "sequence_start", 0.0))
    end = float(getattr(last, "sequence_end", start))
    return start + (end - start) / 2.0


def _source_paths(placements) -> list[str]:
    """Distinct source files behind the cut, in order of first appearance."""
    out: list[str] = []
    for placement in placements:
        path = str(getattr(placement, "source_file", "")
                   or getattr(placement, "source_path", "") or "")
        if path and path not in out and Path(path).is_file():
            out.append(path)
    return out


def _speech_ranges(rough_cut, timeline) -> list:
    """Where somebody is talking, in sequence time.

    Mapped onto the cut rather than read raw: a speech range in source time
    means nothing to a music bed that lives on the edit, and a line the cut
    removed must not duck anything. ``map_to_sequence`` returns None for a
    moment that did not survive, which is exactly the filter wanted here.
    """
    ranges: list = []
    if timeline is None or rough_cut is None:
        return ranges
    try:
        from editing.roughcut.select import map_to_sequence
    except ImportError:  # pragma: no cover - ships together
        return ranges

    placements = list(getattr(rough_cut, "placements", ()) or ())
    for segment in getattr(timeline, "segments", ()) or ():
        asset_id = getattr(segment, "asset_id", "")
        for entry in getattr(segment, "speech_entries", ()) or ():
            start = map_to_sequence(placements, asset_id,
                                    float(getattr(entry, "start", 0.0)))
            end = map_to_sequence(placements, asset_id,
                                  float(getattr(entry, "end", 0.0)))
            if start is None:
                continue
            if end is None:
                # The line runs past the end of the clip it started in. Duck
                # to the end of that clip rather than dropping the range: the
                # speech is audible right up to the cut.
                end = _clip_end_for(placements, asset_id,
                                    float(getattr(entry, "start", 0.0)))
            if end is not None and end > start:
                ranges.append([round(float(start), 3), round(float(end), 3)])
    return _merge(ranges)


def _clip_end_for(placements, asset_id: str, source_time: float):
    """The sequence time at which the clip carrying ``source_time`` ends."""
    for placement in placements:
        if getattr(placement, "asset_id", "") != asset_id:
            continue
        if (float(getattr(placement, "source_in", 0.0)) <= source_time
                <= float(getattr(placement, "source_out", 0.0))):
            return float(getattr(placement, "sequence_end", 0.0))
    return None


def _merge(ranges: list, gap: float = 0.6) -> list:
    if not ranges:
        return []
    ordered = sorted(ranges)
    out = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def _scene_changes(timeline, rough_cut) -> list:
    """Sequence times the structure layer called a scene change.

    Mapped onto the cut for the same reason the speech ranges are: a boundary
    at source time 90s is meaningless to a transition on a timeline where that
    moment was removed or moved.
    """
    if timeline is None or rough_cut is None:
        return []
    try:
        from editing.roughcut.select import map_to_sequence
    except ImportError:  # pragma: no cover - ships together
        return []

    placements = list(getattr(rough_cut, "placements", ()) or ())
    out: list = []
    for segment in getattr(timeline, "segments", ()) or ():
        labels = " ".join(str(v) for v in (
            getattr(segment, "kind", ""),
            getattr(segment, "importance", ""),
            " ".join(getattr(segment, "tags", ()) or ()),
        )).lower()
        if "scene" not in labels and "transition" not in labels:
            continue
        mapped = map_to_sequence(placements, getattr(segment, "asset_id", ""),
                                 float(getattr(segment, "start", 0.0)))
        if mapped is not None:
            out.append(float(mapped))
    return out


def _style_intent(style) -> str:
    """A phrase describing the style, for the colour decision to read."""
    if style is None:
        return ""
    parts = [
        str(getattr(style, "name", "") or ""),
        str(getattr(style, "summary", "") or ""),
        str(getattr(style, "intent", "") or ""),
    ]
    return " ".join(p for p in parts if p).lower()

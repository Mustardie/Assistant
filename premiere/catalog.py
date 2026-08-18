"""The operation catalog: the entire editing interface, as data.

This is the contract between the creative model and Premiere. A plan is a list
of ``{"op": name, ...params}`` objects; every name in here is executable and
every name NOT in here is rejected by the validator before anything runs.

Design rule from the brief, applied throughout: prefer one generic operation
over N hardcoded ones. There is no ``zoom_in``. There is ``property.set`` on
``Motion > Scale``, and ``animate`` to move any numeric property between two
values over time with an easing curve. Everything a "zoom in" needs is a
special case of those, which means the model can express a Ken Burns push, a
whip-pan, a punch-in on a beat, or a parameter ramp we never thought of,
without us shipping a new tool.

Capability honesty: ops that Premiere's scripting API genuinely cannot do are
listed with ``supported=False`` and an ``alternative``. They are still in the
catalog on purpose -- a model that asks for a frame-accurate graph-editor
handle gets a specific "no, use keyframe.bake instead" rather than silence or
a fabricated success.
"""
from __future__ import annotations

from premiere.schema import Field as F
from premiere.schema import OpSpec

# --------------------------------------------------------------------------
# Shared field shapes
# --------------------------------------------------------------------------

CLIP = F("clip", required=True, doc='which clip, e.g. {"track": "V1", "index": 0}')
CLIP_MULTI = F("clip", required=True, doc='one or many clips; supports range/all/name')
TRACK = F("track", required=True, doc='"V1" / "A2"')

#: Interpolation modes. The host maps these onto Premiere's KFInterpMode
#: constants and reports back which ones the installed version accepted --
#: ``caps.probe`` records the real answer rather than trusting this list.
INTERP_MODES = [
    "linear", "hold", "ease_in", "ease_out", "ease_both",
    "bezier", "auto_bezier", "continuous_bezier",
]

#: Named easing curves for ``animate`` / ``keyframe.bake``. These are baked
#: into dense keyframes host-side, which is what makes real curve control
#: possible at all (see the graph-editor note in ``keyframe.bake``).
EASINGS = [
    "linear", "ease_in", "ease_out", "ease_in_out",
    "quad_in", "quad_out", "quad_in_out",
    "cubic_in", "cubic_out", "cubic_in_out",
    "quart_in", "quart_out", "quart_in_out",
    "quint_in", "quint_out", "quint_in_out",
    "sine_in", "sine_out", "sine_in_out",
    "expo_in", "expo_out", "expo_in_out",
    "circ_in", "circ_out", "circ_in_out",
    "back_in", "back_out", "back_in_out",
    "elastic_in", "elastic_out", "elastic_in_out",
    "bounce_in", "bounce_out", "bounce_in_out",
]

EASING_FIELD = F(
    "any",
    default="linear",
    doc='easing: a name from caps.easings, or a cubic-bezier as [x1, y1, x2, y2]',
)

PROPERTY_TARGET = {
    "clip": CLIP,
    "component": F("string", doc='effect/component name, e.g. "Motion", "Opacity", '
                                '"Lumetri Color", "Volume". Omit for the clip\'s '
                                'intrinsic Motion component.'),
    "property": F("string", required=True,
                  doc='parameter name, e.g. "Scale", "Position", "Level", "Exposure". '
                      'Use caps.params to list the real names on a clip.'),
    "index": F("integer", min=0,
               doc="disambiguates repeated components (2nd Gaussian Blur = 1)"),
}


def _t(**extra) -> dict:
    """Property target fields plus operation-specific extras."""
    out = dict(PROPERTY_TARGET)
    out.update(extra)
    return out


OPS: dict = {}


def _op(spec: OpSpec) -> None:
    OPS[spec.name] = spec


# --------------------------------------------------------------------------
# Discovery / inspection  (read-only)
# --------------------------------------------------------------------------

_op(OpSpec(
    "project.info", category="project", mutating=False, needs_sequence=False,
    summary="Project name, path, save state, sequence list, active sequence.",
    fields={},
))

_op(OpSpec(
    "project.assets", category="project", mutating=False, needs_sequence=False,
    summary="List media in the project panel: names, paths, type, duration, fps, "
            "resolution, bin. This is how the editor discovers what footage it has.",
    fields={
        "bin": F("string", doc="only list inside this bin"),
        "media_type": F("enum", choices=["any", "video", "audio", "still", "sequence"],
                        default="any"),
        "recursive": F("boolean", default=True),
    },
))

_op(OpSpec(
    "project.import", category="project", mutating=True, needs_sequence=False,
    summary="Import media files into the project panel (optionally into a bin).",
    fields={
        "paths": F("list", required=True, item=F("string"), min=1,
                   doc="absolute file paths on the Premiere machine"),
        "bin": F("string", doc="target bin name; created if missing"),
        "as_numbered_stills": F("boolean", default=False),
    },
))

_op(OpSpec(
    "project.bin", category="project", mutating=True, needs_sequence=False,
    summary="Create a bin in the project panel.",
    fields={"name": F("string", required=True), "parent": F("string")},
))

_op(OpSpec(
    "project.save", category="project", mutating=True, needs_sequence=False,
    summary="Save the project (optionally Save As to a new path).",
    fields={"path": F("string", doc="omit to save in place")},
))

_op(OpSpec(
    "sequence.list", category="sequence", mutating=False, needs_sequence=False,
    summary="Every sequence in the project with id, name, fps, resolution, duration.",
    fields={},
))

_op(OpSpec(
    "sequence.create", category="sequence", mutating=True, needs_sequence=False,
    summary="Create a sequence, optionally matching a source clip's settings.",
    fields={
        "name": F("string", required=True),
        "from_asset": F("string", doc="asset name/path to match settings from"),
        "preset": F("string", doc="absolute path to a .sqpreset file"),
    },
))

_op(OpSpec(
    "sequence.activate", category="sequence", mutating=True, needs_sequence=False,
    summary="Make a sequence the active one that later operations target.",
    fields={"name": F("string"), "id": F("string")},
))

_op(OpSpec(
    "sequence.info", category="sequence", mutating=False,
    summary="Active sequence settings: fps, resolution, duration, track counts, "
            "in/out points, playhead.",
    fields={},
))

_op(OpSpec(
    "timeline.snapshot", category="timeline", mutating=False,
    summary="Full timeline read: every track, every clip with stable id, name, "
            "start/end, in/out, speed, enabled, and effect list. The primary way "
            "the editor sees the current state of its edit.",
    fields={
        "include_effects": F("boolean", default=True),
        "include_keyframes": F("boolean", default=False,
                               doc="also dump keyframe times per animated parameter "
                                   "(large -- use on one track when debugging)"),
        "track": F("track", doc="limit to one track"),
        "range": F("list", item=F("time"), min=2, max=2,
                   doc="[start, end] seconds -- only clips overlapping this window"),
    },
))

_op(OpSpec(
    "timeline.playhead", category="timeline", mutating=True,
    summary="Move the playhead / set sequence in-out points.",
    fields={
        "time": F("time"),
        "in": F("time"),
        "out": F("time"),
    },
))

# --------------------------------------------------------------------------
# Capability discovery
# --------------------------------------------------------------------------

_op(OpSpec(
    "caps.probe", category="caps", mutating=False, needs_sequence=False,
    summary="What this Premiere install genuinely supports: version, QE DOM "
            "availability, which keyframe interpolation modes take effect, "
            "whether MOGRT/caption/transition APIs are present. Run this before "
            "planning anything exotic instead of guessing.",
    fields={"refresh": F("boolean", default=False, doc="bypass the cached probe")},
))

_op(OpSpec(
    "caps.operations", category="caps", mutating=False, needs_sequence=False,
    summary="This catalog, as data: every operation, its parameters, types, "
            "defaults and support status. Lets the model read the interface "
            "instead of memorising it.",
    fields={"category": F("string", doc="filter by category"),
            "op": F("string", doc="describe one operation in full")},
))

_op(OpSpec(
    "caps.effects", category="caps", mutating=False, needs_sequence=False,
    summary="Every video/audio effect name installed in this Premiere, as "
            "accepted by effect.apply. Prevents inventing effect names.",
    fields={
        "kind": F("enum", choices=["video", "audio", "both"], default="both"),
        "search": F("string", doc="case-insensitive substring filter"),
    },
))

_op(OpSpec(
    "caps.transitions", category="caps", mutating=False, needs_sequence=False,
    summary="Every installed video/audio transition name, as accepted by "
            "transition.apply.",
    fields={
        "kind": F("enum", choices=["video", "audio", "both"], default="both"),
        "search": F("string"),
    },
))

_op(OpSpec(
    "caps.params", category="caps", mutating=False,
    summary="The REAL parameters of a clip or one of its effects: name, current "
            "value, type, whether keyframing is supported, and whether it is "
            "currently animated. This is the antidote to hallucinated property "
            "names -- read it, then drive property.set / keyframe.* with what it "
            "actually returned.",
    fields={
        "clip": CLIP,
        "component": F("string", doc="omit to list every component on the clip"),
        "include_values": F("boolean", default=True),
    },
))

_op(OpSpec(
    "caps.fonts", category="caps", mutating=False, needs_sequence=False,
    summary="Font families available for text rendering, so typography choices "
            "are real rather than guessed.",
    fields={"search": F("string")},
))

_op(OpSpec(
    "caps.easings", category="caps", mutating=False, needs_sequence=False,
    summary="Named easing curves accepted by animate / keyframe.bake.",
    fields={},
))

# --------------------------------------------------------------------------
# Tracks
# --------------------------------------------------------------------------

_op(OpSpec(
    "track.add", category="track",
    summary="Add video and/or audio tracks to the sequence.",
    fields={
        "video": F("integer", default=0, min=0, max=32),
        "audio": F("integer", default=0, min=0, max=32),
        "at": F("integer", min=0, doc="insert position; default appends on top"),
    },
))

_op(OpSpec(
    "track.set", category="track",
    summary="Rename / mute / lock / solo / target a track.",
    fields={
        "track": TRACK,
        "name": F("string"),
        "muted": F("boolean"),
        "locked": F("boolean"),
        "solo": F("boolean"),
        "targeted": F("boolean"),
    },
))

_op(OpSpec(
    "track.remove", category="track",
    summary="Remove a track (or all empty tracks).",
    fields={
        "track": F("track"),
        "empty_only": F("boolean", default=False,
                        doc="true removes every empty track and ignores 'track'"),
    },
))

# --------------------------------------------------------------------------
# Clips: arrangement
# --------------------------------------------------------------------------

_op(OpSpec(
    "clip.insert", category="clip",
    summary="Insert an asset at a time on a track, rippling later clips right.",
    fields={
        "asset": F("string", required=True, doc="asset name or path from project.assets"),
        "track": TRACK,
        "time": F("time", required=True),
        "in": F("time", doc="source in-point"),
        "out": F("time", doc="source out-point"),
    },
))

_op(OpSpec(
    "clip.overwrite", category="clip",
    summary="Place an asset at a time, overwriting whatever is already there.",
    fields={
        "asset": F("string", required=True),
        "track": TRACK,
        "time": F("time", required=True),
        "in": F("time"),
        "out": F("time"),
    },
))

_op(OpSpec(
    "clip.append", category="clip",
    summary="Append an asset after the last clip on a track.",
    fields={
        "asset": F("string", required=True),
        "track": TRACK,
        "in": F("time"),
        "out": F("time"),
        "gap": F("duration", doc="optional gap before the appended clip"),
    },
))

_op(OpSpec(
    "clip.remove", category="clip", multi=True,
    summary="Delete clip(s). ripple=true closes the gap, false leaves a hole.",
    fields={"clip": CLIP_MULTI, "ripple": F("boolean", default=False)},
))

_op(OpSpec(
    "clip.move", category="clip", multi=True,
    summary="Move clip(s) in time and/or to another track. Use 'to' for an "
            "absolute start or 'by' for a relative nudge.",
    fields={
        "clip": CLIP_MULTI,
        "to": F("time"),
        "by": F("time", doc="relative offset in seconds; negative moves earlier"),
        "track": F("track", doc="move to this track"),
    },
))

_op(OpSpec(
    "clip.split", category="clip", multi=True,
    summary="Razor the clip(s) at one or more times.",
    fields={
        "clip": CLIP_MULTI,
        "at": F("list", required=True, item=F("time"), min=1,
                doc="split point(s) in sequence time"),
        "all_tracks": F("boolean", default=False,
                        doc="also cut clips at the same time on every other track"),
    },
))

_op(OpSpec(
    "clip.trim", category="clip", multi=True,
    summary="Trim a clip's head or tail. Positive 'by' shortens, negative extends. "
            "ripple=true shifts later clips to close/open the gap.",
    fields={
        "clip": CLIP_MULTI,
        "edge": F("enum", required=True, choices=["in", "out", "both"]),
        "by": F("time", doc="relative trim amount in seconds"),
        "to": F("time", doc="absolute sequence time to trim the edge to"),
        "ripple": F("boolean", default=False),
    },
))

_op(OpSpec(
    "clip.slip", category="clip", multi=True,
    summary="Slip source media inside the clip without moving the clip.",
    fields={"clip": CLIP_MULTI, "by": F("time", required=True)},
))

_op(OpSpec(
    "clip.duplicate", category="clip",
    summary="Copy a clip (with its effects and keyframes) to another time/track.",
    fields={
        "clip": CLIP,
        "time": F("time", required=True),
        "track": F("track"),
        "mode": F("enum", choices=["overwrite", "insert"], default="overwrite"),
    },
))

_op(OpSpec(
    "clip.set", category="clip", multi=True,
    summary="Set clip flags: name, enabled/disabled, label colour.",
    fields={
        "clip": CLIP_MULTI,
        "name": F("string"),
        "enabled": F("boolean"),
        "label": F("string", doc="Premiere label colour name, e.g. 'Violet'"),
    },
))

_op(OpSpec(
    "clip.speed", category="clip", multi=True,
    summary="Set constant clip speed. rate 2.0 = 2x faster, 0.5 = half speed. "
            "Reverse and audio pitch preservation supported.",
    fields={
        "clip": CLIP_MULTI,
        "rate": F("number", min=0.01, max=100.0,
                  doc="speed multiplier; 1.0 = original"),
        "reverse": F("boolean", default=False),
        "maintain_pitch": F("boolean", default=True),
        "ripple": F("boolean", default=False,
                    doc="shift later clips to absorb the duration change"),
    },
))

_op(OpSpec(
    "clip.speed_ramp", category="clip",
    summary="Variable speed over time (a speed ramp) via Time Remapping keyframes. "
            "Give [{time, rate}] points; smooth=true blends between them instead "
            "of stepping. Requires Time Remapping to be keyframable on the clip -- "
            "caps.params will show it.",
    fields={
        "clip": CLIP,
        "points": F("list", required=True, min=1, item=F("object", fields={
            "time": F("time", required=True, doc="time within the clip"),
            "rate": F("number", required=True, min=0.0, max=100.0),
        })),
        "smooth": F("boolean", default=True,
                    doc="ease between speed changes rather than hard-cutting"),
        "transition": F("duration", default=0.4,
                        doc="length of the blend when smooth=true"),
    },
    notes="Premiere exposes Time Remapping > Speed as a keyframable parameter. "
          "The velocity handles that the graph editor draws are NOT scriptable, "
          "so smooth ramps are produced by baking intermediate keyframes.",
))

_op(OpSpec(
    "clip.freeze", category="clip",
    summary="Freeze-frame a clip at a source time.",
    fields={
        "clip": CLIP,
        "at": F("time", doc="source time to hold; default = clip in-point"),
        "duration": F("duration", doc="length of the freeze"),
    },
    notes="Implemented via time-remap hold keyframes when Frame Hold is not "
          "scriptable on this version; caps.probe reports which path is used.",
))

_op(OpSpec(
    "clip.link", category="clip",
    summary="Link or unlink a video clip and its audio.",
    fields={"clip": CLIP, "linked": F("boolean", required=True)},
))

_op(OpSpec(
    "gap.remove", category="clip",
    summary="Close gaps on a track (all of them, or the one at a time).",
    fields={"track": TRACK, "at": F("time", doc="omit to close every gap")},
))

# --------------------------------------------------------------------------
# THE CORE: generic properties and keyframes
# --------------------------------------------------------------------------

_op(OpSpec(
    "property.get", category="property", mutating=False,
    summary="Read a parameter's current value (and whether it is animated).",
    fields=_t(clip=CLIP, at=F("time", doc="read the animated value at this time")),
))

_op(OpSpec(
    "property.set", category="property", mutating=True, multi=True,
    summary="Set ANY keyframable or static parameter on a clip: Motion "
            "(Position, Scale, Rotation, Anchor Point), Opacity, Crop, Volume, "
            "or any parameter of any applied effect. This is the generic setter "
            "-- there is no separate op per property.",
    fields=_t(
        clip=CLIP_MULTI,
        value=F("value", required=True,
                doc="number, boolean, string, [x, y] point, or [r, g, b, a] colour"),
        at=F("time", doc="if given, sets a keyframe at this time instead of the "
                         "static value"),
    ),
))

_op(OpSpec(
    "property.reset", category="property", multi=True,
    summary="Restore a parameter to its default and clear its keyframes.",
    fields=_t(clip=CLIP_MULTI),
))

_op(OpSpec(
    "keyframe.list", category="keyframe", mutating=False,
    summary="Every keyframe on a parameter: time, value, interpolation.",
    fields=_t(clip=CLIP),
))

_op(OpSpec(
    "keyframe.set", category="keyframe", multi=True,
    summary="Write keyframes on any parameter in one call. Each entry is "
            "{time, value, interp?}. This is the primary animation primitive -- "
            "position moves, scale punches, blur ramps, volume envelopes and "
            "effect-parameter animation are all this operation.",
    fields=_t(
        clip=CLIP_MULTI,
        keyframes=F("list", required=True, min=1, item=F("object", fields={
            "time": F("time", required=True),
            "value": F("value", required=True),
            "interp": F("enum", choices=INTERP_MODES),
        })),
        replace=F("boolean", default=False,
                  doc="clear existing keyframes on this parameter first"),
        relative_to=F("enum", choices=["sequence", "clip"], default="clip",
                      doc="are keyframe times measured from the sequence start or "
                          "the clip start? clip-relative is usually what you want"),
    ),
))

_op(OpSpec(
    "keyframe.add", category="keyframe", multi=True,
    summary="Add a single keyframe, holding the parameter's current value unless "
            "a value is given.",
    fields=_t(
        clip=CLIP_MULTI,
        at=F("time", required=True),
        value=F("value"),
        interp=F("enum", choices=INTERP_MODES),
        relative_to=F("enum", choices=["sequence", "clip"], default="clip"),
    ),
))

_op(OpSpec(
    "keyframe.remove", category="keyframe", multi=True,
    summary="Remove keyframe(s) at a time or across a time range.",
    fields=_t(
        clip=CLIP_MULTI,
        at=F("time"),
        range=F("list", item=F("time"), min=2, max=2),
        relative_to=F("enum", choices=["sequence", "clip"], default="clip"),
    ),
))

_op(OpSpec(
    "keyframe.clear", category="keyframe", multi=True,
    summary="Remove every keyframe on a parameter and make it static again.",
    fields=_t(clip=CLIP_MULTI),
))

_op(OpSpec(
    "keyframe.move", category="keyframe", multi=True,
    summary="Shift keyframes in time (retime an animation without rebuilding it).",
    fields=_t(
        clip=CLIP_MULTI,
        by=F("time", doc="shift every keyframe by this many seconds"),
        scale=F("number", min=0.01, max=100.0,
                doc="stretch/squash the animation around its first keyframe"),
        range=F("list", item=F("time"), min=2, max=2,
                doc="only affect keyframes inside this window"),
        relative_to=F("enum", choices=["sequence", "clip"], default="clip"),
    ),
))

_op(OpSpec(
    "keyframe.interpolation", category="keyframe", multi=True,
    summary="Set the interpolation/easing mode of existing keyframes.",
    fields=_t(
        clip=CLIP_MULTI,
        interp=F("enum", required=True, choices=INTERP_MODES),
        at=F("time", doc="one keyframe; omit to apply to all"),
        range=F("list", item=F("time"), min=2, max=2),
        relative_to=F("enum", choices=["sequence", "clip"], default="clip"),
    ),
    notes="Premiere accepts a keyframe interpolation MODE via script, but the "
          "per-keyframe Bezier handle positions the graph editor draws are not "
          "scriptable. For exact curve shapes use keyframe.bake.",
))

_op(OpSpec(
    "keyframe.bake", category="keyframe", multi=True,
    summary="Bake an arbitrary easing curve into dense keyframes. This is how "
            "you get exact curve control -- including custom cubic-bezier shapes "
            "that the scriptable interpolation modes cannot express. Give the "
            "start/end time and value plus an easing (named or [x1,y1,x2,y2]) and "
            "it samples the curve into keyframes.",
    fields=_t(
        clip=CLIP_MULTI,
        start=F("time", required=True),
        end=F("time", required=True),
        from_value=F("value", doc="omit to use the parameter's value at 'start'"),
        to_value=F("value", required=True),
        easing=EASING_FIELD,
        resolution=F("integer", default=0, min=0, max=240,
                     doc="keyframes per second; 0 = match sequence frame rate"),
        replace=F("boolean", default=True,
                  doc="clear existing keyframes in the baked window first"),
        relative_to=F("enum", choices=["sequence", "clip"], default="clip"),
    ),
    notes="Baking trades timeline tidiness for exactness. It is the honest "
          "workaround for the graph editor's velocity handles being outside the "
          "scripting API.",
))

_op(OpSpec(
    "animate", category="keyframe", multi=True,
    summary="The high-level animation verb: move ANY parameter from one value to "
            "another over a duration with an easing curve. Uses native keyframe "
            "interpolation when the easing maps onto a scriptable mode, and bakes "
            "the curve when it does not. Covers push-ins, slides, fades, blur "
            "ramps, colour moves and effect-parameter animation with one op.",
    fields=_t(
        clip=CLIP_MULTI,
        to=F("value", required=True),
        **{"from": F("value", doc="omit to start from the current value")},
        start=F("time", default=0.0),
        duration=F("duration", required=True),
        easing=EASING_FIELD,
        relative_to=F("enum", choices=["sequence", "clip"], default="clip"),
    ),
))

# --------------------------------------------------------------------------
# Effects
# --------------------------------------------------------------------------

_op(OpSpec(
    "effect.list", category="effect", mutating=False,
    summary="Effects currently applied to a clip, in render order.",
    fields={"clip": CLIP},
))

_op(OpSpec(
    "effect.apply", category="effect", multi=True,
    summary="Apply an effect to clip(s), optionally setting parameters in the "
            "same call. Effect names must come from caps.effects.",
    fields={
        "clip": CLIP_MULTI,
        "effect": F("string", required=True, doc='e.g. "Gaussian Blur", "Lumetri Color"'),
        "params": F("object", doc='{"Blurriness": 20} -- parameter names from caps.params'),
    },
))

_op(OpSpec(
    "effect.remove", category="effect", multi=True,
    summary="Remove an effect from clip(s).",
    fields={
        "clip": CLIP_MULTI,
        "effect": F("string", doc="omit with all=true to strip every removable effect"),
        "index": F("integer", min=0),
        "all": F("boolean", default=False),
    },
))

# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------

_op(OpSpec(
    "transition.apply", category="transition", multi=True,
    summary="Apply a transition at a clip edge or cut point. Names come from "
            "caps.transitions ('Cross Dissolve', 'Dip to Black', 'Constant Power').",
    fields={
        "clip": CLIP_MULTI,
        "transition": F("string", default="Cross Dissolve"),
        "edge": F("enum", choices=["in", "out", "both"], default="out"),
        "duration": F("duration", default=1.0),
        "alignment": F("enum", choices=["center", "start", "end"], default="center",
                       doc="how the transition straddles the cut"),
    },
))

_op(OpSpec(
    "transition.remove", category="transition", multi=True,
    summary="Remove transitions at a clip edge.",
    fields={
        "clip": CLIP_MULTI,
        "edge": F("enum", choices=["in", "out", "both"], default="both"),
    },
))

# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

_op(OpSpec(
    "audio.gain", category="audio", multi=True,
    summary="Set clip volume in dB (static), or at a time as a keyframe.",
    fields={
        "clip": CLIP_MULTI,
        "db": F("number", required=True, min=-96.0, max=15.0),
        "at": F("time", doc="keyframe the level at this time instead of setting it flat"),
        "relative_to": F("enum", choices=["sequence", "clip"], default="clip"),
    },
))

_op(OpSpec(
    "audio.fade", category="audio", multi=True,
    summary="Fade audio in and/or out by writing level keyframes at the clip edges.",
    fields={
        "clip": CLIP_MULTI,
        "in": F("duration", doc="fade-in length"),
        "out": F("duration", doc="fade-out length"),
        "floor_db": F("number", default=-60.0, min=-96.0, max=0.0),
        "easing": EASING_FIELD,
    },
))

_op(OpSpec(
    "audio.duck", category="audio",
    summary="Duck a music/bed clip underneath speech: writes level keyframes that "
            "dip during the given ranges and recover between them. The ranges "
            "normally come from a transcript, so the editor can duck music under "
            "dialogue without hand-placing keyframes.",
    fields={
        "clip": CLIP,
        "under": F("list", required=True, min=1, item=F("object", fields={
            "start": F("time", required=True),
            "end": F("time", required=True),
        }), doc="speech ranges in SEQUENCE time"),
        "duck_db": F("number", default=-14.0, min=-96.0, max=0.0,
                     doc="level while ducked"),
        "base_db": F("number", default=0.0, min=-96.0, max=15.0,
                     doc="level when not ducked"),
        "attack": F("duration", default=0.25, doc="fade-down time"),
        "release": F("duration", default=0.45, doc="fade-up time"),
        "merge_gap": F("duration", default=0.6,
                       doc="speech ranges closer than this are treated as one"),
    },
))

_op(OpSpec(
    "audio.pan", category="audio", multi=True,
    summary="Set the clip's stereo balance (-100 left .. 100 right).",
    fields={
        "clip": CLIP_MULTI,
        "pan": F("number", required=True, min=-100.0, max=100.0),
        "at": F("time"),
    },
))

# --------------------------------------------------------------------------
# Text, graphics, overlays
# --------------------------------------------------------------------------

# Typography fields carry NO schema defaults on purpose.
#
# If they did, the validator would fill every one of them in on every call, and
# the engine could not tell "the model asked for Arial" from "the model said
# nothing about the font". A named style would then be overridden by defaults
# on every field it was meant to supply, silently doing nothing. Defaults live
# in the rendering layer (premiere/assets.py) and in the built-in styles, which
# is the only place that can apply them *after* a style has been merged.
TYPOGRAPHY = {
    "font": F("string", doc="family name from caps.fonts (default: Arial)"),
    "size": F("number", min=1.0, max=2000.0, doc="points (default: 72)"),
    "weight": F("enum", choices=["thin", "light", "regular", "medium", "semibold",
                                 "bold", "black"], doc="default: bold"),
    "italic": F("boolean", doc="default: false"),
    "color": F("value", doc='"#RRGGBB", "#RRGGBBAA" or [r,g,b,a] 0-255 '
                            '(default: white)'),
    "align": F("enum", choices=["left", "center", "right"], doc="default: center"),
    "line_spacing": F("number", min=0.4, max=4.0, doc="default: 1.15"),
    "letter_spacing": F("number", min=-50.0, max=200.0, doc="default: 0"),
    "max_width": F("number", min=0.05, max=1.0,
                   doc="wrap width as a fraction of frame width"),
    "stroke_color": F("value", doc="outline colour"),
    "stroke_width": F("number", min=0.0, max=100.0, doc="default: 0"),
    "shadow": F("object", fields={
        "color": F("value", default="#000000C0"),
        "offset": F("list", item=F("number"), min=2, max=2, default=[4, 4]),
        "blur": F("number", default=6.0, min=0.0, max=100.0),
    }),
    "background": F("object", fields={
        "color": F("value", default="#000000A0"),
        "padding": F("number", default=24.0, min=0.0, max=400.0),
        "radius": F("number", default=12.0, min=0.0, max=400.0),
    }),
}

_op(OpSpec(
    "text.create", category="text",
    summary="Create a styled text overlay on the timeline. Full typography "
            "control: font, size, weight, colour, stroke, shadow, background "
            "plate, alignment, line and letter spacing. The result is an ordinary "
            "timeline clip, so every transform/keyframe/effect op works on it -- "
            "that is what makes text animatable without a separate text-animation "
            "system.",
    fields=dict(
        text=F("string", required=True),
        track=F("track"),
        time=F("time", default=0.0),
        duration=F("duration", default=4.0),
        position=F("list", item=F("number"), min=2, max=2, default=[0.5, 0.5],
                   doc="[x, y] as a fraction of the frame; [0.5, 0.5] is centre"),
        style=F("string", doc="name of a style defined with text.style"),
        engine=F("enum", choices=["auto", "mogrt", "render"], default="auto",
                 doc="'mogrt' uses a Motion Graphics template (live text, editable "
                     "in Premiere); 'render' rasterises to a PNG overlay. 'auto' "
                     "prefers mogrt when a template is configured."),
        **TYPOGRAPHY,
    ),
    notes="Premiere's scripting API cannot construct a native Essential Graphics "
          "text layer from nothing. Two real paths exist and both are implemented: "
          "a .mogrt template whose text/params are then driven by script, or a "
          "rasterised PNG overlay. caps.probe reports which is active.",
))

_op(OpSpec(
    "text.style", category="text", mutating=False, needs_sequence=False,
    summary="Define/list reusable named text styles so a video keeps one look. "
            "Styles are stored with the project and referenced by name from "
            "text.create and captions.build.",
    fields=dict(
        name=F("string", doc="omit to list all defined styles"),
        delete=F("boolean", default=False),
        **{k: F(v.kind, choices=v.choices, min=v.min, max=v.max,
                item=v.item, fields=v.fields, doc=v.doc)
           for k, v in TYPOGRAPHY.items()},
    ),
))

_op(OpSpec(
    "text.set", category="text",
    summary="Change the text content and/or styling of an existing text clip.",
    fields=dict(
        clip=CLIP,
        text=F("string"),
        **{k: F(v.kind, choices=v.choices, min=v.min, max=v.max,
                item=v.item, fields=v.fields, doc=v.doc)
           for k, v in TYPOGRAPHY.items()},
    ),
))

_op(OpSpec(
    "graphic.shape", category="graphic",
    summary="Create a shape overlay: rectangle, rounded rectangle, ellipse/circle, "
            "line, triangle, arrow or polygon. Fill, stroke, opacity, corner "
            "radius. Lands on the timeline as a normal clip, so it animates with "
            "the same keyframe ops as everything else -- lower thirds, progress "
            "bars, callout boxes and wipes are all this.",
    fields={
        "shape": F("enum", required=True, choices=["rectangle", "rounded_rect",
                                                   "ellipse", "circle", "line",
                                                   "triangle", "arrow", "polygon"]),
        "track": F("track"),
        "time": F("time", default=0.0),
        "duration": F("duration", default=4.0),
        "position": F("list", item=F("number"), min=2, max=2, default=[0.5, 0.5]),
        "size": F("list", item=F("number"), min=2, max=2, default=[0.3, 0.1],
                  doc="[w, h] as a fraction of the frame"),
        "fill": F("value", default="#FFFFFF"),
        "stroke_color": F("value"),
        "stroke_width": F("number", default=0.0, min=0.0, max=200.0),
        "radius": F("number", default=0.0, min=0.0, max=400.0),
        "opacity": F("number", default=100.0, min=0.0, max=100.0),
        "rotation": F("number", default=0.0, min=-3600.0, max=3600.0),
        "points": F("list", item=F("list", item=F("number"), min=2, max=2), min=3,
                    doc="polygon vertices as frame fractions"),
        "sides": F("integer", min=3, max=64, doc="regular polygon side count"),
    },
))

_op(OpSpec(
    "graphic.image", category="graphic",
    summary="Place an image/PNG/logo overlay on the timeline with position, "
            "scale, rotation and opacity applied in one step.",
    fields={
        "path": F("string", required=True, doc="absolute path, or an imported asset name"),
        "track": F("track"),
        "time": F("time", default=0.0),
        "duration": F("duration", default=4.0),
        "position": F("list", item=F("number"), min=2, max=2),
        "scale": F("number", min=0.1, max=1000.0),
        "rotation": F("number", min=-3600.0, max=3600.0),
        "opacity": F("number", min=0.0, max=100.0),
    },
))

_op(OpSpec(
    "adjustment.create", category="graphic",
    summary="Create an adjustment layer spanning a time range, so a grade or "
            "effect applies to everything beneath it.",
    fields={
        "track": F("track"),
        "time": F("time", default=0.0),
        "duration": F("duration", required=True),
        "effects": F("list", item=F("object", fields={
            "effect": F("string", required=True),
            "params": F("object"),
        }), doc="effects to apply to the new layer immediately"),
    },
    notes="If this Premiere build cannot synthesise an adjustment layer by script, "
          "the engine falls back to applying the same effects to every clip in the "
          "range and says so in the result.",
))

_op(OpSpec(
    "mogrt.import", category="graphic",
    summary="Place a Motion Graphics template (.mogrt) on the timeline.",
    fields={
        "path": F("string", required=True),
        "track": F("track"),
        "time": F("time", default=0.0),
        "duration": F("duration"),
        "params": F("object", doc="template parameter values to set immediately"),
    },
))

_op(OpSpec(
    "mogrt.params", category="graphic", mutating=False,
    summary="List a placed MOGRT's editable parameters (including text fields), "
            "so they can be driven with property.set / keyframe.set.",
    fields={"clip": CLIP},
))

# --------------------------------------------------------------------------
# Captions / subtitles
# --------------------------------------------------------------------------

_op(OpSpec(
    "captions.build", category="captions",
    summary="Build subtitles from transcript segments. mode='caption' creates a "
            "real Premiere caption track (from generated SRT); mode='graphic' "
            "renders each line as a styled overlay clip, which gives full "
            "typography and animation control for YouTube-style subtitles.",
    fields=dict(
        segments=F("list", required=True, min=1, item=F("object", fields={
            "start": F("time", required=True),
            "end": F("time", required=True),
            "text": F("string", required=True),
        })),
        mode=F("enum", choices=["caption", "graphic"], default="caption"),
        track=F("track", doc="target track for mode='graphic'"),
        style=F("string", doc="named text style"),
        position=F("list", item=F("number"), min=2, max=2, default=[0.5, 0.82]),
        max_chars=F("integer", default=42, min=8, max=200,
                    doc="wrap/split segments longer than this"),
        **{k: F(v.kind, choices=v.choices, min=v.min, max=v.max,
                item=v.item, fields=v.fields, doc=v.doc)
           for k, v in TYPOGRAPHY.items()},
    ),
))

_op(OpSpec(
    "captions.import", category="captions",
    summary="Import an existing .srt/.vtt subtitle file as a caption track.",
    fields={
        "path": F("string", required=True),
        "time": F("time", default=0.0),
    },
))

# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------

_op(OpSpec(
    "color.grade", category="color", multi=True,
    summary="Apply Lumetri Color and set grading parameters in one call: "
            "exposure, contrast, highlights, shadows, whites, blacks, temperature, "
            "tint, saturation, vibrance and any other Lumetri parameter this build "
            "exposes. Unknown/unexposed parameters are reported, never silently "
            "dropped. Every parameter set here is also keyframable via keyframe.*.",
    fields={
        "clip": CLIP_MULTI,
        "exposure": F("number", min=-5.0, max=5.0),
        "contrast": F("number", min=-100.0, max=100.0),
        "highlights": F("number", min=-100.0, max=100.0),
        "shadows": F("number", min=-100.0, max=100.0),
        "whites": F("number", min=-100.0, max=100.0),
        "blacks": F("number", min=-100.0, max=100.0),
        "temperature": F("number", min=-100.0, max=100.0),
        "tint": F("number", min=-100.0, max=100.0),
        "saturation": F("number", min=0.0, max=200.0),
        "vibrance": F("number", min=-100.0, max=100.0),
        "sharpen": F("number", min=0.0, max=100.0),
        "fade": F("number", min=0.0, max=100.0),
        "extra": F("object", doc="any other Lumetri parameter by its real name "
                                 "from caps.params"),
    },
))

_op(OpSpec(
    "color.lut", category="color", multi=True,
    summary="Apply a .cube LUT to clip(s) as an input or creative look.",
    fields={
        "clip": CLIP_MULTI,
        "path": F("string", required=True, doc="absolute path to a .cube LUT"),
        "slot": F("enum", choices=["input", "creative"], default="creative"),
        "intensity": F("number", min=0.0, max=100.0),
    },
    notes="Lumetri's LUT slots are dropdown parameters. Whether a path can be "
          "assigned by script varies by build -- caps.probe reports the answer, "
          "and this op fails loudly rather than pretending when it cannot.",
))

_op(OpSpec(
    "color.preset", category="color", multi=True,
    summary="Apply a saved grading preset (.prfpset) or a named look to clip(s).",
    fields={
        "clip": CLIP_MULTI,
        "name": F("string", required=True),
        "path": F("string", doc="absolute path to a .prfpset file"),
    },
    supported=False,
    unsupported_reason="Premiere's scripting API has no entry point for applying "
                       "an effect preset (.prfpset) to a track item.",
    alternative="color.grade with explicit parameter values, or define the look "
                "once as a LUT and use color.lut",
))

_op(OpSpec(
    "color.curves", category="color",
    summary="Set Lumetri RGB curves / colour wheels directly.",
    fields={
        "clip": CLIP,
        "curve": F("enum", choices=["master", "red", "green", "blue", "hue_sat"]),
        "points": F("list", item=F("list", item=F("number"), min=2, max=2)),
        "wheel": F("enum", choices=["shadows", "midtones", "highlights"]),
        "rgb": F("list", item=F("number"), min=3, max=3),
    },
    supported=False,
    unsupported_reason="Lumetri's curve and colour-wheel controls are opaque "
                       "composite parameters; the scripting API exposes no "
                       "readable/writable point list for them.",
    alternative="color.grade (exposure/contrast/highlights/shadows/whites/blacks "
                "reach most of the same result), or bake the curve into a .cube "
                "LUT and apply it with color.lut",
))

# --------------------------------------------------------------------------
# Safety: history, undo, revisions
# --------------------------------------------------------------------------

_op(OpSpec(
    "history.list", category="history", mutating=False, needs_sequence=False,
    summary="Recent operations this layer executed, with their results and the "
            "revision each produced.",
    fields={"limit": F("integer", default=25, min=1, max=500)},
))

_op(OpSpec(
    "history.undo", category="history", needs_sequence=False,
    summary="Undo the last N executed operations via Premiere's own undo stack.",
    fields={"steps": F("integer", default=1, min=1, max=100)},
))

_op(OpSpec(
    "history.checkpoint", category="history", needs_sequence=False,
    summary="Name the current project state so it can be restored later.",
    fields={"name": F("string", required=True)},
))

_op(OpSpec(
    "history.restore", category="history", needs_sequence=False,
    summary="Restore a named checkpoint (reopens the saved project state).",
    fields={"name": F("string", required=True)},
))


def get(name: str):
    return OPS.get(name)


def describe(category: str = "", op: str = "") -> dict:
    """The catalog as data -- backs caps.operations."""
    if op:
        spec = OPS.get(op)
        if not spec:
            return {"error": f"unknown operation {op!r}",
                    "operations": sorted(OPS)}
        return spec.describe()
    items = [s.describe() for s in OPS.values()
             if not category or s.category == category]
    items.sort(key=lambda d: (d["category"], d["op"]))
    return {
        "count": len(items),
        "categories": sorted({s.category for s in OPS.values()}),
        "operations": items,
    }


def categories() -> list:
    return sorted({s.category for s in OPS.values()})


def unsupported() -> list:
    """Everything deliberately marked as not achievable, with alternatives."""
    return [
        {"op": s.name, "reason": s.unsupported_reason, "alternative": s.alternative}
        for s in OPS.values() if not s.supported
    ]

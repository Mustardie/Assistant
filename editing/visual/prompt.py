"""The prompt Qwen3-VL is given for one window.

Kept in its own module because it is the part of this layer most likely to be
tuned, and because a prompt that lives next to HTTP plumbing never gets read.

Three things it has to get right:

* **Closed vocabularies.** The model is shown the exact allowed values. Free
  text gets coerced later (``editing.schema.coerce_*``), but a model that picks
  from a list is far more consistent than one that is corrected afterwards.
* **Minecraft specifics.** A general "describe this video" prompt returns
  "a person is playing a video game". Naming the biomes, mobs and HUD elements
  is what turns the output into something an editor can cut on.
* **Honest uncertainty.** The model is told to answer ``unknown`` and lower its
  confidence rather than guess, because a confidently wrong environment label
  is worse for downstream ranking than an admitted gap.
"""
from __future__ import annotations

import json

from editing.config import SamplingConfig
from editing.schema import (
    CAMERA_MOTIONS, ENVIRONMENTS, IMPORTANCE_LEVELS, PLAYER_ACTIONS,
)

SYSTEM_PROMPT = (
    "You are a video analysis engine for a Minecraft YouTube editor. You are "
    "shown a few frames sampled from one short window of gameplay footage, in "
    "chronological order. You describe what is happening. You do not edit, "
    "suggest edits, or write commentary.\n\n"
    "Answer with a single JSON object and nothing else: no prose before or "
    "after, no markdown fence. Every key listed in the schema must be present. "
    "If you cannot tell something from the frames, use \"unknown\" (or an empty "
    "list) and lower your confidence -- a wrong guess is worse than an "
    "admitted gap."
)

#: The response contract, embedded in the user prompt. Written as an example
#: rather than a JSON Schema because a small VLM copies a filled-in example far
#: more reliably than it satisfies a formal spec.
_RESPONSE_SHAPE = {
    "environment": "one of the environment values",
    "actions": ["one or more action values, most important first"],
    "entities": ["mobs, players, animals or notable items visible"],
    "threats": ["entities or hazards actively endangering the player"],
    "ui": {
        "inventory_open": False,
        "crafting_open": False,
        "chest_open": False,
        "death_screen": False,
        "achievement_toast": False,
        "low_health": False,
        "chat_open": False,
        "map_open": False,
        "coordinates": "the XYZ readout if the debug screen is visible, else \"\"",
        "hotbar": ["items readable in the hotbar"],
    },
    "camera": {
        "motion": "one of the camera motion values",
        "intensity": "0.0 (still) to 1.0 (violent)",
    },
    "importance": "one of the importance values",
    "confidence": "0.0 to 1.0 -- how sure you are overall",
    "suggested_range": {
        "start": "seconds, absolute, within the window",
        "end": "seconds, absolute, within the window",
    },
    "notes": "one or two sentences describing what you actually see",
}

_GUIDANCE = """\
How to judge each field:

environment  Where the player is. Read the terrain, block types and sky.
             Netherrack/lava seas mean nether. Stone brick corridors with
             silverfish spawners mean stronghold. Wooden supports with rails
             mean mineshaft. Player-built structures mean base.

actions      What the player is doing, not what they might do next. Prefer the
             action that dominates the window. "mining" is breaking blocks for
             resources; "building" is placing them deliberately; "travelling"
             is moving through already-explored space.

entities     Everything alive or notable on screen. Name mobs specifically
             (creeper, zombie, skeleton, enderman, warden, piglin) -- the
             specific name is what makes a moment findable later.

threats      Only entities or hazards actually endangering the player right
             now: a creeper mid-hiss, lava at the player's feet, a mob
             attacking. An unaware zombie across a field is an entity, not a
             threat.

ui           Read the HUD literally. low_health means the hearts are visibly
             depleted (roughly a quarter or less). death_screen means the "You
             Died!" overlay. achievement_toast means the advancement popup in
             the top right.

camera       How the viewport moves across the frames. intensity is how
             violent that motion is, not how fast the player walks.

importance   How much a viewer would care:
             boring   nothing is happening; walking or waiting
             setup    context, preparation, explanation, travel with purpose
             tension  something might go wrong; a threat is building
             danger   the player is actively in danger of dying
             payoff   the goal is achieved: the diamonds, the kill, the finish
             funny    something went comically wrong
             reveal   something is seen for the first time

suggested_range
             The sub-range of this window a human editor would actually keep.
             When the whole window is worth keeping, return the whole window.
             Absolute seconds, inside the window's own start and end.

notes        What you see, plainly. This is read by a human debugging the
             analysis, so describe evidence ("hearts at 2, creeper flashing
             white") rather than conclusions.
"""


def _values(name: str, values) -> str:
    return f"{name}: {', '.join(values)}"


def build_user_prompt(
    *,
    window_start: float,
    window_end: float,
    frame_times,
    source_name: str = "",
    sampling: SamplingConfig | None = None,
) -> str:
    """The per-window user prompt, including the window's real timestamps.

    The absolute timestamps are given so ``suggested_range`` comes back in the
    same coordinate space as everything else. Asking for a range relative to the
    window and converting afterwards sounds equivalent, but in practice a model
    given "0 to 8" answers about the window and a model given "312.0 to 320.0"
    answers about the recording -- and the second is what the timeline needs.
    """
    frames = list(frame_times)
    lines = [
        f"Window: {window_start:.2f}s to {window_end:.2f}s of the recording"
        + (f" ({source_name})" if source_name else "") + ".",
        f"{len(frames)} frames follow, taken at "
        + ", ".join(f"{time:.2f}s" for time in frames) + ".",
        "",
        "Allowed values -- use these exact strings:",
        _values("environment", ENVIRONMENTS),
        _values("actions", PLAYER_ACTIONS),
        _values("camera.motion", CAMERA_MOTIONS),
        _values("importance", IMPORTANCE_LEVELS),
        "",
        _GUIDANCE,
        "",
        "Return exactly this JSON shape:",
        json.dumps(_RESPONSE_SHAPE, indent=2),
    ]
    if sampling is not None and sampling.validated().dense_frames_per_window == len(frames):
        lines.insert(
            2,
            "This window was flagged as high-change, so it is sampled densely: "
            "expect visible movement or a scene change between frames.",
        )
    return "\n".join(lines)

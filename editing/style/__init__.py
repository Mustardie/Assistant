"""Style presets and layered edit execution.

    rough cut + recommendations + a style preset (+ optional critic findings)
        -> seven independent layers of candidates
        -> deduplication and density enforcement
        -> one ordered operation plan
        -> offline dry run
        -> (only on an explicit --yes) applied to the same scratch sequence

    presets.py   the four styles, as numbers, with validation
    schema.py    LayerItem and LayeredEditPlan
    captions.py  which spoken lines earn text, and where it can safely go
    emphasis.py  punches, pushes, and the markers for the ones it refuses
    audio.py     music/SFX placeholders, plus the two fades that are real
    cards.py     title and chapter cards at genuine section boundaries
    compile.py   candidates -> deduped, density-limited, ordered operations
    execute.py   the three modes, the allowlist, the guards
    report.py    the human-readable output

Two properties shape everything here.

**Style ceilings only ever subtract.** No preset can make the system busier
than the evidence justifies -- every density field is a maximum, and the
compiler removes candidates to fit rather than inventing edits to fill a quota.
That is what separates "intentionally styled" from "randomly over-edited", and
it is why a tighter style is always the safer one to try first.

**This layer cannot change timing.** The operation allowlist contains no
``clip.*`` operation at all: it adds a track, scales clips already on the
timeline, writes audio keyframes, places overlays and drops markers. Nothing
ripples, so nothing it plans can end up describing a frame that moved -- and
the whole pass can be undone by deleting one track and its markers.
"""
from editing.style.audio import build_audio
from editing.style.captions import build_captions, condense
from editing.style.cards import build_cards
from editing.style.compile import CompileOptions, compile_layers
from editing.style.emphasis import build_emphasis
from editing.style.execute import (
    ALLOWED_OPS, MODES, changes_timing, check_allowed, run, summarise,
    targets_scratch_sequence,
)
from editing.style.execute import dry_run as dry_run_layers
from editing.style.presets import (
    DEFAULT_PRESET, LAYER_KINDS, PRESETS, TEXT_ZONES, ZONE_POSITION,
    StylePreset, get, names,
)
from editing.style.report import render, render_deferred, render_density
from editing.style.schema import (
    EFFECTS, LAYERS, RISKS, LayerEvidence, LayerItem, LayeredEditPlan,
)

__all__ = [
    # presets
    "StylePreset", "PRESETS", "DEFAULT_PRESET", "get", "names",
    "LAYER_KINDS", "TEXT_ZONES", "ZONE_POSITION",
    # schema
    "LayerItem", "LayeredEditPlan", "LayerEvidence", "LAYERS", "EFFECTS",
    "RISKS",
    # layers
    "build_captions", "build_emphasis", "build_audio", "build_cards",
    "condense",
    # compile
    "compile_layers", "CompileOptions",
    # execute
    "dry_run_layers", "run", "targets_scratch_sequence", "check_allowed",
    "changes_timing", "summarise", "MODES", "ALLOWED_OPS",
    # report
    "render", "render_density", "render_deferred",
]

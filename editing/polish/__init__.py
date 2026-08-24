"""Restrained caption and audio polish: the little that goes on top.

    key-moment captions   only the nine moments that carry an episode
    audio polish          a riser, a hit, a whoosh, a bed, a beat of silence

    schema.py    CaptionConfig, CaptionDecision, CaptionPlan,
                 AudioPolishConfig, AudioCue, AudioPolishPlan
    captions.py  which spoken lines are the episode, and which are talking
    audio.py     which moments earn a sound, and which cues would spam
    sidecar.py   the caption plan as an .srt beside the proxy
    store.py     where the plans live
    report.py    the readable reports
    run.py       one pass, end to end

Everything in this package is **subtractive**. Both halves generate candidates
from what the earlier passes recorded, then refuse most of them against named
rules, and every refusal is kept in the plan with the rule that made it. A pass
that placed four captions out of sixty candidates and a pass that is broken
both print "4"; only the refusal list tells them apart.

Three properties worth stating:

**Captions are punctuation, not subtitles.** ``key_moments`` is the mode that
matters -- a small per-minute budget, a nine-kind vocabulary, and a hard
refusal for anything long, unclear, quiet or filler. ``dense`` exists, is never
a default, and says so in its own plan.

**No cue lands on a word.** Hits, risers and whooshes are checked against the
transcript with a guard band. A bed is the exception and is ducked instead,
which the cue says in its own safety notes.

**Nothing here plays, draws or executes anything.** In ``placeholders`` mode no
library is read at all; in ``assets`` mode anything unmatched stays a note and
lands on the shopping list. Captions are never burned into the proxy -- the
sidecar ``.srt`` is how to see them against it.
"""
from editing.polish.audio import build_audio_plan
from editing.polish.captions import build_caption_plan
from editing.polish.schema import (
    AUDIO_CUE_KINDS, AUDIO_POLISH_MODES, AUDIO_REJECT_REASONS, CAPTION_MODES,
    CAPTION_REJECT_REASONS, KEY_MOMENTS, NOT_MEASURED, AudioCue,
    AudioPolishConfig, AudioPolishPlan, CaptionConfig, CaptionDecision,
    CaptionPlan, allowed_cue_kinds, audio_defaults, caption_defaults,
)
from editing.polish.sidecar import BURN_IN_NOTE, to_srt, write_srt

__all__ = [
    # schema
    "CaptionConfig", "CaptionDecision", "CaptionPlan",
    "AudioPolishConfig", "AudioCue", "AudioPolishPlan",
    "CAPTION_MODES", "AUDIO_POLISH_MODES", "KEY_MOMENTS",
    "CAPTION_REJECT_REASONS", "AUDIO_REJECT_REASONS", "AUDIO_CUE_KINDS",
    "NOT_MEASURED", "caption_defaults", "audio_defaults", "allowed_cue_kinds",
    # passes
    "build_caption_plan", "build_audio_plan",
    # sidecar
    "to_srt", "write_srt", "BURN_IN_NOTE",
]

"""The conform pass: turning every decision into a real timeline, and a file.

Where the rest of the editing package decides *what* the edit should be, this
package makes it exist. It is the last mile:

    plans -> operations -> Premiere -> edited sequence -> exported video

``build``        compose captions, sound, music, visuals, colour, the mix and
                 the transitions into one list of catalog operations
``execute``      the modes and guards that let that list run
``deliver``      identify the finished sequence and render it to a file
``color``        choose a colour treatment and emit ``color.grade``
``music``        choose, place, trim and fade one music bed
``mix``          measure real loudness and set every level from it
``transitions``  place the few transitions that have an argument

Nothing here decides anything the earlier passes already decided. The four
things it does decide -- colour, music, the mix and transitions -- had no owner
before, and each is deliberately the smallest honest version of itself.
"""
from editing.conform.schema import (
    COLOR_LOOKS, CONFORM_MODES, ColorDecision, ConformConfig, ConformPlan,
    DeliveryResult, LevelMeasurement, MixDecision, MusicDecision,
    TransitionDecision,
)

__all__ = [
    "COLOR_LOOKS", "CONFORM_MODES", "ColorDecision", "ConformConfig",
    "ConformPlan", "DeliveryResult", "LevelMeasurement", "MixDecision",
    "MusicDecision", "TransitionDecision",
]

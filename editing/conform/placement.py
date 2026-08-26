"""Where on the frame a caption should actually go.

The style presets carry safe zones, and safe zones are a reasonable *prior*:
"put text in the upper centre, because the player's own HUD usually lives at
the bottom". They are not an answer, because the prior is wrong for any game
that puts something at the top -- and the first real episode this system edited
proved it. The style chose ``upper_center``, the caption rendered exactly where
the game draws its checkpoint counter, and the two sat on top of each other.

A still can settle it. This module samples the frame at the moment the caption
would appear, measures how busy each candidate zone is, and picks the quietest
one.

"Busy" is the luma spread across the band: ``YHIGH - YLOW``, the gap between
the 90th and 10th percentile brightness that ``signalstats`` reports. Bright
interface text on a darker scene produces a large spread; sky, ground, a wall
or motion blur produce a small one. The percentiles rather than min/max
deliberately: a single bright pixel should not disqualify a zone.

Deliberately small:

* one frame per caption, not a temporal analysis;
* candidates come from the style's own zones, so a style that forbids the lower
  third still cannot be talked into using it;
* the style's choice wins on a tie and whenever the frame cannot be read, so
  nothing here can make placement worse than it was.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Sequence

from editing import ffmpeg as ff
from editing.errors import ToolMissingError

logger = logging.getLogger("nova.editing.conform.placement")

#: Height of the band a zone occupies, as a fraction of the frame. Text sits
#: inside this; measuring a taller band would drag in unrelated picture.
BAND_HEIGHT = 0.16

#: Width of the band, centred on the zone's x. Captions wrap at 0.8 of frame
#: width, so a slightly wider measurement is the honest one.
BAND_WIDTH = 0.86

#: How much quieter another zone must be before the style's choice is
#: overruled. A margin, so a frame that is uniformly busy does not produce a
#: different answer every caption.
IMPROVEMENT_MARGIN = 6.0

_YHIGH = re.compile(r"lavfi\.signalstats\.YHIGH=([0-9.]+)")
_YLOW = re.compile(r"lavfi\.signalstats\.YLOW=([0-9.]+)")


def parse_busyness(text: str) -> Optional[float]:
    """Luma spread from a ``signalstats`` dump, or None when unreadable.

    ``signalstats`` reports no standard deviation, so the spread between its
    high and low percentiles stands in for one. It is the right shape for the
    question anyway: what makes a zone bad for text is *contrast* against
    something already drawn there.
    """
    highs = [float(v) for v in _YHIGH.findall(text or "")]
    lows = [float(v) for v in _YLOW.findall(text or "")]
    if not highs or not lows:
        return None
    spread = sum(highs) / len(highs) - sum(lows) / len(lows)
    return round(max(0.0, spread), 2)


def measure_zone(
    frame_path: str | Path,
    position: Sequence[float],
    *,
    ffmpeg: str = "ffmpeg",
    timeout: float = 30.0,
) -> Optional[float]:
    """How busy the band around ``position`` is. ``None`` when unreadable."""
    source = Path(frame_path)
    if not source.is_file():
        return None

    x, y = float(position[0]), float(position[1])
    left = max(0.0, min(1.0 - BAND_WIDTH, x - BAND_WIDTH / 2.0))
    top = max(0.0, min(1.0 - BAND_HEIGHT, y - BAND_HEIGHT / 2.0))

    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-vf", (
            f"crop=iw*{BAND_WIDTH:.4f}:ih*{BAND_HEIGHT:.4f}:"
            f"iw*{left:.4f}:ih*{top:.4f},"
            "signalstats,metadata=print:file=-"
        ),
        "-frames:v", "1", "-f", "null", "-",
    ]
    try:
        result = ff._run(command, timeout=timeout)
    except ToolMissingError:
        return None
    except Exception as exc:  # noqa: BLE001 - unmeasured is a valid answer
        logger.debug("zone measurement failed for %s: %s", source, exc)
        return None
    return parse_busyness(result.stdout or "")


def choose_zone(
    frame_path: str | Path,
    candidates: dict,
    *,
    preferred: str = "",
    ffmpeg: str = "ffmpeg",
) -> tuple:
    """``(zone, position, evidence)`` for the quietest candidate zone.

    ``candidates`` maps zone name -> ``(x, y)``. The preferred zone is returned
    unchanged unless another is quieter by more than ``IMPROVEMENT_MARGIN``.
    """
    if not candidates:
        return preferred, (0.5, 0.82), "no candidate zones"

    measured: dict = {}
    for zone, position in candidates.items():
        busyness = measure_zone(frame_path, position, ffmpeg=ffmpeg)
        if busyness is not None:
            measured[zone] = busyness

    if not measured:
        position = candidates.get(preferred) or next(iter(candidates.values()))
        return (preferred or next(iter(candidates)), tuple(position),
                "the frame could not be measured, so the style's zone stands")

    quietest = min(measured, key=lambda zone: measured[zone])
    if preferred in measured:
        gain = measured[preferred] - measured[quietest]
        if gain <= IMPROVEMENT_MARGIN:
            return preferred, tuple(candidates[preferred]), (
                f"{preferred} measures {measured[preferred]:.0f}, and no zone "
                f"is more than {IMPROVEMENT_MARGIN:.0f} quieter"
            )
        return quietest, tuple(candidates[quietest]), (
            f"moved from {preferred} ({measured[preferred]:.0f}) to "
            f"{quietest} ({measured[quietest]:.0f}): there is something on "
            "screen where the style wanted the text"
        )
    return quietest, tuple(candidates[quietest]), (
        f"{quietest} is the quietest measured zone ({measured[quietest]:.0f})"
    )


def frame_at(
    video: str | Path,
    at: float,
    destination: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> Optional[Path]:
    """One still from a source file, for measuring. ``None`` on failure."""
    try:
        ff.extract_frame(video, at, destination, ffmpeg=ffmpeg)
    except Exception as exc:  # noqa: BLE001 - a missing frame is not fatal
        logger.debug("could not extract %s at %.2fs: %s", video, at, exc)
        return None
    target = Path(destination)
    return target if target.is_file() else None

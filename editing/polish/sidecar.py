"""The caption plan as a subtitle file you can put next to the proxy.

The proxy render encodes each segment and joins them with the concat demuxer,
which is what makes it survive a folder of mismatched game capture. Burning
subtitles into that would mean a second full re-encode of the joined file with
a ``subtitles`` filter -- a different, slower strategy, and one that fails on
exactly the fonts-and-libass edge cases a person cannot debug from here.

So captions are **not burned in**, and this module is the honest alternative:
an SRT in *sequence* time, written beside the video, which every player will
load. It costs nothing, it is inspectable in a text editor, and it cannot
produce a video whose captions disagree with the plan that made them.

``burn_in_note`` is what the reports say about all of this, in one place, so
nothing can quietly start claiming the video has captions in it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from editing.polish.schema import CaptionDecision, CaptionPlan

#: Said wherever a report mentions the proxy and the captions in one breath.
BURN_IN_NOTE = (
    "Captions are not burned into the proxy. The render joins pre-encoded "
    "segments, and adding text would mean re-encoding the whole file with a "
    "subtitle filter -- so the plan writes a .srt beside the video instead. "
    "Open the proxy in VLC or MPV and the captions load with it."
)


def burn_in_note() -> str:
    return BURN_IN_NOTE


def timestamp(seconds: float) -> str:
    """``HH:MM:SS,mmm``, the only format SRT accepts."""
    total = max(0.0, float(seconds))
    hours, rest = divmod(total, 3600.0)
    minutes, secs = divmod(rest, 60.0)
    whole = int(secs)
    millis = int(round((secs - whole) * 1000))
    if millis >= 1000:      # rounding at the second boundary
        whole += 1
        millis = 0
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"


def to_srt(captions: Sequence[CaptionDecision]) -> str:
    """Accepted captions as SRT text, in sequence-time order.

    Only accepted ones. A refused caption in a subtitle file would be a
    caption, which is the whole thing this pass exists to avoid.
    """
    usable = sorted(
        (c for c in captions if c.accepted and c.start >= 0 and c.text),
        key=lambda c: c.start,
    )
    blocks: list[str] = []
    for index, caption in enumerate(usable, start=1):
        end = max(caption.end, caption.start + 0.4)
        blocks.append(
            f"{index}\n"
            f"{timestamp(caption.start)} --> {timestamp(end)}\n"
            f"{caption.text}\n"
        )
    return "\n".join(blocks)


def write_srt(plan: CaptionPlan, path) -> Path:
    """Write the sidecar and record its location on the plan."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_srt(plan.decisions), encoding="utf-8")
    plan.sidecar_path = str(target)
    plan.burned_in = False
    return target

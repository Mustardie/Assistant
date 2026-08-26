"""Choosing a colour treatment, and executing it.

``color.grade`` has been in the operation catalog since the catalog existed and
nothing has ever decided to use it. This module is the smallest thing that
changes that honestly: it looks at the footage, picks one named look from a
short table, and emits the operation.

What it deliberately is not: a colour-science system. It does not shot-match,
it does not build a node graph, it does not know what the scene is. It answers
one question -- "is there a defensible reason to nudge this footage, and in
which direction" -- and the answer is allowed to be no.

Two inputs decide:

* **the style**, which carries an intent ("punchy" for a fast gameplay edit,
  "clean" for a tutorial). This is taste, and taste belongs to the style.
* **the footage**, measured with FFmpeg's ``signalstats``: average luma and
  average saturation across a handful of frames. This is evidence, and it can
  overrule the style in the one case where it should -- footage that is already
  dark should not be given a look that crushes it further, and footage that is
  already vivid should not be pushed until it clips.

Every decision names its reason from ``COLOR_REASONS``, and a decision of
"neutral, no evidence" is a normal outcome rather than a failure.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Sequence

from editing import ffmpeg as ff
from editing.conform.schema import COLOR_LOOKS, ColorDecision
from editing.errors import ToolMissingError

logger = logging.getLogger("nova.editing.conform.color")

#: How many frames to sample when measuring. Enough to be representative of an
#: episode, cheap enough to run every time.
SAMPLE_FRAMES = 12

#: Average luma (0-255) below which footage counts as dark, and above which it
#: counts as bright. Wide on purpose: this only has to catch footage that is
#: obviously one or the other.
DARK_LUMA = 72.0
BRIGHT_LUMA = 176.0

#: Average chroma distance from neutral. Low is washed out, high is already
#: vivid enough that adding saturation risks clipping.
LOW_SATURATION = 18.0
HIGH_SATURATION = 68.0

#: Style intent -> the look it asks for, before the footage gets a say.
STYLE_LOOK = {
    "documentary": "clean",
    "tutorial": "clean",
    "explainer": "clean",
    "vlog": "warm",
    "cinematic": "cool",
    "horror": "cool",
    "gameplay": "punchy",
    "fast": "punchy",
    "energetic": "punchy",
    "highlight": "punchy",
}

_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")
_UAVG = re.compile(r"lavfi\.signalstats\.UAVG=([0-9.]+)")
_VAVG = re.compile(r"lavfi\.signalstats\.VAVG=([0-9.]+)")


def parse_signalstats(text: str) -> dict:
    """Average luma and chroma spread from a ``signalstats`` metadata dump.

    A pure parser, so the thresholds above can be tested against known numbers
    without decoding video. Chroma is reported as the mean distance of U and V
    from neutral (128), which is a serviceable stand-in for saturation and is
    the only one available from ``signalstats`` directly.
    """
    lumas = [float(v) for v in _YAVG.findall(text or "")]
    us = [float(v) for v in _UAVG.findall(text or "")]
    vs = [float(v) for v in _VAVG.findall(text or "")]
    if not lumas:
        return {}
    chroma = 0.0
    if us and vs:
        pairs = list(zip(us, vs))
        chroma = sum(
            ((u - 128.0) ** 2 + (v - 128.0) ** 2) ** 0.5 for u, v in pairs
        ) / len(pairs)
    return {
        "luma": round(sum(lumas) / len(lumas), 2),
        "chroma": round(chroma, 2),
        "frames": len(lumas),
    }


def measure_footage(
    path: str | Path,
    *,
    frames: int = SAMPLE_FRAMES,
    duration: float = 0.0,
    ffmpeg: str = "ffmpeg",
    timeout: float = 180.0,
) -> dict:
    """Average luma and chroma over a sample of frames. ``{}`` when unreadable."""
    target = Path(path)
    if not target.is_file():
        return {}

    # Sample across the file rather than reading all of it: a rate that yields
    # roughly `frames` samples over the known duration, or a fixed low rate
    # when the duration is unknown.
    if duration and duration > 0:
        rate = max(0.05, float(frames) / float(duration))
    else:
        rate = 0.5
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(target),
        "-map", "0:v:0?",
        "-vf", f"fps={rate:.4f},signalstats,metadata=print:file=-",
        "-frames:v", str(int(max(1, frames))),
        "-an", "-sn", "-dn", "-f", "null", "-",
    ]
    try:
        result = ff._run(command, timeout=timeout)
    except ToolMissingError:
        return {}
    except Exception as exc:  # noqa: BLE001 - unmeasured is a valid answer
        logger.debug("signalstats failed for %s: %s", target, exc)
        return {}
    return parse_signalstats(result.stdout or "")


def scale_params(params: dict, strength: float) -> dict:
    """Scale a look towards neutral.

    Multiplicative parameters (saturation is 100 = unchanged) scale around
    100; additive ones (exposure, contrast, temperature: 0 = unchanged) scale
    around zero. Getting that wrong is how a half-strength look ends up
    desaturating the picture.
    """
    factor = max(0.0, min(1.0, float(strength)))
    out: dict = {}
    for name, value in (params or {}).items():
        number = float(value)
        if name in ("saturation", "vibrance") and number > 1.0:
            out[name] = round(100.0 + (number - 100.0) * factor, 3)
        else:
            out[name] = round(number * factor, 3)
    return out


def decide(
    *,
    style_name: str = "",
    style_intent: str = "",
    requested: str = "",
    strength: float = 1.0,
    measurements: Optional[Sequence[dict]] = None,
) -> ColorDecision:
    """Pick a look, and say why.

    Order of precedence, strongest first: an explicit request, then evidence
    from the footage that contradicts the style, then the style's own intent,
    then nothing.
    """
    measurements = [m for m in (measurements or ()) if m]
    decision = ColorDecision(strength=max(0.0, min(1.0, float(strength))))

    if measurements:
        luma = sum(m.get("luma", 0.0) for m in measurements) / len(measurements)
        chroma = sum(m.get("chroma", 0.0) for m in measurements) / len(measurements)
        decision.measured = {
            "luma": round(luma, 2),
            "chroma": round(chroma, 2),
            "clips": len(measurements),
        }
    else:
        luma = chroma = None

    # 1. An explicit request wins, and is not second-guessed.
    if requested:
        look = str(requested).strip().lower()
        if look not in COLOR_LOOKS:
            decision.look = "neutral"
            decision.reason = "requested"
            decision.summary = (
                f"'{requested}' is not a known look. Known looks: "
                + ", ".join(sorted(COLOR_LOOKS))
            )
            decision.evidence.append("an unknown look was requested")
            return decision
        decision.look = look
        decision.reason = "requested"
        decision.evidence.append(f"the look was asked for by name: {look}")
        return _finish(decision)

    # 2. Evidence that overrules taste.
    if luma is not None:
        if luma <= DARK_LUMA:
            decision.look = "flat"
            decision.reason = "dark_footage"
            decision.evidence.append(
                f"average luma is {luma:.0f} of 255, under the {DARK_LUMA:.0f} "
                "mark, so the shadows are already crushed"
            )
            return _finish(decision)
        if luma >= BRIGHT_LUMA:
            decision.look = "clean"
            decision.reason = "bright_footage"
            decision.evidence.append(
                f"average luma is {luma:.0f} of 255, over the "
                f"{BRIGHT_LUMA:.0f} mark; contrast would clip the highlights"
            )
            return _finish(decision)
        if chroma is not None and chroma <= LOW_SATURATION:
            decision.look = "clean"
            decision.reason = "low_saturation"
            decision.evidence.append(
                f"average chroma is {chroma:.0f}, under {LOW_SATURATION:.0f}: "
                "the footage is washed out and wants lifting, not styling"
            )
            return _finish(decision)
        if chroma is not None and chroma >= HIGH_SATURATION:
            decision.look = "neutral"
            decision.reason = "high_saturation"
            decision.evidence.append(
                f"average chroma is {chroma:.0f}, over {HIGH_SATURATION:.0f}: "
                "already vivid, so leave it alone"
            )
            return decision

    # 3. The style's taste.
    intent = (style_intent or style_name or "").strip().lower()
    for keyword, look in STYLE_LOOK.items():
        if keyword and keyword in intent:
            decision.look = look
            decision.reason = "style_default"
            decision.evidence.append(
                f"the '{intent}' style asks for a {look} look"
            )
            return _finish(decision)

    decision.look = "neutral"
    decision.reason = "no_evidence"
    decision.summary = (
        "Nothing about the footage or the style argued for a grade, so the "
        "colour is left alone."
    )
    return decision


def _finish(decision: ColorDecision) -> ColorDecision:
    look = COLOR_LOOKS[decision.look]
    decision.summary = look["summary"]
    decision.params = scale_params(look["params"], decision.strength)
    decision.applied = bool(decision.params)
    if not decision.applied:
        decision.summary = look["summary"] + " (nothing to change)"
    return decision


def grade_ops(decision: ColorDecision, layout, *, clip_count: int = 0) -> list[dict]:
    """The grade, as one catalog operation over the programme track.

    One operation for every clip, not one per clip: a grade that changes at
    every cut reads as a mistake. ``clip.all`` is exactly what the multi-clip
    selector is for.
    """
    if not decision.applied or not decision.params:
        return []
    decision.clips_affected = int(clip_count)
    return [{
        "op": "color.grade",
        "clip": {"track": layout.programme, "all": True},
        "note": f"{decision.look}: {decision.summary}"[:200],
        **decision.params,
    }]

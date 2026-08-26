"""Measuring audio, and turning the measurements into a mix.

Everything about levels in this system before now was a constant with a comment
saying it was an opinion: a bed at -18 dB, a one-shot at -8. Those numbers are
fine as a starting point and useless as an answer, because they say nothing
about the footage they are being applied to. Commentary recorded at -30 LUFS
and commentary recorded at -12 LUFS need opposite treatment, and a table cannot
tell them apart.

So this module measures. FFmpeg's ``ebur128`` filter reports integrated
loudness (LUFS), loudness range (LU) and true peak (dBTP) for any file it can
decode. From those three numbers the mix falls out:

* **dialogue** is moved to the target loudness, so the programme lands where
  the platform expects instead of wherever the microphone happened to sit;
* **music** and **effects** are set relative to *measured* dialogue, so
  "18 dB under the voice" means eighteen dB under the actual voice;
* **nothing** is allowed to push the true peak above the ceiling, and the gain
  is reduced if it would.

The one rule that matters: **an unmeasured source is reported as unmeasured.**
It keeps the documented default and the decision records that it was a default.
A mix that quietly guesses is worse than one that says which parts it knows.

FFmpeg missing, a file with no audio track, and a codec FFmpeg cannot decode
are all ordinary states here and all produce a measurement with
``measured=False`` rather than an exception.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Sequence

from editing import ffmpeg as ff
from editing.conform.schema import (
    DEFAULT_PEAK_CEILING_DB, DEFAULT_TARGET_LUFS, LevelMeasurement,
    MixDecision,
)
from editing.errors import ToolMissingError

logger = logging.getLogger("nova.editing.conform.mix")

#: A loudness scan decodes every sample. Generous for a long recording.
MEASURE_TIMEOUT = 1800.0

#: Below this, ``ebur128`` is measuring silence rather than a signal, and its
#: integrated reading is meaningless. Treat it as unmeasurable.
SILENCE_FLOOR_LUFS = -60.0

#: Never move a source by more than this. A gain this large means the
#: measurement or the source is wrong, and a 40 dB boost turns a quiet room
#: tone into a roar.
MAX_GAIN_DB = 18.0
MIN_GAIN_DB = -30.0

_SUMMARY = re.compile(
    r"Integrated loudness:.*?I:\s*(-?[0-9.]+|-?inf)\s*LUFS"
    r".*?(?:LRA:\s*(-?[0-9.]+)\s*LU)?"
    r".*?(?:Peak:\s*(-?[0-9.]+|-?inf)\s*dBFS)?",
    re.S,
)
_FIELD = re.compile(r"^\s*(I|LRA|Peak|Threshold):\s*(-?[0-9.]+|-?inf)", re.M)


def parse_ebur128_summary(text: str) -> dict:
    """Pull ``I``, ``LRA`` and true peak out of an ``ebur128`` summary block.

    A pure parser so the arithmetic below is testable without FFmpeg. The
    summary FFmpeg prints looks like::

        Integrated loudness:
          I:         -23.7 LUFS
          Threshold: -34.1 LUFS
        Loudness range:
          LRA:         8.2 LU
        True peak:
          Peak:       -1.4 dBFS

    The field names repeat between sections (``Peak`` appears under True peak,
    ``I`` under Integrated), so this reads the *last* occurrence of each, which
    is the final summary rather than a running value.
    """
    out: dict = {}
    for name, value in _FIELD.findall(text or ""):
        cleaned = value.strip().lower()
        if "inf" in cleaned:
            number = float("-inf")
        else:
            try:
                number = float(cleaned)
            except ValueError:
                continue
        out[name] = number
    return {
        "lufs": out.get("I", float("-inf")),
        "lra": out.get("LRA", 0.0),
        "peak_db": out.get("Peak", float("-inf")),
    }


def measure(
    path: str | Path,
    *,
    role: str = "dialogue",
    source: str = "",
    ffmpeg: str = "ffmpeg",
    start: float = 0.0,
    duration: float = 0.0,
    timeout: float = MEASURE_TIMEOUT,
) -> LevelMeasurement:
    """Integrated loudness, loudness range and true peak for one file.

    Never raises for an unreadable file: the result carries ``measured=False``
    and the reason, because a mix that cannot measure one source still has to
    make a decision about the others.
    """
    target = Path(path)
    result = LevelMeasurement(
        source=source or target.name, role=role, path=str(target),
    )
    if not target.is_file():
        result.error = "file not found"
        return result

    command = [ffmpeg, "-nostdin", "-hide_banner"]
    if start > 0:
        command += ["-ss", f"{float(start):.3f}"]
    command += ["-i", str(target)]
    if duration > 0:
        command += ["-t", f"{float(duration):.3f}"]
    command += [
        "-map", "0:a:0?",
        "-af", "ebur128=peak=true",
        "-vn", "-sn", "-dn",
        "-f", "null", "-",
    ]

    try:
        completed = ff._run(command, timeout=timeout)
    except ToolMissingError:
        result.error = "ffmpeg is not installed"
        return result
    except Exception as exc:  # noqa: BLE001 - a failed scan is a result
        result.error = str(exc)[:200]
        return result

    # ebur128 writes its summary to stderr.
    parsed = parse_ebur128_summary(completed.stderr or "")
    lufs = parsed["lufs"]
    if lufs == float("-inf") or lufs < SILENCE_FLOOR_LUFS:
        result.error = (
            "no measurable audio (the source is silent or has no audio track)"
        )
        return result

    result.lufs = round(float(lufs), 2)
    result.lra = round(float(parsed["lra"] or 0.0), 2)
    peak = parsed["peak_db"]
    result.peak_db = -99.0 if peak == float("-inf") else round(float(peak), 2)
    result.measured = True
    return result


def gain_to_reach(
    measurement: LevelMeasurement,
    target_lufs: float,
    *,
    peak_ceiling_db: float = DEFAULT_PEAK_CEILING_DB,
) -> tuple:
    """The gain that moves a measured source to ``target_lufs``.

    Returns ``(db, clipped)``. ``clipped`` is True when the gain was reduced to
    keep the true peak under the ceiling -- which is the whole reason this
    returns a pair rather than a number: "we wanted +9 and took +4 because the
    peaks would not allow it" is a thing the report has to be able to say.
    """
    if not measurement.measured:
        return 0.0, False
    wanted = float(target_lufs) - measurement.lufs
    wanted = max(MIN_GAIN_DB, min(MAX_GAIN_DB, wanted))

    headroom = float(peak_ceiling_db) - measurement.peak_db
    if wanted > headroom:
        return round(max(MIN_GAIN_DB, headroom), 2), True
    return round(wanted, 2), False


def build_mix(
    *,
    dialogue_sources: Sequence[str] = (),
    music_path: str = "",
    sfx_paths: Sequence[str] = (),
    speech_ranges: Sequence[Sequence[float]] = (),
    target_lufs: float = DEFAULT_TARGET_LUFS,
    peak_ceiling_db: float = DEFAULT_PEAK_CEILING_DB,
    music_under_dialogue_db: float = -18.0,
    sfx_under_dialogue_db: float = -8.0,
    programme_fade_out: float = 0.5,
    ffmpeg: str = "ffmpeg",
    measure_fn=measure,
) -> MixDecision:
    """Measure what there is, and decide every level from it.

    ``measure_fn`` is injectable so the arithmetic can be tested against known
    measurements without decoding anything.
    """
    decision = MixDecision(
        target_lufs=float(target_lufs),
        peak_ceiling_db=float(peak_ceiling_db),
        speech_ranges=[[float(a), float(b)] for a, b in speech_ranges],
        programme_fade_out=float(programme_fade_out),
    )

    # -- dialogue --------------------------------------------------------
    # The loudest measured dialogue source is the reference. Using the loudest
    # rather than the mean means the mix is set by the material the viewer will
    # actually hear the voice in, not dragged down by a quiet clip.
    dialogue: Optional[LevelMeasurement] = None
    for path in dialogue_sources:
        entry = measure_fn(path, role="dialogue", ffmpeg=ffmpeg)
        decision.measurements.append(entry)
        if entry.measured and (dialogue is None or entry.lufs > dialogue.lufs):
            dialogue = entry

    if dialogue is None:
        decision.gains["dialogue"] = 0.0
        decision.warnings.append(
            "Dialogue could not be measured, so its level is unchanged and "
            "every relative level below is a default rather than a decision."
        )
        reference_lufs = float(target_lufs)
    else:
        gain, clipped = gain_to_reach(
            dialogue, target_lufs, peak_ceiling_db=peak_ceiling_db
        )
        decision.gains["dialogue"] = gain
        decision.clipping_prevented = decision.clipping_prevented or clipped
        if clipped:
            decision.notes.append(
                f"Dialogue was held at {gain:+.1f} dB rather than the "
                f"{target_lufs - dialogue.lufs:+.1f} dB the loudness target "
                f"asked for, because its true peak is {dialogue.peak_db:.1f} "
                f"dBTP and the ceiling is {peak_ceiling_db:.1f}."
            )
        else:
            decision.notes.append(
                f"Dialogue measured {dialogue.lufs:.1f} LUFS "
                f"(range {dialogue.lra:.1f} LU, peak {dialogue.peak_db:.1f} "
                f"dBTP); {gain:+.1f} dB brings it to {target_lufs:.1f} LUFS."
            )
        # After the dialogue gain, the voice sits at the target by definition.
        reference_lufs = float(target_lufs)

    # -- music -----------------------------------------------------------
    music_target = reference_lufs + float(music_under_dialogue_db)
    if music_path:
        entry = measure_fn(music_path, role="music", ffmpeg=ffmpeg)
        decision.measurements.append(entry)
        if entry.measured:
            gain, clipped = gain_to_reach(
                entry, music_target, peak_ceiling_db=peak_ceiling_db
            )
            decision.gains["music"] = gain
            decision.clipping_prevented = decision.clipping_prevented or clipped
            decision.notes.append(
                f"Music measured {entry.lufs:.1f} LUFS; {gain:+.1f} dB puts it "
                f"{abs(music_under_dialogue_db):.0f} dB under the dialogue."
            )
        else:
            decision.gains["music"] = float(music_under_dialogue_db)
            decision.warnings.append(
                f"Music could not be measured ({entry.error}); using the "
                f"default {music_under_dialogue_db:+.1f} dB."
            )

    # -- effects ---------------------------------------------------------
    sfx_target = reference_lufs + float(sfx_under_dialogue_db)
    measured_sfx = []
    for path in sfx_paths:
        entry = measure_fn(path, role="sfx", ffmpeg=ffmpeg)
        decision.measurements.append(entry)
        if entry.measured:
            measured_sfx.append(entry)
    if sfx_paths:
        if measured_sfx:
            # One gain for the whole track, from the loudest effect: they share
            # a track, so a per-file gain would need per-clip targeting and the
            # loudest one is what sets whether the track is too hot.
            loudest = max(measured_sfx, key=lambda m: m.lufs)
            gain, clipped = gain_to_reach(
                loudest, sfx_target, peak_ceiling_db=peak_ceiling_db
            )
            decision.gains["sfx"] = gain
            decision.clipping_prevented = decision.clipping_prevented or clipped
            decision.notes.append(
                f"{len(measured_sfx)} effect(s) measured; the loudest is "
                f"{loudest.lufs:.1f} LUFS and {gain:+.1f} dB puts it "
                f"{abs(sfx_under_dialogue_db):.0f} dB under the dialogue."
            )
        else:
            decision.gains["sfx"] = float(sfx_under_dialogue_db)
            decision.warnings.append(
                "No effect could be measured; using the default "
                f"{sfx_under_dialogue_db:+.1f} dB."
            )

    expected = {"dialogue"}
    if music_path:
        expected.add("music")
    if sfx_paths:
        expected.add("sfx")
    decision.fully_measured = all(
        decision.measurement_for(role) is not None for role in expected
    )
    return decision


def mix_ops(decision: MixDecision, layout, *, sfx_clip_count: int = 0,
            cut_duration: float = 0.0, tail_at: float = 0.0) -> list[dict]:
    """The mix, as catalog operations.

    Dialogue and programme gain land on the whole track via an ``all``
    selector. Roles with a zero gain emit nothing -- an operation that sets a
    level to the level it already has is noise in the operation log and one
    more thing to fail.

    The closing fade targets the last clip **by time** rather than by index.
    There is no "last" selector and a negative index is refused by the
    validator, so a time is the only way to name it.

    ``tail_at`` should be the *midpoint* of the final clip, not the end of the
    cut. The plan's idea of the cut length and Premiere's disagree by a frame
    or two -- clip durations are rounded to the sequence's frame rate, which is
    itself whatever the source footage turned out to be -- so a time computed
    from the plan's total lands just past the last frame and resolves to
    nothing. A midpoint has half a clip of slack on either side.
    """
    ops: list[dict] = []

    dialogue_gain = decision.gain_for("dialogue")
    if abs(dialogue_gain) >= 0.1:
        ops.append({
            "op": "audio.gain",
            "clip": {"track": layout.dialogue, "all": True},
            "db": dialogue_gain,
            "note": f"dialogue to {decision.target_lufs:.1f} LUFS",
        })

    sfx_gain = decision.gain_for("sfx")
    if sfx_clip_count and abs(sfx_gain) >= 0.1:
        ops.append({
            "op": "audio.gain",
            "clip": {"track": layout.sfx, "all": True},
            "db": sfx_gain,
            "note": "effects, measured against the dialogue",
        })

    at = tail_at if tail_at > 0 else max(0.0, cut_duration * 0.9)
    if decision.programme_fade_out > 0 and at > 0:
        ops.append({
            "op": "audio.fade",
            "clip": {"track": layout.dialogue, "at": round(at, 3)},
            "out": round(decision.programme_fade_out, 3),
            "note": "so the episode does not end on a hard audio cut",
        })
    return ops

"""Choosing a music bed, placing it, trimming it, and fading it.

One bed, done properly, rather than a scoring system that never runs. The pass
answers four questions and stops:

1. **Which track?** From the music the user has actually provided -- the asset
   library's music category, or a folder named explicitly. Nothing is
   downloaded, nothing is generated, and a library with no music produces a
   decision that says so rather than a placeholder.
2. **Where does it go?** Under the episode, starting after any cold open and
   ending at the last frame of the cut.
3. **How long is it?** Trimmed to the range. If the file is shorter than the
   range it is repeated, up to a limit -- past that the track is simply the
   wrong length for the job and the decision says so.
4. **How loud?** Not decided here. The level comes from
   :mod:`editing.conform.mix`, which measured it.

**Beat awareness** is deliberately modest: the onset detector below finds the
strongest regular pulse in the first thirty seconds and, if it finds one, nudges
the bed's start so a downbeat lands on the first frame of the range rather than
somewhere in the middle of it. It is not a beat-matched edit and does not claim
to be; it is the difference between music that starts and music that lurches.
"""
from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Optional, Sequence

from editing import ffmpeg as ff
from editing.conform.schema import MusicDecision, decision_id_for
from editing.errors import ToolMissingError

logger = logging.getLogger("nova.editing.conform.music")

#: Extensions treated as music.
MUSIC_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".aiff")

#: A bed under this long is not worth placing: it is a sting, not a bed.
MIN_BED_SECONDS = 8.0

#: How many times a bed may be repeated to cover the range. Past this the
#: track is the wrong length for the job.
MAX_LOOPS = 8

#: How much of the file to analyse for a tempo. The intro is where a bed's
#: pulse is clearest, and scanning a six-minute track to place one clip is
#: not a good trade.
BEAT_SCAN_SECONDS = 30.0

#: Plausible musical tempos. Outside this the detector has found something
#: that is not a beat.
MIN_BPM = 60.0
MAX_BPM = 180.0

_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


def find_music(root: str | Path, *, recursive: bool = True) -> list[Path]:
    """Every music file under ``root``, sorted for a stable choice."""
    base = Path(root) if root else None
    if base is None or not base.is_dir():
        return []
    walker = base.rglob("*") if recursive else base.glob("*")
    return sorted(
        path for path in walker
        if path.is_file() and path.suffix.lower() in MUSIC_EXTENSIONS
    )


def _duration(path: Path, ffprobe: str = "ffprobe") -> float:
    try:
        return float(ff.probe(path, ffprobe=ffprobe).get("duration") or 0.0)
    except Exception:  # noqa: BLE001 - an unreadable file is simply not usable
        return 0.0


def detect_tempo(
    path: str | Path,
    *,
    scan: float = BEAT_SCAN_SECONDS,
    ffmpeg: str = "ffmpeg",
    timeout: float = 120.0,
) -> tuple:
    """A rough ``(bpm, first_onset_seconds)``, or ``(0.0, 0.0)``.

    Method: run ``silencedetect`` at a high threshold over a low-passed copy.
    On music with any percussion at all, the gaps between "not silent" events
    fall on beats, and the most common gap is the beat period. This is coarse
    and it knows it -- it is used only to nudge a start time by a fraction of a
    second, and it refuses to answer unless the pulse is regular enough to have
    a clear modal interval.
    """
    target = Path(path)
    if not target.is_file():
        return 0.0, 0.0
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info",
        "-t", f"{float(scan):.2f}", "-i", str(target),
        "-map", "0:a:0?",
        # Low-pass to the kick/snare band, then look for short quiet gaps.
        "-af", "lowpass=f=200,silencedetect=noise=-24dB:d=0.06",
        "-vn", "-sn", "-dn", "-f", "null", "-",
    ]
    try:
        result = ff._run(command, timeout=timeout)
    except ToolMissingError:
        return 0.0, 0.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("Tempo scan failed for %s: %s", target, exc)
        return 0.0, 0.0

    onsets = [float(v) for v in _SILENCE_END.findall(result.stderr or "")]
    if len(onsets) < 6:
        return 0.0, 0.0

    gaps = [b - a for a, b in zip(onsets, onsets[1:]) if 0.2 <= (b - a) <= 1.2]
    if len(gaps) < 4:
        return 0.0, 0.0

    # Modal gap, to 20 ms. A regular pulse concentrates in one bucket; a
    # scattered one does not, and the check below refuses the scattered case.
    buckets: dict = {}
    for gap in gaps:
        key = round(gap / 0.02)
        buckets[key] = buckets.get(key, 0) + 1
    key, count = max(buckets.items(), key=lambda item: item[1])
    if count < max(3, len(gaps) // 4):
        return 0.0, 0.0

    period = key * 0.02
    if period <= 0:
        return 0.0, 0.0
    bpm = 60.0 / period
    while bpm < MIN_BPM:
        bpm *= 2.0
    while bpm > MAX_BPM:
        bpm /= 2.0
    if not (MIN_BPM <= bpm <= MAX_BPM):
        return 0.0, 0.0
    return round(bpm, 1), round(onsets[0], 3)


def choose(
    candidates: Sequence[Path],
    *,
    wanted_seconds: float,
    ffprobe: str = "ffprobe",
) -> tuple:
    """The best-fitting track, as ``(path, duration)``.

    "Best" means the shortest track that still covers the range without
    looping, because a bed that loops is always more noticeable than one that
    does not. Failing that, the longest track available -- fewer loops.
    """
    measured = [(path, _duration(path, ffprobe)) for path in candidates]
    usable = [(p, d) for p, d in measured if d >= MIN_BED_SECONDS]
    if not usable:
        return None, 0.0
    covering = [(p, d) for p, d in usable if d >= wanted_seconds]
    if covering:
        return min(covering, key=lambda item: item[1])
    return max(usable, key=lambda item: item[1])


def plan_bed(
    *,
    library_root: str = "",
    candidates: Optional[Sequence[Path]] = None,
    cut_duration: float,
    start: float = 0.0,
    track: str = "A3",
    gain_db: float = -18.0,
    fade_in: float = 1.5,
    fade_out: float = 2.5,
    speech_ranges: Sequence[Sequence[float]] = (),
    beat_align: bool = True,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
) -> MusicDecision:
    """Decide the one music bed for this episode.

    Returns a decision either way. A refusal carries ``reject_reason``, which
    is the only honest thing to do when there is no music to choose from.
    """
    end = max(0.0, float(cut_duration))
    start = max(0.0, min(float(start), end))
    wanted = end - start
    decision = MusicDecision(
        start=round(start, 3), end=round(end, 3), track=track,
        gain_db=float(gain_db), fade_in=float(fade_in),
        fade_out=float(fade_out),
        ducks_under=[[float(a), float(b)] for a, b in speech_ranges],
    )

    if wanted < MIN_BED_SECONDS:
        decision.reject_reason = "cut_too_short"
        decision.reason = (
            f"The cut is {wanted:.1f}s long; a bed under {MIN_BED_SECONDS:.0f}s "
            "is a sting, not music."
        )
        return decision

    pool = list(candidates) if candidates is not None else find_music(library_root)
    if not pool:
        decision.reject_reason = "no_music_available"
        decision.reason = (
            "No music was available to choose from. Point --music-library at a "
            "folder of tracks, or add music to the asset library."
            if not library_root else
            f"No usable music files were found under {library_root}."
        )
        return decision

    chosen, source_duration = choose(pool, wanted_seconds=wanted, ffprobe=ffprobe)
    if chosen is None:
        decision.reject_reason = "all_tracks_too_short"
        decision.reason = (
            f"Every candidate is under {MIN_BED_SECONDS:.0f}s, which is too "
            "short to use as a bed."
        )
        return decision

    decision.asset_path = str(chosen)
    decision.asset_name = chosen.name
    decision.asset_id = decision_id_for("music", start, chosen.name)
    decision.measured = {"source_duration": round(source_duration, 3)}

    # -- beat alignment ---------------------------------------------------
    source_in = 0.0
    if beat_align:
        bpm, first_onset = detect_tempo(chosen, ffmpeg=ffmpeg)
        if bpm:
            decision.bpm = bpm
            decision.beat_offset = first_onset
            # Start the file at its first onset rather than at whatever silence
            # the encoder left at the head, so the bed begins on a beat.
            if 0.0 < first_onset < 2.0:
                source_in = first_onset
                decision.beat_aligned = True
                decision.evidence.append(
                    f"detected {bpm:.0f} BPM; started the file at its first "
                    f"onset ({first_onset:.2f}s) so the bed begins on a beat"
                )

    usable = max(0.0, source_duration - source_in)
    if usable <= 0:
        decision.reject_reason = "no_usable_audio"
        decision.reason = "The chosen track has no usable audio after its head."
        return decision

    loops = max(1, math.ceil(wanted / usable))
    if loops > MAX_LOOPS:
        decision.reject_reason = "would_loop_too_often"
        decision.reason = (
            f"{chosen.name} is {usable:.0f}s and the range is {wanted:.0f}s, "
            f"which would need {loops} repeats. Past {MAX_LOOPS} the track is "
            "simply the wrong length for the job."
        )
        return decision

    decision.loops = loops
    decision.source_in = round(source_in, 3)
    decision.source_out = round(min(source_duration, source_in + wanted), 3)
    decision.placed = True
    decision.reason = (
        f"{chosen.name} runs {source_duration:.0f}s and covers the "
        f"{wanted:.0f}s of cut"
        + (f" in {loops} passes" if loops > 1 else " in one pass")
        + f", at {gain_db:+.0f} dB under the dialogue."
    )
    decision.evidence.append(
        f"chosen from {len(pool)} candidate track(s)"
    )
    if loops > 1:
        decision.evidence.append(
            f"repeated {loops} times; the loop points are audible if the track "
            "does not end where it began"
        )
    return decision


def bed_ops(decision: MusicDecision, *, bin_name: str = "Nova Music") -> list[dict]:
    """The bed, as catalog operations: import, place, level, fade, duck.

    Order matters and is fixed here. The file has to be in the project before
    it can be placed; the level and the fades act on the clip that placing it
    created; the duck is last because it writes keyframes over the level the
    gain operation just set.
    """
    if not decision.placed or not decision.asset_path:
        return []

    ops: list[dict] = [{
        "op": "project.import",
        "paths": [decision.asset_path],
        "bin": bin_name,
        "note": f"music bed: {decision.asset_name}",
    }]

    # One clip per loop, laid end to end across the range.
    span = max(0.0, decision.source_out - decision.source_in)
    at = decision.start
    placed = 0
    while at < decision.end - 0.05 and placed < decision.loops:
        remaining = decision.end - at
        length = min(span, remaining) if span > 0 else remaining
        ops.append({
            "op": "clip.overwrite",
            "asset": decision.asset_path,
            "track": decision.track,
            "time": round(at, 3),
            "in": round(decision.source_in, 3),
            "out": round(decision.source_in + length, 3),
            "note": (f"music bed {placed + 1}/{decision.loops}"
                     if decision.loops > 1 else "music bed"),
        })
        at += length
        placed += 1

    ops.append({
        "op": "audio.gain",
        "clip": {"track": decision.track, "all": True},
        "db": round(decision.gain_db, 2),
        "note": "measured against the dialogue",
    })
    if decision.fade_in > 0:
        ops.append({
            "op": "audio.fade",
            "clip": {"track": decision.track, "index": 0},
            "in": round(decision.fade_in, 3),
            "note": "music in",
        })
    if decision.fade_out > 0 and placed > 0:
        ops.append({
            "op": "audio.fade",
            "clip": {"track": decision.track, "index": placed - 1},
            "out": round(decision.fade_out, 3),
            "note": "music out",
        })
    if decision.ducks_under:
        ops.append({
            "op": "audio.duck",
            "clip": {"track": decision.track, "index": 0},
            # ``under`` takes objects, not pairs: the catalog names the
            # fields so a range can never be read back to front.
            "under": [{"start": round(a, 3), "end": round(b, 3)}
                      for a, b in decision.ducks_under],
            "duck_db": round(decision.duck_db, 2),
            "note": "under the dialogue",
        })
    return ops

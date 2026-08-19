"""Getting a loudness envelope and silence ranges out of a media file.

Two FFmpeg passes, and a pure parser for each so the parsing is testable
without FFmpeg installed:

``loudness_envelope``  ``astats`` over a downsampled mono copy, reset every N
                       samples, giving one RMS/peak reading per interval.
``silence_ranges``     ``silencedetect``, which finds quiet stretches far more
                       precisely than re-deriving them from a 0.25s envelope.

Both are read-only and both fail soft: a file with no audio track, or a codec
FFmpeg cannot decode, returns empty rather than raising, because the visual
half of the pipeline is still perfectly usable without audio. A *missing*
FFmpeg is the one hard error, since nothing else will work either.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from editing import ffmpeg as ff
from editing.audio.signal import SILENT_DB, LoudnessSample, Span
from editing.config import AudioConfig
from editing.errors import ToolMissingError

logger = logging.getLogger("nova.editing.audio.ffmpeg")

#: A whole-file audio scan. Generous: this decodes every audio sample, which on
#: a 40-minute recording takes a couple of minutes.
AUDIO_TIMEOUT = 3600.0

#: Sample rate the envelope is computed at. 8 kHz keeps speech energy intact
#: (the band that matters here is well under 4 kHz) and makes the decode cheap.
ENVELOPE_RATE = 8000

_PTS = re.compile(r"pts_time:([0-9.eE+-]+)")
_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[0-9.]+|-?inf)", re.I)
_PEAK = re.compile(r"lavfi\.astats\.Overall\.Peak_level=(-?[0-9.]+|-?inf)", re.I)

_SILENCE_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def _level(text: str) -> float:
    """Parse a dB reading, mapping FFmpeg's ``-inf`` onto a finite floor."""
    cleaned = str(text).strip().lower()
    if "inf" in cleaned:
        return SILENT_DB
    try:
        value = float(cleaned)
    except (TypeError, ValueError):
        return SILENT_DB
    if value != value:                      # NaN
        return SILENT_DB
    return max(SILENT_DB, min(0.0, value))


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------

def parse_astats_output(text: str) -> list[LoudnessSample]:
    """Parse ``ametadata=print`` output into loudness samples.

    The filter emits a frame header followed by that frame's metadata::

        frame:1    pts:2000    pts_time:0.25
        lavfi.astats.Overall.RMS_level=-23.456
        lavfi.astats.Overall.Peak_level=-3.210

    so both readings belong to the most recent timestamp. A frame missing its
    peak still yields a sample -- RMS is what nearly every detector uses, and
    dropping the reading entirely would punch a hole in the envelope.
    """
    samples: list[LoudnessSample] = []
    time: Optional[float] = None
    rms: Optional[float] = None
    peak: Optional[float] = None

    def flush() -> None:
        nonlocal time, rms, peak
        if time is not None and rms is not None:
            samples.append(LoudnessSample(
                time=time, rms_db=rms,
                peak_db=peak if peak is not None else SILENT_DB,
            ))
        rms = peak = None

    for line in text.splitlines():
        stamp = _PTS.search(line)
        if stamp:
            flush()
            try:
                time = float(stamp.group(1))
            except (TypeError, ValueError):
                time = None
            continue
        found = _RMS.search(line)
        if found:
            rms = _level(found.group(1))
            continue
        found = _PEAK.search(line)
        if found:
            peak = _level(found.group(1))

    flush()
    samples.sort(key=lambda sample: sample.time)
    return samples


def parse_silencedetect_output(text: str) -> list[Span]:
    """Parse ``silencedetect`` output into spans.

    FFmpeg emits ``silence_start`` and ``silence_end`` on separate lines. A
    trailing ``silence_start`` with no matching end means the file ends in
    silence; that span is left open (``end == start``) and closed by the caller,
    which is the only place that knows the duration.
    """
    spans: list[Span] = []
    pending: Optional[float] = None
    for line in text.splitlines():
        start = _SILENCE_START.search(line)
        if start:
            try:
                pending = max(0.0, float(start.group(1)))
            except (TypeError, ValueError):
                pending = None
            continue
        finish = _SILENCE_END.search(line)
        if finish and pending is not None:
            try:
                end = float(finish.group(1))
            except (TypeError, ValueError):
                continue
            if end > pending:
                spans.append(Span(start=pending, end=end, level_db=SILENT_DB))
            pending = None

    if pending is not None:
        spans.append(Span(start=pending, end=pending, level_db=SILENT_DB))
    return spans


def close_open_spans(spans: list[Span], duration: float) -> list[Span]:
    """Give a file-ending silence its end time."""
    out: list[Span] = []
    for span in spans:
        if span.end <= span.start and duration > span.start:
            out.append(Span(start=span.start, end=duration, level_db=span.level_db))
        elif span.end > span.start:
            out.append(span)
    return out


# ---------------------------------------------------------------------------
# FFmpeg passes
# ---------------------------------------------------------------------------

def has_audio_stream(path: str | Path, *, ffprobe: str = "ffprobe") -> bool:
    """Whether the file carries a decodable audio track."""
    try:
        return bool(ff.probe(path, ffprobe=ffprobe).get("has_audio"))
    except ToolMissingError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable file has no audio
        logger.debug("Could not probe audio of %s: %s", path, exc)
        return False


def loudness_envelope(
    path: str | Path,
    *,
    config: AudioConfig,
    ffmpeg: str = "ffmpeg",
    timeout: float = AUDIO_TIMEOUT,
) -> list[LoudnessSample]:
    """One RMS/peak reading per ``sample_interval`` across the whole file.

    Returns ``[]`` when the file has no audio or FFmpeg cannot read it -- both
    ordinary states that must not stop a run.
    """
    config = config.validated()
    block = max(1, int(round(ENVELOPE_RATE * config.sample_interval)))
    filters = (
        f"aresample={ENVELOPE_RATE},aformat=channel_layouts=mono,"
        f"asetnsamples=n={block},"
        f"astats=metadata=1:reset=1,"
        f"ametadata=print:file=-"
    )
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-map", "0:a:0?",
        "-af", filters,
        "-vn", "-sn", "-dn",
        "-f", "null", "-",
    ]

    try:
        result = ff._run(command, timeout=timeout)
    except ToolMissingError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Loudness scan of %s failed: %s", path, exc)
        return []

    if not result.stdout:
        logger.debug(
            "No loudness data for %s (rc=%s): %s",
            path, result.returncode, (result.stderr or "").strip()[:200],
        )
        return []
    return parse_astats_output(result.stdout)


def silence_ranges(
    path: str | Path,
    *,
    config: AudioConfig,
    duration: float = 0.0,
    ffmpeg: str = "ffmpeg",
    timeout: float = AUDIO_TIMEOUT,
) -> list[Span]:
    """Quiet stretches, from FFmpeg's own ``silencedetect``."""
    config = config.validated()
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info",
        "-i", str(path),
        "-map", "0:a:0?",
        "-af", f"silencedetect=n={config.silence_threshold_db:.1f}dB:"
               f"d={config.min_silence_seconds:.2f}",
        "-vn", "-sn", "-dn",
        "-f", "null", "-",
    ]

    try:
        result = ff._run(command, timeout=timeout)
    except ToolMissingError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Silence scan of %s failed: %s", path, exc)
        return []

    # silencedetect logs to stderr; stdout is checked too in case a future
    # FFmpeg moves it.
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    return close_open_spans(parse_silencedetect_output(text), duration)

"""Every subprocess this layer runs, in one place.

Three jobs: read a file's technical metadata, extract single frames at chosen
timestamps, and produce a cheap motion signal over a whole recording.

Nothing else in the package shells out. That keeps the rest of the code
testable without FFmpeg installed -- the sampling planner, normaliser and
aligner are all pure -- and means a missing binary produces one clear error
from one place instead of a stack trace from wherever it happened to be needed.

Timestamp accuracy note: ``-ss`` *before* ``-i`` is used deliberately. Modern
FFmpeg makes that seek both fast (jump to the preceding keyframe) and accurate
(decode forward to the requested time), which is exactly what sampling wants.
Putting it after ``-i`` would decode the whole file up to that point, turning a
40-minute recording into an overnight job.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from editing.errors import ToolMissingError, VisualError

logger = logging.getLogger("nova.editing.ffmpeg")

#: Long enough for a slow seek deep into a large file on a spinning disk.
FRAME_TIMEOUT = 120.0
PROBE_TIMEOUT = 120.0
#: A motion scan reads the whole file (keyframes only), so it gets its own,
#: much larger budget.
MOTION_TIMEOUT = 3600.0


@dataclass(frozen=True)
class MotionSample:
    """How much the picture changed at ``time``. ``score`` is 0..1."""

    time: float
    score: float


def have_tool(name: str) -> bool:
    """True when ``name`` is runnable (a bare command on PATH or a full path)."""
    if shutil.which(name):
        return True
    candidate = Path(name)
    return candidate.is_file()


def _run(command: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run a command, converting a missing binary into a typed error."""
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            timeout=timeout,
            check=False,
            # Frame paths and codec names can contain non-UTF-8 bytes on
            # Windows; replacing them beats crashing on a decode error.
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise ToolMissingError(
            f"'{command[0]}' is not installed or not on PATH",
            detail={"command": command[0]},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VisualError(
            f"'{command[0]}' timed out after {timeout:.0f}s",
            hint="The file may be on a slow or disconnected drive. Raise the "
                 "timeout or copy the footage locally.",
            detail={"command": " ".join(str(c) for c in command[:6])},
        ) from exc


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe(path: str | Path, *, ffprobe: str = "ffprobe") -> dict:
    """Technical metadata for one media file, already flattened.

    Returns the fields the rest of the layer needs rather than raw ffprobe
    JSON, so callers never have to know that ``r_frame_rate`` is a string
    fraction or that duration can live on either the stream or the format.
    """
    target = Path(path)
    command = [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(target),
    ]
    result = _run(command, timeout=PROBE_TIMEOUT)
    if result.returncode != 0:
        raise VisualError(
            f"ffprobe could not read {target.name}",
            hint="The file may be corrupt, still being written, or in a "
                 "container FFmpeg does not support.",
            detail={"stderr": (result.stderr or "")[-500:]},
        )
    try:
        raw = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VisualError(
            f"ffprobe returned unreadable output for {target.name}",
            detail={"reason": str(exc)},
        ) from exc

    return _flatten_probe(raw)


def _flatten_probe(raw: dict) -> dict:
    fmt = raw.get("format") or {}
    streams = raw.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _as_float(fmt.get("duration"))
    if duration <= 0 and video is not None:
        duration = _as_float(video.get("duration"))

    out = {
        "duration": duration,
        "container": str(fmt.get("format_name") or ""),
        "size_bytes": int(_as_float(fmt.get("size"))),
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "video_codec": "",
        "has_audio": audio is not None,
        "audio_codec": str(audio.get("codec_name") or "") if audio else "",
        "audio_channels": int(_as_float(audio.get("channels"))) if audio else 0,
    }
    if video is not None:
        out["width"] = int(_as_float(video.get("width")))
        out["height"] = int(_as_float(video.get("height")))
        out["video_codec"] = str(video.get("codec_name") or "")
        out["fps"] = _parse_fraction(
            video.get("avg_frame_rate") or video.get("r_frame_rate")
        )
        # Rotated phone/console capture reports pre-rotation dimensions; the
        # editor cares about how it will actually display.
        if _rotation(video) in (90, 270):
            out["width"], out["height"] = out["height"], out["width"]
    return out


def _rotation(stream: dict) -> int:
    tags = stream.get("tags") or {}
    rotate = _as_float(tags.get("rotate"))
    if not rotate:
        for entry in stream.get("side_data_list") or []:
            if "rotation" in entry:
                rotate = _as_float(entry.get("rotation"))
                break
    return int(abs(rotate)) % 360


def _as_float(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _parse_fraction(value) -> float:
    """``"60000/1001"`` -> 59.94. Returns 0.0 for ffprobe's ``0/0``."""
    if not value:
        return 0.0
    text = str(value)
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        den = _as_float(denominator)
        return round(_as_float(numerator) / den, 6) if den else 0.0
    return _as_float(text)


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def extract_frame(
    path: str | Path,
    time: float,
    out_path: str | Path,
    *,
    width: int = 768,
    quality: int = 4,
    ffmpeg: str = "ffmpeg",
) -> Optional[Path]:
    """Write one JPEG at ``time``. Returns None when no frame exists there.

    A None return is normal rather than exceptional: the last window of a file
    frequently asks for a timestamp a few milliseconds past the final frame,
    and one missing frame should not abort a 40-minute analysis.
    """
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, float(time)):.3f}",
        "-i", str(path),
        "-frames:v", "1",
        # -2 keeps the height even, which JPEG encoders with chroma
        # subsampling require.
        "-vf", f"scale={int(width)}:-2:flags=bicubic",
        "-q:v", str(int(quality)),
        "-an", "-sn", "-dn",
        "-y", str(target),
    ]
    result = _run(command, timeout=FRAME_TIMEOUT)
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        logger.debug(
            "No frame at %.3fs in %s: %s", time, path, (result.stderr or "").strip()[:200]
        )
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        return None
    return target


def extract_frames(
    path: str | Path,
    times: Iterable[float],
    out_dir: str | Path,
    *,
    width: int = 768,
    quality: int = 4,
    ffmpeg: str = "ffmpeg",
    prefix: str = "f",
) -> list[tuple[float, Path]]:
    """Extract several frames, keeping each one paired with its timestamp.

    Timestamps that yield no frame are dropped, so the result can be shorter
    than ``times`` -- callers must read the returned times rather than assume
    positional correspondence.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    out: list[tuple[float, Path]] = []
    for index, time in enumerate(times):
        frame_path = directory / f"{prefix}_{index:03d}_{max(0.0, time):09.3f}.jpg"
        written = extract_frame(
            path, time, frame_path,
            width=width, quality=quality, ffmpeg=ffmpeg,
        )
        if written is not None:
            out.append((float(time), written))
    return out


# ---------------------------------------------------------------------------
# Motion signal
# ---------------------------------------------------------------------------

_SCENE_RE = re.compile(r"lavfi\.scene_score=([0-9.eE+-]+)")
_PTS_RE = re.compile(r"pts_time:([0-9.eE+-]+)")


def motion_samples(
    path: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    keyframes_only: bool = True,
    interval: float = 2.0,
    timeout: float = MOTION_TIMEOUT,
) -> list[MotionSample]:
    """A coarse "how much is changing" signal across the whole file.

    Uses FFmpeg's own ``scene`` metric, computed on a 160px-wide copy. With
    ``keyframes_only`` the decoder is told to skip non-keyframes, which makes a
    40-minute 4K recording scannable in a couple of minutes instead of an hour;
    for typical game-capture (a keyframe every 1-2s) that is finer than the
    sampling windows anyway.

    Returns ``[]`` rather than raising if the scan fails. Motion data is an
    optimisation -- without it the sampler falls back to uniform windows, which
    is worse but still correct. A missing FFmpeg is the one exception: that is
    a setup problem the user needs told about.
    """
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info"]
    if keyframes_only:
        command += ["-skip_frame", "nokey"]
    filters = f"scale=160:-2,select='gt(scene,0)',metadata=print:file=-"
    if not keyframes_only and interval > 0:
        filters = f"fps=1/{max(0.05, float(interval)):.4f}," + filters
    command += [
        "-i", str(path),
        "-an", "-sn", "-dn",
        "-vf", filters,
        "-f", "null", "-",
    ]

    try:
        result = _run(command, timeout=timeout)
    except ToolMissingError:
        raise
    except VisualError as exc:
        logger.warning("Motion scan of %s failed: %s", path, exc)
        return []

    if result.returncode != 0 and not result.stdout:
        logger.warning(
            "Motion scan of %s returned %s; falling back to uniform sampling",
            path, result.returncode,
        )
        return []
    return parse_motion_output(result.stdout or "")


def parse_motion_output(text: str) -> list[MotionSample]:
    """Parse ``metadata=print`` output into samples.

    The filter emits a frame header line followed by its metadata lines::

        frame:1    pts:2048    pts_time:2.048
        lavfi.scene_score=0.114253

    so a score belongs to the most recent timestamp seen. Kept separate from
    the subprocess call above purely so it can be unit tested on a captured
    fixture.
    """
    samples: list[MotionSample] = []
    current: Optional[float] = None
    for line in text.splitlines():
        pts = _PTS_RE.search(line)
        if pts:
            current = _as_float(pts.group(1), -1.0)
            continue
        score = _SCENE_RE.search(line)
        if score is not None and current is not None and current >= 0:
            samples.append(MotionSample(
                time=current,
                score=max(0.0, min(1.0, _as_float(score.group(1)))),
            ))
            current = None
    samples.sort(key=lambda sample: sample.time)
    return samples

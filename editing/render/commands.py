"""Every FFmpeg command this package runs, built as pure data.

Nothing here shells out. A command is a list of strings, built from a segment
and a config, and returned -- which is what makes the whole render strategy
testable on a machine with no FFmpeg, and what makes ``ffmpeg_commands.json``
an honest record rather than a reconstruction.

## The strategy, and why it is this one

Two ways exist to render a cut with FFmpeg:

1. **One filtergraph.** All sources as inputs, ``trim``/``setpts``/``concat``
   filters, one pass, one encode.
2. **Encode each segment, then join.**

This package does (2), and the reason is game capture. A folder of Minecraft
recordings routinely mixes 1080p60 and 1440p60, OBS files with and without a
microphone track, and clips whose audio starts a few hundred milliseconds
after the video. The one-filtergraph version of that fails with a message
about stream layouts, forty seconds into a decode, with nothing usable on
disk. The per-segment version normalises every clip to identical streams
first, so the join is a stream copy that cannot fail on mismatch, and a
failure names the clip that caused it.

It is slower. It also finishes.

## Three details that are load-bearing

* **``-ss`` and ``-t`` go before ``-i``.** Modern FFmpeg makes that seek fast
  *and* accurate, and it means a 30-second segment from the middle of a 40-
  minute file costs 30 seconds of decoding rather than twenty minutes of it.
* **Every segment gets an audio stream, even silent ones.** A clip with no
  microphone track gets ``anullsrc``. Without this the concat demuxer refuses
  the join outright, which is the single most common way a naive version of
  this package fails.
* **``aresample=async=1:first_pts=0``** on every audio chain. Capture software
  frequently starts the audio stream slightly after the video; without this
  the offset accumulates across a hundred segments into visible desync.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from editing.render.schema import (
    ATEMPO_MAX, ATEMPO_MIN, RenderConfig, RenderSegment,
)

#: Quiet, non-interactive, and never prompting to overwrite. On every call.
BASE_FLAGS = ("-nostdin", "-hide_banner", "-loglevel", "error", "-y")

#: Channel layout names FFmpeg understands, by channel count.
CHANNEL_LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}


def _num(value: float, places: int = 6) -> str:
    """A number FFmpeg will parse the same way on every machine.

    ``repr`` would emit scientific notation for small values and the locale
    could put a comma in it; this cannot.
    """
    text = f"{float(value):.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def layout_for(channels: int) -> str:
    return CHANNEL_LAYOUTS.get(int(channels), "stereo")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def atempo_chain(speed: float) -> list[str]:
    """``atempo`` filters whose product is ``speed``.

    ``atempo`` is only defined for 0.5x-2.0x, so anything outside that is
    reached by chaining -- 4x is ``atempo=2.0,atempo=2.0``. Chaining is exact
    (the factors multiply) and each stage is a real time-stretch, so the pitch
    stays where it was, which is the entire point of using ``atempo`` rather
    than resampling.
    """
    rate = float(speed)
    if rate <= 0 or abs(rate - 1.0) < 1e-6:
        return []
    factors: list[float] = []
    remaining = rate
    # Guarded rather than while-True: a NaN or a denormal must not spin here.
    for _ in range(16):
        if ATEMPO_MIN - 1e-9 <= remaining <= ATEMPO_MAX + 1e-9:
            factors.append(remaining)
            break
        if remaining > ATEMPO_MAX:
            factors.append(ATEMPO_MAX)
            remaining /= ATEMPO_MAX
        else:
            factors.append(ATEMPO_MIN)
            remaining /= ATEMPO_MIN
    else:  # pragma: no cover - unreachable for any speed this package allows
        return []
    return [f"atempo={_num(factor)}" for factor in factors]


def scale_filters(config: RenderConfig) -> list[str]:
    """Fit any source into the output frame, in the configured way.

    ``setsar=1`` is not optional. Capture from consoles and phones frequently
    carries a non-square pixel aspect; without resetting it the padded frame
    is geometrically correct and displays stretched, which reads as a broken
    render.
    """
    if not config.scales:
        return []
    width, height = config.width, config.height
    if config.scale_mode == "stretch":
        return [f"scale={width}:{height}", "setsar=1"]
    if config.scale_mode == "crop":
        return [
            f"scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}",
            "setsar=1",
        ]
    return [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "setsar=1",
    ]


def video_filters(segment: RenderSegment, config: RenderConfig) -> list[str]:
    """The video chain for one segment, in application order.

    Speed first: ``setpts`` rewrites timestamps, and doing it before the
    scaler means the scaler runs on the frames that survive rather than on
    frames that are about to be discarded by ``fps``.
    """
    filters: list[str] = []
    if segment.has_speed_change:
        filters.append(f"setpts={_num(1.0 / segment.speed)}*PTS")
    filters.extend(scale_filters(config))
    if config.fps > 0:
        filters.append(f"fps={_num(config.fps)}")
    return filters


def audio_filters(segment: RenderSegment, config: RenderConfig) -> list[str]:
    """The audio chain for one segment.

    ``aresample`` is last and is always present: it is what pads or trims the
    stream to start exactly with the video, and a segment whose audio starts
    40ms late is otherwise 40ms late for the rest of the render.
    """
    filters: list[str] = []
    if segment.has_speed_change:
        filters.extend(atempo_chain(segment.speed))
    filters.append(
        f"aformat=sample_fmts=fltp:sample_rates={config.sample_rate}"
        f":channel_layouts={layout_for(config.audio_channels)}"
    )
    filters.append("aresample=async=1:first_pts=0")
    return filters


# ---------------------------------------------------------------------------
# Encoder arguments
# ---------------------------------------------------------------------------

def video_encoder_args(config: RenderConfig) -> list[str]:
    """Codec, quality and preset, in whichever dialect this encoder speaks."""
    args = ["-c:v", config.resolved_encoder]
    preset = config.encoder_preset
    if preset:
        args += ["-preset", preset]
    args += [config.quality_flag, str(config.crf)]
    args += ["-pix_fmt", config.pixel_format]
    if config.threads:
        args += ["-threads", str(config.threads)]
    return args


def audio_encoder_args(config: RenderConfig) -> list[str]:
    args = ["-c:a", config.audio_encoder]
    if config.audio_encoder != "pcm_s16le":
        args += ["-b:a", config.audio_bitrate]
    args += ["-ar", str(config.sample_rate), "-ac", str(config.audio_channels)]
    return args


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def segment_command(
    segment: RenderSegment,
    out_path: str | Path,
    config: RenderConfig,
    *,
    source_has_audio: bool = True,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Encode one segment to a normalised intermediate file.

    The output of this is deliberately uniform across every segment in a job --
    same codec, size, frame rate, sample rate and channel count -- because the
    concat that follows is a stream copy and stream copy has no way to
    reconcile a difference.
    """
    duration = segment.source_duration
    silent = not (source_has_audio and segment.audio_enabled
                  and config.include_audio)

    command = [ffmpeg, *BASE_FLAGS]
    # Input options, before -i: a seek here is a seek, not a decode-and-skip.
    command += ["-ss", _num(segment.source_in), "-t", _num(duration),
                "-i", str(segment.source_path)]

    if silent:
        # A silent track rather than no track. The concat demuxer refuses to
        # join files whose stream layouts differ, so one clip recorded without
        # a microphone would otherwise break the whole render.
        command += [
            "-f", "lavfi",
            "-t", _num(segment.duration or duration),
            "-i", f"anullsrc=channel_layout={layout_for(config.audio_channels)}"
                  f":sample_rate={config.sample_rate}",
        ]

    video = video_filters(segment, config)
    if video:
        command += ["-vf", ",".join(video)]

    if silent:
        command += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        audio = audio_filters(segment, config)
        if audio:
            command += ["-af", ",".join(audio)]
        command += ["-map", "0:v:0", "-map", "0:a:0"]

    command += video_encoder_args(config)
    command += audio_encoder_args(config)
    # Timestamps rebased per segment; without this a clip taken from 22:14 into
    # a recording carries that offset into the concat and the join stutters.
    command += ["-video_track_timescale", "90000", "-avoid_negative_ts",
                "make_zero", "-fflags", "+genpts"]
    command += [str(out_path)]
    return command


def concat_list_text(paths: Sequence[str | Path]) -> str:
    """The concat demuxer's list file.

    Single quotes are escaped the way the demuxer wants (``'\\''``), because
    Windows paths with apostrophes in a username are real and produce a
    baffling parse error otherwise.
    """
    lines = []
    for path in paths:
        text = str(path).replace("'", "'\\''")
        lines.append(f"file '{text}'")
    return "\n".join(lines) + "\n"


def concat_command(
    list_path: str | Path,
    out_path: str | Path,
    config: RenderConfig,
    *,
    reencode: bool = False,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Join the normalised segments into the finished proxy.

    ``-c copy`` is the fast path and is what should always happen: the
    segments were encoded to be joinable. ``reencode=True`` is the fallback
    for when it does not, which is rare and worth a warning rather than a
    silent second encode.
    """
    command = [
        ffmpeg, *BASE_FLAGS,
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
    ]
    if reencode:
        command += video_encoder_args(config)
        command += audio_encoder_args(config)
    else:
        command += ["-c", "copy"]
    if config.container == "mp4":
        # Puts the index at the front so the file starts playing before it has
        # finished downloading -- and, more usefully here, so a half-copied
        # file still opens in a player.
        command += ["-movflags", "+faststart"]
    command += [str(out_path)]
    return command


def probe_command(path: str | Path, *, ffprobe: str = "ffprobe") -> list[str]:
    """Read back what was actually produced.

    The render is verified against the plan rather than assumed to match it:
    "the encoder exited 0" and "the file is the length the cut says" are
    different claims, and only the second is worth putting in a report.
    """
    return [
        ffprobe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]


def render_command_line(command: Sequence[str]) -> str:
    """One command as a line a person could paste into a shell."""
    parts = []
    for token in command:
        text = str(token)
        parts.append(f'"{text}"' if " " in text and not text.startswith('"')
                     else text)
    return " ".join(parts)

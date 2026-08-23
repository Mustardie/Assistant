"""What a render is, as data.

## The shape

``RoughCutPlan`` -> ``RenderSegment[]`` -> FFmpeg commands -> ``RenderResult``.

A ``RenderSegment`` is the whole contract between the editing layer and
FFmpeg: one source range, its place on the timeline, its speed, and whether
its audio is wanted. Everything the renderer needs is on it, and everything
that put it there -- the placement it came from, the recommendations behind
that placement -- rides along so a moment in the finished proxy can be traced
back to the evidence that kept it.

## Four rules

* **A render is never faked.** ``RenderResult.rendered`` is True only when
  FFmpeg actually produced a video file. The mock backend writes a placeholder
  and stamps ``mock`` on the result, the report, the review notes and the
  artifact list -- the same rule Session 10A applies to transcripts, for the
  same reason: a fake artifact that reads as real is worse than no artifact.
* **The source is never touched.** Every byte this package writes lands under
  the job folder. A person pointing it at irreplaceable captures gets no new
  files beside them.
* **What FFmpeg cannot represent becomes a warning, not a failure.** A rough
  cut carries markers, captions, effects and Premiere operations that have no
  meaning in a flat proxy. The renderer says so and renders the cut anyway --
  a watchable approximation now beats a perfect render never.
* **Reliability beats cleverness.** Where two FFmpeg strategies exist, this
  package takes the one that survives mixed resolutions, mixed frame rates and
  missing audio streams, even when it is slower.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from editing.schema import (
    _slug, as_bool, as_float, as_str_list, short_hash,
)

#: What can actually run a render. ``mock`` produces a placeholder file and is
#: never quiet about it -- it exists for tests and for exercising the pipeline
#: on a machine with no FFmpeg.
BACKENDS = ("ffmpeg", "mock")

#: Quality presets, cheapest first. These set CRF and encoder preset together,
#: because the pair is what actually decides "how long does this take".
QUALITIES = ("draft", "proxy", "preview", "high")

#: (crf, encoder preset) per quality. Opinions, meant to be edited. ``proxy``
#: is the default: on a laptop CPU it renders a ten-minute cut in about a
#: minute and looks fine at 720p on a second monitor.
QUALITY_SETTINGS = {
    "draft":   (32, "ultrafast"),
    "proxy":   (28, "veryfast"),
    "preview": (23, "fast"),
    "high":    (18, "medium"),
}

#: Video encoders this package knows how to drive. ``auto`` resolves to
#: ``libx264`` unless a hardware encoder was asked for by name -- hardware
#: encoding is faster and lower quality per bit, which is the right trade for
#: a proxy but not a decision to make on somebody's behalf.
VIDEO_ENCODERS = (
    "auto", "libx264", "libx265",
    "h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox",
)

#: Encoders that do not understand ``-crf`` and the flag they use instead.
#: Getting this wrong is the classic hardware-encoding failure: FFmpeg accepts
#: ``-crf`` on nvenc, ignores it, and silently produces an enormous file.
QUALITY_FLAG = {
    "h264_nvenc": "-cq",
    "hevc_nvenc": "-cq",
    "h264_qsv": "-global_quality",
    "h264_amf": "-qp_i",
    "h264_videotoolbox": "-q:v",
}

#: Encoders whose ``-preset`` values are not x264's.
HARDWARE_PRESETS = {
    "h264_nvenc": "p4",
    "hevc_nvenc": "p4",
    "h264_qsv": "medium",
}

AUDIO_ENCODERS = ("aac", "libmp3lame", "pcm_s16le")

#: How a segment whose aspect ratio differs from the output is handled.
#: ``pad`` letterboxes, ``crop`` fills and loses edges, ``stretch`` distorts.
#: ``pad`` is the default because a proxy is for judging pacing, and cropping
#: a 4:3 capture would hide the thing being judged.
SCALE_MODES = ("pad", "crop", "stretch")

#: What a job can be.
#:
#: ``rendered`` produced a real video, ``cached`` reused one, ``planned`` built
#: every command and deliberately ran none of them, and ``mocked`` ran the mock
#: runner -- which completes, writes a placeholder, and produces no video at
#: all. ``mocked`` is its own status rather than ``rendered`` with a flag
#: because the question "did this work" and the question "is there something
#: to watch" have different answers for it, and one field cannot say both.
JOB_STATUSES = ("pending", "running", "rendered", "cached", "planned",
                "mocked", "failed")

#: Where a render can go wrong. Each has a different fix, which is why they
#: are separate rather than one "error".
FAILURE_STAGES = (
    "config",           # the settings themselves are unusable
    "no_plan",          # there is no rough cut to render
    "empty_plan",       # the plan has no placements
    "missing_ffmpeg",   # FFmpeg is not installed
    "missing_source",   # a source file the plan names is not there
    "convert",          # the plan could not become segments
    "encode_segment",   # FFmpeg failed on one segment
    "concat",           # the segments would not join
    "probe",            # the finished file could not be verified
    "write",            # an artifact could not be saved
    "unknown",
)

#: What a rough cut carries that a flat proxy cannot represent. Each becomes a
#: warning rather than a refusal.
UNSUPPORTED_FEATURES = {
    "marker.add": "sequence markers (a flat video has nowhere to put them)",
    "marker.remove": "marker removal",
    "text.create": "text and captions",
    "graphic.image": "graphic overlays",
    "animate": "keyframed effects such as zooms and pushes",
    "property.reset": "effect resets",
    "audio.duck": "ducking under speech",
    "audio.fade": "audio fades",
    "audio.gain": "per-clip level changes",
    "clip.overwrite": "clips on overlay tracks (SFX, music, B-roll)",
    "clip.insert": "inserted clips that ripple the timeline",
    "clip.remove": "clip removal",
    "clip.trim": "trims applied after assembly",
    "clip.move": "clips moved after assembly",
    "gap.remove": "gap closing",
    "transition": "transitions (every cut here is a hard cut)",
}

#: Operations that describe the rough assembly itself and are therefore
#: already represented by the segments. Not warned about.
REPRESENTED_OPS = (
    "sequence.create", "sequence.activate", "project.import", "project.save",
    "clip.append", "clip.speed", "track.add",
)

#: How FFmpeg is installed, quoted so the message is something to paste.
INSTALL_HINT = (
    "Install FFmpeg and put ffmpeg/ffprobe on PATH "
    "(Windows: winget install Gyan.FFmpeg), or set EDITING_FFMPEG and "
    "EDITING_FFPROBE to their full paths."
)

#: ``atempo`` is only defined for 0.5x-2.0x, so anything outside that range is
#: reached by chaining filters. Video has no such limit.
ATEMPO_MIN = 0.5
ATEMPO_MAX = 2.0

#: Speeds outside this are refused and rendered at 1x with a warning: a 40x
#: timelapse is a different feature, not a rough cut.
SPEED_MIN = 0.1
SPEED_MAX = 8.0


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def _dicts(value: Any) -> list[dict]:
    """The dict members of ``value``, or nothing.

    The same guard the transcription schema uses, for the same reason: every
    ``from_dict`` here is reachable from a file some other process wrote, and
    a string where a list was expected iterates as characters.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _even(value: int) -> int:
    """The nearest even number at or below ``value``, and at least 2.

    H.264 with 4:2:0 chroma cannot encode odd dimensions. Rounding here rather
    than letting FFmpeg fail means ``--height 719`` produces a slightly
    shorter video instead of an error four minutes into a render.
    """
    number = int(value)
    return max(2, number - (number % 2))


def _env(name: str, default: str) -> str:
    import os
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    import os
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderConfig:
    """Everything that decides what the proxy looks like.

    Frozen and serialised whole into the cache key, so changing any field that
    affects a pixel correctly re-renders instead of handing back yesterday's
    video. That is the same rule ``SamplingConfig`` and ``TranscriptionConfig``
    follow.

    ``quality``
        Sets CRF and encoder preset together. ``proxy`` is the default and is
        what the iteration loop is built around; ``draft`` is roughly twice as
        fast and looks it.
    ``height``
        Output height. 0 means "keep each source's own size", which is usually
        not what you want -- scaling 4K capture down to 720p is most of the
        speed, and mixed sizes cannot be joined by stream copy.
    ``fps``
        A single frame rate for the whole render. Fixed rather than
        source-following on purpose: segments are joined by stream copy, and a
        60fps clip joined to a 30fps one drifts audibly. 0 keeps each source's
        rate and warns.
    ``scale_mode``
        How a differently-shaped source is fitted. ``pad`` letterboxes.
    ``include_audio``
        Audio is on by default. A rough cut judged silently is a rough cut
        judged wrong -- the commentary is most of the pacing.
    ``max_seconds``
        Render only the first N seconds of the cut. The fastest possible look
        at whether an opening works.
    """

    backend: str = "ffmpeg"
    quality: str = "proxy"
    height: int = 720
    #: 0 derives the width from the height and the output aspect.
    width: int = 0
    #: Output aspect as ``w/h``. 16:9 unless told otherwise.
    aspect: float = 16.0 / 9.0
    fps: float = 30.0
    scale_mode: str = "pad"

    video_encoder: str = "auto"
    #: 0 takes the CRF from ``quality``.
    crf: int = 0
    #: Empty takes the preset from ``quality``.
    preset: str = ""
    pixel_format: str = "yuv420p"

    include_audio: bool = True
    audio_encoder: str = "aac"
    audio_bitrate: str = "160k"
    sample_rate: int = 48000
    audio_channels: int = 2

    container: str = "mp4"
    #: Encoder threads. 0 lets FFmpeg decide, which is normally right.
    threads: int = 0
    #: Render only the first N seconds of the cut. 0 renders all of it.
    max_seconds: float = 0.0
    #: Refuse to start a render with more segments than this. A guard against
    #: pointing this at a plan built from a three-hour recording by accident.
    max_segments: int = 600

    #: Keep the per-segment files after a successful concat.
    keep_temp: bool = False
    use_cache: bool = True
    #: Seconds before one segment encode is abandoned.
    segment_timeout: float = 1800.0
    #: Seconds before the concat is abandoned.
    concat_timeout: float = 3600.0
    #: Interval for review-note sections. 0 writes one section per segment.
    notes_interval: float = 0.0

    @classmethod
    def from_env(cls) -> "RenderConfig":
        return cls(
            backend=_env("EDITING_RENDER_BACKEND", "ffmpeg"),
            quality=_env("EDITING_RENDER_QUALITY", "proxy"),
            height=_env_int("EDITING_RENDER_HEIGHT", 720),
            fps=as_float(_env("EDITING_RENDER_FPS", "30"), 30.0),
            video_encoder=_env("EDITING_RENDER_ENCODER", "auto"),
            include_audio=_env_bool("EDITING_RENDER_AUDIO", True),
            keep_temp=_env_bool("EDITING_RENDER_KEEP_TEMP", False),
        )

    def validated(self) -> "RenderConfig":
        """Clamp to values FFmpeg can honour. Never raises.

        Returned rather than raised for the same reason the other configs do
        it: a nonsensical environment variable should degrade to something
        that renders, not stop a run. The clamped values are what land in the
        cache key, so two configs that clamp to the same thing correctly share
        a render.
        """
        from dataclasses import replace

        quality = coerce_one(self.quality, QUALITIES, "proxy")
        default_crf, default_preset = QUALITY_SETTINGS[quality]
        height = int(as_float(self.height))
        height = _even(height) if height > 0 else 0
        aspect = as_float(self.aspect, 16.0 / 9.0)
        if aspect <= 0:
            aspect = 16.0 / 9.0
        width = int(as_float(self.width))
        if width <= 0 and height > 0:
            width = _even(round(height * aspect))
        elif width > 0:
            width = _even(width)
        return replace(
            self,
            backend=coerce_one(self.backend, BACKENDS, "ffmpeg"),
            quality=quality,
            height=height,
            width=max(0, width),
            aspect=aspect,
            fps=max(0.0, min(as_float(self.fps, 30.0), 240.0)),
            scale_mode=coerce_one(self.scale_mode, SCALE_MODES, "pad"),
            video_encoder=coerce_one(
                self.video_encoder, VIDEO_ENCODERS, "auto"),
            crf=max(0, min(int(as_float(self.crf) or default_crf), 51)),
            preset=_text(self.preset, 20) or default_preset,
            pixel_format=_text(self.pixel_format, 20) or "yuv420p",
            audio_encoder=coerce_one(
                self.audio_encoder, AUDIO_ENCODERS, "aac"),
            audio_bitrate=_text(self.audio_bitrate, 12) or "160k",
            sample_rate=max(8000, min(int(as_float(self.sample_rate, 48000)),
                                      192000)),
            audio_channels=max(1, min(int(as_float(self.audio_channels, 2)),
                                      8)),
            container=_slug(self.container) or "mp4",
            threads=max(0, min(int(as_float(self.threads)), 64)),
            max_seconds=max(0.0, as_float(self.max_seconds)),
            max_segments=max(1, int(as_float(self.max_segments, 600))),
            segment_timeout=max(10.0, as_float(self.segment_timeout, 1800.0)),
            concat_timeout=max(10.0, as_float(self.concat_timeout, 3600.0)),
            notes_interval=max(0.0, as_float(self.notes_interval)),
        )

    # -- derived ---------------------------------------------------------

    @property
    def resolved_encoder(self) -> str:
        """The encoder name to actually pass to FFmpeg, before availability."""
        return "libx264" if self.video_encoder == "auto" \
            else self.video_encoder

    @property
    def is_hardware(self) -> bool:
        return self.resolved_encoder in QUALITY_FLAG

    @property
    def quality_flag(self) -> str:
        return QUALITY_FLAG.get(self.resolved_encoder, "-crf")

    @property
    def encoder_preset(self) -> str:
        """x264's preset, or the hardware equivalent where one is needed."""
        return HARDWARE_PRESETS.get(self.resolved_encoder, self.preset)

    @property
    def scales(self) -> bool:
        return self.height > 0 and self.width > 0

    @property
    def warnings(self) -> list[str]:
        """Things worth saying about these settings before a long render."""
        out: list[str] = []
        if self.fps <= 0:
            out.append(
                "fps is 0, so each segment keeps its source frame rate. "
                "Segments are joined by stream copy, so mixing 60fps and "
                "30fps footage this way can drift out of sync -- set --fps 30 "
                "if the cut mixes capture settings."
            )
        if not self.scales:
            out.append(
                "no output size is set, so every segment keeps its own. A cut "
                "mixing resolutions will fail to join; set --height 720."
            )
        if not self.include_audio:
            out.append(
                "audio is off. A rough cut judged silently is judged wrong -- "
                "the commentary carries most of the pacing."
            )
        if self.is_hardware:
            out.append(
                f"'{self.resolved_encoder}' is a hardware encoder: much "
                "faster, noticeably softer at the same setting. Fine for a "
                "proxy, wrong for anything you keep."
            )
        if self.backend == "mock":
            out.append(
                "MOCK backend: this writes a placeholder file and never runs "
                "FFmpeg. Nothing it produces is watchable."
            )
        return out

    def cache_key_part(self) -> dict:
        """The subset of this config that changes a pixel of the output.

        ``keep_temp``, ``use_cache``, both timeouts and ``notes_interval`` are
        deliberately absent: none of them changes the video, and including
        them would mean turning the cache off invalidated every render already
        on disk.
        """
        clean = self.validated()
        return {
            "backend": clean.backend,
            "quality": clean.quality,
            "height": clean.height,
            "width": clean.width,
            "fps": round(clean.fps, 3),
            "scale_mode": clean.scale_mode,
            "video_encoder": clean.video_encoder,
            "crf": clean.crf,
            "preset": clean.preset,
            "pixel_format": clean.pixel_format,
            "include_audio": clean.include_audio,
            "audio_encoder": clean.audio_encoder,
            "audio_bitrate": clean.audio_bitrate,
            "sample_rate": clean.sample_rate,
            "audio_channels": clean.audio_channels,
            "container": clean.container,
            "max_seconds": round(clean.max_seconds, 3),
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        data["resolved_encoder"] = self.resolved_encoder
        data["encoder_preset"] = self.encoder_preset
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RenderConfig":
        data = data or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# What goes in
# ---------------------------------------------------------------------------

@dataclass
class RenderInput:
    """One source file the render draws on, and whether it is usable.

    Measured before anything is encoded, because "clip_07 is on a drive that
    is not plugged in" should be a sentence at the start of a render rather
    than an FFmpeg error four minutes into one.
    """

    path: str = ""
    asset_id: str = ""
    exists: bool = False
    size_bytes: int = 0
    mtime: float = 0.0
    #: Cheap head+tail hash. Part of the cache key, so a re-export invalidates.
    content_hash: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = True
    #: How many segments come from this file.
    segments: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.exists and self.size_bytes > 0

    def cache_key_part(self) -> dict:
        """Identity, for the cache key.

        ``mtime`` is *not* here: a file copied to a new drive keeps its bytes
        and gets a new mtime, and re-rendering an hour of footage because a
        timestamp moved is exactly the waste the content hash exists to avoid.
        """
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        data["usable"] = self.usable
        data["mtime"] = round(self.mtime, 3)
        data["duration"] = round(self.duration, 3)
        data["fps"] = round(self.fps, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RenderInput":
        data = data or {}
        return cls(
            path=_text(data.get("path"), 500),
            asset_id=_text(data.get("asset_id"), 120),
            exists=as_bool(data.get("exists")),
            size_bytes=int(as_float(data.get("size_bytes"))),
            mtime=as_float(data.get("mtime")),
            content_hash=_text(data.get("content_hash"), 64),
            duration=as_float(data.get("duration")),
            width=int(as_float(data.get("width"))),
            height=int(as_float(data.get("height"))),
            fps=as_float(data.get("fps")),
            has_audio=as_bool(data.get("has_audio"), True),
            segments=int(as_float(data.get("segments"))),
            warnings=as_str_list(data.get("warnings"), limit=20),
        )


@dataclass
class RenderSegment:
    """One source range, at one place on the rendered timeline.

    The whole contract between the editing layer and FFmpeg. ``placement_id``
    and ``recommendation_ids`` carry no weight in the render itself and are
    kept anyway: they are what lets a person watching the proxy at 4:12 ask
    "why is this here" and get an answer.

    ``timeline_in``/``timeline_out`` are *computed* from the durations and
    speeds ahead of the render, not read back from the finished file, so the
    review notes can be written before FFmpeg is even installed.
    """

    segment_id: str = ""
    index: int = 0
    source_path: str = ""
    asset_id: str = ""

    source_in: float = 0.0
    source_out: float = 0.0
    timeline_in: float = 0.0
    timeline_out: float = 0.0

    speed: float = 1.0
    audio_enabled: bool = True

    #: What put this range in the cut.
    placement_id: str = ""
    recommendation_ids: list[str] = field(default_factory=list)
    keep_reason: str = "unknown"
    protected: bool = False
    label: str = ""
    warnings: list[str] = field(default_factory=list)

    # -- derived ---------------------------------------------------------

    @property
    def source_duration(self) -> float:
        """How much footage is read. What FFmpeg is asked for with ``-t``."""
        return max(0.0, self.source_out - self.source_in)

    @property
    def duration(self) -> float:
        """How long this occupies the rendered timeline, after any speed."""
        return max(0.0, self.timeline_out - self.timeline_in)

    @property
    def has_speed_change(self) -> bool:
        return abs(self.speed - 1.0) > 1e-6

    @property
    def is_empty(self) -> bool:
        return self.source_duration <= 0.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update({
            "source_in": round(self.source_in, 3),
            "source_out": round(self.source_out, 3),
            "timeline_in": round(self.timeline_in, 3),
            "timeline_out": round(self.timeline_out, 3),
            "source_duration": round(self.source_duration, 3),
            "duration": round(self.duration, 3),
            "speed": round(self.speed, 4),
            "has_speed_change": self.has_speed_change,
            "source_file": Path(self.source_path).name if self.source_path
            else "",
        })
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RenderSegment":
        data = data or {}
        source_in = max(0.0, as_float(data.get("source_in")))
        source_out = max(source_in, as_float(data.get("source_out"), source_in))
        timeline_in = max(0.0, as_float(data.get("timeline_in")))
        timeline_out = max(timeline_in,
                           as_float(data.get("timeline_out"), timeline_in))
        return cls(
            segment_id=_text(data.get("segment_id"), 60),
            index=int(as_float(data.get("index"))),
            source_path=_text(data.get("source_path"), 500),
            asset_id=_text(data.get("asset_id"), 120),
            source_in=source_in,
            source_out=source_out,
            timeline_in=timeline_in,
            timeline_out=timeline_out,
            speed=max(0.0, as_float(data.get("speed"), 1.0)) or 1.0,
            audio_enabled=as_bool(data.get("audio_enabled"), True),
            placement_id=_text(data.get("placement_id"), 60),
            recommendation_ids=as_str_list(
                data.get("recommendation_ids"), limit=50),
            keep_reason=_text(data.get("keep_reason"), 40) or "unknown",
            protected=as_bool(data.get("protected")),
            label=_text(data.get("label"), 120),
            warnings=as_str_list(data.get("warnings"), limit=20),
        )


def segment_id_for(index: int, placement_id: str, source_in: float) -> str:
    """Stable per (position, placement, in-point).

    The index leads the ID because the per-segment files are also concatenated
    in name order as a second line of defence -- if the list is ever sorted by
    filename, the sort still produces the right cut.
    """
    return f"s{index:04d}_" + short_hash(
        placement_id, round(float(source_in), 3), length=6)


# ---------------------------------------------------------------------------
# What comes out
# ---------------------------------------------------------------------------

@dataclass
class RenderArtifact:
    """One file a render produced, and what it is for."""

    name: str = ""
    path: str = ""
    #: video / notes / report / json / commands / log
    kind: str = "json"
    description: str = ""
    size_bytes: int = 0
    exists: bool = False

    @classmethod
    def describe(cls, path: str | Path, *, kind: str = "json",
                 description: str = "") -> "RenderArtifact":
        target = Path(path)
        try:
            size = target.stat().st_size if target.exists() else 0
        except OSError:
            size = 0
        return cls(
            name=target.name,
            path=str(target),
            kind=kind,
            description=description,
            size_bytes=size,
            exists=target.exists(),
        )

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["size_mb"] = self.size_mb
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RenderArtifact":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 120),
            path=_text(data.get("path"), 500),
            kind=_text(data.get("kind"), 20) or "json",
            description=_text(data.get("description"), 300),
            size_bytes=int(as_float(data.get("size_bytes"))),
            exists=as_bool(data.get("exists")),
        )


@dataclass
class RenderFailure:
    """Why a render did not happen, and what to do about it.

    A record rather than an exception wherever a caller can carry on: a run
    that could not render still produced every plan, and the useful outcome is
    those plans plus an exact account of the render.
    """

    stage: str = "unknown"
    code: str = "render_failed"
    message: str = ""
    hint: str = ""
    path: str = ""
    #: Whether re-running could plausibly work -- after installing FFmpeg, or
    #: with the drive plugged back in. A plan with no placements is not.
    recoverable: bool = True
    #: The FFmpeg invocation that failed, if one did.
    command: list[str] = field(default_factory=list)
    stderr: str = ""
    detail: dict = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"{self.stage}: {self.message}"]
        if self.hint:
            lines.append(f"  fix : {self.hint}")
        if self.path:
            lines.append(f"  file: {self.path}")
        if self.stderr:
            lines.append(f"  ffmpeg: {self.stderr[-300:]}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["RenderFailure"]:
        if not data:
            return None
        return cls(
            stage=coerce_one(data.get("stage"), FAILURE_STAGES, "unknown"),
            code=_text(data.get("code"), 60) or "render_failed",
            message=_text(data.get("message"), 1000),
            hint=_text(data.get("hint"), 1000),
            path=_text(data.get("path"), 500),
            recoverable=as_bool(data.get("recoverable"), True),
            command=as_str_list(data.get("command"), limit=80),
            stderr=_text(data.get("stderr"), 2000),
            detail=dict(data.get("detail") or {}),
        )


@dataclass
class RenderResult:
    """What one render actually produced.

    ``rendered`` is the field to trust. It is True only when a video file
    exists on disk with bytes in it, produced by a real encoder -- not when
    the commands were built, not when the plan validated, and never when the
    mock backend ran.
    """

    job_id: str = ""
    status: str = "pending"
    output_path: str = ""

    #: True only when a real video file was produced.
    rendered: bool = False
    #: True when this came back from a previous render rather than the encoder.
    from_cache: bool = False
    #: True when the mock backend produced it. Loud everywhere, on purpose.
    mock: bool = False

    segments: int = 0
    #: Sum of the segment durations: what the video *should* be.
    planned_duration: float = 0.0
    #: What ffprobe says the finished file is. 0.0 when nothing probed it.
    measured_duration: float = 0.0
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False

    encoder: str = ""
    ffmpeg_version: str = ""
    commands_run: int = 0
    elapsed: float = 0.0
    created_at: str = ""
    cache_key: str = ""

    artifacts: list[RenderArtifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failure: Optional[RenderFailure] = None
    schema_version: int = 1

    # -- derived ---------------------------------------------------------

    @property
    def ok(self) -> bool:
        return self.status in ("rendered", "cached") and self.failure is None

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)

    @property
    def realtime_factor(self) -> float:
        """Video seconds produced per wall-clock second.

        The number that answers "how long will my 20-minute cut take". Zero
        for a cached or mock result, because no encoding happened.
        """
        if self.from_cache or self.mock or self.elapsed <= 0:
            return 0.0
        return round(self.planned_duration / self.elapsed, 2)

    @property
    def duration_drift(self) -> float:
        """How far the finished file is from what the plan predicted.

        More than a second or two means a speed change or a frame-rate
        mismatch did something unexpected, which is worth seeing before
        trusting timings read off the proxy.
        """
        if self.measured_duration <= 0 or self.planned_duration <= 0:
            return 0.0
        return round(self.measured_duration - self.planned_duration, 3)

    def artifact(self, kind: str) -> Optional[RenderArtifact]:
        for item in self.artifacts:
            if item.kind == kind:
                return item
        return None

    def stats(self) -> dict:
        return {
            "segments": self.segments,
            "planned_duration": round(self.planned_duration, 2),
            "measured_duration": round(self.measured_duration, 2),
            "duration_drift": self.duration_drift,
            "size_mb": self.size_mb,
            "resolution": f"{self.width}x{self.height}" if self.width else "",
            "fps": round(self.fps, 2),
            "has_audio": self.has_audio,
            "encoder": self.encoder,
            "commands_run": self.commands_run,
            "elapsed": round(self.elapsed, 2),
            "realtime_factor": self.realtime_factor,
            "from_cache": self.from_cache,
            "mock": self.mock,
            "rendered": self.rendered,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "status": self.status,
            "output_path": self.output_path,
            "rendered": self.rendered,
            "from_cache": self.from_cache,
            "mock": self.mock,
            "ok": self.ok,
            "segments": self.segments,
            "planned_duration": round(self.planned_duration, 3),
            "measured_duration": round(self.measured_duration, 3),
            "duration_drift": self.duration_drift,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "has_audio": self.has_audio,
            "encoder": self.encoder,
            "ffmpeg_version": self.ffmpeg_version,
            "commands_run": self.commands_run,
            "elapsed": round(self.elapsed, 3),
            "created_at": self.created_at,
            "cache_key": self.cache_key,
            "stats": self.stats(),
            "artifacts": [item.to_dict() for item in self.artifacts],
            "warnings": list(self.warnings),
            "failure": self.failure.to_dict() if self.failure else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RenderResult":
        data = data or {}
        return cls(
            job_id=_text(data.get("job_id"), 120),
            status=coerce_one(data.get("status"), JOB_STATUSES, "pending"),
            output_path=_text(data.get("output_path"), 500),
            rendered=as_bool(data.get("rendered")),
            from_cache=as_bool(data.get("from_cache")),
            mock=as_bool(data.get("mock")),
            segments=int(as_float(data.get("segments"))),
            planned_duration=as_float(data.get("planned_duration")),
            measured_duration=as_float(data.get("measured_duration")),
            size_bytes=int(as_float(data.get("size_bytes"))),
            width=int(as_float(data.get("width"))),
            height=int(as_float(data.get("height"))),
            fps=as_float(data.get("fps")),
            has_audio=as_bool(data.get("has_audio")),
            encoder=_text(data.get("encoder"), 40),
            ffmpeg_version=_text(data.get("ffmpeg_version"), 200),
            commands_run=int(as_float(data.get("commands_run"))),
            elapsed=as_float(data.get("elapsed")),
            created_at=_text(data.get("created_at"), 40),
            cache_key=_text(data.get("cache_key"), 80),
            artifacts=[
                RenderArtifact.from_dict(item)
                for item in _dicts(data.get("artifacts"))
            ],
            warnings=as_str_list(data.get("warnings"), limit=100),
            failure=RenderFailure.from_dict(data.get("failure")),
        )


@dataclass
class RenderJob:
    """One rough cut, one configuration, and what happened to it.

    Written to the job folder so a render is inspectable afterwards without
    re-deriving anything: the segments it decided on, the commands it built,
    the sources it read, and the result.
    """

    job_id: str = ""
    status: str = "pending"
    config: RenderConfig = field(default_factory=RenderConfig)

    #: Where the plan came from. Both kept: the name is how the pipeline finds
    #: it, the path is what a person recognises.
    plan_name: str = "structure"
    plan_path: str = ""
    sequence_name: str = ""

    segments: list[RenderSegment] = field(default_factory=list)
    inputs: list[RenderInput] = field(default_factory=list)
    #: Every FFmpeg invocation, in order, exactly as it was (or would be) run.
    commands: list[list[str]] = field(default_factory=list)

    output_dir: str = ""
    cache_key: str = ""
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    elapsed: float = 0.0

    result: Optional[RenderResult] = None
    failure: Optional[RenderFailure] = None
    warnings: list[str] = field(default_factory=list)
    #: Rough-cut features a flat proxy cannot represent, said once each.
    unsupported: list[str] = field(default_factory=list)
    schema_version: int = 1

    # -- derived ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def ok(self) -> bool:
        """The job ran to completion. Says nothing about there being a video.

        ``rendered`` is the field that answers that, and a mocked job is
        deliberately ``ok`` and not ``rendered``.
        """
        return self.status in ("rendered", "cached", "planned", "mocked")

    @property
    def rendered(self) -> bool:
        return bool(self.result and self.result.rendered)

    @property
    def duration(self) -> float:
        return round(sum(segment.duration for segment in self.segments), 3)

    @property
    def source_duration(self) -> float:
        return round(
            sum(segment.source_duration for segment in self.segments), 3)

    @property
    def missing_inputs(self) -> list[RenderInput]:
        return [item for item in self.inputs if not item.usable]

    @property
    def output_path(self) -> str:
        if self.result and self.result.output_path:
            return self.result.output_path
        if not self.output_dir:
            return ""
        return str(Path(self.output_dir) / f"render.{self.config.container}")

    @property
    def notes_path(self) -> str:
        return str(Path(self.output_dir) / "review_notes.md") \
            if self.output_dir else ""

    def stats(self) -> dict:
        return {
            "segments": len(self.segments),
            "duration": self.duration,
            "source_duration": self.source_duration,
            "sources": len(self.inputs),
            "missing_sources": len(self.missing_inputs),
            "speed_changes": sum(
                1 for s in self.segments if s.has_speed_change),
            "muted_segments": sum(
                1 for s in self.segments if not s.audio_enabled),
            "commands": len(self.commands),
            "unsupported_features": len(self.unsupported),
        }

    def line(self) -> str:
        mark = {"rendered": "+", "cached": "=", "planned": ".", "failed": "x",
                "mocked": "~", "running": ">", "pending": " "}.get(
                    self.status, "?")
        detail = f"{len(self.segments)} segment(s), {self.duration:.0f}s"
        if self.result is not None and self.result.size_bytes:
            detail += f", {self.result.size_mb} MB"
        if self.failure is not None:
            detail = self.failure.message[:60]
        return f"{mark} {self.job_id[:30]:<30} {self.status:<9} {detail}"

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "status": self.status,
            "config": self.config.to_dict(),
            "plan_name": self.plan_name,
            "plan_path": self.plan_path,
            "sequence_name": self.sequence_name,
            "output_dir": self.output_dir,
            "output_path": self.output_path,
            "notes_path": self.notes_path,
            "cache_key": self.cache_key,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed": round(self.elapsed, 3),
            "stats": self.stats(),
            "inputs": [item.to_dict() for item in self.inputs],
            "unsupported": list(self.unsupported),
            "warnings": list(self.warnings),
            "result": self.result.to_dict() if self.result else None,
            "failure": self.failure.to_dict() if self.failure else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RenderJob":
        data = data or {}
        return cls(
            job_id=_text(data.get("job_id"), 120),
            status=coerce_one(data.get("status"), JOB_STATUSES, "pending"),
            config=RenderConfig.from_dict(data.get("config")),
            plan_name=_text(data.get("plan_name"), 120) or "structure",
            plan_path=_text(data.get("plan_path"), 500),
            sequence_name=_text(data.get("sequence_name"), 200),
            segments=[
                RenderSegment.from_dict(item)
                for item in _dicts(data.get("segments"))
            ],
            inputs=[
                RenderInput.from_dict(item)
                for item in _dicts(data.get("inputs"))
            ],
            commands=[
                [str(part) for part in command]
                for command in (data.get("commands") or [])
                if isinstance(command, (list, tuple))
            ],
            output_dir=_text(data.get("output_dir"), 500),
            cache_key=_text(data.get("cache_key"), 80),
            created_at=_text(data.get("created_at"), 40),
            started_at=_text(data.get("started_at"), 40),
            ended_at=_text(data.get("ended_at"), 40),
            elapsed=as_float(data.get("elapsed")),
            result=(RenderResult.from_dict(data["result"])
                    if isinstance(data.get("result"), dict) else None),
            failure=RenderFailure.from_dict(data.get("failure")),
            warnings=as_str_list(data.get("warnings"), limit=100),
            unsupported=as_str_list(data.get("unsupported"), limit=40),
        )


def job_id_for(plan_name: str, cache_key: str) -> str:
    """A job ID that is stable for one plan under one configuration.

    Deliberately *not* timestamped, for the same reason transcription job IDs
    are not: re-rendering the same cut with the same settings is the same
    answer, so it belongs in the same folder. That is also what makes the
    cache trivial -- a job folder holding a complete render *is* the cache
    entry, and there is no second place for the two to disagree.
    """
    stem = _slug(plan_name)[:24] or "cut"
    return f"{stem}-{short_hash(cache_key, length=8)}"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

@dataclass
class RenderReport:
    """The readable summary of one render.

    Built from the job, so it can be regenerated at any time without
    re-rendering. Says what was produced, what could not be represented, and
    the exact commands to watch it or do it again.
    """

    job_id: str = ""
    status: str = ""
    output_path: str = ""
    notes_path: str = ""
    rendered: bool = False
    mock: bool = False
    from_cache: bool = False
    stats: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    segments: list[dict] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    next_commands: list[str] = field(default_factory=list)
    failure: Optional[dict] = None
    generated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

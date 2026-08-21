"""Configuration for the editing structure layer.

Two things live here, and the split matters for correctness rather than tidiness:

``SamplingConfig`` is everything that changes *what the model is shown*. It is
serialised into every visual-analysis cache key, so raising ``frames_per_window``
or lowering ``window_seconds`` correctly invalidates old results instead of
silently mixing two different analyses in one timeline.

``EditingConfig`` is everything else -- where files live, which backend serves
the model, timeouts. Only the fields that genuinely affect the *content* of a
result (the model name) reach a cache key.

Every field is environment-overridable so the CLI stays argument-light on the
user's machine, and every default is safe to run against 4K Minecraft capture.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Optional

#: Bumped when a change to the analysis or alignment logic makes previously
#: cached results wrong. Part of every cache key.
SCHEMA_VERSION = 1

#: The vision model this session targets. Kept as a plain name (not a path) so
#: it reads identically in a cache key whichever backend serves it.
DEFAULT_VISION_MODEL = "Qwen3-VL-8B-Instruct"

#: Where the local model lives on the user's machine. Only used to give a
#: better error message when a backend is unreachable -- this layer never
#: loads weights itself.
DEFAULT_MODEL_DIR = r"E:\Assistant\AI_Models\editingllm"

VIDEO_EXTENSIONS = (
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".mxf",
    ".webm", ".wmv", ".flv",
)

TRANSCRIPT_EXTENSIONS = (".srt", ".vtt", ".json", ".txt", ".csv", ".tsv")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SamplingConfig:
    """How a long recording is reduced to something a VLM can actually read.

    A 40-minute Minecraft session is ~72k frames. The model sees a few hundred
    at most, chosen by these numbers:

    ``window_seconds``
        Length of one analysis window. One window becomes at most one visual
        event, so this is also the coarsest time resolution of the output.
    ``window_overlap``
        Seconds of overlap between consecutive windows. A little overlap stops
        an event that straddles a boundary from being cut in half.
    ``frames_per_window``
        Frames shown to the model for a normal window. Three is the useful
        minimum: start, middle, end, which is enough to read motion direction.
    ``dense_frames_per_window``
        Frames for a window flagged as high-change. Fights and deaths get more
        evidence than walking down a corridor.
    ``motion_threshold``
        Scene-change score (0..1) above which a window counts as high-change.
    ``motion_probe_interval``
        How often the cheap motion scout samples the file. This does not
        decode every frame; it reads scene scores at this spacing.
    ``dense_window_seconds``
        High-change stretches are re-cut at this shorter window length so a
        30-second fight does not collapse into five identical events.
    ``max_windows``
        Hard ceiling on windows per file. Protects against pointing the CLI at
        a three-hour recording and waiting all day.
    ``min_window_seconds``
        Windows shorter than this at the tail of a file are merged backwards
        rather than emitted as a sliver.
    ``frame_width``
        Long edge of the extracted JPEG. 768 keeps text (coordinates, chat,
        item counts) legible while staying inside a sane token budget.
    ``frame_quality``
        JPEG quality for extracted frames, 2 (best) to 31 (worst) in ffmpeg's
        scale.
    """

    window_seconds: float = 8.0
    window_overlap: float = 0.5
    frames_per_window: int = 3
    dense_frames_per_window: int = 5
    motion_threshold: float = 0.30
    motion_probe_interval: float = 2.0
    dense_window_seconds: float = 4.0
    max_windows: int = 400
    min_window_seconds: float = 1.5
    frame_width: int = 768
    frame_quality: int = 4

    @classmethod
    def from_env(cls) -> "SamplingConfig":
        return cls(
            window_seconds=_env_float("EDITING_WINDOW_SECONDS", 8.0),
            window_overlap=_env_float("EDITING_WINDOW_OVERLAP", 0.5),
            frames_per_window=_env_int("EDITING_FRAMES_PER_WINDOW", 3),
            dense_frames_per_window=_env_int("EDITING_DENSE_FRAMES", 5),
            motion_threshold=_env_float("EDITING_MOTION_THRESHOLD", 0.30),
            motion_probe_interval=_env_float("EDITING_MOTION_INTERVAL", 2.0),
            dense_window_seconds=_env_float("EDITING_DENSE_WINDOW_SECONDS", 4.0),
            max_windows=_env_int("EDITING_MAX_WINDOWS", 400),
            min_window_seconds=_env_float("EDITING_MIN_WINDOW_SECONDS", 1.5),
            frame_width=_env_int("EDITING_FRAME_WIDTH", 768),
            frame_quality=_env_int("EDITING_FRAME_QUALITY", 4),
        )

    def validated(self) -> "SamplingConfig":
        """Clamp to values the sampler can actually honour.

        Returned rather than raised: a nonsensical env var should degrade to a
        working default, not stop an overnight analysis run. The clamped values
        are what land in the cache key, so two configs that clamp to the same
        thing correctly share a cache entry.
        """
        window = max(0.5, float(self.window_seconds))
        dense = max(0.5, min(float(self.dense_window_seconds), window))
        return replace(
            self,
            window_seconds=window,
            dense_window_seconds=dense,
            # Overlap must stay under the window or the sampler cannot advance.
            window_overlap=max(0.0, min(float(self.window_overlap), window * 0.5)),
            frames_per_window=max(1, int(self.frames_per_window)),
            dense_frames_per_window=max(
                max(1, int(self.frames_per_window)), int(self.dense_frames_per_window)
            ),
            motion_threshold=max(0.0, min(1.0, float(self.motion_threshold))),
            motion_probe_interval=max(0.25, float(self.motion_probe_interval)),
            max_windows=max(1, int(self.max_windows)),
            min_window_seconds=max(0.0, min(float(self.min_window_seconds), window)),
            frame_width=max(128, int(self.frame_width)),
            frame_quality=max(2, min(31, int(self.frame_quality))),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SamplingConfig":
        data = data or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def cache_key_part(self) -> dict:
        """The subset of this config that changes analysis output.

        Every field qualifies today; the method exists so that adding a purely
        cosmetic knob later does not silently invalidate every cached file.
        """
        return self.validated().to_dict()


@dataclass(frozen=True)
class AudioConfig:
    """Thresholds for the audio event layer.

    Like ``SamplingConfig``, this is serialised into every audio cache key, so
    changing a threshold correctly re-analyses rather than mixing two different
    analyses in one timeline.

    The defaults are tuned for game-capture with live commentary, where the
    voice sits well above the game bed. They are all *relative* to the file's
    own median loudness wherever that is meaningful -- an absolute dBFS
    threshold would behave completely differently on a quiet recording and a
    hot one.

    ``sample_interval``
        Spacing of the loudness envelope. 0.25s is fine enough to catch a
        shout and coarse enough to scan a 40-minute file quickly.
    ``silence_threshold_db``
        Absolute dBFS floor for "nothing is happening". Absolute rather than
        relative on purpose: digital silence is digital silence.
    ``min_silence_seconds``
        Shorter quiet stretches are just gaps between words.
    ``long_pause_seconds``
        A transcript gap this long reads as dead air to a viewer.
    ``spike_delta_db``
        How far above the local baseline counts as a spike.
    ``reaction_delta_db``
        A larger jump, out of a quiet run-up: the "oh god" moment.
    ``low_energy_delta_db``
        How far *below* baseline counts as a boring-audio stretch.
    ``clipping_db``
        Peak level at which the signal is distorting.
    ``laughter_min_bursts``
        Loudness bursts within ``laughter_window`` needed to guess laughter.
        Laughter is rhythmic; a single shout is not.
    ``music_min_seconds``
        Sustained energy with little speech, for this long, reads as music.
    ``speech_dense_wps`` / ``speech_sparse_wps``
        Words per second above/below which narration is notably fast or slow.
    """

    sample_interval: float = 0.25
    silence_threshold_db: float = -45.0
    min_silence_seconds: float = 0.8
    long_pause_seconds: float = 2.5
    spike_delta_db: float = 8.0
    reaction_delta_db: float = 14.0
    low_energy_delta_db: float = 10.0
    clipping_db: float = -0.2
    laughter_min_bursts: int = 3
    laughter_window: float = 3.0
    music_min_seconds: float = 6.0
    speech_dense_wps: float = 3.4
    speech_sparse_wps: float = 0.8
    #: Minimum confidence an event needs to be kept at all.
    min_confidence: float = 0.25
    #: Ceiling on the confidence any purely-heuristic inference may claim.
    #: The layer must not be able to assert laughter as strongly as silence.
    max_inferred_confidence: float = 0.45

    @classmethod
    def from_env(cls) -> "AudioConfig":
        return cls(
            sample_interval=_env_float("EDITING_AUDIO_INTERVAL", 0.25),
            silence_threshold_db=_env_float("EDITING_SILENCE_DB", -45.0),
            min_silence_seconds=_env_float("EDITING_MIN_SILENCE", 0.8),
            long_pause_seconds=_env_float("EDITING_LONG_PAUSE", 2.5),
            spike_delta_db=_env_float("EDITING_SPIKE_DELTA_DB", 8.0),
            reaction_delta_db=_env_float("EDITING_REACTION_DELTA_DB", 14.0),
            low_energy_delta_db=_env_float("EDITING_LOW_ENERGY_DELTA_DB", 10.0),
            clipping_db=_env_float("EDITING_CLIPPING_DB", -0.2),
            music_min_seconds=_env_float("EDITING_MUSIC_MIN_SECONDS", 6.0),
        )

    def validated(self) -> "AudioConfig":
        """Clamp to values the detectors can honour. Never raises."""
        return replace(
            self,
            sample_interval=max(0.05, float(self.sample_interval)),
            min_silence_seconds=max(0.1, float(self.min_silence_seconds)),
            long_pause_seconds=max(0.5, float(self.long_pause_seconds)),
            spike_delta_db=max(1.0, float(self.spike_delta_db)),
            # A "reaction" must always be a bigger jump than a plain spike, or
            # every spike would also register as a reaction.
            reaction_delta_db=max(
                max(1.0, float(self.spike_delta_db)) + 1.0,
                float(self.reaction_delta_db),
            ),
            low_energy_delta_db=max(1.0, float(self.low_energy_delta_db)),
            laughter_min_bursts=max(2, int(self.laughter_min_bursts)),
            laughter_window=max(0.5, float(self.laughter_window)),
            music_min_seconds=max(1.0, float(self.music_min_seconds)),
            speech_dense_wps=max(0.1, float(self.speech_dense_wps)),
            speech_sparse_wps=max(0.0, float(self.speech_sparse_wps)),
            min_confidence=max(0.0, min(1.0, float(self.min_confidence))),
            max_inferred_confidence=max(
                0.05, min(1.0, float(self.max_inferred_confidence))
            ),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "AudioConfig":
        data = data or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def cache_key_part(self) -> dict:
        return self.validated().to_dict()


@dataclass(frozen=True)
class EditingConfig:
    """Paths, backend selection and limits for one editing session."""

    #: Root for every output this layer writes.
    output_dir: Path = field(default_factory=lambda: Path("data/editing"))
    #: Where footage is looked for when no folder is given on the CLI.
    footage_dir: Optional[Path] = None

    vision_model: str = DEFAULT_VISION_MODEL
    model_dir: str = DEFAULT_MODEL_DIR
    #: "openai" (vLLM / LM Studio / llama.cpp server), "ollama", or "mock".
    vision_backend: str = "openai"
    vision_base_url: str = "http://localhost:8000/v1"
    vision_api_key: str = "not-needed"
    vision_timeout: float = 180.0
    vision_max_retries: int = 2
    #: Windows analysed concurrently. Local single-GPU serving is usually
    #: saturated at 1-2; higher only helps against a batching server.
    vision_concurrency: int = 1

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    #: Talk to Premiere at all. Off in tests and on headless machines.
    use_premiere: bool = True

    @classmethod
    def from_env(cls) -> "EditingConfig":
        footage = os.getenv("EDITING_FOOTAGE_DIR") or ""
        return cls(
            output_dir=Path(_env("EDITING_OUTPUT_DIR", "data/editing")),
            footage_dir=Path(footage) if footage else None,
            vision_model=_env("EDITING_VISION_MODEL", DEFAULT_VISION_MODEL),
            model_dir=_env("EDITING_MODEL_DIR", DEFAULT_MODEL_DIR),
            vision_backend=_env("EDITING_VISION_BACKEND", "openai").lower(),
            vision_base_url=_env("EDITING_VISION_BASE_URL", "http://localhost:8000/v1"),
            vision_api_key=_env("EDITING_VISION_API_KEY", "not-needed"),
            vision_timeout=_env_float("EDITING_VISION_TIMEOUT", 180.0),
            vision_max_retries=_env_int("EDITING_VISION_RETRIES", 2),
            vision_concurrency=max(1, _env_int("EDITING_VISION_CONCURRENCY", 1)),
            ffmpeg=_env("EDITING_FFMPEG", "ffmpeg"),
            ffprobe=_env("EDITING_FFPROBE", "ffprobe"),
            use_premiere=_env_bool("EDITING_USE_PREMIERE", True),
        )

    # -- derived locations ------------------------------------------------
    # Created on access rather than at construction so importing this module
    # never writes to disk (tests import it constantly).

    @property
    def cache_dir(self) -> Path:
        return self.output_dir / "cache"

    @property
    def transcripts_dir(self) -> Path:
        return self.output_dir / "transcripts"

    @property
    def visual_dir(self) -> Path:
        return self.output_dir / "visual"

    @property
    def timelines_dir(self) -> Path:
        return self.output_dir / "timelines"

    @property
    def audio_dir(self) -> Path:
        return self.output_dir / "audio"

    @property
    def recommendations_dir(self) -> Path:
        return self.output_dir / "recommendations"

    @property
    def plans_dir(self) -> Path:
        return self.output_dir / "plans"

    @property
    def roughcut_dir(self) -> Path:
        return self.output_dir / "roughcut"

    @property
    def review_dir(self) -> Path:
        return self.output_dir / "review"

    @property
    def asset_library_dir(self) -> Path:
        """The asset *index* and placement plans.

        Note this is not where the asset files live. The library itself sits
        beside the model weights (``<model_dir>/assets``) because a sound
        library outlives any one run; what lands here is the derived index and
        the plans, which are disposable.
        """
        return self.output_dir / "assets"

    @property
    def layers_dir(self) -> Path:
        """Layered, styled edit plans and their reports.

        Separate from ``roughcut_dir`` for the same reason ``critic_dir`` is:
        a style pass is one interpretation of a cut, and re-styling must never
        be able to destroy the cut being styled.
        """
        return self.output_dir / "layers"

    @property
    def critic_dir(self) -> Path:
        """Critic findings, revisions and revision plans.

        Separate from ``roughcut_dir`` on purpose: a revision pass must never
        be able to overwrite the cut it is judging.
        """
        return self.output_dir / "critic"

    @property
    def episode_dir(self) -> Path:
        """Episode memory, retention plans and their reports.

        Its own directory for the same reason ``critic_dir`` and ``layers_dir``
        are: an episode memory is an observation about a cut, and re-planning
        must never be able to overwrite the cut -- or the memory -- it reads.
        """
        return self.output_dir / "episode"

    @property
    def feedback_dir(self) -> Path:
        """Feedback sessions, their queues, logs, reports and exports.

        Its own directory, and the only one in this layer that is never
        rebuilt: everything else under ``output_dir`` is derived from the
        footage and can be regenerated by re-running a pass. Human review
        cannot, so a feedback session is append-only and is never cleaned by
        anything that clears plans.
        """
        return self.output_dir / "feedback"

    @property
    def frames_dir(self) -> Path:
        return self.output_dir / "frames"

    @property
    def assets_file(self) -> Path:
        return self.output_dir / "assets.json"

    def ensure_dirs(self) -> None:
        for directory in (
            self.output_dir, self.cache_dir, self.transcripts_dir,
            self.visual_dir, self.timelines_dir, self.frames_dir,
            self.audio_dir, self.recommendations_dir, self.plans_dir,
            self.roughcut_dir, self.review_dir, self.critic_dir,
            self.layers_dir, self.asset_library_dir, self.episode_dir,
            self.feedback_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["output_dir"] = str(self.output_dir)
        data["footage_dir"] = str(self.footage_dir) if self.footage_dir else None
        return data


def load_config(
    *,
    sampling: Optional[SamplingConfig] = None,
    audio: Optional[AudioConfig] = None,
    **overrides,
) -> tuple[EditingConfig, SamplingConfig, AudioConfig]:
    """Build the (config, sampling) pair the whole layer is threaded with.

    Environment first, explicit keyword overrides second, so the CLI can beat
    the environment without every command having to know every field.
    """
    config = EditingConfig.from_env()
    clean = {k: v for k, v in overrides.items() if v is not None}
    if clean:
        config = replace(config, **clean)
    return (
        config,
        (sampling or SamplingConfig.from_env()).validated(),
        (audio or AudioConfig.from_env()).validated(),
    )

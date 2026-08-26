"""What a transcription job is, and what comes out of one.

Sessions 1-9 could *read* a transcript from five formats and could not make
one. Everything downstream that reasons about story -- objectives, open loops,
callbacks, setup and payoff, half the retention risks -- reads the transcript
and goes quiet without it. So this package is not a convenience: it is the
input the episode layer has been missing.

## The shape, and why it is two shapes

``TranscriptionResult`` is what a speech model produced: segments, word
timings, per-segment probabilities, the language it detected, what it was
unsure about. ``editing.schema.Transcript`` is what the rest of the system
consumes. They are deliberately different objects, and ``as_transcript()`` is
the bridge.

Keeping the rich form is what makes the lossy form trustworthy later. A
downstream pass only needs "these words, at this time"; a person debugging a
mis-aligned caption needs to know the model gave that segment a 0.31
probability and a 0.8 no-speech score. Throwing the second away at write time
would mean re-transcribing to get it back.

## Three rules

* **Timing is never invented.** The same rule the normaliser is built on. A
  segment with no usable time is dropped and counted, never spread evenly
  across the runtime.
* **A mock is never silent.** ``mock`` rides on the result, every artifact it
  writes, and the transcript's own note. A fake transcript that reads as real
  is worse than no transcript, because every story finding built on it would
  look sound.
* **Confidence is a statement about the audio.** ``avg_logprob`` and
  ``no_speech_prob`` are what the model reported; they are converted, capped
  and kept, never rounded up into certainty.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence

from editing.schema import (
    Transcript, TranscriptEntry, _slug, as_bool, as_float, as_str_list,
    clamp01, short_hash,
)

#: Backends this package can drive. ``mock`` is for tests and for exercising
#: the pipeline on a machine with no model -- it is never silent about it.
BACKENDS = ("faster_whisper", "mock")

#: Whisper model sizes, smallest first. Not a closed vocabulary -- a local
#: directory or a CTranslate2 conversion is a legitimate value -- but anything
#: outside this list gets a warning, because a typo'd model name otherwise
#: fails deep inside the loader with a download error.
KNOWN_MODELS = (
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3",
    "distil-small.en", "distil-medium.en", "distil-large-v3",
)

DEVICES = ("auto", "cuda", "cpu")

COMPUTE_TYPES = ("auto", "float16", "float32", "int8", "int8_float16",
                 "int8_float32", "bfloat16")

#: Media this package will hand to a backend. Video included: faster-whisper
#: decodes with PyAV and usually reads a container directly, so extracting
#: audio first is a fallback rather than the normal path.
AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus",
                    ".wma")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts",
                    ".m2ts", ".mxf", ".wmv", ".flv")
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS + VIDEO_EXTENSIONS

#: What a job can be. ``cached`` and ``skipped`` are different outcomes: the
#: first reused a real result, the second never needed one.
JOB_STATUSES = ("pending", "running", "done", "cached", "skipped", "failed")

#: Where a transcription can go wrong. Each one has a different fix, which is
#: why they are separate rather than one "error".
FAILURE_STAGES = (
    "config",          # the settings themselves are unusable
    "missing_backend", # faster-whisper is not installed
    "missing_ffmpeg",  # audio extraction needed it and it is not there
    "read_media",      # the file is not media, or is unreadable
    "extract_audio",   # FFmpeg ran and produced nothing usable
    "load_model",      # the model could not be loaded onto the device
    "decode",          # the model ran and failed part-way
    "empty",           # it ran, and found no speech at all
    "write",           # the result could not be saved
    "unknown",
)

#: How faster-whisper is installed, quoted verbatim so the error message is
#: something a person can paste.
INSTALL_HINT = (
    "pip install faster-whisper    (CPU works; for GPU also install a CUDA "
    "build of PyTorch or the cuDNN/cuBLAS runtime CTranslate2 needs)"
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def _dicts(value: Any) -> list[dict]:
    """The dict members of ``value``, or nothing.

    Every ``from_dict`` in this package is reachable from a file some other
    process wrote -- a cache entry truncated by a power loss, a job folder
    edited by hand. A string where a list was expected iterates as characters,
    and each character used to reach a ``from_dict`` that called ``.get`` on
    it. Guarding here means a malformed file degrades to a cache miss instead
    of an AttributeError from four frames down.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env(name: str, default: str) -> str:
    import os
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    import os
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class TranscriptionConfig:
    """Everything that decides what a transcription produces.

    Frozen, and serialised whole into the cache key, so changing any field
    correctly re-transcribes rather than silently mixing two settings in one
    timeline. That is the same rule ``SamplingConfig`` follows for vision, and
    for the same reason.

    ``model``
        Whisper size, or a path to a local CTranslate2 model. ``small`` is the
        default because it is the smallest that reliably handles fast, excited
        game commentary; ``base`` is roughly twice as quick and drops proper
        nouns.
    ``device`` / ``compute_type``
        ``auto`` resolves at load time -- CUDA when it is genuinely usable,
        CPU otherwise, with a compute type that suits whichever won. Resolution
        is recorded on the result so a slow run is explainable afterwards.
    ``language``
        Empty means detect. Setting it is worth doing when you know: detection
        costs a pass over the first thirty seconds and occasionally guesses
        wrong on a quiet intro.
    ``vad_filter``
        Drops silence before decoding. On by default: game capture has long
        quiet stretches, and Whisper hallucinates confident nonsense into
        silence more than it does anything else.
    ``word_timestamps``
        Per-word timing. Costs roughly 10-15% more time and is what makes
        caption timing and phrase-level cutting possible later.
    ``initial_prompt``
        Vocabulary hint. Whisper mis-hears domain nouns constantly -- "creeper"
        as "creature", "nether" as "never" -- and a short prompt naming them
        fixes most of it. Empty by default because a wrong hint biases output.
    """

    backend: str = "faster_whisper"
    model: str = "small"
    device: str = "auto"
    compute_type: str = "auto"
    language: str = ""
    beam_size: int = 5
    vad_filter: bool = True
    word_timestamps: bool = True
    #: Below this, a segment is kept but flagged rather than trusted.
    min_segment_confidence: float = 0.25
    #: Segments whose no-speech probability is at or above this are dropped.
    max_no_speech: float = 0.80
    initial_prompt: str = ""
    use_cache: bool = True
    #: Seconds before one file's transcription is abandoned. A 40-minute
    #: capture on CPU with ``small`` is comfortably inside this.
    timeout: float = 3600.0

    @classmethod
    def from_env(cls) -> "TranscriptionConfig":
        return cls(
            backend=_env("EDITING_TRANSCRIBE_BACKEND", "faster_whisper"),
            model=_env("EDITING_WHISPER_MODEL", "small"),
            device=_env("EDITING_WHISPER_DEVICE", "auto"),
            compute_type=_env("EDITING_WHISPER_COMPUTE", "auto"),
            language=_env("EDITING_WHISPER_LANGUAGE", ""),
            beam_size=_env_int("EDITING_WHISPER_BEAM", 5),
            vad_filter=_env_bool("EDITING_WHISPER_VAD", True),
            word_timestamps=_env_bool("EDITING_WHISPER_WORDS", True),
            initial_prompt=_env("EDITING_WHISPER_PROMPT", ""),
        )

    def validated(self) -> "TranscriptionConfig":
        """Clamp to values a backend can honour. Never raises.

        Returned rather than raised for the same reason ``SamplingConfig`` does
        it: a nonsensical environment variable should degrade to something that
        works, not stop an overnight batch. The clamped values are what land in
        the cache key, so two configs that clamp to the same thing correctly
        share an entry.
        """
        from dataclasses import replace

        return replace(
            self,
            backend=coerce_one(self.backend, BACKENDS, "faster_whisper"),
            model=str(self.model or "small").strip() or "small",
            device=coerce_one(self.device, DEVICES, "auto"),
            compute_type=coerce_one(self.compute_type, COMPUTE_TYPES, "auto"),
            language=str(self.language or "").strip().lower()[:8],
            beam_size=max(1, min(int(as_float(self.beam_size, 5)), 10)),
            min_segment_confidence=clamp01(self.min_segment_confidence, 0.25),
            max_no_speech=clamp01(self.max_no_speech, 0.80),
            initial_prompt=_text(self.initial_prompt, 400),
            timeout=max(30.0, as_float(self.timeout, 3600.0)),
        )

    @property
    def warnings(self) -> list[str]:
        """Things worth saying about these settings before a long batch."""
        out: list[str] = []
        if self.model not in KNOWN_MODELS and "/" not in self.model \
                and "\\" not in self.model:
            out.append(
                f"'{self.model}' is not a known Whisper size; if it is not a "
                "local model directory the loader will try to download it. "
                "Known sizes: " + ", ".join(KNOWN_MODELS[:8]) + ", ..."
            )
        if self.model.startswith("large") and self.device == "cpu":
            out.append(
                "a 'large' model on CPU runs roughly 10x slower than realtime; "
                "'small' is the usual choice for a whole episode"
            )
        if not self.vad_filter:
            out.append(
                "VAD is off, so silence is decoded too -- Whisper tends to "
                "hallucinate confident text into quiet stretches"
            )
        return out

    def cache_key_part(self) -> dict:
        """The subset of this config that changes the transcript.

        ``use_cache`` and ``timeout`` are deliberately absent: neither changes
        a single word of the output, and including them would make turning the
        cache off invalidate everything already in it.
        """
        clean = self.validated()
        return {
            "backend": clean.backend,
            "model": clean.model,
            "device": clean.device,
            "compute_type": clean.compute_type,
            "language": clean.language,
            "beam_size": clean.beam_size,
            "vad_filter": clean.vad_filter,
            "word_timestamps": clean.word_timestamps,
            "max_no_speech": clean.max_no_speech,
            "initial_prompt": clean.initial_prompt,
        }

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "TranscriptionConfig":
        data = data or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------

@dataclass
class TranscriptWord:
    """One word with its own timing. Present only when asked for."""

    word: str = ""
    start: float = 0.0
    end: float = 0.0
    probability: float = 1.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {
            "word": self.word,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "probability": round(self.probability, 4),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptWord":
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        return cls(
            word=str(data.get("word") or "")[:120],
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            probability=clamp01(data.get("probability", 1.0), 1.0),
        )


@dataclass
class TranscriptSegment:
    """One utterance, with everything the model said about it.

    ``source_file``, ``language`` and ``model`` are denormalised onto every
    segment on purpose. A segment quoted in a report, pasted into an issue or
    pulled out of a batch is then self-describing, and the duplication costs
    about a hundred bytes against a file that is already tens of kilobytes.
    """

    index: int = 0
    start: float = 0.0
    end: float = 0.0
    text: str = ""
    #: 0..1, derived from the model's average log-probability.
    confidence: float = 1.0
    #: How likely the model thought this was *not* speech.
    no_speech_prob: float = 0.0
    #: Always ``None`` today: nothing here does diarisation, and inventing a
    #: speaker label would be worse than admitting there is none.
    speaker: Optional[str] = None
    words: list[TranscriptWord] = field(default_factory=list)

    source_file: str = ""
    language: str = ""
    model: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < 0.5

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def as_entry(self) -> TranscriptEntry:
        """The lossy form the rest of the system consumes."""
        return TranscriptEntry(
            start=self.start,
            end=self.end,
            text=self.text,
            speaker=self.speaker or "",
            confidence=clamp01(self.confidence, 1.0),
            source_ref=self.source_file,
        )

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "no_speech_prob": round(self.no_speech_prob, 4),
            "speaker": self.speaker,
            "words": [word.to_dict() for word in self.words],
            "source_file": self.source_file,
            "language": self.language,
            "model": self.model,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptSegment":
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        speaker = data.get("speaker")
        return cls(
            index=int(as_float(data.get("index"))),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            text=_text(data.get("text"), 4000),
            confidence=clamp01(data.get("confidence", 1.0), 1.0),
            no_speech_prob=clamp01(data.get("no_speech_prob"), 0.0),
            speaker=(str(speaker) if speaker else None),
            words=[
                TranscriptWord.from_dict(word)
                for word in _dicts(data.get("words"))
            ],
            source_file=_text(data.get("source_file"), 500),
            language=_text(data.get("language"), 16),
            model=_text(data.get("model"), 120),
            warnings=as_str_list(data.get("warnings"), limit=20),
        )


@dataclass
class TranscriptionFailure:
    """Why a transcription did not happen, and what to do about it.

    A record rather than an exception once it reaches a batch: one unreadable
    file must not cost the other thirty, and "what went wrong with clip_07"
    has to still be answerable at the end of the run.
    """

    stage: str = "unknown"
    code: str = "transcription_failed"
    message: str = ""
    hint: str = ""
    path: str = ""
    #: Whether re-running could plausibly work -- after installing something,
    #: or on a machine with a GPU. A corrupt file is not recoverable.
    recoverable: bool = True
    detail: dict = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"{self.stage}: {self.message}"]
        if self.hint:
            lines.append(f"  fix : {self.hint}")
        if self.path:
            lines.append(f"  file: {self.path}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "path": self.path,
            "recoverable": self.recoverable,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["TranscriptionFailure"]:
        if not data:
            return None
        return cls(
            stage=coerce_one(data.get("stage"), FAILURE_STAGES, "unknown"),
            code=_text(data.get("code"), 60) or "transcription_failed",
            message=_text(data.get("message"), 1000),
            hint=_text(data.get("hint"), 1000),
            path=_text(data.get("path"), 500),
            recoverable=as_bool(data.get("recoverable"), True),
            detail=dict(data.get("detail") or {}),
        )


#: Punctuation that ends a spoken sentence. Whisper punctuates, so this is
#: reliable enough to split on -- and where it is not, the length guard below
#: catches the leftover.
_SENTENCE_END = (".", "!", "?")

#: A segment longer than this is a paragraph, not a line. Every pass
#: downstream that puts text on screen refuses one, so a transcript full of
#: them is a transcript nothing can caption.
LONG_SEGMENT_SECONDS = 6.0

#: Never split into a piece shorter than this: a two-word fragment with its
#: own timestamp is noise, not a line.
MIN_SPLIT_SECONDS = 0.6


def split_at_sentences(segment: "TranscriptSegment") -> list:
    """Break one long segment into sentence-length ones, using word timings.

    Whisper returns whatever it decoded in one window as a single segment, so
    real speech routinely arrives as an eight-second run-on: "Yeah, sure buddy.
    I didn't do it. I didn't do anything." Every caption rule downstream
    measures a *line*, so that arrives as one unreadable paragraph and is
    refused -- which is why a perfectly good transcript can produce no captions
    at all.

    The word timings needed to fix it were already being collected and then
    dropped on the way out. This uses them: each sentence keeps the timing of
    its own first and last word, so the split pieces are as accurate as the
    words were.

    Returns ``[segment]`` unchanged when there is nothing to gain -- the
    segment is short, has no word timings, or is a single sentence.
    """
    if segment.duration <= LONG_SEGMENT_SECONDS or not segment.words:
        return [segment]

    groups: list = []
    current: list = []
    for word in segment.words:
        current.append(word)
        if str(word.word).strip().endswith(_SENTENCE_END):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    if len(groups) < 2:
        return [segment]

    # Merge away pieces too short to stand on their own, so "No." does not
    # become its own line with its own timestamp.
    merged: list = []
    for group in groups:
        span = group[-1].end - group[0].start
        if merged and span < MIN_SPLIT_SECONDS:
            merged[-1].extend(group)
        else:
            merged.append(list(group))
    if len(merged) < 2:
        return [segment]

    out: list = []
    for index, group in enumerate(merged):
        text = "".join(word.word for word in group).strip()
        if not text:
            continue
        out.append(replace(
            segment,
            index=segment.index,
            start=float(group[0].start),
            end=float(group[-1].end),
            text=text,
            words=list(group),
            warnings=list(segment.warnings) + [
                f"split from a {segment.duration:.1f}s segment "
                f"({index + 1} of {len(merged)})"
            ],
        ))
    return out or [segment]


@dataclass
class TranscriptionResult:
    """Everything one transcription produced.

    ``mock`` is load-bearing. A fake transcript that reads as real is worse
    than no transcript, because every story finding built on it would look
    sound. So it rides on the result, on every file written from it, and on
    the transcript's own note.
    """

    job_id: str = ""
    source_path: str = ""
    asset_id: str = ""

    backend: str = "faster_whisper"
    model: str = ""
    #: What ``auto`` actually resolved to. Recorded because "why did this take
    #: forty minutes" is answered by this field and nothing else.
    device: str = ""
    compute_type: str = ""

    language: str = ""
    language_probability: float = 0.0
    #: Runtime of the media, as the backend saw it.
    duration: float = 0.0

    segments: list[TranscriptSegment] = field(default_factory=list)
    word_timestamps: bool = False
    mock: bool = False

    created_at: str = ""
    elapsed: float = 0.0
    #: True when this came back from the cache rather than a model.
    cached: bool = False
    #: The config it was produced with, for reproducibility.
    config: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Segments dropped for having no usable timing or being pure silence.
    dropped_segments: int = 0
    schema_version: int = 1

    # -- derived ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def is_empty(self) -> bool:
        return not self.segments

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text).strip()

    @property
    def word_count(self) -> int:
        return sum(segment.word_count for segment in self.segments)

    @property
    def speech_seconds(self) -> float:
        return round(sum(segment.duration for segment in self.segments), 3)

    @property
    def speech_share(self) -> float:
        """How much of the runtime is speech. A sanity check, not a metric.

        A game capture with commentary usually lands between 0.3 and 0.7. Near
        zero means the transcription found almost nothing, which is worth
        saying out loud before a whole pipeline is built on it.
        """
        return round(self.speech_seconds / self.duration, 3) \
            if self.duration > 0 else 0.0

    @property
    def realtime_factor(self) -> float:
        """Media seconds transcribed per wall-clock second.

        The number that answers "how long will my 40-minute episode take",
        which is otherwise guesswork: above 1.0 is faster than realtime. Zero
        for a cached result, because no decoding happened.
        """
        if self.cached or self.elapsed <= 0 or self.duration <= 0:
            return 0.0
        return round(self.duration / self.elapsed, 2)

    @property
    def mean_confidence(self) -> float:
        if not self.segments:
            return 0.0
        return round(
            sum(s.confidence for s in self.segments) / len(self.segments), 4)

    @property
    def low_confidence_segments(self) -> int:
        return sum(1 for s in self.segments if s.is_low_confidence)

    def stats(self) -> dict:
        return {
            "segments": len(self.segments),
            "words": self.word_count,
            "duration": round(self.duration, 2),
            "speech_seconds": self.speech_seconds,
            "speech_share": self.speech_share,
            "mean_confidence": self.mean_confidence,
            "realtime_factor": self.realtime_factor,
            "low_confidence_segments": self.low_confidence_segments,
            "dropped_segments": self.dropped_segments,
            "language": self.language,
            "word_timestamps": self.word_timestamps,
            "cached": self.cached,
            "mock": self.mock,
            "elapsed": round(self.elapsed, 2),
        }

    def as_transcript(self, *, asset_id: str = "") -> Transcript:
        """The bridge to what every other pass consumes.

        ``source`` is ``whisper`` rather than ``json`` so a later reader can
        tell a machine transcription from one a person exported by hand -- they
        deserve different amounts of trust, and Session 2's audio layer already
        weights a human ``[laughs]`` marker above its own guess.
        """
        note = "Transcribed locally by faster-whisper."
        if self.mock:
            note = ("MOCK TRANSCRIPT -- generated without a speech model. "
                    "Every word here is fabricated and must not be trusted.")
        return Transcript(
            asset_id=asset_id or self.asset_id,
            source="whisper",
            source_path=self.source_path,
            language=self.language,
            entries=[
                piece.as_entry()
                for segment in self.segments
                for piece in split_at_sentences(segment)
            ],
            created_at=self.created_at or now(),
            note=note,
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "source_path": self.source_path,
            "asset_id": self.asset_id,
            "backend": self.backend,
            "model": self.model,
            "device": self.device,
            "compute_type": self.compute_type,
            "language": self.language,
            "language_probability": round(self.language_probability, 4),
            "duration": round(self.duration, 3),
            "created_at": self.created_at,
            "elapsed": round(self.elapsed, 3),
            "cached": self.cached,
            "mock": self.mock,
            "word_timestamps": self.word_timestamps,
            "dropped_segments": self.dropped_segments,
            "config": dict(self.config),
            "stats": self.stats(),
            "warnings": list(self.warnings),
            # ``segments`` is the key the existing normaliser looks for, so
            # this file parses with ``transcripts.normalize.parse_json``
            # without a bridge. ``entries`` carries the canonical shape for
            # anything that would rather have that.
            "segments": [segment.to_dict() for segment in self.segments],
            "entries": [
                segment.as_entry().to_dict() for segment in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptionResult":
        data = data or {}
        return cls(
            job_id=_text(data.get("job_id"), 120),
            source_path=_text(data.get("source_path"), 500),
            asset_id=_text(data.get("asset_id"), 120),
            backend=coerce_one(data.get("backend"), BACKENDS, "faster_whisper"),
            model=_text(data.get("model"), 120),
            device=_text(data.get("device"), 20),
            compute_type=_text(data.get("compute_type"), 30),
            language=_text(data.get("language"), 16),
            language_probability=clamp01(data.get("language_probability"), 0.0),
            duration=as_float(data.get("duration")),
            segments=[
                TranscriptSegment.from_dict(item)
                for item in _dicts(data.get("segments"))
            ],
            word_timestamps=as_bool(data.get("word_timestamps")),
            mock=as_bool(data.get("mock")),
            created_at=_text(data.get("created_at"), 40),
            elapsed=as_float(data.get("elapsed")),
            cached=as_bool(data.get("cached")),
            config=dict(data.get("config") or {}),
            warnings=as_str_list(data.get("warnings"), limit=60),
            dropped_segments=int(as_float(data.get("dropped_segments"))),
        )


# ---------------------------------------------------------------------------
# Jobs and batches
# ---------------------------------------------------------------------------

def job_id_for(asset_id: str, source_path: str, cache_key: str) -> str:
    """A job ID that is stable for one file and one configuration.

    Deliberately *not* timestamped. Re-transcribing the same file with the same
    settings should land in the same folder and overwrite it, because it is the
    same answer; a timestamp would leave a trail of identical folders and make
    ``transcribe show`` a guessing game. Changing any setting changes the cache
    key, and therefore the folder.
    """
    from pathlib import Path

    stem = _slug(Path(str(source_path)).stem)[:28] or (asset_id[:8] or "clip")
    return f"{stem}-{short_hash(cache_key, length=8)}"


@dataclass
class TranscriptionJob:
    """One file, one configuration, and what happened.

    Written to the job folder so a batch is inspectable file by file
    afterwards, rather than only through the summary.
    """

    job_id: str = ""
    source_path: str = ""
    asset_id: str = ""
    status: str = "pending"
    config: TranscriptionConfig = field(default_factory=TranscriptionConfig)

    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    elapsed: float = 0.0

    output_dir: str = ""
    cache_key: str = ""
    result: Optional[TranscriptionResult] = None
    failure: Optional[TranscriptionFailure] = None
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    @property
    def ok(self) -> bool:
        return self.status in ("done", "cached", "skipped")

    @property
    def produced_transcript(self) -> bool:
        return self.status in ("done", "cached") and self.result is not None

    def line(self) -> str:
        mark = {"done": "+", "cached": "=", "skipped": ".", "failed": "x",
                "running": ">", "pending": " "}.get(self.status, "?")
        from pathlib import Path
        name = Path(self.source_path).name if self.source_path else "(no file)"
        detail = ""
        if self.result is not None:
            detail = (f"{len(self.result)} segment(s), "
                      f"{self.result.word_count} word(s)")
        elif self.failure is not None:
            detail = self.failure.message[:60]
        return f"{mark} {name[:38]:<38} {self.status:<8} {detail}"

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "source_path": self.source_path,
            "asset_id": self.asset_id,
            "status": self.status,
            "config": self.config.to_dict(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed": round(self.elapsed, 3),
            "output_dir": self.output_dir,
            "cache_key": self.cache_key,
            "stats": self.result.stats() if self.result else {},
            "failure": self.failure.to_dict() if self.failure else None,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptionJob":
        data = data or {}
        return cls(
            job_id=_text(data.get("job_id"), 120),
            source_path=_text(data.get("source_path"), 500),
            asset_id=_text(data.get("asset_id"), 120),
            status=coerce_one(data.get("status"), JOB_STATUSES, "pending"),
            config=TranscriptionConfig.from_dict(data.get("config")),
            created_at=_text(data.get("created_at"), 40),
            started_at=_text(data.get("started_at"), 40),
            ended_at=_text(data.get("ended_at"), 40),
            elapsed=as_float(data.get("elapsed")),
            output_dir=_text(data.get("output_dir"), 500),
            cache_key=_text(data.get("cache_key"), 80),
            failure=TranscriptionFailure.from_dict(data.get("failure")),
            warnings=as_str_list(data.get("warnings"), limit=60),
        )


@dataclass
class TranscriptionCacheEntry:
    """What was cached, and everything that decided the key.

    Stored beside the payload so a cache entry is auditable without recomputing
    the key. "Why did this not hit?" is a question people ask constantly, and
    a stored key with its parts answers it by inspection.
    """

    key: str = ""
    created_at: str = ""
    source_path: str = ""
    content_hash: str = ""
    size_bytes: int = 0
    model: str = ""
    backend: str = ""
    language: str = ""
    word_timestamps: bool = False
    vad_filter: bool = False
    beam_size: int = 5
    compute_type: str = ""
    segments: int = 0
    schema_version: int = 1

    @classmethod
    def describe(
        cls, key: str, *, fingerprint, config: TranscriptionConfig,
        result: Optional[TranscriptionResult] = None,
    ) -> "TranscriptionCacheEntry":
        clean = config.validated()
        return cls(
            key=key,
            created_at=now(),
            source_path=getattr(fingerprint, "path", ""),
            content_hash=getattr(fingerprint, "content_hash", ""),
            size_bytes=int(getattr(fingerprint, "size_bytes", 0) or 0),
            model=clean.model,
            backend=clean.backend,
            language=clean.language,
            word_timestamps=clean.word_timestamps,
            vad_filter=clean.vad_filter,
            beam_size=clean.beam_size,
            compute_type=clean.compute_type,
            segments=len(result) if result else 0,
        )

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptionCacheEntry":
        data = data or {}
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class TranscriptionBatch:
    """A folder's worth of jobs, and the one-screen answer about them.

    A batch never raises for a file that failed. Thirty clips where two are
    corrupt is a normal afternoon, and the useful output is twenty-eight
    transcripts plus a precise account of the two.
    """

    batch_id: str = ""
    root: str = ""
    created_at: str = ""
    finished_at: str = ""
    config: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    jobs: list[TranscriptionJob] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.jobs)

    def of_status(self, status: str) -> list[TranscriptionJob]:
        return [job for job in self.jobs if job.status == status]

    @property
    def failed(self) -> list[TranscriptionJob]:
        return self.of_status("failed")

    @property
    def elapsed(self) -> float:
        return round(sum(job.elapsed for job in self.jobs), 3)

    def stats(self) -> dict:
        return {
            "files": len(self.jobs),
            "done": len(self.of_status("done")),
            "cached": len(self.of_status("cached")),
            "skipped": len(self.of_status("skipped")),
            "failed": len(self.of_status("failed")),
            "segments": sum(
                len(job.result) for job in self.jobs if job.result),
            "words": sum(
                job.result.word_count for job in self.jobs if job.result),
            "media_seconds": round(sum(
                job.result.duration for job in self.jobs if job.result), 2),
            "elapsed": self.elapsed,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "batch_id": self.batch_id,
            "root": self.root,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "config": self.config.to_dict(),
            "stats": self.stats(),
            "jobs": [job.to_dict() for job in self.jobs],
            "failures": [
                {"file": job.source_path,
                 **(job.failure.to_dict() if job.failure else {})}
                for job in self.failed
            ],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptionBatch":
        data = data or {}
        return cls(
            batch_id=_text(data.get("batch_id"), 120),
            root=_text(data.get("root"), 500),
            created_at=_text(data.get("created_at"), 40),
            finished_at=_text(data.get("finished_at"), 40),
            config=TranscriptionConfig.from_dict(data.get("config")),
            jobs=[
                TranscriptionJob.from_dict(item)
                for item in _dicts(data.get("jobs"))
            ],
            warnings=as_str_list(data.get("warnings"), limit=60),
        )

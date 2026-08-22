"""Driving a speech model, and the one place this package imports one.

Two backends behind one interface:

``FasterWhisperBackend``
    The real thing. Loads a CTranslate2 Whisper model locally, decodes, and
    returns segments with per-word timing when asked. No network, no API key,
    nothing sent anywhere.
``MockBackend``
    Deterministic fake, for tests and for exercising the pipeline on a machine
    with no model installed. **Every result it produces is stamped
    ``mock=True``**, which rides through the result, the files written from it
    and the transcript's own note. A fake transcript that reads as real would
    make every story finding built on it look sound, which is worse than
    having no transcript at all.

## The import is deliberately late

``faster_whisper`` is imported inside ``load()``, not at module scope. That is
what lets the whole package -- schema, cache keys, formats, the CLI, the tests
-- work on a machine where it was never installed, and it is why the test suite
needs no GPU. A missing package becomes a typed error carrying the exact pip
command, at the moment it is actually needed.

## Loading is expensive; the model is held

A Whisper model takes seconds to load and hundreds of megabytes of RAM. A batch
of thirty clips must not pay that thirty times, so a backend instance holds its
model and is reused across a batch. It is not thread-safe and is not meant to
be: transcription is already saturating the device.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from editing.errors import EditingError, ToolMissingError
from editing.transcribe.schema import (
    INSTALL_HINT, TranscriptSegment, TranscriptWord, TranscriptionConfig,
    TranscriptionResult, now,
)

logger = logging.getLogger("nova.editing.transcribe.backends")

#: Log-probability at or below which a segment is treated as worthless. Whisper
#: reports ``avg_logprob`` in roughly ``[-1.5, 0]`` for real speech; anything
#: under this is usually a hallucination into noise.
_MIN_LOGPROB = -1.6


def confidence_from_logprob(avg_logprob: Any) -> float:
    """Turn Whisper's average log-probability into a 0..1 confidence.

    A linear remap of ``[-1.6, 0]`` rather than ``exp()``. Exponentiating is
    defensible and compresses everything usable into ``0.6..1.0``, which makes
    the number useless for ranking -- and ranking by confidence is the only
    thing this value is for. Nothing downstream treats it as a probability.
    """
    try:
        value = float(avg_logprob)
    except (TypeError, ValueError):
        return 1.0
    if value != value:                      # NaN
        return 1.0
    if value >= 0.0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (value / _MIN_LOGPROB)))


class TranscriptionBackend:
    """What a backend has to do. Two methods, one of them optional."""

    name = "backend"

    def health(self) -> dict:
        """Whether this backend could run right now, without running it."""
        raise NotImplementedError

    def transcribe(
        self, path: str | Path, *, config: TranscriptionConfig,
        progress=None,
    ) -> TranscriptionResult:
        raise NotImplementedError

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# faster-whisper
# ---------------------------------------------------------------------------

class FasterWhisperBackend(TranscriptionBackend):
    """Local Whisper via CTranslate2. Nothing leaves the machine."""

    name = "faster_whisper"

    def __init__(self, config: Optional[TranscriptionConfig] = None):
        self.config = (config or TranscriptionConfig()).validated()
        self._model = None
        self._loaded_with: tuple = ()
        #: What ``auto`` resolved to, once something has been loaded.
        self.device = ""
        self.compute_type = ""

    # -- availability ----------------------------------------------------

    @staticmethod
    def installed() -> bool:
        try:
            import faster_whisper  # noqa: F401
        except Exception:  # noqa: BLE001 - any import failure means unusable
            return False
        return True

    @staticmethod
    def cuda_available() -> bool:
        """Whether CUDA is genuinely usable, not merely present.

        Checked through torch when it is installed, because a machine can have
        a CUDA driver and still fail to load a model -- and finding that out
        after a forty-minute batch has started is the worst time to find out.
        Absence of torch is not evidence either way, so it is treated as "no"
        and CPU is chosen, which always works.
        """
        try:
            import torch
            return bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001
            return False

    def health(self) -> dict:
        available = self.installed()
        return {
            "backend": self.name,
            "installed": available,
            "cuda": self.cuda_available() if available else False,
            "model": self.config.model,
            "resolved_device": self.device or self._resolve_device(),
            "hint": "" if available else INSTALL_HINT,
        }

    # -- device resolution ----------------------------------------------

    def _resolve_device(self) -> str:
        if self.config.device != "auto":
            return self.config.device
        return "cuda" if self.cuda_available() else "cpu"

    def _resolve_compute(self, device: str) -> str:
        if self.config.compute_type != "auto":
            return self.config.compute_type
        # int8 on CPU is roughly 3x faster than float32 at a quality cost that
        # does not show up in commentary transcription; float16 is the normal
        # choice on any CUDA card that supports it.
        return "float16" if device == "cuda" else "int8"

    # -- the model -------------------------------------------------------

    def load(self):
        """Load the model, or explain precisely why it cannot be loaded."""
        device = self._resolve_device()
        compute = self._resolve_compute(device)
        signature = (self.config.model, device, compute)
        if self._model is not None and self._loaded_with == signature:
            return self._model

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ToolMissingError(
                "faster-whisper is not installed, so nothing can be "
                "transcribed",
                hint=INSTALL_HINT,
                detail={"package": "faster-whisper", "import_error": str(exc)},
            ) from exc

        started = time.time()
        try:
            model = WhisperModel(
                self.config.model, device=device, compute_type=compute)
        except Exception as exc:  # noqa: BLE001 - loader raises many types
            # A CUDA load failing on a machine that reported CUDA available is
            # common enough (driver/cuDNN mismatch) that falling back is worth
            # more than being right about the cause.
            if device == "cuda":
                logger.warning(
                    "CUDA load of '%s' failed (%s); falling back to CPU",
                    self.config.model, exc,
                )
                device, compute = "cpu", self._resolve_compute("cpu")
                try:
                    model = WhisperModel(
                        self.config.model, device=device, compute_type=compute)
                except Exception as inner:  # noqa: BLE001
                    raise _load_error(self.config, device, inner) from inner
            else:
                raise _load_error(self.config, device, exc) from exc

        self._model = model
        self._loaded_with = (self.config.model, device, compute)
        self.device, self.compute_type = device, compute
        logger.info(
            "Loaded Whisper '%s' on %s/%s in %.1fs",
            self.config.model, device, compute, time.time() - started,
        )
        return model

    def close(self) -> None:
        self._model = None
        self._loaded_with = ()

    # -- transcription ---------------------------------------------------

    def transcribe(
        self, path: str | Path, *, config: Optional[TranscriptionConfig] = None,
        progress=None,
    ) -> TranscriptionResult:
        """Decode one file. Raises only for things a person can act on."""
        settings = (config or self.config).validated()
        self.config = settings
        model = self.load()
        source = Path(path)
        started = time.time()

        try:
            segments_iter, info = model.transcribe(
                str(source),
                language=settings.language or None,
                beam_size=settings.beam_size,
                vad_filter=settings.vad_filter,
                word_timestamps=settings.word_timestamps,
                initial_prompt=settings.initial_prompt or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise EditingError(
                f"Whisper could not read '{source.name}'",
                hint="Check the file plays, and that FFmpeg is installed if "
                     "it is an unusual container. `transcribe file <path> "
                     "--extract-audio` converts it to WAV first.",
                detail={"path": str(source), "reason": str(exc)},
            ) from exc

        result = TranscriptionResult(
            source_path=str(source),
            backend=self.name,
            model=settings.model,
            device=self.device,
            compute_type=self.compute_type,
            language=str(getattr(info, "language", "") or ""),
            language_probability=float(
                getattr(info, "language_probability", 0.0) or 0.0),
            duration=float(getattr(info, "duration", 0.0) or 0.0),
            word_timestamps=settings.word_timestamps,
            created_at=now(),
            config=settings.to_dict(),
        )

        # ``segments_iter`` is a generator: decoding happens as it is walked,
        # which is what makes progress reporting possible at all.
        for index, raw in enumerate(segments_iter):
            segment = _segment_from(raw, index, source, result, settings)
            if segment is None:
                result.dropped_segments += 1
                continue
            result.segments.append(segment)
            if progress is not None:
                progress(len(result.segments), result.duration, segment)

        result.elapsed = round(time.time() - started, 3)
        _add_warnings(result, settings)
        return result


def _load_error(
    config: TranscriptionConfig, device: str, exc: Exception
) -> EditingError:
    """Turn a loader failure into something with a fix attached."""
    reason = str(exc)
    lowered = reason.lower()
    if "cudnn" in lowered or "cublas" in lowered or "libcu" in lowered:
        hint = ("The CUDA runtime libraries CTranslate2 needs are missing. "
                "Install a CUDA build of PyTorch, or run on CPU with "
                "`--device cpu`.")
    elif "out of memory" in lowered:
        hint = (f"Not enough VRAM for '{config.model}' on {device}. Try "
                "`--model small`, or `--device cpu`.")
    elif "no such file" in lowered or "not a valid" in lowered \
            or "repository" in lowered or "404" in lowered:
        hint = (f"'{config.model}' is not a known model size or a local model "
                "directory. Try `--model small`.")
    else:
        hint = (f"Loading '{config.model}' on {device} failed. `--device cpu` "
                "and `--model small` is the combination most likely to work.")
    return EditingError(
        f"Could not load Whisper model '{config.model}' on {device}",
        hint=hint,
        detail={"model": config.model, "device": device, "reason": reason},
    )


def _segment_from(
    raw: Any, index: int, source: Path, result: TranscriptionResult,
    settings: TranscriptionConfig,
) -> Optional[TranscriptSegment]:
    """One backend segment, or ``None`` when it should not be kept.

    Two reasons to drop: no usable timing, and a no-speech probability over the
    ceiling. Both are counted on the result rather than passed over silently --
    "it produced 40 segments and dropped 300" is the single most useful thing
    to know when a transcript comes out wrong.
    """
    start = float(getattr(raw, "start", 0.0) or 0.0)
    end = float(getattr(raw, "end", 0.0) or 0.0)
    text = str(getattr(raw, "text", "") or "").strip()
    if not text or end <= start:
        return None

    no_speech = float(getattr(raw, "no_speech_prob", 0.0) or 0.0)
    if no_speech >= settings.max_no_speech:
        return None

    confidence = confidence_from_logprob(getattr(raw, "avg_logprob", None))
    words: list[TranscriptWord] = []
    for word in (getattr(raw, "words", None) or ()):
        word_start = float(getattr(word, "start", 0.0) or 0.0)
        word_end = float(getattr(word, "end", word_start) or word_start)
        token = str(getattr(word, "word", "") or "")
        if not token.strip() or word_end < word_start:
            continue
        words.append(TranscriptWord(
            word=token,
            start=word_start,
            end=word_end,
            probability=float(getattr(word, "probability", 1.0) or 1.0),
        ))

    warnings: list[str] = []
    if confidence < settings.min_segment_confidence:
        warnings.append(
            f"low confidence ({confidence:.2f}); the audio may be unclear here")
    if no_speech > 0.5:
        warnings.append(
            f"the model was {no_speech:.0%} sure this was not speech")

    return TranscriptSegment(
        index=index,
        start=max(0.0, start),
        end=max(max(0.0, start), end),
        text=text,
        confidence=confidence,
        no_speech_prob=no_speech,
        speaker=None,
        words=words,
        source_file=str(source),
        language=result.language,
        model=settings.model,
        warnings=warnings,
    )


def _add_warnings(
    result: TranscriptionResult, settings: TranscriptionConfig
) -> None:
    """Say what looks wrong about this transcription, on the result itself.

    A transcript that came back nearly empty is the failure mode that costs the
    most downstream, because every story pass silently goes quiet rather than
    complaining. Saying it here means it appears in the report instead.
    """
    result.warnings.extend(settings.warnings)

    if result.is_empty:
        result.warnings.append(
            "no speech was found at all -- check the file has an audio track, "
            "and that the commentary is not on a second track FFmpeg is not "
            "picking up"
        )
        return

    if result.duration > 60 and result.speech_share < 0.05:
        result.warnings.append(
            f"only {result.speech_share:.1%} of the runtime came back as "
            "speech; if this is commentary footage, something is wrong with "
            "the audio track rather than with the transcription"
        )
    if result.dropped_segments > len(result.segments):
        result.warnings.append(
            f"{result.dropped_segments} segment(s) were dropped against "
            f"{len(result.segments)} kept -- mostly silence being decoded as "
            "speech. Leaving VAD on usually fixes this."
        )
    low = result.low_confidence_segments
    if low and low / max(1, len(result.segments)) > 0.3:
        result.warnings.append(
            f"{low} of {len(result.segments)} segments are low confidence; "
            "a larger --model would help if the audio itself is clear"
        )
    if result.language and settings.language and \
            result.language != settings.language:
        result.warnings.append(
            f"detected '{result.language}' but '{settings.language}' was "
            "requested; the requested language was used"
        )


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

#: Deterministic filler. Recognisably not a real transcript at a glance, which
#: is the point -- somebody reading a report should never wonder.
_MOCK_LINES = (
    "MOCK okay so here is the plan for this episode",
    "MOCK we need to find diamonds before it gets dark",
    "MOCK oh no there is a creeper right behind me",
    "MOCK that was close I nearly lost everything",
    "MOCK right lets head back to the base",
    "MOCK this is the part I have been waiting for",
)


class MockBackend(TranscriptionBackend):
    """A deterministic fake that is never mistaken for a transcription.

    Exists so the pipeline, the CLI and the tests can run on a machine with no
    model. Everything it produces is stamped ``mock=True`` and every line is
    prefixed, because the one unacceptable outcome for this package is a
    fabricated transcript that reads as real.
    """

    name = "mock"

    def __init__(self, config: Optional[TranscriptionConfig] = None,
                 *, duration: float = 60.0, segment_seconds: float = 5.0):
        self.config = (config or TranscriptionConfig()).validated()
        self.duration = duration
        self.segment_seconds = max(1.0, segment_seconds)

    @staticmethod
    def installed() -> bool:
        return True

    def health(self) -> dict:
        return {
            "backend": self.name,
            "installed": True,
            "cuda": False,
            "model": "mock",
            "resolved_device": "cpu",
            "hint": "This backend fabricates text. Never use it for a real "
                    "edit.",
        }

    def transcribe(
        self, path: str | Path, *, config: Optional[TranscriptionConfig] = None,
        progress=None,
    ) -> TranscriptionResult:
        settings = (config or self.config).validated()
        source = Path(path)
        started = time.time()

        result = TranscriptionResult(
            source_path=str(source),
            backend=self.name,
            model="mock",
            device="cpu",
            compute_type="int8",
            language=settings.language or "en",
            language_probability=1.0,
            duration=self.duration,
            word_timestamps=settings.word_timestamps,
            mock=True,
            created_at=now(),
            config=settings.to_dict(),
            warnings=[
                "MOCK BACKEND: every line below was fabricated without a "
                "speech model. Nothing derived from this transcript means "
                "anything about the footage."
            ],
        )

        index = 0
        cursor = 0.0
        while cursor + self.segment_seconds <= self.duration:
            text = _MOCK_LINES[index % len(_MOCK_LINES)]
            end = cursor + self.segment_seconds - 0.5
            words = []
            if settings.word_timestamps:
                tokens = text.split()
                span = (end - cursor) / max(1, len(tokens))
                words = [
                    TranscriptWord(
                        word=f" {token}",
                        start=round(cursor + position * span, 3),
                        end=round(cursor + (position + 1) * span, 3),
                        probability=0.9,
                    )
                    for position, token in enumerate(tokens)
                ]
            segment = TranscriptSegment(
                index=index, start=cursor, end=end, text=text,
                confidence=0.9, no_speech_prob=0.05, speaker=None,
                words=words, source_file=str(source),
                language=result.language, model="mock",
                warnings=["fabricated by the mock backend"],
            )
            result.segments.append(segment)
            if progress is not None:
                progress(len(result.segments), result.duration, segment)
            cursor += self.segment_seconds
            index += 1

        result.elapsed = round(time.time() - started, 3)
        return result


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def build_backend(
    config: TranscriptionConfig, **kwargs
) -> TranscriptionBackend:
    """The backend a config asks for. Nothing is loaded until it is used."""
    settings = config.validated()
    if settings.backend == "mock":
        return MockBackend(settings, **kwargs)
    return FasterWhisperBackend(settings)


def check(config: TranscriptionConfig) -> dict:
    """Could this configuration transcribe right now, and if not, why not?

    Used by ``transcribe status`` and by the auto stage, so "is this going to
    work" is answerable in a second rather than after a model load.
    """
    settings = config.validated()
    backend = build_backend(settings)
    health = backend.health()
    health["config_warnings"] = settings.warnings
    health["ready"] = bool(health.get("installed"))
    return health

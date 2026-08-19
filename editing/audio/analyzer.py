"""Turning a media file into ``AudioEvent`` records.

Orchestration for one file: probe for audio, pull the loudness envelope and
silence ranges from FFmpeg, run the pure detectors, add any markers the
transcript already carries, merge, and cache the result on the file
fingerprint plus the audio config.

The whole thing is soft-failing by design. A file with no audio track, a codec
FFmpeg will not decode, or FFmpeg missing entirely all produce an empty result
with a stated reason rather than an exception — the visual timeline is still
worth having without audio, and a silent failure would be worse than either.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence

from editing.audio import ffmpeg_audio, markers as marker_module, signal
from editing.audio.signal import LoudnessSample, Span
from editing.cache import Cache
from editing.config import AudioConfig, EditingConfig
from editing.errors import ToolMissingError
from editing.fingerprint import Fingerprint, fingerprint
from editing.schema import AudioEvent, MediaAsset, Transcript

logger = logging.getLogger("nova.editing.audio.analyzer")


class AudioSource(Protocol):
    """What the analyzer needs to read audio. Stubbed wholesale in tests."""

    def envelope(self, path: str) -> list[LoudnessSample]: ...

    def silence(self, path: str, duration: float) -> list[Span]: ...

    def has_audio(self, path: str) -> bool: ...


class FFmpegAudioSource:
    """The real reader, backed by two FFmpeg passes."""

    def __init__(self, config: EditingConfig, audio: AudioConfig):
        self.config = config
        self.audio = audio.validated()

    def has_audio(self, path: str) -> bool:
        return ffmpeg_audio.has_audio_stream(path, ffprobe=self.config.ffprobe)

    def envelope(self, path: str) -> list[LoudnessSample]:
        return ffmpeg_audio.loudness_envelope(
            path, config=self.audio, ffmpeg=self.config.ffmpeg
        )

    def silence(self, path: str, duration: float) -> list[Span]:
        return ffmpeg_audio.silence_ranges(
            path, config=self.audio, duration=duration, ffmpeg=self.config.ffmpeg
        )


@dataclass
class AudioResult:
    """One file's audio analysis, plus how it went."""

    asset_id: str
    source_file: str
    events: list[AudioEvent] = field(default_factory=list)
    baseline_db: float = signal.SILENT_DB
    samples: int = 0
    cached: bool = False
    elapsed: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.events) or self.samples > 0

    def summary(self) -> dict:
        return signal.summarise(self.events)

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "source_file": self.source_file,
            "baseline_db": round(self.baseline_db, 2),
            "samples": self.samples,
            "cached": self.cached,
            "elapsed": round(self.elapsed, 2),
            "warnings": list(self.warnings),
            "summary": self.summary(),
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AudioResult":
        return cls(
            asset_id=str(data.get("asset_id") or ""),
            source_file=str(data.get("source_file") or ""),
            events=[
                AudioEvent.from_dict(event) for event in (data.get("events") or [])
            ],
            baseline_db=float(data.get("baseline_db") or signal.SILENT_DB),
            samples=int(data.get("samples") or 0),
            warnings=[str(w) for w in (data.get("warnings") or [])],
        )


class AudioAnalyzer:
    """Analyses media files into audio events."""

    def __init__(
        self,
        config: EditingConfig,
        audio: AudioConfig,
        *,
        cache: Optional[Cache] = None,
        source: Optional[AudioSource] = None,
    ):
        self.config = config
        self.audio = audio.validated()
        self.cache = cache
        self.source = source if source is not None else FFmpegAudioSource(config, audio)

    # ------------------------------------------------------------------

    def analyze_asset(
        self,
        asset: MediaAsset,
        *,
        transcript: Optional[Transcript] = None,
        mark: Optional[Fingerprint] = None,
        refresh: bool = False,
    ) -> AudioResult:
        """Analyse one file's audio into events."""
        started = time.time()
        result = AudioResult(asset_id=asset.asset_id, source_file=asset.path)

        entries = list(transcript.entries) if transcript is not None else []
        mark = mark or self._mark_for(asset)
        key = self._cache_key(asset, mark, entries)

        if key is not None and self.cache is not None and not refresh:
            cached = self.cache.get("audio", key)
            if cached is not None:
                restored = AudioResult.from_dict(cached)
                restored.cached = True
                restored.elapsed = time.time() - started
                return restored

        # Markers are free and independent of FFmpeg, so they are collected
        # even when the audio itself cannot be read.
        marker_events = marker_module.detect_markers(
            entries, source_file=asset.path, asset_id=asset.asset_id
        )

        samples: list[LoudnessSample] = []
        silence: Optional[list[Span]] = None
        try:
            if not asset.has_audio and not self.source.has_audio(asset.path):
                result.warnings.append(
                    f"{asset.filename} has no audio track; only transcript "
                    "markers are available."
                )
            else:
                samples = self.source.envelope(asset.path)
                if not samples:
                    result.warnings.append(
                        f"No loudness data could be read from {asset.filename}; "
                        "audio events are limited to transcript markers."
                    )
                else:
                    silence = self.source.silence(asset.path, asset.duration)
        except ToolMissingError as exc:
            # FFmpeg missing is a setup problem worth stating plainly, but the
            # run continues on markers alone rather than aborting.
            result.warnings.append(f"{exc.message}. {exc.hint}")
        except Exception as exc:  # noqa: BLE001 - audio must never kill a run
            logger.warning("Audio analysis of %s failed: %s", asset.path, exc)
            result.warnings.append(f"Audio analysis failed: {exc}")

        detected = signal.analyse(
            samples,
            config=self.audio,
            source_file=asset.path,
            asset_id=asset.asset_id,
            transcript_entries=entries,
            duration=asset.duration,
            silence_spans=silence,
        )

        result.events = signal.merge_adjacent(
            self._dedupe(detected + marker_events)
        )
        result.baseline_db = signal.baseline_db(samples) if samples else signal.SILENT_DB
        result.samples = len(samples)
        result.elapsed = time.time() - started

        if key is not None and self.cache is not None:
            self.cache.put(
                "audio", key, result.to_dict(),
                meta={"asset_id": asset.asset_id, "path": asset.path,
                      "events": len(result.events)},
            )
        return result

    def analyze_assets(
        self,
        assets: Sequence[MediaAsset],
        *,
        transcripts: Optional[dict] = None,
        refresh: bool = False,
    ) -> dict:
        transcripts = transcripts or {}
        return {
            asset.asset_id: self.analyze_asset(
                asset, transcript=transcripts.get(asset.asset_id), refresh=refresh
            )
            for asset in assets
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _dedupe(events: Sequence[AudioEvent]) -> list[AudioEvent]:
        """Drop a heuristic guess that a transcript marker already covers.

        When the transcript says ``[laughs]`` over the same stretch the burst
        detector flagged, the marker is the better record -- it names the sound
        rather than guessing at it. Keeping both would double-count the same
        moment as evidence in the recommendation layers.
        """
        markers = [e for e in events if e.detection == "transcript_marker"]
        out: list[AudioEvent] = list(markers)
        for event in events:
            if event.detection == "transcript_marker":
                continue
            covered = any(
                marker.type == event.type and marker.overlaps(event.start, event.end) > 0
                for marker in markers
            )
            if not covered:
                out.append(event)
        return out

    def _cache_key(
        self,
        asset: MediaAsset,
        mark: Optional[Fingerprint],
        entries: Sequence,
    ) -> Optional[str]:
        """Key for one file's audio analysis, or None when it cannot be trusted.

        The transcript is part of the key because it genuinely changes the
        output -- pauses, speech density and markers all come from it, so
        importing a transcript must invalidate an audio analysis made without
        one.
        """
        if self.cache is None or mark is None:
            return None
        return self.cache.key(
            "audio",
            file=mark.cache_key_part(),
            audio=self.audio.cache_key_part(),
            transcript={
                "entries": len(entries),
                # Cheap content signature: two transcripts with the same line
                # count but different text must not share a key.
                "digest": _digest(entries),
            },
        )

    @staticmethod
    def _mark_for(asset: MediaAsset) -> Optional[Fingerprint]:
        try:
            return fingerprint(asset.path)
        except Exception:  # noqa: BLE001 - offline media is a normal state
            return None


def _digest(entries: Sequence) -> str:
    from editing.schema import short_hash

    if not entries:
        return ""
    return short_hash(*[
        f"{entry.start:.2f}:{entry.end:.2f}:{entry.text}" for entry in entries
    ])


def build_analyzer(
    config: EditingConfig,
    audio: AudioConfig,
    *,
    cache: Optional[Cache] = None,
    source: Optional[AudioSource] = None,
) -> AudioAnalyzer:
    return AudioAnalyzer(config, audio, cache=cache, source=source)

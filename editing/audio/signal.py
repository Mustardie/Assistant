"""Audio event detection from a loudness envelope.

Pure functions over a list of ``LoudnessSample``. No ffmpeg, no files, no
model — which is what makes the detection policy testable, and this is a layer
where being able to test the policy matters, because every threshold here is a
judgement call that will be wrong for somebody's microphone.

**What this layer honestly is.** It measures loudness over time and reasons
about the shape of that curve. From that it can tell you, reliably, where the
recording is silent, where it clips, and where the level jumps. It *cannot*
tell you that someone is laughing. What it can say is "there is a rhythmic
cluster of loudness bursts here, which is often laughter" — and that is exactly
what ``possible_laughter`` means, at a confidence deliberately capped below
anything measured. The prompt asked not to pretend this is emotion detection;
the cap in ``AudioConfig.max_inferred_confidence`` is that promise made
mechanical rather than just documented.

Everything is relative to the file's own median level. An absolute dBFS
threshold behaves completely differently on a quiet recording and a hot one,
and "loud" only means anything next to the rest of the same recording.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from editing.config import AudioConfig
from editing.schema import (
    AUDIO_VALUE_FOR_TYPE, AudioEvent, TranscriptEntry, short_hash,
)

#: Level reported for a sample with no measurable signal. FFmpeg emits ``-inf``
#: for digital silence; this is the finite stand-in used everywhere after
#: parsing so comparisons and medians stay well defined.
SILENT_DB = -100.0


@dataclass(frozen=True)
class LoudnessSample:
    """One reading of the loudness envelope, in dBFS (negative; 0.0 is full)."""

    time: float
    rms_db: float
    peak_db: float = SILENT_DB

    @property
    def silent(self) -> bool:
        return self.rms_db <= SILENT_DB + 1e-6


@dataclass(frozen=True)
class Span:
    """A detected stretch of time with a representative level."""

    start: float
    end: float
    level_db: float = SILENT_DB
    peak_db: float = SILENT_DB

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def baseline_db(samples: Sequence[LoudnessSample], *, floor_db: float = -60.0) -> float:
    """The file's own typical level: the median of its non-silent samples.

    Median rather than mean because a single explosion or a long silent tail
    would drag a mean far enough to make every later threshold meaningless.
    Samples at or below ``floor_db`` are excluded for the same reason — a
    recording that is 70% silence should still have a baseline describing the
    30% where something happens.
    """
    levels = [s.rms_db for s in samples if s.rms_db > floor_db]
    if not levels:
        levels = [s.rms_db for s in samples]
    if not levels:
        return SILENT_DB
    return float(statistics.median(levels))


def _runs(
    samples: Sequence[LoudnessSample],
    predicate,
    *,
    interval: float,
) -> list[tuple[int, int]]:
    """Index ranges (inclusive start, exclusive end) where ``predicate`` holds."""
    runs: list[tuple[int, int]] = []
    start: Optional[int] = None
    for index, sample in enumerate(samples):
        if predicate(sample):
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(samples)))
    return runs


def _span_of(
    samples: Sequence[LoudnessSample],
    first: int,
    last_exclusive: int,
    *,
    interval: float,
) -> Span:
    """Turn an index range into a time span.

    The end extends one sample interval past the final sample, because a
    reading at t describes the audio from t to t+interval — without this every
    detected span would be one interval short, and a 1-second silence between
    two words would be reported as 0.75s.
    """
    window = samples[first:last_exclusive]
    if not window:
        return Span(0.0, 0.0)
    levels = [s.rms_db for s in window]
    peaks = [s.peak_db for s in window]
    return Span(
        start=window[0].time,
        end=window[-1].time + interval,
        level_db=float(statistics.median(levels)),
        peak_db=max(peaks) if peaks else SILENT_DB,
    )


# ---------------------------------------------------------------------------
# Measured detectors
# ---------------------------------------------------------------------------

def detect_silence(
    samples: Sequence[LoudnessSample], config: AudioConfig
) -> list[Span]:
    """Stretches below the absolute silence floor, long enough to matter.

    The one detector that uses an absolute threshold rather than a relative
    one: digital silence is digital silence regardless of how loud the rest of
    the recording is.
    """
    config = config.validated()
    spans = [
        _span_of(samples, first, last, interval=config.sample_interval)
        for first, last in _runs(
            samples,
            lambda s: s.rms_db <= config.silence_threshold_db,
            interval=config.sample_interval,
        )
    ]
    return [span for span in spans if span.duration >= config.min_silence_seconds]


def detect_low_energy(
    samples: Sequence[LoudnessSample], baseline: float, config: AudioConfig
) -> list[Span]:
    """Sustained quiet that is *not* silence — the audio equivalent of boring.

    Distinct from silence because it means something different to an editor:
    silence is a cut point, low energy is a stretch where nothing is being
    said and the game is just humming along.
    """
    config = config.validated()
    threshold = baseline - config.low_energy_delta_db
    spans = [
        _span_of(samples, first, last, interval=config.sample_interval)
        for first, last in _runs(
            samples,
            lambda s: (
                config.silence_threshold_db < s.rms_db <= threshold
            ),
            interval=config.sample_interval,
        )
    ]
    # A longer minimum than silence: a brief dip between sentences is normal
    # speech rhythm, not a boring stretch.
    minimum = max(config.min_silence_seconds * 2.0, 2.0)
    return [span for span in spans if span.duration >= minimum]


def detect_spikes(
    samples: Sequence[LoudnessSample], baseline: float, config: AudioConfig
) -> list[Span]:
    """Stretches jumping ``spike_delta_db`` above the file's baseline."""
    config = config.validated()
    threshold = baseline + config.spike_delta_db
    return [
        _span_of(samples, first, last, interval=config.sample_interval)
        for first, last in _runs(
            samples, lambda s: s.rms_db >= threshold, interval=config.sample_interval
        )
    ]


def detect_clipping(
    samples: Sequence[LoudnessSample], config: AudioConfig
) -> list[Span]:
    """Where the peak level is at or above the distortion threshold."""
    config = config.validated()
    return [
        _span_of(samples, first, last, interval=config.sample_interval)
        for first, last in _runs(
            samples,
            lambda s: s.peak_db >= config.clipping_db,
            interval=config.sample_interval,
        )
    ]


def is_sudden_reaction(
    samples: Sequence[LoudnessSample],
    spike: Span,
    baseline: float,
    config: AudioConfig,
    *,
    lead_in: float = 1.5,
) -> bool:
    """Whether a spike erupts *out of* something quiet.

    This is the difference between a shout and a loud passage. A jump of 15 dB
    from an already-loud fight scene is just more fight; the same jump out of
    near-silence is the moment a viewer's head comes up. Requiring the quiet
    run-up is what stops every loud minute registering as a string of
    reactions.
    """
    config = config.validated()
    if spike.level_db - baseline < config.reaction_delta_db:
        return False

    before = [
        sample for sample in samples
        if spike.start - lead_in <= sample.time < spike.start
    ]
    if not before:
        return False
    quiet_before = statistics.median([s.rms_db for s in before])
    # The run-up must be at or below the file's normal level, and the jump from
    # it must be the full reaction delta.
    return (
        quiet_before <= baseline
        and (spike.level_db - quiet_before) >= config.reaction_delta_db
    )


# ---------------------------------------------------------------------------
# Speech-derived detectors
# ---------------------------------------------------------------------------

def speech_density(
    entries: Sequence[TranscriptEntry], start: float, end: float
) -> float:
    """Words per second of speech overlapping ``[start, end)``.

    Words are apportioned by how much of each line falls inside the window, so
    a line straddling the boundary contributes its share rather than all or
    nothing.
    """
    span = max(0.0, end - start)
    if span <= 0.0:
        return 0.0
    words = 0.0
    for entry in entries:
        overlap = entry.overlaps(start, end)
        if overlap <= 0.0 or entry.duration <= 0.0:
            continue
        words += len(entry.text.split()) * (overlap / entry.duration)
    return words / span


def detect_pauses(
    entries: Sequence[TranscriptEntry],
    duration: float,
    config: AudioConfig,
) -> list[Span]:
    """Gaps between spoken lines that are long enough to read as dead air.

    Transcript-derived rather than loudness-derived, and worth having
    separately: a gap where the game is still making noise is not silent, but
    it is still a hole in the commentary that an editor may want to close.
    """
    config = config.validated()
    ordered = sorted(entries, key=lambda entry: entry.start)
    gaps: list[Span] = []
    cursor = 0.0
    for entry in ordered:
        if entry.start - cursor >= config.long_pause_seconds:
            gaps.append(Span(start=cursor, end=entry.start))
        cursor = max(cursor, entry.end)
    if duration > 0 and duration - cursor >= config.long_pause_seconds:
        gaps.append(Span(start=cursor, end=duration))
    return gaps


def detect_speech_density_changes(
    entries: Sequence[TranscriptEntry],
    duration: float,
    config: AudioConfig,
    *,
    window: float = 5.0,
) -> list[tuple[Span, str]]:
    """Windows where narration is notably faster or slower than usual.

    Returns ``(span, "speech_dense" | "speech_sparse")``. Fast narration tends
    to mark excitement or explanation; a sudden drop tends to mark the player
    concentrating, which is often right before something goes wrong.
    """
    config = config.validated()
    if duration <= 0 or not entries:
        return []

    out: list[tuple[Span, str]] = []
    steps = max(1, int(duration // window))
    for index in range(steps):
        start = index * window
        end = min(duration, start + window)
        density = speech_density(entries, start, end)
        if density >= config.speech_dense_wps:
            out.append((Span(start, end, level_db=SILENT_DB), "speech_dense"))
        elif 0.0 < density <= config.speech_sparse_wps:
            out.append((Span(start, end, level_db=SILENT_DB), "speech_sparse"))
    return _merge_labelled(out)


def _merge_labelled(items: list[tuple[Span, str]]) -> list[tuple[Span, str]]:
    """Join adjacent windows carrying the same label into one span."""
    merged: list[tuple[Span, str]] = []
    for span, label in items:
        if merged and merged[-1][1] == label and span.start <= merged[-1][0].end + 1e-6:
            previous, _ = merged[-1]
            merged[-1] = (Span(previous.start, span.end, previous.level_db), label)
        else:
            merged.append((span, label))
    return merged


# ---------------------------------------------------------------------------
# Inferred detectors -- guesses, and labelled as such
# ---------------------------------------------------------------------------

def detect_laughter_clusters(
    spikes: Sequence[Span], config: AudioConfig
) -> list[Span]:
    """Rhythmic clusters of short bursts, which are *often* laughter.

    The signal is repetition: laughter is several short loud bursts close
    together, where a shout is one longer one. This will also fire on a
    stuttering engine sound or a burst of gunfire, which is precisely why the
    resulting event is named ``possible_laughter`` and capped in confidence.
    """
    config = config.validated()
    # Long bursts are shouts or passages, not laughter.
    short = [spike for spike in spikes if spike.duration <= 1.2]
    clusters: list[Span] = []
    index = 0
    while index < len(short):
        group = [short[index]]
        cursor = index + 1
        while (
            cursor < len(short)
            and short[cursor].start - group[0].start <= config.laughter_window
        ):
            group.append(short[cursor])
            cursor += 1
        if len(group) >= config.laughter_min_bursts:
            clusters.append(Span(
                start=group[0].start,
                end=group[-1].end,
                level_db=max(spike.level_db for spike in group),
                peak_db=max(spike.peak_db for spike in group),
            ))
            index = cursor
        else:
            index += 1
    return clusters


def detect_music_regions(
    samples: Sequence[LoudnessSample],
    entries: Sequence[TranscriptEntry],
    baseline: float,
    config: AudioConfig,
    *,
    spikes: Sequence[Span] = (),
) -> list[Span]:
    """Long, steady, speech-free energy — which is *often* music.

    Four conditions, all required. Sustained level near or above baseline; low
    variance (speech is bursty, a bed is not); little or no transcript
    coverage; and **no loudness spikes inside it**. That last one carries most
    of the weight: without it a long stretch of gameplay containing a shout and
    a burst of laughter reads as one steady region, because the quiet majority
    dominates the variance. A music bed does not contain 20 dB jumps.

    Still a guess, and still capped in confidence by the caller.
    """
    config = config.validated()
    threshold = baseline - 6.0
    regions: list[Span] = []

    for first, last in _runs(
        samples, lambda s: s.rms_db >= threshold, interval=config.sample_interval
    ):
        # Cut the candidate at every spike rather than discarding it whole: a
        # long steady bed interrupted by one shout is still a bed either side,
        # and rejecting the entire run would lose both halves.
        for lo, hi in _split_at_spikes(samples, first, last, spikes):
            window = samples[lo:hi]
            if len(window) < 4:
                continue
            span = _span_of(samples, lo, hi, interval=config.sample_interval)
            if span.duration < config.music_min_seconds:
                continue
            # Speech swings; a bed does not.
            if statistics.pstdev([s.rms_db for s in window]) > 4.0:
                continue
            if speech_density(entries, span.start, span.end) > 0.4:
                continue
            regions.append(span)
    return regions


def _split_at_spikes(
    samples: Sequence[LoudnessSample],
    first: int,
    last: int,
    spikes: Sequence[Span],
) -> list[tuple[int, int]]:
    """Break an index range wherever a spike overlaps it."""
    if not spikes:
        return [(first, last)]
    pieces: list[tuple[int, int]] = []
    start = first
    for index in range(first, last):
        time = samples[index].time
        if any(spike.start <= time < spike.end for spike in spikes):
            if index > start:
                pieces.append((start, index))
            start = index + 1
    if last > start:
        pieces.append((start, last))
    return pieces


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _event(
    *,
    source_file: str,
    asset_id: str,
    span: Span,
    kind: str,
    confidence: float,
    baseline: float,
    config: AudioConfig,
    detection: str = "heuristic",
    notes: str = "",
    density: Optional[float] = None,
    evidence: Optional[dict] = None,
) -> AudioEvent:
    """Build an event, enforcing the confidence ceiling on inferences."""
    config = config.validated()
    inferred = kind.startswith("possible_") or kind == "music_region"
    if inferred and detection == "heuristic":
        confidence = min(confidence, config.max_inferred_confidence)
    return AudioEvent(
        event_id="au_" + short_hash(asset_id or source_file, round(span.start, 3),
                                    round(span.end, 3), kind),
        source_file=source_file,
        asset_id=asset_id,
        start=span.start,
        end=span.end,
        type=kind,
        confidence=max(0.0, min(1.0, confidence)),
        loudness_db=span.level_db,
        peak_db=span.peak_db,
        baseline_db=baseline,
        speech_density=density,
        edit_value=AUDIO_VALUE_FOR_TYPE.get(kind, "unknown"),
        detection=detection,
        notes=notes,
        evidence=dict(evidence or {}),
    )


def analyse(
    samples: Sequence[LoudnessSample],
    *,
    config: AudioConfig,
    source_file: str = "",
    asset_id: str = "",
    transcript_entries: Optional[Sequence[TranscriptEntry]] = None,
    duration: float = 0.0,
    silence_spans: Optional[Sequence[Span]] = None,
) -> list[AudioEvent]:
    """Run every detector and return the events, in time order.

    ``silence_spans`` lets a caller supply FFmpeg's own ``silencedetect``
    output, which is more precise than re-deriving silence from a 0.25s
    envelope. When omitted, silence is detected from the envelope instead, so
    the function works identically on synthetic samples in a test.
    """
    config = config.validated()
    entries = list(transcript_entries or [])
    samples = sorted(samples, key=lambda sample: sample.time)
    if not samples and not entries:
        return []

    baseline = baseline_db(samples) if samples else SILENT_DB
    events: list[AudioEvent] = []

    def add(span, kind, confidence, **kw):
        if span.duration <= 0.0:
            return
        events.append(_event(
            source_file=source_file, asset_id=asset_id, span=span, kind=kind,
            confidence=confidence, baseline=baseline, config=config, **kw,
        ))

    # -- measured ------------------------------------------------------
    quiet = (
        list(silence_spans) if silence_spans is not None
        else detect_silence(samples, config)
    )
    for span in quiet:
        add(span, "silence", 0.9,
            detection="heuristic" if silence_spans is None else "heuristic",
            notes=f"{span.duration:.1f}s below {config.silence_threshold_db:.0f} dBFS",
            evidence={"threshold_db": config.silence_threshold_db})

    for span in detect_low_energy(samples, baseline, config):
        add(span, "low_energy", 0.7,
            notes=f"{span.duration:.1f}s at {span.level_db:.0f} dBFS, "
                  f"{baseline - span.level_db:.0f} dB below this file's normal",
            evidence={"baseline_db": round(baseline, 2)})

    spikes = detect_spikes(samples, baseline, config)
    for span in spikes:
        reaction = is_sudden_reaction(samples, span, baseline, config)
        kind = "sudden_reaction" if reaction else "loudness_spike"
        add(span, kind, 0.8 if reaction else 0.75,
            density=speech_density(entries, span.start, span.end) if entries else None,
            notes=f"+{span.level_db - baseline:.0f} dB above this file's normal"
                  + (" and out of a quiet run-up" if reaction else ""),
            evidence={"delta_db": round(span.level_db - baseline, 2)})

    for span in detect_clipping(samples, config):
        add(span, "clipping", 0.85,
            notes=f"peak {span.peak_db:.1f} dBFS -- the signal is distorting",
            evidence={"peak_db": round(span.peak_db, 2)})

    # -- transcript-derived --------------------------------------------
    if entries:
        for span in detect_pauses(entries, duration, config):
            add(span, "long_pause", 0.8,
                detection="transcript_marker",
                notes=f"{span.duration:.1f}s with nothing said",
                density=0.0)

        for span, label in detect_speech_density_changes(entries, duration, config):
            density = speech_density(entries, span.start, span.end)
            add(span, label, 0.65,
                detection="transcript_marker",
                density=density,
                notes=f"{density:.1f} words/second",
                evidence={"words_per_second": round(density, 2)})

    # -- inferred (capped) ---------------------------------------------
    for span in detect_laughter_clusters(spikes, config):
        add(span, "possible_laughter", 0.4,
            notes="rhythmic burst pattern -- often laughter, but this is a "
                  "loudness-shape guess, not speech recognition",
            evidence={"pattern": "burst_cluster"})

    for span in spikes:
        if span.duration >= 0.6 and (span.level_db - baseline) >= (
            config.reaction_delta_db + 4.0
        ):
            add(span, "possible_scream", 0.4,
                notes="sustained very loud burst -- often a shout or scream, "
                      "inferred from level alone",
                evidence={"delta_db": round(span.level_db - baseline, 2)})

    for span in detect_music_regions(samples, entries, baseline, config,
                                     spikes=spikes):
        add(span, "music_region", 0.4,
            notes="steady speech-free energy -- often music or a loud ambient bed",
            evidence={"speech_density": round(
                speech_density(entries, span.start, span.end), 3)})

    kept = [event for event in events if event.confidence >= config.min_confidence]
    kept.sort(key=lambda event: (event.start, event.end, event.type))
    return kept


def merge_adjacent(
    events: Sequence[AudioEvent], *, gap: float = 0.25
) -> list[AudioEvent]:
    """Join same-type events separated by less than ``gap``.

    A 0.25s envelope chops one long silence into several when a single sample
    blips above the floor; this stitches those back together so the timeline
    shows one three-second silence rather than four short ones.
    """
    ordered = sorted(events, key=lambda event: (event.type, event.start))
    out: list[AudioEvent] = []
    for event in ordered:
        previous = out[-1] if out else None
        if (
            previous is not None
            and previous.type == event.type
            and event.start - previous.end <= gap
        ):
            previous.end = max(previous.end, event.end)
            previous.confidence = max(previous.confidence, event.confidence)
            previous.peak_db = max(previous.peak_db, event.peak_db)
            continue
        out.append(event)
    out.sort(key=lambda event: (event.start, event.end, event.type))
    return out


def summarise(events: Iterable[AudioEvent]) -> dict:
    """Counts per type and per edit value, for the CLI and for debugging."""
    by_type: dict = {}
    by_value: dict = {}
    total = 0
    for event in events:
        total += 1
        by_type[event.type] = by_type.get(event.type, 0) + 1
        by_value[event.edit_value] = by_value.get(event.edit_value, 0) + 1
    return {"events": total, "by_type": by_type, "by_edit_value": by_value}

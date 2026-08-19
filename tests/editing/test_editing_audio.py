"""The audio event layer: schema, detectors, markers, caching.

The theme running through these is **honest confidence**. Several tests exist
specifically to prove that the layer cannot overclaim: an inferred event can
never be as confident as a measured one, a transcript marker always outranks a
loudness guess, and a file with no readable audio produces a stated reason
rather than an empty success.
"""
from __future__ import annotations

import json

import pytest

from editing.audio import ffmpeg_audio, markers, signal
from editing.audio.analyzer import AudioAnalyzer, AudioResult
from editing.audio.signal import SILENT_DB, LoudnessSample, Span
from editing.config import AudioConfig
from editing.schema import (
    AUDIO_DETECTION_METHODS, AUDIO_EDIT_VALUES, AUDIO_EVENT_TYPES, AudioEvent,
    Transcript, TranscriptEntry,
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_audio_event_round_trips():
    original = AudioEvent(
        event_id="au_1", source_file="/f/c.mp4", asset_id="a1",
        start=1.0, end=3.5, type="sudden_reaction", confidence=0.8,
        loudness_db=-6.0, peak_db=-0.5, baseline_db=-24.0,
        speech_density=2.5, edit_value="impact", detection="heuristic",
        notes="loud", evidence={"delta_db": 18.0},
    )
    restored = AudioEvent.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.to_dict() == original.to_dict()
    assert restored.duration == 2.5
    assert restored.relative_db == 18.0


def test_audio_event_coerces_unknown_vocabulary():
    event = AudioEvent.from_dict({
        "type": "something weird", "edit_value": "nonsense",
        "detection": "telepathy", "start": 1, "end": 2,
    })
    assert event.type in AUDIO_EVENT_TYPES
    assert event.edit_value in AUDIO_EDIT_VALUES
    assert event.detection == "unknown"


def test_audio_event_defaults_edit_value_from_type():
    assert AudioEvent.from_dict({"type": "silence"}).edit_value == "pause"
    assert AudioEvent.from_dict({"type": "possible_laughter"}).edit_value == "comedy"


@pytest.mark.parametrize("kind,measured", [
    ("silence", True),
    ("clipping", True),
    ("loudness_spike", True),
    ("sudden_reaction", True),
    ("possible_laughter", False),
    ("possible_scream", False),
    ("music_region", False),
])
def test_is_measured_separates_measurement_from_inference(kind, measured):
    """The distinction the whole layer's honesty rests on."""
    assert AudioEvent(
        event_id="x", source_file="f", type=kind
    ).is_measured is measured


def test_audio_event_end_never_precedes_start():
    assert AudioEvent.from_dict({"start": 8, "end": 2}).end >= 8.0


def test_audio_event_confidence_is_clamped():
    assert AudioEvent.from_dict({"confidence": 5}).confidence == 1.0
    assert AudioEvent.from_dict({"confidence": -1}).confidence == 0.0


def test_speech_density_none_survives_round_trip():
    """None means "no transcript", which is different from 0 words/second."""
    event = AudioEvent(event_id="x", source_file="f", speech_density=None)
    assert AudioEvent.from_dict(event.to_dict()).speech_density is None


def test_all_detection_methods_are_accepted():
    for method in AUDIO_DETECTION_METHODS:
        assert AudioEvent.from_dict({"detection": method}).detection == method


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

def test_baseline_is_the_median_of_non_silent_samples(make_envelope):
    samples = make_envelope((0, 10, -24.0), (10, 30, -95.0))
    # A mean would be dragged towards silence by the long quiet tail.
    assert signal.baseline_db(samples) == pytest.approx(-24.0)


def test_baseline_of_an_entirely_silent_file(make_envelope):
    assert signal.baseline_db(make_envelope((0, 5, -100.0))) == pytest.approx(-100.0)


def test_baseline_of_nothing():
    assert signal.baseline_db([]) == SILENT_DB


# ---------------------------------------------------------------------------
# Silence and pauses
# ---------------------------------------------------------------------------

def test_detect_silence(make_envelope, audio_config):
    samples = make_envelope((0, 4, -24.0), (4, 9, -95.0), (9, 12, -24.0))
    spans = signal.detect_silence(samples, audio_config)
    assert len(spans) == 1
    assert spans[0].start == pytest.approx(4.0)
    # The end extends one interval past the last quiet sample, because a
    # reading at t describes the audio from t to t+interval.
    assert spans[0].end == pytest.approx(9.0)


def test_short_quiet_gaps_are_not_silence(make_envelope, audio_config):
    """A gap between words is not dead air."""
    samples = make_envelope((0, 4, -24.0), (4, 4.25, -95.0), (4.25, 8, -24.0))
    assert signal.detect_silence(samples, audio_config) == []


def test_detect_low_energy_is_not_silence(make_envelope, audio_config):
    samples = make_envelope((0, 10, -24.0), (10, 20, -40.0), (20, 30, -24.0))
    quiet = signal.detect_low_energy(samples, -24.0, audio_config)
    assert quiet and quiet[0].start == pytest.approx(10.0)
    # Above the silence floor, so it is not also reported as silence.
    assert signal.detect_silence(samples, audio_config) == []


def test_detect_pauses_from_transcript_gaps(audio_config):
    entries = [TranscriptEntry(0, 2, "one"), TranscriptEntry(9, 11, "two")]
    gaps = signal.detect_pauses(entries, 20.0, audio_config)
    assert [(g.start, g.end) for g in gaps] == [(2.0, 9.0), (11.0, 20.0)]


def test_detect_pauses_ignores_short_gaps(audio_config):
    entries = [TranscriptEntry(0, 2, "one"), TranscriptEntry(3, 5, "two")]
    assert signal.detect_pauses(entries, 5.0, audio_config) == []


# ---------------------------------------------------------------------------
# Spikes and reactions
# ---------------------------------------------------------------------------

def test_detect_spikes_relative_to_the_file(make_envelope, audio_config):
    samples = make_envelope((0, 10, -24.0), (10, 11, -10.0), (11, 20, -24.0))
    spikes = signal.detect_spikes(samples, -24.0, audio_config)
    assert len(spikes) == 1
    assert spikes[0].start == pytest.approx(10.0)


def test_a_loud_file_does_not_spike_everywhere(make_envelope, audio_config):
    """Thresholds are relative, so a hot recording is not one long spike."""
    samples = make_envelope((0, 20, -6.0))
    assert signal.detect_spikes(samples, signal.baseline_db(samples), audio_config) == []


def test_sudden_reaction_requires_a_quiet_run_up(make_envelope, audio_config):
    samples = make_envelope((0, 10, -40.0), (10, 11, -8.0), (11, 15, -24.0))
    spike = signal.detect_spikes(samples, -24.0, audio_config)[0]
    assert signal.is_sudden_reaction(samples, spike, -24.0, audio_config) is True


def test_a_spike_inside_loud_audio_is_not_a_reaction(make_envelope, audio_config):
    """A jump inside an already-loud fight is just more fight."""
    samples = make_envelope((0, 10, -12.0), (10, 11, -4.0), (11, 15, -12.0))
    baseline = signal.baseline_db(samples)
    spikes = signal.detect_spikes(samples, baseline, audio_config)
    assert all(
        not signal.is_sudden_reaction(samples, spike, baseline, audio_config)
        for spike in spikes
    )


def test_detect_clipping(make_envelope, audio_config):
    samples = make_envelope((0, 5, -24.0, -12.0), (5, 6, -6.0, 0.0))
    spans = signal.detect_clipping(samples, audio_config)
    assert len(spans) == 1
    assert spans[0].start == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Speech density
# ---------------------------------------------------------------------------

def test_speech_density_counts_words_per_second():
    entries = [TranscriptEntry(0, 4, "one two three four five six seven eight")]
    assert signal.speech_density(entries, 0, 4) == pytest.approx(2.0)


def test_speech_density_apportions_a_straddling_line():
    """Half the line inside the window contributes half its words."""
    entries = [TranscriptEntry(0, 4, "one two three four")]
    assert signal.speech_density(entries, 2, 4) == pytest.approx(1.0)


def test_speech_density_of_silence():
    assert signal.speech_density([], 0, 10) == 0.0


def test_detect_speech_density_changes(audio_config):
    fast = "word " * 30
    entries = [TranscriptEntry(0, 5, fast.strip()), TranscriptEntry(10, 15, "one")]
    changes = signal.detect_speech_density_changes(entries, 20.0, audio_config)
    labels = {label for _, label in changes}
    assert "speech_dense" in labels
    assert "speech_sparse" in labels


# ---------------------------------------------------------------------------
# Inference -- and its ceiling
# ---------------------------------------------------------------------------

def test_laughter_needs_repeated_short_bursts(audio_config):
    bursts = [Span(t, t + 0.3, -10.0) for t in (10.0, 10.8, 11.6, 12.4)]
    clusters = signal.detect_laughter_clusters(bursts, audio_config)
    assert len(clusters) == 1


def test_one_shout_is_not_laughter(audio_config):
    assert signal.detect_laughter_clusters([Span(10.0, 11.0, -8.0)], audio_config) == []


def test_a_long_burst_is_not_laughter(audio_config):
    """Laughter is short and repeated; a sustained roar is something else."""
    long_bursts = [Span(t, t + 2.0, -10.0) for t in (10.0, 12.5, 15.0)]
    assert signal.detect_laughter_clusters(long_bursts, audio_config) == []


def test_music_region_needs_steady_speech_free_energy(make_envelope, audio_config):
    samples = make_envelope((0, 20, -25.0))
    regions = signal.detect_music_regions(samples, [], -25.0, audio_config)
    assert regions and regions[0].duration >= audio_config.music_min_seconds


def test_music_region_is_rejected_where_there_is_speech(make_envelope, audio_config):
    samples = make_envelope((0, 20, -25.0))
    talking = [TranscriptEntry(0, 20, "word " * 40)]
    assert signal.detect_music_regions(samples, talking, -25.0, audio_config) == []


def test_music_region_is_cut_at_a_spike(make_envelope, audio_config):
    """A bed interrupted by a shout is still a bed either side of it."""
    samples = make_envelope((0, 10, -25.0), (10, 10.5, -6.0), (10.5, 25, -25.0))
    spikes = signal.detect_spikes(samples, -25.0, audio_config)
    regions = signal.detect_music_regions(
        samples, [], -25.0, audio_config, spikes=spikes
    )
    assert regions
    assert all(
        not (region.start < 10.5 and region.end > 10.0) for region in regions
    )


def test_inferred_confidence_is_capped(make_envelope):
    """The honesty guarantee, enforced rather than merely documented."""
    config = AudioConfig(
        sample_interval=0.25, max_inferred_confidence=0.3, music_min_seconds=2.0
    ).validated()
    samples = make_envelope((0, 20, -25.0))
    events = signal.analyse(samples, config=config, source_file="/f/c.mp4")
    inferred = [event for event in events if not event.is_measured]
    assert inferred
    assert all(event.confidence <= 0.3 for event in inferred)


def test_measured_events_are_not_capped(make_envelope):
    config = AudioConfig(
        sample_interval=0.25, max_inferred_confidence=0.1, min_silence_seconds=0.5
    ).validated()
    samples = make_envelope((0, 5, -24.0), (5, 10, -95.0))
    events = signal.analyse(samples, config=config, source_file="/f/c.mp4")
    silence = [event for event in events if event.type == "silence"]
    assert silence and silence[0].confidence > 0.5


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def test_analyse_produces_a_mixed_set(make_envelope, audio_config):
    samples = make_envelope(
        (0, 8, -24.0), (8, 14, -95.0), (14, 15, -6.0, 0.0), (15, 24, -24.0)
    )
    events = signal.analyse(
        samples, config=audio_config, source_file="/f/c.mp4", asset_id="a1",
        duration=24.0,
    )
    kinds = {event.type for event in events}
    assert "silence" in kinds
    assert "clipping" in kinds
    assert {"sudden_reaction", "loudness_spike"} & kinds
    assert all(event.asset_id == "a1" for event in events)
    assert events == sorted(events, key=lambda e: (e.start, e.end, e.type))


def test_analyse_of_nothing_is_empty(audio_config):
    assert signal.analyse([], config=audio_config) == []


def test_merge_adjacent_joins_a_chopped_silence():
    events = [
        AudioEvent(event_id="1", source_file="f", start=0.0, end=2.0, type="silence"),
        AudioEvent(event_id="2", source_file="f", start=2.1, end=4.0, type="silence"),
    ]
    merged = signal.merge_adjacent(events)
    assert len(merged) == 1
    assert merged[0].end == 4.0


def test_merge_adjacent_keeps_different_types_apart():
    events = [
        AudioEvent(event_id="1", source_file="f", start=0.0, end=2.0, type="silence"),
        AudioEvent(event_id="2", source_file="f", start=2.0, end=4.0,
                   type="low_energy"),
    ]
    assert len(signal.merge_adjacent(events)) == 2


# ---------------------------------------------------------------------------
# FFmpeg parsers (no FFmpeg needed)
# ---------------------------------------------------------------------------

def test_parse_astats_output():
    text = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.astats.Overall.RMS_level=-23.456\n"
        "lavfi.astats.Overall.Peak_level=-3.210\n"
        "frame:1    pts:2000    pts_time:0.25\n"
        "lavfi.astats.Overall.RMS_level=-40.000\n"
        "lavfi.astats.Overall.Peak_level=-20.000\n"
    )
    samples = ffmpeg_audio.parse_astats_output(text)
    assert [(s.time, s.rms_db) for s in samples] == [(0.0, -23.456), (0.25, -40.0)]
    assert samples[0].peak_db == -3.21


def test_parse_astats_maps_negative_infinity_to_the_floor():
    text = "frame:0 pts_time:0\nlavfi.astats.Overall.RMS_level=-inf\n"
    assert ffmpeg_audio.parse_astats_output(text)[0].rms_db == SILENT_DB


def test_parse_astats_keeps_a_sample_missing_its_peak():
    """RMS drives nearly every detector; dropping the reading would leave a hole."""
    text = "frame:0 pts_time:0\nlavfi.astats.Overall.RMS_level=-30.0\n"
    samples = ffmpeg_audio.parse_astats_output(text)
    assert len(samples) == 1
    assert samples[0].peak_db == SILENT_DB


def test_parse_astats_of_junk():
    assert ffmpeg_audio.parse_astats_output("ffmpeg version 6.0\n") == []


def test_parse_silencedetect_output():
    text = (
        "[silencedetect @ 0x1] silence_start: 12.5\n"
        "[silencedetect @ 0x1] silence_end: 15.75 | silence_duration: 3.25\n"
    )
    spans = ffmpeg_audio.parse_silencedetect_output(text)
    assert [(s.start, s.end) for s in spans] == [(12.5, 15.75)]


def test_a_file_ending_in_silence_is_closed_by_the_caller():
    """FFmpeg emits no ``silence_end`` when the file ends quiet."""
    spans = ffmpeg_audio.parse_silencedetect_output("silence_start: 30.0\n")
    assert spans[0].end == spans[0].start
    closed = ffmpeg_audio.close_open_spans(spans, duration=45.0)
    assert closed[0].end == 45.0


def test_close_open_spans_drops_an_uncloseable_span():
    spans = ffmpeg_audio.parse_silencedetect_output("silence_start: 90.0\n")
    assert ffmpeg_audio.close_open_spans(spans, duration=45.0) == []


# ---------------------------------------------------------------------------
# Transcript markers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("laughs", "possible_laughter"),
    ("laughter", "possible_laughter"),
    ("nervous laughter", "possible_laughter"),
    ("screams", "possible_scream"),
    ("music", "music_region"),
    ("sighs", "low_energy"),
    ("applause", "loudness_spike"),
    ("crosstalk", None),
    ("", None),
])
def test_marker_type(text, expected):
    assert markers.marker_type(text) == expected


def test_find_annotations_in_a_line():
    found = markers.find_annotations("haha [laughs] that was close (music) now")
    assert ("laughs", "possible_laughter") in found
    assert ("music", "music_region") in found


def test_unknown_annotations_are_not_guessed_at():
    assert markers.find_annotations("[crosstalk] and [00:12]") == []


def test_detect_markers_builds_high_confidence_events():
    entries = [TranscriptEntry(4.0, 6.0, "[laughs] oh no")]
    events = markers.detect_markers(entries, source_file="/f/c.mp4", asset_id="a1")
    assert len(events) == 1
    assert events[0].type == "possible_laughter"
    assert events[0].detection == "transcript_marker"
    # Someone actually named the sound, so this beats any loudness guess.
    assert events[0].confidence > AudioConfig().max_inferred_confidence
    assert (events[0].start, events[0].end) == (4.0, 6.0)


# ---------------------------------------------------------------------------
# Analyzer and caching
# ---------------------------------------------------------------------------

def _analyzer(config, audio_config, cache, source):
    return AudioAnalyzer(config, audio_config, cache=cache, source=source)


def test_analyzer_produces_events(
    config, audio_config, cache, asset, audio_source_factory, make_envelope
):
    source = audio_source_factory(
        samples=make_envelope((0, 5, -24.0), (5, 10, -95.0), (10, 16, -24.0)),
        silence=[Span(5.0, 10.0)],
    )
    result = _analyzer(config, audio_config, cache, source).analyze_asset(asset)
    assert result.events
    assert any(event.type == "silence" for event in result.events)
    assert result.samples > 0


def test_second_run_hits_the_cache(
    config, audio_config, cache, asset, audio_source_factory, make_envelope
):
    source = audio_source_factory(samples=make_envelope((0, 16, -24.0)))
    analyzer = _analyzer(config, audio_config, cache, source)

    first = analyzer.analyze_asset(asset)
    second = analyzer.analyze_asset(asset)

    assert first.cached is False
    assert second.cached is True
    assert source.calls == 1          # FFmpeg was not asked twice
    assert [e.to_dict() for e in second.events] == [e.to_dict() for e in first.events]


def test_refresh_bypasses_the_cache(
    config, audio_config, cache, asset, audio_source_factory, make_envelope
):
    source = audio_source_factory(samples=make_envelope((0, 16, -24.0)))
    analyzer = _analyzer(config, audio_config, cache, source)
    analyzer.analyze_asset(asset)
    assert analyzer.analyze_asset(asset, refresh=True).cached is False
    assert source.calls == 2


def test_changing_the_audio_config_invalidates(
    config, audio_config, cache, asset, audio_source_factory, make_envelope
):
    from dataclasses import replace

    source = audio_source_factory(samples=make_envelope((0, 16, -24.0)))
    _analyzer(config, audio_config, cache, source).analyze_asset(asset)

    stricter = replace(audio_config, silence_threshold_db=-60.0).validated()
    assert _analyzer(config, stricter, cache, source).analyze_asset(
        asset
    ).cached is False


def test_adding_a_transcript_invalidates(
    config, audio_config, cache, asset, audio_source_factory, make_envelope
):
    """Pauses, density and markers all come from the transcript."""
    source = audio_source_factory(samples=make_envelope((0, 16, -24.0)))
    analyzer = _analyzer(config, audio_config, cache, source)
    analyzer.analyze_asset(asset)

    transcript = Transcript(
        asset_id=asset.asset_id,
        entries=[TranscriptEntry(1, 3, "[laughs] hello")],
    )
    assert analyzer.analyze_asset(asset, transcript=transcript).cached is False


def test_changing_the_file_invalidates(
    config, audio_config, cache, asset, media_file, audio_source_factory,
    make_envelope
):
    source = audio_source_factory(samples=make_envelope((0, 16, -24.0)))
    analyzer = _analyzer(config, audio_config, cache, source)
    analyzer.analyze_asset(asset)
    media_file.write_bytes(b"a re-export with completely different content")
    assert analyzer.analyze_asset(asset).cached is False


def test_a_file_without_audio_says_so(
    config, audio_config, cache, asset, audio_source_factory
):
    from dataclasses import replace

    silent_asset = replace(asset, has_audio=False)
    source = audio_source_factory(has_audio=False)
    result = _analyzer(config, audio_config, cache, source).analyze_asset(silent_asset)

    assert result.events == []
    assert any("no audio track" in warning for warning in result.warnings)


def test_unreadable_audio_degrades_to_markers_only(
    config, audio_config, cache, asset, audio_source_factory
):
    """FFmpeg failing must not lose the evidence the transcript already has.

    Transcript-derived detectors (markers, pauses, speech density) need no
    audio samples at all, so they keep working -- only the loudness-derived
    ones go quiet.
    """
    source = audio_source_factory(samples=[])
    transcript = Transcript(
        asset_id=asset.asset_id, entries=[TranscriptEntry(2, 4, "[laughs] wow")]
    )
    result = _analyzer(config, audio_config, cache, source).analyze_asset(
        asset, transcript=transcript
    )

    kinds = {event.type for event in result.events}
    assert "possible_laughter" in kinds
    # Everything present came from the transcript, not from a loudness curve.
    assert all(
        event.detection == "transcript_marker" for event in result.events
    )
    assert not kinds & {"silence", "loudness_spike", "clipping", "low_energy"}
    assert result.samples == 0
    assert any("loudness data" in warning for warning in result.warnings)


def test_a_marker_supersedes_the_heuristic_guess(
    config, audio_config, cache, asset, audio_source_factory, make_envelope
):
    """The same laughter must not be counted twice as evidence."""
    bursts = []
    for index in range(4):
        start = 4.0 + index * 0.75
        bursts.append((start, start + 0.25, -10.0))
        bursts.append((start + 0.25, start + 0.75, -26.0))
    source = audio_source_factory(
        samples=make_envelope((0, 4, -24.0), *bursts, (7, 16, -24.0))
    )
    transcript = Transcript(
        asset_id=asset.asset_id, entries=[TranscriptEntry(4, 7, "[laughs] oh no")]
    )
    result = _analyzer(config, audio_config, cache, source).analyze_asset(
        asset, transcript=transcript
    )
    laughter = [e for e in result.events if e.type == "possible_laughter"]
    assert len(laughter) == 1
    assert laughter[0].detection == "transcript_marker"


def test_audio_result_round_trips():
    original = AudioResult(
        asset_id="a1", source_file="/f/c.mp4", baseline_db=-24.0, samples=100,
        events=[AudioEvent(event_id="au1", source_file="/f/c.mp4", type="silence")],
        warnings=["something"],
    )
    restored = AudioResult.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.asset_id == "a1"
    assert len(restored.events) == 1
    assert restored.warnings == ["something"]

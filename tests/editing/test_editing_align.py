"""Transcript-to-visual alignment and the structure timeline.

The interesting behaviour under test is the three-way alignment verdict.
``match`` and ``neutral`` are easy to get right by accident; ``contrast`` -- a
player calmly saying "totally safe" over a creeper -- is the one an editor most
wants surfaced and the one a naive keyword check misses, so it gets the most
coverage here.
"""
from __future__ import annotations

import json

import pytest

from editing.align import (
    DEFAULT_USABLE_THRESHOLD, build_segments, build_timeline, classify_alignment,
    group_events, score_segment,
)
from editing.schema import (
    AudioEvent, MediaAsset, TimelineSegment, Transcript, TranscriptEntry,
    UIState, VisualEvent,
)


@pytest.fixture
def asset_a():
    return MediaAsset(
        asset_id="a_test", path="/footage/clip.mp4", filename="clip.mp4",
        duration=40.0,
    )


def transcript_of(*entries) -> Transcript:
    return Transcript(
        asset_id="a_test", source="srt",
        entries=[TranscriptEntry(start, end, text)
                 for start, end, text in entries],
    )


# ---------------------------------------------------------------------------
# Alignment: match
# ---------------------------------------------------------------------------

def test_match_on_the_action(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, environment="cave", actions=("mining",))],
        "okay we are mining for diamonds down here",
    )
    assert verdict.kind == "match"
    assert any("mining" in item or "diamonds" in item for item in verdict.evidence)


def test_match_on_the_environment(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, environment="nether", actions=("exploring",))],
        "the nether is so much scarier than I remember",
    )
    assert verdict.kind == "match"


def test_match_on_a_named_mob(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, entities=("a creeper",), actions=("fighting",))],
        "there is a creeper right behind me",
    )
    assert verdict.kind == "match"


def test_match_when_narration_acknowledges_danger(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, importance="danger", threats=("lava",))],
        "oh no oh no watch out",
    )
    assert verdict.kind == "match"


# ---------------------------------------------------------------------------
# Alignment: contrast -- the editorially valuable case
# ---------------------------------------------------------------------------

def test_contrast_when_narration_plays_down_real_danger(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, importance="danger", threats=("creeper",))],
        "this is totally fine nothing to worry about",
    )
    assert verdict.kind == "contrast"
    assert "plays down" in verdict.reason


def test_contrast_when_narration_hypes_an_empty_shot(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, environment="plains", actions=("travelling",),
                       importance="boring")],
        "this is absolutely insane I cannot believe it",
    )
    assert verdict.kind == "contrast"
    assert "excited" in verdict.reason


def test_contrast_when_narration_names_a_different_place(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, environment="cave", actions=("exploring",))],
        "we really need to get to the village before nightfall",
    )
    assert verdict.kind == "contrast"
    assert "village" in verdict.reason


def test_low_health_counts_as_danger_for_contrast(event_factory):
    event = event_factory(0, 8, importance="setup")
    event.ui = UIState(low_health=True)
    verdict = classify_alignment([event], "everything is under control here")
    assert verdict.kind == "contrast"


def test_agreement_beats_contrast_when_both_could_apply(event_factory):
    """"Safe" plus an on-screen creeper the player names is still a match."""
    verdict = classify_alignment(
        [event_factory(0, 8, importance="danger", threats=("creeper",),
                       entities=("creeper",))],
        "that creeper nearly got me but we are safe now",
    )
    assert verdict.kind == "match"


# ---------------------------------------------------------------------------
# Alignment: neutral and unknown
# ---------------------------------------------------------------------------

def test_neutral_when_speech_is_unrelated(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, environment="cave", actions=("mining",))],
        "make sure to like and subscribe it really helps the channel",
    )
    assert verdict.kind == "neutral"


def test_unknown_without_speech(event_factory):
    verdict = classify_alignment([event_factory(0, 8)], "   ")
    assert verdict.kind == "unknown"
    assert "No speech" in verdict.reason


def test_unknown_without_events():
    verdict = classify_alignment([], "talking over nothing")
    assert verdict.kind == "unknown"


def test_unknown_when_every_event_failed(event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, error="model was down")], "some words"
    )
    assert verdict.kind == "unknown"
    assert "failed" in verdict.reason


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def make_segment(events, said="", alignment="unknown", start=0.0, end=8.0):
    return TimelineSegment(
        segment_id="s1", asset_id="a_test", source_file="/footage/clip.mp4",
        start=start, end=end, said=said, events=list(events), alignment=alignment,
    )


def test_payoff_scores_above_boring(event_factory):
    payoff = make_segment([event_factory(0, 8, importance="payoff")])
    boring = make_segment([event_factory(0, 8, importance="boring")])
    assert score_segment(payoff)[0] > score_segment(boring)[0]


def test_low_confidence_lowers_the_score(event_factory):
    sure = make_segment([event_factory(0, 8, importance="payoff", confidence=1.0)])
    unsure = make_segment([event_factory(0, 8, importance="payoff", confidence=0.25)])
    assert score_segment(sure)[0] > score_segment(unsure)[0]


def test_narration_raises_the_score(event_factory):
    silent = make_segment([event_factory(0, 8, importance="tension")])
    spoken = make_segment(
        [event_factory(0, 8, importance="tension")],
        said="this is the part where it all goes horribly wrong for me",
        alignment="match",
    )
    assert score_segment(spoken)[0] > score_segment(silent)[0]


def test_contrast_scores_like_match(event_factory):
    """A narration/visual mismatch is usually the joke, so it ranks too."""
    matched = make_segment(
        [event_factory(0, 8, importance="danger")], said="oh no", alignment="match"
    )
    contrasted = make_segment(
        [event_factory(0, 8, importance="danger")], said="all good", alignment="contrast"
    )
    assert score_segment(matched)[0] == pytest.approx(score_segment(contrasted)[0])


def test_neutral_scores_below_match(event_factory):
    matched = make_segment(
        [event_factory(0, 8, importance="tension")], said="a b c", alignment="match"
    )
    neutral = make_segment(
        [event_factory(0, 8, importance="tension")], said="a b c", alignment="neutral"
    )
    assert score_segment(matched)[0] > score_segment(neutral)[0]


def test_a_failed_segment_scores_zero_and_is_unusable(event_factory):
    segment = make_segment([event_factory(0, 8, error="model down")])
    score, usable, reasons = score_segment(segment)
    assert score == 0.0 and usable is False
    assert "No usable visual analysis." in reasons


def test_a_full_screen_ui_is_penalised(event_factory):
    plain = event_factory(0, 8, importance="payoff")
    covered = event_factory(0, 8, importance="payoff")
    covered.ui = UIState(inventory_open=True)
    assert score_segment(make_segment([covered]))[0] < score_segment(
        make_segment([plain])
    )[0]


def test_a_death_screen_raises_the_score(event_factory):
    plain = event_factory(0, 8, importance="danger")
    died = event_factory(0, 8, importance="danger")
    died.ui = UIState(death_screen=True)
    assert score_segment(make_segment([died]))[0] > score_segment(
        make_segment([plain])
    )[0]


def test_sub_second_segments_are_not_usable(event_factory):
    segment = make_segment(
        [event_factory(0, 0.5, importance="payoff")], start=0.0, end=0.5
    )
    assert score_segment(segment)[1] is False


def test_score_reasons_always_explain_the_verdict(event_factory):
    segment = make_segment(
        [event_factory(0, 8, importance="payoff", threats=("creeper",))],
        said="that was close", alignment="match",
    )
    _, _, reasons = score_segment(segment)
    assert any("importance=payoff" in reason for reason in reasons)
    assert any("creeper" in reason for reason in reasons)


def test_threshold_controls_usability(event_factory):
    segment = make_segment([event_factory(0, 8, importance="setup")])
    assert score_segment(segment, threshold=0.0)[1] is True
    assert score_segment(segment, threshold=0.99)[1] is False


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def test_similar_adjacent_events_merge(event_factory):
    events = [
        event_factory(0, 8, environment="cave", actions=("mining",)),
        event_factory(8, 16, environment="cave", actions=("mining",)),
        event_factory(16, 24, environment="cave", actions=("mining",)),
    ]
    assert len(group_events(events)) == 1


def test_a_change_of_action_splits(event_factory):
    events = [
        event_factory(0, 8, environment="cave", actions=("mining",)),
        event_factory(8, 16, environment="cave", actions=("fighting",),
                      importance="danger"),
    ]
    assert len(group_events(events)) == 2


def test_a_time_gap_splits(event_factory):
    """Merging across an unanalysed gap would claim coverage we do not have."""
    events = [
        event_factory(0, 8, environment="cave", actions=("mining",)),
        event_factory(40, 48, environment="cave", actions=("mining",)),
    ]
    assert len(group_events(events)) == 2


def test_merging_is_capped_by_duration(event_factory):
    events = [
        event_factory(index * 8, (index + 1) * 8,
                      environment="cave", actions=("mining",))
        for index in range(10)
    ]
    groups = group_events(events, max_segment_seconds=30.0)
    assert len(groups) > 1
    for group in groups:
        assert group[-1].end - group[0].start <= 30.0


def test_failed_events_never_merge(event_factory):
    events = [
        event_factory(0, 8, error="down"),
        event_factory(8, 16, error="down"),
    ]
    assert len(group_events(events)) == 2


def test_merging_can_be_disabled(event_factory):
    events = [
        event_factory(0, 8, environment="cave", actions=("mining",)),
        event_factory(8, 16, environment="cave", actions=("mining",)),
    ]
    assert len(group_events(events, merge_similar=False)) == 2


# ---------------------------------------------------------------------------
# Segment building
# ---------------------------------------------------------------------------

def test_speech_is_attached_by_overlap(asset_a, event_factory):
    events = [event_factory(0, 8, importance="setup"),
              event_factory(8, 16, importance="danger")]
    transcript = transcript_of(
        (1.0, 4.0, "first line"), (9.0, 12.0, "second line"), (30.0, 32.0, "later"),
    )
    segments = build_segments(asset_a, events, transcript, merge_similar=False)

    assert segments[0].said == "first line"
    assert segments[1].said == "second line"
    assert "later" not in " ".join(segment.said for segment in segments)


def test_a_line_spanning_two_segments_appears_in_both(asset_a, event_factory):
    """It is genuinely audible over both, so both must carry it."""
    events = [event_factory(0, 8, importance="setup"),
              event_factory(8, 16, importance="danger")]
    transcript = transcript_of((6.0, 10.0, "straddling the boundary"))
    segments = build_segments(asset_a, events, transcript, merge_similar=False)
    assert segments[0].said == segments[1].said == "straddling the boundary"


def test_segments_carry_their_transcript_entries(asset_a, event_factory):
    transcript = transcript_of((1.0, 4.0, "one"), (5.0, 7.0, "two"))
    segments = build_segments(
        asset_a, [event_factory(0, 8)], transcript, merge_similar=False
    )
    assert [entry.text for entry in segments[0].speech_entries] == ["one", "two"]
    assert segments[0].said == "one two"


def test_segment_ids_are_stable(asset_a, event_factory):
    first = build_segments(asset_a, [event_factory(0, 8)])
    second = build_segments(asset_a, [event_factory(0, 8)])
    assert first[0].segment_id == second[0].segment_id


def test_a_transcript_only_file_still_produces_segments(asset_a):
    """Speech alone tells an editor where the talking is; say so honestly."""
    transcript = transcript_of((1.0, 4.0, "talking with no analysis"))
    segments = build_segments(asset_a, [], transcript)

    assert len(segments) == 1
    assert segments[0].alignment == "unknown"
    assert segments[0].usable is False
    assert "no visual analysis" in segments[0].alignment_reason


def test_no_events_and_no_transcript_produces_nothing(asset_a):
    assert build_segments(asset_a, [], None) == []


# ---------------------------------------------------------------------------
# Whole timeline
# ---------------------------------------------------------------------------

def test_build_timeline_end_to_end(asset_a, event_factory):
    events = [
        event_factory(0, 8, environment="cave", actions=("mining",),
                      importance="setup"),
        event_factory(8, 16, environment="cave", actions=("fighting",),
                      importance="danger", entities=("creeper",),
                      threats=("creeper",)),
        event_factory(16, 24, environment="forest", actions=("travelling",),
                      importance="boring"),
    ]
    transcript = transcript_of(
        (1.0, 6.0, "okay we are mining for diamonds"),
        (9.0, 14.0, "this is completely safe nothing to worry about"),
        (17.0, 22.0, "this is absolutely insane"),
    )
    timeline = build_timeline(
        [asset_a], {"a_test": events}, {"a_test": transcript},
        model="Qwen3-VL-8B-Instruct",
    )

    assert len(timeline.segments) == 3
    assert timeline.segments[0].alignment == "match"
    assert timeline.segments[1].alignment == "contrast"
    assert timeline.segments[1].usable is True

    stats = timeline.stats()
    assert stats["assets"] == 1
    assert stats["segments_with_speech"] == 3
    assert stats["by_importance"]["danger"] == 1

    assert timeline.highlights()[0] is timeline.segments[1]
    assert timeline.model == "Qwen3-VL-8B-Instruct"


def test_timeline_warns_about_unanalysed_files(asset_a):
    timeline = build_timeline([asset_a], {}, {})
    assert timeline.segments == []
    assert any("clip.mp4" in warning for warning in timeline.warnings)


def test_timeline_records_transcript_provenance(asset_a, event_factory):
    timeline = build_timeline(
        [asset_a], {"a_test": [event_factory(0, 8)]}, {},
        transcript_sources={"a_test": {"found": False, "source": "none"}},
    )
    assert timeline.transcript_sources["a_test"]["found"] is False


def test_timeline_segment_order_is_stable(asset_a, event_factory):
    events = [event_factory(16, 24, importance="payoff"),
              event_factory(0, 8, importance="setup")]
    timeline = build_timeline([asset_a], {"a_test": events}, {})
    starts = [segment.start for segment in timeline.segments]
    assert starts == sorted(starts)


def test_multiple_assets_keep_their_segments_separate(event_factory):
    first = MediaAsset(asset_id="a1", path="/f/one.mp4", filename="one.mp4",
                       duration=16.0)
    second = MediaAsset(asset_id="a2", path="/f/two.mp4", filename="two.mp4",
                        duration=16.0)
    timeline = build_timeline(
        [first, second],
        {
            "a1": [event_factory(0, 8, asset_id="a1", source_file="/f/one.mp4")],
            "a2": [event_factory(0, 8, asset_id="a2", source_file="/f/two.mp4")],
        },
        {},
    )
    assert len(timeline.segments_for("a1")) == 1
    assert len(timeline.segments_for("a2")) == 1


def test_usable_threshold_is_honoured(asset_a, event_factory):
    events = [event_factory(0, 8, importance="setup")]
    strict = build_timeline([asset_a], {"a_test": events}, {}, usable_threshold=0.99)
    loose = build_timeline([asset_a], {"a_test": events}, {}, usable_threshold=0.0)
    assert strict.segments[0].usable is False
    assert loose.segments[0].usable is True


def test_default_threshold_is_exposed():
    assert 0.0 < DEFAULT_USABLE_THRESHOLD < 1.0


# ---------------------------------------------------------------------------
# Audio alignment -- the third channel
# ---------------------------------------------------------------------------

def audio_of(*specs):
    """``(start, end, type[, confidence[, detection]])`` -> AudioEvents."""
    out = []
    for spec in specs:
        start, end, kind = spec[0], spec[1], spec[2]
        confidence = spec[3] if len(spec) > 3 else 0.8
        detection = spec[4] if len(spec) > 4 else "heuristic"
        out.append(AudioEvent(
            event_id=f"au_{start}_{kind}", source_file="/footage/clip.mp4",
            asset_id="a_test", start=start, end=end, type=kind,
            confidence=confidence, detection=detection,
            loudness_db=-8.0, baseline_db=-24.0,
        ))
    return out


def test_audio_events_attach_to_the_overlapping_segment(asset_a, event_factory):
    events = [event_factory(0, 8), event_factory(8, 16, importance="danger")]
    audio = audio_of((1.0, 3.0, "silence"), (9.0, 10.0, "sudden_reaction"))

    segments = build_segments(
        asset_a, events, None, audio_events=audio, merge_similar=False
    )
    assert [e.type for e in segments[0].audio_events] == ["silence"]
    assert [e.type for e in segments[1].audio_events] == ["sudden_reaction"]


def test_an_audio_event_spanning_two_segments_appears_in_both(
    asset_a, event_factory
):
    """It is genuinely audible over both, exactly like a spoken line."""
    events = [event_factory(0, 8), event_factory(8, 16, importance="danger")]
    audio = audio_of((6.0, 10.0, "music_region"))

    segments = build_segments(
        asset_a, events, None, audio_events=audio, merge_similar=False
    )
    assert segments[0].audio_types() == {"music_region"}
    assert segments[1].audio_types() == {"music_region"}


def test_audio_events_outside_every_segment_are_dropped(asset_a, event_factory):
    segments = build_segments(
        asset_a, [event_factory(0, 8)],
        None, audio_events=audio_of((50.0, 55.0, "silence")),
    )
    assert segments[0].audio_events == []


def test_audio_events_survive_a_merge(asset_a, event_factory):
    """Merging three similar events must not lose the audio on any of them."""
    events = [
        event_factory(0, 8, environment="cave", actions=("mining",)),
        event_factory(8, 16, environment="cave", actions=("mining",)),
        event_factory(16, 24, environment="cave", actions=("mining",)),
    ]
    audio = audio_of((2.0, 3.0, "loudness_spike"), (18.0, 19.0, "sudden_reaction"))

    segments = build_segments(
        asset_a, events, None, audio_events=audio, merge_similar=True
    )
    assert len(segments) == 1
    assert segments[0].audio_types() == {"loudness_spike", "sudden_reaction"}


def test_build_timeline_routes_audio_per_asset(event_factory):
    """Two files must not receive each other's audio events."""
    first = MediaAsset(asset_id="a1", path="/f/one.mp4", filename="one.mp4",
                       duration=16.0)
    second = MediaAsset(asset_id="a2", path="/f/two.mp4", filename="two.mp4",
                        duration=16.0)

    def audio_for(asset_id, kind):
        return [AudioEvent(
            event_id=f"au_{asset_id}", source_file="f", asset_id=asset_id,
            start=1.0, end=2.0, type=kind, confidence=0.8,
        )]

    timeline = build_timeline(
        [first, second],
        {
            "a1": [event_factory(0, 8, asset_id="a1", source_file="/f/one.mp4")],
            "a2": [event_factory(0, 8, asset_id="a2", source_file="/f/two.mp4")],
        },
        {},
        audio_by_asset={
            "a1": audio_for("a1", "silence"),
            "a2": audio_for("a2", "sudden_reaction"),
        },
    )
    assert timeline.segments_for("a1")[0].audio_types() == {"silence"}
    assert timeline.segments_for("a2")[0].audio_types() == {"sudden_reaction"}


def test_a_transcript_only_timeline_still_carries_audio(asset_a):
    """The speech-only fallback path must not drop the audio channel."""
    transcript = transcript_of((1.0, 4.0, "talking with no visual analysis"))
    segments = build_segments(
        asset_a, [], transcript, audio_events=audio_of((2.0, 3.0, "possible_laughter"))
    )
    assert segments[0].audio_types() == {"possible_laughter"}


# -- segment properties derived from audio ----------------------------------

def test_dead_air_needs_silence_and_no_speech(asset_a, event_factory):
    segments = build_segments(
        asset_a, [event_factory(0, 8)], None,
        audio_events=audio_of((0.0, 8.0, "silence", 0.9)),
    )
    assert segments[0].is_dead_air is True


def test_silence_with_narration_over_it_is_not_dead_air(asset_a, event_factory):
    transcript = transcript_of((1.0, 7.0, "still talking over the quiet"))
    segments = build_segments(
        asset_a, [event_factory(0, 8)], transcript,
        audio_events=audio_of((0.0, 8.0, "silence", 0.9)),
    )
    assert segments[0].is_dead_air is False


def test_a_brief_quiet_patch_is_not_dead_air(asset_a, event_factory):
    """A one-second gap inside eight seconds is normal speech rhythm."""
    segments = build_segments(
        asset_a, [event_factory(0, 8)], None,
        audio_events=audio_of((3.0, 4.0, "silence", 0.9)),
    )
    assert segments[0].is_dead_air is False


def test_audio_reaction_picks_the_most_confident(asset_a, event_factory):
    segments = build_segments(
        asset_a, [event_factory(0, 8)], None,
        audio_events=audio_of(
            (1.0, 2.0, "loudness_spike", 0.6),
            (3.0, 4.0, "possible_laughter", 0.9, "transcript_marker"),
        ),
    )
    assert segments[0].audio_reaction.type == "possible_laughter"


def test_silence_is_not_a_reaction(asset_a, event_factory):
    segments = build_segments(
        asset_a, [event_factory(0, 8)], None,
        audio_events=audio_of((1.0, 5.0, "silence", 0.95)),
    )
    assert segments[0].audio_reaction is None


# -- audio changing the verdict ---------------------------------------------

def test_a_wordless_scream_over_danger_is_a_match(asset_a, event_factory):
    """Invisible to a text-only check: there are no words at all."""
    verdict = classify_alignment(
        [event_factory(0, 8, importance="danger", threats=("creeper",))],
        "",
        audio_events=audio_of((2.0, 3.0, "possible_scream")),
    )
    assert verdict.kind == "match"
    assert "audio:possible_scream" in verdict.evidence


def test_laughter_over_an_uneventful_shot_is_a_contrast(asset_a, event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8, importance="boring")],
        "anyway so as I was saying about the thing",
        audio_events=audio_of(
            (2.0, 4.0, "possible_laughter", 0.85, "transcript_marker")
        ),
    )
    assert verdict.kind == "contrast"


def test_a_silent_segment_says_so_rather_than_shrugging(asset_a, event_factory):
    verdict = classify_alignment(
        [event_factory(0, 8)], "", audio_events=audio_of((0.0, 8.0, "silence"))
    )
    assert verdict.kind == "unknown"
    assert "Silent" in verdict.reason


def test_dead_air_is_never_usable_however_good_the_picture(
    asset_a, event_factory
):
    segments = build_segments(
        asset_a, [event_factory(0, 8, importance="payoff", confidence=1.0)],
        None, audio_events=audio_of((0.0, 8.0, "silence", 0.9)),
    )
    assert segments[0].usable is False
    assert any("dead air" in reason for reason in segments[0].reasons)


def test_an_audio_reaction_lifts_a_segment(asset_a, event_factory):
    quiet = build_segments(asset_a, [event_factory(0, 8, importance="setup")], None)
    lifted = build_segments(
        asset_a, [event_factory(0, 8, importance="setup")], None,
        audio_events=audio_of((2.0, 3.0, "sudden_reaction", 0.9)),
    )
    assert lifted[0].usefulness > quiet[0].usefulness
    assert any("audio:" in reason for reason in lifted[0].reasons)


def test_clipping_is_penalised_and_named(asset_a, event_factory):
    segments = build_segments(
        asset_a, [event_factory(0, 8, importance="payoff")], None,
        audio_events=audio_of((1.0, 2.0, "clipping", 0.85)),
    )
    assert any("clipping" in reason for reason in segments[0].reasons)


# -- export ------------------------------------------------------------------

def test_audio_events_survive_the_export_round_trip(asset_a, event_factory):
    """The deliverable must carry the audio channel, not just compute with it."""
    from editing.schema import StructureTimeline

    timeline = build_timeline(
        [asset_a], {"a_test": [event_factory(0, 8)]}, {},
        audio_by_asset={"a_test": audio_of((1.0, 3.0, "sudden_reaction", 0.8))},
    )
    document = json.loads(json.dumps(timeline.to_dict()))

    exported = document["segments"][0]
    assert exported["audio_events"][0]["type"] == "sudden_reaction"
    assert exported["audio_types"] == ["sudden_reaction"]
    assert "is_dead_air" in exported

    restored = StructureTimeline.from_dict(document)
    assert restored.segments[0].audio_events[0].type == "sudden_reaction"
    assert restored.segments[0].audio_reaction is not None

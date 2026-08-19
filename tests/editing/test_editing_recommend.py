"""Layered edit recommendations, the safety pass, and the draft Premiere plan.

The tests that matter most here are the negative ones. Anyone can make a
planner that suggests edits; the value is in what it refuses to suggest, so
there is a test for each safety mechanism and several for the guarantee that
nothing is ever executed.
"""
from __future__ import annotations

import json

import pytest

from editing.align import build_timeline
from editing.recommend import layers as layer_module
from editing.recommend.planner import PlannerOptions, plan_recommendations
from editing.recommend.premiere_plan import (
    CONVERTIBLE, DraftPlan, build_and_dry_run, build_plan, dry_run,
)
from editing.recommend.report import render, render_top_moments
from editing.recommend.schema import (
    ACTIVE_CATEGORIES, EDIT_CATEGORIES, INTENSITIES, RISKS, STATUSES,
    VIEWER_EFFECTS, EditRecommendation, Evidence, RecommendationSet,
)
from editing.schema import (
    AudioEvent, MediaAsset, TimeRange, Transcript, TranscriptEntry, UIState,
    VisualEvent,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

ASSET = MediaAsset(
    asset_id="a_test", path="/footage/clip.mp4", filename="clip.mp4", duration=200.0
)


def visual(start, end, *, environment="cave", actions=("mining",),
           importance="setup", entities=(), threats=(), ui=None, confidence=0.85):
    event = VisualEvent(
        event_id=f"e_{start}", source_file=ASSET.path, asset_id=ASSET.asset_id,
        start=start, end=end, confidence=confidence, environment=environment,
        actions=list(actions), entities=list(entities), threats=list(threats),
        importance=importance, suggested_range=TimeRange(start, end),
        model="Qwen3-VL-8B-Instruct",
    )
    if ui is not None:
        event.ui = ui
    return event


def audio(start, end, kind, *, confidence=0.8, detection="heuristic"):
    return AudioEvent(
        event_id=f"au_{start}_{kind}", source_file=ASSET.path,
        asset_id=ASSET.asset_id, start=start, end=end, type=kind,
        confidence=confidence, detection=detection, loudness_db=-8.0,
        baseline_db=-24.0,
    )


def timeline_of(events, *, audio_events=(), lines=()):
    transcript = Transcript(
        asset_id=ASSET.asset_id, source="srt",
        entries=[TranscriptEntry(*line) for line in lines],
    ) if lines else None
    return build_timeline(
        [ASSET], {ASSET.asset_id: list(events)},
        {ASSET.asset_id: transcript} if transcript else {},
        audio_by_asset={ASSET.asset_id: list(audio_events)},
        model="Qwen3-VL-8B-Instruct",
    )


@pytest.fixture
def rich_timeline():
    """A timeline with every kind of moment the layers care about."""
    return timeline_of(
        [
            visual(0, 10, environment="forest", actions=("travelling",)),
            visual(10, 20, importance="boring"),
            visual(20, 30, importance="tension"),
            visual(30, 40, importance="payoff", actions=("looting",)),
            visual(40, 50, importance="danger", entities=("creeper",),
                   threats=("creeper",)),
            visual(50, 60, importance="reveal"),
        ],
        audio_events=[
            audio(10, 20, "silence", confidence=0.9),
            audio(41, 42, "sudden_reaction"),
            audio(31, 33, "possible_laughter", confidence=0.85,
                  detection="transcript_marker"),
        ],
        lines=[(2, 8, "heading into the cave"), (31, 34, "[laughs] no way"),
               (41, 44, "this is completely safe nothing to worry about")],
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_recommendation_round_trips():
    original = EditRecommendation(
        recommendation_id="r1", asset_id="a1", source_file="/f/c.mp4",
        start=10.0, end=18.0, category="punch_in", priority=0.7,
        reason="threat on screen",
        evidence=Evidence(visual_event_ids=["e1"], transcript_quotes=["oh no"],
                          audio_event_ids=["au1"], audio_types=["sudden_reaction"]),
        intensity="medium", effects=["tension"], risks=["hides_gameplay"],
        layer="visual", premiere_ops=[{"op": "marker.add", "time": 10}],
    )
    restored = EditRecommendation.from_dict(
        json.loads(json.dumps(original.to_dict()))
    )
    assert restored.to_dict() == original.to_dict()
    assert restored.duration == 8.0


def test_recommendation_coerces_unknown_vocabulary():
    entry = EditRecommendation.from_dict({
        "category": "do a backflip", "intensity": "extreme",
        "effects": ["vibes"], "risks": ["nonsense"], "status": "maybe",
    })
    assert entry.category == "unknown"
    assert entry.intensity == "low"
    assert entry.effects == []
    assert entry.risks == []
    assert entry.status == "accepted"


@pytest.mark.parametrize("collection", [
    EDIT_CATEGORIES, INTENSITIES, VIEWER_EFFECTS, RISKS, STATUSES
])
def test_vocabularies_are_non_empty_and_unique(collection):
    assert collection and len(set(collection)) == len(collection)


def test_active_categories_are_all_real_categories():
    assert ACTIVE_CATEGORIES <= set(EDIT_CATEGORIES)


def test_hold_is_a_first_class_category():
    """A planner that cannot say "leave this alone" will edit everything."""
    assert "hold" in EDIT_CATEGORIES
    assert "hold" not in ACTIVE_CATEGORIES


def test_evidence_channels():
    evidence = Evidence(visual_event_ids=["e1"], audio_event_ids=["au1"])
    assert evidence.channels == ["visual", "audio"]
    assert evidence.is_empty is False
    assert Evidence().is_empty is True


def test_recommendation_without_evidence_is_not_actionable():
    entry = EditRecommendation(
        recommendation_id="r", asset_id="a", source_file="f", start=0, end=1
    )
    assert entry.has_evidence is False
    assert entry.is_actionable is False


def test_downgrade_steps_intensity_down():
    entry = EditRecommendation(
        recommendation_id="r", asset_id="a", source_file="f", start=0, end=1,
        category="punch_in", intensity="high",
    )
    entry.downgrade("too much")
    assert entry.intensity == "medium"
    assert entry.status == "downgraded"
    assert entry.status_reason == "too much"


def test_downgrading_the_lowest_intensity_becomes_a_hold():
    entry = EditRecommendation(
        recommendation_id="r", asset_id="a", source_file="f", start=0, end=1,
        category="punch_in", intensity="low",
        premiere_ops=[{"op": "marker.add", "time": 0}],
    )
    entry.downgrade("over budget")
    assert entry.status == "hold"
    assert entry.category == "hold"
    assert entry.premiere_ops == []          # cannot still carry operations
    assert entry.was_softened is True
    assert entry.is_deliberate_hold is False


def test_a_deliberate_hold_is_not_a_softened_one():
    """The report must not claim restraint the planner was forced into."""
    entry = EditRecommendation(
        recommendation_id="r", asset_id="a", source_file="f", start=0, end=1,
        category="hold",
    )
    assert entry.is_deliberate_hold is True
    assert entry.was_softened is False


def test_reject_keeps_the_record_but_drops_the_ops():
    entry = EditRecommendation(
        recommendation_id="r", asset_id="a", source_file="f", start=0, end=1,
        category="punch_in", premiere_ops=[{"op": "marker.add", "time": 0}],
    )
    entry.reject("hides gameplay")
    assert entry.status == "rejected"
    assert entry.premiere_ops == []
    assert entry.is_actionable is False


def test_recommendation_set_round_trips():
    original = RecommendationSet(
        recommendations=[EditRecommendation(
            recommendation_id="r", asset_id="a", source_file="f",
            start=0, end=1, category="marker",
            evidence=Evidence(visual_event_ids=["e1"]),
        )],
        layer_counts={"story": 1}, warnings=["careful"],
    )
    restored = RecommendationSet.from_dict(
        json.loads(json.dumps(original.to_dict()))
    )
    assert len(restored) == 1
    assert restored.layer_counts == {"story": 1}
    assert restored.warnings == ["careful"]


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

def test_story_layer_marks_high_value_moments(rich_timeline):
    produced = layer_module.layer_story(rich_timeline.segments)
    reasons = " ".join(entry.reason.lower() for entry in produced)
    assert "payoff" in reasons
    assert "danger" in reasons or "reveal" in reasons
    assert all(entry.layer == "story" for entry in produced)


def test_story_layer_marks_a_death():
    timeline = timeline_of([
        visual(0, 10, actions=("dying",), importance="danger",
               ui=UIState(death_screen=True)),
    ])
    produced = layer_module.layer_story(timeline.segments)
    assert any("Death" in entry.reason for entry in produced)


def test_story_layer_cuts_on_a_scene_change():
    timeline = timeline_of([
        visual(0, 10, environment="forest", actions=("travelling",)),
        visual(10, 20, environment="nether", actions=("exploring",)),
    ])
    cuts = [
        entry for entry in layer_module.layer_story(timeline.segments)
        if entry.category == "structure_cut"
    ]
    assert cuts
    assert "forest" in cuts[0].reason and "nether" in cuts[0].reason


def test_pacing_trims_dead_air():
    timeline = timeline_of(
        [visual(0, 10, importance="setup")],
        audio_events=[audio(0, 10, "silence", confidence=0.9)],
    )
    produced = layer_module.layer_pacing(timeline.segments)
    assert any(entry.category == "trim_dead_air" for entry in produced)


def test_pacing_holds_a_strong_raw_moment(rich_timeline):
    holds = [
        entry for entry in layer_module.layer_pacing(rich_timeline.segments)
        if entry.category == "hold"
    ]
    assert holds
    assert any("leave it alone" in entry.reason for entry in holds)


def test_pacing_preserves_anticipation_before_a_payoff():
    timeline = timeline_of([
        visual(0, 10, importance="tension"),
        visual(10, 20, importance="payoff", actions=("looting",)),
    ])
    produced = layer_module.layer_pacing(timeline.segments)
    assert any("Anticipation" in entry.reason for entry in produced)


def test_pacing_speeds_up_silent_boring_footage():
    timeline = timeline_of([visual(0, 10, importance="boring")])
    produced = layer_module.layer_pacing(timeline.segments)
    assert any(entry.category == "speed_ramp" for entry in produced)


def test_visual_layer_punches_in_on_a_threat():
    timeline = timeline_of([
        visual(0, 10, importance="danger", entities=("creeper",),
               threats=("creeper",)),
    ])
    produced = layer_module.layer_visual(timeline.segments)
    assert any(entry.category == "punch_in" for entry in produced)


def test_visual_layer_skips_a_segment_that_is_too_short():
    timeline = timeline_of([
        visual(0, 1.0, importance="reveal"),
    ])
    assert layer_module.layer_visual(timeline.segments) == []


def test_visual_layer_adds_text_on_a_contrast():
    timeline = timeline_of(
        [visual(0, 10, importance="danger", threats=("creeper",))],
        lines=[(1, 8, "this is completely safe nothing to worry about")],
    )
    produced = layer_module.layer_visual(timeline.segments)
    assert any(entry.category == "text_overlay" for entry in produced)


def test_audio_layer_suggests_music_on_a_payoff():
    timeline = timeline_of([visual(0, 10, importance="payoff")])
    produced = layer_module.layer_audio(timeline.segments)
    assert any(entry.category == "music_cue" for entry in produced)


def test_audio_layer_places_a_sound_on_a_reaction():
    timeline = timeline_of(
        [visual(0, 10, importance="danger")],
        audio_events=[audio(4, 5, "sudden_reaction")],
    )
    produced = layer_module.layer_audio(timeline.segments)
    effects = [entry for entry in produced if entry.category == "sound_effect"]
    assert effects
    # Timed to the reaction itself, not to the whole segment.
    assert effects[0].start == 4.0 and effects[0].end == 5.0


def test_audio_layer_ducks_music_under_speech():
    timeline = timeline_of(
        [visual(0, 10)],
        audio_events=[audio(0, 10, "music_region", confidence=0.4)],
        lines=[(1, 8, "talking over the music here")],
    )
    produced = layer_module.layer_audio(timeline.segments)
    assert any(entry.category == "ducking" for entry in produced)


def test_polish_flags_a_dark_environment():
    timeline = timeline_of([visual(0, 10, environment="cave")])
    produced = layer_module.layer_polish(timeline.segments)
    assert any(entry.category == "color_adjust" for entry in produced)


def test_polish_warns_about_an_open_ui():
    timeline = timeline_of([
        visual(0, 10, ui=UIState(inventory_open=True)),
    ])
    produced = layer_module.layer_polish(timeline.segments)
    assert any("UI open" in entry.reason for entry in produced)


# ---------------------------------------------------------------------------
# Safety pass -- the important half
# ---------------------------------------------------------------------------

def test_safety_rejects_a_recommendation_without_evidence():
    timeline = timeline_of([visual(0, 10)])
    naked = EditRecommendation(
        recommendation_id="r", asset_id=ASSET.asset_id, source_file=ASSET.path,
        start=0.0, end=10.0, category="punch_in", priority=0.9,
    )
    result = layer_module.layer_safety([naked], timeline.segments)
    assert result[0].status == "rejected"
    assert "evidence" in result[0].status_reason


def test_safety_rejects_a_zoom_over_an_open_inventory():
    timeline = timeline_of([
        visual(0, 10, importance="payoff", ui=UIState(inventory_open=True)),
    ])
    entry = layer_module._make(
        timeline.segments[0], category="punch_in", reason="test",
        layer="visual", priority=0.9,
    )
    result = layer_module.layer_safety([entry], timeline.segments)
    assert result[0].status == "rejected"
    assert "hide" in result[0].status_reason


def test_safety_rejects_a_zoom_that_would_crop_low_health():
    timeline = timeline_of([
        visual(0, 10, importance="danger", ui=UIState(low_health=True)),
    ])
    entry = layer_module._make(
        timeline.segments[0], category="punch_in", reason="test",
        layer="visual", priority=0.9,
    )
    result = layer_module.layer_safety([entry], timeline.segments)
    assert result[0].status == "rejected"
    assert "HUD" in result[0].status_reason


def test_safety_rejects_a_transcript_only_edit_the_others_contradict():
    """Words alone are the weakest of the three channels."""
    timeline = timeline_of(
        [visual(0, 10, importance="boring")],
        lines=[(1, 8, "this is the most terrifying thing ever")],
    )
    segment = timeline.segments[0]
    entry = layer_module._make(
        segment, category="punch_in", reason="narration is excited",
        layer="visual", priority=0.9,
    )
    entry.evidence = Evidence(transcript_quotes=["this is the most terrifying"])
    result = layer_module.layer_safety([entry], timeline.segments)
    assert result[0].status == "rejected"
    assert "narration" in result[0].status_reason


def test_safety_downgrades_a_weak_single_channel_edit():
    timeline = timeline_of([visual(0, 10, importance="setup")])
    entry = layer_module._make(
        timeline.segments[0], category="punch_in", reason="maybe",
        layer="visual", priority=0.3, intensity="high",
    )
    entry.evidence = Evidence(visual_event_ids=["e_0"])
    result = layer_module.layer_safety([entry], timeline.segments)
    assert result[0].status == "downgraded"
    assert result[0].intensity == "medium"


def _distinct_segments(count, *, importance="danger"):
    """A timeline whose segments will not merge into one another.

    Adjacent events with the same environment/action/importance are merged by
    ``group_events`` -- correct behaviour, but it collapses a fixture meant to
    exercise per-segment rules. Alternating the environment keeps them apart.
    """
    events = [
        visual(index * 10, index * 10 + 10,
               environment="cave" if index % 2 == 0 else "nether",
               importance=importance, threats=("creeper",))
        for index in range(count)
    ]
    # Audio on every segment, so evidence spans two channels and the
    # single-channel check does not fire before the rule under test.
    audio_events = [
        audio(index * 10 + 1, index * 10 + 2, "sudden_reaction")
        for index in range(count)
    ]
    return timeline_of(events, audio_events=audio_events)


def test_safety_spaces_out_repeated_edits():
    """Two punch-ins ten seconds apart look like a tic."""
    timeline = _distinct_segments(2)
    entries = [
        layer_module._make(segment, category="punch_in", reason="threat",
                           layer="visual", priority=0.9, intensity="high")
        for segment in timeline.segments
    ]
    result = layer_module.layer_safety(
        entries, timeline.segments, min_repeat_gap=30.0, budget_seconds=1000.0
    )
    softened = [entry for entry in result if entry.was_softened]
    assert softened
    assert any("earlier" in entry.status_reason for entry in softened)


def test_safety_enforces_an_editing_budget():
    """The check that stops a calm episode becoming a music video."""
    timeline = _distinct_segments(6)
    entries = [
        layer_module._make(segment, category="punch_in", reason="threat",
                           layer="visual", priority=0.7 + index * 0.02,
                           intensity="high")
        for index, segment in enumerate(timeline.segments)
    ]
    result = layer_module.layer_safety(
        entries, timeline.segments, budget_seconds=30.0, min_repeat_gap=0.0
    )
    accepted = [
        entry for entry in result
        if entry.status == "accepted" and entry.category in ACTIVE_CATEGORIES
    ]
    assert len(accepted) <= 2                     # 60s covered / 30s budget
    assert any(
        "budget" in entry.status_reason
        for entry in result if entry.was_softened
    )


def test_the_budget_removes_the_weakest_first():
    """So the budget drops the least defensible ideas, not the last ones."""
    timeline = _distinct_segments(4)
    entries = [
        layer_module._make(
            segment, category="punch_in", reason="threat", layer="visual",
            priority=0.6 + index * 0.1, intensity="high",
        )
        for index, segment in enumerate(timeline.segments)
    ]
    original = {entry.recommendation_id: entry.priority for entry in entries}

    result = layer_module.layer_safety(
        entries, timeline.segments, budget_seconds=40.0, min_repeat_gap=0.0
    )
    strongest = max(result, key=lambda e: original[e.recommendation_id])
    weakest = min(result, key=lambda e: original[e.recommendation_id])
    assert strongest.status == "accepted"
    assert weakest.was_softened


def test_safety_leaves_markers_and_holds_alone():
    """Annotations change nothing, so plenty of them is fine."""
    timeline = timeline_of([
        visual(index * 10, index * 10 + 10) for index in range(6)
    ])
    entries = []
    for segment in timeline.segments:
        entries.append(layer_module._make(
            segment, category="marker", reason="note", layer="story", priority=0.5
        ))
        entries.append(layer_module._make(
            segment, category="hold", reason="leave alone", layer="pacing",
            priority=0.5,
        ))
    result = layer_module.layer_safety(
        entries, timeline.segments, budget_seconds=5.0, min_repeat_gap=60.0
    )
    assert all(entry.status == "accepted" for entry in result)


def test_safety_never_deletes():
    timeline = timeline_of([visual(0, 10, ui=UIState(inventory_open=True))])
    entries = [
        layer_module._make(timeline.segments[0], category="punch_in",
                           reason="x", layer="visual", priority=0.9),
        layer_module._make(timeline.segments[0], category="marker",
                           reason="y", layer="story", priority=0.5),
    ]
    result = layer_module.layer_safety(entries, timeline.segments)
    assert len(result) == 2                       # nothing vanished
    assert {entry.status for entry in result} == {"rejected", "accepted"}


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def test_planner_runs_every_layer(rich_timeline):
    result = plan_recommendations(rich_timeline)
    for name, _ in layer_module.PROPOSING_LAYERS:
        assert name in result.layer_counts
    assert "safety_rejected" in result.layer_counts
    assert len(result) > 0


def test_planner_cites_evidence_on_every_recommendation(rich_timeline):
    """The core promise: no proposal without something behind it."""
    result = plan_recommendations(rich_timeline)
    accepted = result.accepted()
    assert accepted
    assert all(entry.has_evidence for entry in accepted)
    assert all(entry.evidence.channels for entry in accepted)


def test_planner_uses_all_three_channels(rich_timeline):
    result = plan_recommendations(rich_timeline)
    channels = {
        channel
        for entry in result.recommendations
        for channel in entry.evidence.channels
    }
    assert channels == {"visual", "transcript", "audio"}


def test_planner_allows_hold(rich_timeline):
    result = plan_recommendations(rich_timeline)
    assert result.deliberate_holds()


def test_planner_does_not_edit_every_segment(rich_timeline):
    """Restraint is the point; an edit on every beat is the failure mode."""
    result = plan_recommendations(rich_timeline)
    active = [
        entry for entry in result.accepted()
        if entry.category in ACTIVE_CATEGORIES
    ]
    assert len(active) < len(rich_timeline.segments)


def test_planner_output_is_json_serialisable(rich_timeline):
    result = plan_recommendations(rich_timeline)
    restored = RecommendationSet.from_dict(json.loads(json.dumps(result.to_dict())))
    assert len(restored) == len(result)


def test_planner_is_deterministic(rich_timeline):
    first = plan_recommendations(rich_timeline)
    second = plan_recommendations(rich_timeline)
    assert [entry.to_dict() for entry in first.recommendations] == [
        entry.to_dict() for entry in second.recommendations
    ]


def test_planner_warns_when_there_is_no_audio():
    timeline = timeline_of([visual(0, 10)], lines=[(1, 8, "talking")])
    result = plan_recommendations(timeline)
    assert any("audio" in warning.lower() for warning in result.warnings)


def test_planner_warns_when_there_is_no_transcript():
    timeline = timeline_of([visual(0, 10)], audio_events=[audio(0, 5, "silence")])
    result = plan_recommendations(timeline)
    assert any("transcript" in warning.lower() for warning in result.warnings)


def test_planner_on_an_empty_timeline():
    from editing.schema import StructureTimeline

    result = plan_recommendations(StructureTimeline())
    assert len(result) == 0
    assert result.warnings


def test_skipping_safety_is_loudly_warned_about(rich_timeline):
    result = plan_recommendations(
        rich_timeline, options=PlannerOptions(skip_safety=True)
    )
    assert any("NOT been checked" in warning for warning in result.warnings)


def test_a_tighter_budget_produces_fewer_active_edits(rich_timeline):
    calm = plan_recommendations(
        rich_timeline, options=PlannerOptions(budget_seconds=1000.0)
    )
    busy = plan_recommendations(
        rich_timeline, options=PlannerOptions(budget_seconds=5.0)
    )

    def active(result):
        return sum(
            1 for entry in result.accepted()
            if entry.category in ACTIVE_CATEGORIES
        )

    assert active(calm) <= active(busy)


# ---------------------------------------------------------------------------
# Draft Premiere plan
# ---------------------------------------------------------------------------

def test_markers_convert_to_operations(rich_timeline):
    result = plan_recommendations(rich_timeline)
    draft = build_plan(result, asset_paths={ASSET.asset_id: ASSET.path})
    assert draft.ops
    assert all(op["op"] == "marker.add" for op in draft.ops)
    assert all(op["asset"] == ASSET.path for op in draft.ops)


def test_the_draft_plan_validates_offline(rich_timeline):
    """The whole dry-run promise: no Premiere, no bridge, real validation."""
    result = plan_recommendations(rich_timeline)
    draft = build_and_dry_run(result, asset_paths={ASSET.asset_id: ASSET.path})
    assert draft.valid is True
    assert draft.validation_error is None
    assert draft.explanation
    assert len(draft.explanation) == draft.operation_count


def test_nothing_is_executed(rich_timeline):
    result = plan_recommendations(rich_timeline)
    draft = build_and_dry_run(result, asset_paths={ASSET.asset_id: ASSET.path})
    assert draft.executed is False
    assert draft.to_dict()["executed"] is False
    # The plan carries the dry_run flag, so even a careless caller passing it
    # to the engine would validate rather than edit.
    assert draft.as_edit_plan()["dry_run"] is True


def test_the_plan_is_a_valid_edit_plan_shape(rich_timeline):
    from premiere import validator

    result = plan_recommendations(rich_timeline)
    draft = build_plan(result, asset_paths={ASSET.asset_id: ASSET.path})
    validated = validator.validate_plan(draft.as_edit_plan(), fps=30.0)
    assert validated["dry_run"] is True
    assert len(validated["ops"]) == draft.operation_count


def test_every_operation_is_in_the_catalog(rich_timeline):
    from premiere import catalog

    result = plan_recommendations(rich_timeline)
    draft = build_plan(result, asset_paths={ASSET.asset_id: ASSET.path})
    for op in draft.ops:
        assert op["op"] in catalog.OPS


def test_unconvertible_categories_are_reported_not_dropped(rich_timeline):
    result = plan_recommendations(rich_timeline)
    draft = build_plan(result, asset_paths={ASSET.asset_id: ASSET.path})
    assert draft.not_convertible
    for entry in draft.not_convertible:
        assert entry["reason"]
        assert entry["category"] not in CONVERTIBLE


def test_a_hold_produces_no_operations_and_is_not_an_error():
    entry = EditRecommendation(
        recommendation_id="r", asset_id="a1", source_file="/f/c.mp4",
        start=0.0, end=10.0, category="hold",
        evidence=Evidence(visual_event_ids=["e1"]),
    )
    draft = build_plan([entry])
    assert draft.ops == []
    assert draft.no_op and draft.no_op[0]["category"] == "hold"
    assert not draft.not_convertible


def test_rejected_recommendations_never_reach_the_plan():
    entry = EditRecommendation(
        recommendation_id="r", asset_id="a1", source_file="/f/c.mp4",
        start=0.0, end=10.0, category="marker",
        evidence=Evidence(visual_event_ids=["e1"]),
    )
    entry.reject("hides gameplay")
    assert build_plan([entry]).ops == []


def test_an_empty_plan_reports_why():
    draft = dry_run(DraftPlan())
    assert draft.valid is False
    assert draft.validation_error["code"] == "empty_plan"
    assert draft.validation_error["hint"]


def test_a_malformed_plan_is_reported_not_raised():
    draft = DraftPlan(ops=[{"op": "nonsense.operation", "time": 0}])
    result = dry_run(draft)
    assert result.valid is False
    assert result.validation_error
    assert "nonsense" in json.dumps(result.validation_error)


def test_draft_plan_round_trips(rich_timeline):
    result = plan_recommendations(rich_timeline)
    draft = build_and_dry_run(result, asset_paths={ASSET.asset_id: ASSET.path})
    document = json.loads(json.dumps(draft.to_dict()))
    assert document["valid"] is True
    assert document["executed"] is False
    assert document["plan"]["ops"]


def test_marker_comments_carry_the_evidence(rich_timeline):
    """So a human editor can judge a marker without opening the JSON."""
    result = plan_recommendations(rich_timeline)
    draft = build_plan(result, asset_paths={ASSET.asset_id: ASSET.path})
    comments = " ".join(op.get("comment", "") for op in draft.ops)
    assert "evidence:" in comments
    assert "priority" in comments


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def test_report_renders(rich_timeline):
    result = plan_recommendations(rich_timeline)
    draft = build_and_dry_run(result, asset_paths={ASSET.asset_id: ASSET.path})
    text = render(result, timeline=rich_timeline, draft=draft)

    assert "TOP MOMENTS" in text
    assert "DRAFT PREMIERE PLAN" in text
    assert "Nothing in this report has been applied" in text
    assert "executed   : False" in text


def test_report_shows_audio_reaction_moments(rich_timeline):
    result = plan_recommendations(rich_timeline)
    text = render(result, timeline=rich_timeline)
    assert "AUDIO REACTION MOMENTS" in text
    assert "sudden_reaction" in text or "possible_laughter" in text


def test_report_shows_what_was_removed():
    timeline = timeline_of([
        visual(0, 10, importance="payoff", ui=UIState(inventory_open=True)),
    ])
    result = plan_recommendations(timeline)
    for entry in result.recommendations:
        entry.reject("test rejection")
    text = render(result)
    assert "REMOVED OR SOFTENED" in text
    assert "test rejection" in text


def test_top_moments_renders(rich_timeline):
    text = render_top_moments(rich_timeline)
    assert "TOP MOMENTS" in text


def test_top_moments_on_an_empty_timeline():
    from editing.schema import StructureTimeline

    assert "no segment" in render_top_moments(StructureTimeline())

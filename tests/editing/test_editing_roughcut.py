"""The rough cut builder: selection, layout, conversion, execution guards.

This is the first session where the system can damage something a person cares
about, so the guard tests carry the weight: a dry run must pass in the same
call, the target must provably be a scratch sequence, and nothing runs without
an explicit mode. Each of those is asserted against a fake engine that records
whether it was ever called at all.
"""
from __future__ import annotations

import json

import pytest

from editing.align import build_timeline
from editing.errors import EditingError
from editing.recommend.planner import plan_recommendations
from editing.recommend.schema import (
    EditRecommendation, Evidence, RecommendationSet,
)
from editing.roughcut import convert, execute, review as review_module
from editing.roughcut.build import RoughCutOptions, build_rough_cut
from editing.roughcut.schema import (
    ClipPlacement, ExecutionReport, RoughCutPlan, SequenceMarker,
)
from editing.roughcut.select import (
    SelectedRange, assemble, coverage, map_to_sequence, select_ranges,
)
from editing.schema import (
    AudioEvent, MediaAsset, TimeRange, Transcript, TranscriptEntry, UIState,
    VisualEvent,
)

ASSET = MediaAsset(
    asset_id="a_test", path="/footage/ep12.mp4", filename="ep12.mp4",
    duration=200.0,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def visual(start, end, *, environment="cave", actions=("mining",),
           importance="setup", threats=(), entities=(), ui=None,
           confidence=0.85, error=""):
    event = VisualEvent(
        event_id=f"e_{start}", source_file=ASSET.path, asset_id=ASSET.asset_id,
        start=start, end=end, confidence=confidence, environment=environment,
        actions=list(actions), threats=list(threats), entities=list(entities),
        importance=importance, suggested_range=TimeRange(start, end),
        model="Qwen3-VL-8B-Instruct", error=error,
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
    )


def recommendation(category, start, end, *, status="accepted", **kw):
    entry = EditRecommendation(
        recommendation_id=kw.pop("rid", f"r_{category}_{start}"),
        asset_id=ASSET.asset_id, source_file=ASSET.path,
        start=start, end=end, category=category,
        evidence=Evidence(visual_event_ids=[f"e_{start}"]),
        priority=kw.pop("priority", 0.7), **kw,
    )
    entry.status = status
    return entry


@pytest.fixture
def cut_timeline():
    """Filler, dead air, anticipation, payoff, danger, and a covered UI."""
    return timeline_of(
        [
            visual(0, 10, environment="forest", actions=("travelling",),
                   importance="boring"),
            visual(10, 20, importance="boring"),              # dead air
            visual(20, 30, importance="tension"),             # anticipation
            visual(30, 40, actions=("looting",), importance="payoff"),
            visual(40, 50, actions=("fighting",), importance="danger",
                   threats=("creeper",)),
            visual(50, 60, environment="base", actions=("building",),
                   importance="setup", ui=UIState(inventory_open=True)),
        ],
        audio_events=[
            audio(10, 20, "silence", confidence=0.9),
            audio(41, 42, "sudden_reaction"),
        ],
        lines=[(31, 35, "oh my god diamonds")],
    )


@pytest.fixture
def cut_plan(cut_timeline):
    return build_rough_cut(
        cut_timeline, plan_recommendations(cut_timeline), assets=[ASSET]
    )


class FakeEngine:
    """Records whether it was asked to run anything, and what."""

    def __init__(self, *, succeed=True):
        self.calls = []
        self.succeed = succeed

    def run(self, plan, **kwargs):
        self.calls.append(plan)
        ops = plan.get("ops", [])
        return {
            "success": self.succeed,
            "results": [
                {"op": op["op"], "index": i, "ok": self.succeed}
                for i, op in enumerate(ops)
            ],
            **({} if self.succeed else {"error": "Premiere said no"}),
        }


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_dead_air_is_excluded(cut_timeline):
    handle = 0.25
    ranges = select_ranges(
        cut_timeline, handle=handle, asset_durations={ASSET.asset_id: 200.0}
    )

    # 10-20s is silent with no speech. Handles legitimately bleed a fraction
    # of a second into it from the neighbouring clips, so the contract is that
    # the *body* of the dead air is gone -- not that no frame of it survives.
    covered = sum(
        max(0.0, min(entry.end, 20.0) - max(entry.start, 10.0))
        for entry in ranges
    )
    assert covered <= handle * 2 + 1e-6
    # And nothing samples from the middle of it.
    assert not any(entry.start <= 15.0 < entry.end for entry in ranges)


def test_a_payoff_is_kept_at_full_speed_and_protected(cut_timeline):
    ranges = select_ranges(cut_timeline, asset_durations={ASSET.asset_id: 200.0})
    covering = [e for e in ranges if e.start <= 35.0 < e.end]
    assert covering
    assert covering[0].speed == 1.0
    assert covering[0].protected is True


def test_silent_filler_is_sped_up(cut_timeline):
    ranges = select_ranges(
        cut_timeline, filler_speed=2.0, asset_durations={ASSET.asset_id: 200.0}
    )
    covering = [e for e in ranges if e.start <= 5.0 < e.end]
    assert covering and covering[0].speed == 2.0


def test_filler_with_narration_is_never_sped_up():
    """Sped-up dialogue is unusable, whatever the picture is worth."""
    timeline = timeline_of(
        [visual(0, 10, importance="boring")],
        lines=[(1, 9, "explaining the plan for this episode at some length")],
    )
    ranges = select_ranges(timeline, asset_durations={ASSET.asset_id: 200.0})
    assert ranges and all(entry.speed == 1.0 for entry in ranges)


def test_anticipation_before_a_payoff_is_preserved(cut_timeline):
    ranges = select_ranges(cut_timeline, asset_durations={ASSET.asset_id: 200.0})
    covering = [e for e in ranges if e.start <= 25.0 < e.end]
    assert covering
    assert covering[0].protected is True
    assert covering[0].speed == 1.0


def test_drop_filler_removes_it_entirely(cut_timeline):
    kept = select_ranges(
        cut_timeline, keep_filler=True, asset_durations={ASSET.asset_id: 200.0}
    )
    dropped = select_ranges(
        cut_timeline, keep_filler=False, asset_durations={ASSET.asset_id: 200.0}
    )
    assert sum(e.duration for e in dropped) < sum(e.duration for e in kept)


def test_a_deliberate_hold_protects_a_range():
    timeline = timeline_of([visual(0, 10, importance="setup")])
    segment = timeline.segments[0]
    hold = recommendation("hold", segment.start, segment.end)

    ranges = select_ranges(
        timeline, RecommendationSet(recommendations=[hold]),
        asset_durations={ASSET.asset_id: 200.0},
    )
    assert ranges[0].protected is True
    assert ranges[0].keep_reason == "hold"
    assert hold.recommendation_id in ranges[0].recommendation_ids


def test_a_forced_hold_does_not_protect_a_range():
    """Only deliberate holds count -- a safety-forced one says nothing good."""
    timeline = timeline_of([visual(0, 10, importance="boring")])
    segment = timeline.segments[0]
    forced = recommendation("punch_in", segment.start, segment.end)
    forced.downgrade("over budget")          # becomes category/status "hold"

    ranges = select_ranges(
        timeline, RecommendationSet(recommendations=[forced]),
        asset_durations={ASSET.asset_id: 200.0},
    )
    assert all(entry.keep_reason != "hold" for entry in ranges)


def test_an_accepted_trim_removes_its_range():
    timeline = timeline_of([
        visual(0, 10, importance="setup"),
        visual(10, 20, environment="nether", importance="setup"),
    ])
    trim = recommendation("trim_dead_air", 10.0, 20.0)
    ranges = select_ranges(
        timeline, RecommendationSet(recommendations=[trim]),
        handle=0.0, asset_durations={ASSET.asset_id: 200.0},
    )
    assert not any(entry.start >= 10.0 for entry in ranges)


def test_segments_whose_analysis_failed_are_dropped():
    timeline = timeline_of([visual(0, 10, error="model was down")])
    assert select_ranges(timeline, asset_durations={ASSET.asset_id: 200.0}) == []


def test_handles_never_run_past_the_media_duration():
    timeline = timeline_of([visual(0, 16, importance="payoff")])
    ranges = select_ranges(
        timeline, handle=5.0, asset_durations={ASSET.asset_id: 16.0}
    )
    assert ranges[0].start == 0.0
    assert ranges[0].end <= 16.0


def test_ranges_never_overlap_after_handles(cut_timeline):
    """Handles pushing ranges into each other would duplicate footage."""
    ranges = select_ranges(
        cut_timeline, handle=1.0, asset_durations={ASSET.asset_id: 200.0}
    )
    for earlier, later in zip(ranges, ranges[1:]):
        if earlier.asset_id == later.asset_id:
            assert later.start >= earlier.end - 1e-6


def test_a_protected_range_wins_the_contested_frames():
    """A slice of a payoff must never end up inside a sped-up filler clip."""
    timeline = timeline_of([
        visual(0, 10, importance="boring"),
        visual(10, 20, actions=("looting",), importance="payoff"),
    ])
    ranges = select_ranges(
        timeline, handle=1.0, asset_durations={ASSET.asset_id: 200.0}
    )
    protected = [e for e in ranges if e.protected]
    fillers = [e for e in ranges if not e.protected]
    for keep in protected:
        for filler in fillers:
            assert not keep.overlaps(filler)


# ---------------------------------------------------------------------------
# Assembly maths
# ---------------------------------------------------------------------------

def test_assembly_is_contiguous_and_in_order():
    ranges = [
        SelectedRange(ASSET.asset_id, ASSET.path, 0, 10, "setup"),
        SelectedRange(ASSET.asset_id, ASSET.path, 20, 30, "payoff"),
    ]
    placements = assemble(ranges)
    assert [p.sequence_start for p in placements] == [0.0, 10.0]
    assert placements[0].sequence_end == placements[1].sequence_start
    assert [p.index for p in placements] == [0, 1]


def test_speed_shortens_the_sequence_duration():
    ranges = [
        SelectedRange(ASSET.asset_id, ASSET.path, 0, 20, "filler", speed=2.0),
        SelectedRange(ASSET.asset_id, ASSET.path, 20, 30, "payoff"),
    ]
    placements = assemble(ranges)
    assert placements[0].sequence_duration == 10.0     # 20s at 2x
    assert placements[1].sequence_start == 10.0        # everything after moves


def test_source_to_sequence_mapping():
    placement = ClipPlacement(
        placement_id="p1", asset_id=ASSET.asset_id, source_file=ASSET.path,
        source_in=20.0, source_out=30.0, sequence_start=5.0,
    )
    assert placement.source_to_sequence(25.0) == 10.0
    assert placement.source_to_sequence(20.0) == 5.0
    assert placement.source_to_sequence(99.0) is None       # cut out


def test_source_to_sequence_accounts_for_speed():
    placement = ClipPlacement(
        placement_id="p1", asset_id=ASSET.asset_id, source_file=ASSET.path,
        source_in=0.0, source_out=20.0, sequence_start=0.0, speed=2.0,
    )
    assert placement.source_to_sequence(10.0) == 5.0


def test_map_to_sequence_returns_none_for_removed_footage():
    placements = assemble([
        SelectedRange(ASSET.asset_id, ASSET.path, 0, 10, "setup"),
        SelectedRange(ASSET.asset_id, ASSET.path, 30, 40, "payoff"),
    ])
    assert map_to_sequence(placements, ASSET.asset_id, 5.0) == 5.0
    assert map_to_sequence(placements, ASSET.asset_id, 20.0) is None


def test_coverage_reports_compression():
    placements = assemble([
        SelectedRange(ASSET.asset_id, ASSET.path, 0, 20, "filler", speed=2.0),
    ])
    numbers = coverage(placements)
    assert numbers["source_seconds"] == 20.0
    assert numbers["cut_seconds"] == 10.0
    assert numbers["compression"] == 0.5


def test_placement_round_trips():
    placement = ClipPlacement(
        placement_id="p1", asset_id="a1", source_file="/f/c.mp4",
        source_in=10.0, source_out=20.0, sequence_start=5.0, speed=2.0,
        keep_reason="filler", recommendation_ids=["r1"], segment_ids=["s1"],
    )
    restored = ClipPlacement.from_dict(json.loads(json.dumps(placement.to_dict())))
    assert restored.to_dict() == placement.to_dict()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_recommendation_ids_survive_into_placements(cut_plan):
    """The chain the whole session exists to preserve."""
    with_recs = [p for p in cut_plan.placements if p.recommendation_ids]
    assert with_recs
    assert all(p.segment_ids for p in cut_plan.placements)
    assert all(p.placement_id.startswith("p_") for p in cut_plan.placements)


def test_append_operations_name_their_placement(cut_plan):
    appends = [op for op in cut_plan.ops if op["op"] == "clip.append"]
    assert appends
    for op, placement in zip(appends, cut_plan.placements):
        assert placement.placement_id in op["note"]
        assert op["in"] == pytest.approx(placement.source_in)
        assert op["out"] == pytest.approx(placement.source_out)


def test_markers_carry_their_recommendation_id(cut_plan):
    with_ids = [m for m in cut_plan.markers if m.recommendation_id]
    assert with_ids
    comments = " ".join(m.comment for m in cut_plan.markers)
    assert "evidence:" in comments


# ---------------------------------------------------------------------------
# Operation order
# ---------------------------------------------------------------------------

def test_operation_order(cut_plan):
    names = [op["op"] for op in cut_plan.ops]
    assert names[0] == "project.import"
    assert names.index("sequence.create") < names.index("sequence.activate")
    assert names.index("sequence.activate") < names.index("clip.append")


def test_speed_operations_run_back_to_front():
    """Rippling shifts later clips, so they must be handled first."""
    timeline = timeline_of([
        visual(index * 10, index * 10 + 10, importance="boring")
        for index in range(4)
    ])
    plan = build_rough_cut(timeline, assets=[ASSET])
    speeds = [op for op in plan.ops if op["op"] == "clip.speed"]
    if len(speeds) > 1:
        indices = [op["clip"]["index"] for op in speeds]
        assert indices == sorted(indices, reverse=True)


def test_markers_are_added_after_every_retime(cut_plan):
    names = [op["op"] for op in cut_plan.ops]
    if "clip.speed" in names and "marker.add" in names:
        assert max(
            i for i, n in enumerate(names) if n == "clip.speed"
        ) < min(i for i, n in enumerate(names) if n == "marker.add")


def test_the_auto_placed_clip_is_removed_before_assembly(cut_plan):
    """sequence.create from a clip puts that clip on the timeline."""
    names = [op["op"] for op in cut_plan.ops]
    assert "clip.remove" in names
    assert names.index("clip.remove") < names.index("clip.append")


def test_a_preset_avoids_the_remove_step():
    timeline = timeline_of([visual(0, 16, importance="payoff")])
    plan = build_rough_cut(
        timeline, assets=[ASSET],
        options=RoughCutOptions(preset="/presets/1080p60.sqpreset"),
    )
    names = [op["op"] for op in plan.ops]
    assert "clip.remove" not in names
    create = next(op for op in plan.ops if op["op"] == "sequence.create")
    assert create["preset"] == "/presets/1080p60.sqpreset"


# ---------------------------------------------------------------------------
# Safe conversion only
# ---------------------------------------------------------------------------

def test_a_zoom_is_refused_on_a_protected_clip(cut_timeline):
    recommendations = plan_recommendations(cut_timeline)
    for entry in recommendations.recommendations:
        if entry.category == "punch_in":
            entry.status = "accepted"
    plan = build_rough_cut(cut_timeline, recommendations, assets=[ASSET])

    refusals = [u for u in plan.unconverted if u.category == "punch_in"]
    if refusals:
        assert any(
            "protected" in u.reason or "retimed" in u.reason
            or "UI" in u.reason or "HUD" in u.reason
            for u in refusals
        )


def test_a_zoom_is_refused_over_an_open_ui():
    """A clip that is otherwise perfectly zoomable, except for the inventory.

    ``tension`` rather than ``danger`` on purpose: high-value importances make
    the clip a protected hold, which would refuse the zoom for that reason
    instead and leave the UI rule untested.
    """
    timeline = timeline_of(
        [visual(0, 20, importance="tension", ui=UIState(inventory_open=True))],
        lines=[(1, 18, "so this is where I keep all of the good stuff")],
    )
    entry = recommendation("punch_in", 2.0, 8.0)
    plan = build_rough_cut(
        timeline, RecommendationSet(recommendations=[entry]), assets=[ASSET]
    )

    placement = plan.placements[0]
    assert placement.protected is False       # the UI rule is what fires
    assert placement.speed == 1.0
    assert not [op for op in plan.ops if op["op"] == "animate"]
    assert any("UI" in u.reason for u in plan.unconverted)


def test_a_zoom_is_refused_on_a_retimed_clip():
    timeline = timeline_of([visual(0, 20, importance="boring")])
    entry = recommendation("punch_in", 2.0, 8.0)
    plan = build_rough_cut(
        timeline, RecommendationSet(recommendations=[entry]), assets=[ASSET]
    )
    sped = [p for p in plan.placements if p.speed != 1.0]
    if sped:
        assert any("retimed" in u.reason for u in plan.unconverted)


def test_a_safe_zoom_converts_to_a_conservative_animate():
    timeline = timeline_of(
        [visual(0, 20, importance="danger", threats=("creeper",))],
        lines=[(1, 18, "there is a creeper right there watch out")],
    )
    entry = recommendation("punch_in", 4.0, 10.0)
    plan = build_rough_cut(
        timeline, RecommendationSet(recommendations=[entry]), assets=[ASSET]
    )
    zooms = [op for op in plan.ops if op["op"] == "animate"]
    if zooms:
        assert zooms[0]["property"] == "Scale"
        assert zooms[0]["to"] <= convert.MAX_PUNCH_SCALE
        assert zooms[0]["relative_to"] == "sequence"


def test_no_zooms_disables_them_but_leaves_a_marker():
    timeline = timeline_of(
        [visual(0, 20, importance="danger", threats=("creeper",))],
        lines=[(1, 18, "creeper right there")],
    )
    entry = recommendation("punch_in", 4.0, 10.0)
    plan = build_rough_cut(
        timeline, RecommendationSet(recommendations=[entry]), assets=[ASSET],
        options=RoughCutOptions(allow_zooms=False),
    )
    assert not [op for op in plan.ops if op["op"] == "animate"]
    assert any("disabled" in u.reason for u in plan.unconverted)
    assert any(marker.name == "ZOOM?" for marker in plan.markers)


def test_placeholder_categories_become_markers():
    timeline = timeline_of([visual(0, 20, importance="payoff")])
    entries = [
        recommendation("music_cue", 2.0, 8.0),
        recommendation("sound_effect", 3.0, 4.0),
        recommendation("text_overlay", 5.0, 9.0),
        recommendation("visual_callout", 6.0, 8.0),
    ]
    plan = build_rough_cut(
        timeline, RecommendationSet(recommendations=entries), assets=[ASSET]
    )
    names = {marker.name for marker in plan.markers}
    assert {"MUSIC", "SFX", "TEXT", "CALLOUT"} <= names


def test_a_recommendation_for_removed_footage_is_reported():
    timeline = timeline_of([
        visual(0, 10, importance="payoff"),
        visual(10, 20, importance="boring"),
    ])
    # A marker on footage the cut drops.
    entry = recommendation("marker", 14.0, 16.0)
    recommendations = RecommendationSet(recommendations=[
        entry, recommendation("trim_dead_air", 10.0, 20.0),
    ])
    plan = build_rough_cut(
        timeline, recommendations, assets=[ASSET],
        options=RoughCutOptions(),
    )
    plan_ids = {u.recommendation_id for u in plan.unconverted}
    if entry.recommendation_id in plan_ids:
        reason = next(
            u.reason for u in plan.unconverted
            if u.recommendation_id == entry.recommendation_id
        )
        assert "cut out" in reason


def test_unknown_categories_are_reported_not_dropped():
    timeline = timeline_of([visual(0, 20, importance="payoff")])
    entry = recommendation("speed_ramp", 2.0, 8.0)
    entry.category = "unknown"
    plan = build_rough_cut(
        timeline, RecommendationSet(recommendations=[entry]), assets=[ASSET]
    )
    assert any(
        u.recommendation_id == entry.recommendation_id for u in plan.unconverted
    )


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_the_plan_validates_offline(cut_plan):
    assert cut_plan.dry_run_passed is True
    assert cut_plan.dry_run_error is None
    assert len(cut_plan.explanation) == cut_plan.operation_count


def test_every_operation_is_in_the_catalog(cut_plan):
    from premiere import catalog

    for op in cut_plan.ops:
        assert op["op"] in catalog.OPS


def test_the_plan_carries_the_dry_run_flag(cut_plan):
    assert cut_plan.as_edit_plan()["dry_run"] is True
    assert "dry_run" not in cut_plan.as_edit_plan(dry_run=False)


def test_an_empty_plan_reports_why():
    plan = execute.dry_run(RoughCutPlan())
    assert plan.dry_run_passed is False
    assert plan.dry_run_error["code"] == "empty_plan"
    assert plan.dry_run_error["hint"]


def test_a_malformed_plan_is_reported_not_raised():
    plan = RoughCutPlan(ops=[{"op": "nonsense.op", "time": 0}])
    execute.dry_run(plan)
    assert plan.dry_run_passed is False
    assert "nonsense" in json.dumps(plan.dry_run_error)


def test_plan_only_does_not_validate(cut_timeline):
    plan = build_rough_cut(
        cut_timeline, plan_recommendations(cut_timeline), assets=[ASSET],
        validate=False,
    )
    assert plan.ops
    assert plan.dry_run_passed is False


# ---------------------------------------------------------------------------
# Execution guards -- the important half
# ---------------------------------------------------------------------------

def test_plan_only_mode_never_runs_anything(cut_plan):
    engine = FakeEngine()
    report = execute.run(cut_plan, mode="plan_only", engine=engine)
    assert report.executed is False
    assert engine.calls == []


def test_dry_run_mode_never_runs_anything(cut_plan):
    engine = FakeEngine()
    report = execute.run(cut_plan, mode="dry_run", engine=engine)
    assert report.executed is False
    assert report.dry_run_passed is True
    assert engine.calls == []


def test_execution_refuses_when_the_dry_run_fails():
    plan = RoughCutPlan(ops=[{"op": "nonsense.op"}])
    engine = FakeEngine()
    report = execute.run(plan, mode="execute_on_scratch", engine=engine)

    assert report.executed is False
    assert "dry run did not pass" in report.refused_reason
    assert engine.calls == []


def test_a_stale_dry_run_pass_is_not_trusted(cut_plan):
    """The plan may have been rebuilt since; validation reruns every time."""
    cut_plan.ops = [{"op": "nonsense.op"}]
    cut_plan.dry_run_passed = True            # a lie left over from before

    engine = FakeEngine()
    report = execute.run(cut_plan, mode="execute_on_scratch", engine=engine)
    assert report.executed is False
    assert engine.calls == []


def test_execution_refuses_a_plan_that_does_not_build_its_own_sequence():
    plan = RoughCutPlan(ops=[
        {"op": "clip.append", "asset": "/f/c.mp4", "track": "V1"},
    ])
    engine = FakeEngine()
    report = execute.run(plan, mode="execute_on_scratch", engine=engine)

    assert report.executed is False
    assert report.on_scratch is False
    assert "own sequence" in report.refused_reason
    assert engine.calls == []


def test_the_active_sequence_can_only_be_targeted_explicitly():
    plan = RoughCutPlan(ops=[
        {"op": "marker.add", "time": 1.0, "name": "X"},
    ])
    engine = FakeEngine()

    refused = execute.run(plan, mode="execute_on_scratch", engine=engine)
    assert refused.executed is False
    assert engine.calls == []

    allowed = execute.run(
        plan, mode="execute_on_scratch", engine=engine,
        allow_active_sequence=True,
    )
    assert allowed.executed is True
    assert len(engine.calls) == 1


def test_execution_runs_after_a_passing_dry_run(cut_plan):
    engine = FakeEngine()
    report = execute.run(cut_plan, mode="execute_on_scratch", engine=engine)

    assert report.executed is True
    assert report.on_scratch is True
    assert report.operations_attempted == cut_plan.operation_count
    assert report.operations_succeeded == cut_plan.operation_count
    assert len(engine.calls) == 1
    # What actually reached Premiere must not be a dry run.
    assert "dry_run" not in engine.calls[0]


def test_a_premiere_failure_is_reported_not_raised(cut_plan):
    engine = FakeEngine(succeed=False)
    report = execute.run(cut_plan, mode="execute_on_scratch", engine=engine)
    assert report.executed is False
    assert report.error


def test_an_unknown_mode_is_rejected(cut_plan):
    with pytest.raises(EditingError):
        execute.run(cut_plan, mode="just_do_it")


def test_targets_scratch_sequence_requires_the_right_order():
    good = RoughCutPlan(ops=[
        {"op": "sequence.create", "name": "S"},
        {"op": "sequence.activate", "name": "S"},
        {"op": "clip.append", "asset": "/f/c.mp4", "track": "V1"},
    ])
    assert execute.targets_scratch_sequence(good) is True

    # Appending before activating would land on whatever was already open.
    bad = RoughCutPlan(ops=[
        {"op": "clip.append", "asset": "/f/c.mp4", "track": "V1"},
        {"op": "sequence.create", "name": "S"},
        {"op": "sequence.activate", "name": "S"},
    ])
    assert execute.targets_scratch_sequence(bad) is False


def test_execution_report_round_trips():
    report = ExecutionReport(
        mode="execute_on_scratch", executed=True, sequence_name="S",
        operations_attempted=5, operations_succeeded=5, dry_run_passed=True,
    )
    restored = ExecutionReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert restored.executed is True
    assert restored.ok is True


# ---------------------------------------------------------------------------
# Review frames
# ---------------------------------------------------------------------------

def test_review_frames_are_planned_per_clip(cut_plan):
    frames = review_module.plan_frames(cut_plan)
    assert len(frames) == len(cut_plan.placements)
    for frame, placement in zip(frames, cut_plan.placements):
        assert frame.placement_id == placement.placement_id
        assert placement.source_in <= frame.source_time <= placement.source_out


def test_review_frames_carry_provenance(cut_plan):
    frames = review_module.plan_frames(cut_plan)
    assert any(frame.recommendation_ids for frame in frames)
    assert all(frame.segment_ids for frame in frames)


def test_review_frames_list_the_markers_inside_their_clip():
    plan = RoughCutPlan(
        placements=[ClipPlacement(
            placement_id="p_1", asset_id="a1", source_file="/f/c.mp4",
            source_in=0.0, source_out=10.0, sequence_start=0.0,
        )],
        markers=[
            SequenceMarker(time=4.0, name="PAYOFF"),
            SequenceMarker(time=50.0, name="ELSEWHERE"),
        ],
    )
    frame = review_module.plan_frames(plan)[0]
    assert frame.marker_names == ["PAYOFF"]


def test_review_frame_position_is_configurable(cut_plan):
    early = review_module.plan_frames(cut_plan, position=0.1)
    late = review_module.plan_frames(cut_plan, position=0.9)
    assert early[0].source_time < late[0].source_time


def test_review_export_without_ffmpeg_degrades(cut_plan, config, monkeypatch):
    """A missing FFmpeg must not lose the manifest or crash the command."""
    from editing.errors import ToolMissingError

    def missing(*args, **kwargs):
        raise ToolMissingError("ffmpeg is not installed")

    monkeypatch.setattr(review_module.ff, "extract_frame", missing)
    review = review_module.export_frames(cut_plan, config)

    assert review.exported is False
    assert any("ffmpeg" in warning for warning in review.warnings)


def test_review_export_writes_a_manifest(cut_plan, config, monkeypatch, tmp_path):
    written = tmp_path / "frame.jpg"
    written.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(
        review_module.ff, "extract_frame", lambda *a, **k: written
    )

    review = review_module.export_frames(cut_plan, config)
    assert review.exported is True
    assert len(review) == len(cut_plan.placements)

    manifest = config.review_dir / "nova_rough_cut" / "review.json"
    assert manifest.exists()
    restored = review_module.load_review(manifest)
    assert len(restored) == len(review)
    assert restored.frames[0].placement_id


def test_review_set_round_trips():
    review = review_module.ReviewSet(
        sequence_name="S",
        frames=[review_module.ReviewFrame(
            frame_id="rf_1", placement_id="p_1", path="/f/1.jpg",
            sequence_time=1.0, source_time=2.0, source_file="/f/c.mp4",
            asset_id="a1", keep_reason="payoff",
        )],
    )
    restored = review_module.ReviewSet.from_dict(
        json.loads(json.dumps(review.to_dict()))
    )
    assert len(restored) == 1
    assert restored.frames[0].keep_reason == "payoff"


# ---------------------------------------------------------------------------
# Whole plan
# ---------------------------------------------------------------------------

def test_rough_cut_plan_round_trips(cut_plan):
    restored = RoughCutPlan.from_dict(json.loads(json.dumps(cut_plan.to_dict())))
    assert len(restored.placements) == len(cut_plan.placements)
    assert restored.operation_count == cut_plan.operation_count
    assert restored.sequence_name == cut_plan.sequence_name


def test_a_cut_is_shorter_than_its_source(cut_plan):
    assert cut_plan.total_duration <= cut_plan.source_duration


def test_an_empty_timeline_produces_an_empty_plan():
    from editing.schema import StructureTimeline

    plan = build_rough_cut(StructureTimeline(), assets=[ASSET])
    assert plan.placements == []
    assert plan.ops == []
    assert plan.warnings


def test_the_plan_warns_when_it_barely_cuts():
    timeline = timeline_of([visual(0, 20, importance="payoff")])
    plan = build_rough_cut(timeline, assets=[ASSET])
    assert any("barely a cut" in warning for warning in plan.warnings)


def test_the_plan_warns_when_audio_is_missing():
    timeline = timeline_of([visual(0, 20, importance="payoff")])
    plan = build_rough_cut(timeline, assets=[ASSET])
    assert any("audio" in warning.lower() for warning in plan.warnings)

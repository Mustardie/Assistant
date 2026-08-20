"""The critic pass: review frames, findings, revisions, and the guards.

Two things carry most of the weight here.

The first is **the line between a finding and a fix**. A critic that can be
talked into an edit by a confident sentence is worse than no critic, so the
tests below assert on the refusals as much as the conversions: a zoom
complaint about a moment with no zoom, a trim with no audio behind it, a
finding the model was 40% sure of -- each has to come out the other side as a
recommendation with a reason, never as an operation.

The second is **the execution guards**, which are the rough cut's guards plus
one: a revision plan edits a sequence it did not create, so "did you build your
own sandbox" is unanswerable and is replaced by an operation allowlist and a
first-op activate check. Every guard is asserted against a fake engine that
records whether it was called at all.

Nothing here needs FFmpeg, a GPU, a model server or Premiere.
"""
from __future__ import annotations

import json

import pytest

from editing.align import build_timeline
from editing.critic import (
    execute as critic_execute, plan as plan_module, report as critic_report,
    revise as revise_module,
)
from editing.critic.critic import MockCritic, VisualCritic, parse_response
from editing.critic.frames import CoverageOptions, plan_coverage_frames
from editing.critic.revise import RevisionOptions, build_revisions
from editing.critic.schema import (
    FIXES, ISSUE_TYPES, SAFE_FIXES, CriticFinding, CriticReport, RevisionPlan,
    RevisionRecommendation, RevisionSet, coerce_fix, coerce_issue,
    coerce_severity,
)
from editing.errors import EditingError, ModelError
from editing.recommend.schema import (
    EditRecommendation, Evidence, RecommendationSet,
)
from editing.roughcut import review as review_module
from editing.roughcut.schema import (
    ClipPlacement, RoughCutPlan, SequenceMarker,
)
from editing.schema import (
    AudioEvent, MediaAsset, TimeRange, Transcript, TranscriptEntry, UIState,
    VisualEvent,
)

ASSET = MediaAsset(
    asset_id="a_test", path="/footage/ep12.mp4", filename="ep12.mp4",
    duration=200.0,
)
SEQUENCE = "Nova Rough Cut"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def placement(
    pid, *, source_in, source_out, sequence_start, index=0, speed=1.0,
    keep_reason="payoff", protected=False, recs=(), segments=("s_1",),
):
    return ClipPlacement(
        placement_id=pid, asset_id=ASSET.asset_id, source_file=ASSET.path,
        source_in=source_in, source_out=source_out,
        sequence_start=sequence_start, index=index, speed=speed,
        keep_reason=keep_reason, protected=protected,
        recommendation_ids=list(recs), segment_ids=list(segments),
    )


def zoom_op(*, at, start, duration=0.8, to=115.0, rec_id="r_zoom"):
    """An ``animate`` on Motion > Scale, shaped exactly as Session 3 emits it."""
    return {
        "op": "animate",
        "clip": {"track": "V1", "at": at},
        "component": "Motion",
        "property": "Scale",
        "from": 100.0,
        "to": to,
        "start": start,
        "duration": duration,
        "easing": "ease_out",
        "relative_to": "sequence",
        "note": f"punch_in -> {to:g}% (a payoff) [{rec_id}]",
    }


@pytest.fixture
def cut() -> RoughCutPlan:
    """Three clips: a payoff with a zoom, retimed filler, a protected hold."""
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[
            placement("p_1", source_in=0.0, source_out=10.0,
                      sequence_start=0.0, index=0, keep_reason="payoff",
                      recs=["r_payoff"], segments=["s_1"]),
            placement("p_2", source_in=20.0, source_out=40.0,
                      sequence_start=10.0, index=1, speed=2.0,
                      keep_reason="filler", segments=["s_2"]),
            placement("p_3", source_in=60.0, source_out=70.0,
                      sequence_start=20.0, index=2, keep_reason="danger",
                      protected=True, recs=["r_hold"], segments=["s_3"]),
        ],
        markers=[
            SequenceMarker(time=7.0, name="TEXT", category="text_overlay",
                           comment="name the biome", recommendation_id="r_text"),
            SequenceMarker(time=12.0, name="NOTE", category="marker",
                           comment="check this", recommendation_id="r_note"),
        ],
        on_scratch=True,
    )
    plan.ops = [
        {"op": "project.import", "paths": [ASSET.path], "bin": "b"},
        {"op": "sequence.create", "name": SEQUENCE, "from_asset": ASSET.path},
        {"op": "sequence.activate", "name": SEQUENCE},
        {"op": "clip.append", "asset": ASSET.path, "track": "V1",
         "in": 0.0, "out": 10.0},
        zoom_op(at=5.0, start=4.0),
    ]
    return plan


@pytest.fixture
def cut_timeline():
    """A timeline covering the same source ranges the cut draws on."""
    def visual(start, end, **kw):
        ui = kw.pop("ui", None)
        event = VisualEvent(
            event_id=f"e_{start}", source_file=ASSET.path,
            asset_id=ASSET.asset_id, start=start, end=end, confidence=0.85,
            environment=kw.pop("environment", "cave"),
            actions=list(kw.pop("actions", ("mining",))),
            entities=list(kw.pop("entities", ())),
            threats=list(kw.pop("threats", ())),
            importance=kw.pop("importance", "setup"),
            suggested_range=TimeRange(start, end), model="Qwen3-VL-8B-Instruct",
        )
        if ui is not None:
            event.ui = ui
        return event

    return build_timeline(
        [ASSET],
        {ASSET.asset_id: [
            visual(0, 10, importance="payoff", actions=("looting",),
                   entities=("diamond",), ui=UIState(low_health=True)),
            visual(20, 40, importance="boring", actions=("travelling",),
                   environment="forest"),
            visual(60, 70, importance="danger", actions=("fighting",),
                   threats=("creeper",)),
        ]},
        {ASSET.asset_id: Transcript(
            asset_id=ASSET.asset_id, source="srt",
            entries=[TranscriptEntry(2.0, 6.0, "oh my god diamonds")],
        )},
        audio_by_asset={ASSET.asset_id: [
            AudioEvent(
                event_id="au_silence", source_file=ASSET.path,
                asset_id=ASSET.asset_id, start=8.0, end=12.0, type="silence",
                confidence=0.9, detection="measured", loudness_db=-50.0,
                baseline_db=-24.0,
            ),
        ]},
    )


@pytest.fixture
def recommendations() -> RecommendationSet:
    return RecommendationSet(recommendations=[
        EditRecommendation(
            recommendation_id="r_payoff", asset_id=ASSET.asset_id,
            source_file=ASSET.path, start=0.0, end=10.0, category="punch_in",
            priority=0.9, evidence=Evidence(visual_event_ids=["e_0"]),
        ),
    ])


@pytest.fixture
def frames(cut, cut_timeline, recommendations):
    return plan_coverage_frames(
        cut, timeline=cut_timeline, recommendations=recommendations
    )


def as_review(frames, *, path="/frames/f.jpg") -> review_module.ReviewSet:
    """A review set whose frames all claim to have been exported."""
    for frame in frames:
        frame.path = path
    return review_module.ReviewSet(
        sequence_name=SEQUENCE, frames=list(frames), exported=True,
    )


def frame_of(frames, kind):
    return next(frame for frame in frames if frame.frame_kind == kind)


def finding(
    frame, issue, *, confidence=0.8, severity="medium", fix=None, evidence="saw it",
):
    return CriticFinding(
        finding_id=f"cf_{issue}_{frame.frame_id}",
        frame_id=frame.frame_id,
        issue=issue,
        severity=severity,
        confidence=confidence,
        evidence=evidence,
        suggested_fix=fix or "review_marker",
        sequence_time=frame.sequence_time,
        placement_id=frame.placement_id,
    )


def critique_of(*findings, mock=False) -> CriticReport:
    return CriticReport(
        sequence_name=SEQUENCE, findings=list(findings), model="test-critic",
        frames_examined=len(findings), mock=mock,
    )


def revise(critique, frames, cut, **kw):
    kw.setdefault("asset_durations", {ASSET.asset_id: ASSET.duration})
    return build_revisions(critique, as_review(frames), cut, **kw)


class FakeEngine:
    """Records whether it was ever asked to run anything."""

    def __init__(self, *, succeed=True):
        self.calls: list[dict] = []
        self.succeed = succeed

    def run(self, plan):
        self.calls.append(plan)
        if not self.succeed:
            return {"success": False, "error": "Premiere said no",
                    "code": "execution_failed"}
        return {"success": True,
                "results": [{"ok": True} for _ in plan.get("ops", [])]}


# ---------------------------------------------------------------------------
# Part 1 -- review frame coverage and metadata
# ---------------------------------------------------------------------------

def test_coverage_samples_both_ends_of_a_clip(cut, frames):
    kinds = {frame.frame_kind for frame in frames}
    assert "clip_start" in kinds
    assert "clip_end" in kinds


def test_both_sides_of_a_cut_are_sampled(cut, frames):
    """The two edge probes are always closer than the dedupe gap.

    They are also in different source files, so collapsing them would silently
    drop every incoming-cut frame in the review -- the exact frames that answer
    "did the next shot land".
    """
    starts = {f.placement_id for f in frames if f.frame_kind == "clip_start"}
    ends = {f.placement_id for f in frames if f.frame_kind == "clip_end"}
    assert starts == ends == {p.placement_id for p in cut.placements}

    ordered = sorted(frames, key=lambda f: f.sequence_time)
    across = [
        (a, b) for a, b in zip(ordered, ordered[1:])
        if a.frame_kind == "clip_end" and b.frame_kind == "clip_start"
    ]
    assert across
    for outgoing, incoming in across:
        assert incoming.sequence_time - outgoing.sequence_time < 0.75
        assert outgoing.placement_id != incoming.placement_id


def test_two_probes_at_the_same_moment_in_one_clip_collapse(cut, cut_timeline):
    """Within one shot, the more specific label wins and the other goes."""
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", source_in=0.0, source_out=10.0,
                              sequence_start=0.0, keep_reason="payoff")],
        markers=[SequenceMarker(time=5.0, name="NOTE", category="marker")],
        ops=[zoom_op(at=5.0, start=4.4, duration=0.6)],
    )
    frames = plan_coverage_frames(plan, timeline=cut_timeline)
    near_five = [f for f in frames if 4.5 <= f.sequence_time <= 5.5]

    assert len(near_five) == 1
    assert near_five[0].frame_kind == "zoom"


def test_every_frame_links_to_a_real_placement(cut, frames):
    ids = {p.placement_id for p in cut.placements}
    assert frames
    for frame in frames:
        assert frame.placement_id in ids
        placement = next(
            p for p in cut.placements if p.placement_id == frame.placement_id
        )
        assert placement.source_in <= frame.source_time <= placement.source_out
        assert (placement.sequence_start
                <= frame.sequence_time
                <= placement.sequence_end)
        assert frame.sequence_name == SEQUENCE
        assert frame.clip_duration == pytest.approx(
            placement.sequence_duration
        )


def test_source_time_is_the_inverse_of_the_sequence_position(cut, frames):
    """A retimed clip is the case that catches an inverted mapping."""
    retimed = [
        frame for frame in frames
        if frame.placement_id == "p_2" and frame.speed == 2.0
    ]
    assert retimed
    placement = next(p for p in cut.placements if p.placement_id == "p_2")
    for frame in retimed:
        back = placement.source_to_sequence(frame.source_time)
        assert back == pytest.approx(frame.sequence_time, abs=0.01)


def test_a_text_marker_becomes_a_text_placeholder_frame(frames):
    frame = frame_of(frames, "text_placeholder")
    assert frame.sequence_time == pytest.approx(7.0)
    assert frame.has_text


def test_a_plain_marker_becomes_a_marker_frame(frames):
    frame = frame_of(frames, "marker")
    assert frame.sequence_time == pytest.approx(12.0)


def test_a_zoom_is_sampled_where_it_is_strongest(cut, cut_timeline):
    """Mid-push shows a scale nobody complained about; the end is the question."""
    options = CoverageOptions(markers=False, text_placeholders=False,
                              cut_points=False, sanity=False,
                              high_priority=False, speed_changes=False)
    frames = plan_coverage_frames(cut, timeline=cut_timeline, options=options)
    zoom = frame_of(frames, "zoom")
    assert zoom.sequence_time == pytest.approx(4.8, abs=0.01)


def test_a_retimed_clip_is_flagged_as_a_speed_change(frames):
    frame = frame_of(frames, "speed_change")
    assert frame.speed == 2.0
    assert any(edit["kind"] == "speed" for edit in frame.applied_edits)


def test_long_stretches_get_sanity_probes_and_they_are_deterministic(cut_timeline):
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_long", source_in=0.0, source_out=40.0,
                              sequence_start=0.0, keep_reason="setup")],
    )
    first = plan_coverage_frames(plan, timeline=cut_timeline)
    second = plan_coverage_frames(plan, timeline=cut_timeline)

    probes = [f for f in first if f.frame_kind == "sanity"]
    assert probes
    assert [f.sequence_time for f in first] == [f.sequence_time for f in second]


def test_high_priority_moments_are_covered(cut, cut_timeline, recommendations):
    frames = plan_coverage_frames(
        cut, timeline=cut_timeline, recommendations=recommendations,
        options=CoverageOptions(cut_points=False, markers=False, zooms=False,
                                text_placeholders=False, sanity=False,
                                speed_changes=False),
    )
    kinds = {frame.frame_kind for frame in frames}
    # Every clip still gets one frame -- a clip nobody looked at is a worse
    # outcome than one extra frame -- but the strong moments are labelled.
    assert kinds == {"high_priority", "clip_sample"}
    high = {f.placement_id for f in frames if f.frame_kind == "high_priority"}
    assert high == {"p_1", "p_3"}


def test_frames_carry_the_transcript_audio_and_visual_context(cut, frames):
    payoff = next(
        frame for frame in frames
        if frame.placement_id == "p_1" and frame.sequence_time < 4.0
    )
    assert "diamonds" in payoff.transcript
    assert payoff.environment == "cave"
    assert "looting" in payoff.actions
    assert "diamond" in payoff.entities
    assert "low_health" in payoff.ui_flags
    assert payoff.visual_event_ids


def test_frames_carry_the_audio_events_around_them(cut, cut_timeline):
    """The silence at 8-12s of source must reach the frame that sits in it."""
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", source_in=0.0, source_out=10.0,
                              sequence_start=0.0)],
    )
    frames = plan_coverage_frames(plan, timeline=cut_timeline)
    tail = frame_of(frames, "clip_end")
    assert "silence" in tail.audio_types
    assert tail.audio_events[0]["type"] == "silence"


def test_applied_edits_name_the_recommendation_behind_a_zoom(cut, cut_timeline):
    frames = plan_coverage_frames(cut, timeline=cut_timeline)
    zoomed = next(frame for frame in frames if frame.has_zoom)
    edit = next(e for e in zoomed.applied_edits if e["kind"] == "zoom")
    assert edit["to"] == 115.0
    assert edit["recommendation_id"] == "r_zoom"


def test_priority_is_carried_from_the_recommendation(cut, frames):
    payoff = next(frame for frame in frames if frame.placement_id == "p_1")
    assert payoff.priority == pytest.approx(0.9)


def test_the_frame_cap_keeps_the_most_informative_kinds(cut, cut_timeline):
    frames = plan_coverage_frames(
        cut, timeline=cut_timeline, options=CoverageOptions(max_frames=2)
    )
    assert len(frames) == 2
    assert {frame.frame_kind for frame in frames} <= {
        "zoom", "text_placeholder", "marker"
    }


def test_frames_round_trip_through_the_manifest(cut, frames, config, monkeypatch):
    written = config.output_dir / "f.jpg"
    written.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(
        review_module.ff, "extract_frame", lambda *a, **k: written
    )

    review = review_module.export_frames(cut, config, frames=frames)
    assert len(review) == len(frames)

    restored = review_module.ReviewSet.from_dict(
        json.loads(json.dumps(review.to_dict()))
    )
    original = review.frames[0]
    back = restored.frame(original.frame_id)
    assert back is not None
    assert back.frame_kind == original.frame_kind
    assert back.applied_edits == original.applied_edits
    assert back.audio_types == original.audio_types
    assert back.clip_duration == pytest.approx(original.clip_duration)


def test_the_simple_export_is_unchanged_by_the_coverage_rules(cut, config,
                                                              monkeypatch):
    """Session 3's one-frame-per-clip export must still behave as documented."""
    written = config.output_dir / "f.jpg"
    written.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(
        review_module.ff, "extract_frame", lambda *a, **k: written
    )
    review = review_module.export_frames(cut, config)
    assert len(review) == len(cut.placements)
    assert {frame.frame_kind for frame in review.frames} == {"clip_sample"}


# ---------------------------------------------------------------------------
# Part 2 -- critic response parsing and coercion
# ---------------------------------------------------------------------------

def test_a_clean_frame_produces_no_findings(frames):
    frame = frames[0]
    assert parse_response({"looks_ok": True, "issues": []}, frame) == []


def test_a_normal_answer_is_parsed(frames):
    frame = frames[0]
    findings = parse_response({
        "looks_ok": False,
        "issues": [{
            "issue": "hud_hidden", "severity": "high", "confidence": 0.9,
            "evidence": "the hearts are cropped off the bottom",
            "suggested_fix": "remove_zoom",
        }],
    }, frame)

    assert len(findings) == 1
    assert findings[0].issue == "hud_hidden"
    assert findings[0].severity == "high"
    assert findings[0].confidence == pytest.approx(0.9)
    assert findings[0].suggested_fix == "remove_zoom"
    assert findings[0].frame_id == frame.frame_id
    assert findings[0].sequence_time == pytest.approx(frame.sequence_time)
    assert findings[0].placement_id == frame.placement_id


@pytest.mark.parametrize("payload", [
    {"findings": [{"issue": "too_dark"}]},
    {"problems": [{"issue": "too_dark"}]},
    {"issues": {"issue": "too_dark"}},
    {"issues": ["too_dark"]},
    {"issues": "too_dark"},
    {"issue": "too_dark"},
    {"type": "too_dark"},
])
def test_the_shapes_small_models_actually_return_all_parse(payload, frames):
    findings = parse_response(payload, frames[0])
    assert [f.issue for f in findings] == ["too_dark"]


def test_an_unknown_issue_becomes_needs_human_review_but_keeps_its_words(frames):
    findings = parse_response(
        {"issues": [{"issue": "the vibes are off", "confidence": 0.8}]},
        frames[0],
    )
    assert findings[0].issue == "needs_human_review"
    assert findings[0].raw_issue == "the vibes are off"


@pytest.mark.parametrize("said,expected", [
    ("too zoomed in", "zoom_too_strong"),
    ("HUD obscured", "hud_hidden"),
    ("underexposed", "too_dark"),
    ("blown out", "too_bright"),
    ("cut early", "cut_too_early"),
    ("needs callout", "callout_needed"),
])
def test_common_phrasings_coerce_onto_the_vocabulary(said, expected):
    assert coerce_issue(said) == expected


def test_not_ok_with_no_named_issue_is_not_read_as_fine(frames):
    """Discarding this would silently lose a real signal."""
    findings = parse_response(
        {"looks_ok": False, "notes": "something is wrong with the framing"},
        frames[0],
    )
    assert len(findings) == 1
    assert findings[0].issue == "needs_human_review"
    assert "framing" in findings[0].evidence


def test_a_non_object_answer_is_a_finding_not_a_crash(frames):
    findings = parse_response(["nope"], frames[0])
    assert findings[0].issue == "needs_human_review"


def test_confidence_and_severity_are_coerced_into_range(frames):
    findings = parse_response({
        "issues": [{"issue": "too_dark", "confidence": 5.0, "severity": "?"}]
    }, frames[0])
    assert findings[0].confidence == pytest.approx(1.0)
    assert findings[0].severity == "medium"     # the default for too_dark


def test_a_missing_severity_uses_the_issues_own_default():
    assert coerce_severity(None, issue="hud_hidden") == "high"
    assert coerce_severity(None, issue="callout_needed") == "low"


def test_a_missing_fix_uses_the_issues_own_default():
    assert coerce_fix(None, issue="zoom_too_strong") == "reduce_zoom"
    assert coerce_fix("", issue="too_bright") == "color_marker"
    assert coerce_fix("nonsense", issue="marker_mismatch") == "review_marker"


def test_every_default_fix_is_a_known_fix():
    from editing.critic.schema import DEFAULT_FIX, DEFAULT_SEVERITY

    assert set(DEFAULT_FIX) == set(ISSUE_TYPES)
    assert set(DEFAULT_SEVERITY) == set(ISSUE_TYPES)
    assert set(DEFAULT_FIX.values()) <= set(FIXES)


def test_every_safe_fix_has_a_handler():
    assert SAFE_FIXES == set(revise_module._HANDLERS)


# ---------------------------------------------------------------------------
# The critic runner
# ---------------------------------------------------------------------------

def test_the_mock_critic_marks_everything_it_says(config, frames):
    critic = VisualCritic(config, model=MockCritic())
    report = critic.critique(as_review(frames))

    assert report.mock is True
    assert all(finding.mock for finding in report.findings)
    assert any("mock" in warning.lower() for warning in report.warnings)


def test_the_critic_reaches_the_model_with_the_frames_context(config, frames):
    model = MockCritic(responses=[{"looks_ok": True, "issues": []}])
    critic = VisualCritic(config, model=model)
    critic.critique(as_review(frames[:1]))

    assert model.calls
    prompt = model.calls[0]["user"]
    assert "Position in the cut" in prompt
    assert "Why this frame was picked" in prompt


def test_a_failed_frame_costs_that_frame_and_not_the_pass(config, frames):
    model = MockCritic(responses=[
        ModelError("the server is down"),
        {"issues": [{"issue": "too_dark", "confidence": 0.9}]},
    ])
    critic = VisualCritic(config, model=model)
    report = critic.critique(as_review(frames[:2]))

    assert report.frames_failed == 1
    assert len(report.findings) == 1
    assert any("server is down" in warning for warning in report.warnings)


def test_frames_with_no_exported_image_are_skipped_loudly(config, frames):
    review = as_review(frames)
    review.frames[0].path = ""
    report = VisualCritic(config, model=MockCritic()).critique(review)

    assert report.frames_examined == len(frames) - 1
    assert any("no exported image" in warning for warning in report.warnings)


def test_a_second_pass_over_the_same_frames_hits_the_cache(config, cache, frames,
                                                           tmp_path):
    image = tmp_path / "f.jpg"
    image.write_bytes(b"\xff\xd8jpeg")
    review = as_review(frames[:2], path=str(image))

    critic = VisualCritic(config, model=MockCritic(), cache=cache)
    first = critic.critique(review)
    second = critic.critique(review)

    assert first.cache_misses == 2
    assert second.cache_hits == 2
    assert [f.issue for f in second.findings] == [f.issue for f in first.findings]


def test_a_changed_context_misses_the_cache(config, cache, frames, tmp_path):
    """The same picture judged against a different edit is a different question."""
    image = tmp_path / "f.jpg"
    image.write_bytes(b"\xff\xd8jpeg")
    review = as_review(frames[:1], path=str(image))
    critic = VisualCritic(config, model=MockCritic(), cache=cache)

    critic.critique(review)
    review.frames[0].applied_edits = [
        {"kind": "zoom", "to": 130.0, "detail": "a zoom to 130% ends here"}
    ]
    second = critic.critique(review)

    assert second.cache_hits == 0


def test_a_mock_critic_and_a_real_one_never_share_cache_entries(config, cache,
                                                                frames, tmp_path):
    image = tmp_path / "f.jpg"
    image.write_bytes(b"\xff\xd8jpeg")
    review = as_review(frames[:1], path=str(image))

    VisualCritic(config, model=MockCritic(), cache=cache).critique(review)

    other = MockCritic()
    other.name = "Qwen3-VL-8B-Instruct"
    assert VisualCritic(config, model=other, cache=cache).critique(
        review
    ).cache_hits == 0


def test_the_mock_critic_finds_the_zoom_it_was_told_about(config, cut,
                                                          cut_timeline):
    frames = plan_coverage_frames(cut, timeline=cut_timeline)
    zoomed = [frame for frame in frames if frame.has_zoom]
    report = VisualCritic(config, model=MockCritic()).critique(as_review(zoomed))

    assert {f.issue for f in report.findings} & {"zoom_too_strong", "hud_hidden"}


# ---------------------------------------------------------------------------
# Part 3 -- the revision schema
# ---------------------------------------------------------------------------

def test_a_revision_round_trips_through_json():
    revision = RevisionRecommendation(
        revision_id="rv_1", source_recommendation_id="r_zoom",
        finding_id="cf_1", frame_id="rf_1", placement_id="p_1",
        start=4.0, end=5.0, issue="zoom_too_strong", severity="medium",
        confidence=0.72, visual_evidence="the hotbar is cut off",
        transcript_evidence="oh my god", audio_evidence=["sudden_reaction"],
        suggested_fix="reduce_zoom", risks=["removes_an_edit"],
        status="accepted", premiere_ops=[{"op": "property.reset"}],
    )
    restored = RevisionRecommendation.from_dict(
        json.loads(json.dumps(revision.to_dict()))
    )
    assert restored == revision


def test_the_revision_schema_coerces_nonsense():
    restored = RevisionRecommendation.from_dict({
        "revision_id": "", "frame_id": "rf_1", "issue": "everything is fine?",
        "severity": "catastrophic", "confidence": "very", "status": "applied",
        "suggested_fix": "just fix it", "risks": ["invented_risk"],
        "start": 9.0, "end": 2.0,
    })
    assert restored.revision_id.startswith("rv_")
    assert restored.issue == "needs_human_review"
    assert restored.severity == "low"          # default for needs_human_review
    assert restored.confidence == pytest.approx(0.5)
    assert restored.status == "needs_human_review"
    assert restored.suggested_fix == "review_marker"
    assert restored.risks == []
    assert restored.end >= restored.start


def test_a_revision_is_only_actionable_when_accepted_and_armed():
    revision = RevisionRecommendation(revision_id="rv_1")
    assert not revision.is_actionable

    revision.accept("because", [{"op": "marker.add"}])
    assert revision.is_actionable

    revision.defer("on second thoughts")
    assert not revision.is_actionable
    assert revision.premiere_ops == []
    assert revision.status == "needs_human_review"


def test_a_revision_set_round_trips_and_ranks_worst_first():
    revisions = RevisionSet(sequence_name=SEQUENCE, revisions=[
        RevisionRecommendation(revision_id="rv_low", severity="low",
                               confidence=0.9, start=1.0),
        RevisionRecommendation(revision_id="rv_high", severity="high",
                               confidence=0.5, start=9.0),
        RevisionRecommendation(revision_id="rv_med", severity="medium",
                               confidence=0.7, start=5.0),
    ])
    assert [r.revision_id for r in revisions.ranked()] == [
        "rv_high", "rv_med", "rv_low"
    ]
    restored = RevisionSet.from_dict(
        json.loads(json.dumps(revisions.to_dict()))
    )
    assert len(restored) == 3
    assert restored.sequence_name == SEQUENCE


# ---------------------------------------------------------------------------
# Part 4 -- unsafe findings stay recommendations
# ---------------------------------------------------------------------------

def test_a_low_confidence_finding_is_never_acted_on(cut, frames):
    frame = next(f for f in frames if f.has_zoom)
    revisions = revise(
        critique_of(finding(frame, "zoom_too_strong", confidence=0.4,
                            fix="reduce_zoom")),
        frames, cut,
    )
    entry = revisions.revisions[0]
    assert entry.status == "needs_human_review"
    assert entry.premiere_ops == []
    assert "low_confidence" in entry.risks
    assert "40%" in entry.status_reason


@pytest.mark.parametrize("fix", ["shorten_section", "reframe", "none"])
def test_a_fix_with_no_safe_form_stays_a_recommendation(cut, frames, fix):
    frame = frames[0]
    revisions = revise(
        critique_of(finding(frame, "bad_crop", confidence=0.95, fix=fix)),
        frames, cut,
    )
    entry = revisions.revisions[0]
    assert entry.status == "needs_human_review"
    assert entry.premiere_ops == []
    assert fix in entry.status_reason


def test_a_zoom_complaint_about_a_frame_with_no_zoom_is_refused(cut, frames):
    """A hallucinated premise must not be able to cause an edit."""
    frame = next(f for f in frames if not f.has_zoom)
    revisions = revise(
        critique_of(finding(frame, "zoom_too_strong", confidence=0.95,
                            fix="reduce_zoom")),
        frames, cut,
    )
    entry = revisions.revisions[0]
    assert entry.status == "needs_human_review"
    assert entry.premiere_ops == []
    assert "not_verifiable" in entry.risks
    assert "no zoom" in entry.status_reason


def test_a_text_complaint_with_no_placeholder_is_refused(cut, frames):
    frame = next(f for f in frames if not f.has_text)
    revisions = revise(
        critique_of(finding(frame, "text_placed_badly", confidence=0.9,
                            fix="move_text_placeholder")),
        frames, cut,
    )
    assert revisions.revisions[0].status == "needs_human_review"
    assert "nothing to move" in revisions.revisions[0].status_reason


def test_a_finding_naming_an_unknown_frame_is_kept_and_reported(cut, frames):
    orphan = CriticFinding(
        finding_id="cf_orphan", frame_id="rf_nowhere", issue="too_dark",
        confidence=0.9, suggested_fix="color_marker",
    )
    revisions = revise(critique_of(orphan), frames, cut)

    assert revisions.revisions[0].status == "needs_human_review"
    assert any("not in the review manifest" in w for w in revisions.warnings)


def test_timing_fixes_can_be_turned_off_entirely(cut, frames):
    frame = frame_of(frames, "clip_end")
    revisions = revise(
        critique_of(finding(frame, "hold_longer", confidence=0.9,
                            fix="extend_hold")),
        frames, cut,
        options=RevisionOptions(allow_timing=False),
    )
    entry = revisions.revisions[0]
    assert entry.status == "needs_human_review"
    assert "--no-timing" in entry.status_reason


def test_extending_a_hold_needs_to_know_the_source_is_long_enough(cut, frames):
    frame = next(f for f in frames if f.placement_id == "p_3")
    revisions = build_revisions(
        critique_of(finding(frame, "hold_longer", confidence=0.9,
                            fix="extend_hold")),
        as_review(frames), cut, asset_durations={},
    )
    entry = revisions.revisions[0]
    assert entry.status == "needs_human_review"
    assert "not_verifiable" in entry.risks


def test_trimming_dead_air_needs_the_audio_layer_to_agree(cut, frames):
    frame = frame_of(frames, "clip_start")
    frame.audio_events = []
    frame.audio_types = []
    revisions = revise(
        critique_of(finding(frame, "cut_too_late", confidence=0.95,
                            fix="trim_dead_air")),
        frames, cut,
    )
    entry = revisions.revisions[0]
    assert entry.status == "needs_human_review"
    assert "not_verifiable" in entry.risks
    assert "does not confirm dead air" in entry.status_reason


def test_trimming_needs_more_confidence_than_anything_else(cut, cut_timeline):
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", source_in=0.0, source_out=10.0,
                              sequence_start=0.0)],
    )
    frames = plan_coverage_frames(plan, timeline=cut_timeline)
    tail = frame_of(frames, "clip_end")
    assert "silence" in tail.audio_types      # the premise is genuinely there

    revisions = build_revisions(
        critique_of(finding(tail, "cut_too_late", confidence=0.72,
                            fix="trim_dead_air")),
        as_review(frames), plan,
        asset_durations={ASSET.asset_id: ASSET.duration},
    )
    entry = revisions.revisions[0]
    assert entry.status == "needs_human_review"
    assert "80%" in entry.status_reason


# ---------------------------------------------------------------------------
# Part 4 -- safe fixes become draft operations
# ---------------------------------------------------------------------------

def test_a_confident_zoom_complaint_reduces_the_zoom(cut, frames):
    frame = next(f for f in frames if f.has_zoom)
    revisions = revise(
        critique_of(finding(frame, "zoom_too_strong", confidence=0.8,
                            fix="reduce_zoom")),
        frames, cut,
    )
    entry = revisions.revisions[0]
    assert entry.status == "accepted"
    assert [op["op"] for op in entry.premiere_ops] == [
        "property.reset", "animate"
    ]
    animate = entry.premiere_ops[1]
    assert animate["to"] == pytest.approx(revise_module.REDUCED_ZOOM_SCALE)
    assert animate["property"] == "Scale"
    assert entry.source_recommendation_id == "r_zoom"


def test_the_reset_comes_before_the_reanimation(cut, frames):
    """Animating Scale twice would stack two pushes, not replace one."""
    frame = next(f for f in frames if f.has_zoom)
    entry = revise(
        critique_of(finding(frame, "zoom_too_strong", confidence=0.8,
                            fix="reduce_zoom")),
        frames, cut,
    ).revisions[0]
    assert entry.premiere_ops[0]["op"] == "property.reset"


def test_a_zoom_already_gentler_than_the_target_is_left_alone(cut, cut_timeline):
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", source_in=0.0, source_out=10.0,
                              sequence_start=0.0)],
        ops=[zoom_op(at=5.0, start=4.0, to=104.0)],
    )
    frames = plan_coverage_frames(plan, timeline=cut_timeline)
    frame = next(f for f in frames if f.has_zoom)

    entry = build_revisions(
        critique_of(finding(frame, "zoom_too_strong", confidence=0.9,
                            fix="reduce_zoom")),
        as_review(frames), plan,
    ).revisions[0]
    assert entry.status == "needs_human_review"
    assert "already" in entry.status_reason


def test_a_hidden_hud_removes_the_zoom_outright(cut, frames):
    frame = next(f for f in frames if f.has_zoom)
    entry = revise(
        critique_of(finding(frame, "hud_hidden", confidence=0.8,
                            severity="high", fix="remove_zoom")),
        frames, cut,
    ).revisions[0]

    assert entry.status == "accepted"
    assert [op["op"] for op in entry.premiere_ops] == ["property.reset"]
    assert "removes_an_edit" in entry.risks


def test_a_text_placeholder_is_re_sited_rather_than_invented(cut, frames):
    frame = frame_of(frames, "text_placeholder")
    entry = revise(
        critique_of(finding(frame, "text_placed_badly", confidence=0.7,
                            fix="move_text_placeholder")),
        frames, cut,
    ).revisions[0]

    assert entry.status == "accepted"
    assert [op["op"] for op in entry.premiere_ops] == [
        "marker.remove", "marker.add"
    ]
    assert "UPPER LEFT THIRD" in entry.premiere_ops[1]["comment"]
    assert "annotation_only" in entry.risks
    assert "No graphic exists yet" in entry.fix_detail


def test_a_full_screen_menu_moves_text_below_it_instead(frames):
    frame = frame_of(frames, "text_placeholder")
    frame.ui_flags = ["inventory_open"]
    assert "lower left" in revise_module._safe_text_zone(frame)


def test_a_brightness_complaint_becomes_a_colour_marker(cut, frames):
    frame = frames[0]
    entry = revise(
        critique_of(finding(frame, "too_dark", confidence=0.75,
                            fix="color_marker")),
        frames, cut,
    ).revisions[0]

    assert entry.status == "accepted"
    assert entry.premiere_ops[0]["op"] == "marker.add"
    assert entry.premiere_ops[0]["name"] == "COLOR"
    assert "lift the shadows" in entry.premiere_ops[0]["comment"]
    assert "annotation_only" in entry.risks


def test_a_callout_suggestion_becomes_a_callout_marker(cut, frames):
    frame = frames[0]
    entry = revise(
        critique_of(finding(frame, "callout_needed", confidence=0.7,
                            fix="callout_marker")),
        frames, cut,
    ).revisions[0]
    assert entry.premiere_ops[0]["name"] == "CALLOUT"


def test_an_unfixable_finding_can_still_become_a_review_marker(cut, frames):
    frame = frames[0]
    entry = revise(
        critique_of(finding(frame, "marker_mismatch", confidence=0.8,
                            fix="review_marker")),
        frames, cut,
    ).revisions[0]

    assert entry.status == "accepted"
    assert entry.premiere_ops[0]["name"] == "REVIEW"
    assert entry.revision_id in entry.premiere_ops[0]["comment"]


def test_a_hold_extends_by_a_bounded_amount(cut, frames):
    frame = next(f for f in frames if f.placement_id == "p_3")
    entry = revise(
        critique_of(finding(frame, "hold_longer", confidence=0.9,
                            fix="extend_hold")),
        frames, cut,
    ).revisions[0]

    assert entry.status == "accepted"
    op = entry.premiere_ops[0]
    assert op["op"] == "clip.trim"
    assert op["edge"] == "out"
    # Negative extends; the amount is capped however much footage remains.
    assert op["by"] == pytest.approx(-revise_module.MAX_HOLD_EXTENSION)
    assert op["ripple"] is True
    assert "changes_timing" in entry.risks


def test_dead_air_is_trimmed_only_with_both_channels_agreeing(cut_timeline):
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", source_in=0.0, source_out=10.0,
                              sequence_start=0.0)],
    )
    frames = plan_coverage_frames(plan, timeline=cut_timeline)
    tail = frame_of(frames, "clip_end")

    entry = build_revisions(
        critique_of(finding(tail, "cut_too_late", confidence=0.9,
                            fix="trim_dead_air")),
        as_review(frames), plan,
        asset_durations={ASSET.asset_id: ASSET.duration},
    ).revisions[0]

    assert entry.status == "accepted"
    op = entry.premiere_ops[0]
    assert op["op"] == "clip.trim"
    assert op["edge"] == "out"
    assert 0 < op["by"] <= revise_module.MAX_TRIM_SECONDS


def test_a_trim_never_leaves_a_sliver_of_a_clip(cut_timeline):
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", source_in=8.0, source_out=9.1,
                              sequence_start=0.0)],
    )
    frames = plan_coverage_frames(plan, timeline=cut_timeline)
    frame = frames[0]
    frame.frame_kind = "clip_end"
    frame.audio_events = [{"type": "silence", "start": 8.0, "end": 9.1}]

    entry = build_revisions(
        critique_of(finding(frame, "cut_too_late", confidence=0.95,
                            fix="trim_dead_air")),
        as_review(frames), plan,
    ).revisions[0]
    assert entry.status == "needs_human_review"
    assert "1.0s long" in entry.status_reason


# ---------------------------------------------------------------------------
# Part 4 -- the revision plan
# ---------------------------------------------------------------------------

def accepted_plan(cut, frames, *findings, **kw):
    revisions = revise(critique_of(*findings), frames, cut)
    return revisions, plan_module.build_revision_plan(revisions, cut, **kw)


def test_the_plan_activates_the_rough_cuts_own_sequence_first(cut, frames):
    frame = frames[0]
    _, plan = accepted_plan(
        cut, frames,
        finding(frame, "too_dark", confidence=0.8, fix="color_marker"),
    )
    assert plan.ops[0]["op"] == "sequence.activate"
    assert plan.ops[0]["name"] == SEQUENCE
    assert not any(op["op"] == "sequence.create" for op in plan.ops)


def test_the_plan_orders_zooms_then_trims_then_markers(cut, frames):
    zoomed = next(f for f in frames if f.has_zoom)
    hold = next(f for f in frames if f.placement_id == "p_3")
    _, plan = accepted_plan(
        cut, frames,
        finding(zoomed, "zoom_too_strong", confidence=0.85, fix="reduce_zoom"),
        finding(hold, "hold_longer", confidence=0.9, fix="extend_hold"),
        finding(frames[0], "too_dark", confidence=0.8, fix="color_marker"),
    )
    order = [op["op"] for op in plan.ops]
    assert order[0] == "sequence.activate"
    assert order.index("property.reset") < order.index("clip.trim")
    assert order.index("clip.trim") < order.index("marker.add")


def test_trims_run_back_to_front(cut, cut_timeline):
    """Rippling shifts later clips, so the last one has to go first."""
    plan_in = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[
            placement("p_a", source_in=0.0, source_out=10.0,
                      sequence_start=0.0, index=0),
            placement("p_b", source_in=20.0, source_out=30.0,
                      sequence_start=10.0, index=1),
        ],
    )
    frames = plan_coverage_frames(plan_in, timeline=cut_timeline)
    first = next(f for f in frames if f.placement_id == "p_a")
    second = next(f for f in frames if f.placement_id == "p_b")

    revisions = build_revisions(
        critique_of(
            finding(first, "hold_longer", confidence=0.9, fix="extend_hold"),
            finding(second, "hold_longer", confidence=0.9, fix="extend_hold"),
        ),
        as_review(frames), plan_in,
        asset_durations={ASSET.asset_id: ASSET.duration},
    )
    plan = plan_module.build_revision_plan(revisions, plan_in)
    trims = [op for op in plan.ops if op["op"] == "clip.trim"]

    assert len(trims) == 2
    assert trims[0]["clip"]["at"] > trims[1]["clip"]["at"]


def test_a_marker_after_a_trim_is_moved_to_follow_its_content(cut, frames):
    hold = next(f for f in frames if f.placement_id == "p_3")
    hold.frame_kind = "clip_start"
    hold.audio_events = [{"type": "silence", "start": 60.0, "end": 63.0}]
    late = next(
        f for f in frames
        if f.placement_id == "p_3" and f is not hold
    ) if sum(1 for f in frames if f.placement_id == "p_3") > 1 else hold

    revisions = revise(
        critique_of(
            finding(hold, "cut_too_late", confidence=0.95, fix="trim_dead_air"),
            finding(late, "too_dark", confidence=0.8, fix="color_marker"),
        ),
        frames, cut,
    )
    plan = plan_module.build_revision_plan(revisions, cut)
    trim = next(op for op in plan.ops if op["op"] == "clip.trim")
    marker = next(op for op in plan.ops if op["op"] == "marker.add")

    assert trim["by"] > 0
    assert marker["time"] == pytest.approx(late.sequence_time - trim["by"])
    assert "moved" in marker["note"]


def test_a_timing_change_warns_about_the_markers_it_invalidates(cut, frames):
    hold = next(f for f in frames if f.placement_id == "p_3")
    _, plan = accepted_plan(
        cut, frames,
        finding(hold, "hold_longer", confidence=0.9, fix="extend_hold"),
    )
    assert any("ripple" in warning for warning in plan.warnings)


def test_a_deferred_finding_that_matters_still_reaches_the_timeline(cut, frames):
    frame = frames[0]
    _, plan = accepted_plan(
        cut, frames,
        finding(frame, "bad_crop", confidence=0.7, severity="high",
                fix="reframe"),
    )
    review_markers = [
        op for op in plan.ops
        if op["op"] == "marker.add" and op["name"] == "REVIEW"
    ]
    assert len(review_markers) == 1
    assert "NEEDS HUMAN REVIEW" in review_markers[0]["comment"]
    assert plan.not_applied
    assert "REVIEW marker was placed" in plan.not_applied[0].reason


def test_a_deferred_finding_nobody_believes_stays_off_the_timeline(cut, frames):
    frame = frames[0]
    _, plan = accepted_plan(
        cut, frames,
        finding(frame, "bad_crop", confidence=0.2, severity="low",
                fix="reframe"),
    )
    assert not any(op["op"] == "marker.add" for op in plan.ops)
    assert plan.not_applied
    assert "below" in plan.not_applied[0].reason


def test_review_markers_can_be_turned_off(cut, frames):
    frame = frames[0]
    _, plan = accepted_plan(
        cut, frames,
        finding(frame, "bad_crop", confidence=0.9, severity="high",
                fix="reframe"),
        mark_deferred=False,
    )
    assert plan.ops == []


def test_a_plan_with_nothing_to_do_is_empty_rather_than_a_no_op(cut, frames):
    frame = frames[0]
    _, plan = accepted_plan(
        cut, frames,
        finding(frame, "too_dark", confidence=0.1, severity="low",
                fix="color_marker"),
    )
    assert plan.ops == []
    assert any("nothing to apply" in warning for warning in plan.warnings)


def test_the_plan_says_when_the_rough_cut_was_never_executed(cut, frames):
    _, plan = accepted_plan(
        cut, frames,
        finding(frames[0], "too_dark", confidence=0.8, fix="color_marker"),
    )
    assert plan.roughcut_executed is False
    assert any("never" in w or "may not exist" in w for w in plan.warnings)


def test_the_plan_round_trips_through_json(cut, frames):
    _, plan = accepted_plan(
        cut, frames,
        finding(frames[0], "too_dark", confidence=0.8, fix="color_marker"),
    )
    restored = RevisionPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert restored.ops == plan.ops
    assert restored.sequence_name == plan.sequence_name
    assert [n.to_dict() for n in restored.not_applied] == [
        n.to_dict() for n in plan.not_applied
    ]


# ---------------------------------------------------------------------------
# Part 6 -- execution guards
# ---------------------------------------------------------------------------

@pytest.fixture
def runnable(cut, frames):
    """A revision plan that passes every guard, on an executed rough cut."""
    zoomed = next(f for f in frames if f.has_zoom)
    revisions = revise(
        critique_of(finding(zoomed, "hud_hidden", confidence=0.85,
                            severity="high", fix="remove_zoom")),
        frames, cut,
    )
    return plan_module.build_revision_plan(
        revisions, cut, roughcut_executed=True
    )


def test_the_dry_run_validates_offline(runnable):
    critic_execute.dry_run(runnable)
    assert runnable.dry_run_passed is True
    assert runnable.explanation
    assert runnable.dry_run_error is None


def test_an_empty_plan_fails_the_dry_run_with_a_reason():
    plan = RevisionPlan(sequence_name=SEQUENCE)
    critic_execute.dry_run(plan)
    assert plan.dry_run_passed is False
    assert plan.dry_run_error["code"] == "empty_plan"


@pytest.mark.parametrize("op", [
    {"op": "sequence.create", "name": "Something Else"},
    {"op": "project.import", "paths": ["/x.mp4"]},
    {"op": "project.save"},
    {"op": "clip.remove", "clip": {"track": "V1", "index": 0}},
    {"op": "clip.append", "asset": "/x.mp4", "track": "V1"},
])
def test_a_revision_may_not_contain_a_re_edit_or_a_side_effect(runnable, op):
    runnable.ops.append(op)
    critic_execute.dry_run(runnable)

    assert runnable.dry_run_passed is False
    assert runnable.dry_run_error["code"] == "forbidden_operation"
    assert op["op"] in runnable.dry_run_error["error"]


def test_plan_only_validates_nothing_and_runs_nothing(runnable):
    engine = FakeEngine()
    report = critic_execute.run(runnable, mode="plan_only", engine=engine)

    assert report.executed is False
    assert report.dry_run_passed is False
    assert engine.calls == []


def test_a_dry_run_never_reaches_the_engine(runnable, cut):
    engine = FakeEngine()
    report = critic_execute.run(
        runnable, mode="dry_run", roughcut=cut, engine=engine
    )
    assert report.executed is False
    assert report.dry_run_passed is True
    assert engine.calls == []


def test_execution_refuses_when_the_dry_run_fails(runnable, cut):
    runnable.ops.append({"op": "project.save"})
    engine = FakeEngine()
    report = critic_execute.run(
        runnable, mode="execute", roughcut=cut, engine=engine
    )

    assert report.executed is False
    assert report.refused_reason
    assert engine.calls == []


def test_execution_validates_again_rather_than_trusting_a_stored_pass(runnable,
                                                                      cut):
    """A pass recorded earlier is not evidence about the plan being run now."""
    critic_execute.dry_run(runnable)
    assert runnable.dry_run_passed is True

    runnable.ops.append({"op": "clip.remove", "clip": {"track": "V1",
                                                       "index": 0}})
    engine = FakeEngine()
    report = critic_execute.run(
        runnable, mode="execute", roughcut=cut, engine=engine
    )
    assert report.executed is False
    assert engine.calls == []


def test_execution_refuses_a_plan_that_does_not_activate_its_target(runnable,
                                                                    cut):
    runnable.ops[0] = {"op": "sequence.activate", "name": "Someone's Real Edit"}
    engine = FakeEngine()
    report = critic_execute.run(
        runnable, mode="execute", roughcut=cut, engine=engine
    )

    assert report.executed is False
    assert report.on_scratch is False
    assert "Someone's Real Edit" in report.refused_reason
    assert engine.calls == []


def test_execution_refuses_a_plan_that_does_not_activate_at_all(runnable, cut):
    runnable.ops = [op for op in runnable.ops
                    if op["op"] != "sequence.activate"]
    engine = FakeEngine()
    report = critic_execute.run(
        runnable, mode="execute", roughcut=cut, engine=engine
    )
    assert report.executed is False
    assert "whichever sequence happens to be open" in report.refused_reason
    assert engine.calls == []


def test_execution_refuses_when_the_rough_cut_was_not_itself_scratch_safe(
    runnable, cut
):
    cut.ops = [op for op in cut.ops if op["op"] != "sequence.create"]
    engine = FakeEngine()
    report = critic_execute.run(
        runnable, mode="execute", roughcut=cut, engine=engine
    )

    assert report.executed is False
    assert "scratch" in report.refused_reason
    assert engine.calls == []


def test_execution_refuses_when_the_sequence_was_never_built(cut, frames):
    zoomed = next(f for f in frames if f.has_zoom)
    revisions = revise(
        critique_of(finding(zoomed, "hud_hidden", confidence=0.85,
                            fix="remove_zoom")),
        frames, cut,
    )
    plan = plan_module.build_revision_plan(
        revisions, cut, roughcut_executed=False
    )
    engine = FakeEngine()
    report = critic_execute.run(
        plan, mode="execute", roughcut=cut, engine=engine
    )

    assert report.executed is False
    assert "no record" in report.refused_reason
    assert engine.calls == []


def test_a_plan_that_passes_every_guard_runs(runnable, cut):
    engine = FakeEngine()
    report = critic_execute.run(
        runnable, mode="execute", roughcut=cut, engine=engine
    )

    assert report.executed is True
    assert report.on_scratch is True
    assert report.operations_succeeded == len(runnable.ops)
    assert len(engine.calls) == 1
    assert engine.calls[0].get("dry_run") is not True
    assert runnable.executed is True


def test_a_premiere_failure_is_reported_rather_than_raised(runnable, cut):
    report = critic_execute.run(
        runnable, mode="execute", roughcut=cut,
        engine=FakeEngine(succeed=False),
    )
    assert report.executed is False
    assert report.error["error"] == "Premiere said no"


def test_an_unknown_mode_is_a_usage_error(runnable):
    with pytest.raises(EditingError):
        critic_execute.run(runnable, mode="just-do-it")


def test_the_allowlist_is_the_whole_guarantee():
    """Every op the revision path can emit must be on the allowlist."""
    emitted = {
        "sequence.activate", "property.reset", "animate", "clip.trim",
        "marker.add", "marker.remove",
    }
    assert emitted == set(critic_execute.ALLOWED_OPS)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def test_the_report_leads_with_what_it_could_not_fix(cut, frames):
    revisions = revise(
        critique_of(
            finding(frames[0], "bad_crop", confidence=0.9, severity="high",
                    fix="reframe"),
            finding(frames[0], "too_dark", confidence=0.8, fix="color_marker"),
        ),
        frames, cut,
    )
    text = critic_report.render(revisions)
    assert text.index("NOT FIXED AUTOMATICALLY") < text.index("WOULD BE APPLIED")
    assert "nothing in this report has been applied" in text.lower()


def test_the_report_says_loudly_when_the_critic_was_a_mock(cut, frames):
    revisions = revise(
        critique_of(finding(frames[0], "too_dark", confidence=0.8,
                            fix="color_marker"), mock=True),
        frames, cut,
    )
    assert "MOCK CRITIC" in critic_report.render(revisions)


def test_show_issues_ranks_and_filters_by_severity(cut, frames):
    revisions = revise(
        critique_of(
            finding(frames[0], "bad_crop", confidence=0.9, severity="high",
                    fix="reframe"),
            finding(frames[1], "callout_needed", confidence=0.8,
                    severity="low", fix="callout_marker"),
        ),
        frames, cut,
    )
    everything = critic_report.render_issues(revisions)
    assert "bad_crop" in everything and "callout_needed" in everything

    severe = critic_report.render_issues(revisions, severity="high")
    assert "bad_crop" in severe
    assert "callout_needed" not in severe


# ---------------------------------------------------------------------------
# Pipeline and CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def staged(config, cut, cut_timeline, recommendations, monkeypatch, tmp_path):
    """A rough cut, timeline and exported review frames, all on disk."""
    from editing.pipeline import build_pipeline

    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(
        review_module.ff, "extract_frame", lambda *a, **k: image
    )

    from dataclasses import replace
    pipeline = build_pipeline(
        replace(config, vision_backend="mock"),
        __import__("editing.config", fromlist=["SamplingConfig"]).SamplingConfig(),
    )
    pipeline.write_timeline(cut_timeline)
    pipeline.write_recommendations(recommendations)
    pipeline.write_rough_cut(cut)
    pipeline.review_frames(cut)
    return pipeline


def test_the_pipeline_runs_the_whole_pass_without_any_externals(staged):
    critique = staged.critique()
    assert critique.mock is True

    revisions, plan = staged.revise()
    assert isinstance(revisions, RevisionSet)
    assert (staged.config.critic_dir / "structure.critique.json").exists()
    assert (staged.config.critic_dir / "structure.revisions.json").exists()
    assert (staged.config.critic_dir / "structure.revision-plan.json").exists()
    assert (staged.config.critic_dir / "structure.revisions.txt").exists()


def test_the_rough_cut_report_survives_the_revision_pass(staged, cut):
    """A second opinion must not overwrite the thing it is judging."""
    before = (staged.config.roughcut_dir / "structure.json").read_text("utf-8")
    staged.critique()
    staged.revise()
    after = (staged.config.roughcut_dir / "structure.json").read_text("utf-8")
    assert before == after


def test_the_revision_pass_reloads_from_disk_unchanged(staged):
    staged.critique()
    revisions, plan = staged.revise()

    assert staged.load_critique().stats() == staged.load_critique().stats()
    assert len(staged.load_revisions()) == len(revisions)
    assert staged.load_revision_plan().ops == plan.ops


def run_cli(argv, capsys):
    from editing.cli import main

    code = main(argv)
    return code, capsys.readouterr()


def test_the_cli_refuses_to_execute_without_yes(staged, capsys):
    staged.critique()
    staged.revise()
    code, captured = run_cli([
        "review", "execute", "--output-dir", str(staged.config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 1
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert "--yes" in payload["hint"]
    assert (
        not (staged.config.critic_dir / "structure.revision-execution.json")
        .exists()
    )


def test_the_bare_review_command_still_lists_frames(staged, capsys):
    code, captured = run_cli([
        "review", "--list", "--output-dir", str(staged.config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["exported"] is False
    assert payload["count"] > 0


def test_the_cli_shows_issues_as_json(staged, capsys):
    staged.critique()
    staged.revise()
    code, captured = run_cli([
        "review", "show-issues", "--output-dir", str(staged.config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert payload["mock"] is True
    assert all("issue" in entry for entry in payload["issues"])


def test_the_cli_dry_run_applies_nothing(staged, capsys):
    staged.critique()
    staged.revise()
    code, captured = run_cli([
        "review", "dry-run", "--output-dir", str(staged.config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["report"]["executed"] is False

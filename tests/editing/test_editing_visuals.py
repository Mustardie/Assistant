"""The creative visual layer: what earns emphasis, and what is refused.

Four properties carry the weight, and all four are about restraint.

**No effect without evidence.** Every moment comes from something an earlier
pass recorded. The tests assert that a moment carries its source and its
evidence, and that a timeline with nothing in it produces nothing.

**The HUD is protected before anything else.** It is the one check a style may
not override, and the tests exercise it from both sides: a covering effect over
an open inventory, and a zoom that would push the health bar off the frame.

**Every refusal is inspectable.** Including the ones the style never offered
anything for. A test that only checked "two effects were planned" would pass
just as happily against a module that plans everything.

**Nothing is drawn or executed.** ``burned_in`` is False everywhere, the
Premiere plan validates against the real catalog without touching a host, and
no test in this file needs Premiere, FFmpeg, a GPU, a model or real footage.
"""
from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest

from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.schema import (
    AudioEvent, StructureTimeline, TimeRange, TimelineSegment, TranscriptEntry,
    VisualEvent,
)
from editing.style import presets as style_presets
from editing.visuals import compose as compose_module
from editing.visuals import moments as moments_module
from editing.visuals import plan as plan_module
from editing.visuals import premiere as premiere_module
from editing.visuals import preview as preview_module
from editing.visuals import report as report_module
from editing.visuals import safety as safety_module
from editing.visuals import store as visuals_store
from editing.visuals import treatments as treatments_module
from editing.visuals.execution import (
    FinalEditPlan, PremiereVisualOperationPlan, build_comparison,
)
from editing.visuals.schema import (
    EFFECT_FAMILY, EFFECT_TYPES, MARKER_EFFECTS, REJECT_REASONS,
    VISUAL_MOMENT_TYPES, VisualConfig, VisualLayerPlan, VisualMoment,
    VisualTreatment,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# Built locally rather than shared, because ``tests/`` deliberately has no
# ``__init__.py`` -- that is what puts the repo root on sys.path -- so test
# modules cannot import from each other.

def make_segment(
    index: int,
    said: str = "",
    importance: str = "setup",
    *,
    threats=(),
    entities=(),
    death: bool = False,
    low_health: bool = False,
    audio_types=(),
    ui: str = "",
    confidence: float = 0.85,
    length: float = 10.0,
    coordinates: str = "",
) -> TimelineSegment:
    start, end = index * length, index * length + length
    event = VisualEvent(
        event_id=f"e{index}", source_file="/f/a.mp4", asset_id="a1",
        start=start, end=end, confidence=0.9, environment="cave",
        actions=["mining"], threats=list(threats), entities=list(entities),
        importance=importance,
        suggested_range=TimeRange(start=start, end=end), model="test",
    )
    if death:
        event.ui.death_screen = True
    if low_health:
        event.ui.low_health = True
    if ui:
        setattr(event.ui, ui, True)
    if coordinates:
        event.ui.coordinates = coordinates

    entries = []
    if said:
        entries.append(TranscriptEntry(
            start=start + 0.5, end=start + 3.0, text=said,
            confidence=confidence))
    return TimelineSegment(
        segment_id=f"s{index}", asset_id="a1", source_file="/f/a.mp4",
        start=start, end=end, said=said, speech_entries=entries,
        events=[event],
        audio_events=[
            AudioEvent(event_id=f"au{index}{kind}", source_file="/f/a.mp4",
                       asset_id="a1", start=start + 5, end=start + 6,
                       type=kind, confidence=0.8, detection="heuristic")
            for kind in audio_types
        ],
        alignment="match", usefulness=0.7, usable=True,
    )


def make_cut(count: int, *, length: float = 10.0) -> RoughCutPlan:
    return RoughCutPlan(sequence_name="Test Cut", placements=[
        ClipPlacement(
            placement_id=f"p{index}", asset_id="a1", source_file="/f/a.mp4",
            source_in=index * length, source_out=index * length + length,
            sequence_start=index * length, index=index)
        for index in range(count)
    ])


@pytest.fixture
def episode() -> StructureTimeline:
    """An episode with a death, a creeper, a find and a sign-off in it."""
    return StructureTimeline(segments=[
        make_segment(0, "right so today we are going to find some diamonds",
                     "setup"),
        make_segment(1, "just walking along here", "boring"),
        make_segment(2, "oh god a creeper watch out run", "danger",
                     threats=("creeper",), audio_types=("sudden_reaction",)),
        make_segment(3, "i died that killed me", "payoff", death=True),
        make_segment(4, "look at that diamonds there we go", "reveal",
                     entities=("diamond_ore",),
                     audio_types=("loudness_spike",)),
        make_segment(5, "um", "boring"),
        make_segment(6, "half a heart left oh no", "danger",
                     threats=("skeleton",), low_health=True),
        make_segment(7, "next time we go to the nether", "setup"),
    ])


@pytest.fixture
def cut() -> RoughCutPlan:
    return make_cut(8)


@pytest.fixture
def style():
    return style_presets.get("fast_funny")


def plan_for(episode, cut, style, layer="balanced", **overrides):
    config = replace(
        treatments_module.visual_defaults(style, layer), **overrides
    ).validated()
    return plan_module.build_visual_plan(episode, cut, style, config)


def moment(**overrides) -> VisualMoment:
    base = {
        "moment_id": "m1", "kind": "danger", "source_type": "visual",
        "source_id": "s1", "start": 20.0, "end": 24.0, "confidence": 0.8,
        "importance": 0.8, "label": "a creeper", "entities": ["creeper"],
        "hud": {},
    }
    base.update(overrides)
    return VisualMoment(**base)


def treatment(**overrides) -> VisualTreatment:
    base = {
        "treatment_id": "t1", "moment_id": "m1", "effect": "zoom_punch",
        "intensity": "medium", "start": 20.0, "end": 21.0,
        "priority": 0.8, "payload": {"scale": 110.0},
    }
    base.update(overrides)
    return VisualTreatment(**base)


# ---------------------------------------------------------------------------
# Part 1 -- schemas
# ---------------------------------------------------------------------------

def test_the_visual_layer_is_off_by_default():
    """Deciding where somebody's video zooms is not a default."""
    config = VisualConfig()
    assert config.layer == "off"
    assert config.enabled is False
    assert config.mode == "plan_only"


def test_every_effect_has_a_family():
    for effect in EFFECT_TYPES:
        assert effect in EFFECT_FAMILY, effect


def test_every_moment_kind_the_library_names_is_a_real_kind():
    for kind in treatments_module.MOMENT_EFFECTS:
        assert kind in VISUAL_MOMENT_TYPES, kind


def test_every_effect_the_library_offers_is_a_real_effect():
    for kind, effects in treatments_module.MOMENT_EFFECTS.items():
        for effect in effects:
            assert effect in EFFECT_TYPES, (kind, effect)


def test_every_effect_has_defaults_and_a_reason():
    for effect in EFFECT_TYPES:
        assert effect in treatments_module.EFFECT_DEFAULTS, effect
        assert treatments_module._reason(moment(), effect)


def test_a_plan_survives_a_round_trip(episode, cut, style):
    plan = plan_for(episode, cut, style)
    restored = VisualLayerPlan.from_dict(plan.to_dict())
    assert len(restored.accepted) == len(plan.accepted)
    assert len(restored.rejected) == len(plan.rejected)
    assert restored.stats()["by_effect"] == plan.stats()["by_effect"]
    assert len(restored.moments) == len(plan.moments)


def test_a_round_trip_never_promotes_a_rejection(episode, cut, style):
    plan = plan_for(episode, cut, style, layer="minimal")
    restored = VisualLayerPlan.from_dict(plan.to_dict())
    assert len(restored.accepted) == len(plan.accepted)


def test_a_nonsense_config_clamps_rather_than_raising():
    config = VisualConfig(
        layer="loud", mode="do-it", max_effects_per_minute=-4.0,
        max_effect_seconds=900.0, min_confidence=5.0,
    ).validated()
    assert config.layer == "off"
    assert config.mode == "plan_only"
    assert config.max_effects_per_minute == 0.0
    assert config.max_effect_seconds == 30.0
    assert 0.0 <= config.min_confidence <= 1.0


# ---------------------------------------------------------------------------
# Part 2 -- moment detection
# ---------------------------------------------------------------------------

def test_a_death_screen_becomes_a_death_moment(episode, cut):
    found = moments_module.detect_moments(episode, cut)
    deaths = [m for m in found if m.kind == "death_or_fail"]
    assert deaths
    assert deaths[0].source_type == "visual"
    assert deaths[0].evidence


def test_a_creeper_on_screen_becomes_a_danger_moment(episode, cut):
    found = moments_module.detect_moments(episode, cut)
    danger = [m for m in found if m.kind == "danger"]
    assert danger
    assert "creeper" in danger[0].entities


def test_low_health_with_a_threat_is_a_near_death(episode, cut):
    found = moments_module.detect_moments(episode, cut)
    assert any(m.kind == "near_death" for m in found)


def test_a_sign_off_line_is_a_cliffhanger(episode, cut):
    found = moments_module.detect_moments(episode, cut)
    cliff = [m for m in found if m.kind == "cliffhanger"]
    assert cliff
    assert cliff[0].source_type == "transcript"


def test_every_moment_names_its_source_and_carries_evidence(episode, cut):
    for found in moments_module.detect_moments(episode, cut):
        assert found.source_type
        assert found.source_id
        assert found.evidence, found.kind
        assert found.kind in VISUAL_MOMENT_TYPES


def test_footage_with_nothing_in_it_produces_no_moments():
    quiet = StructureTimeline(segments=[
        make_segment(0, "", "boring"), make_segment(1, "", "boring"),
    ])
    assert moments_module.detect_moments(quiet, make_cut(2)) == []


def test_a_moment_whose_footage_was_cut_is_dropped(episode):
    """Placing an effect on footage nobody kept would put it over nothing."""
    # A cut that keeps only the first two clips: the death is at clip 3.
    found = moments_module.detect_moments(episode, make_cut(2))
    assert not any(m.kind == "death_or_fail" for m in found)


def test_two_layers_noticing_one_death_produce_one_moment(episode, cut):
    """Otherwise a single death earns a zoom, a freeze and an arrow."""
    class FakeCaption:
        accepted = [type("C", (), {
            "moment": "death_or_fail", "start": 30.5, "end": 32.0,
            "caption_id": "cap1", "priority": 0.9, "text": "i died",
            "full_line": "i died that killed me", "reason": "a death",
            "asset_id": "a1", "segment_id": "s3",
        })()]

    found = moments_module.detect_moments(
        episode, cut, caption_plan=FakeCaption())
    deaths = [m for m in found if m.kind == "death_or_fail"]
    assert len(deaths) == 1
    # And the merged record carries both layers' evidence.
    assert len(deaths[0].evidence) >= 2


def test_an_accepted_caption_becomes_a_moment(episode, cut):
    class FakeCaption:
        accepted = [type("C", (), {
            "moment": "payoff_line", "start": 45.0, "end": 47.0,
            "caption_id": "cap1", "priority": 0.85, "text": "there we go",
            "full_line": "there we go", "reason": "the payoff lands",
            "asset_id": "a1", "segment_id": "s4",
        })()]

    found = moments_module.detect_moments(
        episode, cut, caption_plan=FakeCaption())
    payoffs = [m for m in found if m.source_type == "polish"]
    assert payoffs
    assert payoffs[0].kind == "payoff"


def test_a_cold_open_becomes_an_opening_hook(episode, cut):
    class FakeRetention:
        cold_open = type("C", (), {
            "chosen": True, "hook_id": "h1", "hook_type": "danger",
            "duration": 8.0, "original_start": 90.0,
            "suggested_text": "a creeper moment",
        })()
        decisions = []

    found = moments_module.detect_moments(
        episode, cut, retention_plan=FakeRetention())
    hooks = [m for m in found if m.kind == "opening_hook"]
    assert hooks
    # A cold open is at the *front* of the cut, whatever its source time was.
    assert hooks[0].start == 0.0


def test_an_episode_memory_in_the_wrong_timebase_resolves_by_segment(
    episode, cut
):
    """Session 10D's rule: guessing places every finding, all of them wrong."""
    class FakeBeat:
        item_id = "b1"
        kind = "reveal"
        confidence = 0.8
        interest = 0.7
        why = "a reveal"
        start = 999.0                 # nonsense in the cut's timebase
        end = 1009.0
        segment_ids = ["s4"]

    class FakeMemory:
        timebase = "timeline"
        beats = [FakeBeat()]
        main_objective = None
        callbacks = []
        payoffs = []

    found = moments_module.detect_moments(episode, cut, memory=FakeMemory())
    reveals = [m for m in found
               if m.source_type == "episode" and m.kind == "reveal"]
    assert reveals
    # Resolved through segment s4, which is clip 4: 40s-50s on the cut.
    assert 40.0 <= reveals[0].start < 50.0


def test_a_moment_carries_the_hud_state_the_safety_pass_needs(episode, cut):
    found = moments_module.detect_moments(episode, cut)
    deaths = [m for m in found if m.kind == "death_or_fail"]
    assert deaths[0].hud.get("death_screen") is True


# ---------------------------------------------------------------------------
# Part 3 and 4 -- the library and the styles
# ---------------------------------------------------------------------------

def test_the_library_offers_nothing_when_the_layer_is_off(style):
    config = VisualConfig(layer="off").validated()
    assert treatments_module.propose(moment(), style, config) == []


def test_a_style_narrows_what_the_evidence_can_become():
    """Two styles, the same layer, the same evidence, different effects."""
    quiet = style_presets.get("cinematic_minecraft")
    loud = style_presets.get("fast_funny")

    loud_allowed = treatments_module.allowed_effects(
        loud, treatments_module.visual_defaults(loud, "balanced"))
    quiet_allowed = treatments_module.allowed_effects(
        quiet, treatments_module.visual_defaults(quiet, "balanced"))

    # fast_funny is the one style that reads a punch zoom as the point rather
    # than as noise, and it is the only one whose defaults turn meme effects on.
    assert "zoom_punch" in loud_allowed
    assert "zoom_punch" not in quiet_allowed
    # And the quiet style keeps what it is for.
    assert "slow_zoom_hold" in quiet_allowed


def test_a_bare_config_turns_meme_effects_off_whatever_the_style():
    """The switch is the configuration's, not the style's, so it always wins."""
    loud = style_presets.get("fast_funny")
    plain = VisualConfig(layer="balanced").validated()
    assert plain.allow_meme_effects is False
    assert "zoom_punch" not in treatments_module.allowed_effects(loud, plain)


def test_minimal_clean_allows_almost_nothing():
    style = style_presets.get("minimal_clean")
    config = treatments_module.visual_defaults(style, "high")
    allowed = treatments_module.allowed_effects(style, config)
    assert len(allowed) <= 6
    assert "screen_shake" not in allowed
    assert "impact_flash" not in allowed


def test_a_style_with_no_zooms_never_plans_one():
    style = style_presets.get("minimal_clean")   # max_zoom_scale 100.0
    config = treatments_module.visual_defaults(style, "high")
    allowed = treatments_module.allowed_effects(style, config)
    assert not {"zoom_punch", "quick_punch_in", "slow_zoom_hold"} & allowed


@pytest.mark.parametrize("name", style_presets.names())
def test_every_style_produces_a_valid_config(name):
    config = treatments_module.visual_defaults(
        style_presets.get(name), "balanced")
    assert config.layer == "balanced"
    assert 0 <= config.max_effects_per_minute <= 30
    assert 0 <= config.max_callouts_per_minute <= 30


def test_a_louder_layer_never_plans_less_than_a_quieter_one(episode, cut,
                                                            style):
    minimal = plan_for(episode, cut, style, "minimal")
    balanced = plan_for(episode, cut, style, "balanced")
    high = plan_for(episode, cut, style, "high")
    assert len(minimal.accepted) <= len(balanced.accepted) <= len(high.accepted)


def test_a_quiet_style_stays_quieter_than_a_loud_one_at_every_layer():
    """Picking a style has to still mean something at --visual-layer high."""
    quiet = style_presets.get("cinematic_minecraft")
    loud = style_presets.get("fast_funny")
    for layer in ("minimal", "balanced", "high"):
        quiet_config = treatments_module.visual_defaults(quiet, layer)
        loud_config = treatments_module.visual_defaults(loud, layer)
        assert quiet_config.max_effects_per_minute <= \
            loud_config.max_effects_per_minute


def test_a_card_with_nothing_to_say_is_not_built(style):
    """No card with invented words on it."""
    config = VisualConfig(layer="balanced").validated()
    bare = moment(kind="objective_start", label="", entities=[])
    proposed = treatments_module.propose(bare, style, config, context={})
    assert not [c for c in proposed if c.effect == "objective_card"]


def test_a_card_says_what_was_actually_stated():
    config = VisualConfig(layer="balanced").validated()
    documentary = style_presets.get("documentary_story")
    proposed = treatments_module.propose(
        moment(kind="objective_start", label="find diamonds"),
        documentary, config, context={"objective": "find diamonds today"})
    cards = [c for c in proposed if c.effect == "objective_card"]
    assert cards
    assert cards[0].payload["text"] == "find diamonds today"
    assert cards[0].payload["text_source"] == "transcript_quote"


def test_a_zoom_never_exceeds_the_styles_own_ceiling():
    quiet = style_presets.get("documentary_story")   # max_zoom_scale 104.0
    config = VisualConfig(layer="high", allow_meme_effects=True).validated()
    proposed = treatments_module.propose(
        moment(kind="reveal"), quiet, config)
    for candidate in proposed:
        if "scale" in candidate.payload:
            assert candidate.payload["scale"] <= quiet.max_zoom_scale


# ---------------------------------------------------------------------------
# Part 5 -- safety
# ---------------------------------------------------------------------------

def check(treat, mom, config=None, **kwargs):
    config = config or VisualConfig(layer="balanced").validated()
    return safety_module.check_all(treat, mom, config, **kwargs)


def test_an_effect_over_an_open_inventory_is_refused():
    result = check(treatment(effect="label_tag", payload={"text": "hi"}),
                   moment(hud={"inventory_open": True}))
    assert result.accepted is False
    assert result.reject_reason == "hides_hud"
    assert "inventory" in result.reject_detail


def test_a_zoom_that_would_crop_the_hud_is_lowered_not_refused():
    result = check(treatment(effect="zoom_punch", payload={"scale": 120.0}),
                   moment())
    assert result.accepted is True
    assert result.lowered is True
    assert result.payload["scale"] <= safety_module.HUD_SAFE_SCALE
    assert result.safety_notes


def test_a_zoom_over_low_health_is_held_to_a_tighter_ceiling():
    result = check(treatment(effect="zoom_punch", payload={"scale": 110.0}),
                   moment(hud={"low_health": True}))
    assert result.payload["scale"] <= safety_module.LOW_HEALTH_SAFE_SCALE
    assert "health bar" in " ".join(result.safety_notes)


def test_a_freeze_on_a_death_screen_is_allowed():
    """The frame a viewer wants held is the one with the death message on it."""
    result = check(treatment(effect="freeze_frame", payload={}),
                   moment(kind="death_or_fail",
                          hud={"death_screen": True}))
    assert result.accepted is True


def test_a_callout_with_nothing_to_point_at_is_refused():
    result = check(
        treatment(effect="arrow_callout", payload={"target": ""}),
        moment(entities=[]))
    assert result.accepted is False
    assert result.reject_reason == "unknown_target"


def test_a_callout_with_a_target_still_says_the_position_is_unknown():
    result = check(
        treatment(effect="arrow_callout", payload={"target": "creeper"}),
        moment())
    assert result.accepted is True
    assert any("position" in note for note in result.safety_notes)


def test_a_low_confidence_moment_is_refused():
    result = check(treatment(), moment(confidence=0.2))
    assert result.accepted is False
    assert result.reject_reason == "low_confidence"


def test_a_transcript_moment_with_unclear_speech_is_refused():
    result = check(
        treatment(effect="label_tag", payload={"text": "hi"}),
        moment(source_type="transcript", transcript_confidence=0.3))
    assert result.accepted is False
    assert result.reject_reason == "low_transcript_confidence"


def test_a_vision_moment_with_nothing_named_cannot_change_the_picture():
    result = check(
        treatment(effect="zoom_punch", payload={"scale": 106.0}),
        moment(source_type="visual", entities=[], confidence=0.5))
    assert result.accepted is False
    assert result.reject_reason == "weak_visual_label"


def test_a_freeze_mid_fight_is_refused_and_a_freeze_on_a_death_is_not():
    mid_fight = check(
        treatment(effect="freeze_frame", payload={}),
        moment(kind="panic"))
    assert mid_fight.accepted is False
    assert mid_fight.reject_reason == "interrupts_action"

    on_a_death = check(
        treatment(effect="freeze_frame", payload={}),
        moment(kind="death_or_fail"))
    assert on_a_death.accepted is True


def test_screen_shake_with_something_to_aim_at_is_refused():
    config = VisualConfig(layer="high", allow_screen_shake=True).validated()
    result = check(
        treatment(effect="screen_shake", payload={"amplitude": 0.01}),
        moment(entities=["creeper"]), config)
    assert result.accepted is False
    assert result.reject_reason == "shake_during_combat"


def test_a_label_over_a_caption_is_refused():
    result = check(
        treatment(effect="label_tag", start=20.0, end=22.0,
                  payload={"text": "creeper"}),
        moment(),
        captions=[(19.5, 22.5, "oh god a creeper")])
    assert result.accepted is False
    assert result.reject_reason == "caption_overlap"


def test_the_same_effect_too_many_times_is_refused():
    config = VisualConfig(layer="balanced", max_per_effect=2).validated()
    result = check(treatment(), moment(), config,
                   effect_counts={"zoom_punch": 2})
    assert result.accepted is False
    assert result.reject_reason == "repeated_effect"


def test_an_opening_that_already_carries_enough_is_refused():
    result = check(
        treatment(effect="title_card", payload={"text": "hi"}),
        moment(kind="opening_hook", start=0.0, end=6.0),
        hook_polish=3)
    assert result.accepted is False
    assert result.reject_reason == "hook_already_polished"


def test_a_clip_too_short_for_an_effect_refuses_or_shortens():
    class Placement:
        sequence_end = 20.6

    result = check(
        treatment(effect="title_card", start=20.0, end=23.0,
                  payload={"text": "hi"}),
        moment(), placement=Placement())
    assert result.accepted is False
    assert result.reject_reason == "clip_too_short"


def test_an_effect_longer_than_the_ceiling_is_trimmed():
    config = VisualConfig(layer="balanced", max_effect_seconds=2.0).validated()
    result = check(
        treatment(effect="title_card", start=20.0, end=28.0,
                  payload={"text": "hi"}),
        moment(), config)
    assert result.accepted is True
    assert result.duration <= 2.0
    assert result.lowered is True


def test_a_second_effect_on_one_moment_is_refused():
    kept = treatment(treatment_id="t0", effect="freeze_frame",
                     start=20.0, end=21.0)
    kept.accepted = True
    result = check(treatment(effect="zoom_punch"), moment(), kept=[kept])
    assert result.accepted is False
    assert result.reject_reason == "too_close_to_another"
    assert "one gesture" in result.reject_detail


def test_markers_are_exempt_from_spacing():
    """A note costs a viewer nothing, so plenty of them is fine."""
    kept = treatment(treatment_id="t0", effect="zoom_punch",
                     start=20.0, end=21.0)
    kept.accepted = True
    result = check(
        treatment(effect="replay_marker", payload={"note": "x"}),
        moment(), kept=[kept])
    assert result.accepted is True


def test_every_check_that_runs_is_recorded():
    """A plan where the HUD check never ran and one where it passed look the
    same from the outside otherwise."""
    result = check(treatment(), moment())
    assert result.checks
    assert any(entry.name == "hides_hud" for entry in result.checks)
    for entry in result.checks:
        assert entry.outcome in ("pass", "lowered", "reject")


def test_every_refusal_uses_a_named_reason(episode, cut, style):
    plan = plan_for(episode, cut, style)
    assert plan.rejected
    for refused in plan.rejected:
        assert refused.reject_reason in REJECT_REASONS, refused.reject_reason
        assert refused.reject_detail, refused.effect


# ---------------------------------------------------------------------------
# Density
# ---------------------------------------------------------------------------

def test_the_effects_ceiling_is_enforced(episode, cut, style):
    plan = plan_for(episode, cut, style, max_effects_per_minute=0.8,
                    min_spacing=0.0)
    # 80 seconds at 0.8 a minute is one effect.
    counted = [t for t in plan.accepted if t.counts_against_density]
    assert len(counted) <= 1
    assert plan.by_reject_reason().get("density_limit")


def test_the_callout_ceiling_is_separate(episode, cut, style):
    plan = plan_for(episode, cut, style, max_effects_per_minute=30.0,
                    max_callouts_per_minute=0.0, min_spacing=0.0)
    assert not [t for t in plan.accepted if t.family == "callout"]


def test_what_survives_the_ceiling_is_what_scored_best(episode, cut, style):
    plan = plan_for(episode, cut, style, max_effects_per_minute=0.8,
                    min_spacing=0.0)
    kept = [t for t in plan.accepted if t.counts_against_density]
    if not kept:
        pytest.skip("nothing survived, so there is no ranking to check")
    for refused in plan.rejected:
        if refused.reject_reason == "density_limit":
            assert refused.priority <= kept[0].priority


def test_markers_do_not_eat_the_effect_budget(episode, cut, style):
    plan = plan_for(episode, cut, style)
    for treat in plan.accepted:
        if treat.effect in MARKER_EFFECTS:
            assert treat.counts_against_density is False


def test_a_short_cut_still_allows_one_effect(episode, style):
    plan = plan_for(episode, make_cut(2), style,
                    max_effects_per_minute=0.1)
    assert len(plan.accepted) >= 1
    assert any("rounds down to none" in note for note in plan.safety_notes)


# ---------------------------------------------------------------------------
# The plan as a whole
# ---------------------------------------------------------------------------

def test_a_disabled_layer_plans_nothing(episode, cut, style):
    config = VisualConfig(layer="off").validated()
    plan = plan_module.build_visual_plan(episode, cut, style, config)
    assert plan.moments == []
    assert plan.treatments == []
    assert any("visual layer is off" in w for w in plan.warnings)


def test_a_moment_the_style_never_offers_for_is_still_recorded(episode, cut):
    """Otherwise "why did this death get nothing" is unanswerable."""
    quiet = style_presets.get("minimal_clean")
    plan = plan_for(episode, cut, quiet, "balanced")
    assert plan.moments
    assert plan.rejected
    reasons = plan.by_reject_reason()
    assert reasons.get("style_forbids") or reasons.get("layer_forbids") \
        or reasons.get("no_evidence")


def test_untreated_moments_are_reported_with_a_reason(episode, cut, style):
    plan = plan_for(episode, cut, style, max_effects_per_minute=0.8)
    untreated = plan.untreated_moments()
    assert untreated
    comparison = build_comparison(plan)
    assert len(comparison.untreated) == len(untreated)
    for entry in comparison.untreated:
        assert entry["why"]


def test_the_plan_records_which_cut_it_was_built_against(episode, cut, style):
    config = treatments_module.visual_defaults(style, "balanced")
    plan = plan_module.build_visual_plan(
        episode, cut, style, config, base="retention")
    assert plan.base == "retention"


def test_high_says_it_is_high(episode, cut, style):
    plan = plan_for(episode, cut, style, "high")
    assert any("high" in warning for warning in plan.warnings)


# ---------------------------------------------------------------------------
# Part 6 -- the composer
# ---------------------------------------------------------------------------

def test_the_composer_produces_one_segment_per_clip(episode, cut, style):
    plan = plan_for(episode, cut, style)
    config = treatments_module.visual_defaults(style, "balanced")
    final = compose_module.compose_final_edit(cut, plan, config)
    assert len(final.segments) == len(cut.placements)


def test_treatments_land_on_the_clip_that_is_playing(episode, cut, style):
    plan = plan_for(episode, cut, style)
    config = treatments_module.visual_defaults(style, "balanced")
    final = compose_module.compose_final_edit(cut, plan, config)

    by_id = {t.treatment_id: t for t in plan.accepted}
    for segment in final.segments:
        for treatment_id in segment.treatments:
            treat = by_id[treatment_id]
            assert segment.start <= treat.start < segment.end


def test_the_composer_is_off_when_the_mode_is_off(episode, cut, style):
    plan = plan_for(episode, cut, style)
    config = VisualConfig(layer="balanced", mode="off").validated()
    final = compose_module.compose_final_edit(cut, plan, config)
    assert final.segments == []
    assert any("composer is off" in w for w in final.warnings)


def test_plan_only_builds_neither_output(episode, cut, style):
    plan = plan_for(episode, cut, style)
    config = VisualConfig(layer="balanced", mode="plan_only").validated()
    final = compose_module.compose_final_edit(cut, plan, config)
    assert final.execution.premiere is None
    assert final.execution.preview is None


def test_hybrid_builds_both(episode, cut, style):
    plan = plan_for(episode, cut, style)
    config = replace(treatments_module.visual_defaults(style, "balanced"),
                     mode="hybrid").validated()
    final = compose_module.compose_final_edit(cut, plan, config)
    assert final.execution.premiere is not None
    assert final.execution.preview is not None


def test_the_final_plan_never_claims_to_have_been_executed(episode, cut,
                                                           style):
    plan = plan_for(episode, cut, style)
    config = replace(treatments_module.visual_defaults(style, "balanced"),
                     mode="hybrid").validated()
    final = compose_module.compose_final_edit(cut, plan, config)
    assert final.execution.executes_anything is False
    assert final.to_dict()["execution"]["executed"] is False
    assert final.stats()["execution_executed"] is False


def test_a_final_plan_survives_a_round_trip(episode, cut, style):
    plan = plan_for(episode, cut, style)
    config = replace(treatments_module.visual_defaults(style, "balanced"),
                     mode="hybrid").validated()
    final = compose_module.compose_final_edit(cut, plan, config)
    restored = FinalEditPlan.from_dict(final.to_dict())
    assert len(restored.segments) == len(final.segments)
    assert restored.stats()["visual_treatments"] == \
        final.stats()["visual_treatments"]


def test_the_composer_carries_the_caption_and_audio_counts(episode, cut,
                                                           style):
    class FakeCaption:
        mode = "key_moments"
        accepted = []
        burned_in = False
        sidecar_path = "/x.srt"

        def stats(self):
            return {"accepted": 3, "rejected": 9, "captions_per_minute": 1.1}

    plan = plan_for(episode, cut, style)
    config = treatments_module.visual_defaults(style, "balanced")
    final = compose_module.compose_final_edit(
        cut, plan, config, caption_plan=FakeCaption())
    assert final.caption_summary["accepted"] == 3
    assert final.caption_summary["burned_in"] is False


# ---------------------------------------------------------------------------
# Part 7 -- FFmpeg preview honesty
# ---------------------------------------------------------------------------

def test_every_effect_has_a_preview_verdict():
    for effect in EFFECT_TYPES:
        support, reason = preview_module.support_for(effect)
        assert support in ("burn_in", "sidecar", "none"), effect
        if support != "burn_in":
            assert reason, effect


def test_the_preview_plan_never_claims_anything_was_burned_in(episode, cut,
                                                              style):
    plan = plan_for(episode, cut, style)
    preview = preview_module.build_preview_plan(plan)
    assert preview.burned_in is False
    assert preview.to_dict()["burned_in"] is False
    assert preview.stats()["burned_in"] is False


def test_a_preview_plan_read_back_still_claims_nothing():
    """A document claiming a burned-in effect is one this package could not
    have written."""
    from editing.visuals.execution import FFmpegVisualPreviewPlan

    restored = FFmpegVisualPreviewPlan.from_dict(
        {"name": "x", "burned_in": True, "items": []})
    assert restored.burned_in is False


def test_burnable_effects_record_the_filter_they_would_need(episode, cut):
    documentary = style_presets.get("documentary_story")
    plan = plan_for(episode, cut, documentary, "high")
    preview = preview_module.build_preview_plan(plan)
    for item in preview.burnable:
        assert item.filter_fragment, item.effect


def test_effects_ffmpeg_cannot_show_say_so():
    support, reason = preview_module.support_for("arrow_callout")
    assert support == "none"
    assert "arrow" in reason


def test_the_marker_file_says_nothing_is_in_the_proxy(episode, cut, style):
    plan = plan_for(episode, cut, style)
    preview = preview_module.build_preview_plan(plan)
    text = preview_module.render_markers(plan, preview)
    assert "carries no visual treatment" in text
    if plan.accepted:
        assert "no —" in text or "no -" in text


def test_no_marker_file_is_written_when_nothing_was_planned(episode, tmp_path,
                                                            style):
    config = VisualConfig(layer="off").validated()
    plan = plan_module.build_visual_plan(
        episode, make_cut(2), style, config)
    preview = preview_module.build_preview_plan(plan)
    assert preview_module.markers_beside(
        plan, preview, str(tmp_path / "v.mp4")) is None


def test_the_marker_file_lands_beside_the_video(episode, cut, style,
                                                tmp_path):
    plan = plan_for(episode, cut, style)
    if not plan.accepted:
        pytest.skip("nothing was planned, so there is no marker file")
    preview = preview_module.build_preview_plan(plan)
    video = tmp_path / "render.mp4"
    video.write_bytes(b"video")
    written = preview_module.markers_beside(plan, preview, str(video))
    assert written is not None
    assert written.parent == tmp_path
    assert written.suffix == ".md"


# ---------------------------------------------------------------------------
# Part 8 -- the Premiere operation plan
# ---------------------------------------------------------------------------

def test_the_premiere_plan_validates_against_the_real_catalog(episode, cut,
                                                              style):
    """Offline, against the catalog, with no host application involved."""
    plan = plan_for(episode, cut, style)
    if not plan.accepted:
        pytest.skip("nothing was planned, so there is nothing to validate")
    ops = premiere_module.build_premiere_plan(plan)
    premiere_module.validate_offline(ops)
    assert ops.dry_run_passed is True, ops.dry_run_error


def test_every_operation_is_a_catalog_operation(episode, cut, style):
    from premiere.catalog import OPS

    plan = plan_for(episode, cut, style, "high")
    ops = premiere_module.build_premiere_plan(plan)
    for entry in ops.operations:
        assert entry.name in OPS, entry.name


def test_an_effect_the_catalog_cannot_express_is_listed_not_dropped():
    plan = VisualLayerPlan(name="x", layer="high", sequence_name="Seq")
    shake = treatment(effect="screen_shake", payload={"amplitude": 0.01})
    shake.accepted = True
    plan.treatments = [shake]

    ops = premiere_module.build_premiere_plan(plan)
    assert ops.operations == []
    assert len(ops.unsupported) == 1
    assert ops.unsupported[0].effect == "screen_shake"
    assert ops.unsupported[0].reason
    assert ops.unsupported[0].alternative


def test_can_express_agrees_with_what_the_builder_does():
    for effect in EFFECT_TYPES:
        expressible = premiere_module.can_express(effect)
        treat = treatment(effect=effect, payload={"text": "x", "target": "y",
                                                  "note": "z", "scale": 106.0})
        treat.accepted = True
        plan = VisualLayerPlan(treatments=[treat])
        ops = premiere_module.build_premiere_plan(plan)
        if expressible:
            assert ops.operations or ops.unsupported, effect
        else:
            assert ops.unsupported, effect


def test_a_callout_operation_says_its_position_is_a_guess():
    plan = VisualLayerPlan(name="x", sequence_name="Seq")
    callout = treatment(effect="arrow_callout",
                        payload={"target": "creeper", "shape": "arrow"})
    callout.accepted = True
    plan.treatments = [callout]

    ops = premiere_module.build_premiere_plan(plan)
    assert ops.operations
    assert "POSITION IS A GUESS" in ops.operations[0].op["note"]
    assert any("by hand" in warning for warning in ops.warnings)


def test_an_empty_plan_does_not_validate_and_says_why():
    ops = premiere_module.build_premiere_plan(VisualLayerPlan())
    premiere_module.validate_offline(ops)
    assert ops.dry_run_passed is False
    assert ops.dry_run_error["code"] == "empty_plan"


def test_the_premiere_plan_never_claims_to_have_run(episode, cut, style):
    plan = plan_for(episode, cut, style)
    ops = premiere_module.build_premiere_plan(plan)
    assert any("Nothing here has run" in w for w in ops.warnings)
    assert ops.as_edit_plan()["dry_run"] is True


def test_a_premiere_plan_survives_a_round_trip(episode, cut, style):
    plan = plan_for(episode, cut, style)
    ops = premiere_module.build_premiere_plan(plan)
    restored = PremiereVisualOperationPlan.from_dict(ops.to_dict())
    assert restored.operation_count == ops.operation_count
    assert restored.by_op() == ops.by_op()


def test_visual_operations_land_above_the_cut(episode, cut, style):
    plan = plan_for(episode, cut, style, "high")
    ops = premiere_module.build_premiere_plan(plan)
    for entry in ops.operations:
        if "track" in entry.op:
            assert entry.op["track"] == premiere_module.VISUAL_TRACK


# ---------------------------------------------------------------------------
# Reports and storage
# ---------------------------------------------------------------------------

def test_the_report_answers_six_questions(episode, cut, style):
    plan = plan_for(episode, cut, style)
    built = report_module.build_report(plan)
    assert len(built.answers) == len(report_module.QUESTIONS)
    for entry in built.answers:
        assert entry["question"]
        assert entry["answer"]


def test_the_report_shows_what_got_nothing(episode, cut, style):
    plan = plan_for(episode, cut, style)
    text = report_module.render(plan)
    assert "MOMENTS THAT GOT NOTHING" in text
    assert "WHAT WAS REFUSED" in text
    assert "WHAT MIGHT BE OVERDONE" in text


def test_no_report_claims_the_edit_is_better(episode, cut, style):
    plan = plan_for(episode, cut, style, "high")
    config = replace(treatments_module.visual_defaults(style, "high"),
                     mode="hybrid").validated()
    final = compose_module.compose_final_edit(cut, plan, config)
    for text in (report_module.render(plan),
                 report_module.render_final(final)):
        lowered = text.lower()
        assert "guaranteed" not in lowered
        assert "retention improved" not in lowered
        assert "has been drawn, rendered or executed" in lowered


def test_the_final_report_says_nothing_was_executed(episode, cut, style):
    plan = plan_for(episode, cut, style)
    config = replace(treatments_module.visual_defaults(style, "balanced"),
                     mode="hybrid").validated()
    final = compose_module.compose_final_edit(cut, plan, config)
    text = report_module.render_final(final)
    assert "executed    : no" in text


def test_an_overdone_plan_says_so():
    plan = VisualLayerPlan(name="x", layer="high", cut_duration=60.0)
    for index in range(8):
        treat = treatment(treatment_id=f"t{index}", effect="zoom_punch",
                          start=index * 2.0, end=index * 2.0 + 1.0)
        treat.accepted = True
        plan.treatments.append(treat)

    risks = report_module.overdone_risks(plan)
    assert any("a minute" in line for line in risks)
    assert any("tic" in line for line in risks)


def test_plans_round_trip_through_the_store(config, episode, cut, style):
    plan = plan_for(episode, cut, style)
    visuals_store.save_plan(config, plan)
    assert len(visuals_store.load_plan(config).accepted) == len(plan.accepted)

    settings = replace(treatments_module.visual_defaults(style, "balanced"),
                       mode="hybrid").validated()
    final = compose_module.compose_final_edit(cut, plan, settings)
    visuals_store.save_final(config, final)
    assert len(visuals_store.load_final(config).segments) == \
        len(final.segments)


def test_a_missing_plan_is_a_result_not_a_crash(config):
    assert visuals_store.plan_or_none(config) is None
    assert visuals_store.final_or_none(config) is None
    assert visuals_store.premiere_or_none(config) is None


def test_the_comparison_counts_and_never_scores(episode, cut, style):
    plan = plan_for(episode, cut, style)
    settings = treatments_module.visual_defaults(style, "balanced")
    final = compose_module.compose_final_edit(cut, plan, settings)
    comparison = build_comparison(plan, final)

    assert comparison.segments == len(cut.placements)
    assert comparison.treatments == len(plan.accepted)
    text = report_module.render_comparison(comparison).lower()
    assert "score" not in text
    assert "better" not in text


def test_the_report_says_what_a_proxy_could_not_show():
    """"What is placeholder-only" is incomplete without "and what a proxy
    could not show you either".

    Built rather than planned: whether a given fixture happens to earn a
    callout is not what this is about, and a conditional skip would leave the
    assertion untested on the day it mattered.
    """
    plan = VisualLayerPlan(name="x", layer="high", cut_duration=120.0)
    arrow = treatment(effect="arrow_callout",
                      payload={"target": "creeper", "shape": "arrow"})
    arrow.accepted = True
    arrow.target_output = "premiere_plan"
    plan.treatments = [arrow]

    preview = preview_module.build_preview_plan(plan)
    assert preview.invisible, "an arrow is the canonical FFmpeg-can't case"

    built = report_module.build_report(plan, preview=preview)
    answer = built.answers[3]["answer"]
    assert "FFmpeg" in answer


def test_the_report_works_without_a_preview_plan(episode, cut, style):
    plan = plan_for(episode, cut, style)
    built = report_module.build_report(plan)
    assert len(built.answers) == len(report_module.QUESTIONS)


def test_the_documented_counts_match_the_code():
    """The docs quote four numbers. Two of them were wrong when written.

    Pinned here rather than trusted, because a prose count is the one kind of
    claim nothing else in this system checks -- and "thirty-four effects" read
    exactly as plausibly as "thirty-six".
    """
    import inspect

    from editing.visuals import safety as safety_source

    assert len(EFFECT_TYPES) == 36
    assert len(VISUAL_MOMENT_TYPES) == 20

    dispatched = [
        line.strip().rstrip(",")
        for line in inspect.getsource(safety_source.check_all).splitlines()
        if line.strip().startswith("_") and line.strip().endswith(",")
    ]
    assert len(dispatched) == 13

    readme = pathlib.Path(__file__).resolve().parents[2] / "editing" / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "thirty-six effects" in text
    assert "thirteen safety checks" in text
    assert "Thirty-six treatments" in text

"""Style presets and the layered edit: selection, ceilings, and the guards.

Three things carry the weight here.

**Ceilings only ever subtract.** Every style test checks the same property from
a different angle: a preset can make the edit quieter than the evidence
justifies, never busier. If a compiled plan ever exceeds its own style's
density, the whole premise of the session is gone -- so that is asserted
directly, for every preset, on the same input.

**Text spam is the failure mode.** A system that can put a transcript on screen
will, so the caption tests assert on what is *refused*: lines that score too
low, lines with nowhere safe to sit, lines while a menu is open, and two
captions trying to share the screen.

**Additive only.** This layer's guarantee is stronger than the rough cut's or
the critic's: it cannot trim, retime, move or remove a clip. That is enforced
by an operation allowlist, and pinned by a test that compares the set of
operations the code can emit against the set it is permitted to emit.

Nothing here needs FFmpeg, a GPU, a model server or Premiere.
"""
from __future__ import annotations

import json

import pytest

from editing.align import build_timeline
from editing.critic.schema import RevisionRecommendation, RevisionSet
from editing.errors import EditingError
from editing.recommend.schema import (
    EditRecommendation, Evidence, RecommendationSet,
)
from editing.roughcut.schema import ClipPlacement, RoughCutPlan, SequenceMarker
from editing.schema import (
    AudioEvent, MediaAsset, TimeRange, Transcript, TranscriptEntry, UIState,
    VisualEvent,
)
from editing.style import audio as audio_layer
from editing.style import captions as caption_layer
from editing.style import compile as compile_module
from editing.style import execute as style_execute
from editing.style import presets as style_presets
from editing.style import report as style_report
from editing.style.compile import CompileOptions, compile_layers
from editing.style.presets import StylePreset
from editing.style.schema import LAYERS, LayerItem, LayeredEditPlan

ASSET = MediaAsset(
    asset_id="a_test", path="/footage/ep12.mp4", filename="ep12.mp4",
    duration=400.0,
)
SEQUENCE = "Nova Rough Cut"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def visual(start, end, **kw):
    ui = kw.pop("ui", None)
    event = VisualEvent(
        event_id=f"e_{start}", source_file=ASSET.path, asset_id=ASSET.asset_id,
        start=start, end=end, confidence=0.85,
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


def audio_event(start, end, kind, *, confidence=0.8, detection="measured"):
    return AudioEvent(
        event_id=f"au_{start}_{kind}", source_file=ASSET.path,
        asset_id=ASSET.asset_id, start=start, end=end, type=kind,
        confidence=confidence, detection=detection, loudness_db=-8.0,
        baseline_db=-24.0,
    )


def placement(pid, source_in, source_out, sequence_start, index=0, **kw):
    return ClipPlacement(
        placement_id=pid, asset_id=ASSET.asset_id, source_file=ASSET.path,
        source_in=source_in, source_out=source_out,
        sequence_start=sequence_start, index=index,
        segment_ids=kw.pop("segments", [f"s_{index}"]), **kw,
    )


def timeline_of(events, *, lines=(), audio_events=()):
    transcript = Transcript(
        asset_id=ASSET.asset_id, source="srt",
        entries=[TranscriptEntry(*line) for line in lines],
    ) if lines else None
    return build_timeline(
        [ASSET], {ASSET.asset_id: list(events)},
        {ASSET.asset_id: transcript} if transcript else {},
        audio_by_asset={ASSET.asset_id: list(audio_events)},
    )


def one_clip_cut(*, duration=200.0, markers=(), ops=()):
    """A cut that is one long clip, so source time == sequence time."""
    plan = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", 0.0, duration, 0.0, 0)],
        markers=list(markers),
        on_scratch=True,
    )
    plan.ops = list(ops) or [
        {"op": "project.import", "paths": [ASSET.path], "bin": "b"},
        {"op": "sequence.create", "name": SEQUENCE, "from_asset": ASSET.path},
        {"op": "sequence.activate", "name": SEQUENCE},
        {"op": "clip.append", "asset": ASSET.path, "track": "V1",
         "in": 0.0, "out": duration},
    ]
    return plan


@pytest.fixture
def rich_timeline():
    """A cut with a bit of everything: setup, payoff, danger, a dimension change."""
    return timeline_of(
        [
            visual(0, 20, environment="forest", actions=("travelling",),
                   importance="setup"),
            visual(20, 40, importance="boring", actions=("travelling",)),
            visual(40, 60, importance="tension", threats=("creeper",)),
            visual(60, 80, actions=("looting",), importance="payoff",
                   entities=("diamond",)),
            visual(80, 100, actions=("fighting",), importance="danger",
                   threats=("creeper",)),
            visual(100, 130, environment="nether", actions=("travelling",),
                   importance="reveal"),
            visual(130, 160, environment="nether", actions=("fighting",),
                   importance="danger", threats=("piglin",)),
            visual(160, 200, environment="base", actions=("building",),
                   importance="setup", ui=UIState(inventory_open=True)),
        ],
        lines=[
            (2, 8, "okay so the plan is to find some diamonds today"),
            (25, 30, "just walking for a bit here nothing much"),
            (45, 49, "wait what was that behind me"),
            (62, 68, "oh my god diamonds actual diamonds right there"),
            (84, 88, "creeper creeper get away from me"),
            (104, 110, "we need to get to the nether fortress next"),
            (135, 140, "i died that is a death back to spawn"),
            (170, 176, "because this base needs a proper storage room"),
        ],
        audio_events=[
            audio_event(20, 26, "silence", confidence=0.9),
            audio_event(46, 47, "sudden_reaction"),
            audio_event(63, 64, "sudden_reaction"),
            audio_event(85, 86, "possible_scream", confidence=0.4,
                        detection="heuristic"),
            audio_event(136, 138, "possible_laughter", confidence=0.42,
                        detection="heuristic"),
        ],
    )


@pytest.fixture
def cut():
    return one_clip_cut()


@pytest.fixture
def recommendations():
    return RecommendationSet(recommendations=[
        EditRecommendation(
            recommendation_id="r_punch_1", asset_id=ASSET.asset_id,
            source_file=ASSET.path, start=60.0, end=64.0, category="punch_in",
            priority=0.9, intensity="high",
            evidence=Evidence(visual_event_ids=["e_60"]),
        ),
        EditRecommendation(
            recommendation_id="r_push_1", asset_id=ASSET.asset_id,
            source_file=ASSET.path, start=100.0, end=106.0,
            category="slow_push_in", priority=0.8, intensity="medium",
            evidence=Evidence(visual_event_ids=["e_100"]),
        ),
        EditRecommendation(
            recommendation_id="r_text_1", asset_id=ASSET.asset_id,
            source_file=ASSET.path, start=62.0, end=68.0,
            category="text_overlay", priority=0.8,
            evidence=Evidence(transcript_quotes=["oh my god diamonds"]),
        ),
    ])


def compiled(timeline, cut, style_name, **kw):
    kw.setdefault("roughcut_executed", True)
    return compile_layers(
        timeline, cut, style=style_presets.get(style_name), **kw
    )


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
# Part 1 -- style presets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", style_presets.names())
def test_every_shipped_preset_validates_clean(name):
    preset = style_presets.get(name)
    assert preset.problems() == []
    assert preset.is_valid


def test_the_four_presets_are_a_spectrum():
    """Each style should genuinely differ, or there is no point having four."""
    rates = {
        name: style_presets.get(name).max_edits_per_minute
        for name in style_presets.names()
    }
    assert rates["minimal_clean"] < rates["cinematic_minecraft"]
    assert rates["cinematic_minecraft"] < rates["documentary_story"]
    assert rates["documentary_story"] < rates["fast_funny"]


def test_the_default_is_the_most_restrained_style():
    """The safest style is the one you get without asking."""
    default = style_presets.get()
    assert default.name == "minimal_clean"
    assert not default.zooms_allowed
    assert not default.allow_real_text


def test_minimal_clean_cannot_scale_the_picture_at_all():
    preset = style_presets.get("minimal_clean")
    assert preset.max_zoom_scale == 100.0
    assert preset.zooms_allowed is False
    assert "punch_in" in preset.forbidden_kinds


def test_an_unknown_preset_is_an_error_not_a_silent_fallback():
    """Getting a style nobody chose would produce an edit nobody chose."""
    with pytest.raises(EditingError) as caught:
        style_presets.get("fast_funnny")
    assert "fast_funny" in str(caught.value.hint)


def test_a_preset_can_be_overridden_inline():
    preset = style_presets.get("fast_funny", max_edits_per_minute=2.0)
    assert preset.max_edits_per_minute == 2.0
    # The original is untouched.
    assert style_presets.get("fast_funny").max_edits_per_minute == 7.0


@pytest.mark.parametrize("field,value,fragment", [
    ("max_edits_per_minute", -1.0, "negative"),
    ("max_edits_per_minute", 120.0, "strobe"),
    ("max_caption_words", 0, "at least 1"),
    ("max_caption_words", 40, "skipped"),
    ("max_zoom_scale", 90.0, "below 100"),
    ("max_zoom_scale", 200.0, "softens"),
    ("pacing", "vibey", "not one of"),
    ("min_confidence", 4.0, "outside 0..1"),
])
def test_validation_names_the_field_and_the_problem(field, value, fragment):
    preset = StylePreset(name="custom", **{field: value})
    problems = " ".join(preset.problems())
    assert fragment in problems


def test_a_push_may_never_out_scale_a_punch():
    preset = StylePreset(name="custom", max_zoom_scale=105.0,
                         max_push_scale=120.0)
    assert any("never end up stronger" in p for p in preset.problems())
    assert preset.validated().max_push_scale <= 105.0


def test_a_style_allowing_captions_with_no_safe_zone_is_a_problem():
    preset = StylePreset(name="custom", text_zones=("center",),
                         max_captions_per_minute=2.0)
    assert any("every preferred zone is unsafe" in p for p in preset.problems())


def test_validation_clamps_rather_than_raising():
    """One bad number should degrade to a working style, not stop a run."""
    preset = StylePreset(
        name="Custom Style!", pacing="chaotic", max_edits_per_minute=-5.0,
        max_caption_words=99, max_zoom_scale=400.0,
        text_zones=("nowhere", "upper_left"),
    ).validated()

    assert preset.name == "custom_style"
    assert preset.pacing == "measured"
    assert preset.max_edits_per_minute == 0.0
    assert preset.max_caption_words == 14
    assert preset.max_zoom_scale == 130.0
    assert preset.text_zones == ("upper_left",)


def test_a_preset_round_trips_through_json():
    original = style_presets.get("documentary_story")
    restored = StylePreset.from_dict(
        json.loads(json.dumps(original.to_dict()))
    )
    assert restored.name == original.name
    assert restored.forbidden_kinds == original.forbidden_kinds
    assert restored.limited_kinds == original.limited_kinds
    assert restored.text_zones == original.text_zones


def test_marker_names_cover_every_kind():
    """A new kind must never silently lose its marker name."""
    preset = style_presets.get("cinematic_minecraft")
    for kind in style_presets.LAYER_KINDS:
        assert preset.marker_name(kind)


def test_a_marker_prefix_reaches_every_name():
    preset = style_presets.get("fast_funny", marker_prefix="NOVA ")
    assert preset.marker_name("music_start").startswith("NOVA ")


def test_the_centre_of_frame_is_never_a_caption_zone():
    """The crosshair lives there and the hotbar is just below it."""
    preset = style_presets.get(
        "fast_funny", text_zones=("center", "upper_left")
    )
    assert preset.zone_for("reaction_caption") == "upper_left"
    # A card is meant to cover the frame, so it may use the centre.
    assert preset.zone_for("title_card") == "center"


def test_a_blocked_zone_is_skipped_and_running_out_returns_none():
    preset = style_presets.get("fast_funny")
    assert preset.zone_for("key_phrase", blocked=["upper_center"]) == "upper_left"
    assert preset.zone_for("key_phrase", blocked=preset.text_zones) is None


# ---------------------------------------------------------------------------
# Part 2 -- the layer schema
# ---------------------------------------------------------------------------

def test_a_layer_item_round_trips_through_json():
    item = LayerItem(
        item_id="li_1", layer="caption", kind="reaction_caption",
        recommendation_id="r_1", placement_id="p_1", start=4.0, end=6.2,
        source_start=4.0, source_end=6.2, asset_id=ASSET.asset_id,
        style="fast_funny", reason="the audio spikes here", effect="impact",
        priority=0.72, risks=["text_spam"], status="planned",
        premiere_ops=[{"op": "marker.add", "time": 4.0}],
        payload={"text": "oh my god"},
    )
    restored = LayerItem.from_dict(json.loads(json.dumps(item.to_dict())))
    assert restored == item


def test_the_layer_schema_coerces_nonsense():
    restored = LayerItem.from_dict({
        "item_id": "", "layer": "sparkle", "kind": "explode",
        "status": "applied", "effect": "vibes", "risks": ["made_up"],
        "priority": "loads", "start": 9.0, "end": 2.0,
    })
    assert restored.item_id.startswith("li_")
    assert restored.layer == "marker"
    assert restored.kind == "structure_marker"
    assert restored.status == "planned"
    assert restored.effect == "unknown"
    assert restored.risks == []
    assert restored.end >= restored.start


def test_deferring_keeps_the_item_and_drops_the_operations():
    item = LayerItem(item_id="li_1", kind="punch_in",
                     premiere_ops=[{"op": "animate"}])
    item.defer("no room in this style", risk="over_editing")
    assert item.status == "deferred"
    assert item.premiere_ops == []
    assert "over_editing" in item.risks
    assert item.status_reason


def test_a_marker_only_item_is_not_counted_as_an_active_edit():
    """A note costs the viewer nothing, so it must not spend the budget."""
    drawn = LayerItem(item_id="li_1", kind="reaction_caption",
                      premiere_ops=[{"op": "text.create"}])
    noted = LayerItem(item_id="li_2", kind="reaction_caption",
                      premiere_ops=[{"op": "marker.add"}])
    assert drawn.is_active and not drawn.is_marker_only
    assert noted.is_marker_only and not noted.is_active


def test_a_plan_round_trips_and_keeps_its_layers(rich_timeline, cut,
                                                 recommendations):
    plan = compiled(rich_timeline, cut, "documentary_story",
                    recommendations=recommendations)
    restored = LayeredEditPlan.from_dict(
        json.loads(json.dumps(plan.to_dict()))
    )
    assert len(restored) == len(plan)
    assert restored.ops == plan.ops
    assert restored.style == plan.style
    for name in LAYERS:
        assert len(restored.layer(name)) == len(plan.layer(name))


# ---------------------------------------------------------------------------
# Part 3 -- captions
# ---------------------------------------------------------------------------

def test_captions_come_only_from_lines_that_were_actually_said(rich_timeline,
                                                               cut):
    plan = compiled(rich_timeline, cut, "fast_funny")
    said = {
        entry.text.lower()
        for segment in rich_timeline.segments
        for entry in segment.speech_entries
    }
    for item in plan.layer("caption"):
        if item.kind == "callout_label":
            continue
        full = item.payload.get("full_line", "").lower()
        assert any(full in line or line in full for line in said)


def test_a_dull_line_over_dull_footage_never_becomes_a_caption(rich_timeline,
                                                               cut):
    """"just walking for a bit here" is the line this rule exists for."""
    plan = compiled(rich_timeline, cut, "fast_funny")
    for item in plan.items:
        assert "just walking" not in str(item.payload.get("text", ""))


def test_a_style_can_refuse_text_entirely(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "fast_funny",
                    options=CompileOptions(markers_only=True))
    assert not any(op.get("op") == "text.create" for op in plan.ops)


def test_minimal_clean_never_draws_text(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "minimal_clean")
    assert not any(op.get("op") == "text.create" for op in plan.ops)
    for item in plan.layer("caption"):
        if item.status == "planned":
            assert item.is_marker_only


def test_a_long_line_is_condensed_to_its_strongest_phrase():
    text = "okay so anyway I think that was probably a creeper behind us"
    condensed, was = caption_layer.condense(text, 4)
    assert was is True
    assert "creeper" in condensed
    assert condensed.startswith("...")


def test_a_short_line_is_left_exactly_as_it_was_said():
    condensed, was = caption_layer.condense("oh my god diamonds", 7)
    assert was is False
    assert condensed == "oh my god diamonds"


def test_the_keyword_lists_are_disjoint():
    """One word in two lists scores an utterance twice.

    This is not hypothetical: "run" was in both the reaction and danger lists,
    and "creeper creeper run run run" reached a perfect 1.0 and outranked a
    chapter card because of it.
    """
    reaction = set(caption_layer.REACTION_WORDS)
    danger = set(caption_layer.DANGER_WORDS)
    explain = set(caption_layer.EXPLANATORY_WORDS)
    assert reaction & danger == set()
    assert reaction & explain == set()
    assert danger & explain == set()


def test_no_caption_exceeds_the_styles_word_limit(rich_timeline, cut):
    for name in style_presets.names():
        style = style_presets.get(name)
        plan = compiled(rich_timeline, cut, name)
        for item in plan.layer("caption"):
            words = item.payload.get("words")
            if words:
                assert words <= style.max_caption_words, name


def test_captions_are_never_placed_over_a_full_screen_menu(rich_timeline, cut):
    """The base segment at 160s has an inventory open."""
    plan = compiled(rich_timeline, cut, "documentary_story")
    for item in plan.items:
        if not item.is_text or not item.is_convertible:
            continue
        if 160.0 <= item.start <= 200.0:
            assert item.is_marker_only, (
                f"{item.kind} at {item.start} was drawn over an open menu"
            )


def test_a_caption_with_nowhere_safe_to_go_becomes_a_marker(rich_timeline, cut):
    style = style_presets.get("fast_funny", text_zones=("center",))
    plan = compile_layers(rich_timeline, cut, style=style)
    for item in plan.layer("caption"):
        if item.status == "planned":
            assert item.is_marker_only
            assert "placeholder_only" in item.risks


def test_two_captions_never_share_the_screen(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "fast_funny")
    drawn = sorted(
        (item for item in plan.planned()
         if item.is_caption and not item.is_marker_only),
        key=lambda item: item.start,
    )
    for earlier, later in zip(drawn, drawn[1:]):
        assert later.start >= earlier.end, (
            f"{earlier.item_id} is still on screen when {later.item_id} starts"
        )


def test_a_caption_never_outlives_its_clip():
    timeline = timeline_of(
        [visual(0, 6, importance="payoff", entities=("diamond",))],
        lines=[(4.0, 5.0, "oh my god diamonds")],
    )
    cut = one_clip_cut(duration=6.0)
    plan = compiled(timeline, cut, "fast_funny")
    for item in plan.layer("caption"):
        assert item.end <= cut.total_duration + 0.01


def test_a_callout_label_names_the_thing_and_is_never_drawn(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "fast_funny")
    labels = plan.of_kind("callout_label")
    assert labels
    for item in labels:
        assert item.payload.get("label")
        if item.premiere_ops:
            assert all(op["op"] == "marker.add" for op in item.premiere_ops)
            assert item.payload["label"] in item.premiere_ops[0]["comment"]


def test_a_marker_standing_in_for_text_still_carries_the_words(rich_timeline,
                                                               cut):
    """Losing the line would make the marker useless."""
    plan = compiled(rich_timeline, cut, "minimal_clean")
    captions = [
        item for item in plan.layer("caption")
        if item.status == "planned" and item.payload.get("text")
    ]
    assert captions
    for item in captions:
        assert item.payload["text"] in item.premiere_ops[0]["comment"]


# ---------------------------------------------------------------------------
# Part 4 -- visual emphasis
# ---------------------------------------------------------------------------

def test_a_zoom_never_exceeds_the_styles_ceiling(rich_timeline, cut,
                                                 recommendations):
    for name in style_presets.names():
        style = style_presets.get(name)
        plan = compiled(rich_timeline, cut, name,
                        recommendations=recommendations)
        for op in plan.ops:
            if op.get("op") != "animate":
                continue
            assert op["to"] <= style.max_zoom_scale + 1e-6, name


def test_a_style_with_no_zooms_emits_none(rich_timeline, cut, recommendations):
    plan = compiled(rich_timeline, cut, "minimal_clean",
                    recommendations=recommendations)
    assert not any(op.get("op") == "animate" for op in plan.ops)


def test_a_protected_hold_is_not_zoomed(rich_timeline, recommendations):
    cut = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", 0.0, 200.0, 0.0, 0, protected=True)],
        on_scratch=True,
    )
    plan = compiled(rich_timeline, cut, "fast_funny",
                    recommendations=recommendations)
    zooms = [item for item in plan.items if item.is_zoom]
    assert zooms
    for item in zooms:
        assert item.is_marker_only or item.status != "planned"
        assert "protected hold" in (item.notes + item.status_reason)


def test_a_retimed_clip_is_not_zoomed(rich_timeline, recommendations):
    cut = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", 0.0, 200.0, 0.0, 0, speed=2.0)],
        on_scratch=True,
    )
    plan = compiled(rich_timeline, cut, "fast_funny",
                    recommendations=recommendations)
    for item in plan.items:
        if item.is_zoom and item.status == "planned":
            assert item.is_marker_only
            assert "retimed" in item.notes


def test_a_zoom_over_an_open_ui_is_refused(recommendations):
    timeline = timeline_of([
        visual(0, 20, importance="payoff", ui=UIState(inventory_open=True)),
    ])
    cut = one_clip_cut(duration=20.0)
    recs = RecommendationSet(recommendations=[EditRecommendation(
        recommendation_id="r_1", asset_id=ASSET.asset_id,
        source_file=ASSET.path, start=2.0, end=6.0, category="punch_in",
        priority=0.9, evidence=Evidence(visual_event_ids=["e_0"]),
    )])
    plan = compiled(timeline, cut, "fast_funny", recommendations=recs)
    zooms = [item for item in plan.items if item.is_zoom]
    assert zooms
    for item in zooms:
        assert item.is_marker_only or item.status != "planned"
        assert "full-screen UI" in (item.notes + item.status_reason)


def test_a_refused_zoom_still_tells_the_editor_what_was_wanted(recommendations):
    """"declined to zoom" and "did nothing" must not look the same."""
    cut = RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[placement("p_1", 0.0, 200.0, 0.0, 0, protected=True)],
        on_scratch=True,
    )
    timeline = timeline_of([visual(0, 200, importance="payoff")])
    plan = compiled(timeline, cut, "fast_funny",
                    recommendations=recommendations)
    refused = [item for item in plan.items if item.is_zoom]
    assert refused
    comment = refused[0].premiere_ops[0]["comment"]
    assert "was NOT applied" in comment


def test_the_critic_can_veto_emphasis_at_a_moment_it_flagged(rich_timeline, cut,
                                                             recommendations):
    revisions = RevisionSet(sequence_name=SEQUENCE, revisions=[
        RevisionRecommendation(
            revision_id="rv_1", issue="hud_hidden", severity="high",
            confidence=0.8, start=60.0, end=64.0,
        ),
    ])
    plan = compile_layers(
        rich_timeline, cut, style=style_presets.get("fast_funny"),
        recommendations=recommendations, revisions=revisions,
        roughcut_executed=True,
    )
    near = [
        item for item in plan.items
        if item.is_zoom and 58.0 <= item.start <= 66.0
    ]
    assert near
    for item in near:
        assert item.is_marker_only or item.status != "planned"


def test_ignoring_the_critic_is_explicit(rich_timeline, cut, recommendations):
    revisions = RevisionSet(sequence_name=SEQUENCE, revisions=[
        RevisionRecommendation(
            revision_id="rv_1", issue="hud_hidden", confidence=0.8,
            start=60.0, end=64.0,
        ),
    ])
    with_critic = compile_layers(
        rich_timeline, cut, style=style_presets.get("fast_funny"),
        recommendations=recommendations, revisions=revisions,
        roughcut_executed=True,
    )
    without = compile_layers(
        rich_timeline, cut, style=style_presets.get("fast_funny"),
        recommendations=recommendations, revisions=revisions,
        options=CompileOptions(use_critic=False), roughcut_executed=True,
    )
    assert any("critic" in w for w in with_critic.warnings)
    assert not any("flagged by the critic" in w for w in without.warnings)


def test_a_restrained_style_still_marks_the_strong_moments(rich_timeline, cut):
    """minimal_clean will not zoom the diamonds, but it says where they are."""
    plan = compiled(rich_timeline, cut, "minimal_clean")
    kinds = {item.kind for item in plan.planned()}
    assert kinds & {"reveal_marker", "danger_marker", "visual_callout"}


# ---------------------------------------------------------------------------
# Part 5 -- audio placeholders
# ---------------------------------------------------------------------------

def test_music_and_sfx_are_placeholders_not_operations(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "cinematic_minecraft")
    for item in plan.layer("audio"):
        if item.kind in audio_layer.CONVERTIBLE_KINDS:
            continue
        if item.status != "planned":
            continue
        assert item.is_marker_only
        assert "placeholder_only" in item.risks


def test_a_placeholder_says_why_it_is_only_a_placeholder(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "cinematic_minecraft")
    music = plan.of_kind("music_start")
    assert music
    assert "no sound library" in music[0].notes


def test_the_fades_are_the_only_real_audio_operations(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "fast_funny")
    for op in plan.ops:
        if op.get("op", "").startswith("audio."):
            assert op["op"] == "audio.fade"


def test_a_style_can_leave_audio_entirely_to_the_editor(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "minimal_clean")
    assert not any(op.get("op") == "audio.fade" for op in plan.ops)


def test_ducking_records_the_speech_ranges_it_would_need(rich_timeline, cut):
    """audio.duck needs a bed clip. There is none, so the ranges are kept."""
    plan = compiled(rich_timeline, cut, "cinematic_minecraft")
    duck = plan.of_kind("duck_narration")
    assert duck
    ranges = duck[0].payload.get("under")
    assert ranges and all("start" in entry and "end" in entry for entry in ranges)
    assert "not_convertible" in duck[0].risks


def test_an_sfx_placeholder_is_anchored_to_a_real_audio_event(rich_timeline,
                                                              cut):
    plan = compiled(rich_timeline, cut, "fast_funny")
    for item in plan.of_kind("impact_sfx"):
        assert item.evidence.audio_event_ids


def test_a_guessed_audio_event_ranks_below_a_measured_one(rich_timeline, cut):
    """The 0.45 inference cap has to survive into the style layer."""
    plan = compiled(rich_timeline, cut, "fast_funny")
    measured = [
        item for item in plan.items
        if item.kind == "impact_sfx" and "sudden_reaction" in item.evidence.audio_types
    ]
    guessed = [
        item for item in plan.items
        if item.kind == "impact_sfx" and "possible_scream" in item.evidence.audio_types
    ]
    assert measured and guessed
    assert min(i.priority for i in measured) > max(i.priority for i in guessed)


def test_a_style_only_emits_the_audio_kinds_it_declares(rich_timeline, cut):
    for name in style_presets.names():
        style = style_presets.get(name)
        plan = compiled(rich_timeline, cut, name)
        for item in plan.layer("audio"):
            assert (
                item.kind in style.audio_kinds
                or item.kind in audio_layer.CONVERTIBLE_KINDS
            ), f"{name} emitted {item.kind}"


# ---------------------------------------------------------------------------
# Part 6 -- title and chapter cards
# ---------------------------------------------------------------------------

def test_only_the_styles_that_want_cards_get_them(rich_timeline, cut):
    assert compiled(rich_timeline, cut, "documentary_story").layer("title")
    assert not compiled(rich_timeline, cut, "minimal_clean").layer("title")


def test_a_chapter_card_lands_on_a_real_section_boundary(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "documentary_story")
    reasons = " ".join(item.reason for item in plan.layer("title"))
    assert "environment changes" in reasons
    assert "failure" in reasons


def test_a_card_is_never_titled_with_something_nobody_said(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "documentary_story")
    for item in plan.layer("title"):
        title = item.payload.get("text", "")
        if not title:
            continue
        known = title in {
            "Into the Nether", "Starting Over", "Back at Base", "Underground",
            "After the Death",
        }
        spoken = " ".join(
            entry.text.lower()
            for segment in rich_timeline.segments
            for entry in segment.speech_entries
        )
        assert known or title.lower() in spoken


def test_a_section_shorter_than_the_styles_minimum_gets_no_card():
    """A run of biome changes while sprinting is one journey, not six chapters."""
    timeline = timeline_of([
        visual(0, 10, environment="cave"),
        visual(10, 20, environment="nether"),
        visual(20, 30, environment="stronghold"),
        visual(30, 40, environment="village"),
    ])
    cut = one_clip_cut(duration=40.0)
    plan = compiled(timeline, cut, "documentary_story")
    chapters = plan.of_kind("chapter_card")
    assert len(chapters) <= 1


def test_a_card_with_nothing_to_call_it_asks_the_editor_to_name_it():
    timeline = timeline_of([
        visual(0, 60, environment="unknown", importance="setup"),
        visual(60, 130, environment="nether", importance="setup"),
    ])
    cut = one_clip_cut(duration=130.0)
    plan = compiled(timeline, cut, "documentary_story")
    untitled = [
        item for item in plan.layer("title") if not item.payload.get("text")
    ]
    assert untitled
    assert "editor should title it" in untitled[0].notes


def test_a_card_never_covers_an_open_menu(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "documentary_story")
    for item in plan.layer("title"):
        if 160.0 <= item.start <= 200.0 and item.status == "planned":
            assert item.is_marker_only


def test_a_card_held_back_for_room_still_leaves_a_marker(rich_timeline, cut):
    """A documentary that silently loses a chapter has lost its shape."""
    style = style_presets.get("documentary_story", max_edits_per_minute=0.5)
    plan = compile_layers(rich_timeline, cut, style=style,
                          roughcut_executed=True)
    cards = plan.layer("title")
    assert cards
    for item in cards:
        assert item.status == "planned"
        assert item.premiere_ops, "a section boundary vanished entirely"


# ---------------------------------------------------------------------------
# Part 7 -- the compiler
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", style_presets.names())
def test_no_style_ever_exceeds_its_own_ceilings(name, rich_timeline, cut,
                                                recommendations):
    """The premise of the whole session, asserted directly."""
    style = style_presets.get(name)
    plan = compiled(rich_timeline, cut, name, recommendations=recommendations)
    density = plan.density()

    assert density["edits_per_minute"] <= style.max_edits_per_minute + 1e-6
    assert density["captions_per_minute"] <= style.max_captions_per_minute + 1e-6
    assert density["zooms_per_minute"] <= style.max_zooms_per_minute + 1e-6


@pytest.mark.parametrize("name", style_presets.names())
def test_a_short_cut_cannot_exceed_a_ceiling_either(name, rich_timeline,
                                                    recommendations):
    """The case an "at least one" floor quietly broke.

    documentary_story allows 0.4 zooms a minute. On a 30-second cut that is
    0.2 zooms -- so none. Rounding up to one gave it five times its own
    ceiling, which is the sort of violation nobody notices because the number
    still looks small.
    """
    short = one_clip_cut(duration=30.0)
    style = style_presets.get(name)
    plan = compiled(rich_timeline, short, name,
                    recommendations=recommendations)
    density = plan.density()

    assert density["edits_per_minute"] <= style.max_edits_per_minute + 1e-6
    assert density["captions_per_minute"] <= style.max_captions_per_minute + 1e-6
    assert density["zooms_per_minute"] <= style.max_zooms_per_minute + 1e-6


def test_a_sub_one_rate_is_a_whole_cut_budget(rich_timeline, cut,
                                              recommendations):
    """Below one a minute, a window count floors to zero and forbids everything.

    0.8 captions a minute has to mean "about one every 75 seconds", not "none,
    ever" -- so under one per minute the ceiling becomes a budget across the
    whole cut plus the spacing the rate implies.
    """
    style = style_presets.get("cinematic_minecraft")
    assert style.max_captions_per_minute < 1.0
    plan = compiled(rich_timeline, cut, "cinematic_minecraft",
                    recommendations=recommendations)
    captions = [i for i in plan.planned() if i.is_caption]

    assert captions, "a sub-one rate must still allow some captions"
    minutes = plan.cut_duration / 60.0
    assert len(captions) <= int(style.max_captions_per_minute * minutes)


def test_a_tighter_ceiling_plans_strictly_fewer_edits(rich_timeline, cut,
                                                      recommendations):
    loose = compiled(rich_timeline, cut, "fast_funny",
                     recommendations=recommendations)
    tight = compiled(rich_timeline, cut, "minimal_clean",
                     recommendations=recommendations)
    assert (
        tight.density()["active_edits"] < loose.density()["active_edits"]
    )


def test_the_ceiling_drops_the_weakest_candidates_first(rich_timeline, cut,
                                                        recommendations):
    style = style_presets.get("fast_funny", max_edits_per_minute=1.0)
    plan = compile_layers(rich_timeline, cut, style=style,
                          recommendations=recommendations,
                          roughcut_executed=True)
    kept = [i.priority for i in plan.planned() if i.is_active]
    dropped = [i.priority for i in plan.deferred() if i.layer != "base"]
    if kept and dropped:
        assert max(kept) >= max(dropped)


def test_a_style_never_emits_a_kind_it_forbids(rich_timeline, cut,
                                               recommendations):
    for name in style_presets.names():
        style = style_presets.get(name)
        plan = compiled(rich_timeline, cut, name,
                        recommendations=recommendations)
        for item in plan.planned():
            assert item.kind not in style.forbidden_kinds, name


def test_a_per_kind_limit_beats_the_global_ceiling(rich_timeline, cut,
                                                   recommendations):
    style = style_presets.get(
        "fast_funny", limited_kinds={"reaction_caption": 0.3}
    )
    plan = compile_layers(rich_timeline, cut, style=style,
                          recommendations=recommendations,
                          roughcut_executed=True)
    planned = [i for i in plan.planned() if i.kind == "reaction_caption"]
    assert len(planned) <= 1


def test_the_rough_cuts_own_markers_are_not_duplicated(rich_timeline):
    cut = one_clip_cut(markers=[
        SequenceMarker(time=60.0, name="CALLOUT", category="visual_callout"),
    ])
    plan = compiled(rich_timeline, cut, "fast_funny")
    duplicated = [
        item for item in plan.planned()
        if abs(item.start - 60.0) < 0.5
        and any(op.get("name") == "CALLOUT" for op in item.premiere_ops)
    ]
    assert not duplicated
    held = [i for i in plan.deferred() if "already placed" in i.status_reason]
    assert held


def test_two_items_of_one_kind_at_one_moment_collapse(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "fast_funny")
    seen: dict = {}
    for item in plan.planned():
        if item.layer == "base":
            continue
        key = (item.kind, round(item.start, 1))
        assert key not in seen, f"{item.kind} duplicated at {item.start}"
        seen[key] = item


def test_active_edits_of_one_sense_do_not_stack(rich_timeline, cut,
                                                recommendations):
    style = style_presets.get("cinematic_minecraft")
    plan = compiled(rich_timeline, cut, "cinematic_minecraft",
                    recommendations=recommendations)
    picture = sorted(
        (item for item in plan.planned()
         if item.is_active and compile_module._CHANNEL.get(item.kind) == "picture"),
        key=lambda item: item.start,
    )
    for earlier, later in zip(picture, picture[1:]):
        if earlier.kind == later.kind:
            continue
        assert later.start - earlier.start >= style.min_stack_spacing - 1e-6


def test_an_audio_fade_may_sit_under_a_title_card():
    """Stacking is per sense: a fade under a card is ordinary editing."""
    style = style_presets.get("fast_funny")
    item = LayerItem(item_id="li_1", kind="audio_fade_in", start=0.0)
    card = LayerItem(item_id="li_2", kind="title_card", start=0.0,
                     premiere_ops=[{"op": "text.create"}])
    assert compile_module._stacked(item, [card], style) == ""


def test_the_base_layer_carries_the_clips_and_no_operations(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "fast_funny")
    base = plan.layer("base")
    assert len(base) == len(cut.placements)
    for item in base:
        assert item.premiere_ops == []
        assert not item.is_convertible


def test_the_base_layer_can_be_left_out(rich_timeline, cut):
    plan = compiled(rich_timeline, cut, "fast_funny",
                    options=CompileOptions(include_base=False))
    assert plan.layer("base") == []


def test_operations_run_in_an_order_that_works(rich_timeline, cut,
                                               recommendations):
    plan = compiled(rich_timeline, cut, "fast_funny",
                    recommendations=recommendations)
    names = [op["op"] for op in plan.ops]
    assert names[0] == "sequence.activate"
    if "text.create" in names:
        assert names.index("track.add") < names.index("text.create")
    if "marker.add" in names and "animate" in names:
        assert names.index("animate") < names.index("marker.add")


def test_the_overlay_track_is_added_once_and_only_when_needed(rich_timeline,
                                                              cut):
    drawn = compiled(rich_timeline, cut, "fast_funny")
    if any(op["op"] == "text.create" for op in drawn.ops):
        assert sum(1 for op in drawn.ops if op["op"] == "track.add") == 1

    marked = compiled(rich_timeline, cut, "minimal_clean")
    assert not any(op["op"] == "track.add" for op in marked.ops)


def test_an_empty_rough_cut_is_reported_not_crashed(rich_timeline):
    plan = compile_layers(
        rich_timeline, RoughCutPlan(sequence_name=SEQUENCE),
        style=style_presets.get("fast_funny"),
    )
    assert plan.ops == []
    assert any("nothing to style" in w for w in plan.warnings)


def test_the_operation_ceiling_is_reported_when_it_bites(rich_timeline, cut,
                                                         recommendations):
    plan = compiled(rich_timeline, cut, "fast_funny",
                    recommendations=recommendations,
                    options=CompileOptions(max_operations=5))
    assert len(plan.ops) <= 5
    assert any("ceiling" in w for w in plan.warnings)


# ---------------------------------------------------------------------------
# Execution guards
# ---------------------------------------------------------------------------

@pytest.fixture
def runnable(rich_timeline, cut, recommendations):
    plan = compiled(rich_timeline, cut, "fast_funny",
                    recommendations=recommendations)
    style_execute.dry_run(plan)
    return plan


def test_the_dry_run_validates_offline(runnable):
    assert runnable.dry_run_passed is True
    assert runnable.explanation
    assert runnable.dry_run_error is None


def test_a_style_pass_can_never_change_timing(runnable):
    """The guarantee that makes this the safest of the three passes."""
    assert style_execute.changes_timing(runnable) is False
    for op in runnable.ops:
        assert not str(op["op"]).startswith("clip.")


def test_the_allowlist_is_the_whole_guarantee():
    """Every op the style path can emit must be one it is permitted to emit."""
    emitted = {
        "sequence.activate", "track.add", "animate", "audio.fade",
        "text.create", "marker.add",
    }
    assert emitted == set(style_execute.ALLOWED_OPS)
    assert not any(op.startswith("clip.") for op in style_execute.ALLOWED_OPS)


@pytest.mark.parametrize("op", [
    {"op": "clip.trim", "clip": {"track": "V1", "index": 0}, "edge": "out",
     "by": 1.0},
    {"op": "clip.speed", "clip": {"track": "V1", "index": 0}, "rate": 2.0},
    {"op": "clip.remove", "clip": {"track": "V1", "index": 0}},
    {"op": "sequence.create", "name": "Something Else"},
    {"op": "project.save"},
    {"op": "marker.remove", "at": 4.0},
])
def test_anything_that_could_move_or_destroy_a_clip_is_refused(runnable, op):
    runnable.ops.append(op)
    style_execute.dry_run(runnable)

    assert runnable.dry_run_passed is False
    assert runnable.dry_run_error["code"] == "forbidden_operation"
    assert op["op"] in runnable.dry_run_error["error"]


def test_an_empty_plan_fails_the_dry_run_with_a_reason():
    plan = LayeredEditPlan(sequence_name=SEQUENCE)
    style_execute.dry_run(plan)
    assert plan.dry_run_passed is False
    assert plan.dry_run_error["code"] == "empty_plan"


def test_plan_only_validates_nothing_and_runs_nothing(runnable):
    engine = FakeEngine()
    report = style_execute.run(runnable, mode="plan_only", engine=engine)
    assert report.executed is False
    assert report.dry_run_passed is False
    assert engine.calls == []


def test_a_dry_run_never_reaches_the_engine(runnable, cut):
    engine = FakeEngine()
    report = style_execute.run(runnable, mode="dry_run", roughcut=cut,
                               engine=engine)
    assert report.executed is False
    assert report.dry_run_passed is True
    assert engine.calls == []


def test_execution_refuses_when_the_dry_run_fails(runnable, cut):
    runnable.ops.append({"op": "project.save"})
    engine = FakeEngine()
    report = style_execute.run(runnable, mode="execute", roughcut=cut,
                               engine=engine)
    assert report.executed is False
    assert report.refused_reason
    assert engine.calls == []


def test_execution_validates_again_rather_than_trusting_a_stored_pass(runnable,
                                                                      cut):
    assert runnable.dry_run_passed is True
    runnable.ops.append({"op": "clip.remove", "clip": {"track": "V1",
                                                       "index": 0}})
    engine = FakeEngine()
    report = style_execute.run(runnable, mode="execute", roughcut=cut,
                               engine=engine)
    assert report.executed is False
    assert engine.calls == []


def test_execution_refuses_a_plan_that_does_not_activate_its_target(runnable,
                                                                    cut):
    runnable.ops[0] = {"op": "sequence.activate", "name": "Someone's Real Edit"}
    engine = FakeEngine()
    report = style_execute.run(runnable, mode="execute", roughcut=cut,
                               engine=engine)
    assert report.executed is False
    assert report.on_scratch is False
    assert "Someone's Real Edit" in report.refused_reason
    assert engine.calls == []


def test_execution_refuses_when_the_rough_cut_was_not_scratch_safe(runnable,
                                                                   cut):
    cut.ops = [op for op in cut.ops if op["op"] != "sequence.create"]
    engine = FakeEngine()
    report = style_execute.run(runnable, mode="execute", roughcut=cut,
                               engine=engine)
    assert report.executed is False
    assert "scratch" in report.refused_reason
    assert engine.calls == []


def test_execution_refuses_when_the_sequence_was_never_built(rich_timeline, cut,
                                                             recommendations):
    plan = compile_layers(
        rich_timeline, cut, style=style_presets.get("fast_funny"),
        recommendations=recommendations, roughcut_executed=False,
    )
    engine = FakeEngine()
    report = style_execute.run(plan, mode="execute", roughcut=cut,
                               engine=engine)
    assert report.executed is False
    assert "no record" in report.refused_reason
    assert engine.calls == []


def test_a_plan_that_passes_every_guard_runs(runnable, cut):
    engine = FakeEngine()
    report = style_execute.run(runnable, mode="execute", roughcut=cut,
                               engine=engine)
    assert report.executed is True
    assert report.on_scratch is True
    assert report.operations_succeeded == len(runnable.ops)
    assert len(engine.calls) == 1
    assert engine.calls[0].get("dry_run") is not True
    assert runnable.executed is True


def test_a_premiere_failure_is_reported_rather_than_raised(runnable, cut):
    report = style_execute.run(runnable, mode="execute", roughcut=cut,
                               engine=FakeEngine(succeed=False))
    assert report.executed is False
    assert report.error["error"] == "Premiere said no"


def test_an_unknown_mode_is_a_usage_error(runnable):
    with pytest.raises(EditingError):
        style_execute.run(runnable, mode="just-style-it")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_the_report_leads_with_density(runnable):
    text = style_report.render(runnable)
    assert text.index("DENSITY") < text.index("OPERATION PLAN")
    assert "cannot trim" in text


def test_the_density_view_shows_per_minute_buckets(runnable):
    text = style_report.render_density(runnable)
    assert "active edits" in text
    assert "minute   active" in text


def test_the_deferred_view_names_the_ceiling_that_stopped_each_item(
    rich_timeline, cut, recommendations
):
    plan = compiled(rich_timeline, cut, "minimal_clean",
                    recommendations=recommendations)
    text = style_report.render_deferred(plan)
    assert "held back" in text
    assert "by reason:" in text


# ---------------------------------------------------------------------------
# Pipeline and CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def staged(config, rich_timeline, cut, recommendations):
    """A timeline, recommendations and an executed rough cut, on disk."""
    from dataclasses import replace
    from editing.config import SamplingConfig
    from editing.pipeline import build_pipeline
    from editing.roughcut.schema import ExecutionReport

    pipeline = build_pipeline(replace(config), SamplingConfig())
    pipeline.write_timeline(rich_timeline)
    pipeline.write_recommendations(recommendations)
    pipeline.write_rough_cut(cut)
    pipeline.assets = [ASSET]
    pipeline.write_assets()
    pipeline.write_execution_report(ExecutionReport(
        mode="execute_on_scratch", executed=True, sequence_name=SEQUENCE,
        on_scratch=True, dry_run_passed=True,
    ))
    return pipeline


def test_the_pipeline_builds_and_saves_a_layered_edit(staged):
    plan = staged.layers(style=style_presets.get("documentary_story"))
    assert plan.dry_run_passed is True
    assert plan.roughcut_executed is True
    assert (staged.config.layers_dir / "structure.json").exists()
    assert (staged.config.layers_dir / "structure.txt").exists()
    assert staged.load_layers().ops == plan.ops


def test_the_rough_cut_survives_being_styled(staged):
    """A style pass is one interpretation; the cut has to outlive it."""
    before = (staged.config.roughcut_dir / "structure.json").read_text("utf-8")
    staged.layers(style=style_presets.get("fast_funny"))
    staged.layers(style=style_presets.get("minimal_clean"))
    after = (staged.config.roughcut_dir / "structure.json").read_text("utf-8")
    assert before == after


def test_restyling_replaces_the_plan_and_nothing_else(staged):
    first = staged.layers(style=style_presets.get("fast_funny"))
    second = staged.layers(style=style_presets.get("minimal_clean"))
    assert first.style != second.style
    assert staged.load_layers().style == "minimal_clean"


def run_cli(argv, capsys):
    from editing.cli import main

    code = main(argv)
    return code, capsys.readouterr()


def test_the_cli_lists_styles(capsys):
    code, captured = run_cli(["style", "list", "--json", "-q"], capsys)
    assert code == 0
    payload = json.loads(captured.out)
    assert {p["name"] for p in payload["presets"]} == set(style_presets.names())
    assert payload["default"] == style_presets.DEFAULT_PRESET


def test_the_cli_shows_one_style(capsys):
    code, captured = run_cli(
        ["style", "show", "fast_funny", "--json", "-q"], capsys
    )
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["name"] == "fast_funny"
    assert payload["problems"] == []


def test_the_cli_rejects_an_unknown_style(capsys):
    code, captured = run_cli(
        ["style", "show", "nonsense", "--json", "-q"], capsys
    )
    assert code == 1
    assert json.loads(captured.out)["success"] is False


def test_the_cli_builds_layers(staged, capsys):
    code, captured = run_cli([
        "layers", "build", "--style", "documentary_story",
        "--output-dir", str(staged.config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["style"] == "documentary_story"
    assert payload["dry_run_passed"] is True
    assert payload["executed"] is False


def test_the_cli_refuses_to_execute_without_yes(staged, capsys):
    staged.layers(style=style_presets.get("fast_funny"))
    code, captured = run_cli([
        "layers", "execute", "--output-dir", str(staged.config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 1
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert "--yes" in payload["hint"]
    assert not (staged.config.layers_dir / "structure.execution.json").exists()


def test_the_cli_dry_run_applies_nothing(staged, capsys):
    staged.layers(style=style_presets.get("fast_funny"))
    code, captured = run_cli([
        "layers", "dry-run", "--output-dir", str(staged.config.output_dir),
        "--no-premiere", "--json", "-q",
    ], capsys)
    assert code == 0
    assert json.loads(captured.out)["report"]["executed"] is False


def test_the_cli_shows_density_and_deferred(staged, capsys):
    staged.layers(style=style_presets.get("minimal_clean"))
    code, captured = run_cli([
        "layers", "show-density", "--output-dir",
        str(staged.config.output_dir), "--no-premiere", "--json", "-q",
    ], capsys)
    assert code == 0
    assert "edits_per_minute" in json.loads(captured.out)

    code, captured = run_cli([
        "layers", "show-deferred", "--output-dir",
        str(staged.config.output_dir), "--no-premiere", "--json", "-q",
    ], capsys)
    assert code == 0
    assert "deferred" in json.loads(captured.out)


def test_the_cli_exports_the_plan(staged, tmp_path, capsys):
    staged.layers(style=style_presets.get("fast_funny"))
    target = tmp_path / "handoff" / "layers.json"
    code, captured = run_cli([
        "layers", "export", "--out", str(target),
        "--output-dir", str(staged.config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)
    assert code == 0
    assert target.exists()
    assert json.loads(target.read_text("utf-8"))["style"] == "fast_funny"


def test_the_cli_can_build_a_markers_only_pass(staged, capsys):
    """The safest possible pass: nothing is drawn and nothing is scaled."""
    code, captured = run_cli([
        "layers", "build", "--style", "fast_funny", "--markers-only",
        "--output-dir", str(staged.config.output_dir), "--no-premiere",
        "--json", "-q",
    ], capsys)
    assert code == 0
    payload = json.loads(captured.out)
    ops = {op["op"] for op in payload["plan"]["ops"]}
    assert ops <= {"sequence.activate", "marker.add"}


# ---------------------------------------------------------------------------
# Condensing: a caption has to be a thing somebody said
# ---------------------------------------------------------------------------

class TestCondenseKeepsSentences:
    def test_a_sentence_that_fits_is_used_whole(self):
        """The window logic read straight across a sentence boundary and
        dropped the punctuation on the way. On the first real episode that put
        "I fell off What do..." on screen -- ungrammatical, and cut in the
        middle of a thought."""
        from editing.style.captions import condense

        text, condensed = condense("I fell off. What do you mean?", 5)
        assert condensed
        assert text == "What do you mean?"
        assert "..." not in text

    def test_the_strongest_fitting_sentence_wins(self):
        from editing.style.captions import condense

        text, _ = condense(
            "Yeah, sure buddy. I did not do it. I did not do anything.", 5)
        assert text in ("Yeah, sure buddy.", "I did not do it.")
        assert text.endswith((".", "!", "?"))

    def test_a_line_that_already_fits_is_untouched(self):
        from editing.style.captions import condense

        assert condense("short line", 7) == ("short line", False)

    def test_no_sentence_fits_so_a_window_is_taken(self):
        from editing.style.captions import condense

        text, condensed = condense(
            "okay so anyway I think that was actually a creeper right there", 4)
        assert condensed
        assert "creeper" in text
        assert "..." in text

    def test_sentence_splitting_keeps_punctuation(self):
        from editing.style.captions import sentences_in

        assert sentences_in("One. Two! Three?") == ["One.", "Two!", "Three?"]
        assert sentences_in("   ") == []

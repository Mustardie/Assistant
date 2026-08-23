"""The director pass: a model proposes, deterministic rules decide.

Five properties carry the weight.

**The model can never place footage that does not exist.** Decisions name
segment ids; times come from the context those ids resolve to. A hallucinated
id produces a rejection with a reason, and there is no path by which a number
a model typed becomes a source time -- except ``shorten``, which is clamped
inside the segments the decision named and is tested from both sides.

**Rejection is the default.** A decision arrives ``accepted=False`` and only a
deterministic check can change that. Every check has a test that it fires and
a test that it does not fire on the honest case, because a safety layer that
rejects everything is as useless as one that rejects nothing.

**The whole-episode checks are the point.** Cutting the setup for a payoff
that stays in, cutting the payoff itself, three hooks, a runtime cap: none of
these can be seen eight seconds at a time, and they are what justifies asking
a model at all.

**Nothing is faked.** A model that answers with prose, with an empty list, or
not at all produces a typed failure and no decisions -- never an empty plan
that reads as a considered decision to keep everything. The mock backend
stamps itself on the plan, the report and the auto stage summary.

**The heuristic never goes away.** Every mode falls back to it, and a rough
cut asked for in director mode without a plan says so on the cut rather than
quietly producing a threshold cut under a director label.

Nothing here needs a model, a GPU, Premiere, FFmpeg, Whisper or real footage.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from editing.align import build_timeline
from editing.errors import EditingError, ModelError
from editing.director import backends as backends_module
from editing.director import compare as compare_module
from editing.director import context as context_module
from editing.director import convert as convert_module
from editing.director import parse as parse_module
from editing.director import prompt as prompt_module
from editing.director import report as report_module
from editing.director import run as run_module
from editing.director import safety as safety_module
from editing.director import store as store_module
from editing.director import style_guide as style_guide_module
from editing.director.schema import (
    ACTIONS, MIN_ACTIONABLE_CONFIDENCE, MODES, REASON_CATEGORIES,
    SINGLE_CHANNEL_CAP, ContextSegment, DirectorConfig, DirectorContext,
    DirectorDecision, DirectorFailure, DirectorPlan, DirectorPrompt,
    DirectorRange, DirectorReason, DirectorResult, DirectorSafetyReview,
    StyleGuide, decision_id_for,
)
from editing.roughcut.build import RoughCutOptions, build_rough_cut
from editing.schema import (
    AudioEvent, MediaAsset, TimeRange, Transcript, TranscriptEntry,
    VisualEvent,
)

ASSET = MediaAsset(
    asset_id="a_test", path="/footage/ep12.mp4", filename="ep12.mp4",
    duration=400.0,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def visual(start, end, *, importance="setup", environment="cave",
           actions=("mining",), threats=(), confidence=0.85):
    return VisualEvent(
        event_id=f"e_{start}", source_file=ASSET.path, asset_id=ASSET.asset_id,
        start=start, end=end, confidence=confidence, environment=environment,
        actions=list(actions), threats=list(threats), importance=importance,
        suggested_range=TimeRange(start, end), model="test-model",
    )


def audio(start, end, kind, *, confidence=0.8):
    return AudioEvent(
        event_id=f"au_{start}_{kind}", source_file=ASSET.path,
        asset_id=ASSET.asset_id, start=start, end=end, type=kind,
        confidence=confidence, detection="heuristic", loudness_db=-8.0,
        baseline_db=-24.0,
    )


def a_timeline(events=None, audio_events=(), lines=()):
    """A timeline with three channels, so decisions are not single-channel."""
    events = events if events is not None else [
        visual(0, 20, importance="setup", environment="plains",
               actions=("walking",)),
        visual(20, 40, importance="setup"),
        visual(40, 60, importance="danger", actions=("fighting",),
               threats=("creeper",)),
        visual(60, 90, importance="setup"),
        visual(90, 110, importance="payoff"),
        visual(110, 130, importance="setup"),
    ]
    transcript = Transcript(
        asset_id=ASSET.asset_id, source="srt",
        entries=[TranscriptEntry(*line) for line in lines],
    ) if lines else None
    return build_timeline(
        [ASSET], {ASSET.asset_id: list(events)},
        {ASSET.asset_id: transcript} if transcript else {},
        audio_by_asset={ASSET.asset_id: list(audio_events)},
    )


DEFAULT_LINES = (
    (0.0, 18.0, "right so today we are going to find some diamonds"),
    (40.0, 58.0, "oh god a creeper watch out watch out"),
    (90.0, 108.0, "there we go netherite that is what we came for"),
)

DEFAULT_AUDIO = (
    audio(20, 40, "silence"),
    audio(40, 60, "sudden_reaction"),
    audio(90, 110, "possible_laughter"),
)


@pytest.fixture
def timeline():
    return a_timeline(audio_events=DEFAULT_AUDIO, lines=DEFAULT_LINES)


@pytest.fixture
def context(timeline):
    return context_module.build(
        timeline, config=DirectorConfig().validated(),
        style_guide=StyleGuide(text="Never open on walking.", source="inline"),
    )


def a_context(segments=None, **fields) -> DirectorContext:
    """A context built by hand, for the safety tests."""
    built = DirectorContext(
        name="structure",
        duration=200.0,
        segments=segments if segments is not None else [
            ContextSegment(segment_id="s_1", asset_id="a_test",
                           source_file="/footage/ep12.mp4",
                           start=0.0, end=20.0, said="hello there",
                           importance="setup", audio=["silence"]),
            ContextSegment(segment_id="s_2", asset_id="a_test",
                           source_file="/footage/ep12.mp4",
                           start=20.0, end=40.0, said="a creeper",
                           importance="danger", audio=["sudden_reaction"]),
            ContextSegment(segment_id="s_3", asset_id="a_test",
                           source_file="/footage/ep12.mp4",
                           start=40.0, end=60.0, said="we got it",
                           importance="payoff", audio=["possible_laughter"]),
        ],
        sources={"timeline": True, "transcript": True},
    )
    for key, value in fields.items():
        setattr(built, key, value)
    return built


def a_decision(action="keep", segment_ids=("s_1",), **fields
               ) -> DirectorDecision:
    decision = DirectorDecision(
        decision_id=decision_id_for(action, segment_ids),
        action=action,
        segment_ids=list(segment_ids),
        asset_id="a_test",
        source_file="/footage/ep12.mp4",
        start=fields.pop("start", 0.0),
        end=fields.pop("end", 20.0),
        confidence=fields.pop("confidence", 0.8),
        priority=fields.pop("priority", 0.5),
        reason=fields.pop("reason", DirectorReason(
            category="pacing", text="because it works")),
        evidence=fields.pop("evidence", ["s_1"]),
    )
    decision.out_start = fields.pop("out_start", decision.start)
    decision.out_end = fields.pop("out_end", decision.end)
    for key, value in fields.items():
        setattr(decision, key, value)
    return decision


def answer(*decisions, approach="a plan") -> str:
    """A model answer, as JSON text."""
    return json.dumps({"approach": approach, "decisions": list(decisions)})


def raw(action="keep", segment_ids=("s_1",), **fields) -> dict:
    entry = {
        "segment_ids": list(segment_ids),
        "action": action,
        "reason": {"category": "pacing", "text": "it reads well"},
        "confidence": 0.8,
        "priority": 0.5,
        "evidence": ["s_1"],
    }
    entry.update(fields)
    return entry


class ScriptedModel:
    """A model that says exactly what a test tells it to."""

    name = "scripted"

    def __init__(self, *responses, fail=None):
        self.responses = list(responses)
        self.fail = fail
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if self.fail is not None:
            raise self.fail
        return self.responses.pop(0) if self.responses else answer()

    def health(self) -> dict:
        return {"backend": "scripted", "ready": True}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_nonsense_settings_clamp_rather_than_raise():
    settings = DirectorConfig(
        backend="banana", mode="freestyle", temperature=99.0,
        max_segments=0, max_output_tokens=1, max_context_chars=1,
        default_speed=99.0, model="",
    ).validated()

    assert settings.backend == "openai"
    assert settings.mode == "director"
    assert settings.temperature == 2.0, "clamped, not rejected"
    assert settings.max_segments == 1
    assert settings.max_output_tokens == 512
    assert settings.max_context_chars == 2000
    assert settings.default_speed == 8.0
    assert settings.model


def test_the_cache_key_ignores_settings_that_change_no_answer():
    base = DirectorConfig().validated()
    for field, value in (
        ("use_cache", False), ("timeout", 30.0), ("max_retries", 0),
        ("api_key", "sk-different"), ("min_confidence", 0.9),
    ):
        changed = replace(base, **{field: value}).validated()
        assert changed.cache_key_part() == base.cache_key_part(), field


def test_the_cache_key_notices_settings_that_do():
    base = DirectorConfig().validated()
    for field, value in (
        ("model", "other"), ("backend", "mock"), ("temperature", 0.9),
        ("mode", "hybrid"), ("max_segments", 20), ("target_duration", 600.0),
        ("style", "fast_funny"), ("max_context_chars", 9000),
    ):
        changed = replace(base, **{field: value}).validated()
        assert changed.cache_key_part() != base.cache_key_part(), field


def test_an_api_key_is_never_written_to_disk():
    settings = DirectorConfig(api_key="sk-secret-value")
    assert "sk-secret" not in json.dumps(settings.to_dict())
    assert settings.to_dict()["api_key"] == "***"

    # And a redacted key round-trips to the default rather than to "***".
    restored = DirectorConfig.from_dict(settings.to_dict())
    assert restored.api_key == "not-needed"


def test_settings_warn_about_the_choices_that_bite():
    assert any("MOCK" in w for w in DirectorConfig(backend="mock").warnings)
    assert any("temperature" in w
               for w in DirectorConfig(temperature=0.9).warnings)
    assert any("no model runs" in w
               for w in DirectorConfig(mode="heuristic").warnings)
    assert any("maximum" in w for w in DirectorConfig(
        target_duration=600.0, max_duration=300.0).warnings)
    assert DirectorConfig().validated().warnings == []


def test_only_the_model_modes_run_a_model():
    assert DirectorConfig(mode="director").runs_model
    assert DirectorConfig(mode="hybrid").runs_model
    assert not DirectorConfig(mode="heuristic").runs_model


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_a_decision_is_not_accepted_until_something_accepts_it():
    """The safety model, expressed as a default value."""
    assert DirectorDecision().accepted is False
    assert DirectorDecision.from_dict({"action": "keep"}).accepted is False


def test_a_decision_computes_what_it_costs_the_cut():
    decision = a_decision("speed_up", start=0.0, end=60.0, speed=2.0)
    assert decision.duration == 60.0
    assert decision.cut_duration == 30.0

    passive = a_decision("marker_only", start=0.0, end=60.0)
    assert passive.cut_duration == 0.0, "a note occupies no runtime"
    assert passive.changes_nothing


def test_the_action_decides_whether_footage_is_protected():
    for action in ("hold", "payoff", "hook", "setup"):
        assert a_decision(action).is_protecting, action
    for action in ("keep", "speed_up", "shorten"):
        assert not a_decision(action).is_protecting, action


def test_a_decision_only_becomes_a_range_when_it_keeps_footage():
    assert a_decision("keep").as_range() is not None
    assert a_decision("cut").as_range() is None
    assert a_decision("needs_human_review").as_range() is None
    assert a_decision("keep", start=5.0, end=5.0, out_start=5.0,
                      out_end=5.0).as_range() is None


def test_a_hook_range_is_flagged_rather_than_inferred():
    """A genuine reveal that sorts first is not a hook."""
    hook = a_decision("hook").as_range()
    assert hook.is_hook and hook.protected
    assert not a_decision("keep").as_range().is_hook


def test_an_unknown_action_coerces_to_keep_on_read():
    assert DirectorDecision.from_dict({"action": "explode"}).action == "keep"
    assert DirectorDecision.from_dict(
        {"action": "payoff"}).action == "payoff"


def test_a_reason_survives_being_given_as_a_sentence():
    """Models told to give an object sometimes give a string."""
    reason = DirectorReason.from_dict("it is the best bit")
    assert reason.text == "it is the best bit"
    assert reason.category == "unknown"


def test_a_decision_round_trips_through_a_dict():
    decision = a_decision("shorten", segment_ids=("s_1", "s_2"),
                          out_start=4.0, out_end=12.0, accepted=True)
    restored = DirectorDecision.from_dict(decision.to_dict())
    assert restored.action == "shorten"
    assert restored.out_start == 4.0 and restored.out_end == 12.0
    assert restored.segment_ids == ["s_1", "s_2"]
    assert restored.accepted is True


def test_a_plan_round_trips_and_keeps_its_rejections():
    plan = DirectorPlan(
        decisions=[a_decision("keep", accepted=True),
                   a_decision("cut", segment_ids=("s_2",), accepted=False,
                              rejected_reason="no")],
    )
    restored = DirectorPlan.from_dict(plan.to_dict())
    assert len(restored.accepted) == 1
    assert len(restored.rejected) == 1
    assert restored.rejected[0].rejected_reason == "no"


def test_every_plan_says_it_measures_nothing():
    plan = DirectorPlan()
    assert "retention" in plan.not_measured
    assert "analytics" in plan.not_measured
    assert plan.not_measured in json.dumps(plan.to_dict())


def test_a_decision_id_is_stable_for_one_call_over_one_range():
    assert decision_id_for("keep", ["s_1", "s_2"]) == \
        decision_id_for("keep", ["s_2", "s_1"]), "order must not matter"
    assert decision_id_for("keep", ["s_1"]) != decision_id_for("cut", ["s_1"])


# ---------------------------------------------------------------------------
# The style guide
# ---------------------------------------------------------------------------

def test_the_builtin_guide_has_real_rules_in_it():
    guide = style_guide_module.load()
    assert guide.is_default and guide.source == "builtin"
    assert len(guide.rules) > 10
    # A guide saying "make good choices" tells a model nothing.
    assert "grind" in guide.text
    assert "payoff" in guide.text


def test_a_style_guide_loads_from_a_file(tmp_path):
    target = tmp_path / "mine.md"
    target.write_text("I hold two beats after deaths.\n", encoding="utf-8")

    guide = style_guide_module.load(str(target))
    assert "two beats" in guide.text
    assert guide.source == "argument"
    assert guide.name == "mine"
    assert not guide.is_default


def test_a_style_guide_path_that_is_wrong_is_an_error_not_a_fallback(tmp_path):
    """Somebody who typed a path wants that guide, not a different one."""
    with pytest.raises(EditingError) as caught:
        style_guide_module.load(str(tmp_path / "nope.md"))
    assert "style guide" in caught.value.message
    assert "built-in" in caught.value.hint


def test_an_empty_style_guide_is_refused(tmp_path):
    target = tmp_path / "empty.md"
    target.write_text("   \n", encoding="utf-8")
    with pytest.raises(EditingError) as caught:
        style_guide_module.load(str(target))
    assert "empty" in caught.value.message


def test_a_style_guide_is_found_beside_the_project(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "editing_style.md").write_text("cut fast", encoding="utf-8")

    guide = style_guide_module.load(search_root=str(tmp_path))
    assert guide.source == "project"
    assert "cut fast" in guide.text


def test_the_environment_can_name_a_style_guide(tmp_path, monkeypatch):
    target = tmp_path / "env.md"
    target.write_text("from the environment", encoding="utf-8")
    monkeypatch.setenv("EDITING_STYLE_GUIDE", str(target))

    guide = style_guide_module.load()
    assert guide.source == "environment"
    assert "from the environment" in guide.text


def test_an_enormous_style_guide_is_truncated_and_says_so():
    guide = style_guide_module.load(text="x" * 40000)
    assert len(guide.text) < 40000
    assert "truncated" in guide.text


def test_a_style_guide_fingerprint_changes_with_its_text():
    first = StyleGuide(text="cut fast")
    assert first.fingerprint() == StyleGuide(text="cut fast").fingerprint()
    assert first.fingerprint() != StyleGuide(text="cut slow").fingerprint()


# ---------------------------------------------------------------------------
# The context builder
# ---------------------------------------------------------------------------

def test_the_context_carries_every_candidate_range(timeline):
    built = context_module.build(timeline)
    assert built.segments
    assert all(segment.segment_id for segment in built.segments)
    assert built.duration > 0
    assert built.sources["timeline"] is True
    assert built.sources["transcript"] is True


def test_adjacent_segments_that_read_the_same_are_merged():
    """The biggest reduction, and it loses nothing.

    Eight identical stretches of tunnelling are one thing to decide about,
    not eight -- and the director would have made one decision about them
    anyway.
    """
    grind = a_timeline(events=[
        visual(index * 10, index * 10 + 10, importance="setup",
               actions=("mining",))
        for index in range(8)
    ])
    built = context_module.build(grind)

    assert len(built.segments) < len(grind.segments)
    assert built.segments[0].start == 0
    assert built.segments[-1].end == 80
    for segment in built.segments:
        assert segment.end > segment.start


def test_a_merge_never_crosses_a_change_of_verdict():
    first = ContextSegment(segment_id="a", asset_id="x", start=0, end=10,
                           heuristic="keep", importance="payoff")
    second = ContextSegment(segment_id="b", asset_id="x", start=10, end=20,
                            heuristic="cut", importance="payoff")
    assert not context_module._mergeable(first, second)

    third = ContextSegment(segment_id="c", asset_id="x", start=10, end=20,
                           heuristic="keep", importance="setup")
    assert not context_module._mergeable(first, third)

    same = ContextSegment(segment_id="d", asset_id="x", start=10, end=20,
                          heuristic="keep", importance="payoff")
    assert context_module._mergeable(first, same)


def test_a_merge_never_crosses_a_file_boundary():
    first = ContextSegment(segment_id="a", asset_id="x", start=0, end=10,
                           heuristic="keep")
    other = ContextSegment(segment_id="b", asset_id="y", start=10, end=20,
                           heuristic="keep")
    assert not context_module._mergeable(first, other)


def test_speech_is_shortened_and_never_paraphrased():
    said = " ".join(f"word{i}" for i in range(200))
    trimmed = context_module._trim_speech(said, 100)

    assert len(trimmed) <= 105
    assert "..." in trimmed
    # Head and tail, because the punchline is usually at the end.
    assert trimmed.startswith("word0")
    assert trimmed.endswith("word199")
    for fragment in trimmed.replace("...", " ").split():
        assert fragment in said, "nothing was invented"


def test_a_short_line_is_left_alone():
    assert context_module._trim_speech("hello there", 100) == "hello there"


def test_the_context_records_what_it_could_not_see(timeline):
    built = context_module.build(timeline)
    assert built.sources["episode_memory"] is False
    assert any("No episode memory" in w for w in built.warnings)
    assert any("No retention plan" in w for w in built.warnings)


def test_footage_with_no_transcript_says_so_loudly():
    built = context_module.build(a_timeline(audio_events=DEFAULT_AUDIO))
    assert built.sources["transcript"] is False
    assert any("cannot hear the episode" in w for w in built.warnings)
    assert "no transcript" in built.summary


def test_the_context_is_capped_at_the_configured_segment_count(timeline):
    settings = DirectorConfig(max_segments=2).validated()
    built = context_module.build(timeline, config=settings)
    assert len(built.segments) <= 2


def test_thinning_keeps_the_opening_the_ending_and_the_good_bits():
    segments = [
        ContextSegment(segment_id=f"s_{i}", asset_id="x", start=i * 10,
                       end=i * 10 + 10, position=i / 10,
                       importance="payoff" if i == 5 else "setup",
                       usefulness=0.9 if i == 5 else 0.1,
                       dead_air=i in (2, 3))
        for i in range(10)
    ]
    kept = context_module._thin(segments, 4)
    ids = [segment.segment_id for segment in kept]

    assert "s_5" in ids, "the payoff"
    assert "s_0" in ids, "the opening"
    assert "s_9" in ids, "the ending"
    assert "s_2" not in ids and "s_3" not in ids, "dead air goes first"
    # Position in the episode is information, so order is restored.
    assert ids == sorted(ids, key=lambda item: int(item.split("_")[1]))


def test_the_budget_drops_the_least_valuable_sections_first(timeline):
    settings = DirectorConfig(max_context_chars=3000).validated()
    built = context_module.build(
        timeline, config=settings,
        recommendations=None,
    )
    assert built.dropped or len(prompt_module.render_context(built)) <= 3000
    # The story layer is never what goes.
    assert built.segments


def test_the_heuristic_verdict_travels_with_every_candidate(timeline):
    built = context_module.build(timeline)
    verdicts = {segment.heuristic for segment in built.segments}
    assert verdicts, "the model can agree or disagree, not start from nothing"
    assert verdicts <= {"keep", "cut", "speed_up", "keep-talking"}


def test_the_context_fingerprint_changes_with_the_footage(timeline):
    first = context_module.build(timeline)
    same = context_module.build(timeline)
    assert first.fingerprint() == same.fingerprint()

    other = context_module.build(
        a_timeline(audio_events=DEFAULT_AUDIO,
                   lines=((0.0, 18.0, "completely different words"),)))
    assert other.fingerprint() != first.fingerprint()


def test_the_context_fingerprint_changes_with_the_style_guide(timeline):
    first = context_module.build(
        timeline, style_guide=StyleGuide(text="cut fast"))
    second = context_module.build(
        timeline, style_guide=StyleGuide(text="cut slow"))
    assert first.fingerprint() != second.fingerprint()


def test_the_context_round_trips_through_a_dict(context):
    restored = DirectorContext.from_dict(context.to_dict())
    assert len(restored.segments) == len(context.segments)
    assert restored.fingerprint() == context.fingerprint()


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def test_the_prompt_tells_the_model_to_be_an_editor(context):
    built = prompt_module.build(context)
    assert "editor" in built.system
    assert "not a summariser" in built.system


def test_the_prompt_forbids_inventing_ranges(context):
    system = prompt_module.build(context).system
    assert "Never invent an id" in system
    assert "segment ids" in system


def test_the_prompt_forbids_claiming_analytics(context):
    built = prompt_module.build(context)
    assert "Never claim to know what viewers will do" in built.system
    assert "retention" in built.user


def test_the_prompt_asks_for_json_and_shows_the_shape(context):
    built = prompt_module.build(context)
    assert "single JSON object" in built.system
    # An example beats three sentences of schema description.
    payload = json.loads(prompt_module.OUTPUT_SHAPE)
    assert payload["decisions"][0]["action"] in ACTIONS
    assert payload["decisions"][0]["reason"]["category"] in REASON_CATEGORIES


def test_the_prompt_carries_the_style_guide_and_the_candidates(context):
    built = prompt_module.build(context)
    assert "Never open on walking." in built.user
    assert "# CANDIDATE RANGES" in built.user
    for segment in context.segments:
        assert f"[{segment.segment_id}]" in built.user


def test_the_prompt_states_the_runtime_target_when_there_is_one(context):
    settings = DirectorConfig(target_duration=600.0,
                              max_duration=900.0).validated()
    built = prompt_module.build(context, settings)
    assert "Target runtime" in built.user
    assert "Hard maximum runtime" in built.user

    assert "Target runtime" not in prompt_module.build(context).user


def test_the_story_layer_reaches_the_prompt(timeline):
    built = context_module.build(timeline)
    built.open_loops = [{"id": "loop_1", "question": "where are the diamonds",
                         "opened_at": 10.0, "resolved": False}]
    built.setups = [{"id": "set_1", "start": 60.0, "end": 90.0,
                     "what": "puts them in the chest", "payoff_id": "pay_1"}]
    built.payoffs = [{"id": "pay_1", "start": 90.0, "end": 110.0,
                      "what": "the chest explodes", "setup_id": "set_1"}]

    text = prompt_module.render_context(built)
    assert "NEVER ANSWERED" in text
    assert "SETUP [set_1]" in text and "-> paid off by pay_1" in text
    assert "PAYOFF [pay_1]" in text


def test_a_prompt_records_what_it_was_built_from(context):
    built = prompt_module.build(context)
    assert built.context_fingerprint == context.fingerprint()
    assert built.style_guide_fingerprint == context.style_guide.fingerprint()
    assert built.approx_tokens > 0
    assert built.characters == len(built.system) + len(built.user)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def test_the_mock_backend_decides_and_says_it_is_a_mock(context):
    built = prompt_module.build(context)
    payload = json.loads(
        backends_module.MockDirector().complete(
            system=built.system, user=built.user))

    assert payload["decisions"]
    assert "MOCK" in payload["approach"]
    assert all("MOCK" in entry["reason"]["text"]
               for entry in payload["decisions"])


def test_the_mock_backend_reads_the_candidates_out_of_the_prompt(context):
    built = prompt_module.build(context)
    payload = json.loads(backends_module.MockDirector().complete(
        system=built.system, user=built.user))
    named = {entry["segment_ids"][0] for entry in payload["decisions"]}
    assert named <= context.segment_ids
    assert named


def test_the_mock_backend_can_be_scripted(context):
    canned = answer(raw("cut"))
    model = backends_module.MockDirector(responses=[canned])
    assert model.complete(system="s", user="u") == canned


def test_building_a_backend_never_silently_falls_back_to_the_mock():
    """Replacing an editor's judgement with four rules must be deliberate."""
    assert backends_module.build_model(
        DirectorConfig(backend="mock")).name == "mock-director"
    real = backends_module.build_model(DirectorConfig(backend="banana"))
    assert real.name != "mock-director"


def test_an_answer_envelope_is_unwrapped():
    assert backends_module._text_of(
        {"choices": [{"message": {"content": "hello"}}]}) == "hello"
    # Some servers return content as typed parts.
    assert backends_module._text_of({"choices": [
        {"message": {"content": [{"type": "text", "text": "a"},
                                 {"type": "text", "text": "b"}]}}]}) == "ab"


def test_an_empty_answer_says_which_limit_was_hit():
    with pytest.raises(ModelError) as caught:
        backends_module._text_of(
            {"choices": [{"message": {"content": ""},
                          "finish_reason": "length"}]})
    assert "max-tokens" in caught.value.hint

    with pytest.raises(ModelError):
        backends_module._text_of({"choices": []})


def test_the_status_check_carries_the_fix_when_nothing_is_reachable():
    health = backends_module.check(
        DirectorConfig(base_url="http://127.0.0.1:9/v1", max_retries=0))
    assert health["ready"] is False
    assert "OpenAI-compatible" in health.get("hint", "")
    assert health["backend"] == "openai"


def test_a_mock_status_check_is_ready_and_says_what_it_is():
    health = backends_module.check(DirectorConfig(backend="mock"))
    assert health["ready"] is True
    assert "mock" in health["note"]
    assert any("MOCK" in w for w in health["config_warnings"])


def test_status_hints_name_the_actual_fix():
    assert "API_KEY" in backends_module._status_hint(401)
    assert "/v1" in backends_module._status_hint(404)
    assert "context-chars" in backends_module._status_hint(413)
    assert "Rate limited" in backends_module._status_hint(429)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_a_clean_answer_becomes_decisions():
    ctx = a_context()
    decisions, approach, discarded, warnings = parse_module.parse_response(
        answer(raw("keep", ("s_1",)), raw("cut", ("s_2",)),
               approach="tight cut"),
        ctx,
    )
    assert [d.action for d in decisions] == ["keep", "cut"]
    assert approach == "tight cut"
    assert discarded == []
    assert warnings == []


def test_times_come_from_the_context_not_from_the_model():
    """The whole anti-hallucination guarantee, in one assertion."""
    ctx = a_context()
    decisions, *_ = parse_module.parse_response(
        answer(raw("keep", ("s_2",), start=999.0, end=1234.0)), ctx)

    assert decisions[0].start == 20.0
    assert decisions[0].end == 40.0


def test_an_invented_segment_id_is_discarded_with_a_reason():
    ctx = a_context()
    with pytest.raises(ModelError) as caught:
        parse_module.parse_response(answer(raw("keep", ("s_9999",))), ctx)
    assert "no usable decisions" in caught.value.message
    assert "invented segment ids" in caught.value.hint


def test_a_partly_invented_decision_keeps_the_real_segments():
    ctx = a_context()
    decisions, _approach, _discarded, _warnings = parse_module.parse_response(
        answer(raw("keep", ("s_1", "s_9999"))), ctx)

    assert decisions[0].segment_ids == ["s_1"]
    assert any("unknown segment id" in note
               for note in decisions[0].safety_notes)


def test_inventing_ids_is_counted_and_named_in_the_warnings():
    ctx = a_context()
    decisions, _a, discarded, warnings = parse_module.parse_response(
        answer(raw("keep", ("s_1",)), raw("cut", ("nope",))), ctx)

    assert len(decisions) == 1
    assert len(discarded) == 1
    assert any("inventing ranges" in w for w in warnings)


def test_an_answer_wrapped_in_a_markdown_fence_still_parses():
    ctx = a_context()
    fenced = "Here you go:\n```json\n" + answer(raw()) + "\n```\nHope that "\
             "helps!"
    decisions, *_ = parse_module.parse_response(fenced, ctx)
    assert len(decisions) == 1


def test_a_trailing_comma_is_repaired():
    """The one failure that is always formatting in a long array."""
    ctx = a_context()
    broken = '{"decisions": [{"segment_ids": ["s_1"], "action": "keep",' \
             ' "confidence": 0.8, "evidence": ["s_1"],' \
             ' "reason": {"category": "pacing", "text": "fine"}},]}'
    decisions, *_ = parse_module.parse_response(broken, ctx)
    assert len(decisions) == 1


def test_prose_with_no_json_is_a_typed_failure():
    with pytest.raises(ModelError) as caught:
        parse_module.parse_response(
            "I think you should keep the good bits.", a_context())
    assert "JSON" in caught.value.message


def test_an_empty_response_is_a_typed_failure():
    with pytest.raises(ModelError):
        parse_module.parse_response("", a_context())
    with pytest.raises(ModelError):
        parse_module.parse_response("   ", a_context())


def test_json_with_no_decision_list_is_a_typed_failure():
    with pytest.raises(ModelError) as caught:
        parse_module.parse_response('{"thoughts": "nice footage"}',
                                    a_context())
    assert "no decision list" in caught.value.message


def test_alternative_keys_for_the_decision_list_are_accepted():
    ctx = a_context()
    decisions, *_ = parse_module.parse_response(
        json.dumps({"edits": [raw()]}), ctx)
    assert len(decisions) == 1


def test_a_bare_string_of_segment_ids_is_one_id_not_a_list_of_letters():
    ctx = a_context()
    # The bug this prevents: a string iterated as characters produces eleven
    # decisions about segments named "s", "_", "1".
    decisions, *_ = parse_module.parse_response(
        json.dumps({"decisions": [{
            "segment_ids": "s_1", "action": "keep", "confidence": 0.8,
            "evidence": ["s_1"],
            "reason": {"category": "pacing", "text": "fine"},
        }]}), ctx)
    assert decisions[0].segment_ids == ["s_1"]

    assert parse_module._as_ids("a, b; c") == ["a", "b", "c"]
    assert parse_module._as_ids([{"id": "x"}, "y"]) == ["x", "y"]
    assert parse_module._as_ids(None) == []


def test_a_decision_that_is_not_an_object_is_discarded():
    ctx = a_context()
    decisions, _a, discarded, _w = parse_module.parse_response(
        json.dumps({"decisions": ["keep everything", raw()]}), ctx)
    assert len(decisions) == 1
    assert discarded[0]["why"] == "not an object"


def test_an_unknown_action_is_discarded_rather_than_guessed_at():
    ctx = a_context()
    decisions, _a, discarded, _w = parse_module.parse_response(
        json.dumps({"decisions": [raw(action="obliterate"), raw()]}), ctx)
    assert len(decisions) == 1
    assert "unknown action" in discarded[0]["why"]


def test_the_same_decision_twice_is_counted_once():
    ctx = a_context()
    decisions, _a, discarded, _w = parse_module.parse_response(
        answer(raw("keep", ("s_1",)), raw("keep", ("s_1",))), ctx)
    assert len(decisions) == 1
    assert "duplicate" in discarded[0]["why"]


def test_a_speed_up_with_no_speed_gets_the_default_and_records_it():
    ctx = a_context()
    settings = DirectorConfig(default_speed=2.0).validated()
    decisions, *_ = parse_module.parse_response(
        answer(raw("speed_up", ("s_1",))), ctx, config=settings)

    assert decisions[0].speed == 2.0
    assert any("no speed given" in note for note in decisions[0].safety_notes)


def test_a_shorten_is_clamped_inside_the_segments_it_names():
    ctx = a_context()
    decisions, *_ = parse_module.parse_response(
        answer(raw("shorten", ("s_2",), out_start=0.0, out_end=9999.0)), ctx)

    assert decisions[0].out_start >= 20.0
    assert decisions[0].out_end <= 40.0
    assert any("clamped" in note for note in decisions[0].safety_notes)


def test_a_shorten_given_as_an_offset_is_understood():
    ctx = a_context()
    decisions, *_ = parse_module.parse_response(
        answer(raw("shorten", ("s_2",), out_start=2.0, out_end=8.0)), ctx)

    assert decisions[0].out_start == 22.0
    assert decisions[0].out_end == 28.0
    assert any("offsets" in note for note in decisions[0].safety_notes)


def test_a_shorten_to_nothing_is_read_as_a_cut():
    ctx = a_context()
    decisions, *_ = parse_module.parse_response(
        answer(raw("shorten", ("s_2",), out_start=25.0, out_end=25.0)), ctx)
    assert decisions[0].action == "cut"
    assert any("read as a cut" in note for note in decisions[0].safety_notes)


def test_a_decision_crossing_two_files_uses_only_the_first():
    ctx = a_context(segments=[
        ContextSegment(segment_id="s_1", asset_id="a_one",
                       source_file="/a.mp4", start=0, end=10, said="hi"),
        ContextSegment(segment_id="s_2", asset_id="a_two",
                       source_file="/b.mp4", start=0, end=10, said="hi"),
    ])
    decisions, *_ = parse_module.parse_response(
        answer(raw("keep", ("s_1", "s_2"))), ctx)

    assert decisions[0].asset_id == "a_one"
    assert decisions[0].segment_ids == ["s_1"]
    assert any("more than one source file" in note
               for note in decisions[0].safety_notes)


def test_confidence_is_capped_when_only_one_channel_covers_the_range():
    """Session 8's rule: one channel cannot corroborate itself."""
    ctx = a_context(segments=[
        ContextSegment(segment_id="s_1", asset_id="a_test", start=0, end=10,
                       said="", audio=[], importance="unknown"),
    ])
    decisions, *_ = parse_module.parse_response(
        answer(raw("keep", ("s_1",), confidence=0.95)), ctx)

    assert decisions[0].confidence == SINGLE_CHANNEL_CAP
    assert any("capped" in note for note in decisions[0].safety_notes)


def test_confidence_is_left_alone_when_channels_agree():
    decisions, *_ = parse_module.parse_response(
        answer(raw("keep", ("s_2",), confidence=0.95)), a_context())
    assert decisions[0].confidence == 0.95


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def review(decisions, ctx=None, **settings):
    return safety_module.review(
        decisions, ctx or a_context(),
        config=DirectorConfig(**settings).validated())


def test_nothing_is_accepted_without_the_safety_pass_saying_so():
    decision = a_decision()
    assert decision.accepted is False
    decisions, ranges, record = review([decision])
    assert decisions[0].accepted is True
    assert record.proposed == 1 and record.accepted == 1
    assert len(ranges) == 1


def test_a_decision_naming_nothing_is_rejected():
    decisions, _ranges, record = review([a_decision(segment_ids=())])
    assert not decisions[0].accepted
    assert record.of_check("resolvable")


def test_a_decision_naming_only_unknown_segments_is_rejected():
    decision = a_decision(segment_ids=("s_gone",))
    decisions, _ranges, record = review([decision])
    assert not decisions[0].accepted
    assert "invented" in decisions[0].rejected_reason


def test_a_reversed_range_is_rejected():
    decisions, _r, record = review([a_decision(start=40.0, end=10.0)])
    assert not decisions[0].accepted
    assert record.of_check("valid_range")


def test_a_range_reaching_outside_its_segments_is_clamped_not_dropped():
    decision = a_decision("shorten", start=0.0, end=20.0,
                          out_start=-5.0, out_end=999.0)
    decisions, ranges, record = review([decision])

    assert decisions[0].accepted and decisions[0].modified
    assert decisions[0].out_start == 0.0 and decisions[0].out_end == 20.0
    assert record.of_check("valid_range")
    assert len(ranges) == 1


def test_an_impossible_speed_is_reset_to_1x():
    decisions, _r, _record = review([a_decision("speed_up", speed=40.0)])
    assert decisions[0].speed == 1.0
    assert decisions[0].modified


def test_a_decision_below_the_confidence_floor_changes_no_frame():
    decisions, ranges, record = review([a_decision(confidence=0.2)])
    assert decisions[0].action == "needs_human_review"
    assert decisions[0].accepted, "recorded, not thrown away"
    assert ranges == []
    assert record.of_check("confidence")


def test_a_confident_decision_is_left_alone():
    decisions, ranges, _record = review(
        [a_decision(confidence=MIN_ACTIONABLE_CONFIDENCE)])
    assert decisions[0].action == "keep"
    assert len(ranges) == 1


def test_a_decision_with_no_reason_becomes_a_note_for_a_person():
    decision = a_decision(reason=DirectorReason(category="pacing", text=""))
    decisions, _r, record = review([decision])
    assert decisions[0].action == "needs_human_review"
    assert record.of_check("evidence")


def test_a_reasoned_decision_citing_nothing_is_warned_about_not_refused():
    decision = a_decision(evidence=[])
    decisions, ranges, record = review([decision])
    assert decisions[0].accepted and decisions[0].action == "keep"
    assert len(ranges) == 1
    assert record.of_check("evidence")[0].severity == "warn"


def test_speech_is_never_sped_up():
    """Sped-up dialogue is unusable, full stop."""
    decision = a_decision("speed_up", segment_ids=("s_1",), speed=2.0)
    decisions, ranges, record = review([decision])

    assert decisions[0].action == "keep", "the judgement survives"
    assert decisions[0].speed == 1.0, "the remedy does not"
    assert decisions[0].accepted
    assert record.of_check("speech_speed")
    assert ranges[0].speed == 1.0


def test_silent_footage_may_be_sped_up():
    ctx = a_context(segments=[
        ContextSegment(segment_id="s_q", asset_id="a_test", start=0, end=30,
                       said="", audio=["low_energy"], importance="setup"),
    ])
    decision = a_decision("speed_up", segment_ids=("s_q",), start=0.0,
                          end=30.0, speed=2.0)
    decisions, ranges, _record = review([decision], ctx)

    assert decisions[0].action == "speed_up"
    assert ranges[0].speed == 2.0
    assert ranges[0].cut_duration == 15.0


def test_cutting_the_payoff_the_episode_built_to_is_refused():
    ctx = a_context(payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0,
                              "what": "the diamonds"}])
    decision = a_decision("cut", segment_ids=("s_3",), start=40.0, end=60.0)
    decisions, _r, record = review([decision], ctx)

    assert not decisions[0].accepted
    assert "pay_1" in decisions[0].rejected_reason
    assert record.of_check("protected_payoff")


def test_retiming_the_payoff_is_downgraded_to_a_hold():
    ctx = a_context(
        segments=[ContextSegment(segment_id="s_p", asset_id="a_test",
                                 source_file="/footage/ep12.mp4",
                                 start=40.0, end=60.0, said="",
                                 audio=["possible_laughter"],
                                 importance="payoff")],
        payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0,
                  "what": "the diamonds"}],
    )
    decision = a_decision("speed_up", segment_ids=("s_p",), start=40.0,
                          end=60.0, speed=2.0)
    decisions, ranges, _record = review([decision], ctx)

    assert decisions[0].action == "hold"
    assert decisions[0].speed == 1.0
    assert ranges[0].protected


def test_anything_that_keeps_a_payoff_ends_up_holding_it():
    """A plain keep over a payoff is still unprotected, so a later pass could
    zoom or duck it."""
    ctx = a_context(payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0,
                              "what": "the diamonds"}])
    decision = a_decision("keep", segment_ids=("s_3",), start=40.0, end=60.0)
    decisions, ranges, record = review([decision], ctx)

    assert decisions[0].action == "hold"
    assert ranges[0].protected
    assert record.of_check("protected_payoff")


def test_trimming_the_payoff_keeps_it_whole():
    ctx = a_context(payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0,
                              "what": "x"}])
    decision = a_decision("shorten", segment_ids=("s_3",), start=40.0,
                          end=60.0, out_start=50.0, out_end=55.0)
    decisions, _r, _record = review([decision], ctx)

    assert decisions[0].action == "hold"
    assert (decisions[0].out_start, decisions[0].out_end) == (40.0, 60.0)


def test_the_climax_is_protected_like_a_payoff():
    ctx = a_context(climax={"id": "cli_1", "start": 40.0, "end": 60.0})
    decisions, _r, record = review(
        [a_decision("cut", segment_ids=("s_3",), start=40.0, end=60.0)], ctx)
    assert not decisions[0].accepted
    assert record.of_check("protected_payoff")


def test_cutting_the_setup_for_a_kept_payoff_is_refused():
    """The check that most justifies this whole layer.

    No local heuristic can see it: the setup looks like nothing, and the only
    reason to keep it sits twenty minutes later.
    """
    ctx = a_context(
        setups=[{"id": "set_1", "start": 0.0, "end": 20.0,
                 "what": "puts them in the chest", "payoff_id": "pay_1"}],
        payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0,
                  "what": "the chest explodes", "setup_id": "set_1"}],
    )
    keep_payoff = a_decision("payoff", segment_ids=("s_3",), start=40.0,
                             end=60.0)
    cut_setup = a_decision("cut", segment_ids=("s_1",), start=0.0, end=20.0)

    decisions, _ranges, record = review([keep_payoff, cut_setup], ctx)

    assert decisions[0].accepted
    assert not decisions[1].accepted
    assert "set_1" in decisions[1].rejected_reason
    assert "arrive from nowhere" in decisions[1].rejected_reason
    assert record.of_check("required_setup")


def test_cutting_a_setup_whose_payoff_was_also_cut_is_allowed():
    """Cutting a whole thread is a legitimate editing decision."""
    ctx = a_context(
        setups=[{"id": "set_1", "start": 0.0, "end": 20.0, "what": "x",
                 "payoff_id": "pay_1"}],
        payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0, "what": "y",
                  "setup_id": "set_1"}],
    )
    # No decision keeps the payoff, so its setup is not required.
    decisions, _r, _record = review(
        [a_decision("cut", segment_ids=("s_1",), start=0.0, end=20.0)], ctx)
    assert decisions[0].accepted


def test_a_setup_with_no_payoff_is_not_protected():
    ctx = a_context(setups=[{"id": "set_1", "start": 0.0, "end": 20.0,
                             "what": "never paid off", "payoff_id": ""}])
    decisions, _r, _record = review(
        [a_decision("cut", segment_ids=("s_1",), start=0.0, end=20.0)], ctx)
    assert decisions[0].accepted


def test_two_kept_ranges_over_the_same_footage_do_not_both_survive():
    first = a_decision("keep", segment_ids=("s_1",), start=0.0, end=20.0)
    second = a_decision("keep", segment_ids=("s_1",), start=0.0, end=20.0)
    second.decision_id = "d_other"

    decisions, ranges, record = review([first, second])
    assert len([d for d in decisions if d.accepted and d.keeps_footage]) == 1
    assert len(ranges) == 1
    assert record.of_check("overlap")


def test_a_partial_overlap_is_trimmed_rather_than_dropped():
    first = a_decision("keep", segment_ids=("s_1",), start=0.0, end=20.0)
    second = a_decision("keep", segment_ids=("s_2",), start=10.0, end=40.0)
    second.out_start, second.out_end = 10.0, 40.0

    decisions, ranges, record = review([first, second])
    assert all(d.accepted for d in decisions)
    assert len(ranges) == 2
    assert record.of_check("overlap")[0].severity == "modify"
    assert ranges[0].end <= ranges[1].start + 0.01


def test_a_protected_range_wins_a_collision():
    ordinary = a_decision("keep", segment_ids=("s_1",), start=0.0, end=20.0)
    protected = a_decision("payoff", segment_ids=("s_1",), start=0.0,
                           end=20.0)
    protected.decision_id = "d_payoff"

    decisions, ranges, _record = review([ordinary, protected])
    survivors = [d for d in decisions if d.accepted and d.keeps_footage]
    assert len(survivors) == 1
    assert survivors[0].action == "payoff"
    assert ranges[0].protected


def test_more_hooks_than_the_ceiling_become_ordinary_clips():
    hooks = []
    for index, segment_id in enumerate(("s_1", "s_2", "s_3")):
        hook = a_decision("hook", segment_ids=(segment_id,),
                          start=index * 20.0, end=index * 20.0 + 20.0,
                          priority=0.9 - index * 0.1)
        hook.decision_id = f"d_hook_{index}"
        hooks.append(hook)

    decisions, _ranges, record = review(hooks, max_hooks_in_cut=1)
    actions = [d.action for d in decisions]
    assert actions.count("hook") == 1
    assert actions.count("keep") == 2
    assert record.of_check("hook_ceiling")
    # The best one survives, not the first one.
    assert decisions[0].action == "hook"


def test_more_callbacks_than_the_ceiling_become_ordinary_clips():
    callbacks = []
    for index, segment_id in enumerate(("s_1", "s_2", "s_3")):
        entry = a_decision("callback", segment_ids=(segment_id,),
                           start=index * 20.0, end=index * 20.0 + 20.0)
        entry.decision_id = f"d_cb_{index}"
        callbacks.append(entry)

    decisions, _r, record = review(callbacks, max_callbacks_in_cut=1)
    assert [d.action for d in decisions].count("callback") == 1
    assert record.of_check("callback_ceiling")


def test_too_much_grind_is_trimmed_least_defensible_first():
    ctx = a_context(segments=[
        ContextSegment(segment_id=f"g_{i}", asset_id="a_test",
                       start=i * 60.0, end=i * 60.0 + 60.0, said="",
                       audio=["low_energy"], importance="setup")
        for i in range(4)
    ])
    grind = []
    for index in range(4):
        entry = a_decision("speed_up", segment_ids=(f"g_{index}",),
                           start=index * 60.0, end=index * 60.0 + 60.0,
                           speed=2.0, priority=0.1 * index)
        entry.decision_id = f"d_g_{index}"
        grind.append(entry)

    # 4 x 60s at 2x = 120s of cut, against a 60s budget.
    decisions, _ranges, record = review(grind, ctx, max_grind_seconds=60.0)
    assert record.of_check("grind_budget")
    kept = [d for d in decisions if d.accepted]
    assert len(kept) < 4
    # The lowest-priority ones went.
    assert decisions[0].accepted is False


def test_an_ordinary_keep_is_not_counted_as_grind():
    """"pacing" is the natural category for an ordinary keep.

    Counting it against the grind budget made the budget reject most of a
    normal cut.
    """
    ctx = a_context(segments=[
        ContextSegment(segment_id=f"k_{i}", asset_id="a_test",
                       start=i * 60.0, end=i * 60.0 + 60.0, said="talking",
                       audio=["sudden_reaction"], importance="setup")
        for i in range(4)
    ])
    decisions = []
    for index in range(4):
        entry = a_decision("keep", segment_ids=(f"k_{index}",),
                           start=index * 60.0, end=index * 60.0 + 60.0,
                           reason=DirectorReason(category="pacing",
                                                 text="reads well"))
        entry.decision_id = f"d_k_{index}"
        decisions.append(entry)

    reviewed, _ranges, record = review(decisions, ctx, max_grind_seconds=60.0)
    assert all(entry.accepted for entry in reviewed)
    assert not record.of_check("grind_budget")


def test_a_runtime_cap_drops_the_least_important_first_and_never_the_payoff():
    ctx = a_context(segments=[
        ContextSegment(segment_id=f"s_{i}", asset_id="a_test",
                       start=i * 60.0, end=i * 60.0 + 60.0, said="talking",
                       audio=["sudden_reaction"], importance="setup")
        for i in range(4)
    ])
    decisions = []
    for index in range(4):
        action = "payoff" if index == 3 else "keep"
        entry = a_decision(action, segment_ids=(f"s_{index}",),
                           start=index * 60.0, end=index * 60.0 + 60.0,
                           priority=0.9 if index == 3 else 0.2 + index * 0.1,
                           reason=DirectorReason(category="viewer_curiosity",
                                                 text="worth keeping"))
        entry.decision_id = f"d_{index}"
        decisions.append(entry)

    reviewed, ranges, record = review(decisions, ctx, max_duration=120.0)

    total = sum(item.cut_duration for item in ranges)
    assert total <= 120.0
    assert record.of_check("duration")
    assert any("trimmed to fit" in w for w in record.warnings)
    # The payoff is protected, so it is dropped last -- and here, never.
    assert reviewed[3].accepted, "the payoff survived the runtime cap"


def test_a_cut_inside_the_runtime_cap_is_left_alone():
    _decisions, ranges, record = review([a_decision()], max_duration=600.0)
    assert len(ranges) == 1
    assert not record.of_check("duration")


def test_hooks_are_ordered_to_the_front_of_the_cut():
    hook = a_decision("hook", segment_ids=("s_3",), start=40.0, end=60.0,
                      order=0)
    hook.decision_id = "d_hook"
    ordinary = a_decision("keep", segment_ids=("s_1",), start=0.0, end=20.0)

    _decisions, ranges, _record = review([ordinary, hook])
    assert ranges[0].is_hook
    assert ranges[0].start == 40.0, "minute nine plays first"
    assert ranges[1].start == 0.0


def test_a_rejected_decision_is_kept_with_the_check_that_refused_it():
    """Nothing is deleted -- Session 2's rule, in a new layer."""
    ctx = a_context(payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0,
                              "what": "x"}])
    decisions, _r, record = review(
        [a_decision("cut", segment_ids=("s_3",), start=40.0, end=60.0)], ctx)

    assert len(decisions) == 1, "still in the list"
    assert decisions[0].rejected_reason
    assert record.violations[0].check == "protected_payoff"
    assert record.violations[0].decision_id == decisions[0].decision_id


def test_the_review_counts_what_it_did():
    decisions = [
        a_decision("keep", segment_ids=("s_1",)),
        a_decision("cut", segment_ids=("s_2",), start=20.0, end=40.0),
        a_decision("keep", segment_ids=("s_3",), start=40.0, end=60.0,
                   confidence=0.1),
    ]
    decisions[1].decision_id = "d_2"
    decisions[2].decision_id = "d_3"

    _reviewed, _ranges, record = review(decisions)
    assert record.proposed == 3
    assert record.accepted + record.rejected == 3
    assert 0.0 <= record.acceptance_rate <= 1.0
    assert set(record.checks_run) == set(safety_module.CHECKS)


def test_an_episode_with_no_transcript_explains_why_nothing_was_actionable():
    ctx = a_context(
        segments=[
            ContextSegment(segment_id=f"s_{i}", asset_id="a_test",
                           start=i * 20.0, end=i * 20.0 + 20.0, said="",
                           audio=[], importance="unknown")
            for i in range(4)
        ],
        sources={"timeline": True, "transcript": False},
    )
    decisions = []
    for index in range(4):
        entry = a_decision("keep", segment_ids=(f"s_{index}",),
                           start=index * 20.0, end=index * 20.0 + 20.0,
                           confidence=SINGLE_CHANNEL_CAP)
        entry.decision_id = f"d_{index}"
        entry.safety_notes.append("confidence capped at 0.45")
        decisions.append(entry)

    _reviewed, _ranges, record = review(decisions, ctx)
    assert any("no transcript" in w for w in record.warnings)
    assert any("transcribe folder" in w for w in record.warnings)


# ---------------------------------------------------------------------------
# The whole pass
# ---------------------------------------------------------------------------

def test_a_pass_produces_a_plan_with_ranges(config, timeline):
    ctx = context_module.build(timeline)
    ids = [segment.segment_id for segment in ctx.segments]
    model = ScriptedModel(answer(
        raw("keep", (ids[0],)),
        raw("cut", (ids[1],)),
    ))
    plan = run_module.plan(config, ctx, model=model,
                           settings=DirectorConfig().validated())

    assert plan.ok
    assert plan.failure is None
    assert len(plan.decisions) == 2
    assert plan.ranges
    assert plan.safety.proposed == 2


def test_a_pass_records_the_prompt_it_sent(config, context):
    model = ScriptedModel(answer(raw("keep", (context.segments[0].segment_id,))))
    plan = run_module.plan(config, context, model=model)

    assert plan.prompt is not None
    assert plan.prompt.context_fingerprint == context.fingerprint()
    assert "CANDIDATE RANGES" in plan.prompt.user


def test_an_unreachable_model_is_a_failure_not_an_exception(config, context):
    model = ScriptedModel(fail=ModelError(
        "Could not reach the director model after 3 attempt(s)",
        hint="start a server"))
    plan = run_module.plan(config, context, model=model)

    assert not plan.ok
    assert plan.failure.stage == "no_backend"
    assert plan.failure.recoverable
    assert plan.ranges == []
    assert plan.decisions == []


def test_an_unparseable_answer_is_a_failure_carrying_what_was_said(
        config, context):
    model = ScriptedModel("I would keep the good bits and cut the rest.")
    plan = run_module.plan(config, context, model=model)

    assert plan.failure.stage == "invalid_json"
    assert "keep the good bits" in plan.failure.response_excerpt
    assert plan.ranges == []


def test_an_answer_with_no_usable_decisions_is_its_own_failure(
        config, context):
    model = ScriptedModel(answer(raw("keep", ("s_invented",))))
    plan = run_module.plan(config, context, model=model)

    assert plan.failure.stage == "no_decisions"
    assert "show-rejected" in plan.failure.hint


def test_a_plan_where_everything_was_rejected_says_so(config, timeline):
    ctx = context_module.build(timeline)
    ctx.payoffs = [{"id": "pay_1", "start": 0.0, "end": 1000.0, "what": "all"}]
    ids = [segment.segment_id for segment in ctx.segments]
    model = ScriptedModel(answer(raw("cut", (ids[0],)), raw("cut", (ids[1],))))

    plan = run_module.plan(config, ctx, model=model)
    assert plan.failure.stage == "safety"
    assert "show-rejected" in plan.failure.hint
    assert plan.decisions, "the rejections are kept"


def test_an_empty_context_fails_before_any_model_is_asked(config):
    model = ScriptedModel()
    plan = run_module.plan(config, DirectorContext(), model=model)

    assert plan.failure.stage == "empty_context"
    assert not plan.failure.recoverable
    assert model.calls == [], "nothing was asked"


def test_heuristic_mode_runs_no_model(config, context):
    model = ScriptedModel()
    plan = run_module.plan(config, context, model=model,
                           settings=DirectorConfig(mode="heuristic"))

    assert plan.failure.stage == "config"
    assert model.calls == []


def test_a_mock_pass_says_it_is_a_mock_everywhere(config, context):
    plan = run_module.plan(
        config, context, settings=DirectorConfig(backend="mock").validated())

    assert plan.mock
    assert any("MOCK" in w for w in plan.warnings)
    assert "MOCK DIRECTOR" in report_module.render(plan)
    assert json.loads(json.dumps(plan.to_dict()))["mock"] is True


def test_the_answer_is_cached_and_reused(config, context, cache):
    model = ScriptedModel(
        answer(raw("keep", (context.segments[0].segment_id,))))
    settings = DirectorConfig().validated()

    first = run_module.plan(config, context, model=model, cache=cache,
                            settings=settings)
    assert first.ok
    assert len(model.calls) == 1

    second = run_module.plan(config, context, model=model, cache=cache,
                             settings=settings)
    assert second.cached
    assert len(model.calls) == 1, "no second call"
    assert len(second.decisions) == len(first.decisions)


def test_force_asks_again(config, context, cache):
    reply = answer(raw("keep", (context.segments[0].segment_id,)))
    model = ScriptedModel(reply, reply)
    settings = DirectorConfig().validated()

    run_module.plan(config, context, model=model, cache=cache,
                    settings=settings)
    run_module.plan(config, context, model=model, cache=cache,
                    settings=settings, force=True)
    assert len(model.calls) == 2


def test_a_mock_answer_is_never_cached(config, context, cache):
    settings = DirectorConfig(backend="mock").validated()
    run_module.plan(config, context, cache=cache, settings=settings)
    key = store_module.cache_key(cache, context, settings)
    assert store_module.cached_response(cache, key, settings=settings) is None


def test_the_cache_misses_when_the_style_guide_changes(config, timeline,
                                                       cache):
    settings = DirectorConfig().validated()
    first = context_module.build(timeline,
                                 style_guide=StyleGuide(text="cut fast"))
    second = context_module.build(timeline,
                                  style_guide=StyleGuide(text="cut slow"))
    assert store_module.cache_key(cache, first, settings) != \
        store_module.cache_key(cache, second, settings)


def test_the_cache_stores_text_not_parsed_decisions(config, context, cache):
    """So fixing a parser bug fixes everything already cached."""
    model = ScriptedModel(
        answer(raw("keep", (context.segments[0].segment_id,))))
    settings = DirectorConfig().validated()
    run_module.plan(config, context, model=model, cache=cache,
                    settings=settings)

    key = store_module.cache_key(cache, context, settings)
    stored = store_module.cached_response(cache, key, settings=settings)
    assert isinstance(stored, str)
    assert "decisions" in stored


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_a_pass_writes_everything_it_produced(config, context):
    model = ScriptedModel(
        answer(raw("keep", (context.segments[0].segment_id,))))
    plan = run_module.plan(config, context, model=model)
    written = run_module.persist(config, plan, context)

    for key in ("context", "prompt", "plan", "report"):
        assert Path(written[key]).exists(), key
    # The prompt is text, because reading it out of a JSON string field is
    # unpleasant enough that people do not.
    assert "=== SYSTEM ===" in Path(written["prompt"]).read_text("utf-8")


def test_a_failed_pass_is_written_too(config, context):
    plan = run_module.plan(config, context, model=ScriptedModel("nonsense"))
    written = run_module.persist(config, plan, context)

    assert Path(written["plan"]).exists()
    assert "FAILED" in Path(written["report"]).read_text("utf-8")


def test_a_plan_reads_back_from_disk(config, context):
    model = ScriptedModel(
        answer(raw("keep", (context.segments[0].segment_id,))))
    plan = run_module.plan(config, context, model=model)
    run_module.persist(config, plan, context)

    loaded = store_module.load_plan(config)
    assert len(loaded.decisions) == len(plan.decisions)
    assert loaded.ranges[0].start == plan.ranges[0].start


def test_a_missing_plan_says_how_to_build_one(config):
    with pytest.raises(EditingError) as caught:
        store_module.load_plan(config)
    assert "director plan" in caught.value.hint
    assert store_module.plan_or_none(config) is None


def test_an_unreadable_plan_is_none_rather_than_an_exception(config):
    store_module.plan_path(config).parent.mkdir(parents=True, exist_ok=True)
    store_module.plan_path(config).write_text("{ broken", encoding="utf-8")
    assert store_module.plan_or_none(config) is None


# ---------------------------------------------------------------------------
# Becoming a rough cut
# ---------------------------------------------------------------------------

def a_plan_with(*decisions) -> DirectorPlan:
    reviewed, ranges, record = safety_module.review(
        list(decisions), a_context(), config=DirectorConfig().validated())
    return DirectorPlan(decisions=reviewed, ranges=ranges, safety=record)


def test_accepted_ranges_become_selected_ranges():
    plan = a_plan_with(a_decision("keep", segment_ids=("s_1",)))
    selected = convert_module.to_selected(plan)

    assert len(selected) == 1
    assert selected[0].asset_id == "a_test"
    assert selected[0].start == 0.0 and selected[0].end == 20.0
    assert "director[" in selected[0].notes


def test_the_directors_own_sentence_survives_onto_the_clip():
    decision = a_decision("hold", segment_ids=("s_3",), start=40.0, end=60.0,
                          reason=DirectorReason(
                              category="setup_payoff",
                              text="this is what the episode built to"))
    selected = convert_module.to_selected(a_plan_with(decision))
    assert "this is what the episode built to" in selected[0].notes


def test_a_rejected_decision_never_becomes_a_range():
    ctx = a_context(payoffs=[{"id": "p", "start": 40.0, "end": 60.0,
                              "what": "x"}])
    reviewed, ranges, record = safety_module.review(
        [a_decision("cut", segment_ids=("s_3",), start=40.0, end=60.0)],
        ctx, config=DirectorConfig().validated())
    plan = DirectorPlan(decisions=reviewed, ranges=ranges, safety=record)

    assert convert_module.to_selected(plan) == []


def test_hybrid_fills_what_the_director_did_not_mention(timeline):
    plan = a_plan_with(a_decision("keep", segment_ids=("s_1",),
                                  start=0.0, end=20.0))
    ranges, notes = convert_module.merged_with_heuristic(plan, timeline)

    assert notes["from_director"] == 1
    assert notes["from_heuristic"] > 0, "the rest fell through to the rules"
    assert len(ranges) == notes["from_director"] + notes["from_heuristic"]
    assert any("the director said nothing about this range" in entry.notes
               for entry in ranges)


def test_hybrid_does_not_re_add_footage_the_director_cut(timeline):
    """The entire point of asking it."""
    ctx = context_module.build(timeline)
    first = ctx.segments[0]
    decision = a_decision("cut", segment_ids=(first.segment_id,),
                          start=first.start, end=first.end)
    reviewed, ranges, record = safety_module.review(
        [decision], ctx, config=DirectorConfig().validated())
    plan = DirectorPlan(decisions=reviewed, ranges=ranges, safety=record)

    merged, notes = convert_module.merged_with_heuristic(plan, timeline)
    assert notes["heuristic_dropped"] >= 1
    covered = [
        entry for entry in merged
        if entry.start < first.end and entry.end > first.start
    ]
    assert not covered, "the cut range did not come back"


def test_director_mode_builds_a_cut_from_director_ranges_only(timeline):
    plan = a_plan_with(a_decision("keep", segment_ids=("s_1",),
                                  start=0.0, end=20.0))
    cut = build_rough_cut(
        timeline, options=RoughCutOptions(mode="director"),
        director_plan=plan, validate=False,
    )
    assert len(cut.placements) == 1
    assert any("director pass" in line for line in cut.explanation)


def test_hybrid_mode_builds_a_cut_from_both(timeline):
    plan = a_plan_with(a_decision("keep", segment_ids=("s_1",),
                                  start=0.0, end=20.0))
    cut = build_rough_cut(
        timeline, options=RoughCutOptions(mode="hybrid"),
        director_plan=plan, validate=False,
    )
    assert len(cut.placements) > 1
    assert any("hybrid mode" in line for line in cut.explanation)


def test_director_mode_without_a_plan_falls_back_and_says_so(timeline):
    """Silently producing a threshold cut under a director label is the worst
    outcome available here."""
    cut = build_rough_cut(
        timeline, options=RoughCutOptions(mode="director"),
        director_plan=None, validate=False,
    )
    assert cut.placements, "the heuristic still works"
    assert any("no usable director plan" in line for line in cut.explanation)


def test_a_plan_with_no_ranges_produces_a_threshold_cut_that_says_so(
        timeline):
    """The one outcome this layer is not allowed to produce.

    A blocked director pass still has a plan -- its rejections are worth
    keeping -- and keying on the object's existence rather than on its ranges
    made a cut the thresholds chose entirely get reported as a director cut.
    """
    empty = DirectorPlan(decisions=[a_decision("keep", accepted=False)],
                         ranges=[])
    cut = build_rough_cut(
        timeline, options=RoughCutOptions(mode="hybrid"),
        director_plan=empty, validate=False,
    )
    assert cut.placements
    assert any("no usable director plan" in line for line in cut.explanation)
    assert not any("hybrid mode" in line for line in cut.explanation)


def test_the_heuristic_path_is_unchanged(timeline):
    """Session 3, byte for byte, when nothing asks for anything else."""
    before = build_rough_cut(timeline, validate=False)
    after = build_rough_cut(
        timeline, options=RoughCutOptions(mode="heuristic"), validate=False)

    assert len(before.placements) == len(after.placements)
    assert [p.source_in for p in before.placements] == \
        [p.source_in for p in after.placements]
    assert any("rule-based selector" in line for line in after.explanation)


def test_a_director_cut_still_goes_through_every_existing_guard(timeline):
    plan = a_plan_with(a_decision("keep", segment_ids=("s_1",),
                                  start=0.0, end=20.0))
    cut = build_rough_cut(
        timeline, options=RoughCutOptions(mode="director"),
        director_plan=plan, validate=True,
    )
    # The same dry run, the same operations, the same scratch guarantee.
    assert cut.dry_run_passed
    assert cut.on_scratch
    assert cut.ops


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def test_the_comparison_measures_agreement_not_quality(timeline):
    from editing.roughcut.select import select_ranges

    plan = a_plan_with(a_decision("keep", segment_ids=("s_1",),
                                  start=0.0, end=20.0))
    heuristic = select_ranges(timeline)
    payload = compare_module.compare(plan, heuristic)

    assert 0.0 <= payload["difference"]["agreement"] <= 1.0
    assert "director" in payload and "heuristic" in payload
    # No metric claims one is better.
    assert "better" not in json.dumps(payload).lower()
    assert payload["not_measured"]


def test_the_comparison_names_decisions_no_threshold_could_make(timeline):
    from editing.roughcut.select import select_ranges

    story = a_decision(
        "setup", segment_ids=("s_1",), start=0.0, end=20.0,
        reason=DirectorReason(category="setup_payoff",
                              text="needed for the chest at 31 minutes"))
    plan = a_plan_with(story)
    payload = compare_module.compare(plan, select_ranges(timeline))

    assert payload["story_decision_count"] == 1
    entry = payload["decisions_no_threshold_could_make"][0]
    assert entry["category"] == "setup_payoff"
    assert "31 minutes" in entry["why"]


def test_a_director_agreeing_with_the_thresholds_shows_no_story_decisions():
    plan = a_plan_with(a_decision(
        "keep", segment_ids=("s_1",),
        reason=DirectorReason(category="pacing", text="looks fine")))
    payload = compare_module.compare(plan, [])
    assert payload["story_decision_count"] == 0
    assert "agreeing with it rather than adding" in \
        compare_module.render(payload)


def test_the_comparison_lists_what_each_side_kept_that_the_other_dropped(
        timeline):
    from editing.roughcut.select import select_ranges

    plan = a_plan_with(a_decision(
        "keep", segment_ids=("s_1",), start=0.0, end=20.0,
        reason=DirectorReason(category="setup_payoff", text="setup")))
    heuristic = [entry for entry in select_ranges(timeline)
                 if entry.start > 100.0]
    payload = compare_module.compare(plan, heuristic)

    assert payload["director_kept_that_heuristic_dropped"]
    assert payload["difference"]["director_only_seconds"] > 0


def test_the_comparison_renders_with_the_commands_to_actually_tell(timeline):
    from editing.roughcut.select import select_ranges

    plan = a_plan_with(a_decision("keep", segment_ids=("s_1",)))
    text = compare_module.render(
        compare_module.compare(plan, select_ranges(timeline)))

    assert "DIRECTOR vs HEURISTIC" in text
    assert "HOW TO ACTUALLY TELL" in text
    assert "render roughcut" in text
    assert "director render" in text


def test_a_mock_comparison_says_it_is_two_rule_sets(timeline):
    from editing.roughcut.select import select_ranges

    plan = a_plan_with(a_decision("keep", segment_ids=("s_1",)))
    plan.mock = True
    text = compare_module.render(
        compare_module.compare(plan, select_ranges(timeline)))
    assert "two rule sets" in text


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_the_report_shows_what_the_rules_refused(config, context):
    ctx = a_context(payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0,
                              "what": "x"}])
    reviewed, ranges, record = safety_module.review(
        [a_decision("keep", segment_ids=("s_1",)),
         a_decision("cut", segment_ids=("s_3",), start=40.0, end=60.0)],
        ctx, config=DirectorConfig().validated())
    plan = DirectorPlan(decisions=reviewed, ranges=ranges, safety=record)

    text = report_module.render(plan)
    assert "WHAT THE RULES REFUSED (1)" in text
    assert "protected_payoff" in text
    assert "The model proposes; deterministic checks decide." in text


def test_the_report_states_the_limitations_every_time():
    text = report_module.render(DirectorPlan())
    assert "has not seen a single frame" in text
    assert "not for taste" in text or "not for taste." in text
    assert "executes nothing" in text
    assert "retention" in text


def test_the_report_shows_the_style_rules_that_were_cited():
    decision = a_decision(reason=DirectorReason(
        category="style_guide", text="opening on walking",
        style_rule="Never open on walking."))
    plan = a_plan_with(decision)
    assert "Never open on walking." in report_module.render(plan)


def test_decisions_can_be_shown_in_full_and_filtered():
    plan = a_plan_with(
        a_decision("keep", segment_ids=("s_1",)),
        a_decision("hold", segment_ids=("s_3",), start=40.0, end=60.0),
    )
    text = report_module.render_decisions(plan)
    assert "KEEP" in text and "HOLD" in text

    filtered = report_module.render_decisions(plan, action="hold")
    assert "HOLD" in filtered
    assert "KEEP" not in filtered


def test_rejected_decisions_are_shown_with_the_rule_that_refused_them():
    ctx = a_context(payoffs=[{"id": "pay_1", "start": 40.0, "end": 60.0,
                              "what": "x"}])
    reviewed, ranges, record = safety_module.review(
        [a_decision("cut", segment_ids=("s_3",), start=40.0, end=60.0)],
        ctx, config=DirectorConfig().validated())
    plan = DirectorPlan(decisions=reviewed, ranges=ranges, safety=record)

    text = report_module.render_decisions(plan, rejected=True)
    assert "REJECTED DECISIONS" in text
    assert "REFUSED" in text
    assert "pay_1" in text


def test_the_context_summary_says_what_it_could_not_see(context):
    text = report_module.render_context_summary(context)
    assert "DIRECTOR CONTEXT" in text
    assert "missing" in text
    assert "candidate range(s)" in text


def test_the_report_points_at_the_next_command():
    plan = a_plan_with(a_decision())
    commands = report_module.next_commands(plan)
    assert any("show-decisions" in c for c in commands)
    assert any("compare-heuristic" in c for c in commands)


def test_a_failed_report_leads_with_the_fix():
    plan = DirectorPlan(failure=DirectorFailure(
        stage="no_backend", message="cannot reach it",
        hint="start a server"))
    text = report_module.render(plan)
    assert "FAILED" in text
    assert "start a server" in text
    assert "heuristic selector is unaffected" in text


def test_nothing_reaches_the_network_at_import_time():
    """``requests`` stays inside the function that needs it.

    The same rule Session 10A applies to faster-whisper: a module-scope import
    of an optional dependency would make importing the CLI fail on a machine
    that never installed it, taking every other editing command down with it.
    """
    import ast

    package = Path(__file__).resolve().parents[2] / "editing" / "director"
    heavy = {"requests", "subprocess"}
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:            # module scope only
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            if heavy.intersection(names):
                offenders.append(path.name)
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# Through the pipeline
# ---------------------------------------------------------------------------

def _pipeline(config, sampling):
    from editing.pipeline import build_pipeline
    return build_pipeline(config, sampling)


def test_the_pipeline_builds_a_context_and_a_plan(config, sampling, timeline):
    pipeline = _pipeline(config, sampling)
    pipeline.assets = [ASSET]
    pipeline.write_timeline(timeline)

    settings = pipeline.director_config(backend="mock")
    context = pipeline.director_context(settings=settings)
    assert context.segments

    plan = pipeline.director_plan(settings=settings, context=context)
    assert plan.mock
    assert store_module.plan_path(config).exists()
    assert pipeline.load_director_plan().decisions


def test_the_pipeline_falls_back_to_the_heuristic_with_no_plan(
        config, sampling, timeline):
    pipeline = _pipeline(config, sampling)
    pipeline.assets = [ASSET]
    pipeline.write_timeline(timeline)
    recommendations = pipeline.recommend(timeline)

    cut = pipeline.rough_cut(
        timeline=timeline, recommendations=recommendations,
        options=RoughCutOptions(mode="director"), validate=False, save=False)
    assert cut.placements
    assert any("no usable director plan" in line for line in cut.explanation)


def test_the_pipeline_loads_the_plan_for_a_director_cut(
        config, sampling, timeline):
    pipeline = _pipeline(config, sampling)
    pipeline.assets = [ASSET]
    pipeline.write_timeline(timeline)
    recommendations = pipeline.recommend(timeline)
    pipeline.write_recommendations(recommendations)
    pipeline.director_plan(settings=pipeline.director_config(backend="mock"))

    cut = pipeline.rough_cut(
        timeline=timeline, recommendations=recommendations,
        options=RoughCutOptions(mode="hybrid"), validate=False, save=False)
    assert any("hybrid mode" in line for line in cut.explanation)


def test_the_pipeline_compares_the_two_cuts(config, sampling, timeline):
    pipeline = _pipeline(config, sampling)
    pipeline.assets = [ASSET]
    pipeline.write_timeline(timeline)
    pipeline.write_recommendations(pipeline.recommend(timeline))
    pipeline.director_plan(settings=pipeline.director_config(backend="mock"))

    payload = pipeline.compare_director(timeline=timeline)
    assert "agreement" in payload["difference"]
    assert store_module.compare_path(config).exists()


def test_the_pipeline_reports_whether_a_director_could_run(config, sampling):
    status = _pipeline(config, sampling).director_status(
        DirectorConfig(backend="mock"))
    assert status["ready"] is True
    assert "config_warnings" in status


def test_director_artifacts_live_in_their_own_directory(config):
    assert config.director_dir.name == "director"
    assert config.director_dir.parent == config.output_dir


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def test_the_director_commands_parse():
    from editing.cli import build_parser

    parser = build_parser()
    for argv, expected in (
        (["director", "build-context"], "build-context"),
        (["director", "plan"], "plan"),
        (["director", "plan", "--style", "cinematic_minecraft"], "plan"),
        (["director", "report"], "report"),
        (["director", "show-decisions"], "show-decisions"),
        (["director", "show-rejected"], "show-rejected"),
        (["director", "show-style"], "show-style"),
        (["director", "compare-heuristic"], "compare-heuristic"),
        (["director", "render", "--quality", "proxy"], "render"),
        (["director", "status"], "status"),
        (["director", "clear-cache", "--yes"], "clear-cache"),
    ):
        args = parser.parse_args(argv)
        assert args.director_command == expected
        assert args.func.__name__ == "cmd_director"


def test_director_options_reach_the_parsed_arguments():
    from editing.cli import build_parser

    args = build_parser().parse_args([
        "director", "plan", "--backend", "mock", "--model", "llama-3.3",
        "--base-url", "http://elsewhere/v1", "--temperature", "0.4",
        "--style-guide", "docs/mine.md", "--target", "600",
        "--max-duration", "900", "--max-segments", "40", "--mode", "hybrid",
        "--force",
    ])
    assert args.backend_name == "mock"
    assert args.model_name == "llama-3.3"
    assert args.base_url == "http://elsewhere/v1"
    assert args.temperature == 0.4
    assert args.style_guide == "docs/mine.md"
    assert args.target == 600.0 and args.max_duration == 900.0
    assert args.max_segments == 40 and args.mode == "hybrid"
    assert args.force


def test_every_director_command_can_be_scoped_to_an_auto_run():
    from editing.cli import build_parser

    parser = build_parser()
    for command in ("plan", "report", "show-decisions", "compare-heuristic",
                    "render", "status", "build-context"):
        args = parser.parse_args(
            ["director", command, "--run", "20260101T000000-abc-style"])
        assert args.run == "20260101T000000-abc-style"


def test_an_unknown_director_subcommand_is_a_usage_error():
    from editing.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["director", "improvise"])


def test_clearing_the_director_cache_needs_yes(config, sampling, monkeypatch):
    from editing import cli

    monkeypatch.setattr(cli, "_run_scoped_pipeline",
                        lambda args: _pipeline(config, sampling))
    assert cli.main(["director", "clear-cache"]) == 1


def test_showing_the_style_guide_from_the_command_line(config, sampling,
                                                       monkeypatch, capsys):
    from editing import cli

    monkeypatch.setattr(cli, "_run_scoped_pipeline",
                        lambda args: _pipeline(config, sampling))
    assert cli.main(["director", "show-style"]) == 0
    assert "STYLE GUIDE" in capsys.readouterr().out


def _cli_pipeline(config, sampling, timeline, monkeypatch):
    """A pipeline with a timeline and a mock director plan already on disk."""
    from editing import cli

    pipeline = _pipeline(config, sampling)
    pipeline.assets = [ASSET]
    pipeline.write_timeline(timeline)
    pipeline.write_recommendations(pipeline.recommend(timeline))
    pipeline.director_plan(settings=pipeline.director_config(backend="mock"))
    monkeypatch.setattr(cli, "_run_scoped_pipeline", lambda args: pipeline)
    return pipeline


def test_comparing_from_the_command_line_runs(config, sampling, timeline,
                                              monkeypatch, capsys):
    """Parsing is not running.

    ``compare-heuristic`` and ``render`` build rough-cut options, and the
    helper that does that used to read every selection flag by attribute --
    which the director subcommands do not declare. Asserting only on the
    parsed namespace missed it entirely.
    """
    from editing import cli

    _cli_pipeline(config, sampling, timeline, monkeypatch)
    assert cli.main(["director", "compare-heuristic"]) == 0
    assert "DIRECTOR vs HEURISTIC" in capsys.readouterr().out


def test_showing_decisions_from_the_command_line_runs(
        config, sampling, timeline, monkeypatch, capsys):
    from editing import cli

    _cli_pipeline(config, sampling, timeline, monkeypatch)
    assert cli.main(["director", "show-decisions"]) == 0
    assert cli.main(["director", "show-rejected"]) == 0
    assert cli.main(["director", "report"]) == 0
    assert "DIRECTOR PASS" in capsys.readouterr().out


def test_building_a_context_from_the_command_line_runs(
        config, sampling, timeline, monkeypatch, capsys):
    from editing import cli

    _cli_pipeline(config, sampling, timeline, monkeypatch)
    assert cli.main(["director", "build-context", "--show-prompt"]) == 0
    out = capsys.readouterr().out
    assert "DIRECTOR CONTEXT" in out
    assert "CANDIDATE RANGES" in out, "the prompt was printed"


def test_rendering_a_director_cut_from_the_command_line_runs(
        config, sampling, tmp_path, monkeypatch):
    """The whole chain: decisions -> ranges -> rough cut -> a render job."""
    from editing import cli
    from editing.render.runner import MockRunner

    # A real file on disk, because the renderer measures its sources before
    # it encodes anything.
    clip = tmp_path / "ep12.mp4"
    clip.write_bytes(b"x" * 4096)
    asset = MediaAsset(asset_id="a_real", path=str(clip),
                       filename="ep12.mp4", duration=400.0)

    def visual_for(start, end, importance):
        return VisualEvent(
            event_id=f"e_{start}", source_file=asset.path,
            asset_id=asset.asset_id, start=start, end=end, confidence=0.85,
            environment="cave", actions=["mining"], importance=importance,
            suggested_range=TimeRange(start, end), model="test-model")

    local = build_timeline(
        [asset],
        {asset.asset_id: [visual_for(0, 20, "danger"),
                          visual_for(20, 40, "setup"),
                          visual_for(40, 60, "payoff")]},
        {asset.asset_id: Transcript(
            asset_id=asset.asset_id, source="srt",
            entries=[TranscriptEntry(0.0, 18.0, "oh god a creeper"),
                     TranscriptEntry(40.0, 58.0, "there we go")])},
        audio_by_asset={asset.asset_id: [
            AudioEvent(event_id="au_0", source_file=asset.path,
                       asset_id=asset.asset_id, start=0.0, end=20.0,
                       type="sudden_reaction", confidence=0.8,
                       detection="heuristic", loudness_db=-8.0,
                       baseline_db=-24.0)]},
    )

    pipeline = _pipeline(config, sampling)
    pipeline.assets = [asset]
    pipeline.write_timeline(local)
    pipeline.write_recommendations(pipeline.recommend(local))
    pipeline.director_plan(settings=pipeline.director_config(backend="mock"))
    monkeypatch.setattr(cli, "_run_scoped_pipeline", lambda args: pipeline)
    monkeypatch.setattr(
        "editing.render.runner.build_runner",
        lambda config, backend="ffmpeg": MockRunner())

    assert cli.main(["director", "render", "--mock", "--json"]) == 0
    job = pipeline.render_job()
    assert job.status == "mocked", "completed, and produced no video"
    assert job.segments, "the director's ranges reached the renderer"


def test_a_failed_plan_exits_non_zero(config, sampling, timeline,
                                      monkeypatch):
    from editing import cli

    pipeline = _pipeline(config, sampling)
    pipeline.assets = [ASSET]
    pipeline.write_timeline(timeline)
    monkeypatch.setattr(cli, "_run_scoped_pipeline", lambda args: pipeline)
    monkeypatch.setattr(
        "editing.pipeline.Pipeline.director_plan",
        lambda self, **kwargs: DirectorPlan(
            failure=DirectorFailure(stage="no_backend", message="nope")))

    assert cli.main(["director", "plan"]) == 1


# ---------------------------------------------------------------------------
# The HTTP contract, without a model
# ---------------------------------------------------------------------------
#
# The client is the one part of this layer that cannot be checked by calling
# it with a stub object: what matters is the *envelope* -- the URL, the
# message roles, the JSON-mode hint, and unwrapping an answer that arrives
# fenced with a sentence either side of it. So these run a real
# OpenAI-compatible server on loopback for a few milliseconds.
#
# Nothing leaves the machine and no model is involved. They skip rather than
# fail where a socket cannot be bound.

class _StubHandler(BaseHTTPRequestHandler):
    """The two endpoints every OpenAI-compatible server serves."""

    received: list = []

    def log_message(self, *args):
        return

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "stub-model"}]})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append(
            {"body": body, "path": self.path,
             "auth": self.headers.get("Authorization")})

        user = body.get("messages", [{}, {}])[1].get("content", "")
        ids = re.findall(r"^\[(\w+)\]", user, flags=re.MULTILINE)
        decisions = [{
            "segment_ids": [ids[0]] if ids else ["s_1"],
            "action": "hook",
            "reason": {"category": "hook_strength", "text": "the best bit",
                       "style_rule": "Open on something happening."},
            "confidence": 0.85,
            "priority": 0.9,
            "evidence": ids[:1] or ["s_1"],
            "order": 0,
        }]
        # Fenced, with a sentence either side -- the way models really answer.
        content = ("Here is my cut:\n```json\n"
                   + json.dumps({"approach": "open on the creeper",
                                 "decisions": decisions})
                   + "\n```\nLet me know if you want it tighter!")
        self._send({"choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": content}}]})

    def _send(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub_endpoint():
    """A real OpenAI-compatible server on loopback. Yields its base URL."""
    pytest.importorskip("requests")
    _StubHandler.received = []
    try:
        server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    except OSError:  # pragma: no cover - a sandbox with no sockets
        pytest.skip("cannot bind a loopback socket here")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_the_client_reads_a_real_chat_completions_answer(
        config, context, stub_endpoint):
    settings = DirectorConfig(
        backend="openai", base_url=stub_endpoint, model="stub-model",
        max_retries=0, timeout=15.0,
    ).validated()
    plan = run_module.plan(config, context, settings=settings)

    assert plan.ok
    assert not plan.mock
    assert plan.approach == "open on the creeper"
    assert plan.ranges and plan.ranges[0].is_hook


def test_the_request_carries_the_envelope_a_server_expects(
        config, context, stub_endpoint):
    settings = DirectorConfig(
        backend="openai", base_url=stub_endpoint, model="stub-model",
        temperature=0.3, api_key="sk-test", max_retries=0,
    ).validated()
    run_module.plan(config, context, settings=settings)

    sent = _StubHandler.received[0]
    assert sent["path"].endswith("/chat/completions")
    assert sent["auth"] == "Bearer sk-test"

    body = sent["body"]
    assert body["model"] == "stub-model"
    assert body["temperature"] == 0.3
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "editor" in body["messages"][0]["content"]
    assert "CANDIDATE RANGES" in body["messages"][1]["content"]


def test_no_authorization_header_when_no_key_is_needed(
        config, context, stub_endpoint):
    settings = DirectorConfig(
        backend="openai", base_url=stub_endpoint, model="stub-model",
        api_key="not-needed", max_retries=0,
    ).validated()
    run_module.plan(config, context, settings=settings)
    assert _StubHandler.received[0]["auth"] is None


def test_the_status_check_reads_the_served_model_list(stub_endpoint):
    health = backends_module.check(DirectorConfig(
        backend="openai", base_url=stub_endpoint, model="stub-model",
        max_retries=0))

    assert health["ready"] is True
    assert health["served_models"] == ["stub-model"]
    assert "warning" not in health


def test_a_model_the_server_does_not_serve_is_a_warning_not_a_refusal(
        stub_endpoint):
    """The server may still route it, so this is worth saying and not fatal."""
    health = backends_module.check(DirectorConfig(
        backend="openai", base_url=stub_endpoint, model="some-other-model",
        max_retries=0))

    assert health["ready"] is True
    assert "does not list" in health["warning"]

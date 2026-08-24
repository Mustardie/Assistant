"""Retention structure wiring: reshaping a cut around what Session 8 found.

Five properties carry the weight.

**Protection is applied before anything that removes.** A setup whose payoff
is in the cut is claimed first, and every rule that runs afterwards checks the
claim. That is an ordering, not a negotiation, and it is tested from both
sides: the claim is made, and the compressor is refused.

**A cold open moves footage; it does not copy it.** The default removes the
original, and the one place this layer is allowed to trim protected footage is
carving the teased seconds out of where they used to be. The finished cut is
checked for duplication rather than the policy being trusted.

**Silence is judged by what it is for.** A pause after a scream is the joke; a
pause in an empty tunnel is dead air. They are the same measurement and only
the context tells them apart, so the context rules get a test each.

**Nothing claims analytics.** Every count is a count of what changed in the
edit. There is no score, no grade and no percentage anywhere in the output,
and that is asserted over the whole rendered report rather than spot-checked.

**The base cut is never touched.** A retention pass writes a variant. Reading
it back and comparing is how you decide, and disagreeing costs nothing.

Nothing here needs Premiere, FFmpeg, a GPU, a model, Whisper or real footage.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from editing.align import build_timeline
from editing.errors import EditingError
from editing.retention import coldopen as coldopen_module
from editing.retention import compare as compare_module
from editing.retention import compile as compile_module
from editing.retention import deadair as deadair_module
from editing.retention import protect as protect_module
from editing.retention import report as report_module
from editing.retention import resolve as resolve_module
from editing.retention import run as run_module
from editing.retention import sag as sag_module
from editing.retention import store as store_module
from editing.retention.schema import (
    ACTIONS, AGGRESSIVENESS, MODES, NOT_MEASURED, ORDINARY_SILENCE,
    ColdOpenPlan, DeadAirDecision, PayoffProtectionDecision,
    RetentionCutConfig, RetentionCutDecision, RetentionCutFailure,
    RetentionCutPlan, SagCompressionPlan, SetupProtectionDecision, SourceSpan,
    decision_id_for,
)
from editing.roughcut.build import RoughCutOptions
from editing.roughcut.select import SelectedRange
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


DEFAULT_EVENTS = (
    visual(0, 20, importance="setup", environment="plains",
           actions=("walking",)),
    visual(20, 60, importance="setup", actions=("mining",)),
    visual(60, 100, importance="setup", actions=("mining",)),
    visual(100, 140, importance="setup", actions=("mining",)),
    visual(140, 170, importance="danger", actions=("fighting",),
           threats=("creeper",)),
    visual(170, 200, importance="setup", actions=("mining",)),
    visual(200, 230, importance="payoff", actions=("mining",)),
    visual(230, 260, importance="setup", actions=("walking",)),
)

DEFAULT_AUDIO = (
    audio(20, 60, "silence"),
    audio(60, 100, "low_energy"),
    audio(140, 170, "sudden_reaction"),
    audio(200, 230, "possible_laughter"),
    audio(232, 244, "silence"),
)

DEFAULT_LINES = (
    (0.0, 18.0, "right so today we are going to find some diamonds"),
    (140.0, 165.0, "oh god a creeper watch out that was close"),
    (200.0, 228.0, "there we go diamonds that is what we came down here for"),
)


def a_timeline(events=DEFAULT_EVENTS, audio_events=DEFAULT_AUDIO,
               lines=DEFAULT_LINES):
    transcript = Transcript(
        asset_id=ASSET.asset_id, source="srt",
        entries=[TranscriptEntry(*line) for line in lines],
    ) if lines else None
    return build_timeline(
        [ASSET], {ASSET.asset_id: list(events)},
        {ASSET.asset_id: transcript} if transcript else {},
        audio_by_asset={ASSET.asset_id: list(audio_events)},
    )


@pytest.fixture
def timeline():
    return a_timeline()


def a_range(start, end, *, speed=1.0, protected=False, reason="setup",
            asset_id="a_test") -> SelectedRange:
    return SelectedRange(
        asset_id=asset_id, source_file=ASSET.path, start=start, end=end,
        keep_reason=reason, speed=speed, protected=protected,
        segment_ids=[], notes="",
    )


class Track:
    """An episode clock built by hand, for the resolver tests."""

    def __init__(self, slots, *, timebase="roughcut", duration=None):
        self.slots = list(slots)
        self.timebase = timebase
        self.duration = duration if duration is not None else (
            max((slot.end for slot in self.slots), default=0.0))
        self.is_empty = not self.slots
        self.has_motion = False
        self.warnings: list = []


class Slot:
    def __init__(self, start, end, source_start, source_end, *,
                 said="", importance="setup", actions=(), audio=(),
                 speed=1.0, protected=False, environment="cave",
                 segment_id="", asset_id="a_test"):
        self.start = start
        self.end = end
        self.source_start = source_start
        self.source_end = source_end
        self.speed = speed
        self.protected = protected
        self.placement_id = ""
        self.environment = environment
        self.actions = list(actions)
        self.importance = importance
        self.audio_types = set(audio)
        self.segment = _Segment(
            segment_id or f"s_{start:.0f}", asset_id, said,
            [audio_event for audio_event in audio])

    def overlaps(self, start, end):
        return max(0.0, min(self.end, end) - max(self.start, start))


class _Segment:
    def __init__(self, segment_id, asset_id, said, audio_kinds):
        self.segment_id = segment_id
        self.asset_id = asset_id
        self.source_file = ASSET.path
        self.said = said
        self.has_speech = bool(said.strip())
        self.audio_events = [
            audio(0, 1, kind) for kind in audio_kinds
            if kind in ("silence", "long_pause", "low_energy")
        ]


def a_resolver(slots=None, *, timebase="roughcut"):
    slots = slots if slots is not None else [
        Slot(0, 20, 0, 20, said="hello", importance="setup",
             actions=["walking"]),
        Slot(20, 50, 140, 170, said="a creeper", importance="danger",
             actions=["fighting"], audio=["sudden_reaction"]),
        Slot(50, 80, 200, 230, said="we got it", importance="payoff",
             actions=["mining"], audio=["possible_laughter"]),
    ]
    return resolve_module.Resolver(Track(slots, timebase=timebase))


class Item:
    """An episode-layer finding, built by hand."""

    def __init__(self, item_id="i_1", start=0.0, end=10.0, **fields):
        self.item_id = item_id
        self.start = start
        self.end = end
        self.confidence = fields.pop("confidence", 0.8)
        self.why = fields.pop("why", "because")
        self.segment_ids = fields.pop("segment_ids", [])
        for key, value in fields.items():
            setattr(self, key, value)


def a_hook(**fields):
    defaults = {
        "item_id": "hook_1", "start": 20.0, "end": 32.0,
        "hook_type": "danger", "score": 0.9, "confidence": 0.8,
        "viewer_question": "does it survive?", "suggested_text": "oh god",
        "text_source": "transcript_quote", "payoff_at": 55.0,
        "payoff_id": "pay_1", "setup_seconds": 20.0, "risks": [],
    }
    defaults.update(fields)
    return Item(**defaults)


def a_risk(**fields):
    defaults = {
        "item_id": "risk_1", "start": 0.0, "end": 20.0,
        "risk": "boring_repetition", "severity": "high", "score": 0.8,
        "confidence": 0.8,
    }
    defaults.update(fields)
    return Item(**defaults)


class Memory:
    """An episode memory built by hand.

    ``timebase`` defaults to ``timeline`` because these tests have no rough
    cut: a ``roughcut`` memory resolved without one is a mismatch the compiler
    refuses, which has a test of its own.
    """

    def __init__(self, *, setups=(), payoffs=(), callbacks=(),
                 timebase="timeline"):
        self.setups = list(setups)
        self.payoffs = list(payoffs)
        self.callbacks = list(callbacks)
        self.timebase = timebase
        self.episode_id = "ep_1"


class Retention:
    def __init__(self, *, hooks=(), risks=(), climax=None, ending=None):
        self.hooks = list(hooks)
        self.risks = list(risks)
        self.climax = climax
        self.ending = ending


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_nonsense_settings_clamp_rather_than_raise():
    settings = RetentionCutConfig(
        mode="aggressive", duplicate_policy="obliterate",
        dead_air_aggressiveness="maximum", grind_speed=99.0,
        max_compression_share=5.0, min_cold_open_seconds=-4.0,
        max_cold_open_seconds=1.0,
    ).validated()

    assert settings.mode == "report_only"
    assert settings.duplicate_policy == "remove"
    assert settings.dead_air_aggressiveness == "medium"
    assert settings.grind_speed == 8.0, "clamped, not rejected"
    assert settings.max_compression_share == 1.0
    assert settings.min_cold_open_seconds >= 0.5
    assert settings.max_cold_open_seconds >= settings.min_cold_open_seconds


def test_report_only_is_the_default():
    """The safe setting: decide everything, change nothing."""
    assert RetentionCutConfig().mode == "report_only"
    assert not RetentionCutConfig().acts
    assert RetentionCutConfig(mode="retention").acts
    assert RetentionCutConfig(mode="director_retention").acts
    assert not RetentionCutConfig(mode="off").acts


def test_the_aggressiveness_setting_decides_the_silence_limit():
    for level in AGGRESSIVENESS:
        settings = RetentionCutConfig(dead_air_aggressiveness=level)
        assert settings.ordinary_silence_limit == ORDINARY_SILENCE[level]

    assert ORDINARY_SILENCE["high"] < ORDINARY_SILENCE["low"]
    # An explicit number beats the setting.
    assert RetentionCutConfig(
        dead_air_aggressiveness="low",
        max_ordinary_silence=0.25).ordinary_silence_limit == 0.25


def test_settings_warn_about_the_choices_that_bite():
    assert any("change nothing" in w
               for w in RetentionCutConfig(mode="off").warnings)
    assert any("none of them was applied" in w
               for w in RetentionCutConfig(mode="report_only").warnings)
    assert any("teaser" in w for w in RetentionCutConfig(
        allow_duplicate_footage=True).warnings)
    assert any("clipped" in w for w in RetentionCutConfig(
        dead_air_aggressiveness="high").warnings)
    assert any("deleting the video" in w for w in RetentionCutConfig(
        max_compression_share=0.9).warnings)


def test_only_the_director_modes_prefer_the_director_cut():
    assert RetentionCutConfig(mode="director_retention").prefers_director
    assert RetentionCutConfig(mode="hybrid").prefers_director
    assert not RetentionCutConfig(mode="retention").prefers_director


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_a_decision_is_not_accepted_until_something_accepts_it():
    assert RetentionCutDecision().accepted is False
    assert RetentionCutDecision.from_dict({"action": "cut"}).accepted is False


def test_a_decision_knows_what_kind_of_thing_it_is():
    assert RetentionCutDecision(action="cut").changes_footage
    assert RetentionCutDecision(action="cold_open").changes_footage
    assert RetentionCutDecision(action="protect").protects
    assert RetentionCutDecision(action="hold").protects
    assert not RetentionCutDecision(action="marker_only").changes_footage
    assert not RetentionCutDecision(action="keep").changes_footage


def test_a_decision_round_trips_through_a_dict():
    decision = RetentionCutDecision(
        decision_id="r_1", action="speed_up", source_type="risk",
        episode_start=10.0, episode_end=40.0, speed=2.0, accepted=True,
        spans=[SourceSpan(asset_id="a", start=1.0, end=5.0)],
    )
    restored = RetentionCutDecision.from_dict(decision.to_dict())
    assert restored.action == "speed_up"
    assert restored.speed == 2.0
    assert restored.accepted is True
    assert restored.spans[0].duration == 4.0


def test_an_unknown_action_coerces_on_read():
    assert RetentionCutDecision.from_dict(
        {"action": "obliterate"}).action == "marker_only"
    assert RetentionCutDecision.from_dict(
        {"action": "cold_open"}).action == "cold_open"


def test_a_span_measures_its_own_overlap():
    span = SourceSpan(asset_id="a", start=10.0, end=30.0)
    assert span.covers("a", 20.0, 40.0) == 10.0
    assert span.covers("b", 20.0, 40.0) == 0.0, "different file, no overlap"
    assert span.duration == 20.0


def test_a_plan_only_counts_as_applied_when_it_changed_something():
    plan = RetentionCutPlan(mode="retention")
    assert not plan.applied, "no accepted decisions"

    plan.decisions = [RetentionCutDecision(action="cut", accepted=True)]
    assert plan.applied

    assert not replace(plan, mode="report_only").applied
    assert not replace(plan, mode="off").applied


def test_every_plan_says_it_measures_nothing():
    plan = RetentionCutPlan()
    assert "measures retention" in plan.not_measured
    assert plan.not_measured in json.dumps(plan.to_dict())


def test_a_plan_round_trips_and_keeps_its_refusals():
    plan = RetentionCutPlan(
        decisions=[
            RetentionCutDecision(action="cut", accepted=True),
            RetentionCutDecision(action="cut", accepted=False,
                                 reject_code="protected_range",
                                 rejected_reason="no"),
        ],
    )
    restored = RetentionCutPlan.from_dict(plan.to_dict())
    assert len(restored.accepted) == 1
    assert len(restored.rejected) == 1
    assert restored.rejected[0].reject_code == "protected_range"


def test_a_decision_id_is_stable():
    assert decision_id_for("cut", "risk_1", 10.0) == \
        decision_id_for("cut", "risk_1", 10.0)
    assert decision_id_for("cut", "risk_1", 10.0) != \
        decision_id_for("cut", "risk_1", 11.0)


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------

def test_episode_time_resolves_to_real_footage():
    resolver = a_resolver()
    spans = resolver.spans(20.0, 50.0)

    assert len(spans) == 1
    assert spans[0].start == 140.0 and spans[0].end == 170.0


def test_a_range_spanning_slots_resolves_to_each_of_them():
    spans = a_resolver().spans(10.0, 60.0)
    assert len(spans) == 3
    assert [round(span.start) for span in spans] == [10, 140, 200]


def test_a_partial_overlap_resolves_to_the_matching_part():
    spans = a_resolver().spans(25.0, 35.0)
    assert spans[0].start == 145.0
    assert spans[0].end == 155.0


def test_a_sped_up_slot_resolves_to_the_footage_it_really_covers():
    """One second of episode time over a 2x clip is two seconds of footage."""
    resolver = resolve_module.Resolver(Track([
        Slot(0, 15, 100, 130, speed=2.0),
    ]))
    spans = resolver.spans(0.0, 5.0)
    assert spans[0].start == 100.0
    assert spans[0].end == 110.0, "5s of episode = 10s of source at 2x"


def test_a_range_touching_nothing_resolves_to_nothing():
    """The honest answer for a finding built against a different cut."""
    assert a_resolver().spans(900.0, 950.0) == []
    assert a_resolver().spans(10.0, 10.0) == []


def test_segment_ids_are_the_fallback_when_a_range_does_not_resolve():
    resolver = a_resolver()
    item = Item(start=900.0, end=950.0, segment_ids=["s_20"])
    spans = resolver.resolve_item(item)
    assert spans and spans[0].start == 140.0


def test_touching_spans_are_joined():
    spans = a_resolver().spans(0.0, 80.0)
    # Slot 1 ends at source 20 and slot 2 starts at 140, so they stay apart.
    assert len(spans) == 3
    joined = resolve_module._join([
        SourceSpan(asset_id="a", start=0.0, end=10.0),
        SourceSpan(asset_id="a", start=10.1, end=20.0),
    ])
    assert len(joined) == 1 and joined[0].end == 20.0


def test_the_resolver_reads_the_timebase_rather_than_guessing(timeline):
    """Acting on the wrong clock would put the cold open somewhere else."""
    from editing.roughcut.build import build_rough_cut

    cut = build_rough_cut(timeline, validate=False)
    on_cut = resolve_module.build_resolver(timeline, cut, timebase="roughcut")
    on_timeline = resolve_module.build_resolver(
        timeline, cut, timebase="timeline")

    assert on_cut.timebase == "roughcut"
    assert on_timeline.timebase == "timeline", (
        "a memory built without a cut must not be resolved through one")
    assert on_cut.duration != on_timeline.duration


def test_the_resolver_answers_the_questions_the_rules_ask():
    resolver = a_resolver()
    assert resolver.has_speech(20.0, 50.0)
    assert "danger" in resolver.importances(20.0, 50.0)
    assert "fighting" in resolver.actions(20.0, 50.0)
    assert resolver.position(40.0) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Cold open
# ---------------------------------------------------------------------------

def choose(hooks, *, resolver=None, climax=None, protected=(), **settings):
    return coldopen_module.choose(
        hooks, resolver or a_resolver(),
        RetentionCutConfig(**settings).validated(),
        climax=climax, protected_ranges=protected,
    )


def test_the_best_hook_becomes_the_cold_open():
    plan, decision = choose([a_hook()])

    assert plan.chosen
    assert plan.hook_type == "danger"
    assert plan.duration > 0
    assert decision is not None and decision.action == "cold_open"
    assert decision.spans[0].start == 140.0


def test_the_highest_scoring_hook_wins():
    weak = a_hook(item_id="hook_weak", score=0.5)
    strong = a_hook(item_id="hook_strong", score=0.95)
    plan, _decision = choose([weak, strong])
    assert plan.hook_id == "hook_strong"


def test_a_hook_over_walking_is_refused():
    """The single most common way an episode loses a viewer at ten seconds."""
    resolver = a_resolver([
        Slot(0, 30, 0, 30, said="", importance="setup",
             actions=["walking", "sorting"]),
    ])
    plan, decision = choose([a_hook(start=0.0, end=20.0)], resolver=resolver)

    assert not plan.chosen and decision is None
    assert plan.rejected[0]["code"] == "hook_is_boring"
    assert "walking" in plan.rejected[0]["why"]


def test_a_strong_moment_is_not_refused_for_also_containing_walking():
    """A creeper explosion is a cold open even if the model said 'walking'."""
    resolver = a_resolver([
        Slot(0, 30, 0, 30, said="", importance="setup", actions=["idle"]),
        Slot(30, 60, 100, 130, said="oh god", importance="danger",
             actions=["walking", "fighting"]),
    ])
    plan, _decision = choose([a_hook(start=30.0, end=50.0)], resolver=resolver)
    assert plan.chosen


def test_a_hook_type_that_does_not_hold_anybody_is_refused():
    plan, _decision = choose([a_hook(hook_type="goal")])
    assert not plan.chosen
    assert plan.rejected[0]["code"] == "hook_is_boring"


def test_a_hook_too_short_to_land_is_refused():
    plan, _decision = choose([a_hook(start=20.0, end=22.0)])
    assert plan.rejected[0]["code"] == "too_short"


def test_a_hook_from_the_very_end_is_refused_as_a_spoiler():
    resolver = a_resolver()
    plan, _decision = choose([a_hook(start=76.0, end=80.0)],
                             resolver=resolver, min_cold_open_seconds=2.0)
    assert plan.rejected[0]["code"] == "hook_spoils_ending"


def test_a_hook_nobody_could_follow_is_refused():
    """No speech and no strong label: a stranger doing something unexplained."""
    resolver = a_resolver([
        Slot(0, 30, 0, 30, said="", importance="unknown", actions=[]),
    ])
    plan, _decision = choose([a_hook(start=0.0, end=20.0)], resolver=resolver)
    assert plan.rejected[0]["code"] == "hook_needs_context"


def test_the_setup_seconds_field_is_not_read_as_context_needed():
    """Session 8 sets it to the beat's position, despite the name.

    Reading it as "seconds of context needed" refused every hook past the
    first few seconds, which is every hook worth having.
    """
    plan, _decision = choose([a_hook(setup_seconds=900.0)])
    assert plan.chosen, "a hook 900s in is still a hook"


def test_a_hook_already_at_the_start_is_refused_as_a_no_op():
    """Moving the first fifteen seconds to the front changes nothing."""
    resolver = a_resolver([
        Slot(0, 30, 0, 30, said="oh god a creeper", importance="danger",
             actions=["fighting"]),
        Slot(30, 60, 100, 130, said="x", importance="setup"),
    ])
    plan, _decision = choose([a_hook(start=0.0, end=15.0)], resolver=resolver)
    assert not plan.chosen
    assert "already where the episode starts" in plan.rejected[0]["why"]


def test_a_low_scoring_hook_is_refused():
    plan, _decision = choose([a_hook(score=0.1)])
    assert plan.rejected[0]["code"] == "low_confidence"


def test_no_hooks_at_all_says_why_the_opening_is_unchanged():
    plan, decision = choose([])
    assert not plan.chosen and decision is None
    assert "no hook candidates" in plan.fallback_reason


def test_every_hook_refused_says_so_and_lists_them():
    plan, _decision = choose([a_hook(score=0.1), a_hook(hook_type="goal")])
    assert "All 2 hook candidate(s) were refused" in plan.fallback_reason
    assert len(plan.rejected) == 2


def test_cold_opens_can_be_switched_off():
    plan, decision = choose([a_hook()], cold_open=False)
    assert not plan.chosen and decision is None
    assert "switched off" in plan.fallback_reason


def test_a_long_hook_is_trimmed_to_the_ceiling():
    plan, _decision = choose([a_hook(start=20.0, end=50.0)],
                             max_cold_open_seconds=10.0)
    assert plan.chosen
    assert plan.duration <= 10.1
    assert any("trimmed" in warning for warning in plan.warnings)


def test_a_hook_with_no_payoff_is_used_and_warned_about():
    """Opening a question the episode never answers is a promise not kept."""
    plan, _decision = choose([a_hook(payoff_at=None)])
    assert plan.chosen
    assert any("never answers" in warning for warning in plan.warnings)


def test_the_original_is_removed_by_default():
    plan, _decision = choose([a_hook()])
    assert plan.duplicate_policy == "remove"
    assert plan.original_removed
    assert not plan.duplicates_footage

    teaser = coldopen_module.teaser_decision(plan)
    assert teaser is not None and teaser.action == "cut"


def test_a_hook_that_is_the_peak_is_shortened_rather_than_removed():
    """Removing it would move the ending to the front and leave nothing."""
    climax = Item(item_id="cli_1", start=20.0, end=50.0)
    plan, _decision = choose([a_hook()], climax=climax)

    assert plan.duplicate_policy == "shorten"
    assert not plan.original_removed
    assert plan.original_shortened_to > 0
    assert any("peak of the episode" in w for w in plan.warnings)

    teaser = coldopen_module.teaser_decision(plan)
    assert teaser is not None and teaser.action == "shorten"


def test_a_hook_on_protected_footage_is_shortened_rather_than_removed():
    protected = [SourceSpan(asset_id="a_test", start=140.0, end=170.0)]
    plan, _decision = choose([a_hook()], protected=protected)

    assert plan.duplicate_policy == "shorten"
    assert any("protected footage" in w for w in plan.warnings)


def test_duplication_is_only_allowed_when_it_is_asked_for():
    plan, _decision = choose([a_hook()], allow_duplicate_footage=True)
    assert plan.duplicate_policy == "keep"
    assert plan.duplicates_footage
    assert coldopen_module.teaser_decision(plan) is None


def test_a_cold_open_plan_round_trips():
    plan, _decision = choose([a_hook()])
    restored = ColdOpenPlan.from_dict(plan.to_dict())
    assert restored.chosen and restored.hook_type == "danger"
    assert restored.spans[0].start == 140.0


# ---------------------------------------------------------------------------
# Protection
# ---------------------------------------------------------------------------

def protect(memory, *, base=None, climax=None, **settings):
    resolver = a_resolver()
    base_spans = base if base is not None else [
        SourceSpan(asset_id="a_test", start=0.0, end=260.0)]
    return protect_module.protect(
        memory, resolver, RetentionCutConfig(**settings).validated(),
        base_spans, climax=climax,
    )


def test_a_setup_is_protected_when_its_payoff_is_in_the_cut():
    """The check that most justifies this layer.

    The setup looks like nothing; the only reason to keep it sits later.
    """
    memory = Memory(
        setups=[Item("set_1", 0.0, 20.0, payoff_id="pay_1")],
        payoffs=[Item("pay_1", 50.0, 80.0, setup_id="set_1")],
    )
    setups, payoffs, decisions = protect(memory)

    assert setups[0].protected
    assert setups[0].payoff_kept
    assert "arrives from nowhere" in setups[0].reason
    assert payoffs[0].protected
    assert any(d.action == "protect" and d.source_type == "setup"
               for d in decisions)


def test_a_setup_whose_payoff_was_cut_is_not_protected():
    """Footage with no destination is not worth defending."""
    memory = Memory(
        setups=[Item("set_1", 0.0, 20.0, payoff_id="pay_1")],
        payoffs=[Item("pay_1", 50.0, 80.0, setup_id="set_1")],
    )
    # The base cut contains the setup but not the payoff.
    base = [SourceSpan(asset_id="a_test", start=0.0, end=30.0)]
    setups, _payoffs, _decisions = protect(memory, base=base)

    assert not setups[0].protected
    assert "not in the cut" in setups[0].reason


def test_a_setup_that_never_pays_off_warns_rather_than_protects():
    memory = Memory(setups=[Item("set_1", 0.0, 20.0, payoff_id="")])
    setups, _payoffs, _decisions = protect(memory)

    assert not setups[0].protected
    assert "never pays off" in setups[0].warning
    assert "left waiting" in setups[0].warning


def test_a_payoff_without_its_setup_warns():
    memory = Memory(
        setups=[Item("set_1", 0.0, 20.0, payoff_id="pay_1")],
        payoffs=[Item("pay_1", 50.0, 80.0, setup_id="set_1")],
    )
    # The cut contains the payoff and not the setup.
    base = [SourceSpan(asset_id="a_test", start=190.0, end=260.0)]
    _setups, payoffs, _decisions = protect(memory, base=base)

    assert payoffs[0].protected
    assert not payoffs[0].setup_kept
    assert "without knowing why it matters" in payoffs[0].warning


def test_a_payoff_with_no_setup_anywhere_warns_differently():
    memory = Memory(payoffs=[Item("pay_1", 50.0, 80.0, setup_id="")])
    _setups, payoffs, _decisions = protect(memory)
    assert "no setup recorded" in payoffs[0].warning


def test_the_peak_is_protected_even_when_it_is_not_a_payoff():
    climax = Item("cli_1", 50.0, 80.0)
    _setups, payoffs, decisions = protect(Memory(), climax=climax)

    assert payoffs and payoffs[0].is_climax
    assert payoffs[0].protected
    assert any(d.source_type == "climax" for d in decisions)


def test_a_callback_protects_what_it_calls_back_to():
    memory = Memory(callbacks=[Item("cb_1", 20.0, 40.0)])
    _setups, _payoffs, decisions = protect(memory)
    assert any(d.source_type == "callback" and d.action == "protect"
               for d in decisions)


def test_protection_can_be_switched_off():
    memory = Memory(
        setups=[Item("set_1", 0.0, 20.0, payoff_id="pay_1")],
        payoffs=[Item("pay_1", 50.0, 80.0, setup_id="set_1")],
    )
    setups, payoffs, decisions = protect(
        memory, protect_setups=False, protect_payoffs=False)
    assert not setups[0].protected
    assert not payoffs[0].protected
    assert decisions == []


def test_a_finding_not_in_the_cut_is_not_protected():
    memory = Memory(payoffs=[Item("pay_1", 900.0, 950.0)])
    _setups, payoffs, _decisions = protect(memory)
    assert not payoffs[0].protected
    assert "not in the cut" in payoffs[0].reason


# ---------------------------------------------------------------------------
# Sag compression
# ---------------------------------------------------------------------------

def compress(risks, *, resolver=None, protected=(), base_seconds=300.0,
             **settings):
    return sag_module.compress(
        risks, resolver or a_resolver(),
        RetentionCutConfig(**settings).validated(),
        protected, base_seconds=base_seconds,
    )


def test_a_silent_boring_stretch_is_cut():
    resolver = a_resolver([
        Slot(0, 60, 0, 60, said="", importance="setup", actions=["idle"]),
    ])
    plan, decisions = compress([a_risk(start=0.0, end=60.0)],
                               resolver=resolver)

    assert decisions[0].action == "cut"
    assert decisions[0].accepted
    assert plan.zones_compressed == 1
    assert plan.seconds_removed > 0


def test_a_stretch_where_the_picture_changes_is_sped_up_not_cut():
    """A viewer needs to see it happened; they do not need to watch it."""
    resolver = a_resolver([
        Slot(0, 60, 0, 60, said="", importance="setup", actions=["mining"]),
    ])
    _plan, decisions = compress([a_risk(start=0.0, end=60.0)],
                                resolver=resolver, grind_speed=2.0)

    assert decisions[0].action == "speed_up"
    assert decisions[0].speed == 2.0
    assert "needs to see it happened" in decisions[0].reason


def test_a_stretch_with_speech_is_never_touched():
    resolver = a_resolver([
        Slot(0, 60, 0, 60, said="I am explaining something", actions=["idle"]),
    ])
    _plan, decisions = compress([a_risk(start=0.0, end=60.0)],
                                resolver=resolver)

    assert not decisions[0].accepted
    assert decisions[0].reject_code == "speech_present"
    assert decisions[0].action == "marker_only"


def test_protected_footage_is_never_compressed():
    """Protection is applied before compression, and this is why."""
    resolver = a_resolver([
        Slot(0, 60, 0, 60, said="", importance="setup", actions=["idle"]),
    ])
    protected = [SourceSpan(asset_id="a_test", start=0.0, end=60.0)]
    _plan, decisions = compress([a_risk(start=0.0, end=60.0)],
                                resolver=resolver, protected=protected)

    assert not decisions[0].accepted
    assert decisions[0].reject_code == "protected_range"
    assert "protection is applied before" in decisions[0].rejected_reason


def test_context_is_kept_at_each_end_of_a_compressed_stretch():
    resolver = a_resolver([
        Slot(0, 60, 0, 60, said="", importance="setup", actions=["idle"]),
    ])
    _plan, decisions = compress([a_risk(start=0.0, end=60.0)],
                                resolver=resolver, keep_context_seconds=5.0)

    span = decisions[0].spans[0]
    assert span.start == 5.0, "five seconds of context at the head"
    assert span.end == 55.0, "and at the tail"


def test_a_stretch_with_no_room_after_context_is_refused():
    resolver = a_resolver([
        Slot(0, 6, 0, 6, said="", importance="setup", actions=["idle"]),
    ])
    _plan, decisions = compress([a_risk(start=0.0, end=6.0)],
                                resolver=resolver, keep_context_seconds=5.0)
    assert decisions[0].reject_code == "too_short"


def test_a_low_severity_risk_is_marked_rather_than_cut():
    _plan, decisions = compress([a_risk(severity="low")],
                                min_risk_severity="medium")
    assert not decisions[0].accepted
    assert decisions[0].reject_code == "low_confidence"
    assert decisions[0].action == "marker_only"


def test_a_story_problem_is_not_treated_as_a_length_problem():
    """Shortening a confusing transition does not make it less confusing."""
    _plan, decisions = compress([a_risk(risk="confusing_transition")])
    assert not decisions[0].accepted
    assert "story problem, not a length one" in decisions[0].rejected_reason


def test_compression_stops_at_its_ceiling():
    slots = [Slot(i * 60, i * 60 + 60, i * 60, i * 60 + 60, said="",
                  actions=["idle"], segment_id=f"s_{i}")
             for i in range(5)]
    risks = [a_risk(item_id=f"risk_{i}", start=i * 60.0, end=i * 60.0 + 60.0)
             for i in range(5)]
    plan, decisions = compress(
        risks, resolver=a_resolver(slots), base_seconds=300.0,
        max_compression_share=0.2, keep_context_seconds=0.0)

    accepted = [item for item in decisions if item.accepted]
    assert len(accepted) < 5
    assert plan.seconds_removed <= 300.0 * 0.2 + 1.0
    assert any("ceiling" in warning for warning in plan.warnings)


def test_compression_can_be_switched_off():
    plan, decisions = compress([a_risk()], compress_sag=False)
    assert not decisions[0].accepted
    assert decisions[0].reject_code == "disabled"
    assert any("switched off" in warning for warning in plan.warnings)


def test_an_unresolvable_risk_is_refused():
    _plan, decisions = compress([a_risk(start=900.0, end=960.0)])
    assert decisions[0].reject_code == "unresolvable"


def test_every_zone_produces_exactly_one_decision():
    """So a report can account for all of them."""
    risks = [a_risk(item_id=f"risk_{i}", start=i * 10.0, end=i * 10.0 + 10.0)
             for i in range(4)]
    plan, decisions = compress(risks)
    assert len(decisions) == 4
    assert len(plan.zones) == 4


# ---------------------------------------------------------------------------
# Dead air
# ---------------------------------------------------------------------------

def sweep(slots, *, protected=(), **settings):
    resolver = a_resolver(slots)
    return deadair_module.sweep(
        resolver, RetentionCutConfig(**settings).validated(), protected)


def silent_slot(start, end, source_start, source_end, **fields):
    slot = Slot(start, end, source_start, source_end, said="", **fields)
    slot.segment.audio_events = [audio(source_start, source_end, "silence")]
    return slot


def test_ordinary_silence_is_trimmed_to_the_limit():
    records, decisions = sweep(
        [silent_slot(0, 20, 0, 20, importance="setup", actions=["idle"])],
        dead_air_aggressiveness="high")

    assert records and records[0].accepted
    assert records[0].seconds_kept == pytest.approx(0.6)
    assert decisions and decisions[0].action == "cut"


def test_short_silence_is_left_alone():
    slot = Slot(0, 20, 0, 20, said="")
    slot.segment.audio_events = [audio(0, 0.5, "silence")]
    records, decisions = sweep([slot], dead_air_aggressiveness="low")
    assert records and not records[0].accepted
    assert decisions == []


def test_the_aggressive_setting_cuts_what_the_gentle_one_keeps():
    slot = lambda: silent_slot(0, 20, 0, 20, actions=["idle"])  # noqa: E731
    gentle, _d = sweep([slot()], dead_air_aggressiveness="low",
                       max_ordinary_silence=25.0)
    hard, _d2 = sweep([slot()], dead_air_aggressiveness="high")

    assert not gentle[0].accepted
    assert hard[0].accepted


def test_silence_after_a_reaction_is_kept_as_aftermath():
    """The beat after a scream is the joke."""
    slots = [
        Slot(0, 10, 0, 10, said="aaah", audio=["sudden_reaction"]),
        silent_slot(10, 24, 10, 24, actions=["idle"]),
    ]
    records, _decisions = sweep(slots, dead_air_aggressiveness="high",
                                max_purposeful_silence=20.0)
    silent = [r for r in records if r.episode_start >= 10]
    assert silent and silent[0].purpose == "aftermath"
    assert not silent[0].accepted


def test_silence_around_a_payoff_is_kept_as_tension():
    slots = [
        silent_slot(0, 14, 0, 14, importance="payoff", actions=["idle"]),
    ]
    records, _decisions = sweep(slots, dead_air_aggressiveness="high",
                                max_purposeful_silence=20.0)
    assert records[0].purpose == "tension"
    assert records[0].is_purposeful
    assert not records[0].accepted


def test_silence_between_two_places_is_kept_as_a_transition():
    slots = [
        Slot(0, 5, 0, 5, said="x", environment="cave"),
        silent_slot(5, 19, 5, 19, environment="nether", actions=["idle"]),
        Slot(19, 25, 19, 25, said="y", environment="plains"),
    ]
    records, _decisions = sweep(slots, dead_air_aggressiveness="high",
                                max_purposeful_silence=20.0)
    silent = [r for r in records if r.episode_start >= 5]
    assert silent and silent[0].purpose == "transition"


def test_purposeful_silence_is_still_capped():
    """Long enough to land, short enough not to become its own problem."""
    slots = [silent_slot(0, 30, 0, 30, importance="payoff", actions=["idle"])]
    records, decisions = sweep(slots, max_purposeful_silence=2.5)

    assert records[0].is_purposeful
    assert records[0].accepted, "capped even though it is doing something"
    assert records[0].seconds_kept == pytest.approx(2.5)
    assert decisions


def test_silence_with_speech_across_it_is_speech_pacing():
    slot = Slot(0, 20, 0, 20, said="I am still talking here")
    slot.segment.audio_events = [audio(0, 20, "silence")]
    records, decisions = sweep([slot], dead_air_aggressiveness="high")
    assert not records[0].accepted
    assert "speech pacing" in records[0].rejected_reason
    assert decisions == []


def test_silence_inside_protected_footage_is_left_alone():
    slots = [silent_slot(0, 20, 100, 120, actions=["idle"])]
    protected = [SourceSpan(asset_id="a_test", start=100.0, end=120.0)]
    records, decisions = sweep(slots, protected=protected,
                               dead_air_aggressiveness="high")
    assert not records[0].accepted
    assert "protected" in records[0].rejected_reason
    assert decisions == []


def test_dead_air_handling_can_be_switched_off():
    records, decisions = sweep(
        [silent_slot(0, 20, 0, 20, actions=["idle"])], kill_dead_air=False)
    assert records == [] and decisions == []


# ---------------------------------------------------------------------------
# The compiler
# ---------------------------------------------------------------------------

_UNSET = object()


def build(memory=None, retention=_UNSET, base=None, timeline=None,
          roughcut=None, **settings):
    """Compile with hand-built inputs.

    The memory defaults to a ``timeline`` timebase because there is no rough
    cut here -- a ``roughcut`` memory resolved without one is a mismatch the
    compiler now refuses, which is its own test.
    """
    return compile_module.build(
        memory or Memory(timebase="timeline"),
        Retention() if retention is _UNSET else retention,
        base if base is not None else [a_range(0, 260)],
        timeline if timeline is not None else a_timeline(),
        config=RetentionCutConfig(**settings).validated(),
        roughcut=roughcut,
    )


def test_report_only_decides_everything_and_changes_nothing():
    base = [a_range(0, 260)]
    plan, ranges = build(
        retention=Retention(risks=[a_risk(start=0.0, end=60.0)]),
        base=base, mode="report_only")

    assert plan.decisions, "the decisions were still made"
    assert not plan.applied
    assert ranges == base, "and none of them was applied"
    assert any("changes nothing" in w or "none was applied" in w
               for w in plan.warnings)


def test_mode_off_does_not_even_decide():
    plan, ranges = build(mode="off", base=[a_range(0, 260)])
    assert plan.failure is not None
    assert plan.failure.code == "mode_is_off"
    assert plan.decisions == []
    assert len(ranges) == 1


def test_no_retention_plan_fails_with_the_command_to_make_one():
    plan, _ranges = build(retention=None, mode="retention")
    assert plan.failure.stage == "no_retention_plan"
    assert "plan-retention" in plan.failure.hint


def test_a_roughcut_memory_without_a_rough_cut_is_refused():
    """Sequence times read against a synthetic ordering are all wrong.

    Every number would still be a number and every finding would land
    somewhere, which is exactly why this has to refuse rather than resolve.
    """
    plan, ranges = build(memory=Memory(timebase="roughcut"),
                         base=[a_range(0, 260)], mode="retention")

    assert plan.failure is not None
    assert plan.failure.code == "timebase_mismatch"
    assert "roughcut build" in plan.failure.hint
    assert len(ranges) == 1, "the cut is handed back untouched"


def test_no_base_cut_fails_clearly():
    plan, ranges = build(base=[], mode="retention")
    assert plan.failure.stage == "no_base_cut"
    assert "roughcut build" in plan.failure.hint
    assert ranges == []


def test_protection_is_claimed_before_compression_runs():
    """The ordering that makes the whole layer safe."""
    memory = Memory(
        setups=[Item("set_1", 0.0, 20.0, payoff_id="pay_1")],
        payoffs=[Item("pay_1", 200.0, 230.0, setup_id="set_1")],
    )
    retention = Retention(risks=[a_risk(start=0.0, end=20.0)])
    plan, _ranges = build(memory=memory, retention=retention,
                          base=[a_range(0, 260)], mode="retention")

    protecting = [d for d in plan.accepted if d.protects]
    assert protecting, "the setup was claimed"
    refused = [d for d in plan.decisions
               if d.source_type == "risk" and not d.accepted]
    assert refused and refused[0].reject_code == "protected_range"


def test_a_decision_on_footage_the_cut_does_not_contain_is_refused():
    plan, _ranges = build(
        retention=Retention(risks=[a_risk(start=0.0, end=20.0)]),
        base=[a_range(230, 260)], mode="retention")

    refused = [d for d in plan.decisions if d.reject_code == "unresolvable"]
    assert refused


def test_the_base_cut_is_never_mutated():
    base = [a_range(0, 260)]
    before = (base[0].start, base[0].end, base[0].speed, base[0].protected)
    _plan, ranges = build(
        retention=Retention(hooks=[a_hook(start=20.0, end=32.0)]),
        base=base, mode="retention")

    assert (base[0].start, base[0].end, base[0].speed,
            base[0].protected) == before
    assert ranges is not base


def test_a_cold_open_lands_at_the_front_of_the_cut():
    _plan, ranges = build(
        retention=Retention(hooks=[a_hook(start=140.0, end=160.0)]),
        base=[a_range(0, 260)], mode="retention")

    assert ranges
    assert ranges[0].keep_reason == "reveal"
    assert ranges[0].protected
    assert "COLD OPEN" in ranges[0].notes


def test_the_cold_open_footage_is_carved_out_of_where_it_used_to_be():
    """Otherwise the opening plays twice, which reads as a bug."""
    _plan, ranges = build(
        retention=Retention(hooks=[a_hook(start=140.0, end=160.0)]),
        base=[a_range(0, 260)], mode="retention")

    opening = ranges[0]
    later = ranges[1:]
    for entry in later:
        overlap = max(0.0, min(entry.end, opening.end)
                      - max(entry.start, opening.start))
        assert overlap <= 0.1, "the same footage is in the cut twice"


def test_a_protected_range_is_marked_and_never_retimed():
    memory = Memory(payoffs=[Item("pay_1", 200.0, 230.0)])
    _plan, ranges = build(
        memory=memory,
        retention=Retention(climax=Item("pay_1", 200.0, 230.0)),
        base=[a_range(200, 230, speed=2.0)], mode="retention")

    covering = [entry for entry in ranges if entry.start >= 200]
    assert covering and covering[0].protected
    assert covering[0].speed == 1.0, "protection un-retimes"


def test_a_cut_decision_splits_a_range_it_lands_inside():
    resolver_slots = [Slot(0, 260, 0, 260, said="", actions=["idle"],
                           segment_id="s_all")]
    plan, decisions = sag_module.compress(
        [a_risk(start=100.0, end=140.0)],
        resolve_module.Resolver(Track(resolver_slots)),
        RetentionCutConfig(keep_context_seconds=0.0).validated(),
        [], base_seconds=260.0,
    )
    assert decisions[0].accepted

    built = RetentionCutPlan(mode="retention", decisions=decisions,
                             sag=plan)
    ranges = compile_module.apply(
        [a_range(0, 260)], built, RetentionCutConfig(mode="retention"))

    assert len(ranges) == 2, "the range split around the cut"
    assert ranges[0].end == pytest.approx(100.0)
    assert ranges[1].start == pytest.approx(140.0)


def test_the_compiler_records_where_the_findings_came_from():
    plan, _ranges = build(mode="retention")
    assert plan.sources["episode_memory"] is True
    assert plan.sources["retention_plan"] is True
    assert plan.timebase in ("roughcut", "timeline", "empty")


def test_a_dead_air_record_and_its_decision_never_disagree():
    """Two objects describing one judgement, kept in step by validation."""
    memory = Memory(payoffs=[Item("pay_1", 0.0, 400.0)])
    plan, _ranges = build(
        memory=memory,
        retention=Retention(climax=Item("pay_1", 0.0, 400.0)),
        base=[a_range(0, 260)], mode="retention",
        dead_air_aggressiveness="high")

    by_id = {d.decision_id: d for d in plan.decisions}
    for record in plan.dead_air:
        decision = by_id.get(record.decision_id)
        if decision is None:
            continue
        assert record.accepted == decision.accepted
        if not record.accepted:
            assert record.seconds_removed == 0.0


# ---------------------------------------------------------------------------
# Choosing the base cut
# ---------------------------------------------------------------------------

class FakeDirectorPlan:
    def __init__(self, ranges):
        from editing.director.schema import DirectorRange
        self.ranges = [
            DirectorRange(asset_id="a_test", source_file=ASSET.path,
                          start=start, end=end, keep_reason="payoff",
                          decision_id="d_1")
            for start, end in ranges
        ]
        self.decisions = []
        self.accepted = []
        self.rejected = []


def base_for(mode, *, roughcut=None, director=None, timeline=None):
    return run_module._base(
        RetentionCutConfig(mode=mode).validated(),
        timeline or a_timeline(), None, roughcut, director,
        RoughCutOptions(), [ASSET],
    )


def test_director_retention_uses_the_director_cut():
    director = FakeDirectorPlan([(100.0, 140.0)])
    ranges, base, failure = base_for("director_retention", director=director)

    assert failure is None
    assert base == "director"
    assert ranges[0].start == 100.0


def test_director_retention_without_a_director_plan_fails_clearly():
    """"I asked for the director's cut and did not get it" is a real answer."""
    ranges, base, failure = base_for("director_retention")
    assert failure is not None
    assert failure.code == "no_director_plan"
    assert "director plan" in failure.hint
    assert ranges == []


def test_hybrid_falls_back_to_the_heuristic_without_a_director_plan(timeline):
    from editing.roughcut.build import build_rough_cut

    cut = build_rough_cut(timeline, validate=False)
    ranges, base, failure = base_for("hybrid", roughcut=cut,
                                     timeline=timeline)
    assert failure is None
    assert base == "heuristic"
    assert ranges


def test_hybrid_prefers_the_director_cut_when_there_is_one():
    director = FakeDirectorPlan([(100.0, 140.0)])
    _ranges, base, failure = base_for("hybrid", director=director)
    assert base == "director" and failure is None


def test_the_existing_cut_is_read_rather_than_re_derived(timeline):
    """A cut edited by hand, or built by the director, must survive."""
    from editing.roughcut.build import build_rough_cut

    cut = build_rough_cut(timeline, validate=False)
    cut.placements[0].source_in = 3.0
    ranges, _base, _failure = base_for("retention", roughcut=cut,
                                       timeline=timeline)
    assert ranges[0].start == 3.0


# ---------------------------------------------------------------------------
# Becoming a rough cut
# ---------------------------------------------------------------------------

def test_retention_ranges_become_a_cut_through_the_existing_builder(timeline):
    ranges = [a_range(0, 20), a_range(140, 170)]
    cut = run_module.to_rough_cut(
        ranges, timeline, assets=[ASSET],
        sequence_name="Nova Retention Cut")

    assert len(cut.placements) == 2
    assert cut.sequence_name == "Nova Retention Cut"
    assert cut.ops, "the same operations as any other cut"
    assert cut.on_scratch
    assert any("supplied by the caller" in line for line in cut.explanation)


def test_a_retention_cut_still_passes_the_same_dry_run(timeline):
    cut = run_module.to_rough_cut(
        [a_range(0, 20), a_range(140, 170)], timeline, assets=[ASSET],
        validate=True)
    assert cut.dry_run_passed


def test_preselected_with_no_ranges_falls_back_to_the_thresholds(timeline):
    from editing.roughcut.build import build_rough_cut

    cut = build_rough_cut(
        timeline, options=RoughCutOptions(mode="preselected"),
        preselected=[], validate=False)
    assert cut.placements
    assert any("rule-based selector" in line for line in cut.explanation)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def test_the_comparison_counts_what_changed():
    plan, ranges = build(
        retention=Retention(hooks=[a_hook(start=140.0, end=160.0)],
                            risks=[a_risk(start=0.0, end=20.0)]),
        base=[a_range(0, 260)], mode="retention")
    comparison = compare_module.compare(plan, [a_range(0, 260)], ranges)

    assert comparison.before["ranges"] == 1
    assert comparison.after["ranges"] == len(ranges)
    assert "seconds_removed" in comparison.difference
    assert comparison.cold_open["chosen"] is True


def test_the_comparison_never_claims_analytics():
    plan, ranges = build(
        retention=Retention(hooks=[a_hook(start=140.0, end=160.0)]),
        base=[a_range(0, 260)], mode="retention")
    comparison = compare_module.compare(plan, [a_range(0, 260)], ranges)
    text = compare_module.render(comparison) + json.dumps(
        comparison.to_dict())

    # The disclaimer itself says the words "retention" and "watch time", so
    # what is checked is the *claim*: no phrasing anywhere that reads as a
    # prediction about an audience.
    lowered = text.lower()
    for phrase in ("retention improved", "improves retention", "% more",
                   "more watchable", "viewers will", "will retain",
                   "boost", "uplift"):
        assert phrase not in lowered, phrase
    assert comparison.not_measured
    assert "not a claim about what a viewer will do" in         comparison.not_measured


def test_the_comparison_reports_duplicated_footage_from_the_result():
    """Checked on the cut, not trusted from the policy."""
    plan = RetentionCutPlan(mode="retention")
    duplicated = [a_range(100, 140), a_range(120, 160)]
    comparison = compare_module.compare(plan, [], duplicated)

    assert comparison.duplicated_footage
    assert comparison.duplicated_footage[0]["seconds"] == pytest.approx(20.0)
    assert any("appear twice" in note for note in comparison.notes)


def test_the_comparison_lists_refused_actions():
    plan, ranges = build(
        memory=Memory(
            setups=[Item("set_1", 0.0, 20.0, payoff_id="pay_1")],
            payoffs=[Item("pay_1", 200.0, 230.0, setup_id="set_1")]),
        retention=Retention(risks=[a_risk(start=0.0, end=20.0)]),
        base=[a_range(0, 260)], mode="retention")
    comparison = compare_module.compare(plan, [a_range(0, 260)], ranges)

    assert comparison.rejected
    assert comparison.difference["actions_refused"] >= 1
    assert any("refused by the rules" in note for note in comparison.notes)


def test_the_comparison_renders_the_commands_to_actually_tell():
    plan, ranges = build(mode="retention", base=[a_range(0, 260)])
    text = compare_module.render(
        compare_module.compare(plan, [a_range(0, 260)], ranges))

    assert "RETENTION CUT vs" in text
    assert "HOW TO ACTUALLY TELL" in text
    assert "retention render" in text


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_the_report_says_what_it_opened_on_and_what_it_refused():
    plan, _ranges = build(
        memory=Memory(
            setups=[Item("set_1", 0.0, 20.0, payoff_id="pay_1")],
            payoffs=[Item("pay_1", 200.0, 230.0, setup_id="set_1")]),
        retention=Retention(hooks=[a_hook(start=20.0, end=32.0)],
                            risks=[a_risk(start=0.0, end=20.0)]),
        base=[a_range(0, 260)], mode="retention")
    text = report_module.render(plan)

    assert "OPENS ON" in text
    assert "WHAT THE RULES REFUSED" in text
    assert "PROTECTED" in text
    assert "SILENCE" in text


def test_the_report_states_its_limitations_every_time():
    text = report_module.render(RetentionCutPlan())
    assert "Nothing here measures retention" in text
    assert "executes nothing" in text
    assert "calibrated against intuition" in text


def test_a_failed_report_says_the_old_cut_is_untouched():
    plan = RetentionCutPlan(failure=RetentionCutFailure(
        stage="no_retention_plan", message="nothing to wire",
        hint="run plan-retention"))
    text = report_module.render(plan)

    assert "NOTHING WAS DONE" in text
    assert "run plan-retention" in text
    assert "untouched" in text


def test_the_cold_open_view_shows_every_refusal():
    plan, _ranges = build(
        retention=Retention(hooks=[a_hook(score=0.1),
                                   a_hook(hook_type="goal")]),
        base=[a_range(0, 260)], mode="retention")
    text = report_module.render_cold_open(plan)

    assert "No cold open was chosen" in text
    assert "CANDIDATES REFUSED" in text


def test_the_protected_view_separates_what_was_not_protected():
    plan, _ranges = build(
        memory=Memory(setups=[Item("set_1", 0.0, 20.0, payoff_id="")]),
        base=[a_range(0, 260)], mode="retention")
    text = report_module.render_protected(plan)
    assert "NOT PROTECTED" in text


def test_the_compression_and_rejected_views_render():
    plan, _ranges = build(
        retention=Retention(risks=[a_risk(risk="confusing_transition")]),
        base=[a_range(0, 260)], mode="retention")

    assert "COMPRESSION" in report_module.render_compression(plan)
    assert "REFUSED RETENTION ACTIONS" in report_module.render_rejected(plan)


def test_the_report_points_at_the_next_command():
    plan, _ranges = build(mode="report_only", base=[a_range(0, 260)])
    commands = report_module.next_commands(plan)
    assert any("show-cold-open" in c for c in commands)
    assert any("--mode retention" in c for c in commands)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_a_plan_is_written_and_read_back(config):
    plan = RetentionCutPlan(name="structure", mode="retention")
    store_module.save_plan(config, plan)
    store_module.save_report(config, "report text")

    assert store_module.load_plan(config).mode == "retention"
    assert store_module.report_path(config).read_text("utf-8") == "report text"


def test_a_missing_plan_says_how_to_build_one(config):
    with pytest.raises(EditingError) as caught:
        store_module.load_plan(config)
    assert "retention plan" in caught.value.hint
    assert store_module.plan_or_none(config) is None


def test_an_unreadable_plan_is_none_rather_than_an_exception(config):
    store_module.plan_path(config).parent.mkdir(parents=True, exist_ok=True)
    store_module.plan_path(config).write_text("{ broken", encoding="utf-8")
    assert store_module.plan_or_none(config) is None


def test_the_retention_cut_is_written_beside_the_original_not_over_it(
        config, timeline):
    """Disagreeing with this pass must not cost the cut it argued with."""
    cut = run_module.to_rough_cut([a_range(0, 20)], timeline, assets=[ASSET])
    store_module.save_roughcut(config, cut)

    assert store_module.roughcut_path(config).exists()
    assert store_module.roughcut_path(config).parent == config.retention_dir
    assert not (config.roughcut_dir / "structure.json").exists()


def test_a_missing_retention_cut_explains_report_only_mode(config):
    with pytest.raises(EditingError) as caught:
        store_module.load_roughcut(config)
    assert "report-only" in caught.value.hint
    assert store_module.roughcut_or_none(config) is None


def test_retention_artifacts_live_in_their_own_directory(config):
    assert config.retention_dir.name == "retention"
    assert config.retention_dir.parent == config.output_dir


# ---------------------------------------------------------------------------
# Through the pipeline
# ---------------------------------------------------------------------------

def _pipeline(config, sampling):
    from editing.pipeline import build_pipeline
    return build_pipeline(config, sampling)


def a_prepared_pipeline(config, sampling, timeline):
    """A pipeline with a timeline, a cut, a memory and a retention plan."""
    pipeline = _pipeline(config, sampling)
    pipeline.assets = [ASSET]
    pipeline.write_timeline(timeline)
    recommendations = pipeline.recommend(timeline)
    pipeline.write_recommendations(recommendations)
    cut = pipeline.rough_cut(timeline=timeline,
                             recommendations=recommendations, validate=False)
    memory = pipeline.episode_memory(timeline=timeline, roughcut=cut)
    pipeline.retention_plan(memory=memory, timeline=timeline, roughcut=cut)
    return pipeline


def test_the_pipeline_builds_a_retention_cut(config, sampling, timeline):
    pipeline = a_prepared_pipeline(config, sampling, timeline)
    plan, cut = pipeline.retention_cut(
        settings=pipeline.retention_config(mode="retention"))

    assert plan.ok
    assert plan.applied
    assert cut is not None and cut.placements
    assert store_module.plan_path(config).exists()
    assert store_module.roughcut_path(config).exists()


def test_report_only_writes_no_cut(config, sampling, timeline):
    pipeline = a_prepared_pipeline(config, sampling, timeline)
    plan, cut = pipeline.retention_cut(
        settings=pipeline.retention_config(mode="report_only"))

    assert cut is None, "nothing changed, so there is no variant to write"
    assert not plan.applied
    assert store_module.plan_path(config).exists(), "the decisions are kept"
    assert pipeline.retention_roughcut_or_none() is None


def test_the_pipeline_compares_the_two_cuts(config, sampling, timeline):
    pipeline = a_prepared_pipeline(config, sampling, timeline)
    pipeline.retention_cut(
        settings=pipeline.retention_config(mode="retention"))

    comparison = pipeline.compare_retention()
    assert comparison.before["ranges"] > 0
    assert store_module.compare_path(config).exists()
    assert comparison.not_measured


def test_the_original_cut_survives_a_retention_pass(config, sampling,
                                                    timeline):
    pipeline = a_prepared_pipeline(config, sampling, timeline)
    before = pipeline.load_rough_cut()
    pipeline.retention_cut(
        settings=pipeline.retention_config(mode="retention"))
    after = pipeline.load_rough_cut()

    assert len(after.placements) == len(before.placements)
    assert [p.source_in for p in after.placements] == \
        [p.source_in for p in before.placements]


def test_the_pipeline_reports_on_a_retention_cut(config, sampling, timeline):
    pipeline = a_prepared_pipeline(config, sampling, timeline)
    pipeline.retention_cut(
        settings=pipeline.retention_config(mode="retention"))
    assert "RETENTION CUT" in pipeline.retention_report()


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def test_the_retention_commands_parse():
    from editing.cli import build_parser

    parser = build_parser()
    for argv, expected in (
        (["retention", "plan"], "plan"),
        (["retention", "plan", "--mode", "director_retention"], "plan"),
        (["retention", "report"], "report"),
        (["retention", "show-cold-open"], "show-cold-open"),
        (["retention", "show-compression"], "show-compression"),
        (["retention", "show-protected"], "show-protected"),
        (["retention", "show-rejected"], "show-rejected"),
        (["retention", "compare"], "compare"),
        (["retention", "render", "--quality", "proxy"], "render"),
    ):
        args = parser.parse_args(argv)
        assert args.retention_command == expected
        assert args.func.__name__ == "cmd_retention"


def test_retention_options_reach_the_parsed_arguments():
    from editing.cli import build_parser

    args = build_parser().parse_args([
        "retention", "plan", "--mode", "retention", "--no-cold-open",
        "--max-cold-open-seconds", "15", "--duplicate-policy", "shorten",
        "--dead-air-aggressiveness", "high", "--grind-speed", "3",
        "--max-compression", "0.3", "--target", "600",
    ])
    assert args.mode == "retention"
    assert args.no_cold_open is True
    assert args.max_cold_open_seconds == 15.0
    assert args.duplicate_policy == "shorten"
    assert args.dead_air == "high"
    assert args.grind_speed == 3.0
    assert args.max_compression == 0.3
    assert args.target == 600.0


def test_every_retention_command_can_be_scoped_to_an_auto_run():
    from editing.cli import build_parser

    parser = build_parser()
    for command in ("plan", "report", "show-cold-open", "compare", "render"):
        args = parser.parse_args(
            ["retention", command, "--run", "20260101T000000-abc-style"])
        assert args.run == "20260101T000000-abc-style"


def test_an_unknown_retention_subcommand_is_a_usage_error():
    from editing.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["retention", "improvise"])


def test_planning_from_the_command_line_runs(config, sampling, timeline,
                                             monkeypatch, capsys):
    """Parsing is not running."""
    from editing import cli

    pipeline = a_prepared_pipeline(config, sampling, timeline)
    monkeypatch.setattr(cli, "_run_scoped_pipeline", lambda args: pipeline)

    assert cli.main(["retention", "plan", "--mode", "retention"]) == 0
    assert "RETENTION CUT" in capsys.readouterr().out

    for command in ("report", "show-cold-open", "show-compression",
                    "show-protected", "show-rejected", "compare"):
        assert cli.main(["retention", command]) == 0, command


def test_rendering_without_a_retention_cut_says_why(config, sampling,
                                                    timeline, monkeypatch):
    from editing import cli

    pipeline = a_prepared_pipeline(config, sampling, timeline)
    pipeline.retention_cut(
        settings=pipeline.retention_config(mode="report_only"))
    monkeypatch.setattr(cli, "_run_scoped_pipeline", lambda args: pipeline)

    assert cli.main(["retention", "render"]) == 1


def test_rendering_a_retention_cut_from_the_command_line(
        config, sampling, tmp_path, monkeypatch):
    """The whole chain: findings -> reshaped cut -> a render job."""
    from editing import cli
    from editing.render.runner import MockRunner

    clip = tmp_path / "ep12.mp4"
    clip.write_bytes(b"x" * 4096)
    asset = MediaAsset(asset_id="a_test", path=str(clip),
                       filename="ep12.mp4", duration=400.0)

    pipeline = a_prepared_pipeline(config, sampling, a_timeline())
    pipeline.assets = [asset]
    pipeline.retention_cut(
        settings=pipeline.retention_config(mode="retention"))

    monkeypatch.setattr(cli, "_run_scoped_pipeline", lambda args: pipeline)
    monkeypatch.setattr(
        "editing.render.runner.build_runner",
        lambda config, backend="ffmpeg": MockRunner())

    assert cli.main(["retention", "render", "--mock", "--json"]) == 0
    job = pipeline.render_job()
    assert job.segments, "the retention ranges reached the renderer"


def test_nothing_in_the_package_shells_out_or_reaches_the_network():
    """No subprocess, no requests: this layer is pure decision-making."""
    import ast

    package = Path(__file__).resolve().parents[2] / "editing" / "retention"
    heavy = {"subprocess", "requests"}
    offenders = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            if heavy.intersection(names):
                offenders.append(path.name)
    assert offenders == [], offenders

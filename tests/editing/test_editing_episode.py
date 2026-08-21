"""The episode layer: story, questions, risks, hooks and what to do about them.

Four properties carry the weight here, and each is asserted from several
angles rather than once.

**Uncertainty survives.** The layer's whole claim is that it says what it can
support and no more. So the confidence cap is tested directly (one channel can
never reach the edit threshold), through the detectors (a keyword-only beat is
recorded and cannot affect an edit), and at the edges (empty evidence produces
low confidence and a review flag, never silence and never certainty).

**No fake analytics.** Nothing this layer generates may claim to know what
viewers will do. That is asserted over every generated string in a full plan,
not spot-checked, because the failure mode is one reason line drifting into a
promise.

**Conservative fixes.** A marker is always safe; a change to timing is safe
only where the evidence was *measured*. The rule lives in one function and is
tested there as well as through the suggestions that carry it.

**One answer per question.** The memory and the plan must not each decide the
climax separately -- they did once, and disagreed on real footage.

Nothing here needs FFmpeg, a GPU, a model server, Premiere or real footage.
"""
from __future__ import annotations

import json

import pytest

from editing.align import build_timeline
from editing.errors import EditingError
from editing.recommend.schema import (
    EditRecommendation, Evidence, RecommendationSet,
)
from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.schema import (
    AudioEvent, MediaAsset, TimeRange, Transcript, TranscriptEntry, UIState,
    VisualEvent,
)
from editing.episode import beats as beats_module
from editing.episode import language
from editing.episode import loops as loops_module
from editing.episode import memory as memory_module
from editing.episode import plan as plan_module
from editing.episode import report as episode_report
from editing.episode import risks as risks_module
from editing.episode import suggest as suggest_module
from editing.episode import track as track_module
from editing.episode.schema import (
    BEAT_KINDS, CONFIDENCE_CAP, DOWNSTREAM_FOR, EpisodeBeat, EpisodeEvidence,
    EpisodeMemory, EpisodeRetentionPlan, EpisodeRiskZone,
    MARKER_SUGGESTIONS, MIN_EDIT_CONFIDENCE, NOT_ANALYTICS,
    RISK_TYPES, SUGGESTION_TYPES, TIMING_SUGGESTIONS, capped, cap_for,
    contains_claim, new_id,
)

ASSET = MediaAsset(
    asset_id="a_ep", path="/footage/ep12.mp4", filename="ep12.mp4",
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
        motion_score=kw.pop("motion", 0.5),
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


def timeline_of(events, *, lines=(), audio_events=(), sampling=None):
    transcript = Transcript(
        asset_id=ASSET.asset_id, source="srt",
        entries=[TranscriptEntry(*line) for line in lines],
    ) if lines else None
    built = build_timeline(
        [ASSET], {ASSET.asset_id: list(events)},
        {ASSET.asset_id: transcript} if transcript else {},
        audio_by_asset={ASSET.asset_id: list(audio_events)},
    )
    if sampling is not None:
        built.sampling = dict(sampling)
    return built


def one_clip_cut(duration=360.0, **kw):
    """A cut that is one long clip, so source time == sequence time."""
    return RoughCutPlan(
        sequence_name=SEQUENCE,
        placements=[ClipPlacement(
            placement_id="p_1", asset_id=ASSET.asset_id,
            source_file=ASSET.path, source_in=0.0, source_out=duration,
            sequence_start=0.0, index=0, segment_ids=["s_0"], **kw,
        )],
        on_scratch=True,
    )


STORY_EVENTS = [
    visual(0, 20, environment="forest", actions=("travelling",),
           importance="setup"),
    visual(20, 40, importance="boring", actions=("travelling",), motion=0.2),
    visual(40, 60, importance="tension", threats=("creeper",)),
    visual(60, 80, actions=("mining",), importance="boring"),
    visual(80, 100, actions=("mining",), importance="boring"),
    visual(100, 120, actions=("mining",), importance="boring"),
    visual(120, 140, actions=("looting",), importance="payoff",
           entities=("diamond",)),
    visual(140, 160, actions=("fighting",), importance="danger",
           threats=("creeper",)),
    visual(160, 190, environment="nether", actions=("travelling",),
           importance="reveal"),
    visual(190, 220, environment="nether", actions=("fighting",),
           importance="danger", threats=("piglin",), ui=UIState(low_health=True)),
    visual(220, 250, environment="nether", actions=("dying",),
           importance="danger", ui=UIState(death_screen=True)),
    visual(250, 280, environment="base", actions=("building",),
           importance="setup"),
    visual(280, 320, environment="base", actions=("looting",),
           importance="payoff", entities=("diamond",)),
    visual(320, 360, environment="base", actions=("talking",),
           importance="setup"),
]

STORY_LINES = [
    (2, 8, "okay so the plan is to find some diamonds today"),
    (25, 30, "just walking for a bit here nothing much"),
    (45, 49, "wait what was that behind me"),
    (62, 68, "right lets get mining then"),
    (104, 110, "still mining still no diamonds"),
    (124, 130, "oh my god diamonds actual diamonds right there"),
    (144, 148, "creeper get away from me"),
    (164, 170, "we need to get to the nether fortress next"),
    (225, 230, "i died that is a death back to spawn"),
    (255, 262, "because this base needs a proper storage room"),
    (285, 292, "we did it we finally have the diamonds we wanted"),
    (330, 338, "thanks for watching see you next episode"),
]

STORY_AUDIO = [
    audio(20, 26, "silence", confidence=0.9),
    audio(46, 47, "sudden_reaction"),
    audio(80, 100, "low_energy", confidence=0.7),
    audio(125, 126, "sudden_reaction"),
    audio(145, 146, "possible_scream", confidence=0.5),
    audio(226, 228, "possible_laughter", confidence=0.45),
    audio(286, 288, "sudden_reaction"),
]


@pytest.fixture
def story_timeline():
    return timeline_of(
        STORY_EVENTS, lines=STORY_LINES, audio_events=STORY_AUDIO,
        sampling={"use_motion": True},
    )


@pytest.fixture
def cut():
    return one_clip_cut()


@pytest.fixture
def story(story_timeline, cut):
    return memory_module.build(story_timeline, roughcut=cut)


@pytest.fixture
def story_track(story_timeline, cut):
    return track_module.build(story_timeline, cut)


@pytest.fixture
def retention(story, story_timeline, cut):
    return plan_module.build(story, timeline=story_timeline, roughcut=cut)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_every_record_round_trips_through_json(self, story, retention):
        again = EpisodeMemory.from_dict(
            json.loads(json.dumps(story.to_dict())))
        assert len(again.beats) == len(story.beats)
        assert len(again.open_loops) == len(story.open_loops)
        assert again.timebase == story.timebase
        assert again.main_objective.text == story.main_objective.text

        replan = EpisodeRetentionPlan.from_dict(
            json.loads(json.dumps(retention.to_dict())))
        assert len(replan.risks) == len(retention.risks)
        assert len(replan.suggestions) == len(retention.suggestions)
        assert (replan.climax is None) == (retention.climax is None)

    def test_unknown_vocabulary_coerces_rather_than_raising(self):
        beat = EpisodeBeat.from_dict({
            "kind": "buildup", "start": 1.0, "end": 2.0, "confidence": 0.9,
        })
        assert beat.kind == "unknown"
        assert beat.kind in BEAT_KINDS

        zone = EpisodeRiskZone.from_dict({"risk": "vibes", "severity": "nuclear"})
        assert zone.risk in RISK_TYPES
        assert zone.severity == "low"

    def test_a_hand_written_file_keeps_its_segment_ids(self):
        """Top-level segment_ids are the only trace back to the footage."""
        beat = EpisodeBeat.from_dict({
            "kind": "danger", "segment_ids": ["s_9"], "start": 0, "end": 1,
        })
        assert beat.segment_ids == ["s_9"]

    def test_end_is_never_before_start(self):
        beat = EpisodeBeat.from_dict({"start": 90.0, "end": 10.0})
        assert beat.end >= beat.start

    def test_the_downstream_table_covers_every_suggestion_type(self):
        assert set(DOWNSTREAM_FOR) == set(SUGGESTION_TYPES)

    def test_marker_and_timing_suggestions_do_not_overlap(self):
        """A suggestion cannot be both always-safe and never-automatic."""
        assert not (MARKER_SUGGESTIONS & TIMING_SUGGESTIONS)


class TestConfidence:
    def test_one_channel_can_never_reach_the_edit_threshold(self):
        """The structural version of "do not depend only on keywords"."""
        assert CONFIDENCE_CAP[1] < MIN_EDIT_CONFIDENCE
        assert capped(1.0, {"transcript"}) < MIN_EDIT_CONFIDENCE

    def test_nothing_is_ever_certain(self):
        assert capped(1.0, {"visual", "transcript", "audio"}) < 1.0
        assert cap_for(3) < 0.9

    def test_a_recommendation_does_not_raise_the_cap(self):
        """Sessions 2-6 read the same three channels; they cannot vote twice."""
        evidence = EpisodeEvidence(
            quotes=["we need diamonds"],
            recommendation_ids=["r1", "r2", "r3"],
            layer_item_ids=["l1"],
        )
        assert evidence.channels == ["transcript"]
        assert capped(1.0, evidence.channels) == CONFIDENCE_CAP[1]
        assert evidence.corroborated is True

    def test_settle_downgrades_rather_than_deletes(self):
        beat = EpisodeBeat(
            item_id="b", start=0, end=1, confidence=0.99,
            evidence=EpisodeEvidence(quotes=["hi"]), affects_edit=True,
            needs_human_review=False,
        )
        beat.settle()
        assert beat.confidence == CONFIDENCE_CAP[1]
        assert beat.affects_edit is False
        assert beat.needs_human_review is True

    def test_no_evidence_at_all_is_low_confidence_not_zero_items(self):
        beat = EpisodeBeat(item_id="b", start=0, end=1, confidence=0.8)
        beat.settle()
        assert beat.confidence <= CONFIDENCE_CAP[0]
        assert beat.needs_human_review is True


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

class TestLanguage:
    def test_one_phrase_never_scores_two_families(self):
        """The Session 5 double-scoring bug, made impossible.

        "got it back" contains "got it". Longest-match-wins claims the
        characters, so the recovery family gets it and the payoff family does
        not also fire on the same words.
        """
        hits = language.cue_hits("okay we got it back")
        assert "recovery" in hits
        assert "payoff" not in hits

    def test_no_two_families_can_claim_the_same_characters(self):
        """Asserted over the whole cue table, not one example."""
        every = " ".join(
            phrase for phrases in language.CUES.values() for phrase in phrases
        )
        hits = language.cue_hits(every)
        found = [p for phrases in hits.values() for p in phrases]
        assert len(found) == len(set(found))

    def test_questions_are_recognised_with_and_without_a_mark(self):
        assert language.is_question("will we survive this cave?")
        assert language.is_question("can we get diamonds")
        assert not language.is_question("we are getting diamonds")

    def test_a_topic_drops_words_that_identify_nothing(self):
        topic = language.topic("okay so we are going to get the diamonds")
        assert "diamonds" in topic
        assert "the" not in topic and "okay" not in topic

    def test_a_shared_salient_word_outweighs_shared_filler(self):
        strong = language.topic_overlap(
            ["diamonds", "corner"], ["diamonds", "ledge"])
        weak = language.topic_overlap(["corner", "ledge"], ["corner", "patch"])
        assert strong > weak

    def test_topics_with_nothing_in_common_score_zero(self):
        assert language.topic_overlap(["diamonds"], ["villager"]) == 0.0

    def test_condense_never_invents_words(self):
        line = "oh my god there are diamonds right there in the wall"
        short = language.condense(line, limit=30)
        assert short.rstrip(".").strip() in line
        assert len(short) <= 34

    def test_repeated_phrases_ignore_pure_filler(self):
        found = dict(language.repeated_phrases([
            "and then we go", "and then we go", "the diamond curse strikes",
            "the diamond curse strikes again",
        ]))
        assert any("diamond curse" in phrase for phrase in found)
        assert "and then" not in found

    def test_a_sentence_opening_capital_is_not_read_as_a_name(self):
        assert "Okay" not in language.candidate_names("Okay we go now")
        assert "Tarun" in language.candidate_names("okay so Tarun has the totem")


# ---------------------------------------------------------------------------
# The episode clock
# ---------------------------------------------------------------------------

class TestTrack:
    def test_a_rough_cut_gives_sequence_time(self, story_timeline, cut):
        built = track_module.build(story_timeline, cut)
        assert built.timebase == "roughcut"
        assert built.sequence_name == SEQUENCE
        assert built.duration == pytest.approx(360.0)

    def test_no_rough_cut_gives_a_labelled_synthetic_ordering(
        self, story_timeline
    ):
        built = track_module.build(story_timeline, None)
        assert built.timebase == "timeline"
        assert built.duration > 0

    def test_a_retimed_clip_maps_source_time_through_its_speed(
        self, story_timeline
    ):
        """A 2x clip occupies half as much sequence time as source time."""
        fast = RoughCutPlan(
            sequence_name=SEQUENCE,
            placements=[ClipPlacement(
                placement_id="p_fast", asset_id=ASSET.asset_id,
                source_file=ASSET.path, source_in=0.0, source_out=120.0,
                sequence_start=0.0, index=0, speed=2.0,
            )],
        )
        built = track_module.build(story_timeline, fast)
        assert built.duration == pytest.approx(60.0)
        assert all(slot.speed == 2.0 for slot in built.slots)

    def test_a_placement_covering_no_analysis_still_occupies_the_clock(self):
        """Skipping it would shift every later moment on the timeline."""
        thin = timeline_of([visual(0, 10, importance="setup")])
        plan = RoughCutPlan(
            sequence_name=SEQUENCE,
            placements=[
                ClipPlacement(
                    placement_id="p_1", asset_id=ASSET.asset_id,
                    source_file=ASSET.path, source_in=0.0, source_out=10.0,
                    sequence_start=0.0, index=0),
                ClipPlacement(
                    placement_id="p_gap", asset_id=ASSET.asset_id,
                    source_file=ASSET.path, source_in=200.0, source_out=230.0,
                    sequence_start=10.0, index=1),
            ],
        )
        built = track_module.build(thin, plan)
        assert built.duration == pytest.approx(40.0)
        assert any("carries no evidence" in w for w in built.warnings)

    def test_motion_probing_is_reported_not_guessed(self):
        without = timeline_of(
            [visual(0, 10, motion=0.0)], sampling={"use_motion": False})
        assert track_module.build(without, None).has_motion is False
        with_motion = timeline_of([visual(0, 10, motion=0.6)])
        assert track_module.build(with_motion, None).has_motion is True

    def test_a_plateau_reports_one_spike_not_six(self):
        built = track_module.build(timeline_of([
            visual(0, 10, importance="boring"),
            visual(10, 20, importance="payoff"),
            visual(20, 30, importance="payoff"),
            visual(30, 40, importance="payoff"),
        ]), None)
        assert len(built.spikes()) == 1


# ---------------------------------------------------------------------------
# Beats
# ---------------------------------------------------------------------------

class TestBeats:
    def test_beats_come_from_several_channels_at_once(self, story):
        strong = [
            beat for beat in story.beats
            if len(beat.channels) >= 2 and beat.kind != "unknown"
        ]
        assert strong
        assert all(beat.confidence <= CONFIDENCE_CAP[len(beat.channels)]
                   for beat in story.beats)

    def test_the_visible_story_is_detected(self, story):
        kinds = {beat.kind for beat in story.beats}
        assert "objective_stated" in kinds
        assert "danger" in kinds
        assert "failure" in kinds
        assert "grind" in kinds
        assert "outro" in kinds

    def test_a_keyword_only_beat_is_recorded_and_cannot_move_a_frame(self):
        """The rule the whole layer rests on, at the detector."""
        spoken = timeline_of([], lines=[(0, 4, "we did it that worked")])
        built = track_module.build(spoken, None)
        found = beats_module.detect(built)
        assert found, "a transcript-only beat is still recorded"
        for beat in found:
            assert beat.channels == ["transcript"]
            assert beat.confidence < MIN_EDIT_CONFIDENCE
            assert beat.affects_edit is False
            assert beat.needs_human_review is True

    def test_adjacent_beats_of_one_kind_merge(self):
        """Three mining windows are one grind, not three."""
        built = track_module.build(timeline_of([
            visual(0, 20, actions=("mining",), importance="boring"),
            visual(20, 40, actions=("mining",), importance="boring"),
            visual(40, 60, actions=("mining",), importance="boring"),
        ]), None)
        found = beats_module.detect(built)
        grinds = [beat for beat in found if beat.kind == "grind"]
        assert len(grinds) == 1
        assert grinds[0].span_count == 3
        assert grinds[0].start == 0.0 and grinds[0].end == 60.0

    def test_merging_identical_weak_beats_cannot_lift_them_over_the_line(self):
        """Agreement raises confidence, but the channel cap still applies."""
        spoken = timeline_of([], lines=[
            (0, 4, "we did it that worked"),
            (5, 9, "we did it that worked"),
            (10, 14, "we did it that worked"),
        ])
        built = track_module.build(spoken, None)
        for beat in beats_module.detect(built):
            assert beat.confidence <= CONFIDENCE_CAP[1]

    def test_slots_with_nothing_to_say_stay_unknown(self):
        """Do not over-label: an unremarkable stretch keeps no label."""
        built = track_module.build(timeline_of([
            visual(0, 20, actions=("unknown",), importance="boring",
                   environment="unknown"),
        ]), None)
        found = beats_module.detect(built)
        assert [beat.kind for beat in found] == ["unknown"]
        assert found[0].confidence < MIN_EDIT_CONFIDENCE

    def test_a_close_call_stays_visible_as_one(self, story):
        with_alternative = [
            beat for beat in story.beats if beat.alternative
        ]
        assert with_alternative
        assert all(beat.scores for beat in with_alternative)

    def test_a_recommendation_corroborates_but_cannot_create_a_beat(self):
        """It was derived from the same evidence; it gets no channel."""
        flat = timeline_of([
            visual(0, 20, actions=("unknown",), importance="boring",
                   environment="unknown"),
        ])
        recommendations = RecommendationSet(recommendations=[
            EditRecommendation(
                recommendation_id="r1", asset_id=ASSET.asset_id,
                source_file=ASSET.path, start=0.0, end=20.0,
                category="punch_in", effects=["comedy"], status="accepted",
                evidence=Evidence(visual_event_ids=["e_0"]),
            ),
        ])
        built = track_module.build(flat, None)
        found = beats_module.detect(built, recommendations=recommendations)
        assert [beat.kind for beat in found] == ["unknown"]

    def test_a_flat_episode_gets_no_climax(self):
        """Inventing a peak is worse than reporting there isn't one."""
        built = track_module.build(timeline_of([
            visual(0, 20, actions=("looting",), importance="payoff"),
            visual(20, 40, actions=("looting",), importance="payoff"),
            visual(40, 60, actions=("looting",), importance="payoff"),
        ]), None)
        found = beats_module.detect(built)
        assert not [beat for beat in found if beat.kind == "climax"]

    def test_a_clear_peak_late_is_marked(self, story):
        climaxes = [beat for beat in story.beats if beat.kind == "climax"]
        assert len(climaxes) == 1
        assert climaxes[0].position >= 0.5
        assert climaxes[0].alternative  # what it was before relabelling


# ---------------------------------------------------------------------------
# Open loops, setups, payoffs, callbacks
# ---------------------------------------------------------------------------

class TestOpenLoops:
    def test_a_stated_goal_opens_a_loop(self, story):
        assert story.open_loops
        assert any("diamond" in " ".join(loop.topic)
                   for loop in story.open_loops)

    def test_a_topical_answer_closes_it(self, story):
        diamonds = next(
            loop for loop in story.open_loops
            if "diamonds" in loop.topic
        )
        assert diamonds.status == "resolved"
        assert diamonds.resolved_at is not None
        assert "diamonds" in diamonds.resolution_reason

    def test_a_payoff_about_something_else_does_not_close_it(self):
        """Position is not resolution: the two have to be about one thing."""
        unrelated = timeline_of(
            [
                visual(0, 20, importance="setup"),
                visual(20, 60, importance="boring", actions=("travelling",)),
                visual(60, 90, actions=("looting",), importance="payoff",
                       entities=("emerald",)),
            ],
            lines=[
                (2, 8, "the plan today is to find diamonds"),
                (65, 70, "look at all these emeralds from the villager"),
            ],
        )
        built = memory_module.build(unrelated)
        loop = next(
            loop for loop in built.open_loops if "diamonds" in loop.topic)
        assert loop.status != "resolved"

    def test_an_unanswered_question_stays_open_and_says_so(self):
        hanging = timeline_of(
            [
                visual(0, 30, importance="setup"),
                visual(30, 90, importance="boring", actions=("travelling",)),
            ],
            lines=[(2, 8, "can we find the ancient city today")],
        )
        built = memory_module.build(hanging)
        assert built.open_loops
        loop = built.open_loops[0]
        assert loop.status == "open"
        assert loop.resolved is False
        assert loop.needs_human_review is True

    def test_a_restated_goal_is_one_loop_not_three(self):
        repeated = timeline_of(
            [visual(0, 90, importance="setup")],
            lines=[
                (2, 8, "the plan is to find diamonds"),
                (20, 26, "we need to find diamonds today"),
                (40, 46, "we have to find diamonds remember"),
            ],
        )
        built = memory_module.build(repeated)
        about_diamonds = [
            loop for loop in built.open_loops if "diamonds" in loop.topic]
        assert len(about_diamonds) == 1

    def test_a_question_about_nothing_is_not_tracked(self):
        """"What?" raises nothing a payoff could ever answer."""
        noise = timeline_of(
            [visual(0, 30, importance="setup")], lines=[(2, 3, "what?")])
        assert memory_module.build(noise).open_loops == []

    def test_a_weak_link_is_possibly_resolved_rather_than_resolved(self, story):
        weak = [
            loop for loop in story.open_loops
            if loop.status == "possibly_resolved"
        ]
        for loop in weak:
            assert loop.needs_human_review is True
            assert "confirm" in loop.resolution_reason


class TestSetupPayoff:
    def test_a_payoff_is_linked_to_the_setup_it_spends(self, story):
        assert story.payoffs
        payoff = story.payoffs[0]
        assert payoff.setup_id
        setup = next(
            item for item in story.setups if item.item_id == payoff.setup_id)
        assert setup.paid_off is True
        assert setup.start < payoff.start

    def test_the_gap_between_them_is_recorded(self, story):
        assert all(payoff.gap_seconds > 0 for payoff in story.payoffs)

    def test_a_setup_nothing_spends_is_visible_as_unpaid(self, story):
        assert any(not setup.paid_off for setup in story.setups)

    def test_one_setup_is_not_spent_twice(self, story):
        spent = [payoff.setup_id for payoff in story.payoffs]
        assert len(spent) == len(set(spent))


class TestCallbacks:
    def test_a_spoken_reference_is_a_callback(self):
        spoken = timeline_of(
            [
                visual(0, 60, importance="setup", entities=("diamond",)),
                visual(60, 200, importance="boring", actions=("travelling",)),
                visual(200, 240, importance="payoff", entities=("diamond",)),
            ],
            lines=[
                (10, 16, "we lost all the diamonds in the lava"),
                (205, 212, "remember when we lost the diamonds in the lava"),
            ],
        )
        built = memory_module.build(spoken)
        assert any(
            item.start >= 200 for item in built.callbacks
        ), "the later mention should point back at the earlier one"

    def test_returning_somewhere_is_an_opportunity_not_a_reference(self):
        returning = timeline_of([
            visual(0, 40, environment="base", importance="setup"),
            visual(40, 200, environment="cave", actions=("mining",),
                   importance="boring"),
            visual(200, 240, environment="base", importance="setup"),
        ])
        built = memory_module.build(returning)
        places = [item for item in built.callbacks if item.kind == "place"]
        assert places
        assert "opportunity" in places[0].why
        assert places[0].confidence < MIN_EDIT_CONFIDENCE

    def test_a_reference_seconds_later_is_not_a_callback(self):
        """Two mentions in one breath are one conversation."""
        close = timeline_of(
            [visual(0, 60, environment="base", importance="setup")],
            lines=[
                (2, 8, "the diamond curse is real"),
                (12, 18, "the diamond curse strikes again"),
            ],
        )
        built = memory_module.build(close)
        assert all(item.gap_seconds >= loops_module.MIN_CALLBACK_GAP
                   for item in built.callbacks)


# ---------------------------------------------------------------------------
# Objectives, places, people, motifs
# ---------------------------------------------------------------------------

class TestMemory:
    def test_the_main_objective_is_the_one_stated_early(self, story):
        assert story.main_objective is not None
        assert story.main_objective.primary is True
        assert "diamonds" in story.main_objective.text

    def test_achieving_the_objective_is_recorded(self, story):
        assert story.main_objective.status == "achieved"
        assert story.main_objective.resolved_at is not None

    def test_an_episode_with_no_stated_goal_says_so(self):
        """Inventing one would destroy the risk that depends on its absence."""
        silent = timeline_of([
            visual(0, 60, actions=("mining",), importance="boring"),
            visual(60, 120, actions=("mining",), importance="boring"),
        ])
        built = memory_module.build(silent)
        objective = built.main_objective
        assert objective is None or objective.status == "implied"
        if objective is not None:
            assert objective.confidence < MIN_EDIT_CONFIDENCE
            assert objective.needs_human_review is True
            assert "never stated" in objective.text

    def test_a_dominant_action_gives_only_an_implied_objective(self):
        built = memory_module.build(timeline_of([
            visual(0, 100, actions=("building",), importance="setup"),
            visual(100, 200, actions=("building",), importance="setup"),
        ]))
        assert built.main_objective is not None
        assert built.main_objective.status == "implied"

    def test_places_are_measured_not_guessed(self, story):
        assert story.locations
        primary = [place for place in story.locations if place.primary]
        assert len(primary) == 1
        assert primary[0].total_seconds == max(
            place.total_seconds for place in story.locations)

    def test_a_returned_to_place_counts_two_visits(self):
        built = memory_module.build(timeline_of([
            visual(0, 40, environment="base"),
            visual(40, 90, environment="cave"),
            visual(90, 130, environment="base"),
        ]))
        base = next(
            place for place in built.locations if place.environment == "base")
        assert base.visits == 2
        assert base.total_seconds == pytest.approx(80.0)

    def test_names_are_a_guess_and_are_labelled_as_one(self):
        built = memory_module.build(timeline_of(
            [visual(0, 90, importance="setup")],
            lines=[
                (2, 8, "okay so Tarun has the totem"),
                (40, 46, "wait did Tarun bring the food"),
            ],
        ))
        assert built.roles
        role = built.roles[0]
        assert role.name == "Tarun"
        assert role.confidence < MIN_EDIT_CONFIDENCE
        assert role.needs_human_review is True

    def test_repeated_threats_become_a_motif(self, story):
        creepers = [
            motif for motif in story.motifs if motif.label == "creeper"]
        assert creepers
        assert creepers[0].occurrences >= 2
        assert creepers[0].kind == "danger"

    def test_a_thing_seen_once_is_not_a_motif(self):
        built = memory_module.build(timeline_of([
            visual(0, 40, threats=("warden",), importance="danger"),
        ]))
        assert not [
            motif for motif in built.motifs if motif.label == "warden"]

    def test_what_was_available_is_recorded(self, story):
        assert story.sources["roughcut"] is True
        assert story.sources["transcript"] is True
        assert story.sources["recommendations"] is False

    def test_a_memory_with_no_transcript_warns_rather_than_pretending(self):
        built = memory_module.build(timeline_of([
            visual(0, 60, actions=("mining",), importance="boring"),
        ]))
        assert built.sources["transcript"] is False
        assert any("no transcript" in warning for warning in built.warnings)

    def test_an_empty_timeline_produces_an_empty_memory_not_a_crash(self):
        built = memory_module.build(timeline_of([]))
        assert built.is_empty
        assert built.beats == []
        assert any("nothing to read" in warning for warning in built.warnings)


# ---------------------------------------------------------------------------
# Risks
# ---------------------------------------------------------------------------

class TestRisks:
    def test_a_long_grind_is_flagged(self):
        grind = timeline_of(
            [visual(t, t + 20, actions=("mining",), importance="boring",
                    motion=0.3)
             for t in range(0, 200, 20)],
            audio_events=[audio(0, 200, "low_energy", confidence=0.7)],
        )
        built = memory_module.build(grind)
        found = plan_module.build(built, timeline=grind)
        boring = found.risks_of("boring_repetition")
        assert boring
        assert boring[0].duration >= risks_module.BORING_SECONDS

    def test_a_short_grind_is_not(self):
        short = timeline_of([
            visual(0, 20, actions=("mining",), importance="boring"),
            visual(20, 40, actions=("mining",), importance="boring"),
        ])
        built = memory_module.build(short)
        found = plan_module.build(built, timeline=short)
        assert not found.risks_of("boring_repetition")

    def test_no_stated_objective_is_a_risk_scoped_to_the_opening(self):
        silent = timeline_of([
            visual(t, t + 30, actions=("building",), importance="setup")
            for t in range(0, 180, 30)
        ])
        built = memory_module.build(silent)
        found = plan_module.build(built, timeline=silent)
        zones = found.risks_of("no_clear_objective")
        assert zones
        assert zones[0].start == 0.0
        assert zones[0].end < built.duration, (
            "a risk spanning the whole runtime is not actionable"
        )

    def test_an_unbridged_cut_between_places_is_confusing(self):
        jump = timeline_of([
            visual(0, 30, environment="base", importance="setup"),
            visual(30, 60, environment="nether", actions=("fighting",),
                   importance="danger", threats=("piglin",)),
        ])
        plan = RoughCutPlan(
            sequence_name=SEQUENCE,
            placements=[
                ClipPlacement(
                    placement_id="p_1", asset_id=ASSET.asset_id,
                    source_file=ASSET.path, source_in=0.0, source_out=30.0,
                    sequence_start=0.0, index=0),
                ClipPlacement(
                    placement_id="p_2", asset_id=ASSET.asset_id,
                    source_file=ASSET.path, source_in=30.0, source_out=60.0,
                    sequence_start=30.0, index=1),
            ],
        )
        built = memory_module.build(jump, roughcut=plan)
        found = plan_module.build(built, timeline=jump, roughcut=plan)
        assert found.risks_of("confusing_transition")

    def test_speech_across_a_place_change_is_not_confusing(self):
        bridged = timeline_of(
            [
                visual(0, 30, environment="base", importance="setup"),
                visual(30, 60, environment="nether", importance="reveal"),
            ],
            lines=[
                (10, 20, "right lets head through the portal now"),
                (35, 45, "and here we are in the nether"),
            ],
        )
        built = memory_module.build(bridged)
        found = plan_module.build(built, timeline=bridged)
        assert not found.risks_of("confusing_transition")

    def test_measured_silence_is_flagged(self):
        quiet = timeline_of(
            [visual(0, 60, actions=("travelling",), importance="boring")],
            audio_events=[audio(5, 35, "silence", confidence=0.95)],
        )
        built = memory_module.build(quiet)
        found = plan_module.build(built, timeline=quiet)
        assert found.risks_of("dead_air")

    def test_a_weak_opening_is_flagged(self, retention):
        assert retention.risks_of("weak_hook")

    def test_a_strong_opening_is_not(self):
        strong = timeline_of(
            [
                visual(0, 20, actions=("fighting",), importance="danger",
                       threats=("warden",)),
                visual(20, 120, actions=("mining",), importance="boring"),
            ],
            audio_events=[audio(2, 4, "possible_scream", confidence=0.8)],
        )
        built = memory_module.build(strong)
        found = plan_module.build(built, timeline=strong)
        assert not found.risks_of("weak_hook")

    def test_low_visual_change_stays_quiet_when_motion_was_not_probed(self):
        """0.0 means "not measured" as often as it means "nothing moved"."""
        unprobed = timeline_of(
            [visual(t, t + 30, motion=0.0, importance="setup")
             for t in range(0, 180, 30)],
            sampling={"use_motion": False},
        )
        built = memory_module.build(unprobed)
        found = plan_module.build(built, timeline=unprobed)
        assert not found.risks_of("low_visual_change")

    def test_low_visual_change_fires_when_it_was(self):
        probed = timeline_of(
            [visual(t, t + 30, motion=0.02, importance="setup")
             for t in range(0, 180, 30)],
            sampling={"use_motion": True},
        )
        built = memory_module.build(probed)
        found = plan_module.build(built, timeline=probed)
        assert found.risks_of("low_visual_change")

    def test_a_hanging_question_is_an_unresolved_setup(self):
        hanging = timeline_of(
            [
                visual(0, 30, importance="setup", entities=("diamond",)),
                visual(30, 180, importance="boring", actions=("travelling",)),
            ],
            lines=[(2, 10, "can we actually find any diamonds down here")],
        )
        built = memory_module.build(hanging)
        found = plan_module.build(built, timeline=hanging)
        assert found.risks_of("unresolved_setup")

    def test_an_episode_that_just_stops_is_flagged(self):
        stopping = timeline_of([
            visual(t, t + 30, actions=("mining",), importance="boring")
            for t in range(0, 180, 30)
        ])
        built = memory_module.build(stopping)
        found = plan_module.build(built, timeline=stopping)
        assert found.risks_of("unclear_ending")

    def test_a_detector_that_raises_costs_only_itself(self, story,
                                                      story_track, monkeypatch):
        def explode(memory, track):
            raise RuntimeError("boom")

        monkeypatch.setattr(
            risks_module, "DETECTORS",
            (("boring_repetition", explode),) + risks_module.DETECTORS[:3],
        )
        found = risks_module.detect(story, story_track)
        assert any("boring_repetition detector failed" in zone.why
                   for zone in found)
        assert len(found) > 1, "the other detectors still ran"

    def test_severity_and_confidence_are_different_claims(self, retention):
        for zone in retention.risks:
            assert zone.severity in ("low", "medium", "high")
            assert 0.0 <= zone.confidence <= 0.88


class TestFixSafety:
    def test_a_marker_fix_is_safe_when_confident(self):
        assert risks_module.is_auto_safe("weak_hook", "add_card", 0.70)

    def test_a_marker_fix_is_not_safe_when_unconfident(self):
        assert not risks_module.is_auto_safe("weak_hook", "add_card", 0.30)

    def test_a_timing_fix_is_never_safe_on_an_inferred_risk(self):
        """Boredom is a judgement; silence is a number."""
        assert not risks_module.is_auto_safe(
            "boring_repetition", "speed_up_grind", 0.88)

    def test_a_timing_fix_can_be_safe_on_a_measured_one(self):
        assert risks_module.is_auto_safe("dead_air", "shorten_boring", 0.70)

    def test_the_timing_threshold_is_actually_reachable(self):
        """A threshold above the two-channel cap would be a dead code path."""
        assert risks_module.AUTO_TIMING_CONFIDENCE <= CONFIDENCE_CAP[2]

    def test_a_zone_never_claims_a_safe_fix_it_cannot_support(self, retention):
        for zone in retention.risks:
            if zone.fix_is_safe_automatically:
                assert zone.confidence >= MIN_EDIT_CONFIDENCE
                assert zone.affects_edit is True


# ---------------------------------------------------------------------------
# Hooks, climax, ending
# ---------------------------------------------------------------------------

class TestHooks:
    def test_several_candidates_are_offered_not_one(self, retention):
        assert len(retention.hooks) > 1

    def test_every_hook_says_what_question_it_opens(self, retention):
        assert all(hook.viewer_question for hook in retention.hooks)

    def test_a_hook_reports_where_its_question_is_answered(self, retention):
        answered = [hook for hook in retention.hooks if hook.has_payoff]
        assert answered
        assert all(hook.payoff_at > hook.start for hook in answered)

    def test_a_hook_with_no_payoff_says_so_rather_than_being_hidden(self):
        no_payoff = timeline_of([
            visual(0, 40, importance="setup"),
            visual(40, 80, actions=("fighting",), importance="danger",
                   threats=("creeper",)),
        ])
        built = memory_module.build(no_payoff)
        found = plan_module.build(built, timeline=no_payoff)
        for hook in found.hooks:
            if not hook.has_payoff:
                assert any("no payoff" in risk for risk in hook.risks)

    def test_suggested_text_is_quoted_or_labelled_as_generated(self, retention):
        for hook in retention.hooks:
            assert hook.text_source in (
                "transcript_quote", "generated_description", "none")
            if hook.text_source == "generated_description":
                assert hook.needs_human_review is True
                assert any("generated" in risk for risk in hook.risks)

    def test_a_quoted_hook_is_a_prefix_of_what_was_actually_said(
        self, retention, story_track
    ):
        for hook in retention.hooks:
            if hook.text_source != "transcript_quote":
                continue
            spoken = story_track.quotes_between(hook.start, hook.end + 20.0)
            stem = hook.suggested_text.rstrip(".").strip()
            assert any(stem in line for line in spoken)

    def test_a_hook_score_is_itemised_so_a_ranking_can_be_argued_with(
        self, retention
    ):
        assert all(hook.score_parts for hook in retention.hooks)
        for hook in retention.hooks:
            assert hook.score == pytest.approx(
                min(1.0, sum(hook.score_parts.values())), abs=0.01)

    def test_a_score_is_not_a_confidence(self, retention):
        assert any(
            hook.score != hook.confidence for hook in retention.hooks)

    def test_opening_on_the_payoff_is_flagged_as_a_spoiler(self, retention):
        spoilers = [
            hook for hook in retention.hooks
            if any("spoils" in risk for risk in hook.risks)
        ]
        for hook in spoilers:
            assert hook.hook_type in ("reveal", "danger")

    def test_a_goal_statement_is_not_quoted_as_the_viewers_question(
        self, retention
    ):
        """The goal is a statement; the hook's job is to turn it into a question."""
        for hook in retention.hooks:
            if hook.viewer_question.startswith("okay so the plan"):
                pytest.fail(
                    "a stated goal was reused verbatim as a viewer question")


class TestClimaxAndEnding:
    def test_the_plan_reports_the_memorys_climax_rather_than_its_own(
        self, story, retention
    ):
        """They disagreed once, on real footage. One question, one answer."""
        marked = [beat for beat in story.beats if beat.kind == "climax"]
        assert marked and retention.climax is not None
        assert retention.climax.start == marked[0].start
        assert marked[0].item_id in retention.climax.beat_ids

    def test_a_flat_episode_gets_no_climax_and_shows_the_field(self):
        flat = timeline_of([
            visual(t, t + 20, actions=("looting",), importance="payoff")
            for t in range(0, 120, 20)
        ])
        built = memory_module.build(flat)
        found = plan_module.build(built, timeline=flat)
        assert found.climax is None
        assert found.climax_alternatives
        assert any("no single moment" in w for w in found.warnings)

    def test_the_margin_over_the_runner_up_travels_with_the_climax(
        self, retention
    ):
        assert retention.climax.margin > 0

    def test_an_ending_that_closes_the_objective_wins(self):
        closing = timeline_of(
            [
                visual(0, 30, importance="setup"),
                visual(30, 120, actions=("mining",), importance="boring"),
                visual(120, 180, actions=("looting",), importance="payoff",
                       entities=("diamond",)),
            ],
            lines=[
                (2, 10, "the plan today is to find diamonds"),
                (125, 133, "we did it we finally have the diamonds"),
            ],
        )
        built = memory_module.build(closing)
        found = plan_module.build(built, timeline=closing)
        assert found.ending is not None

    def test_the_midpoint_marker_never_lands_on_top_of_the_action(self, story,
                                                                  retention):
        if retention.midpoint_reset is None:
            pytest.skip("no midpoint reset for this episode")
        covering = story.beat_at(retention.midpoint_reset.start)
        assert covering is None or covering.start == pytest.approx(
            retention.midpoint_reset.start
        ) or covering.is_quiet


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

class TestSuggestions:
    def test_every_suggestion_is_routed_somewhere(self, retention):
        for item in retention.suggestions:
            assert item.downstream in ("roughcut", "style", "assets", "human")
            assert item.type in SUGGESTION_TYPES

    def test_every_suggestion_carries_a_marker_fallback(self, retention):
        """Refusing to act is only useful if something still lands."""
        assert all(item.marker_fallback for item in retention.suggestions)

    def test_every_suggestion_carries_a_reason_and_evidence(self, retention):
        for item in retention.suggestions:
            assert item.reason
            assert item.evidence.segment_ids or item.evidence.quotes

    def test_no_suggestion_carries_a_premiere_operation(self, retention):
        """This layer plans. It has no idea what an operation looks like."""
        for item in retention.suggestions:
            assert not hasattr(item, "premiere_ops")

    def test_nothing_below_the_threshold_is_applied_automatically(
        self, retention
    ):
        for item in retention.suggestions:
            if item.confidence < MIN_EDIT_CONFIDENCE:
                assert item.auto_safe is False
                assert item.is_marker_only is True
                assert item.needs_human_review is True

    def test_a_timing_suggestion_is_never_automatic_from_inference(
        self, retention
    ):
        for item in retention.suggestions:
            if item.type in TIMING_SUGGESTIONS and item.auto_safe:
                assert item.risk_ids, (
                    "a timing change with no measured risk behind it"
                )

    def test_the_same_stretch_is_not_shortened_twice(self):
        """A grind that is also dead air must produce one shortening."""
        overlapping = timeline_of(
            [visual(t, t + 20, actions=("mining",), importance="boring")
             for t in range(0, 200, 20)],
            audio_events=[audio(0, 200, "silence", confidence=0.9)],
        )
        built = memory_module.build(overlapping)
        found = plan_module.build(built, timeline=overlapping)
        for kind in ("shorten_boring", "speed_up_grind"):
            starts = [
                item.start for item in found.suggestions if item.type == kind]
            for left, right in zip(starts, starts[1:]):
                assert right - left > suggest_module.DEDUPE_WINDOW

    def test_a_resolved_loops_setup_is_protected(self, retention):
        keeps = [
            item for item in retention.suggestions
            if item.type == "keep_setup"
        ]
        assert keeps
        assert all(item.downstream == "roughcut" for item in keeps)

    def test_the_climax_produces_a_marker(self, retention):
        assert any(
            item.type == "mark_climax" for item in retention.suggestions)

    def test_suggestions_can_be_filtered_by_the_pass_that_would_act(
        self, retention
    ):
        for stage in ("roughcut", "style", "assets", "human"):
            for item in retention.suggestions_for(stage):
                assert item.downstream == stage

    def test_a_far_apart_callback_gets_a_caption(self):
        distant = timeline_of(
            [
                visual(0, 60, environment="base", importance="setup"),
                visual(60, 240, environment="cave", actions=("mining",),
                       importance="boring"),
                visual(240, 300, environment="base", importance="setup"),
            ],
        )
        built = memory_module.build(distant)
        found = plan_module.build(built, timeline=distant)
        captions = [
            item for item in found.suggestions
            if item.type == "add_callback_caption"
        ]
        for item in captions:
            assert item.downstream == "style"


class TestNoFakeAnalytics:
    def test_no_generated_string_claims_to_know_what_viewers_will_do(
        self, retention, story
    ):
        """Asserted over every field, not spot-checked."""
        items = (
            list(retention.risks) + list(retention.hooks)
            + list(retention.suggestions) + list(story.beats)
            + list(story.open_loops) + list(story.callbacks)
            + list(story.setups) + list(story.payoffs)
            + list(story.motifs) + list(story.locations)
            + list(story.objectives)
        )
        offenders = []
        for item in items:
            for name in ("why", "reason", "marker_fallback", "suggested_text",
                         "viewer_question", "why_viewer_cares", "notes",
                         "resolution_reason", "match_reason", "text"):
                hit = contains_claim(getattr(item, name, ""))
                if hit:
                    offenders.append((item.item_id, name, hit))
        assert offenders == []

    def test_the_warnings_do_not_claim_either(self, retention, story):
        for warning in list(retention.warnings) + list(story.warnings):
            assert contains_claim(warning) is None

    def test_the_banner_denies_analytics_rather_than_promising_them(self):
        assert "not retention analytics" in NOT_ANALYTICS.lower()
        assert "guarantee" not in NOT_ANALYTICS.lower()

    def test_both_artifacts_carry_the_banner(self, retention, story):
        assert story.to_dict()["basis"] == NOT_ANALYTICS
        assert retention.to_dict()["basis"] == NOT_ANALYTICS

    def test_both_reports_print_it(self, retention, story):
        def flat(text: str) -> str:
            return " ".join(text.split())

        banner = flat(NOT_ANALYTICS)
        assert banner in flat(episode_report.render_memory(story))
        assert banner in flat(episode_report.render_plan(retention))
        assert banner in flat(episode_report.render_risks(retention))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class TestReports:
    def test_the_memory_report_leads_with_what_it_could_not_see(self, story):
        text = episode_report.render_memory(story)
        limits = text.index("WHAT THIS COULD AND COULD NOT SEE")
        assert limits < text.index("BEATS")

    def test_the_plan_report_says_which_suggestions_are_markers(self, retention):
        text = episode_report.render_plan(retention)
        assert "marker only" in text or "marker" in text
        assert "safe to apply" in text

    def test_a_synthetic_timebase_is_called_out(self, story_timeline):
        built = memory_module.build(story_timeline)
        text = episode_report.render_memory(built)
        assert "timeline" in text
        assert "not sequence time" in text

    def test_the_reports_survive_an_empty_episode(self):
        empty = memory_module.build(timeline_of([]))
        found = plan_module.build(empty, timeline=timeline_of([]))
        assert episode_report.render_memory(empty)
        assert episode_report.render_plan(found)
        assert episode_report.render_hooks(found)
        assert episode_report.render_open_loops(empty)
        assert episode_report.render_callbacks(empty)
        assert episode_report.render_beats(empty)


# ---------------------------------------------------------------------------
# Pipeline and CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def wired(config, sampling, story_timeline, cut):
    """A pipeline with a timeline and a rough cut already on disk."""
    from editing.pipeline import build_pipeline

    pipeline = build_pipeline(
        config, sampling, say=lambda _message: None, use_cache=False)
    pipeline.write_timeline(story_timeline)
    pipeline.write_rough_cut(cut)
    return pipeline


class TestPipeline:
    def test_memory_round_trips_through_disk(self, wired):
        built = wired.episode_memory()
        again = wired.load_episode_memory()
        assert again.episode_id == built.episode_id
        assert len(again.beats) == len(built.beats)

    def test_the_plan_round_trips_too(self, wired):
        wired.episode_memory()
        built = wired.retention_plan()
        again = wired.load_retention_plan()
        assert len(again.suggestions) == len(built.suggestions)

    def test_both_reports_are_written(self, wired):
        wired.episode_memory()
        wired.retention_plan()
        directory = wired.config.episode_dir
        assert (directory / "structure.memory.txt").exists()
        assert (directory / "structure.retention.txt").exists()

    def test_loading_before_building_explains_what_to_run(self, wired):
        with pytest.raises(EditingError) as caught:
            wired.load_retention_plan()
        assert "plan-retention" in (caught.value.hint or "")

    def test_ignoring_the_rough_cut_changes_the_timebase(self, wired):
        assert wired.episode_memory().timebase == "roughcut"
        assert wired.episode_memory(use_roughcut=False).timebase == "timeline"

    def test_the_downstream_seam_filters_without_operations(self, wired):
        wired.episode_memory()
        wired.retention_plan()
        for stage in ("roughcut", "style", "assets"):
            wanted = wired.retention_suggestions_for(stage)
            assert all(item.downstream == stage for item in wanted)
        safe = wired.retention_suggestions_for("style", safe_only=True)
        assert all(item.auto_safe for item in safe)

    def test_no_save_leaves_the_disk_alone(self, wired):
        wired.episode_memory(save=False)
        assert not (
            wired.config.episode_dir / "structure.memory.json").exists()


class TestCLI:
    """Every command runs with no model, no FFmpeg, no Premiere and no assets."""

    def _run(self, argv, config, capsys):
        from editing.cli import main

        code = main(argv + ["--output-dir", str(config.output_dir),
                            "--no-premiere", "--quiet"])
        return code, capsys.readouterr().out

    def test_build_memory_then_plan_retention(self, wired, config, capsys):
        code, out = self._run(
            ["episode", "build-memory"], config, capsys)
        assert code == 0
        assert "EPISODE MEMORY" in out

        code, out = self._run(
            ["episode", "plan-retention"], config, capsys)
        assert code == 0
        assert "RETENTION PLAN" in out

    def test_every_show_command_runs(self, wired, config, capsys):
        self._run(["episode", "build-memory"], config, capsys)
        self._run(["episode", "plan-retention"], config, capsys)
        for command in ("show-beats", "show-risks", "show-hooks",
                        "show-open-loops", "show-callbacks", "report"):
            code, out = self._run(["episode", command], config, capsys)
            assert code == 0, command
            assert out.strip(), command

    def test_json_mode_prints_one_object(self, wired, config, capsys):
        self._run(["episode", "build-memory"], config, capsys)
        code, out = self._run(
            ["episode", "show-beats", "--json"], config, capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["success"] is True
        assert "beats" in payload

    def test_the_risks_json_carries_the_banner(self, wired, config, capsys):
        self._run(["episode", "build-memory"], config, capsys)
        self._run(["episode", "plan-retention"], config, capsys)
        code, out = self._run(
            ["episode", "show-risks", "--json"], config, capsys)
        assert json.loads(out)["basis"] == NOT_ANALYTICS

    def test_export_writes_one_stages_suggestions(
        self, wired, config, capsys, tmp_path
    ):
        self._run(["episode", "build-memory"], config, capsys)
        self._run(["episode", "plan-retention"], config, capsys)
        target = tmp_path / "for_style.json"
        code, _ = self._run(
            ["episode", "export", str(target), "--suggestions-for", "style"],
            config, capsys)
        assert code == 0
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["downstream"] == "style"
        assert payload["basis"] == NOT_ANALYTICS
        assert all(
            item["downstream"] == "style" for item in payload["suggestions"])

    def test_a_missing_plan_fails_with_a_hint_not_a_traceback(
        self, wired, config, capsys
    ):
        from editing.cli import main

        code = main(["episode", "show-risks", "--json",
                     "--output-dir", str(config.output_dir), "--quiet"])
        assert code != 0
        payload = json.loads(capsys.readouterr().out)
        assert "plan-retention" in payload.get("hint", "")


class TestAutoIntegration:
    def test_the_two_stages_are_in_the_pipeline_and_non_critical(self):
        from editing.auto import stages as stages_module
        from editing.auto.schema import STAGE_ORDER

        for name in ("episode_memory", "retention_plan"):
            assert name in STAGE_ORDER
            assert stages_module.stage(name).critical is False
            assert name in stages_module.RUNNERS

    def test_neither_stage_can_be_executed(self):
        """Nothing here touches Premiere, so neither may become a gate."""
        from editing.auto.schema import GATE_STAGES

        assert "episode_memory" not in GATE_STAGES.values()
        assert "retention_plan" not in GATE_STAGES.values()

    def test_the_retention_stage_depends_on_the_memory(self):
        from editing.auto import stages as stages_module

        assert "episode_memory" in stages_module.stage(
            "retention_plan").requires
        assert "retention_plan" in stages_module.dependents("episode_memory")

    def test_skipping_them_is_a_skip_not_a_failure(self):
        from editing.auto import stages as stages_module
        from editing.auto.runner import AutoRunner
        from editing.auto.schema import AutoRunConfig

        run = AutoRunConfig(skip_episode=True)
        for name in stages_module.EPISODE_STAGES:
            assert AutoRunner._skip_reason(run, name) == "--skip-episode was set"
        assert AutoRunner._skip_reason(AutoRunConfig(), "episode_memory") == ""


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------

class TestWeakEvidence:
    def test_a_timeline_with_only_visuals_produces_low_confidence(self):
        visuals_only = timeline_of([
            visual(0, 30, actions=("mining",), importance="boring"),
            visual(30, 60, actions=("mining",), importance="boring"),
        ])
        built = memory_module.build(visuals_only)
        assert all(len(beat.channels) <= 1 for beat in built.beats)
        assert all(
            beat.confidence <= CONFIDENCE_CAP[1] for beat in built.beats)

    def test_a_one_second_episode_does_not_divide_by_zero(self):
        tiny = timeline_of([visual(0, 1, importance="setup")])
        built = memory_module.build(tiny)
        found = plan_module.build(built, timeline=tiny)
        assert found.stats()["risks"] >= 0

    def test_an_episode_of_pure_silence_says_what_it_could_not_see(self):
        silent = timeline_of(
            [visual(t, t + 30, importance="boring", actions=("idle",))
             for t in range(0, 120, 30)],
        )
        built = memory_module.build(silent)
        found = plan_module.build(built, timeline=silent)
        assert any("no transcript" in w for w in found.warnings)

    def test_building_a_plan_with_neither_track_nor_timeline_refuses(self,
                                                                     story):
        with pytest.raises(ValueError):
            plan_module.build(story)

    def test_an_empty_plan_is_still_a_valid_plan(self):
        empty = memory_module.build(timeline_of([]))
        found = plan_module.build(empty, timeline=timeline_of([]))
        assert found.suggestions == []
        assert any("empty" in w for w in found.warnings)
        assert EpisodeRetentionPlan.from_dict(found.to_dict()) is not None

    def test_ids_are_stable_across_two_identical_builds(self, story_timeline,
                                                        cut):
        first = memory_module.build(story_timeline, roughcut=cut)
        second = memory_module.build(story_timeline, roughcut=cut)
        assert [beat.item_id for beat in first.beats] == [
            beat.item_id for beat in second.beats]
        assert first.episode_id == second.episode_id

    def test_new_id_is_a_function_of_its_inputs(self):
        assert new_id("beat", "a", 1) == new_id("beat", "a", 1)
        assert new_id("beat", "a", 1) != new_id("beat", "a", 2)

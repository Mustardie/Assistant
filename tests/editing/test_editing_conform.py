"""The conform pass: decisions becoming operations, and a finished file.

What these tests are for is narrower than "does the code run". Every pass
before this one could pass its tests while producing nothing a viewer would
ever see, because its output was a plan and a plan is easy to assert on. So
the assertions here are deliberately about *conversion*: an accepted caption
has to produce a ``text.create``, a matched sound has to produce a
``clip.overwrite`` on the effects track, and a decision that cannot convert has
to appear in ``unconverted`` with a reason rather than vanishing.

Nothing here touches Premiere. The execution path is exercised through the
same injected engine the other passes use, which verifies the half that is
ours: what gets sent, in what order, and what happens when the host says no.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from editing.conform import build as conform_build
from editing.conform import color as conform_color
from editing.conform import deliver as conform_deliver
from editing.conform import execute as conform_execute
from editing.conform import mix as conform_mix
from editing.conform import music as conform_music
from editing.conform import transitions as conform_transitions
from editing.conform.schema import (
    COLOR_LOOKS, ConformConfig, ConformPlan, DeliveryResult, LevelMeasurement,
)
from editing.polish.schema import AudioCue, AudioPolishPlan, CaptionDecision, CaptionPlan
from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.tracks import DEFAULT_LAYOUT, TrackLayout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _placement(index: int, start: float, end: float, *, asset: str = "a1",
               source_in: float = 0.0, path: str = "") -> ClipPlacement:
    return ClipPlacement(
        placement_id=f"p{index}",
        asset_id=asset,
        source_file=path,
        source_in=source_in,
        source_out=source_in + (end - start),
        sequence_start=start,
        index=index,
    )


@pytest.fixture
def cut() -> RoughCutPlan:
    plan = RoughCutPlan(sequence_name="Nova Rough Cut")
    plan.placements = [
        # 0 -> 1 is a continuous shot cut to itself: an ordinary cut.
        _placement(0, 0.0, 12.0, asset="a1", source_in=0.0),
        _placement(1, 12.0, 24.0, asset="a1", source_in=12.0),
        # 1 -> 2 crosses into a different recording: a scene change.
        _placement(2, 24.0, 40.0, asset="a2"),
    ]
    return plan


@pytest.fixture
def captions() -> CaptionPlan:
    plan = CaptionPlan(sequence_name="Nova Rough Cut")
    plan.decisions = [
        CaptionDecision(caption_id="c1", accepted=True, moment="reaction",
                        text="that should not have worked", start=3.0, end=5.0,
                        zone="lower_third", reason="the line is the moment"),
        CaptionDecision(caption_id="c2", accepted=True, moment="explanation",
                        text="two blocks of redstone", start=15.0, end=17.5,
                        zone="lower_third", reason="names the mechanism"),
        # Refused: the cut removed the line it came from.
        CaptionDecision(caption_id="c3", accepted=True, moment="reaction",
                        text="cut from the edit", start=-1.0, end=-1.0),
        CaptionDecision(caption_id="c4", accepted=False, text="not chosen"),
    ]
    return plan


@pytest.fixture
def sounds(tmp_path) -> AudioPolishPlan:
    real = tmp_path / "whoosh.wav"
    real.write_bytes(b"RIFF....WAVEfmt ")
    plan = AudioPolishPlan(sequence_name="Nova Rough Cut")
    plan.cues = [
        AudioCue(cue_id="s1", kind="whoosh", accepted=True, start=12.0,
                 end=12.6, asset_path=str(real), asset_filename="whoosh.wav",
                 target="the cut into the build"),
        # Accepted but nothing matched: must stay a marker, never silence.
        AudioCue(cue_id="s2", kind="impact", accepted=True, start=25.0,
                 end=25.4, placeholder="impact_sfx",
                 target="the block lands"),
        AudioCue(cue_id="s3", kind="hit", accepted=False, start=30.0),
    ]
    return plan


def _built(cut, **kwargs) -> ConformPlan:
    config = kwargs.pop("config", None) or ConformConfig(
        music=False, color=False, sound=False, transitions=False,
    )
    return conform_build.build(rough_cut=cut, config=config, **kwargs)


# ---------------------------------------------------------------------------
# Track layout
# ---------------------------------------------------------------------------

class TestTrackLayout:
    def test_the_cut_tracks_are_protected(self):
        assert DEFAULT_LAYOUT.is_protected("V1")
        assert DEFAULT_LAYOUT.is_protected("a1")     # case-insensitive
        assert not DEFAULT_LAYOUT.is_protected("V2")

    def test_a_short_sequence_gets_the_tracks_it_needs(self):
        ops = DEFAULT_LAYOUT.ensure_ops(existing_video=1, existing_audio=1)
        assert len(ops) == 1
        assert ops[0]["op"] == "track.add"
        assert ops[0]["video"] == 3      # V2, V3, V4
        assert ops[0]["audio"] == 2      # A2, A3

    def test_a_tall_enough_sequence_gets_nothing(self):
        assert DEFAULT_LAYOUT.ensure_ops(existing_video=4, existing_audio=3) == []

    def test_a_layout_can_be_moved(self):
        moved = DEFAULT_LAYOUT.with_overrides(captions="V5")
        assert moved.captions == "V5"
        assert moved.video_tracks_needed == 5
        assert DEFAULT_LAYOUT.captions == "V2"   # frozen: the original stands

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(ValueError):
            DEFAULT_LAYOUT.with_overrides(subtitles="V9")


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

class TestCaptions:
    def test_an_accepted_caption_becomes_a_text_clip(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        texts = [op for op in plan.ops if op["op"] == "text.create"]
        assert len(texts) == 2
        assert texts[0]["text"] == "that should not have worked"
        assert texts[0]["track"] == DEFAULT_LAYOUT.captions
        assert texts[0]["time"] == pytest.approx(3.0)

    def test_captions_carry_their_styling(self, cut, captions):
        config = ConformConfig(
            music=False, color=False, sound=False, transitions=False,
            caption_font="Impact", caption_size=72, caption_color="#FFEE00",
        )
        plan = _built(cut, caption_plan=captions, config=config)
        first = next(op for op in plan.ops if op["op"] == "text.create")
        assert first["font"] == "Impact"
        assert first["size"] == 72
        assert first["color"] == "#FFEE00"
        assert first["stroke_width"] > 0

    def test_a_caption_fades_rather_than_popping(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        fades = [op for op in plan.ops
                 if op["op"] == "animate" and op.get("property") == "Opacity"]
        assert len(fades) == 4          # up and down, per caption
        assert fades[0]["from"] == 0.0 and fades[0]["to"] == 100.0
        assert fades[1]["from"] == 100.0 and fades[1]["to"] == 0.0

    def test_a_caption_the_cut_removed_is_reported_not_dropped(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        reasons = {entry["reason"] for entry in plan.unconverted}
        assert "not_on_the_cut" in reasons

    def test_a_caption_with_no_room_at_the_end_is_refused(self, cut, captions):
        """A real run put a caption on screen for the final two frames.

        Unreadable, and it landed past the last frame Premiere actually had
        once clip lengths were rounded to the sequence frame rate.
        """
        captions.decisions = [
            CaptionDecision(caption_id="late", accepted=True, moment="reaction",
                            text="right at the end", start=39.95, end=40.0,
                            zone="lower_third"),
        ]
        plan = _built(cut, caption_plan=captions)
        assert not [op for op in plan.ops if op["op"] == "text.create"]
        assert any(entry["reason"] == "no_room" for entry in plan.unconverted)

    def test_captions_never_land_on_the_programme_track(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        for op in plan.ops:
            if op["op"] == "text.create":
                assert not plan.layout.is_protected(op["track"])


# ---------------------------------------------------------------------------
# Sound
# ---------------------------------------------------------------------------

class TestSound:
    def test_a_matched_sound_is_imported_and_placed(self, cut, sounds):
        config = ConformConfig(music=False, color=False, transitions=False,
                               captions=False)
        plan = _built(cut, audio_plan=sounds, config=config)
        assert any(op["op"] == "project.import" for op in plan.ops)
        placed = [op for op in plan.ops if op["op"] == "clip.overwrite"]
        assert len(placed) == 1
        assert placed[0]["track"] == DEFAULT_LAYOUT.sfx
        assert placed[0]["time"] == pytest.approx(12.0)

    def test_a_cue_with_no_file_stays_a_marker(self, cut, sounds):
        config = ConformConfig(music=False, color=False, transitions=False,
                               captions=False)
        plan = _built(cut, audio_plan=sounds, config=config)
        markers = [op for op in plan.ops if op["op"] == "marker.add"]
        assert len(markers) == 1
        assert markers[0]["time"] == pytest.approx(25.0)
        assert any(entry["reason"] == "no_asset" for entry in plan.unconverted)

    def test_a_sound_past_the_end_of_the_cut_is_refused(self, cut, sounds, tmp_path):
        real = tmp_path / "late.wav"
        real.write_bytes(b"RIFF")
        sounds.cues.append(AudioCue(cue_id="s9", kind="hit", accepted=True,
                                    start=999.0, end=999.5,
                                    asset_path=str(real)))
        config = ConformConfig(music=False, color=False, transitions=False,
                               captions=False)
        plan = conform_build.build(rough_cut=cut, config=config,
                                   audio_plan=sounds)
        plan.cut_duration = 40.0
        assert any(entry["reason"] in ("past_the_end", "no_asset")
                   for entry in plan.unconverted)


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

class TestColour:
    def test_dark_footage_is_not_given_more_contrast(self):
        decision = conform_color.decide(
            style_intent="gameplay",
            measurements=[{"luma": 40.0, "chroma": 30.0}],
        )
        assert decision.look == "flat"
        assert decision.reason == "dark_footage"

    def test_already_vivid_footage_is_left_alone(self):
        decision = conform_color.decide(
            style_intent="gameplay",
            measurements=[{"luma": 120.0, "chroma": 90.0}],
        )
        assert decision.look == "neutral"
        assert not decision.applied

    def test_the_style_decides_when_the_footage_is_unremarkable(self):
        decision = conform_color.decide(
            style_intent="fast gameplay highlight",
            measurements=[{"luma": 120.0, "chroma": 30.0}],
        )
        assert decision.look == "punchy"
        assert decision.applied

    def test_no_evidence_means_no_grade(self):
        decision = conform_color.decide()
        assert decision.look == "neutral"
        assert decision.reason == "no_evidence"
        assert not decision.applied

    def test_an_explicit_request_wins(self):
        decision = conform_color.decide(
            style_intent="gameplay", requested="cool",
            measurements=[{"luma": 40.0, "chroma": 30.0}],
        )
        assert decision.look == "cool"
        assert decision.reason == "requested"

    def test_strength_scales_around_the_right_neutral(self):
        full = COLOR_LOOKS["punchy"]["params"]
        half = conform_color.scale_params(full, 0.5)
        # Saturation is 100 = unchanged, so half strength moves halfway to 100.
        assert half["saturation"] == pytest.approx(106.0)
        # Contrast is 0 = unchanged, so half strength halves it.
        assert half["contrast"] == pytest.approx(7.0)

    def test_the_grade_is_one_operation_over_the_whole_track(self):
        decision = conform_color.decide(requested="warm")
        ops = conform_color.grade_ops(decision, DEFAULT_LAYOUT, clip_count=9)
        assert len(ops) == 1
        assert ops[0]["clip"] == {"track": "V1", "all": True}

    def test_signalstats_parsing(self):
        text = (
            "lavfi.signalstats.YAVG=100.5\nlavfi.signalstats.UAVG=138.0\n"
            "lavfi.signalstats.VAVG=118.0\n"
            "lavfi.signalstats.YAVG=120.5\nlavfi.signalstats.UAVG=128.0\n"
            "lavfi.signalstats.VAVG=128.0\n"
        )
        parsed = conform_color.parse_signalstats(text)
        assert parsed["frames"] == 2
        assert parsed["luma"] == pytest.approx(110.5)
        assert parsed["chroma"] > 0


# ---------------------------------------------------------------------------
# Mix
# ---------------------------------------------------------------------------

class TestMix:
    def test_ebur128_parsing(self):
        text = (
            "[Parsed_ebur128_0 @ 0] Summary:\n\n"
            "  Integrated loudness:\n    I:         -23.7 LUFS\n"
            "    Threshold: -34.1 LUFS\n"
            "  Loudness range:\n    LRA:         8.2 LU\n"
            "  True peak:\n    Peak:       -1.4 dBFS\n"
        )
        parsed = conform_mix.parse_ebur128_summary(text)
        assert parsed["lufs"] == pytest.approx(-23.7)
        assert parsed["lra"] == pytest.approx(8.2)
        assert parsed["peak_db"] == pytest.approx(-1.4)

    def test_a_quiet_source_is_brought_up_to_the_target(self):
        measurement = LevelMeasurement(role="dialogue", lufs=-24.0,
                                       peak_db=-12.0, measured=True)
        gain, clipped = conform_mix.gain_to_reach(measurement, -14.0)
        assert gain == pytest.approx(10.0)
        assert not clipped

    def test_the_peak_ceiling_beats_the_loudness_target(self):
        # Wants +10 dB, but the peaks are already at -2 dBTP.
        measurement = LevelMeasurement(role="dialogue", lufs=-24.0,
                                       peak_db=-2.0, measured=True)
        gain, clipped = conform_mix.gain_to_reach(measurement, -14.0,
                                                  peak_ceiling_db=-1.0)
        assert gain == pytest.approx(1.0)
        assert clipped

    def test_an_unmeasured_source_is_not_moved(self):
        gain, clipped = conform_mix.gain_to_reach(
            LevelMeasurement(role="music", measured=False), -14.0
        )
        assert gain == 0.0 and not clipped

    def test_music_is_set_relative_to_measured_dialogue(self):
        def fake_measure(path, *, role="dialogue", **kwargs):
            table = {"dialogue": (-20.0, -12.0), "music": (-10.0, -3.0)}
            lufs, peak = table[role]
            return LevelMeasurement(role=role, path=str(path), lufs=lufs,
                                    peak_db=peak, measured=True)

        decision = conform_mix.build_mix(
            dialogue_sources=["voice.mp4"], music_path="bed.wav",
            target_lufs=-14.0, music_under_dialogue_db=-18.0,
            measure_fn=fake_measure,
        )
        assert decision.gains["dialogue"] == pytest.approx(6.0)
        # Music must land at -32 LUFS: measured -10, so -22 dB of gain.
        assert decision.gains["music"] == pytest.approx(-22.0)
        assert decision.fully_measured

    def test_an_unmeasurable_source_is_reported_not_guessed(self):
        def fake_measure(path, *, role="dialogue", **kwargs):
            return LevelMeasurement(role=role, measured=False,
                                    error="no audio stream")

        decision = conform_mix.build_mix(
            dialogue_sources=["silent.mp4"], measure_fn=fake_measure,
        )
        assert not decision.fully_measured
        assert decision.gains["dialogue"] == 0.0
        assert any("could not be measured" in w for w in decision.warnings)

    def test_the_mix_emits_a_gain_and_a_tail_fade(self):
        decision = conform_mix.MixDecision(gains={"dialogue": -3.0})
        ops = conform_mix.mix_ops(decision, DEFAULT_LAYOUT, cut_duration=40.0)
        kinds = [op["op"] for op in ops]
        assert "audio.gain" in kinds
        assert "audio.fade" in kinds

    def test_a_zero_gain_emits_nothing(self):
        decision = conform_mix.MixDecision(gains={"dialogue": 0.0},
                                           programme_fade_out=0.0)
        assert conform_mix.mix_ops(decision, DEFAULT_LAYOUT,
                                   cut_duration=40.0) == []

    def test_the_tail_fade_targets_the_end_by_time_not_by_index(self):
        decision = conform_mix.MixDecision(programme_fade_out=0.5)
        ops = conform_mix.mix_ops(decision, DEFAULT_LAYOUT, cut_duration=40.0,
                                  tail_at=36.0)
        fade = next(op for op in ops if op["op"] == "audio.fade")
        # A negative index is refused by the validator, and there is no "last"
        # selector; a time inside the final clip is what actually works.
        assert "index" not in fade["clip"]
        assert fade["clip"]["at"] == pytest.approx(36.0)

    def test_the_tail_fade_stays_clear_of_the_end_of_the_cut(self):
        """Premiere rounds clip lengths to the frame rate, so the plan's idea
        of the cut length lands a frame or two past the real last frame."""
        decision = conform_mix.MixDecision(programme_fade_out=0.5)
        ops = conform_mix.mix_ops(decision, DEFAULT_LAYOUT, cut_duration=20.2)
        fade = next(op for op in ops if op["op"] == "audio.fade")
        assert fade["clip"]["at"] < 20.0


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

class TestMusic:
    def test_no_music_available_is_a_refusal_with_a_reason(self):
        decision = conform_music.plan_bed(
            candidates=[], cut_duration=120.0,
        )
        assert not decision.placed
        assert decision.reject_reason == "no_music_available"

    def test_a_short_cut_gets_no_bed(self, tmp_path):
        track = tmp_path / "bed.wav"
        track.write_bytes(b"RIFF")
        decision = conform_music.plan_bed(
            candidates=[track], cut_duration=3.0,
        )
        assert not decision.placed
        assert decision.reject_reason == "cut_too_short"

    def test_the_shortest_covering_track_is_preferred(self, tmp_path, monkeypatch):
        short, exact, long = (tmp_path / n for n in ("a.wav", "b.wav", "c.wav"))
        for path in (short, exact, long):
            path.write_bytes(b"RIFF")
        durations = {short: 20.0, exact: 65.0, long: 300.0}
        monkeypatch.setattr(conform_music, "_duration",
                            lambda p, ffprobe="ffprobe": durations[Path(p)])
        chosen, duration = conform_music.choose(
            [short, exact, long], wanted_seconds=60.0
        )
        assert chosen == exact and duration == 65.0

    def test_a_track_that_would_loop_forever_is_refused(self, tmp_path, monkeypatch):
        track = tmp_path / "sting.wav"
        track.write_bytes(b"RIFF")
        monkeypatch.setattr(conform_music, "_duration",
                            lambda p, ffprobe="ffprobe": 10.0)
        decision = conform_music.plan_bed(
            candidates=[track], cut_duration=600.0, beat_align=False,
        )
        assert not decision.placed
        assert decision.reject_reason == "would_loop_too_often"

    def test_a_placed_bed_is_imported_levelled_and_faded(self, tmp_path, monkeypatch):
        track = tmp_path / "bed.wav"
        track.write_bytes(b"RIFF")
        monkeypatch.setattr(conform_music, "_duration",
                            lambda p, ffprobe="ffprobe": 120.0)
        decision = conform_music.plan_bed(
            candidates=[track], cut_duration=60.0, beat_align=False,
            gain_db=-18.0,
        )
        assert decision.placed
        ops = conform_music.bed_ops(decision)
        kinds = [op["op"] for op in ops]
        assert kinds[0] == "project.import"
        assert "clip.overwrite" in kinds
        assert "audio.gain" in kinds
        assert kinds.count("audio.fade") == 2

    def test_a_duck_names_its_range_fields(self, tmp_path, monkeypatch):
        """The catalog takes objects here, not pairs, and a list is refused."""
        track = tmp_path / "bed.wav"
        track.write_bytes(b"RIFF")
        monkeypatch.setattr(conform_music, "_duration",
                            lambda p, ffprobe="ffprobe": 120.0)
        decision = conform_music.plan_bed(
            candidates=[track], cut_duration=60.0, beat_align=False,
            speech_ranges=[(2.0, 5.0), (9.0, 12.0)],
        )
        duck = next(op for op in conform_music.bed_ops(decision)
                    if op["op"] == "audio.duck")
        assert duck["under"][0] == {"start": 2.0, "end": 5.0}

    def test_a_bed_never_lands_on_the_dialogue_track(self, tmp_path, monkeypatch):
        track = tmp_path / "bed.wav"
        track.write_bytes(b"RIFF")
        monkeypatch.setattr(conform_music, "_duration",
                            lambda p, ffprobe="ffprobe": 120.0)
        decision = conform_music.plan_bed(
            candidates=[track], cut_duration=60.0, beat_align=False,
        )
        for op in conform_music.bed_ops(decision):
            track_name = op.get("track") or (op.get("clip") or {}).get("track")
            if track_name:
                assert not DEFAULT_LAYOUT.is_protected(track_name)


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_an_ordinary_cut_gets_nothing(self, cut):
        decisions = conform_transitions.decide(cut.placements, fade_ends=False)
        ordinary = [d for d in decisions if d.reject_reason == "ordinary_cut"]
        assert ordinary, "a continuous shot cut to itself must be refused"

    def test_a_source_change_earns_a_dissolve(self, cut):
        decisions = conform_transitions.decide(cut.placements, fade_ends=False)
        applied = [d for d in decisions if d.applied]
        assert len(applied) == 1
        assert applied[0].clip_index == 1     # the a1 -> a2 boundary
        assert applied[0].transition == "Cross Dissolve"

    def test_a_time_jump_earns_a_dissolve(self):
        clips = [
            _placement(0, 0.0, 10.0, asset="a1", source_in=0.0),
            _placement(1, 10.0, 20.0, asset="a1", source_in=300.0),
        ]
        decisions = conform_transitions.decide(clips, fade_ends=False)
        assert any(d.applied for d in decisions)

    def test_the_episode_opens_and_closes_on_black(self, cut):
        decisions = conform_transitions.decide(cut.placements)
        dips = [d for d in decisions
                if d.applied and d.transition == "Dip to Black"]
        assert len(dips) == 2

    def test_the_ceiling_refuses_extra_dissolves_not_the_end_fades(self):
        clips = [
            _placement(i, i * 10.0, (i + 1) * 10.0, asset=f"a{i}")
            for i in range(10)
        ]
        decisions = conform_transitions.decide(clips, max_transitions=2)
        dissolves = [d for d in decisions
                     if d.applied and d.transition == "Cross Dissolve"]
        dips = [d for d in decisions
                if d.applied and d.transition == "Dip to Black"]
        assert len(dissolves) == 2
        assert len(dips) == 2
        assert any(d.reject_reason == "density_limit" for d in decisions)

    def test_a_clip_too_short_to_carry_one_refuses(self):
        clips = [
            _placement(0, 0.0, 0.4, asset="a1"),
            _placement(1, 0.4, 0.8, asset="a2"),
        ]
        decisions = conform_transitions.decide(clips, fade_ends=False)
        assert all(not d.applied for d in decisions)
        assert any(d.reject_reason == "clip_too_short" for d in decisions)


# ---------------------------------------------------------------------------
# Building and ordering
# ---------------------------------------------------------------------------

class TestBuild:
    def test_the_plan_fixes_its_target_first(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        assert plan.ops[0]["op"] == "sequence.activate"
        assert plan.ops[0]["name"] == "Nova Rough Cut"

    def test_the_tracks_are_added_before_anything_lands_on_them(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        names = [op["op"] for op in plan.ops]
        assert names.index("track.add") < names.index("text.create")

    def test_media_is_imported_before_it_is_placed(self, cut, sounds):
        config = ConformConfig(music=False, color=False, transitions=False,
                               captions=False)
        plan = _built(cut, audio_plan=sounds, config=config)
        names = [op["op"] for op in plan.ops]
        assert names.index("project.import") < names.index("clip.overwrite")

    def test_every_layer_reports_what_it_contributed(self, cut, captions, sounds):
        config = ConformConfig(music=False, color=False)
        plan = _built(cut, caption_plan=captions, audio_plan=sounds,
                      config=config)
        assert plan.contributions["captions"] > 0
        assert plan.contributions["sound"] > 0
        assert "transitions" in plan.contributions

    def test_mode_off_builds_nothing(self, cut, captions):
        plan = conform_build.build(
            rough_cut=cut, config=ConformConfig(mode="off"),
            caption_plan=captions,
        )
        assert plan.ops == []
        assert plan.warnings

    def test_captions_mode_builds_only_captions(self, cut, captions, sounds):
        plan = conform_build.build(
            rough_cut=cut, config=ConformConfig(mode="captions"),
            caption_plan=captions, audio_plan=sounds,
        )
        assert any(op["op"] == "text.create" for op in plan.ops)
        assert not any(op["op"] == "clip.overwrite" for op in plan.ops)

    def test_the_plan_round_trips(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        restored = ConformPlan.from_dict(
            json.loads(json.dumps(plan.to_dict()))
        )
        assert restored.ops == plan.ops
        assert restored.sequence_name == plan.sequence_name
        assert restored.layout == plan.layout


# ---------------------------------------------------------------------------
# Execution guards
# ---------------------------------------------------------------------------

class FakeEngine:
    """Records the plan it was handed and replays a canned outcome."""

    def __init__(self, *, fail_at=()):
        self.plans = []
        self.fail_at = set(fail_at)

    def run(self, plan):
        self.plans.append(plan)
        results = []
        for index, op in enumerate(plan["ops"]):
            ok = index not in self.fail_at
            entry = {"op": op["op"], "index": index, "ok": ok}
            if not ok:
                entry["error"] = "the host said no"
            results.append(entry)
        return {"success": not self.fail_at, "results": results,
                "applied": len(results) - len(self.fail_at)}


class TestExecution:
    def test_a_valid_plan_validates(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        conform_execute.dry_run(plan)
        assert plan.dry_run_passed, plan.dry_run_error

    def test_an_empty_plan_is_refused_with_a_reason(self, cut):
        plan = ConformPlan(sequence_name="Nova Rough Cut")
        conform_execute.dry_run(plan)
        assert not plan.dry_run_passed
        assert plan.dry_run_error["code"] == "empty_plan"

    def test_execution_needs_the_dry_run_to_pass_in_the_same_call(self, cut):
        plan = ConformPlan(sequence_name="Nova Rough Cut")
        engine = FakeEngine()
        report = conform_execute.run(plan, mode="execute", engine=engine)
        assert not report.executed
        assert engine.plans == []
        assert report.refused_reason

    def test_a_plan_that_does_not_fix_its_target_is_refused(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        plan.ops = plan.ops[1:]          # drop the sequence.activate
        safe, reason = conform_execute.targets_scratch_sequence(plan)
        assert not safe
        assert "sequence.activate" in reason

    def test_an_operation_off_the_allowlist_is_refused(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        plan.ops.append({"op": "clip.remove", "clip": {"track": "V1", "index": 0}})
        safe, reason = conform_execute.targets_scratch_sequence(plan)
        assert not safe
        assert "clip.remove" in reason

    def test_writing_to_the_programme_track_is_refused(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        for op in plan.ops:
            if op["op"] == "text.create":
                op["track"] = "V1"
                break
        safe, reason = conform_execute.targets_scratch_sequence(plan)
        assert not safe
        assert "V1" in reason

    def test_a_second_activate_is_refused(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        plan.ops.append({"op": "sequence.activate", "name": "Something Else"})
        safe, reason = conform_execute.targets_scratch_sequence(plan)
        assert not safe
        assert "more than once" in reason

    def test_a_valid_plan_reaches_the_engine(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        engine = FakeEngine()
        report = conform_execute.run(plan, mode="execute", engine=engine)
        assert report.executed
        assert report.operations_succeeded == plan.operation_count
        assert engine.plans[0]["ops"] == plan.ops

    def test_one_failed_operation_does_not_abandon_the_rest(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        engine = FakeEngine(fail_at=(2,))
        report = conform_execute.run(plan, mode="execute", engine=engine)
        assert engine.plans[0]["on_error"] == "continue"
        assert report.executed                       # the rest landed
        assert report.operations_succeeded == plan.operation_count - 1
        assert report.error["detail"]["failed"][0]["index"] == 2

    def test_the_report_counts_what_landed_per_operation(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        report = conform_execute.run(plan, mode="execute", engine=FakeEngine())
        by_layer = conform_execute.executed_by_layer(report, plan)
        assert by_layer["text.create"]["ok"] == 2

    def test_a_dry_run_executes_nothing(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        engine = FakeEngine()
        report = conform_execute.run(plan, mode="dry_run", engine=engine)
        assert not report.executed
        assert engine.plans == []


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class FakeBridge:
    def __init__(self, sequences, export=None, raises=None):
        self.sequences = sequences
        self.export = export or {}
        self.raises = raises
        self.calls = []

    def call(self, op, params=None, **kwargs):
        self.calls.append((op, params))
        if op == "sequence.list":
            return {"sequences": [{"name": n} for n in self.sequences]}
        if op == "sequence.export":
            if self.raises:
                raise self.raises
            return self.export
        raise AssertionError(f"unexpected op {op}")


class TestDelivery:
    def test_the_named_sequence_is_verified_not_trusted(self):
        bridge = FakeBridge(["Nova Rough Cut"])
        name, note = conform_deliver.find_sequence(bridge, "Something Else")
        assert name == ""
        assert "Nova Rough Cut" in note

    def test_several_sequences_and_no_name_is_an_error(self):
        bridge = FakeBridge(["A", "B"])
        name, note = conform_deliver.find_sequence(bridge, "")
        assert name == ""
        assert "several sequences" in note

    def test_a_direct_export_that_writes_a_file_is_delivered(self, tmp_path):
        output = tmp_path / "episode.mp4"

        class WritingBridge(FakeBridge):
            def call(self, op, params=None, **kwargs):
                if op == "sequence.export":
                    Path(params["path"]).write_bytes(b"x" * 2048)
                    return {"started": True, "complete": True,
                            "method": "direct", "path": params["path"],
                            "preset": "H264.epr"}
                return super().call(op, params, **kwargs)

        result = conform_deliver.deliver(
            bridge=WritingBridge(["Nova Rough Cut"]),
            sequence_name="Nova Rough Cut",
            output_path=str(output),
        )
        assert result.delivered
        assert result.size_bytes == 2048
        assert result.method == "direct"

    def test_an_export_that_writes_nothing_is_not_a_success(self, tmp_path):
        bridge = FakeBridge(
            ["Nova Rough Cut"],
            export={"started": True, "complete": True, "method": "direct",
                    "path": str(tmp_path / "missing.mp4")},
        )
        result = conform_deliver.deliver(
            bridge=bridge, sequence_name="Nova Rough Cut",
            output_path=str(tmp_path / "missing.mp4"),
        )
        assert not result.delivered
        assert result.error["code"] == "no_output"

    def test_a_queued_render_is_not_reported_as_finished(self, tmp_path):
        bridge = FakeBridge(
            ["Nova Rough Cut"],
            export={"started": True, "complete": False,
                    "method": "media_encoder",
                    "path": str(tmp_path / "queued.mp4")},
        )
        result = conform_deliver.deliver(
            bridge=bridge, sequence_name="Nova Rough Cut",
            output_path=str(tmp_path / "queued.mp4"),
            wait=0.1, poll=0.05,
        )
        assert not result.delivered
        assert result.method == "media_encoder"
        assert result.warnings

    def test_a_delivery_round_trips(self):
        result = DeliveryResult(sequence_name="S", output_path="x.mp4",
                                exists=True, size_bytes=10)
        restored = DeliveryResult.from_dict(
            json.loads(json.dumps(result.to_dict()))
        )
        assert restored.delivered
        assert restored.output_path == "x.mp4"


# ---------------------------------------------------------------------------
# Verification: judging the edit rather than the footage
# ---------------------------------------------------------------------------

from editing.conform import placement as conform_placement  # noqa: E402
from editing.conform import verify as conform_verify  # noqa: E402


class TestVerification:
    def test_the_moments_are_the_ones_the_editor_changed(self, cut, captions):
        plan = _built(cut, caption_plan=captions)
        moments = conform_verify.moments_of_interest(plan)
        assert moments, "a plan with captions must have something to look at"
        # Sampled a little way in, not on the first frame of the fade-in.
        assert all(at > 0 for at, _ in moments)

    def test_a_caption_is_photographed_after_its_fade_has_finished(self):
        plan = ConformPlan(cut_duration=40.0)
        plan.ops = [{"op": "text.create", "time": 10.0, "duration": 3.0,
                     "note": "a caption"}]
        (at, expects), = conform_verify.moments_of_interest(plan)
        assert at > 10.0
        assert "text.create" in expects[0]

    def test_a_plan_that_changes_nothing_visible_says_so(self, tmp_path):
        plan = ConformPlan(sequence_name="S")
        plan.ops = [{"op": "audio.gain", "clip": {"track": "A1", "all": True},
                     "db": -3.0}]
        result = conform_verify.verify(plan, bridge=None,
                                       output_dir=tmp_path / "frames")
        assert not result.usable
        assert result.note

    def test_no_render_and_no_bridge_is_reported_not_faked(self, tmp_path):
        plan = ConformPlan(sequence_name="S", cut_duration=10.0)
        plan.ops = [{"op": "text.create", "time": 2.0, "duration": 2.0}]
        result = conform_verify.verify(plan, bridge=None,
                                       output_dir=tmp_path / "frames")
        assert not result.supported
        assert not result.usable

    def test_an_unsupported_host_stops_rather_than_inventing_frames(self, tmp_path):
        """Premiere 25 has no scriptable frame export. Saying so beats
        quietly photographing the source footage and calling it the edit."""
        from premiere.errors import UnsupportedError

        class Refusing:
            def call(self, op, params=None, **kwargs):
                if op == "sequence.activate":
                    return {"active": "S"}
                raise UnsupportedError("no scriptable frame export",
                                       alternative="read the source instead")

        plan = ConformPlan(sequence_name="S", cut_duration=10.0)
        plan.ops = [{"op": "text.create", "time": 2.0, "duration": 2.0}]
        result = conform_verify.verify(plan, bridge=Refusing(),
                                       output_dir=tmp_path / "frames")
        assert not result.supported
        assert not result.usable
        assert "frame export" in result.note


class TestCaptionPlacement:
    def test_busyness_comes_from_the_luma_spread(self):
        text = chr(10).join([
            "lavfi.signalstats.YLOW=20",
            "lavfi.signalstats.YHIGH=120",
        ])
        assert conform_placement.parse_busyness(text) == pytest.approx(100.0)

    def test_an_unreadable_frame_measures_nothing(self):
        assert conform_placement.parse_busyness("") is None

    def test_the_quietest_zone_wins_when_it_is_clearly_quieter(self, monkeypatch):
        scores = {(0.5, 0.15): 90.0, (0.26, 0.18): 20.0}
        monkeypatch.setattr(
            conform_placement, "measure_zone",
            lambda path, position, **kw: scores[tuple(position)],
        )
        zone, position, why = conform_placement.choose_zone(
            "frame.png",
            {"upper_center": (0.5, 0.15), "upper_left": (0.26, 0.18)},
            preferred="upper_center",
        )
        assert zone == "upper_left"
        assert position == (0.26, 0.18)
        assert "moved from upper_center" in why

    def test_a_marginal_difference_leaves_the_style_alone(self, monkeypatch):
        scores = {(0.5, 0.15): 90.0, (0.26, 0.18): 87.0}
        monkeypatch.setattr(
            conform_placement, "measure_zone",
            lambda path, position, **kw: scores[tuple(position)],
        )
        zone, _position, why = conform_placement.choose_zone(
            "frame.png",
            {"upper_center": (0.5, 0.15), "upper_left": (0.26, 0.18)},
            preferred="upper_center",
        )
        assert zone == "upper_center"
        assert "no zone is more than" in why

    def test_an_unmeasurable_frame_leaves_the_style_alone(self, monkeypatch):
        monkeypatch.setattr(
            conform_placement, "measure_zone",
            lambda path, position, **kw: None,
        )
        zone, position, why = conform_placement.choose_zone(
            "frame.png", {"upper_center": (0.5, 0.15)},
            preferred="upper_center",
        )
        assert zone == "upper_center"
        assert position == (0.5, 0.15)
        assert "could not be measured" in why

    def test_only_the_styles_own_zones_are_candidates(self):
        """A measurement must never talk a style into a zone it ruled out."""
        from editing.conform.build import _candidate_zones
        from editing.style.presets import ZONE_POSITION, get

        style = get("fast_funny")
        candidates = _candidate_zones(style, ZONE_POSITION)
        assert set(candidates) <= set(style.text_zones)

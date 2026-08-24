"""Caption and audio polish: what earns a place, and what is refused.

Three things carry the weight here, and all three are about *restraint*.

**A caption has to be a key moment.** The failure this pass exists to prevent
is full subtitles, so most of these tests assert that something was refused and
name the rule that refused it. A test that only checked "captions were placed"
would pass just as happily against a module that captions everything.

**A cue may not land on a word.** Every hit, riser and whoosh is checked
against the transcript. The speech-overlap tests are the ones that would catch
a rewrite that made this pass sound automated.

**Nothing here plays, draws or executes anything.** No FFmpeg, no library, no
Premiere, no model. The one external edge -- the asset matcher -- is fed a
hand-built library.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from editing.polish import audio as audio_module
from editing.polish import captions as captions_module
from editing.polish import report as polish_report
from editing.polish import sidecar as sidecar_module
from editing.polish import store as polish_store
from editing.polish.schema import (
    AUDIO_POLISH_MODES, CAPTION_MODES, KEY_MOMENTS, AudioPolishConfig,
    AudioPolishPlan, CaptionConfig, CaptionPlan, allowed_cue_kinds,
    audio_defaults, caption_defaults,
)
from editing.roughcut.schema import ClipPlacement, RoughCutPlan
from editing.schema import (
    AudioEvent, StructureTimeline, TimeRange, TimelineSegment, TranscriptEntry,
    VisualEvent,
)
from editing.style import presets as style_presets


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# Built locally rather than shared, because ``tests/`` deliberately has no
# ``__init__.py`` -- that is what puts the repo root on sys.path -- so test
# modules cannot import from each other.

def make_segment(
    index: int,
    said: str,
    importance: str = "setup",
    *,
    threats=(),
    death: bool = False,
    audio_types=(),
    confidence: float = 0.9,
    length: float = 10.0,
    speech_at: float = 0.5,
    audio_at: float = 0.0,
    audio_length: float = 0.0,
    alignment: str = "match",
    ui: str = "",
) -> TimelineSegment:
    start = index * length
    end = start + length
    event = VisualEvent(
        event_id=f"e{index}", source_file="/f/a.mp4", asset_id="a1",
        start=start, end=end, confidence=0.9, environment="cave",
        actions=["mining"], threats=list(threats), importance=importance,
        suggested_range=TimeRange(start=start, end=end), model="test",
    )
    if death:
        event.ui.death_screen = True
    if ui:
        setattr(event.ui, ui, True)

    entries = []
    if said:
        entries.append(TranscriptEntry(
            start=start + speech_at, end=start + speech_at + 2.0,
            text=said, confidence=confidence,
        ))
    return TimelineSegment(
        segment_id=f"s{index}", asset_id="a1", source_file="/f/a.mp4",
        start=start, end=end, said=said, speech_entries=entries,
        events=[event],
        audio_events=[
            AudioEvent(
                event_id=f"au{index}{kind}", source_file="/f/a.mp4",
                asset_id="a1",
                start=start + audio_at,
                end=(start + audio_at + audio_length) if audio_length else end,
                type=kind, confidence=0.8, detection="heuristic")
            for kind in audio_types
        ],
        alignment=alignment, usefulness=0.7, usable=True,
    )


def make_cut(count: int, *, length: float = 10.0,
             assets=None) -> RoughCutPlan:
    placements = []
    for index in range(count):
        asset = assets[index] if assets else "a1"
        placements.append(ClipPlacement(
            placement_id=f"p{index}", asset_id=asset,
            source_file=f"/f/{asset}.mp4",
            source_in=index * length, source_out=index * length + length,
            sequence_start=index * length, index=index,
        ))
    return RoughCutPlan(sequence_name="Test Cut", placements=placements)


@pytest.fixture
def episode():
    """Seven segments covering most of what this pass can find."""
    return StructureTimeline(segments=[
        make_segment(0, "okay so the plan is to find diamonds today", "setup"),
        make_segment(1, "just walking along here for a bit", "boring"),
        make_segment(2, "oh my god watch out creeper", "danger",
                     threats=("creeper",), audio_types=("sudden_reaction",)),
        make_segment(3, "i died that killed me", "payoff", death=True),
        # The spike sits well clear of the spoken line, so a sting placed on
        # it does not land on a word. That is the arrangement a real reaction
        # has, and without it every hit is (correctly) refused and the
        # placement rules never get exercised.
        make_segment(4, "look at that there it is diamonds", "reveal",
                     audio_types=("loudness_spike",),
                     audio_at=6.0, audio_length=1.0),
        make_segment(5, "um", "boring"),
        make_segment(
            6,
            "so anyway the reason we are doing this is because the enchanting "
            "table needs lapis and obsidian and we also have to build a "
            "nether portal before it gets dark", "boring"),
    ])


@pytest.fixture
def cut():
    return make_cut(7)


@pytest.fixture
def style():
    return style_presets.get("fast_funny")


def plan_captions(episode, cut, style, **overrides):
    config = replace(
        caption_defaults(style, "key_moments"), **overrides).validated()
    return captions_module.build_caption_plan(episode, cut, style, config)


def reasons(plan: CaptionPlan) -> dict:
    return plan.by_reject_reason()


# ---------------------------------------------------------------------------
# Part 1 -- key moment selection
# ---------------------------------------------------------------------------

def test_captions_are_off_by_default():
    """Putting text on somebody's video is not a default."""
    assert CaptionConfig().mode == "off"
    assert CaptionConfig().enabled is False


def test_a_disabled_pass_considers_nothing(episode, cut, style):
    plan = captions_module.build_caption_plan(
        episode, cut, style, CaptionConfig(mode="off"))
    assert plan.decisions == []
    assert any("captions are off" in w for w in plan.warnings)


def test_only_key_moments_are_captioned(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    assert plan.accepted, "nothing was captioned at all"
    for decision in plan.accepted:
        assert decision.moment in KEY_MOMENTS


def test_a_reveal_line_earns_a_caption(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    reveal = [d for d in plan.accepted if d.moment == "reveal"]
    assert reveal, [d.line() for d in plan.decisions]
    assert "diamonds" in reveal[0].full_line


def test_an_objective_line_is_recognised(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    assert any(d.moment == "objective" for d in plan.accepted), \
        [d.line() for d in plan.decisions]


def test_a_named_threat_over_danger_is_a_danger_caption(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    danger = [d for d in plan.accepted if d.moment == "danger"]
    assert danger
    assert "creeper" in danger[0].reason


def test_ordinary_talking_is_refused_as_not_a_key_moment(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    walking = [d for d in plan.decisions
               if d.full_line.startswith("just walking")]
    assert walking and not walking[0].accepted
    assert walking[0].reject_reason == "not_a_key_moment"


def test_every_refusal_names_a_rule_and_says_what_it_saw(episode, cut, style):
    plan = plan_captions(episode, cut, style)
    assert plan.rejected
    for decision in plan.rejected:
        assert decision.reject_reason, decision.line()
        assert decision.reject_detail, decision.line()


def test_a_line_that_was_cut_is_refused_not_moved(episode, style):
    """Captioning footage nobody kept would put text over nothing."""
    # A cut that keeps only the first two clips.
    plan = plan_captions(episode, make_cut(2), style, max_per_minute=60.0)
    cut_out = [d for d in plan.rejected
               if d.reject_reason == "cut_from_the_edit"]
    assert cut_out
    assert all(d.start < 0 for d in cut_out)


def test_a_full_screen_menu_blocks_a_caption(cut, style):
    timeline = StructureTimeline(segments=[
        make_segment(0, "look at that there it is diamonds", "reveal",
                     audio_types=("loudness_spike",), ui="inventory_open"),
    ])
    plan = plan_captions(timeline, cut, style, max_per_minute=60.0)
    assert plan.rejected[0].reject_reason == "blocked_by_ui"
    assert "inventory" in plan.rejected[0].reject_detail


def test_a_style_with_no_text_refuses_every_line(episode, cut):
    quiet = style_presets.get("minimal_clean", max_captions_per_minute=0.0)
    plan = captions_module.build_caption_plan(
        episode, cut, quiet, CaptionConfig(mode="key_moments"))
    assert plan.accepted == []
    assert set(reasons(plan)) == {"style_forbids_text"}


# ---------------------------------------------------------------------------
# Part 1 -- legibility
# ---------------------------------------------------------------------------

def test_a_long_paragraph_is_refused_rather_than_condensed(
    episode, cut, style
):
    """Condensing thirty words to five picks a phrase and calls it a sentence."""
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    long_line = [d for d in plan.decisions if d.full_line.startswith("so anyway")]
    assert long_line and not long_line[0].accepted
    assert long_line[0].reject_reason == "too_many_words"


def test_a_line_that_runs_too_long_in_time_is_refused(cut, style):
    segment = make_segment(0, "look at that there it is diamonds", "reveal",
                           audio_types=("loudness_spike",))
    segment.speech_entries[0].end = segment.speech_entries[0].start + 20.0
    plan = plan_captions(
        StructureTimeline(segments=[segment]), cut, style, max_per_minute=60.0)
    assert plan.rejected[0].reject_reason == "too_long"


def test_filler_is_refused(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    filler = [d for d in plan.decisions if d.full_line.strip() == "um"]
    assert filler and filler[0].reject_reason == "repeated_filler"


def test_an_unclear_transcript_line_is_refused(cut, style):
    timeline = StructureTimeline(segments=[
        make_segment(0, "look at that [inaudible] diamonds", "reveal",
                     audio_types=("loudness_spike",)),
    ])
    plan = plan_captions(timeline, cut, style, max_per_minute=60.0)
    assert plan.rejected[0].reject_reason == "unclear_transcript"


def test_the_same_text_is_never_captioned_twice(cut, style):
    timeline = StructureTimeline(segments=[
        make_segment(0, "look at that there it is diamonds", "reveal",
                     audio_types=("loudness_spike",)),
        make_segment(1, "look at that there it is diamonds", "reveal",
                     audio_types=("loudness_spike",)),
    ])
    plan = plan_captions(timeline, cut, style, max_per_minute=60.0,
                         min_spacing=0.0)
    assert len(plan.accepted) == 1
    assert plan.rejected[0].reject_reason == "duplicate_line"


# ---------------------------------------------------------------------------
# Part 1 -- transcript confidence
# ---------------------------------------------------------------------------

def test_low_confidence_speech_is_refused(cut, style):
    timeline = StructureTimeline(segments=[
        make_segment(0, "look at that there it is diamonds", "reveal",
                     audio_types=("loudness_spike",), confidence=0.3),
    ])
    plan = plan_captions(timeline, cut, style, max_per_minute=60.0,
                         min_confidence=0.6)
    assert plan.rejected[0].reject_reason == "low_confidence"
    assert "0.30" in plan.rejected[0].reject_detail


def test_a_transcript_with_no_confidence_is_trusted_by_default(cut, style):
    """1.0 is ``TranscriptEntry``'s default: nobody said, not "very sure"."""
    timeline = StructureTimeline(segments=[
        make_segment(0, "look at that there it is diamonds", "reveal",
                     audio_types=("loudness_spike",), confidence=1.0),
    ])
    plan = plan_captions(timeline, cut, style, max_per_minute=60.0)
    assert plan.accepted


def test_requiring_confidence_refuses_a_transcript_without_it(cut, style):
    timeline = StructureTimeline(segments=[
        make_segment(0, "look at that there it is diamonds", "reveal",
                     audio_types=("loudness_spike",), confidence=1.0),
    ])
    plan = plan_captions(timeline, cut, style, max_per_minute=60.0,
                         require_confidence=True)
    assert plan.rejected[0].reject_reason == "low_confidence"


def test_quiet_uncertain_speech_reads_as_background(cut, style):
    timeline = StructureTimeline(segments=[
        make_segment(0, "look at that there it is diamonds", "reveal",
                     audio_types=("low_energy",), confidence=0.65),
    ])
    plan = plan_captions(timeline, cut, style, max_per_minute=60.0,
                         min_confidence=0.5)
    assert plan.rejected[0].reject_reason == "background_speech"


# ---------------------------------------------------------------------------
# Part 1 -- density
# ---------------------------------------------------------------------------

def test_the_per_minute_ceiling_is_enforced(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=1.0,
                         min_spacing=0.0)
    # 70 seconds at one a minute is one caption.
    assert len(plan.accepted) == 1
    assert reasons(plan).get("density_limit")


def test_what_survives_the_ceiling_is_what_scored_best(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=1.0,
                         min_spacing=0.0)
    kept = plan.accepted[0]
    for refused in plan.rejected:
        if refused.reject_reason == "density_limit":
            assert refused.priority <= kept.priority


def test_two_captions_never_sit_on_top_of_each_other(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0,
                         min_spacing=0.0)
    windows = sorted((d.start, d.end) for d in plan.accepted)
    for (_, first_end), (second_start, _) in zip(windows, windows[1:]):
        assert second_start >= first_end


def test_spacing_refusals_name_the_caption_they_clash_with(
    episode, cut, style
):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0,
                         min_spacing=30.0)
    clashes = [d for d in plan.rejected
               if d.reject_reason == "too_close_to_another"]
    assert clashes
    assert "from the caption" in clashes[0].reject_detail


def test_a_short_cut_still_allows_one_caption(episode, style):
    """A rate that rounds down to nothing would disable the feature."""
    plan = plan_captions(episode, make_cut(2), style, max_per_minute=0.4)
    assert len(plan.accepted) == 1
    assert any("rounds down to none" in note for note in plan.safety_notes)


def test_no_caption_outlasts_the_ceiling(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0,
                         max_seconds=1.5)
    assert plan.accepted
    for decision in plan.accepted:
        assert decision.duration <= 1.5 + 1e-6


def test_no_caption_carries_more_words_than_the_style_allows(
    episode, cut, style
):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0,
                         max_words=4)
    for decision in plan.accepted:
        assert decision.words <= 4


def test_dense_mode_warns_that_it_is_close_to_subtitles(episode, cut, style):
    config = caption_defaults(style, "dense")
    plan = captions_module.build_caption_plan(episode, cut, style, config)
    assert any("dense" in warning for warning in plan.warnings)


def test_dense_mode_captions_more_than_key_moments(episode, cut):
    loose = style_presets.get("fast_funny", max_captions_per_minute=30.0,
                              min_caption_spacing=0.0)
    key = captions_module.build_caption_plan(
        episode, cut, loose, caption_defaults(loose, "key_moments"))
    dense = captions_module.build_caption_plan(
        episode, cut, loose, caption_defaults(loose, "dense"))
    assert len(dense.accepted) >= len(key.accepted)


# ---------------------------------------------------------------------------
# Part 1 -- style defaults and serialisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", style_presets.names())
def test_every_style_produces_a_valid_caption_config(name):
    config = caption_defaults(style_presets.get(name), "key_moments")
    assert config.mode == "key_moments"
    assert 0 <= config.max_per_minute <= 30
    assert 1 <= config.max_words <= 14


def test_key_moment_mode_is_never_looser_than_the_style():
    loud = style_presets.get("fast_funny")
    config = caption_defaults(loud, "key_moments")
    assert config.max_per_minute <= loud.max_captions_per_minute
    assert config.min_spacing >= loud.min_caption_spacing


def test_a_caption_plan_survives_a_round_trip(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    restored = CaptionPlan.from_dict(plan.to_dict())
    assert len(restored.accepted) == len(plan.accepted)
    assert len(restored.rejected) == len(plan.rejected)
    assert restored.stats()["by_moment"] == plan.stats()["by_moment"]


def test_a_round_trip_never_promotes_a_rejection(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=1.0)
    restored = CaptionPlan.from_dict(plan.to_dict())
    assert len(restored.accepted) == len(plan.accepted)


# ---------------------------------------------------------------------------
# Part 1 -- the sidecar
# ---------------------------------------------------------------------------

def test_the_sidecar_carries_only_accepted_captions(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=1.0)
    text = sidecar_module.to_srt(plan.decisions)
    assert text.count("-->") == len(plan.accepted)


def test_srt_timestamps_are_the_only_format_srt_accepts():
    assert sidecar_module.timestamp(0.0) == "00:00:00,000"
    assert sidecar_module.timestamp(61.5) == "00:01:01,500"
    assert sidecar_module.timestamp(3661.25) == "01:01:01,250"
    # Rounding at a second boundary must not produce ",1000".
    assert sidecar_module.timestamp(1.9999) == "00:00:02,000"


def test_captions_are_never_claimed_to_be_in_the_video(
    episode, cut, style, tmp_path
):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    sidecar_module.write_srt(plan, tmp_path / "cut.srt")
    assert plan.burned_in is False
    assert plan.sidecar_path
    text = polish_report.render_captions(plan)
    assert "not burned into the proxy" in text


# ---------------------------------------------------------------------------
# Part 2 -- audio polish
# ---------------------------------------------------------------------------

def plan_audio(episode, cut, style, library=None, **overrides):
    config = replace(
        audio_defaults(style, "placeholders"), **overrides).validated()
    return audio_module.build_audio_plan(
        episode, cut, style, config, library=library)


def test_audio_polish_is_off_by_default():
    assert AudioPolishConfig().mode == "off"
    assert AudioPolishConfig().enabled is False


def test_a_disabled_pass_considers_no_cue(episode, cut, style):
    plan = audio_module.build_audio_plan(
        episode, cut, style, AudioPolishConfig(mode="off"))
    assert plan.cues == []


def test_every_cue_names_the_moment_it_is_for(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, max_sfx_per_minute=30.0)
    assert plan.accepted
    for cue in plan.accepted:
        assert cue.target
        assert cue.reason


def test_a_cue_never_lands_on_a_spoken_word(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, max_sfx_per_minute=30.0,
                      min_spacing=0.0)
    speech = audio_module._speech_spans(episode, cut)
    for cue in plan.accepted:
        if not cue.counts_as_sfx:
            continue
        for start, end in speech:
            assert not (cue.end > start and cue.start < end), cue.line()


def test_covering_speech_is_a_named_refusal(cut):
    quiet = style_presets.get("cinematic_minecraft")
    timeline = StructureTimeline(segments=[
        # A reveal with the line spoken right over the top of it.
        make_segment(0, "look at that", "reveal",
                     audio_types=("loudness_spike",), speech_at=0.0),
    ])
    plan = plan_audio(timeline, cut, quiet, max_sfx_per_minute=30.0)
    covered = [c for c in plan.rejected
               if c.reject_reason == "would_cover_speech"]
    assert covered
    assert "spoken" in covered[0].reject_detail


def test_the_effect_ceiling_stops_spam(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    loose = plan_audio(episode, cut, quiet, max_sfx_per_minute=30.0,
                       min_spacing=0.0)
    tight = plan_audio(episode, cut, quiet, max_sfx_per_minute=0.9,
                       min_spacing=0.0)
    loose_effects = sum(1 for c in loose.accepted if c.counts_as_sfx)
    tight_effects = sum(1 for c in tight.accepted if c.counts_as_sfx)
    assert tight_effects <= 1
    assert tight_effects < loose_effects
    assert tight.by_reject_reason().get("density_limit")


def test_a_whoosh_needs_a_real_transition_not_a_cut(episode):
    """Two clips from one recording is a trim, not a section change."""
    same_file = make_cut(7)
    plan = plan_audio(episode, same_file,
                      style_presets.get("fast_funny"),
                      max_sfx_per_minute=30.0, min_spacing=0.0)
    assert not [c for c in plan.cues if c.kind == "whoosh"]


def test_a_change_of_source_file_earns_a_whoosh(episode):
    mixed = make_cut(7, assets=["a1", "a1", "a1", "a2", "a2", "a2", "a2"])
    plan = plan_audio(episode, mixed, style_presets.get("fast_funny"),
                      max_sfx_per_minute=30.0, min_spacing=0.0)
    whooshes = [c for c in plan.cues if c.kind == "whoosh"]
    assert len(whooshes) == 1


def test_a_style_that_forbids_a_cue_refuses_it(episode):
    quiet = style_presets.get("cinematic_minecraft")
    assert "whoosh" not in allowed_cue_kinds(quiet)
    mixed = make_cut(7, assets=["a1", "a1", "a1", "a2", "a2", "a2", "a2"])
    plan = plan_audio(episode, mixed, quiet, max_sfx_per_minute=30.0)
    whooshes = [c for c in plan.cues if c.kind == "whoosh"]
    assert whooshes and whooshes[0].reject_reason == "style_forbids"


def test_a_bed_is_refused_when_it_is_not_allowed(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, music_bed=False)
    beds = [c for c in plan.cues if c.kind == "music_bed"]
    assert beds and beds[0].reject_reason == "bed_not_allowed"


def test_a_bed_that_is_allowed_says_how_it_would_behave(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, music_bed=True, ducking=True)
    bed = [c for c in plan.accepted if c.kind == "music_bed"]
    assert bed
    notes = " ".join(bed[0].safety_notes)
    assert "duck" in notes
    assert "tiled" in notes


def test_only_one_bed_is_ever_planned(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, music_bed=True)
    assert sum(1 for c in plan.accepted if c.kind == "music_bed") <= 1


def test_placeholder_mode_reads_no_library(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, max_sfx_per_minute=30.0)
    assert plan.library_size == 0
    for cue in plan.accepted:
        assert cue.placeholder_only is True
        assert not cue.asset_path
    assert plan.placed == []


def test_every_refusal_names_a_rule(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, max_sfx_per_minute=0.5)
    assert plan.rejected
    for cue in plan.rejected:
        assert cue.reject_reason
        assert cue.reject_detail


def test_an_audio_plan_survives_a_round_trip(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, max_sfx_per_minute=30.0)
    restored = AudioPolishPlan.from_dict(plan.to_dict())
    assert len(restored.accepted) == len(plan.accepted)
    assert restored.by_kind() == plan.by_kind()


# ---------------------------------------------------------------------------
# Part 2 -- assets
# ---------------------------------------------------------------------------

def make_library(tmp_path, *, with_impact: bool = True):
    """A hand-built library, so nothing probes or decodes a file."""
    from editing.assets.schema import AssetItem, AssetLibrary, AssetTag

    items = []
    if with_impact:
        path = tmp_path / "sfx" / "impact_boom.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"riff" * 64)
        items.append(AssetItem(
            asset_id="as_impact", path=str(path), filename="impact_boom.wav",
            category="sfx", media_type="audio", duration=1.4,
            tags=[AssetTag(name=name, source="sidecar", confidence=0.9)
                  for name in ("impact", "boom", "hit")],
            intensity="high", safe_for_auto=True,
        ))
    return AssetLibrary(root=str(tmp_path), items=items)


def test_asset_mode_places_what_it_can_match(episode, cut, tmp_path):
    quiet = style_presets.get("cinematic_minecraft")
    library = make_library(tmp_path)
    plan = plan_audio(episode, cut, quiet, library=library,
                      mode="assets", max_sfx_per_minute=30.0)
    placed = plan.placed
    assert placed
    assert placed[0].asset_path.endswith("impact_boom.wav")
    assert placed[0].placeholder_only is False


def test_a_cue_with_nothing_to_play_is_reported_missing(
    episode, cut, tmp_path
):
    quiet = style_presets.get("cinematic_minecraft")
    library = make_library(tmp_path, with_impact=False)
    plan = plan_audio(episode, cut, quiet, library=library,
                      mode="assets", max_sfx_per_minute=30.0)
    assert plan.missing
    assert plan.shopping_list()
    assert all(cue.placeholder_only for cue in plan.missing)


def test_a_missing_library_is_a_warning_not_a_crash(episode, cut):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet, library=None,
                      mode="assets", max_sfx_per_minute=30.0)
    assert any("library" in warning for warning in plan.warnings)
    assert plan.accepted


def test_a_matched_asset_is_never_claimed_to_have_been_heard(
    episode, cut, tmp_path
):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet,
                      library=make_library(tmp_path), mode="assets",
                      max_sfx_per_minute=30.0)
    for cue in plan.placed:
        assert any("listened" in note for note in cue.safety_notes)


# ---------------------------------------------------------------------------
# Reports and storage
# ---------------------------------------------------------------------------

def test_the_caption_report_shows_what_was_refused(episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=1.0)
    text = polish_report.render_captions(plan)
    assert "WHAT WAS REFUSED" in text
    assert "density_limit" in text
    assert "CHECK BY HAND" in text


def test_the_audio_report_lists_the_shopping_list(episode, cut, tmp_path):
    quiet = style_presets.get("cinematic_minecraft")
    plan = plan_audio(episode, cut, quiet,
                      library=make_library(tmp_path, with_impact=False),
                      mode="assets", max_sfx_per_minute=30.0)
    text = polish_report.render_audio(plan)
    assert "MISSING ASSETS" in text
    assert "CHECK BY EAR" in text


def test_no_report_claims_the_edit_is_better(episode, cut, style):
    caption_plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    audio_plan = plan_audio(episode, cut, style_presets.get(
        "cinematic_minecraft"), max_sfx_per_minute=30.0)
    for text in (polish_report.render_captions(caption_plan),
                 polish_report.render_audio(audio_plan)):
        lowered = text.lower()
        assert "retention improved" not in lowered
        assert "guaranteed" not in lowered
        assert "better" not in lowered.split("check by")[0]


def test_plans_round_trip_through_the_store(config, episode, cut, style):
    plan = plan_captions(episode, cut, style, max_per_minute=60.0)
    polish_store.save_captions(config, plan)
    restored = polish_store.load_captions(config)
    assert len(restored.accepted) == len(plan.accepted)

    audio_plan = plan_audio(episode, cut,
                            style_presets.get("cinematic_minecraft"),
                            max_sfx_per_minute=30.0)
    polish_store.save_audio(config, audio_plan)
    assert len(polish_store.load_audio(config).accepted) == \
        len(audio_plan.accepted)


def test_a_missing_plan_is_a_result_not_a_crash(config):
    assert polish_store.captions_or_none(config) is None
    assert polish_store.audio_or_none(config) is None


@pytest.mark.parametrize("mode", CAPTION_MODES)
def test_every_caption_mode_validates(mode):
    assert CaptionConfig(mode=mode).validated().mode == mode


@pytest.mark.parametrize("mode", AUDIO_POLISH_MODES)
def test_every_audio_mode_validates(mode):
    assert AudioPolishConfig(mode=mode).validated().mode == mode


def test_a_nonsense_config_clamps_rather_than_raising():
    config = CaptionConfig(
        mode="nonsense", max_per_minute=-4.0, max_words=99,
        max_seconds=900.0, min_priority=5.0,
    ).validated()
    assert config.mode == "off"
    assert config.max_per_minute == 0.0
    assert config.max_words == 14
    assert config.max_seconds == 12.0
    assert 0.0 <= config.min_priority <= 1.0

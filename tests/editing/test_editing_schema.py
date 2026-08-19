"""Schema tests: coercion, clamping and lossless round-tripping.

The schema is the layer's contract with everything downstream, and the input it
has to survive is a small vision model's free text. These tests are mostly
about the ugly answers -- a duration where a timestamp was asked for, "the
Nether" where "nether" was asked for, a list where an object was asked for --
because those are what actually arrive.
"""
from __future__ import annotations

import json

import pytest

from editing.schema import (
    ENVIRONMENTS, IMPORTANCE_LEVELS, PLAYER_ACTIONS, MediaAsset, PremiereRef,
    StructureTimeline, TimeRange, TimelineSegment, Transcript, TranscriptEntry,
    UIState, VisualEvent, as_float, as_str_list, clamp01, coerce_action,
    coerce_camera_motion, coerce_environment, coerce_importance, parse_timecode,
    short_hash,
)


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("00:00:04,500", 4.5),
    ("00:01:02.250", 62.25),
    ("1:02", 62.0),
    ("01:00:00", 3600.0),
    ("not a time", None),
    ("", None),
])
def test_parse_timecode(text, expected):
    assert parse_timecode(text) == expected


@pytest.mark.parametrize("value,expected", [
    (12, 12.0),
    ("12.5", 12.5),
    ("12.5s", 12.5),
    ("00:00:12.5", 12.5),
    ("about 3 seconds", 3.0),
    (None, 0.0),
    (True, 0.0),          # a bool is not a number here
    (float("nan"), 0.0),  # NaN would poison every later comparison
    (float("inf"), 0.0),
])
def test_as_float(value, expected):
    assert as_float(value) == expected


def test_clamp01_bounds():
    assert clamp01(1.7) == 1.0
    assert clamp01(-3) == 0.0
    assert clamp01("0.65") == 0.65
    assert clamp01(None, 0.5) == 0.5


@pytest.mark.parametrize("value,expected", [
    (["creeper", "zombie"], ["creeper", "zombie"]),
    ("creeper, zombie", ["creeper", "zombie"]),
    ("creeper and zombie", ["creeper", "zombie"]),
    ("none", []),
    ("N/A", []),
    (None, []),
    ([{"name": "creeper"}, {"label": "zombie"}], ["creeper", "zombie"]),
    (["creeper", "Creeper", "CREEPER"], ["creeper"]),   # de-duplicated
])
def test_as_str_list(value, expected):
    assert as_str_list(value) == expected


def test_short_hash_is_stable_and_short():
    assert short_hash("a", 1) == short_hash("a", 1)
    assert short_hash("a", 1) != short_hash("a", 2)
    assert len(short_hash("a")) == 12


# ---------------------------------------------------------------------------
# Vocabulary coercion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("said,expected", [
    ("cave", "cave"),
    ("The Nether", "nether"),
    ("a dark cavern", "cave"),
    ("abandoned mineshaft", "mineshaft"),
    ("Soul Sand Valley", "nether"),
    ("player's base", "base"),
    ("somewhere indescribable", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_coerce_environment(said, expected):
    assert coerce_environment(said) == expected
    assert coerce_environment(said) in ENVIRONMENTS


@pytest.mark.parametrize("said,expected", [
    ("mining", "mining"),
    ("traveling", "travelling"),
    ("running away", "escaping"),
    ("opening a chest", "looting"),
    ("Combat", "fighting"),
    ("nonsense verb", "unknown"),
])
def test_coerce_action(said, expected):
    assert coerce_action(said) == expected
    assert coerce_action(said) in PLAYER_ACTIONS


@pytest.mark.parametrize("said,expected", [
    ("payoff", "payoff"),
    ("climax", "payoff"),
    ("suspense", "tension"),
    ("near death", "danger"),
    ("comedy", "funny"),
    ("discovery", "reveal"),
    ("uneventful", "boring"),
])
def test_coerce_importance(said, expected):
    assert coerce_importance(said) == expected
    assert coerce_importance(said) in IMPORTANCE_LEVELS


def test_coerce_importance_defaults_to_setup():
    """An unreadable importance must not become the highest-ranked value."""
    assert coerce_importance("???") == "setup"


def test_coerce_camera_motion():
    assert coerce_camera_motion("panning") == "pan"
    assert coerce_camera_motion("very shaky") == "shake"
    assert coerce_camera_motion(None) == "unknown"


# ---------------------------------------------------------------------------
# UIState
# ---------------------------------------------------------------------------

def test_ui_state_from_object():
    ui = UIState.from_dict({
        "inventory_open": True,
        "low_health": "yes",
        "coordinates": "X: 112 Y: -54 Z: 88",
        "hotbar": ["diamond pickaxe", "torch"],
    })
    assert ui.inventory_open is True
    assert ui.low_health is True
    assert ui.coordinates.startswith("X: 112")
    assert ui.hotbar == ["diamond pickaxe", "torch"]
    assert ui.any_screen_open is True


def test_ui_state_from_list_answer():
    """Models often answer this field as a list of what is visible."""
    ui = UIState.from_dict(["inventory", "low health"])
    assert ui.inventory_open is True
    assert ui.low_health is True
    assert ui.chest_open is False
    assert "inventory" in ui.other


def test_ui_state_any_screen_open_excludes_toasts():
    """A toast overlays the game; it does not make the footage unusable."""
    ui = UIState.from_dict({"achievement_toast": True})
    assert ui.any_screen_open is False


# ---------------------------------------------------------------------------
# VisualEvent
# ---------------------------------------------------------------------------

def test_visual_event_from_messy_model_output():
    event = VisualEvent.from_dict({
        "source_file": "/f/clip.mp4",
        "start": "10",
        "end": "18",
        "environment": "a deep cavern",
        "actions": "mining and fighting",
        "entities": "2 creepers, a zombie",
        "ui": ["low health"],
        "camera": "very shaky",
        "importance": "close call",
        "confidence": "0.85",
        "notes": "hearts at 2",
    })
    assert event.start == 10.0 and event.end == 18.0
    assert event.environment == "cave"
    assert event.actions == ["mining", "fighting"]
    assert event.entities == ["2 creepers", "a zombie"]
    assert event.ui.low_health is True
    assert event.camera.motion == "shake"
    assert event.importance == "tension"
    assert event.confidence == 0.85


def test_visual_event_preserves_raw_wording():
    """Coercion must never destroy what the model actually said."""
    event = VisualEvent.from_dict({
        "environment": "a spooky deep dark cavern",
        "actions": ["creeping around nervously"],
        "importance": "very suspenseful",
    })
    assert event.raw_environment == "a spooky deep dark cavern"
    assert event.raw_actions == ["creeping around nervously"]
    assert event.raw_importance == "very suspenseful"


def test_visual_event_unknown_action_survives_as_unknown():
    event = VisualEvent.from_dict({"actions": ["doing something weird"]})
    assert event.actions == ["unknown"]
    assert event.primary_action == "unknown"


def test_visual_event_end_never_precedes_start():
    event = VisualEvent.from_dict({"start": 10, "end": 4})
    assert event.end >= event.start


def test_visual_event_suggested_range_defaults_to_window():
    event = VisualEvent.from_dict({"start": 5, "end": 9})
    assert event.suggested_range.start == 5.0
    assert event.suggested_range.end == 9.0


def test_visual_event_weight_scales_with_confidence():
    confident = VisualEvent.from_dict({"importance": "payoff", "confidence": 1.0})
    unsure = VisualEvent.from_dict({"importance": "payoff", "confidence": 0.3})
    assert confident.weight > unsure.weight


def test_visual_event_round_trips():
    original = VisualEvent.from_dict({
        "source_file": "/f/clip.mp4",
        "start": 1.5, "end": 9.5,
        "environment": "nether",
        "actions": ["escaping"],
        "entities": ["ghast"],
        "threats": ["ghast"],
        "importance": "danger",
        "confidence": 0.7,
        "notes": "fireball incoming",
        "frame_times": [2.0, 5.5, 9.0],
    })
    restored = VisualEvent.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.to_dict() == original.to_dict()


def test_time_range_accepts_a_pair():
    assert TimeRange.from_dict([3, 7]).to_dict() == {
        "start": 3.0, "end": 7.0, "duration": 4.0
    }


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

def test_transcript_entry_from_duration():
    entry = TranscriptEntry.from_dict({"start": 4.0, "duration": 2.5, "text": "hi"})
    assert entry.end == 6.5


def test_transcript_entry_overlaps():
    entry = TranscriptEntry(start=4.0, end=8.0, text="hi")
    assert entry.overlaps(6.0, 10.0) == 2.0
    assert entry.overlaps(0.0, 4.0) == 0.0
    assert entry.overlaps(10.0, 12.0) == 0.0


def test_transcript_entries_between():
    transcript = Transcript(asset_id="a1", entries=[
        TranscriptEntry(0, 3, "one"),
        TranscriptEntry(4, 7, "two"),
        TranscriptEntry(9, 12, "three"),
    ])
    picked = transcript.entries_between(3.5, 9.5)
    assert [entry.text for entry in picked] == ["two", "three"]


def test_transcript_round_trips():
    original = Transcript(
        asset_id="a1", source="srt", entries=[TranscriptEntry(1, 2, "hello", "Steve")]
    )
    restored = Transcript.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.source == "srt"
    assert restored.entries[0].speaker == "Steve"
    assert restored.duration == 2.0


def test_transcript_unknown_source_is_rejected():
    """A source outside the vocabulary must not silently pass through."""
    assert Transcript.from_dict({"source": "telepathy"}).source == "unknown"


# ---------------------------------------------------------------------------
# MediaAsset and timeline
# ---------------------------------------------------------------------------

def test_media_asset_round_trips_with_premiere_ref():
    original = MediaAsset(
        asset_id="a1", path="/f/clip.mp4", filename="clip.mp4",
        duration=120.0, width=2560, height=1440, fps=59.94, has_audio=True,
        premiere=PremiereRef(matched=True, item_name="clip.mp4", bin="Footage",
                             sequences=["Episode 12"]),
    )
    restored = MediaAsset.from_dict(json.loads(json.dumps(original.to_dict())))
    assert restored.resolution == "2560x1440"
    assert restored.premiere.matched is True
    assert restored.premiere.sequences == ["Episode 12"]


def test_structure_timeline_stats_and_round_trip():
    segment = TimelineSegment(
        segment_id="s1", asset_id="a1", source_file="/f/clip.mp4",
        start=0.0, end=8.0, said="hello",
        events=[VisualEvent.from_dict({"importance": "payoff", "start": 0, "end": 8})],
        alignment="match", usefulness=0.8, usable=True,
    )
    timeline = StructureTimeline(segments=[segment], assets=[
        MediaAsset(asset_id="a1", path="/f/clip.mp4", filename="clip.mp4")
    ])
    stats = timeline.stats()
    assert stats["segments"] == 1
    assert stats["usable_segments"] == 1
    assert stats["segments_with_speech"] == 1
    assert stats["by_importance"] == {"payoff": 1}

    restored = StructureTimeline.from_dict(json.loads(json.dumps(timeline.to_dict())))
    assert len(restored.segments) == 1
    assert restored.segments[0].alignment == "match"
    assert restored.segments[0].usable is True


def test_segment_importance_takes_the_strongest_event():
    segment = TimelineSegment(
        segment_id="s1", asset_id="a1", source_file="/f/c.mp4", start=0, end=10,
        events=[
            VisualEvent.from_dict({"importance": "boring"}),
            VisualEvent.from_dict({"importance": "payoff"}),
            VisualEvent.from_dict({"importance": "setup"}),
        ],
    )
    assert segment.importance == "payoff"


def test_segment_with_no_events_is_boring():
    segment = TimelineSegment(
        segment_id="s1", asset_id="a1", source_file="/f/c.mp4", start=0, end=1
    )
    assert segment.importance == "boring"


def test_timeline_highlights_are_ranked_and_usable_only():
    def segment(name, score, usable):
        return TimelineSegment(
            segment_id=name, asset_id="a1", source_file="/f/c.mp4",
            start=0, end=5, usefulness=score, usable=usable,
            events=[VisualEvent.from_dict({"importance": "payoff"})],
        )

    timeline = StructureTimeline(segments=[
        segment("low", 0.2, False), segment("best", 0.9, True),
        segment("mid", 0.6, True),
    ])
    assert [s.segment_id for s in timeline.highlights()] == ["best", "mid"]

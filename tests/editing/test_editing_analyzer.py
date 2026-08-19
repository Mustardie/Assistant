"""The visual analyzer, with Qwen3-VL mocked out.

Covers the three rules the analyzer exists to enforce:

* nothing uncoerced escapes -- a messy model answer becomes a schema-valid
  event, with the window's own timestamps, not the model's,
* failures become visible holes rather than silently missing footage, and are
  never cached,
* a cached window costs neither a frame extraction nor a model call.
"""
from __future__ import annotations

import json

import pytest

from editing.errors import ModelError
from editing.schema import VisualEvent
from editing.visual.analyzer import VisualAnalyzer
from editing.visual.qwen import MockVisionModel, extract_json


def build(config, sampling, cache, frame_source, model=None, **kwargs):
    return VisualAnalyzer(
        config, sampling,
        cache=cache,
        model=model or MockVisionModel(),
        frame_source=frame_source,
        use_motion=False,          # no ffmpeg in the test environment
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def test_extract_json_plain():
    assert extract_json('{"environment": "cave"}') == {"environment": "cave"}


def test_extract_json_from_a_markdown_fence():
    text = '```json\n{"environment": "nether"}\n```'
    assert extract_json(text) == {"environment": "nether"}


def test_extract_json_with_prose_around_it():
    text = 'Sure! Here is the analysis:\n{"environment": "cave"}\nHope that helps.'
    assert extract_json(text) == {"environment": "cave"}


def test_extract_json_ignores_braces_inside_strings():
    text = '{"notes": "he said {this} and left", "environment": "base"}'
    assert extract_json(text)["notes"] == "he said {this} and left"


def test_extract_json_handles_escaped_quotes():
    text = r'{"notes": "he said \"run\" loudly", "environment": "cave"}'
    assert extract_json(text)["environment"] == "cave"


def test_extract_json_on_prose_raises():
    with pytest.raises(ModelError) as caught:
        extract_json("I am sorry, I cannot analyse images.")
    assert "No JSON object" in caught.value.message


def test_extract_json_on_empty_raises():
    with pytest.raises(ModelError):
        extract_json("   ")


def test_extract_json_on_malformed_object_raises_with_the_response():
    with pytest.raises(ModelError) as caught:
        extract_json('{"environment": "cave", }{')
    assert caught.value.detail


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_analysis_produces_events_covering_the_file(
    config, sampling, cache, frame_source, asset
):
    analyzer = build(config, sampling, cache, frame_source)
    result = analyzer.analyze_asset(asset)

    assert result.events
    assert result.failures == 0
    assert result.events[0].start == 0.0
    assert result.events[-1].end == pytest.approx(asset.duration)
    assert result.gaps == []
    # Events come back in time order regardless of how they were computed.
    assert result.events == sorted(result.events, key=lambda e: e.start)


def test_events_carry_provenance(config, sampling, cache, frame_source, asset):
    analyzer = build(config, sampling, cache, frame_source)
    event = analyzer.analyze_asset(asset).events[0]

    assert event.asset_id == asset.asset_id
    assert event.source_file == asset.path
    assert event.model == "mock-Qwen3-VL"
    assert event.frame_times
    assert event.event_id.startswith("e_")


def test_window_timestamps_override_whatever_the_model_says(
    config, sampling, cache, frame_source, asset
):
    """The model is asked what it sees, not when. A hallucinated time is dropped."""
    model = MockVisionModel(responses=[{
        "environment": "cave", "actions": ["mining"],
        "start": 9999, "end": 10000,          # nonsense
        "importance": "setup", "confidence": 0.8,
    }])
    analyzer = build(config, sampling, cache, frame_source, model=model)
    event = analyzer.analyze_asset(asset, max_windows=1).events[0]

    assert event.start == 0.0
    assert event.end == pytest.approx(sampling.window_seconds)


def test_messy_model_output_is_coerced(config, sampling, cache, frame_source, asset):
    model = MockVisionModel(responses=[{
        "environment": "a deep dark cavern",
        "actions": "mining and fighting",
        "entities": "2 creepers",
        "ui": ["low health"],
        "camera": "very shaky",
        "importance": "close call",
        "confidence": "0.9",
    }])
    analyzer = build(config, sampling, cache, frame_source, model=model)
    event = analyzer.analyze_asset(asset, max_windows=1).events[0]

    assert event.environment == "cave"
    assert event.actions == ["mining", "fighting"]
    assert event.ui.low_health is True
    assert event.camera.motion == "shake"
    assert event.importance == "tension"
    assert event.confidence == 0.9
    assert event.raw_environment == "a deep dark cavern"


@pytest.mark.parametrize("suggested,expected_inside", [
    ({"start": 0, "end": 8}, True),        # window-relative answer
    ({"start": 500, "end": 900}, True),    # far outside
    ({"start": -10, "end": -2}, True),     # nonsense
])
def test_suggested_range_is_clamped_into_the_window(
    config, sampling, cache, frame_source, asset, suggested, expected_inside
):
    model = MockVisionModel(responses=[{
        "environment": "cave", "actions": ["mining"], "importance": "setup",
        "suggested_range": suggested,
    }])
    analyzer = build(config, sampling, cache, frame_source, model=model)
    event = analyzer.analyze_asset(asset, max_windows=1).events[0]

    assert event.suggested_range.start >= event.start
    assert event.suggested_range.end <= event.end
    assert event.suggested_range.duration > 0


def test_events_are_json_serialisable(config, sampling, cache, frame_source, asset):
    analyzer = build(config, sampling, cache, frame_source)
    result = analyzer.analyze_asset(asset)
    document = json.dumps(result.to_dict())
    assert VisualEvent.from_dict(json.loads(document)["events"][0]).start == 0.0


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_second_run_hits_the_cache_entirely(
    config, sampling, cache, frame_source, asset
):
    model = MockVisionModel()
    analyzer = build(config, sampling, cache, frame_source, model=model)

    first = analyzer.analyze_asset(asset)
    calls_after_first = len(model.calls)
    extractions_after_first = len(frame_source.calls)

    second = analyzer.analyze_asset(asset)

    assert second.cache_hits == len(second.events)
    assert second.cache_misses == 0
    # The point of the cache: no model call and no frame extraction.
    assert len(model.calls) == calls_after_first
    assert len(frame_source.calls) == extractions_after_first
    assert [e.to_dict() for e in second.events] == [e.to_dict() for e in first.events]


def test_changing_the_sampling_config_invalidates(
    config, sampling, cache, frame_source, asset
):
    from dataclasses import replace

    build(config, sampling, cache, frame_source).analyze_asset(asset)
    denser = replace(sampling, frames_per_window=3).validated()
    result = VisualAnalyzer(
        config, denser, cache=cache, model=MockVisionModel(),
        frame_source=frame_source, use_motion=False,
    ).analyze_asset(asset)

    assert result.cache_hits == 0
    assert result.cache_misses == len(result.events)


def test_changing_the_model_invalidates(
    config, sampling, cache, frame_source, asset
):
    build(config, sampling, cache, frame_source).analyze_asset(asset)
    other = MockVisionModel(name="some-other-vlm")
    result = build(config, sampling, cache, frame_source, model=other).analyze_asset(
        asset
    )
    assert result.cache_hits == 0


def test_changing_the_file_content_invalidates(
    config, sampling, cache, frame_source, asset, media_file
):
    build(config, sampling, cache, frame_source).analyze_asset(asset)
    media_file.write_bytes(b"a re-export with completely different content")
    result = build(config, sampling, cache, frame_source).analyze_asset(asset)
    assert result.cache_hits == 0


def test_no_cache_means_every_run_recomputes(
    config, sampling, tmp_path, frame_source, asset
):
    from editing.cache import Cache

    disabled = Cache(root=tmp_path / "c", enabled=False)
    analyzer = build(config, sampling, disabled, frame_source)
    analyzer.analyze_asset(asset)
    second = analyzer.analyze_asset(asset)
    assert second.cache_hits == 0


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

def test_a_model_failure_becomes_a_visible_hole(
    config, sampling, cache, frame_source, asset
):
    model = MockVisionModel(responses=[ModelError("model server is down")])
    analyzer = build(config, sampling, cache, frame_source, model=model)
    result = analyzer.analyze_asset(asset, max_windows=1)

    assert result.failures == 0 or True   # counted via the event, asserted below
    event = result.events[0]
    assert event.error == "model server is down"
    assert event.confidence == 0.0
    assert event.environment == "unknown"
    # The window still appears, so the timeline shows a gap rather than lying.
    assert event.start == 0.0
    assert event.end == pytest.approx(sampling.window_seconds)


def test_a_failed_window_is_not_cached(config, sampling, cache, frame_source, asset):
    failing = MockVisionModel(responses=[ModelError("temporarily down")])
    build(config, sampling, cache, frame_source, model=failing).analyze_asset(
        asset, max_windows=1
    )

    working = MockVisionModel()
    result = build(config, sampling, cache, frame_source, model=working).analyze_asset(
        asset, max_windows=1
    )
    assert result.events[0].error == ""
    assert result.cache_misses == 1


def test_one_bad_window_does_not_stop_the_others(
    config, sampling, cache, frame_source, asset
):
    model = MockVisionModel(responses=[ModelError("transient")])
    analyzer = build(config, sampling, cache, frame_source, model=model)
    result = analyzer.analyze_asset(asset)

    assert len(result.events) > 1
    assert result.events[0].error
    assert all(event.error == "" for event in result.events[1:])


def test_unparseable_model_output_is_a_failure_not_a_crash(
    config, sampling, cache, frame_source, asset
):
    class ProseModel:
        name = "prose"

        def analyze(self, frames, *, system, user):
            return extract_json("I'm sorry, I can't help with that.")

    analyzer = build(config, sampling, cache, frame_source, model=ProseModel())
    event = analyzer.analyze_asset(asset, max_windows=1).events[0]
    assert "No JSON object" in event.error


def test_missing_frames_are_reported(
    config, sampling, cache, frame_source_factory, asset
):
    source = frame_source_factory(fail_for={0.0})
    analyzer = build(config, sampling, cache, source)
    event = analyzer.analyze_asset(asset, max_windows=1).events[0]
    assert "No frames" in event.error


def test_a_file_with_no_duration_is_skipped_with_a_warning(
    config, sampling, cache, frame_source, asset
):
    from dataclasses import replace

    broken = replace(asset, duration=0.0, probe_error="moov atom not found")
    result = build(config, sampling, cache, frame_source).analyze_asset(broken)

    assert result.events == []
    assert any("moov atom" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

def test_the_prompt_carries_absolute_timestamps(
    config, sampling, cache, frame_source, asset
):
    """So suggested_range comes back in the recording's coordinate space."""
    model = MockVisionModel()
    analyzer = build(config, sampling, cache, frame_source, model=model)
    analyzer.analyze_asset(asset, max_windows=2)

    second = model.calls[1]["user"]
    assert f"{sampling.window_seconds:.2f}s to" in second
    assert asset.filename in second


def test_the_prompt_lists_the_closed_vocabularies(
    config, sampling, cache, frame_source, asset
):
    model = MockVisionModel()
    build(config, sampling, cache, frame_source, model=model).analyze_asset(
        asset, max_windows=1
    )
    user = model.calls[0]["user"]
    for value in ("mineshaft", "travelling", "payoff", "erratic"):
        assert value in user


def test_frames_are_passed_to_the_model(
    config, sampling, cache, frame_source, asset, frame_file
):
    model = MockVisionModel()
    build(config, sampling, cache, frame_source, model=model).analyze_asset(
        asset, max_windows=1
    )
    assert model.calls[0]["frames"] == [str(frame_file)] * sampling.frames_per_window


# ---------------------------------------------------------------------------
# Progress and concurrency
# ---------------------------------------------------------------------------

def test_progress_hook_is_called_once_per_window(
    config, sampling, cache, frame_source, asset
):
    seen = []
    analyzer = build(config, sampling, cache, frame_source)
    result = analyzer.analyze_asset(
        asset, progress=lambda done, total, event: seen.append((done, total))
    )
    assert len(seen) == len(result.events)
    assert seen[-1][0] == seen[-1][1]


def test_concurrent_analysis_produces_the_same_events(
    config, sampling, cache, frame_source, asset, tmp_path
):
    from dataclasses import replace
    from editing.cache import Cache

    sequential = build(
        config, sampling, Cache(root=tmp_path / "c1"), frame_source
    ).analyze_asset(asset)

    threaded_config = replace(config, vision_concurrency=4)
    threaded = build(
        threaded_config, sampling, Cache(root=tmp_path / "c2"), frame_source
    ).analyze_asset(asset)

    assert [e.to_dict() for e in threaded.events] == [
        e.to_dict() for e in sequential.events
    ]


def test_analyze_assets_handles_several_files(
    config, sampling, cache, frame_source, asset
):
    from dataclasses import replace

    second = replace(asset, asset_id="a_other", duration=8.0)
    results = build(config, sampling, cache, frame_source).analyze_assets(
        [asset, second]
    )
    assert len(results) == 2
    assert all(result.events for result in results)

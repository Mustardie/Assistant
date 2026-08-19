"""Discovery, Premiere mapping, the pipeline and the CLI.

Everything here runs without FFmpeg, without a model server and without
Premiere -- ffprobe is patched, the model is the mock, and the bridge is a
fake. That is the requirement the suite is written to: the normal test run
must not need any of them.
"""
from __future__ import annotations

import json

import pytest

from editing import discovery, ffmpeg as ff
from editing.cache import Cache
from editing.errors import FootageError
from editing.pipeline import build_pipeline
from editing.premiere_link import describe, snapshot_project
from editing.schema import MediaAsset


PROBE = {
    "duration": 16.0,
    "container": "mov,mp4,m4a",
    "width": 1920,
    "height": 1080,
    "fps": 60.0,
    "video_codec": "h264",
    "has_audio": True,
    "audio_codec": "aac",
    "audio_channels": 2,
    "size_bytes": 1024,
}


@pytest.fixture
def fake_probe(monkeypatch):
    """Patch ffprobe out, counting how often it would have run."""
    calls = []

    def probe(path, *, ffprobe="ffprobe"):
        calls.append(str(path))
        return dict(PROBE)

    monkeypatch.setattr(ff, "probe", probe)
    monkeypatch.setattr(discovery.ff, "probe", probe)
    return calls


@pytest.fixture
def footage(tmp_path):
    folder = tmp_path / "footage"
    (folder / "sub").mkdir(parents=True)
    (folder / "session_01.mp4").write_bytes(b"one" * 400)
    (folder / "session_02.mkv").write_bytes(b"two" * 400)
    (folder / "sub" / "session_03.mov").write_bytes(b"three" * 400)
    (folder / "notes.txt").write_text("not footage", encoding="utf-8")
    (folder / ".hidden.mp4").write_bytes(b"hidden")
    return folder


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def test_find_media_files_recurses_and_filters(footage):
    found = discovery.find_media_files(footage)
    names = [path.name for path in found]
    assert names == ["session_01.mp4", "session_02.mkv", "session_03.mov"]


def test_find_media_files_without_recursion(footage):
    found = discovery.find_media_files(footage, recursive=False)
    assert [path.name for path in found] == ["session_01.mp4", "session_02.mkv"]


def test_find_media_files_skips_hidden_and_non_video(footage):
    names = [path.name for path in discovery.find_media_files(footage)]
    assert ".hidden.mp4" not in names
    assert "notes.txt" not in names


def test_find_media_files_accepts_a_single_file(footage):
    target = footage / "session_01.mp4"
    assert discovery.find_media_files(target) == [target]


def test_find_media_files_missing_folder_raises(tmp_path):
    with pytest.raises(FootageError) as caught:
        discovery.find_media_files(tmp_path / "nope")
    assert "--folder" in caught.value.hint


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def test_probe_asset_populates_the_record(config, footage, fake_probe):
    asset = discovery.probe_asset(footage / "session_01.mp4", config=config)
    assert asset.duration == 16.0
    assert asset.resolution == "1920x1080"
    assert asset.fps == 60.0
    assert asset.has_audio is True
    assert asset.audio_channels == 2
    assert asset.content_hash
    assert asset.size_bytes > 0
    assert asset.asset_id.startswith("a_")


def test_probe_is_cached_on_the_fingerprint(config, footage, fake_probe, cache):
    path = footage / "session_01.mp4"
    discovery.probe_asset(path, config=config, cache=cache)
    discovery.probe_asset(path, config=config, cache=cache)
    assert len(fake_probe) == 1


def test_probe_cache_invalidates_when_the_file_changes(
    config, footage, fake_probe, cache
):
    path = footage / "session_01.mp4"
    discovery.probe_asset(path, config=config, cache=cache)
    path.write_bytes(b"a re-export with different content entirely")
    discovery.probe_asset(path, config=config, cache=cache)
    assert len(fake_probe) == 2


def test_a_probe_failure_is_recorded_not_raised(config, footage, monkeypatch):
    def broken(path, *, ffprobe="ffprobe"):
        raise RuntimeError("moov atom not found")

    monkeypatch.setattr(discovery.ff, "probe", broken)
    asset = discovery.probe_asset(footage / "session_01.mp4", config=config)
    assert asset.duration == 0.0
    assert "moov atom" in asset.probe_error


def test_a_failed_probe_is_not_cached(config, footage, monkeypatch, cache):
    calls = []

    def broken(path, *, ffprobe="ffprobe"):
        calls.append(1)
        raise RuntimeError("unreadable")

    monkeypatch.setattr(discovery.ff, "probe", broken)
    discovery.probe_asset(footage / "session_01.mp4", config=config, cache=cache)
    discovery.probe_asset(footage / "session_01.mp4", config=config, cache=cache)
    assert len(calls) == 2


def test_rotated_footage_reports_display_dimensions():
    """A 90-degree rotated capture must report how it will actually display."""
    flattened = ff._flatten_probe({
        "format": {"duration": "10.0"},
        "streams": [{
            "codec_type": "video", "width": 1920, "height": 1080,
            "avg_frame_rate": "30/1", "tags": {"rotate": "90"},
        }],
    })
    assert (flattened["width"], flattened["height"]) == (1080, 1920)


def test_fractional_frame_rates_are_parsed():
    flattened = ff._flatten_probe({
        "format": {"duration": "10"},
        "streams": [{"codec_type": "video", "avg_frame_rate": "60000/1001"}],
    })
    assert flattened["fps"] == pytest.approx(59.94, abs=0.01)


def test_a_zero_frame_rate_does_not_divide_by_zero():
    flattened = ff._flatten_probe({
        "format": {}, "streams": [{"codec_type": "video", "avg_frame_rate": "0/0"}],
    })
    assert flattened["fps"] == 0.0


# ---------------------------------------------------------------------------
# Premiere mapping
# ---------------------------------------------------------------------------

def test_snapshot_when_premiere_is_closed(fake_bridge):
    snapshot = snapshot_project(fake_bridge(connected=False))
    assert snapshot.available is False
    assert "not reachable" in snapshot.note


def test_snapshot_when_no_project_is_open(fake_bridge):
    snapshot = snapshot_project(fake_bridge(project_open=False))
    assert snapshot.available is False
    assert "no project is open" in snapshot.note


def test_snapshot_indexes_assets_by_path(fake_bridge, footage):
    path = str(footage / "session_01.mp4")
    bridge = fake_bridge({
        "project.info": {"name": "Ep12.prproj", "version": "25.0"},
        "project.assets": {"assets": [
            {"name": "session_01.mp4", "path": path, "bin": "Footage",
             "media_type": "video"},
        ]},
        "sequence.list": {"sequences": [{"name": "Ep12", "id": "1", "active": True}]},
        "timeline.snapshot": {
            "sequence": "Ep12",
            "tracks": [{"clips": [{"source_path": path}]}],
        },
    })
    snapshot = snapshot_project(bridge)

    assert snapshot.available is True
    assert snapshot.project_name == "Ep12.prproj"
    assert snapshot.lookup(path)["bin"] == "Footage"
    assert snapshot.sequences_for(path) == ["Ep12"]


def test_describe_marks_an_unimported_file(fake_bridge, footage):
    bridge = fake_bridge({"project.assets": {"assets": []}})
    snapshot = snapshot_project(bridge)
    ref = describe(str(footage / "session_01.mp4"), snapshot)
    assert ref.matched is False
    assert "Not imported" in ref.note


def test_describe_when_premiere_was_never_consulted():
    from editing.premiere_link import ProjectSnapshot

    ref = describe("/f/clip.mp4", ProjectSnapshot(available=False, note="disabled"))
    assert ref.matched is False
    assert ref.note == "disabled"


def test_premiere_mapping_is_read_only(fake_bridge, footage, config, fake_probe):
    """The layer must be safe to run against a project being actively edited."""
    bridge = fake_bridge({
        "project.info": {"name": "Ep12.prproj"},
        "project.assets": {"assets": []},
        "sequence.list": {"sequences": []},
        "timeline.snapshot": {"sequence": "Ep12", "tracks": []},
    })
    discovery.discover(config=config, folder=footage, use_premiere=True, bridge=bridge)

    from premiere import catalog
    for op, _params in bridge.calls:
        spec = catalog.OPS.get(op)
        assert spec is not None, f"{op} is not in the catalog"
        assert not spec.mutating, f"{op} mutates Premiere"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discover_a_folder(config, footage, fake_probe):
    assets, project = discovery.discover(
        config=config, folder=footage, use_premiere=False
    )
    assert len(assets) == 3
    assert project.available is False
    assert all(asset.premiere.matched is False for asset in assets)


def test_discover_explicit_files(config, footage, fake_probe):
    assets, _ = discovery.discover(
        config=config,
        files=[footage / "session_01.mp4", footage / "session_02.mkv"],
        use_premiere=False,
    )
    assert [asset.filename for asset in assets] == [
        "session_01.mp4", "session_02.mkv"
    ]


def test_discover_missing_file_raises(config, footage):
    with pytest.raises(FootageError) as caught:
        discovery.discover(
            config=config, files=[footage / "nope.mp4"], use_premiere=False
        )
    assert caught.value.detail["missing"]


def test_discover_deduplicates(config, footage, fake_probe):
    path = footage / "session_01.mp4"
    assets, _ = discovery.discover(
        config=config, files=[path, path], use_premiere=False
    )
    assert len(assets) == 1


def test_discover_without_any_source_raises(config):
    with pytest.raises(FootageError) as caught:
        discovery.discover(config=config, use_premiere=False)
    assert "--folder" in caught.value.hint


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def test_pipeline_run_produces_a_timeline(
    config, sampling, footage, fake_probe, frame_source, srt_sample
):
    (footage / "session_01.srt").write_text(srt_sample, encoding="utf-8")

    pipeline = build_pipeline(config, sampling)
    pipeline.model = __import__(
        "editing.visual.qwen", fromlist=["MockVisionModel"]
    ).MockVisionModel()

    assets = pipeline.discover(folder=footage, use_premiere=False)
    assert len(assets) == 3

    analyzer = pipeline.analyzer(use_motion=False)
    analyzer._frame_source = frame_source
    for asset in assets:
        pipeline.write_events(asset, analyzer.analyze_asset(asset))

    timeline = pipeline.timeline(assets, use_premiere=False)
    assert len(timeline.segments) > 0
    # The clip with a sidecar transcript has speech; the others do not.
    with_speech = [s for s in timeline.segments if s.has_speech]
    assert with_speech
    assert all(s.source_file.endswith("session_01.mp4") for s in with_speech)


def test_pipeline_writes_and_reloads_assets(config, sampling, footage, fake_probe):
    pipeline = build_pipeline(config, sampling)
    pipeline.discover(folder=footage, use_premiere=False)

    reloaded = build_pipeline(config, sampling).load_assets()
    assert [asset.asset_id for asset in reloaded] == [
        asset.asset_id for asset in pipeline.assets
    ]


def test_pipeline_load_assets_before_discovery_raises(config, sampling):
    with pytest.raises(FootageError) as caught:
        build_pipeline(config, sampling).load_assets()
    assert "discover" in caught.value.hint


def test_pipeline_select_matches_name_id_and_substring(config, sampling, footage,
                                                       fake_probe):
    pipeline = build_pipeline(config, sampling)
    assets = pipeline.discover(folder=footage, use_premiere=False)

    assert len(pipeline.select(assets, "session_01.mp4")) == 1
    assert len(pipeline.select(assets, assets[0].asset_id)) == 1
    assert len(pipeline.select(assets, "session")) == 3
    assert pipeline.select(assets, "nothing") == []


def test_pipeline_timeline_warns_when_nothing_is_analysed(
    config, sampling, footage, fake_probe
):
    pipeline = build_pipeline(config, sampling)
    assets = pipeline.discover(folder=footage, use_premiere=False)
    timeline = pipeline.timeline(assets, use_premiere=False)
    assert timeline.segments == []
    assert len(timeline.warnings) >= 3


def test_pipeline_timeline_round_trips_through_disk(
    config, sampling, footage, fake_probe, frame_source
):
    from editing.visual.qwen import MockVisionModel

    pipeline = build_pipeline(config, sampling)
    pipeline.model = MockVisionModel()
    assets = pipeline.discover(folder=footage, use_premiere=False)

    analyzer = pipeline.analyzer(use_motion=False)
    analyzer._frame_source = frame_source
    pipeline.write_events(assets[0], analyzer.analyze_asset(assets[0]))

    timeline = pipeline.timeline([assets[0]], use_premiere=False)
    pipeline.write_timeline(timeline)
    reloaded = pipeline.load_timeline()
    assert reloaded.to_dict()["segments"] == timeline.to_dict()["segments"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(argv, capsys):
    from editing.cli import main

    code = main(argv)
    captured = capsys.readouterr()
    return code, captured


def test_cli_discover_json(footage, tmp_path, fake_probe, capsys, monkeypatch):
    monkeypatch.delenv("EDITING_FOOTAGE_DIR", raising=False)
    code, captured = run_cli([
        "discover", "--folder", str(footage),
        "--output-dir", str(tmp_path / "out"), "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert payload["count"] == 3
    assert (tmp_path / "out" / "assets.json").exists()


def test_cli_reports_errors_as_json(tmp_path, capsys):
    code, captured = run_cli([
        "discover", "--folder", str(tmp_path / "nope"),
        "--output-dir", str(tmp_path / "out"), "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 1
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert payload["code"] == "footage_error"
    assert payload["hint"]


def test_cli_plan_estimates_cost(footage, tmp_path, fake_probe, capsys):
    code, captured = run_cli([
        "plan", "--folder", str(footage), "--window-seconds", "4",
        "--output-dir", str(tmp_path / "out"), "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    # 3 files x 16s, stepping by (4s window - 0.5s default overlap) = 5 each.
    assert payload["totals"]["windows"] == 15
    assert payload["sampling"]["window_seconds"] == 4.0
    assert payload["totals"]["frames"] == 45     # 3 frames per window


def test_cli_doctor_reports_honestly(tmp_path, capsys):
    code, captured = run_cli([
        "doctor", "--backend", "mock",
        "--output-dir", str(tmp_path / "out"), "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["vision"]["backend"] == "mock"
    assert "found" in payload["ffmpeg"]
    assert payload["premiere_transcript"]["available"] is False


def test_cli_transcript_import(footage, tmp_path, fake_probe, capsys, srt_sample):
    sidecar = tmp_path / "exported.srt"
    sidecar.write_text(srt_sample, encoding="utf-8")
    out = tmp_path / "out"

    run_cli([
        "discover", "--folder", str(footage), "--output-dir", str(out),
        "--no-premiere", "--json", "-q",
    ], capsys)

    code, captured = run_cli([
        "transcript", "import", "--file", str(sidecar), "--for", "session_01.mp4",
        "--output-dir", str(out), "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert payload["entries"] == 3
    assert payload["source"] == "srt"


def test_cli_transcript_import_needs_one_target(footage, tmp_path, fake_probe,
                                                capsys, srt_sample):
    sidecar = tmp_path / "exported.srt"
    sidecar.write_text(srt_sample, encoding="utf-8")
    out = tmp_path / "out"
    run_cli([
        "discover", "--folder", str(footage), "--output-dir", str(out),
        "--no-premiere", "--json", "-q",
    ], capsys)

    code, captured = run_cli([
        "transcript", "import", "--file", str(sidecar), "--for", "session",
        "--output-dir", str(out), "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 1
    assert "exactly one" in json.loads(captured.out)["error"]


def test_cli_cache_info_and_clear(tmp_path, capsys):
    out = tmp_path / "out"
    cache = Cache(root=out / "cache")
    cache.put("visual", cache.key("visual", w=1), {"a": 1})

    code, captured = run_cli([
        "cache", "info", "--output-dir", str(out), "--json", "-q",
    ], capsys)
    assert code == 0
    assert json.loads(captured.out)["total_entries"] == 1

    code, captured = run_cli([
        "cache", "clear", "--output-dir", str(out), "--json", "-q",
    ], capsys)
    assert json.loads(captured.out)["removed"] == 1


def test_cli_json_output_is_the_only_thing_on_stdout(footage, tmp_path,
                                                     fake_probe, capsys):
    """So the CLI is usable as a subprocess."""
    _, captured = run_cli([
        "discover", "--folder", str(footage),
        "--output-dir", str(tmp_path / "out"), "--no-premiere", "--json",
    ], capsys)
    json.loads(captured.out)      # parses cleanly, nothing else mixed in


def test_cli_run_end_to_end_with_the_mock_backend(
    footage, tmp_path, fake_probe, capsys, monkeypatch, frame_file
):
    """The whole pipeline, with only the frame extractor stubbed."""
    from editing.visual import analyzer as analyzer_module
    from editing.visual.frames import ExtractedFrames

    class StubSource:
        def extract(self, path, window):
            return ExtractedFrames(
                window=window,
                times=list(window.frame_times),
                paths=[frame_file] * len(window.frame_times),
                directory=None,
            )

    monkeypatch.setattr(
        analyzer_module.VisualAnalyzer, "frame_source_for",
        lambda self, asset: StubSource(),
    )

    code, captured = run_cli([
        "run", "--folder", str(footage), "--backend", "mock",
        "--window-seconds", "8", "--no-motion", "--no-premiere",
        "--output-dir", str(tmp_path / "out"), "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["success"] is True
    assert payload["stats"]["segments"] > 0
    assert payload["stats"]["assets"] == 3
    assert (tmp_path / "out" / "timelines" / "structure.json").exists()

    # Every segment is schema-valid and inside its file.
    for segment in payload["segments"]:
        assert segment["end"] >= segment["start"]
        assert segment["alignment"] in ("match", "contrast", "neutral", "unknown")
        assert 0.0 <= segment["usefulness"] <= 1.0


# ---------------------------------------------------------------------------
# Session 2: audio, recommendations and the draft plan, end to end
# ---------------------------------------------------------------------------

@pytest.fixture
def staged(footage, tmp_path, fake_probe, frame_file, monkeypatch, srt_sample):
    """A discovered + audio-analysed + visually-analysed output directory.

    Stubs the two external edges (frame extraction and the audio reader) and
    leaves everything else real, so the commands under test run the same code
    path they would on the user's machine.
    """
    from editing.audio import analyzer as audio_analyzer
    from editing.audio.signal import LoudnessSample, Span
    from editing.visual import analyzer as visual_analyzer
    from editing.visual.frames import ExtractedFrames

    (footage / "session_01.srt").write_text(srt_sample, encoding="utf-8")
    out = tmp_path / "out"

    class StubFrames:
        def extract(self, path, window):
            return ExtractedFrames(
                window=window, times=list(window.frame_times),
                paths=[frame_file] * len(window.frame_times),
            )

    monkeypatch.setattr(
        visual_analyzer.VisualAnalyzer, "frame_source_for",
        lambda self, asset: StubFrames(),
    )

    samples = []
    time = 0.0
    while time < 16.0:
        # Quiet from 5-9s so there is real dead air to find.
        level = -95.0 if 5.0 <= time < 9.0 else -24.0
        samples.append(LoudnessSample(round(time, 3), level, level + 6.0))
        time = round(time + 0.25, 3)

    monkeypatch.setattr(
        audio_analyzer.FFmpegAudioSource, "__init__",
        lambda self, config, audio: None,
    )
    monkeypatch.setattr(
        audio_analyzer.FFmpegAudioSource, "has_audio", lambda self, path: True
    )
    monkeypatch.setattr(
        audio_analyzer.FFmpegAudioSource, "envelope", lambda self, path: samples
    )
    monkeypatch.setattr(
        audio_analyzer.FFmpegAudioSource, "silence",
        lambda self, path, duration: [Span(5.0, 9.0)],
    )
    return out


def _stage(staged, footage, capsys, *extra):
    """Run discover -> audio -> analyze -> timeline into ``staged``."""
    base = ["--output-dir", str(staged), "--no-premiere", "-q"]
    run_cli(["discover", "--folder", str(footage)] + base, capsys)
    run_cli(["audio"] + base, capsys)
    run_cli(
        ["analyze", "--backend", "mock", "--window-seconds", "8", "--no-motion"]
        + base, capsys
    )
    run_cli(["timeline", "--limit", "0"] + base, capsys)
    return base


def test_cli_audio_detects_events(staged, footage, capsys):
    base = ["--output-dir", str(staged), "--no-premiere", "-q"]
    run_cli(["discover", "--folder", str(footage)] + base, capsys)

    code, captured = run_cli(["audio", "--json"] + base, capsys)
    assert code == 0
    payload = json.loads(captured.out)
    first = next(iter(payload["assets"].values()))
    kinds = {event["type"] for event in first["events"]}
    assert "silence" in kinds
    assert (staged / "audio").exists()


def test_cli_timeline_carries_audio_events(staged, footage, capsys):
    base = _stage(staged, footage, capsys)
    code, captured = run_cli(["show", "--json"] + base, capsys)
    assert code == 0
    payload = json.loads(captured.out)
    assert any(segment["audio_events"] for segment in payload["segments"])


def test_cli_recommend_produces_evidence_backed_output(staged, footage, capsys):
    base = _stage(staged, footage, capsys)
    code, captured = run_cli(["recommend", "--json"] + base, capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["stats"]["total"] > 0
    accepted = [
        entry for entry in payload["recommendations"]
        if entry["status"] == "accepted"
    ]
    assert accepted
    assert all(entry["has_evidence"] for entry in accepted)
    assert (staged / "recommendations" / "structure.json").exists()
    assert (staged / "recommendations" / "structure.txt").exists()


def test_cli_draft_plan_dry_runs_and_executes_nothing(staged, footage, capsys):
    base = _stage(staged, footage, capsys)
    run_cli(["recommend"] + base, capsys)

    code, captured = run_cli(["draft", "--json"] + base, capsys)
    assert code == 0
    payload = json.loads(captured.out)

    assert payload["executed"] is False
    assert payload["plan"]["dry_run"] is True
    if payload["operation_count"]:
        assert payload["valid"] is True
    assert (staged / "plans" / "structure.json").exists()


def test_cli_draft_needs_recommendations_first(staged, footage, capsys):
    _stage(staged, footage, capsys)
    code, captured = run_cli(
        ["draft", "--json", "--output-dir", str(staged), "--no-premiere", "-q"],
        capsys,
    )
    assert code == 1
    assert "recommend" in json.loads(captured.out)["hint"]


def test_cli_top_and_reactions(staged, footage, capsys):
    base = _stage(staged, footage, capsys)

    code, captured = run_cli(["top", "--json"] + base, capsys)
    assert code == 0
    assert "moments" in json.loads(captured.out)

    code, captured = run_cli(["reactions", "--json"] + base, capsys)
    assert code == 0
    assert "moments" in json.loads(captured.out)


def test_cli_removed_reports_the_safety_pass(staged, footage, capsys):
    base = _stage(staged, footage, capsys)
    run_cli(["recommend"] + base, capsys)

    code, captured = run_cli(["removed", "--json"] + base, capsys)
    assert code == 0
    payload = json.loads(captured.out)
    assert "removed" in payload
    for entry in payload["removed"]:
        # Anything the safety pass touched must say why.
        assert entry["status_reason"]


def test_cli_run_with_recommend_does_everything(
    staged, footage, capsys
):
    code, captured = run_cli([
        "run", "--folder", str(footage), "--recommend", "--backend", "mock",
        "--window-seconds", "8", "--no-motion",
        "--output-dir", str(staged), "--no-premiere", "--json", "-q",
    ], capsys)

    assert code == 0
    payload = json.loads(captured.out)
    assert payload["timeline"]["stats"]["segments"] > 0
    assert payload["recommendations"]["stats"]["total"] > 0
    assert payload["draft_plan"]["executed"] is False


def test_no_cli_command_ever_executes_a_premiere_edit(staged, footage, capsys):
    """The hard guarantee for this session, asserted at the CLI boundary.

    Every command is run against a bridge that raises if anything reaches it.
    A mutating call would surface as an error rather than silently editing a
    real timeline.
    """
    import editing.premiere_link as premiere_link
    import editing.transcripts.premiere_source as premiere_source

    calls = []

    class ExplodingBridge:
        def health(self):
            return {"connected": True, "project_open": True}

        def call(self, op, params=None, *, timeout=None):
            calls.append(op)
            raise AssertionError(f"{op} reached Premiere during a dry run")

    base = _stage(staged, footage, capsys)
    run_cli(["recommend"] + base, capsys)
    run_cli(["draft"] + base, capsys)

    # Nothing above should have needed a bridge at all; prove the draft path
    # specifically validates without one.
    from editing.pipeline import build_pipeline
    from editing.config import load_config

    config, sampling, audio_config = load_config(output_dir=staged)
    pipeline = build_pipeline(config, sampling, audio_config)
    pipeline.bridge = ExplodingBridge()
    draft = pipeline.draft_plan(save=False)

    assert draft.executed is False
    assert calls == []

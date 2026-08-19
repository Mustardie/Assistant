"""Fixtures for the editing structure layer tests.

Nothing in this suite touches FFmpeg, a GPU, a model server or Premiere. That
is a requirement rather than a convenience: the layer has to be verifiable on a
machine that has none of them, so every external edge has a stub here and the
tests assert on the half of the system that is ours -- sampling maths,
normalisation, coercion, cache keys and alignment.

The stubs deliberately mirror the real interfaces exactly (``FakeBridge``
speaks the panel's envelope, ``StubFrameSource`` returns real ``ExtractedFrames``),
so a test that passes against a stub is asserting on the same call shape the
real component would receive.
"""
from __future__ import annotations

import pytest

from editing.config import EditingConfig, SamplingConfig
from editing.cache import Cache
from editing.schema import MediaAsset, TimeRange, VisualEvent
from editing.visual.frames import ExtractedFrames


# ---------------------------------------------------------------------------
# Config and cache
# ---------------------------------------------------------------------------

@pytest.fixture
def config(tmp_path) -> EditingConfig:
    """A config rooted in a temp dir, with Premiere and the network off."""
    cfg = EditingConfig(
        output_dir=tmp_path / "out",
        use_premiere=False,
        vision_backend="mock",
        vision_model="Qwen3-VL-8B-Instruct",
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def sampling() -> SamplingConfig:
    """Small, exact numbers so window boundaries are easy to assert on."""
    return SamplingConfig(
        window_seconds=4.0,
        window_overlap=0.0,
        frames_per_window=2,
        dense_frames_per_window=4,
        dense_window_seconds=2.0,
        motion_threshold=0.3,
        min_window_seconds=1.0,
    ).validated()


@pytest.fixture
def cache(tmp_path) -> Cache:
    return Cache(root=tmp_path / "cache")


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

@pytest.fixture
def media_file(tmp_path):
    """A real file on disk, so fingerprinting works. Contents are irrelevant."""
    path = tmp_path / "session_01.mp4"
    path.write_bytes(b"not really a video" * 64)
    return path


@pytest.fixture
def asset(media_file) -> MediaAsset:
    from editing.fingerprint import asset_id_for

    return MediaAsset(
        asset_id=asset_id_for(media_file),
        path=str(media_file),
        filename=media_file.name,
        duration=16.0,
        width=1920,
        height=1080,
        fps=60.0,
        has_audio=True,
        audio_channels=2,
        container="mov,mp4",
        video_codec="h264",
    )


def make_event(
    start: float,
    end: float,
    *,
    environment: str = "cave",
    actions=("mining",),
    importance: str = "setup",
    entities=(),
    threats=(),
    confidence: float = 0.8,
    asset_id: str = "a_test",
    source_file: str = "/footage/clip.mp4",
    error: str = "",
) -> VisualEvent:
    """A schema-valid event, for alignment and timeline tests."""
    return VisualEvent(
        event_id=f"e_{start}_{end}",
        source_file=source_file,
        asset_id=asset_id,
        start=start,
        end=end,
        confidence=confidence,
        environment=environment,
        actions=list(actions),
        entities=list(entities),
        threats=list(threats),
        importance=importance,
        suggested_range=TimeRange(start=start, end=end),
        model="test-model",
        error=error,
    )


@pytest.fixture
def event_factory():
    return make_event


# ---------------------------------------------------------------------------
# Stubs for the external edges
# ---------------------------------------------------------------------------

class StubFrameSource:
    """Stands in for FFmpeg frame extraction.

    Returns one existing file per requested timestamp and counts calls, which
    is how the cache tests prove that a cached window does no extraction.
    """

    def __init__(self, frame_path, *, fail_for=()):
        self.frame_path = frame_path
        self.calls = []
        self.fail_for = set(fail_for)

    def extract(self, path, window) -> ExtractedFrames:
        self.calls.append((path, window.start, window.end))
        if window.start in self.fail_for:
            return ExtractedFrames(window=window, directory=None)
        return ExtractedFrames(
            window=window,
            times=list(window.frame_times),
            paths=[self.frame_path] * len(window.frame_times),
            directory=None,
        )


@pytest.fixture
def frame_file(tmp_path):
    path = tmp_path / "frame.jpg"
    # A real JPEG magic number, so anything that sniffs the bytes is happy.
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"stub frame data" * 8)
    return path


@pytest.fixture
def frame_source(frame_file):
    return StubFrameSource(frame_file)


@pytest.fixture
def frame_source_factory(frame_file):
    """Builds a frame source that fails for chosen window start times.

    A factory rather than an importable class: ``tests/`` deliberately has no
    ``__init__.py`` (it is what puts the repo root on ``sys.path``), so test
    modules cannot import from each other.
    """
    def make(*, fail_for=()):
        return StubFrameSource(frame_file, fail_for=fail_for)
    return make


class FakeBridge:
    """Speaks the Premiere panel's call surface without Premiere.

    Mirrors ``tests/premiere/conftest.py``'s fake so the two suites agree on
    what the transport looks like.
    """

    def __init__(self, responses=None, *, connected=True, project_open=True):
        self.responses = dict(responses or {})
        self.connected = connected
        self.project_open = project_open
        self.calls = []
        self.failures = {}

    def health(self):
        return {
            "connected": self.connected,
            "project_open": self.project_open,
            "project": "Test.prproj",
            "version": "25.0.0",
        }

    def require_connected(self):
        if not self.connected:
            from premiere.errors import BridgeError
            raise BridgeError("Cannot reach Adobe Premiere Pro")
        return self.health()

    def call(self, op, params=None, *, timeout=None):
        self.calls.append((op, params or {}))
        if op in self.failures:
            raise self.failures[op]
        if op in self.responses:
            value = self.responses[op]
            return value(params or {}) if callable(value) else value
        return {}


@pytest.fixture
def fake_bridge():
    return FakeBridge


# ---------------------------------------------------------------------------
# Transcript samples
# ---------------------------------------------------------------------------

SRT_SAMPLE = """1
00:00:01,000 --> 00:00:04,000
okay so we are going mining today

2
00:00:05,500 --> 00:00:08,000
>> Steve: watch out for that creeper

3
00:00:09,000 --> 00:00:11,500
<i>that was close</i>
"""

VTT_SAMPLE = """WEBVTT

NOTE this is a note block
that spans lines

cue-1
00:00:01.000 --> 00:00:04.000 align:start position:0%
<v Alice>okay so we are going mining today

00:00:05.500 --> 00:00:08.000
watch out for that creeper
"""


@pytest.fixture
def srt_sample() -> str:
    return SRT_SAMPLE


@pytest.fixture
def vtt_sample() -> str:
    return VTT_SAMPLE

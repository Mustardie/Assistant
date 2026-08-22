"""Producing transcripts locally, and never pretending to.

Four properties carry the weight here.

**A fake is never mistaken for a transcription.** The mock backend exists so
this suite needs no speech model, and every artifact it produces says so -- on
the result, in the JSON, in the text header, and in the note on the transcript
the rest of the system reads. A fabricated transcript that read as real would
make every story finding built on it look sound, which is the one unacceptable
outcome for this package.

**The cache is keyed on everything that changes a word.** Changing the model,
the language, VAD, word timings or the audio itself must miss. Changing the
timeout must not. Both directions are tested, because a cache that over-hits
serves a transcript of the wrong audio and a cache that under-hits costs hours.

**A batch survives its worst file.** Thirty clips where two are corrupt is an
ordinary afternoon; the useful outcome is twenty-eight transcripts and an exact
account of the two.

**A missing dependency is explained, not swallowed.** faster-whisper and FFmpeg
are both optional at import time and both produce an error carrying the command
that fixes it.

Nothing here needs Whisper, a GPU, FFmpeg, Premiere or real media.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from editing.cache import Cache
from editing.errors import EditingError, ToolMissingError
from editing.schema import MediaAsset
from editing.transcribe import audio as audio_module
from editing.transcribe import backends as backends_module
from editing.transcribe import formats as formats_module
from editing.transcribe import run as run_module
from editing.transcribe import store as store_module
from editing.transcribe.schema import (
    BACKENDS, INSTALL_HINT, KNOWN_MODELS, MEDIA_EXTENSIONS,
    TranscriptSegment, TranscriptWord, TranscriptionBatch,
    TranscriptionCacheEntry, TranscriptionConfig, TranscriptionFailure,
    TranscriptionJob, TranscriptionResult, job_id_for,
)
from editing.transcripts import normalize, store as transcript_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def clips(tmp_path) -> Path:
    """Three files that look like media, one that is not, one that is empty."""
    folder = tmp_path / "clips"
    folder.mkdir()
    for index in range(3):
        (folder / f"clip_{index:02d}.mp4").write_bytes(b"fake video " * 300)
    (folder / "notes.txt").write_text("not media", encoding="utf-8")
    (folder / "broken.mp4").write_bytes(b"")
    return folder


@pytest.fixture
def mock_settings() -> TranscriptionConfig:
    return TranscriptionConfig(backend="mock", model="small").validated()


@pytest.fixture
def cache(config) -> Cache:
    return Cache(root=config.cache_dir)


def transcribe(config, path, settings, cache, **kwargs):
    return run_module.transcribe_file(
        config, path, settings=settings, cache=cache, **kwargs)


# ---------------------------------------------------------------------------
# Part 1 -- configuration
# ---------------------------------------------------------------------------

def test_the_defaults_are_practical_rather_than_maximal():
    settings = TranscriptionConfig()
    assert settings.model == "small", "large is unusably slow on CPU"
    assert settings.device == "auto"
    assert settings.compute_type == "auto"
    assert settings.vad_filter is True
    assert settings.word_timestamps is True
    assert settings.language == "", "auto-detect by default"


def test_a_nonsense_config_degrades_instead_of_raising():
    """A bad environment variable must not stop an overnight batch."""
    settings = TranscriptionConfig(
        backend="banana", device="quantum", compute_type="wishful",
        beam_size=999, model="",
    ).validated()

    assert settings.backend == "faster_whisper"
    assert settings.device == "auto"
    assert settings.compute_type == "auto"
    assert settings.beam_size == 10, "clamped, not rejected"
    assert settings.model == "small"


def test_settings_warn_about_the_choices_that_bite():
    assert any("not a known Whisper size" in w
               for w in TranscriptionConfig(model="smalll").warnings)
    assert any("slower than realtime" in w for w in TranscriptionConfig(
        model="large-v3", device="cpu").warnings)
    assert any("hallucinate" in w
               for w in TranscriptionConfig(vad_filter=False).warnings)
    assert TranscriptionConfig(model="small").warnings == []

    # A local model directory is legitimate and must not be warned about.
    assert TranscriptionConfig(model="D:/models/whisper-small").warnings == []


def test_a_config_round_trips():
    settings = TranscriptionConfig(
        model="medium", language="en", word_timestamps=False)
    restored = TranscriptionConfig.from_dict(
        json.loads(json.dumps(settings.to_dict())))
    assert restored == settings


def test_every_known_model_and_backend_is_a_real_string():
    assert "small" in KNOWN_MODELS and "large-v3" in KNOWN_MODELS
    assert set(BACKENDS) == {"faster_whisper", "mock"}
    assert ".mp4" in MEDIA_EXTENSIONS and ".wav" in MEDIA_EXTENSIONS


# ---------------------------------------------------------------------------
# Part 2 -- media detection
# ---------------------------------------------------------------------------

def test_media_files_are_found_and_non_media_is_left_alone(clips):
    found = audio_module.find_media(clips)
    names = {path.name for path in found}
    assert names == {"clip_00.mp4", "clip_01.mp4", "clip_02.mp4",
                     "broken.mp4"}
    assert "notes.txt" not in names


def test_media_scanning_is_sorted_and_deduplicated(clips):
    """A reproducible order is what makes a batch summary comparable."""
    first = audio_module.find_media(clips)
    second = audio_module.find_media(clips)
    assert first == second == sorted(first)


def test_a_file_that_is_not_media_is_refused_with_the_list(clips):
    with pytest.raises(EditingError) as error:
        audio_module.check_readable(clips / "notes.txt")
    assert "not a media file" in str(error.value)
    assert ".mp4" in error.value.hint


def test_an_empty_file_is_refused_before_a_model_is_loaded(clips):
    with pytest.raises(EditingError) as error:
        audio_module.check_readable(clips / "broken.mp4")
    assert "is empty" in str(error.value)
    assert "zero bytes" in error.value.hint


def test_a_missing_path_says_so(tmp_path):
    with pytest.raises(EditingError) as error:
        audio_module.check_readable(tmp_path / "nope.mp4")
    assert "does not exist" in str(error.value)


def test_pointing_folder_at_a_file_and_file_at_a_folder_both_explain(clips):
    with pytest.raises(EditingError) as error:
        audio_module.check_readable(clips)
    assert "transcribe folder" in error.value.hint

    with pytest.raises(EditingError) as error:
        audio_module.find_media(clips / "clip_00.mp4" / "deeper")
    assert "not a file or a folder" in str(error.value)


# ---------------------------------------------------------------------------
# Part 3 -- audio extraction
# ---------------------------------------------------------------------------

def test_the_default_path_extracts_nothing(clips, tmp_path):
    """faster-whisper reads containers itself; extraction is the fallback."""
    media, extracted = audio_module.prepare(
        clips / "clip_00.mp4", cache_dir=tmp_path / "cache")
    assert media == clips / "clip_00.mp4"
    assert extracted is False
    assert not (tmp_path / "cache").exists(), "nothing should have been written"


def test_extraction_writes_to_the_cache_and_never_beside_the_footage(
    clips, tmp_path, monkeypatch
):
    """Someone pointing this at irreplaceable captures gets no new files."""
    written: dict = {}

    def fake_run(command, *, timeout):
        target = Path(command[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF....WAVEfmt ")
        written["command"] = command

        class Completed:
            returncode = 0
            stderr = ""
        return Completed()

    monkeypatch.setattr(audio_module.ff, "_run", fake_run)
    cache_dir = tmp_path / "cache"
    media, extracted = audio_module.prepare(
        clips / "clip_00.mp4", cache_dir=cache_dir, force_extract=True)

    assert extracted is True
    assert cache_dir in media.parents, media
    assert clips not in media.parents, "wrote next to the source footage"
    assert media.suffix == ".wav"

    command = written["command"]
    assert "-vn" in command, "the picture must never be re-encoded"
    assert "16000" in command and "1" in command


def test_extraction_is_reused_rather_than_repeated(clips, tmp_path, monkeypatch):
    calls = []

    def fake_run(command, *, timeout):
        calls.append(command)
        target = Path(command[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"RIFF....WAVEfmt ")

        class Completed:
            returncode = 0
            stderr = ""
        return Completed()

    monkeypatch.setattr(audio_module.ff, "_run", fake_run)
    for _ in range(3):
        audio_module.prepare(
            clips / "clip_00.mp4", cache_dir=tmp_path / "cache",
            force_extract=True)
    assert len(calls) == 1


def test_missing_ffmpeg_during_extraction_says_it_is_only_the_fallback(
    clips, tmp_path, monkeypatch
):
    def missing(command, *, timeout):
        raise ToolMissingError("'ffmpeg' is not installed or not on PATH")

    monkeypatch.setattr(audio_module.ff, "_run", missing)
    with pytest.raises(ToolMissingError) as error:
        audio_module.prepare(
            clips / "clip_00.mp4", cache_dir=tmp_path / "cache",
            force_extract=True)
    assert "FFmpeg is needed to extract audio" in str(error.value)
    assert "Most files transcribe without it" in error.value.hint


def test_a_failed_extraction_leaves_no_half_written_wav(
    clips, tmp_path, monkeypatch
):
    """A partial file is worse than none: a later run would reuse it."""
    def fails(command, *, timeout):
        target = Path(command[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")

        class Completed:
            returncode = 1
            stderr = "Output file does not contain any stream"
        return Completed()

    monkeypatch.setattr(audio_module.ff, "_run", fails)
    cache_dir = tmp_path / "cache"
    with pytest.raises(EditingError) as error:
        audio_module.prepare(
            clips / "clip_00.mp4", cache_dir=cache_dir, force_extract=True)

    assert "no audio track" in error.value.hint
    assert not list((cache_dir / "audio").glob("*.wav"))


# ---------------------------------------------------------------------------
# Part 4 -- the mock backend, and its honesty
# ---------------------------------------------------------------------------

def test_the_mock_backend_marks_everything_it_touches(
    config, clips, mock_settings, cache
):
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    result = job.result

    assert result.mock is True
    assert any("MOCK" in w for w in result.warnings)
    assert all("MOCK" in segment.text for segment in result.segments)

    folder = store_module.job_dir(config, job.job_id)
    saved = json.loads((folder / "transcript.json").read_text("utf-8"))
    assert saved["mock"] is True
    assert "MOCK" in (folder / "transcript.txt").read_text("utf-8")

    stored, _stale = transcript_store.load(config, job.asset_id)
    assert "MOCK TRANSCRIPT" in stored.note
    assert "fabricated" in stored.note


def test_a_real_result_is_not_marked_mock():
    result = TranscriptionResult(backend="faster_whisper")
    assert result.mock is False
    assert "MOCK" not in result.as_transcript(asset_id="a").note


# ---------------------------------------------------------------------------
# Part 5 -- output compatibility
# ---------------------------------------------------------------------------

def test_the_job_folder_holds_every_promised_file(
    config, clips, mock_settings, cache
):
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    folder = store_module.job_dir(config, job.job_id)

    for name in ("transcript.json", "transcript.srt", "transcript.txt",
                 "metadata.json", "warnings.json"):
        assert (folder / name).exists(), name
        assert (folder / name).stat().st_size > 0, name


def test_the_transcript_json_parses_with_the_existing_normalizer(
    config, clips, mock_settings, cache
):
    """The compatibility promise, asserted against the real parser.

    Not a bespoke reader: ``transcripts.normalize.parse_json`` is what every
    other transcript in the system goes through, and this file has to work
    with it unmodified.
    """
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    text = (store_module.job_dir(config, job.job_id) / "transcript.json") \
        .read_text("utf-8")

    entries = normalize.parse_json(text)
    assert len(entries) == len(job.result)
    assert entries[0].text
    assert entries[0].end > entries[0].start


def test_transcribing_publishes_the_transcript_the_pipeline_reads(
    config, clips, mock_settings, cache
):
    """The actual seam. Everything else this package writes is a record."""
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)

    stored, stale = transcript_store.load(config, job.asset_id)
    assert stored is not None
    assert stored.source == "whisper"
    assert len(stored) == len(job.result)
    assert stale is False

    asset = MediaAsset(
        asset_id=job.asset_id, path=str(clips / "clip_00.mp4"),
        filename="clip_00.mp4", duration=60.0)
    resolution = transcript_store.resolve(
        config, asset, cache=cache, use_premiere=False)
    assert resolution.found
    assert resolution.transcript.source == "whisper"


def test_no_publish_writes_the_job_and_not_the_transcript(
    config, clips, mock_settings, cache
):
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache,
                     publish=False)
    assert store_module.job_dir(config, job.job_id).exists()
    stored, _stale = transcript_store.load(config, job.asset_id)
    assert stored is None


def test_srt_is_written_to_the_spec():
    segments = [
        TranscriptSegment(index=0, start=0.0, end=1.5, text="first line"),
        TranscriptSegment(index=1, start=61.25, end=3661.5, text="second"),
    ]
    srt = formats_module.render_srt(segments)
    lines = srt.splitlines()

    assert lines[0] == "1", "1-based indices"
    assert lines[1] == "00:00:00,000 --> 00:00:01,500", "comma, 3 decimals"
    assert lines[2] == "first line"
    assert lines[3] == "", "blank line between cues"
    assert "00:01:01,250 --> 01:01:01,500" in srt, "hours and minutes carry"


def test_srt_timestamps_do_not_round_into_a_thousand_milliseconds():
    """``1.9999`` must become ``00:00:02,000`` and never ``00:00:01,1000``."""
    assert formats_module.srt_timestamp(1.9999) == "00:00:02,000"
    assert formats_module.srt_timestamp(59.9999) == "00:01:00,000"
    assert formats_module.srt_timestamp(3599.9999) == "01:00:00,000"
    assert formats_module.srt_timestamp(0.0) == "00:00:00,000"
    assert formats_module.srt_timestamp(-5.0) == "00:00:00,000"


def test_vtt_differs_from_srt_only_where_the_spec_does():
    segments = [TranscriptSegment(start=1.5, end=2.0, text="hello")]
    vtt = formats_module.render_vtt(segments)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.500 --> 00:00:02.000" in vtt


def test_the_srt_never_re_times_what_the_model_produced():
    """A renderer that nudged cues apart would be invisible until export."""
    segments = [
        TranscriptSegment(start=0.0, end=2.0, text="one"),
        TranscriptSegment(start=1.5, end=3.0, text="two"),   # overlapping
    ]
    srt = formats_module.render_srt(segments)
    assert "00:00:00,000 --> 00:00:02,000" in srt
    assert "00:00:01,500 --> 00:00:03,000" in srt


def test_the_text_export_carries_its_provenance():
    result = TranscriptionResult(
        source_path="/f/ep.mp4", backend="mock", model="small",
        language="en", mock=True,
        segments=[TranscriptSegment(start=0.0, end=1.0, text="hello")],
    )
    text = formats_module.render_txt(result)
    assert "/f/ep.mp4" in text
    assert "MOCK TRANSCRIPT" in text
    assert "[00:00.00] hello" in text


def test_export_writes_every_format(config, clips, mock_settings, cache,
                                    tmp_path):
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    for fmt, marker in (("srt", "-->"), ("vtt", "WEBVTT"),
                        ("txt", "#"), ("json", "segments")):
        target = tmp_path / f"out.{fmt}"
        store_module.export_job(config, job.job_id, target, fmt=fmt)
        assert marker in target.read_text("utf-8"), fmt

    with pytest.raises(EditingError) as error:
        store_module.export_job(config, job.job_id, tmp_path / "x", fmt="docx")
    assert "srt, vtt, txt, json" in error.value.hint


# ---------------------------------------------------------------------------
# Part 6 -- caching
# ---------------------------------------------------------------------------

def test_a_repeat_run_comes_from_the_cache(
    config, clips, mock_settings, cache
):
    first = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    second = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)

    assert first.status == "done"
    assert second.status == "cached"
    assert second.result.cached is True
    assert second.job_id == first.job_id, "same answer, same folder"
    assert len(second.result) == len(first.result)


def test_force_ignores_the_cache(config, clips, mock_settings, cache):
    transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    forced = transcribe(config, clips / "clip_00.mp4", mock_settings, cache,
                        force=True)
    assert forced.status == "done"
    assert forced.result.cached is False


@pytest.mark.parametrize("field,value", [
    ("model", "medium"),
    ("language", "fr"),
    ("word_timestamps", False),
    ("vad_filter", False),
    ("beam_size", 3),
    ("compute_type", "float32"),
    ("backend", "faster_whisper"),
    ("initial_prompt", "Minecraft, creeper"),
])
def test_changing_anything_that_changes_a_word_misses_the_cache(
    config, clips, mock_settings, cache, field, value
):
    first = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    changed = replace(mock_settings, **{field: value}).validated()

    key_a = store_module.cache_key(
        cache, run_module.fingerprint(clips / "clip_00.mp4"), mock_settings)
    key_b = store_module.cache_key(
        cache, run_module.fingerprint(clips / "clip_00.mp4"), changed)
    assert key_a != key_b, f"{field} must change the cache key"
    assert job_id_for("a", "x.mp4", key_a) != job_id_for("a", "x.mp4", key_b)


@pytest.mark.parametrize("field,value", [
    ("use_cache", False),
    ("timeout", 60.0),
    ("min_segment_confidence", 0.9),
])
def test_settings_that_change_no_word_do_not_invalidate_the_cache(
    config, clips, cache, field, value
):
    """Turning the cache off must not throw away everything already in it."""
    base = TranscriptionConfig(backend="mock").validated()
    changed = replace(base, **{field: value}).validated()
    mark = run_module.fingerprint(clips / "clip_00.mp4")
    assert store_module.cache_key(cache, mark, base) == \
        store_module.cache_key(cache, mark, changed)


def test_changing_the_source_file_invalidates_the_cache(
    config, clips, mock_settings, cache
):
    """A re-export must never serve a transcript of the old audio."""
    media = clips / "clip_00.mp4"
    first = transcribe(config, media, mock_settings, cache)

    media.write_bytes(b"completely different audio " * 300)
    second = transcribe(config, media, mock_settings, cache)

    assert second.status == "done", "the content hash changed"
    assert second.job_id != first.job_id


def test_an_empty_transcript_is_never_cached(config, clips, cache, monkeypatch):
    """Caching one bad run would make it permanent, and it is cheap to retry."""
    empty = TranscriptionConfig(backend="mock").validated()
    monkeypatch.setattr(
        backends_module.MockBackend, "transcribe",
        lambda self, path, **kw: TranscriptionResult(
            source_path=str(path), backend="mock", mock=True, duration=10.0),
    )
    transcribe(config, clips / "clip_00.mp4", empty, cache, publish=False)
    key = store_module.cache_key(
        cache, run_module.fingerprint(clips / "clip_00.mp4"), empty)
    assert store_module.cached_result(cache, key, settings=empty) is None


def test_an_unreadable_cache_entry_is_a_miss_not_a_crash(
    config, clips, mock_settings, cache
):
    transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    key = store_module.cache_key(
        cache, run_module.fingerprint(clips / "clip_00.mp4"), mock_settings)
    cache.put(store_module.CACHE_KIND, key, {"segments": "not a list"})

    assert store_module.cached_result(
        cache, key, settings=mock_settings) is None


def test_use_cache_false_never_reads_the_cache(config, clips, cache):
    settings = TranscriptionConfig(backend="mock", use_cache=False).validated()
    transcribe(config, clips / "clip_00.mp4", settings, cache)
    second = transcribe(config, clips / "clip_00.mp4", settings, cache)
    assert second.status == "done"


def test_clearing_the_cache_leaves_the_durable_transcript(
    config, clips, mock_settings, cache
):
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    store_module.clear_cache(cache)

    stored, _stale = transcript_store.load(config, job.asset_id)
    assert stored is not None and len(stored), (
        "clearing a derived cache must never destroy the transcript itself")


def test_a_cache_entry_describes_what_decided_its_key(config, clips, cache):
    settings = TranscriptionConfig(
        backend="mock", model="medium", language="en").validated()
    mark = run_module.fingerprint(clips / "clip_00.mp4")
    entry = TranscriptionCacheEntry.describe(
        "abc", fingerprint=mark, config=settings)

    assert entry.model == "medium" and entry.language == "en"
    assert entry.content_hash == mark.content_hash
    restored = TranscriptionCacheEntry.from_dict(
        json.loads(json.dumps(entry.to_dict())))
    assert restored.key == "abc"


# ---------------------------------------------------------------------------
# Part 7 -- batches
# ---------------------------------------------------------------------------

def test_a_batch_transcribes_every_clip_and_survives_the_broken_one(
    config, clips, mock_settings, cache
):
    batch = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)
    stats = batch.stats()

    assert stats["files"] == 4
    assert stats["done"] == 3
    assert stats["failed"] == 1
    assert stats["words"] > 0

    failure = batch.failed[0]
    assert "broken.mp4" in failure.source_path
    assert failure.failure is not None
    assert failure.failure.stage == "read_media"
    assert failure.failure.recoverable is False, "an empty file stays empty"


def test_a_batch_summary_is_written_and_reloads(
    config, clips, mock_settings, cache
):
    batch = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)
    saved = store_module.list_batches(config)

    assert saved and saved[0].batch_id == batch.batch_id
    assert saved[0].stats()["done"] == 3
    assert saved[0].to_dict()["failures"], "the failures survive the round trip"


def test_a_second_batch_skips_what_already_has_a_transcript(
    config, clips, mock_settings, cache
):
    """What makes ``auto run --transcribe`` safe to leave on."""
    run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)
    again = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)

    stats = again.stats()
    assert stats["skipped"] == 3
    assert stats["done"] == 0
    assert stats["failed"] == 1, "the broken file is still broken"


def test_redoing_existing_transcripts_is_possible(
    config, clips, mock_settings, cache
):
    run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)
    again = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache,
        skip_existing=False, force=True)
    assert again.stats()["done"] == 3


def test_a_stale_transcript_does_not_count_as_present(
    config, clips, mock_settings, cache
):
    """A transcript made from different audio is the thing that needs redoing."""
    run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)
    (clips / "clip_00.mp4").write_bytes(b"different audio entirely " * 300)

    again = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)
    assert again.stats()["done"] == 1, "the changed clip was redone"
    assert again.stats()["skipped"] == 2


def test_an_empty_folder_says_so_rather_than_failing(config, tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    batch = run_module.transcribe_folder(
        config, empty, settings=TranscriptionConfig(backend="mock"))

    assert len(batch) == 0
    assert any("no media files found" in w for w in batch.warnings)
    assert any(".mp4" in w for w in batch.warnings)


def test_a_batch_can_be_limited(config, clips, mock_settings, cache):
    batch = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache, limit=2)
    assert len(batch) == 2


def test_a_batch_transcribes_exactly_the_assets_it_is_given(
    config, clips, mock_settings, cache
):
    """What the auto stage uses: the clips this run is about to analyse."""
    assets = [MediaAsset(
        asset_id="a1", path=str(clips / "clip_01.mp4"),
        filename="clip_01.mp4", duration=60.0)]
    batch = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache, assets=assets)

    assert len(batch) == 1
    assert batch.jobs[0].asset_id == "a1"


def test_one_bug_in_a_file_does_not_abort_the_batch(
    config, clips, mock_settings, cache, monkeypatch
):
    """Not just typed errors: an unexpected exception is still a batch outcome."""
    real = run_module.transcribe_file

    def sometimes_explodes(cfg, path, **kwargs):
        # A file that would otherwise have succeeded: ``find_media`` sorts, so
        # picking "the first call" would have landed on broken.mp4, which was
        # already failing for its own reasons.
        if Path(path).name == "clip_01.mp4":
            raise RuntimeError("something nobody predicted")
        return real(cfg, path, **kwargs)

    monkeypatch.setattr(run_module, "transcribe_file", sometimes_explodes)
    batch = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)

    assert batch.stats()["failed"] == 2      # the bug, plus broken.mp4
    assert batch.stats()["done"] == 2
    internal = [job for job in batch.failed
                if job.failure and job.failure.code == "internal_error"]
    assert internal and "nobody predicted" in internal[0].failure.message


def test_missing_transcripts_reports_what_still_needs_doing(
    config, clips, mock_settings, cache
):
    assets = [
        MediaAsset(asset_id=f"a{i}", path=str(clips / f"clip_{i:02d}.mp4"),
                   filename=f"clip_{i:02d}.mp4", duration=60.0)
        for i in range(3)
    ]
    assert len(run_module.missing_transcripts(config, assets)) == 3

    transcribe(config, clips / "clip_00.mp4", mock_settings, cache,
               asset=assets[0])
    assert len(run_module.missing_transcripts(config, assets)) == 2


# ---------------------------------------------------------------------------
# Part 8 -- missing dependencies
# ---------------------------------------------------------------------------

def test_missing_faster_whisper_names_the_install_command(monkeypatch):
    backend = backends_module.FasterWhisperBackend(TranscriptionConfig())
    monkeypatch.setattr(
        backends_module.FasterWhisperBackend, "installed",
        staticmethod(lambda: False))

    import builtins
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("No module named 'faster_whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ToolMissingError) as error:
        backend.load()

    assert "faster-whisper is not installed" in str(error.value)
    assert "pip install faster-whisper" in error.value.hint


def test_health_reports_readiness_without_loading_a_model(monkeypatch):
    monkeypatch.setattr(
        backends_module.FasterWhisperBackend, "installed",
        staticmethod(lambda: False))
    health = backends_module.check(TranscriptionConfig())

    assert health["ready"] is False
    assert health["backend"] == "faster_whisper"
    assert "pip install faster-whisper" in health["hint"]


def test_the_mock_backend_is_always_ready_and_says_what_it_is():
    health = backends_module.check(TranscriptionConfig(backend="mock"))
    assert health["ready"] is True
    assert "fabricates" in health["hint"]


def test_a_model_that_will_not_load_gets_a_targeted_fix():
    settings = TranscriptionConfig(model="large-v3")
    for reason, expected in (
        ("Library cudnn_ops64_9.dll is not found", "CUDA runtime"),
        ("CUDA failed with error out of memory", "--model small"),
        ("Repository not found: whisper-enormous", "not a known model size"),
        ("something else entirely", "--device cpu"),
    ):
        error = backends_module._load_error(
            settings, "cuda", RuntimeError(reason))
        assert expected in error.hint, reason


def test_a_decode_failure_suggests_extracting_the_audio():
    class Exploding:
        def transcribe(self, *a, **k):
            raise RuntimeError("Invalid data found when processing input")

    backend = backends_module.FasterWhisperBackend(TranscriptionConfig())
    backend._model = Exploding()
    backend._loaded_with = ("small", "cpu", "int8")
    backend.device, backend.compute_type = "cpu", "int8"

    with pytest.raises(EditingError) as error:
        backend.transcribe("clip.mp4", config=TranscriptionConfig())
    assert "--extract-audio" in error.value.hint


# ---------------------------------------------------------------------------
# Part 9 -- reading the model's own numbers honestly
# ---------------------------------------------------------------------------

def test_log_probability_becomes_a_spread_out_confidence():
    """Ranking is the only thing this number is for, so it must spread."""
    assert backends_module.confidence_from_logprob(0.0) == 1.0
    assert backends_module.confidence_from_logprob(-1.6) == 0.0
    assert backends_module.confidence_from_logprob(-0.8) == pytest.approx(0.5)
    assert backends_module.confidence_from_logprob(-99.0) == 0.0
    assert backends_module.confidence_from_logprob(None) == 1.0
    assert backends_module.confidence_from_logprob(float("nan")) == 1.0


class FakeRaw:
    def __init__(self, **kw):
        self.start = kw.get("start", 0.0)
        self.end = kw.get("end", 1.0)
        self.text = kw.get("text", "hello")
        self.avg_logprob = kw.get("avg_logprob", -0.2)
        self.no_speech_prob = kw.get("no_speech_prob", 0.05)
        self.words = kw.get("words")


def test_a_segment_with_no_usable_timing_is_dropped():
    settings = TranscriptionConfig().validated()
    result = TranscriptionResult()
    assert backends_module._segment_from(
        FakeRaw(start=5.0, end=5.0), 0, Path("a.mp4"), result, settings) is None
    assert backends_module._segment_from(
        FakeRaw(text="   "), 0, Path("a.mp4"), result, settings) is None


def test_a_segment_the_model_calls_silence_is_dropped():
    settings = TranscriptionConfig(max_no_speech=0.8).validated()
    result = TranscriptionResult()
    assert backends_module._segment_from(
        FakeRaw(no_speech_prob=0.95), 0, Path("a.mp4"), result,
        settings) is None
    assert backends_module._segment_from(
        FakeRaw(no_speech_prob=0.1), 0, Path("a.mp4"), result,
        settings) is not None


def test_a_low_confidence_segment_is_kept_and_flagged():
    settings = TranscriptionConfig().validated()
    segment = backends_module._segment_from(
        FakeRaw(avg_logprob=-1.5), 0, Path("a.mp4"), TranscriptionResult(),
        settings)
    assert segment is not None
    assert segment.is_low_confidence
    assert any("low confidence" in w for w in segment.warnings)


def test_word_timings_survive_when_they_are_asked_for():
    class Word:
        def __init__(self, word, start, end, probability):
            self.word, self.start = word, start
            self.end, self.probability = end, probability

    settings = TranscriptionConfig(word_timestamps=True).validated()
    segment = backends_module._segment_from(
        FakeRaw(words=[Word(" hello", 0.0, 0.4, 0.9),
                       Word(" there", 0.4, 0.9, 0.8),
                       Word("  ", 0.9, 1.0, 0.1)]),      # blank, dropped
        0, Path("a.mp4"), TranscriptionResult(), settings)

    assert [w.word for w in segment.words] == [" hello", " there"]
    assert segment.words[0].probability == pytest.approx(0.9)


def test_an_empty_result_says_what_to_check():
    result = TranscriptionResult(duration=600.0)
    backends_module._add_warnings(result, TranscriptionConfig().validated())
    assert any("no speech was found" in w for w in result.warnings)
    assert any("audio track" in w for w in result.warnings)


def test_a_nearly_silent_result_is_flagged_rather_than_shipped_quietly():
    result = TranscriptionResult(
        duration=600.0,
        segments=[TranscriptSegment(start=0.0, end=2.0, text="hi")],
    )
    backends_module._add_warnings(result, TranscriptionConfig().validated())
    assert any("came back as speech" in w for w in result.warnings)


def test_the_realtime_factor_answers_the_question_people_actually_have():
    result = TranscriptionResult(duration=2400.0, elapsed=600.0)
    assert result.realtime_factor == 4.0

    cached = TranscriptionResult(duration=2400.0, elapsed=600.0, cached=True)
    assert cached.realtime_factor == 0.0, "nothing was decoded"


# ---------------------------------------------------------------------------
# Part 10 -- records and jobs
# ---------------------------------------------------------------------------

def test_a_result_round_trips_through_json():
    result = TranscriptionResult(
        job_id="j1", source_path="/f/ep.mp4", backend="faster_whisper",
        model="small", device="cpu", language="en", duration=120.0,
        word_timestamps=True,
        segments=[TranscriptSegment(
            index=0, start=1.0, end=2.5, text="hello there",
            confidence=0.8, no_speech_prob=0.1,
            words=[TranscriptWord(word=" hello", start=1.0, end=1.6,
                                  probability=0.95)],
            source_file="/f/ep.mp4", language="en", model="small",
        )],
    )
    restored = TranscriptionResult.from_dict(
        json.loads(json.dumps(result.to_dict())))

    assert restored.job_id == "j1"
    assert len(restored) == 1
    assert restored.segments[0].text == "hello there"
    assert restored.segments[0].words[0].word == " hello"
    assert restored.word_count == 2


def test_a_job_id_is_stable_for_one_file_and_one_configuration():
    """Re-transcribing the same thing must land in the same folder."""
    first = job_id_for("a1", "D:/clips/ep12.mp4", "key-abc")
    assert first == job_id_for("a1", "D:/clips/ep12.mp4", "key-abc")
    assert first != job_id_for("a1", "D:/clips/ep12.mp4", "key-xyz")
    assert "ep12" in first, "browsable in a file manager"


def test_a_failed_job_still_leaves_something_to_debug(
    config, clips, mock_settings, cache
):
    batch = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)
    failed = batch.failed[0]

    assert failed.failure is not None
    assert failed.failure.render()
    restored = TranscriptionJob.from_dict(
        json.loads(json.dumps(failed.to_dict())))
    assert restored.status == "failed"
    assert restored.failure.message == failed.failure.message


def test_jobs_are_listable_and_loadable(config, clips, mock_settings, cache):
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    listed = store_module.list_jobs(config)

    assert [item.job_id for item in listed] == [job.job_id]
    assert store_module.load_job(config, job.job_id).job_id == job.job_id
    assert len(store_module.load_result(config, job.job_id)) == len(job.result)


def test_listing_jobs_ignores_the_durable_transcript_files(
    config, clips, mock_settings, cache
):
    """Job folders and ``<asset_id>.json`` share a directory on purpose."""
    transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    files = list(config.transcripts_dir.glob("*.json"))
    assert files, "the durable transcript is a file in transcripts/"
    assert len(store_module.list_jobs(config)) == 1, "and is not a job"


def test_an_unknown_job_says_how_to_list_them(config):
    with pytest.raises(EditingError) as error:
        store_module.load_job(config, "nope")
    assert "transcribe status" in error.value.hint


def test_a_batch_record_round_trips():
    batch = TranscriptionBatch(
        batch_id="b1", root="/clips",
        jobs=[TranscriptionJob(job_id="j1", status="done"),
              TranscriptionJob(job_id="j2", status="failed",
                               failure=TranscriptionFailure(
                                   stage="decode", message="nope"))],
    )
    restored = TranscriptionBatch.from_dict(
        json.loads(json.dumps(batch.to_dict())))
    assert len(restored) == 2
    assert restored.stats()["failed"] == 1


# ---------------------------------------------------------------------------
# Part 11 -- the report
# ---------------------------------------------------------------------------

def test_the_report_leads_with_the_numbers_that_matter(
    config, clips, mock_settings, cache
):
    job = transcribe(config, clips / "clip_00.mp4", mock_settings, cache)
    text = formats_module.render_report(job.result)

    assert "TRANSCRIPTION" in text
    assert "speech" in text and "segments" in text
    assert "MOCK BACKEND" in text, "a fake must announce itself in the report"


def test_the_report_estimates_a_real_episode():
    result = TranscriptionResult(
        duration=60.0, elapsed=10.0,
        segments=[TranscriptSegment(start=0.0, end=1.0, text="hi")],
    )
    text = formats_module.render_report(result)
    assert "6.0x realtime" in text
    assert "40-minute episode would take ~7 min" in text


def test_a_cached_report_does_not_claim_a_speed():
    result = TranscriptionResult(duration=60.0, elapsed=10.0, cached=True)
    assert "from the cache" in formats_module.render_report(result)


# ---------------------------------------------------------------------------
# Part 12 -- nothing here needs a model, a GPU, FFmpeg or real media
# ---------------------------------------------------------------------------

def test_the_package_imports_nothing_heavy_at_module_scope():
    """The late import is what makes this whole suite runnable anywhere.

    If ``faster_whisper`` moved to module scope, importing the CLI would fail
    on a machine that never installed it -- and every command in the editing
    layer would go down with it.
    """
    import ast
    import importlib
    import inspect

    heavy = {"faster_whisper", "torch", "ctranslate2"}
    for name in ("schema", "audio", "formats", "store", "run", "backends"):
        module = importlib.import_module(f"editing.transcribe.{name}")
        tree = ast.parse(inspect.getsource(module))
        # Only top-level statements: an import inside a function body is
        # exactly the pattern being checked for, not a violation of it.
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            assert not (heavy & set(names)), (
                f"editing.transcribe.{name} imports {names} at module scope; "
                "it must be imported inside the function that needs it"
            )


def test_a_whole_batch_runs_with_no_external_tool(
    config, clips, mock_settings, cache
):
    """The end-to-end promise of this suite, asserted in one place."""
    batch = run_module.transcribe_folder(
        config, clips, settings=mock_settings, cache=cache)
    assert batch.stats()["done"] == 3

    for job in batch.jobs:
        if job.result is None:
            continue
        folder = store_module.job_dir(config, job.job_id)
        assert (folder / "transcript.srt").exists()
        entries = normalize.parse_json(
            (folder / "transcript.json").read_text("utf-8"))
        assert entries

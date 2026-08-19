"""Transcript normalisation, Premiere marker grouping, and the store.

The rule these tests exist to defend: **timing is never invented**. Several of
them assert that a transcript with no timestamps is rejected rather than
spread across the runtime, because that failure would be invisible in the
output and would misalign every segment downstream.
"""
from __future__ import annotations

import json

import pytest

from editing.errors import TranscriptError
from editing.schema import TranscriptEntry
from editing.transcripts import normalize, store
from editing.transcripts.premiere_source import (
    PREMIERE_TICKS_PER_SECOND, group_word_markers, probe_support, pull,
)


# ---------------------------------------------------------------------------
# SRT / VTT
# ---------------------------------------------------------------------------

def test_parse_srt(srt_sample):
    entries = normalize.parse_srt(srt_sample)
    assert len(entries) == 3
    assert entries[0].start == 1.0 and entries[0].end == 4.0
    assert entries[0].text == "okay so we are going mining today"
    assert entries[1].speaker == "Steve"
    assert entries[1].text == "watch out for that creeper"
    # Markup is stripped, the words are kept.
    assert entries[2].text == "that was close"


def test_parse_vtt_strips_header_notes_and_settings(vtt_sample):
    entries = normalize.parse_vtt(vtt_sample)
    assert len(entries) == 2
    assert entries[0].speaker == "Alice"
    assert entries[0].text == "okay so we are going mining today"
    assert entries[1].start == 5.5


def test_srt_multiline_cue_is_joined():
    entries = normalize.parse_srt(
        "1\n00:00:01,000 --> 00:00:04,000\nfirst line\nsecond line\n"
    )
    assert entries[0].text == "first line second line"


def test_srt_skips_contentless_cues():
    entries = normalize.parse_srt(
        "1\n00:00:01,000 --> 00:00:02,000\n[inaudible]\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nreal words\n"
    )
    assert [entry.text for entry in entries] == ["real words"]


def test_srt_keeps_sound_markers_for_the_audio_layer():
    """``[laughs]``/``[music]`` are evidence, not noise -- they must survive.

    A transcriber naming a sound is stronger evidence than any loudness
    heuristic, so ``editing.audio.markers`` needs these cues to still be here.
    """
    entries = normalize.parse_srt(
        "1\n00:00:01,000 --> 00:00:02,000\n[laughs]\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n[music]\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nreal words\n"
    )
    assert [entry.text for entry in entries] == ["[laughs]", "[music]", "real words"]


def test_inline_speaker_only_split_when_it_looks_like_a_name():
    """"Okay: let's go" must not produce a speaker called "Okay"."""
    entries = normalize.parse_srt(
        "1\n00:00:01,000 --> 00:00:02,000\nOkay: lets go now everyone\n"
    )
    assert entries[0].speaker == ""
    assert entries[0].text.startswith("Okay:")


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def test_parse_json_whisper_segments():
    entries = normalize.parse_json(json.dumps({
        "text": "full text",
        "segments": [
            {"start": 0.0, "end": 2.5, "text": " hello there"},
            {"start": 2.5, "end": 5.0, "text": " general kenobi"},
        ],
    }))
    assert [entry.text for entry in entries] == ["hello there", "general kenobi"]


def test_parse_json_finds_nested_cue_list():
    """Vendor formats nest the cues; the parser must not need a dialect flag."""
    entries = normalize.parse_json(json.dumps({
        "results": {"channels": [{"alternatives": [{"utterances": [
            {"start": 1.0, "end": 2.0, "text": "nested"},
            {"start": 2.0, "end": 3.0, "text": "deeply"},
        ]}]}]}
    }))
    assert [entry.text for entry in entries] == ["nested", "deeply"]


def test_parse_json_top_level_list():
    entries = normalize.parse_json(json.dumps([
        {"start": 0, "end": 1, "text": "a"}, {"start": 1, "end": 2, "text": "b"},
    ]))
    assert len(entries) == 2


def test_parse_json_without_timed_entries_raises():
    with pytest.raises(TranscriptError) as caught:
        normalize.parse_json(json.dumps({"words": "no timing here"}))
    assert "No timed entries" in caught.value.message


def test_parse_json_invalid_raises_with_a_hint():
    with pytest.raises(TranscriptError) as caught:
        normalize.parse_json("{not json,}")
    assert caught.value.hint


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def test_parse_csv_premiere_style():
    entries = normalize.parse_csv(
        "Speaker,Start Time,End Time,Text\n"
        "Speaker 1,00:00:01,00:00:03,hello there\n"
        "Speaker 2,00:00:04,00:00:06,hi back\n"
    )
    assert len(entries) == 2
    assert entries[0].speaker == "Speaker 1"
    assert entries[0].start == 1.0 and entries[0].end == 3.0


def test_parse_tsv_is_detected_by_content():
    entries = normalize.parse_csv(
        "start\tend\ttext\n00:00:01\t00:00:02\ttabbed line\n"
    )
    assert entries[0].text == "tabbed line"


def test_parse_csv_without_usable_columns_raises():
    with pytest.raises(TranscriptError) as caught:
        normalize.parse_csv("colour,shape\nred,round\n")
    assert "start/text columns" in caught.value.message


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------

def test_parse_txt_bracketed_timestamps():
    entries = normalize.parse_txt(
        "[00:00:03] we found diamonds\n[00:00:06] lets go\n"
    )
    assert [entry.start for entry in entries] == [3.0, 6.0]


def test_parse_txt_premiere_speaker_blocks():
    """Premiere's .txt export: speaker + time on one line, words below."""
    entries = normalize.parse_txt(
        "Speaker 1\t00:00:03\n"
        "we found diamonds\n"
        "and there are loads of them\n"
        "Speaker 2\t00:00:09\n"
        "nice one\n"
    )
    assert len(entries) == 2
    assert entries[0].speaker == "Speaker 1"
    assert entries[0].text == "we found diamonds and there are loads of them"
    assert entries[1].start == 9.0


def test_parse_txt_ranged_timestamps():
    entries = normalize.parse_txt("00:00:03 --> 00:00:07  mining away\n")
    assert entries[0].start == 3.0 and entries[0].end == 7.0


def test_parse_txt_without_timestamps_is_refused():
    """The critical honesty test: no timings means no transcript, not a guess."""
    with pytest.raises(TranscriptError) as caught:
        normalize.parse_txt("we went mining and it was great\nthen we died\n")
    assert "no timestamps" in caught.value.message
    assert "will not be guessed at" in caught.value.hint


# ---------------------------------------------------------------------------
# Sniffing and dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,suffix,expected", [
    ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi", ".txt", ".vtt"),
    ('{"segments": []}', ".txt", ".json"),
    ("[00:00:03] words", ".txt", ".txt"),      # not JSON despite the bracket
    ('[{"start": 1}]', ".srt", ".json"),
    ("1\n00:00:01,000 --> 00:00:02,000\nhi", ".dat", ".srt"),
])
def test_sniff_format_prefers_content_over_extension(text, suffix, expected):
    assert normalize.sniff_format(text, suffix) == expected


def test_parse_file_reads_and_reports_source(tmp_path, srt_sample):
    path = tmp_path / "clip.srt"
    path.write_text(srt_sample, encoding="utf-8")
    entries, source = normalize.parse_file(path)
    assert source == "srt"
    assert len(entries) == 3


def test_parse_file_missing_raises_with_export_hint(tmp_path):
    with pytest.raises(TranscriptError) as caught:
        normalize.parse_file(tmp_path / "nope.srt")
    assert "Text panel" in caught.value.hint


def test_parse_file_strips_a_bom(tmp_path):
    path = tmp_path / "clip.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8-sig")
    entries, _ = normalize.parse_file(path)
    assert entries[0].text == "hi"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def test_normalize_sorts_and_gives_point_cues_an_end():
    entries = normalize.normalize_entries([
        TranscriptEntry(6.0, 6.0, "second"),
        TranscriptEntry(3.0, 3.0, "first"),
    ])
    assert [entry.text for entry in entries] == ["first", "second"]
    # A point cue ends where the next one starts, capped at the default gap.
    assert entries[0].end == 5.0
    assert entries[1].end == 8.0


def test_normalize_clamps_to_media_duration():
    entries = normalize.normalize_entries(
        [TranscriptEntry(8.0, 20.0, "over the end")], max_duration=10.0
    )
    assert entries[0].end == 10.0


def test_normalize_drops_empty_and_negative():
    entries = normalize.normalize_entries([
        TranscriptEntry(1.0, 2.0, "   "),
        TranscriptEntry(-5.0, -1.0, "before the start"),
        TranscriptEntry(1.0, 2.0, "keep me"),
    ])
    assert [entry.text for entry in entries] == ["keep me"]


def test_normalize_merges_repeated_held_lines():
    """Subtitle exports repeat a line held across a cue boundary."""
    entries = normalize.normalize_entries([
        TranscriptEntry(1.0, 3.0, "same line"),
        TranscriptEntry(3.0, 5.0, "same line"),
    ])
    assert len(entries) == 1
    assert entries[0].start == 1.0 and entries[0].end == 5.0


def test_normalize_keeps_repeats_that_are_far_apart():
    entries = normalize.normalize_entries([
        TranscriptEntry(1.0, 3.0, "same line"),
        TranscriptEntry(30.0, 32.0, "same line"),
    ])
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# Premiere word markers
# ---------------------------------------------------------------------------

def _marker(second, text, *, duration=0.4, speaker="", probability=1.0):
    return {
        "start": second * PREMIERE_TICKS_PER_SECOND,
        "duration": duration * PREMIERE_TICKS_PER_SECOND,
        "text": text,
        "speaker": speaker,
        "probability": probability,
    }


def test_group_word_markers_builds_lines():
    entries = group_word_markers([
        _marker(1.0, "we"), _marker(1.5, "found"), _marker(2.0, "diamonds."),
        _marker(5.0, "lets"), _marker(5.5, "go"),
    ])
    assert len(entries) == 2
    assert entries[0].text == "we found diamonds."
    assert entries[0].start == 1.0
    assert entries[1].text == "lets go"


def test_group_word_markers_breaks_on_a_long_pause():
    entries = group_word_markers(
        [_marker(0.0, "one"), _marker(4.0, "two")], max_gap=0.65
    )
    assert len(entries) == 2


def test_group_word_markers_breaks_on_speaker_change():
    entries = group_word_markers([
        _marker(0.0, "hello", speaker="Steve"),
        _marker(0.5, "there", speaker="Alex"),
    ])
    assert [entry.speaker for entry in entries] == ["Steve", "Alex"]


def test_group_word_markers_averages_confidence():
    entries = group_word_markers([
        _marker(0.0, "one", probability=0.6),
        _marker(0.5, "two", probability=1.0),
    ])
    assert entries[0].confidence == pytest.approx(0.8)


def test_group_word_markers_respects_a_seconds_scale():
    """Caption-track reads report seconds, not ticks; scale=1 must work."""
    entries = group_word_markers(
        [{"start": 3.0, "duration": 1.0, "text": "in seconds"}], scale=1.0
    )
    assert entries[0].start == 3.0 and entries[0].end == 4.0


def test_group_word_markers_ignores_empty_text():
    assert group_word_markers([_marker(0.0, "  ")]) == []


# ---------------------------------------------------------------------------
# Premiere source, through a fake bridge
# ---------------------------------------------------------------------------

def test_probe_support_when_premiere_is_closed(fake_bridge):
    support = probe_support(fake_bridge(connected=False))
    assert support.available is False
    assert support.readable is False
    assert "import a transcript file" in support.note


def test_probe_support_reports_what_the_host_measured(fake_bridge):
    bridge = fake_bridge({"transcript.caps": {
        "version": "25.1.0",
        "readable": True,
        "apis": {"getXMPMetadata": True, "getCaptionTracks": False},
        "note": "may be readable",
        "manual_export": "Text panel > Transcript",
    }})
    support = probe_support(bridge)
    assert support.available is True
    assert support.readable is True
    assert support.premiere_version == "25.1.0"
    assert support.apis["getXMPMetadata"] is True


def test_probe_support_handles_an_old_panel_without_the_op(fake_bridge):
    from premiere.errors import HostError

    bridge = fake_bridge()
    bridge.failures["transcript.caps"] = HostError("Unknown operation")
    support = probe_support(bridge)
    assert support.available is False
    assert "premiere.install" in support.note


def test_pull_returns_a_transcript_from_markers(fake_bridge, asset):
    bridge = fake_bridge({"transcript.read": {
        "found": True,
        "method": "xmp_speech_track",
        "path": asset.path,
        "scale": PREMIERE_TICKS_PER_SECOND,
        "markers": [_marker(1.0, "hello"), _marker(1.4, "world")],
        "checked": ["xmp_speech_track"],
    }})
    result = pull(asset, bridge=bridge)
    assert result.found is True
    assert result.transcript.source == "premiere"
    assert result.transcript.entries[0].text == "hello world"
    assert result.method == "xmp_speech_track"


def test_pull_reports_absence_without_inventing(fake_bridge, asset):
    bridge = fake_bridge({"transcript.read": {
        "found": False,
        "checked": ["xmp_speech_track"],
        "note": "No transcript data is reachable for this item by script.",
        "manual_export": "Text panel > Transcript tab > Export",
    }})
    result = pull(asset, bridge=bridge)
    assert result.found is False
    assert result.transcript is None
    assert result.checked == ["xmp_speech_track"]


def test_pull_with_markers_but_no_text_is_not_a_find(fake_bridge, asset):
    bridge = fake_bridge({"transcript.read": {
        "found": True, "method": "xmp_speech_track",
        "scale": PREMIERE_TICKS_PER_SECOND,
        "markers": [{"start": 0, "duration": 0, "text": ""}],
    }})
    assert pull(asset, bridge=bridge).found is False


def test_pull_when_premiere_is_closed(fake_bridge, asset):
    result = pull(asset, bridge=fake_bridge(connected=False))
    assert result.found is False
    assert "not reachable" in result.note


# ---------------------------------------------------------------------------
# Store and resolution
# ---------------------------------------------------------------------------

def test_import_file_normalises_and_stores(config, asset, tmp_path, srt_sample):
    path = tmp_path / "clip.srt"
    path.write_text(srt_sample, encoding="utf-8")

    transcript = store.import_file(config, asset, path)
    assert transcript.source == "srt"
    assert len(transcript) == 3

    reloaded, stale = store.load(config, asset.asset_id)
    assert reloaded is not None
    assert len(reloaded) == 3
    assert stale is False


def test_import_file_clamps_to_the_asset_duration(config, asset, tmp_path):
    path = tmp_path / "clip.srt"
    path.write_text(
        "1\n00:00:10,000 --> 00:00:40,000\npast the end\n", encoding="utf-8"
    )
    transcript = store.import_file(config, asset, path)   # asset is 16s long
    assert transcript.entries[0].end == 16.0


def test_load_flags_a_transcript_made_for_different_content(
    config, asset, tmp_path, srt_sample, media_file
):
    from editing.fingerprint import fingerprint

    path = tmp_path / "clip.srt"
    path.write_text(srt_sample, encoding="utf-8")
    store.import_file(config, asset, path, mark=fingerprint(media_file))

    media_file.write_bytes(b"completely different content")
    _, stale = store.load(config, asset.asset_id, mark=fingerprint(media_file))
    assert stale is True


def test_find_sidecar_prefers_srt_over_txt(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    (tmp_path / "clip.txt").write_text("[00:00:01] hi", encoding="utf-8")
    (tmp_path / "clip.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8"
    )
    assert store.find_sidecar(media).suffix == ".srt"


def test_find_sidecar_returns_none_when_absent(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    assert store.find_sidecar(media) is None


def test_resolve_uses_a_sidecar_when_premiere_has_nothing(
    config, asset, media_file, srt_sample
):
    (media_file.parent / f"{media_file.stem}.srt").write_text(
        srt_sample, encoding="utf-8"
    )
    resolution = store.resolve(config, asset, use_premiere=False)
    assert resolution.found is True
    assert resolution.origin.startswith("sidecar:")
    assert len(resolution.transcript) == 3


def test_resolve_reports_absence_with_the_manual_export_path(config, asset):
    resolution = store.resolve(config, asset, use_premiere=False)
    assert resolution.found is False
    assert resolution.source == "none"
    assert "Text panel" in resolution.note


def test_resolve_prefers_the_stored_copy_on_a_second_call(
    config, asset, media_file, srt_sample
):
    sidecar = media_file.parent / f"{media_file.stem}.srt"
    sidecar.write_text(srt_sample, encoding="utf-8")
    first = store.resolve(config, asset, use_premiere=False)
    assert first.origin.startswith("sidecar:")

    sidecar.unlink()   # the stored copy must survive the source going away
    second = store.resolve(config, asset, use_premiere=False)
    assert second.found is True
    assert second.origin == "stored"


def test_resolve_prefers_premiere_over_a_sidecar(
    config, asset, media_file, srt_sample, fake_bridge
):
    (media_file.parent / f"{media_file.stem}.srt").write_text(
        srt_sample, encoding="utf-8"
    )
    bridge = fake_bridge({"transcript.read": {
        "found": True, "method": "xmp_speech_track",
        "scale": PREMIERE_TICKS_PER_SECOND,
        "markers": [_marker(1.0, "from"), _marker(1.4, "premiere")],
    }})
    resolution = store.resolve(config, asset, use_premiere=True, bridge=bridge)
    assert resolution.source == "premiere"
    assert resolution.transcript.entries[0].text == "from premiere"

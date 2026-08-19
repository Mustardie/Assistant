"""Transcript acquisition and normalisation.

    Premiere Speech to Text ─┐
    exported .srt/.vtt/.txt ─┼─> normalise ─> Transcript ─> durable store
    any JSON/CSV transcript ─┘

``normalize`` is the pure parsing half, ``premiere_source`` reaches into
Premiere, and ``store`` decides which source to use and keeps the result.
"""
from editing.transcripts.normalize import (
    normalize_entries, parse_csv, parse_file, parse_json, parse_srt, parse_text,
    parse_txt, parse_vtt, sniff_format,
)
from editing.transcripts.premiere_source import (
    MANUAL_EXPORT_HINT, PullResult, TranscriptSupport, group_word_markers,
    probe_support, pull,
)
from editing.transcripts.store import (
    TranscriptResolution, find_sidecar, import_file, load, resolve, save,
)

__all__ = [
    "parse_srt", "parse_vtt", "parse_json", "parse_csv", "parse_txt",
    "parse_text", "parse_file", "sniff_format", "normalize_entries",
    "probe_support", "pull", "group_word_markers", "TranscriptSupport",
    "PullResult", "MANUAL_EXPORT_HINT",
    "resolve", "save", "load", "import_file", "find_sidecar",
    "TranscriptResolution",
]

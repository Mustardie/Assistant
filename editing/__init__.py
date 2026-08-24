"""Editing Brain V1 -- the structure layer.

    footage -> Premiere mapping -> transcript -> Qwen3-VL vision -> timeline

This package answers one question about a folder of raw footage: *what
actually happens in it, and what is being said while it happens?* It does not
edit anything. It produces a machine-readable structure timeline that a later
creative layer can plan cuts from.

Layout::

    config.py       paths, model selection, sampling parameters
    schema.py       every record type, with strict from_dict/to_dict
    fingerprint.py  stable identity for a file (path + mtime + size + hash)
    cache.py        content-addressed cache keyed on the fingerprint
    discovery.py    footage scan, ffprobe metadata, Premiere project mapping
    transcripts/    Premiere Speech-to-Text access + SRT/VTT/TXT/JSON import
    visual/         sampling plan, frame extraction, Qwen3-VL, event analysis
    align.py        transcript + visual events -> combined structure timeline
    recommend/      six layers of edit proposals + a safety pass
    roughcut/       selected ranges -> a validated scratch-sequence plan
    critic/         review frames -> Qwen3-VL critique -> one revision pass
    polish/         key-moment captions and restrained sound. Plans only
    reliability/    fifteen checks on whether a run produced a usable thing
    review/         one folder per run, with an index that reads top to bottom
    batch/          one configuration over every footage folder under a root
    pipeline.py     the orchestration each CLI command calls into
    cli.py          ``python -m editing.cli``

Design rules this layer holds to:

* Nothing is fabricated. If Premiere cannot give us a transcript, the layer
  says so and offers the manual export path rather than inventing entries.
* Every expensive step is cached on a fingerprint that includes the model and
  the sampling configuration, so re-running is cheap and changing either one
  correctly invalidates.
* The pure logic (sampling maths, normalisation, alignment) never touches the
  filesystem, ffmpeg, Premiere or a model, so it is all directly testable.
"""
from editing.align import build_segments, build_timeline, classify_alignment
from editing.cache import Cache, build_cache
from editing.config import EditingConfig, SamplingConfig, load_config
from editing.discovery import discover, find_media_files
from editing.errors import (
    EditingError, FootageError, ModelError, TranscriptError, VisualError,
)
from editing.fingerprint import asset_id_for, fingerprint
from editing.schema import (
    MediaAsset, StructureTimeline, TimelineSegment, Transcript,
    TranscriptEntry, VisualEvent,
)

__all__ = [
    "EditingConfig", "SamplingConfig", "load_config",
    "EditingError", "FootageError", "TranscriptError", "VisualError",
    "ModelError",
    "MediaAsset", "Transcript", "TranscriptEntry", "VisualEvent",
    "TimelineSegment", "StructureTimeline",
    "Cache", "build_cache", "fingerprint", "asset_id_for",
    "discover", "find_media_files",
    "build_timeline", "build_segments", "classify_alignment",
]

__version__ = "1.0.0"

"""Non-speech markers written into the transcript itself.

``[laughs]``, ``[music]``, ``[sighs]`` — when a transcriber or an ASR model
writes one of these, that is *far* stronger evidence than anything a loudness
curve can offer. Somebody (or something trained on speech) listened and named
the sound. So these events carry ``detection="transcript_marker"`` and a high
confidence, and the safety pass downstream weighs them accordingly.

This is why ``editing.transcripts.normalize`` deliberately keeps marker-only
cues instead of discarding them as non-speech: they are the best audio evidence
the pipeline ever gets for free.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

from editing.schema import (
    AUDIO_VALUE_FOR_TYPE, AudioEvent, TranscriptEntry, short_hash,
)

#: Marker text -> the audio event type it implies. Ordered most specific first
#: so "nervous laughter" matches laughter rather than falling through.
_MARKERS: tuple[tuple[str, str], ...] = (
    (r"laugh\w*", "possible_laughter"),
    (r"giggl\w*", "possible_laughter"),
    (r"chuckl\w*", "possible_laughter"),
    (r"lol", "possible_laughter"),
    (r"scream\w*", "possible_scream"),
    (r"shout\w*", "possible_scream"),
    (r"yell\w*", "possible_scream"),
    (r"gasp\w*", "possible_scream"),
    (r"music", "music_region"),
    (r"singing", "music_region"),
    (r"song", "music_region"),
    (r"applause", "loudness_spike"),
    (r"clapping", "loudness_spike"),
    (r"explosion", "loudness_spike"),
    (r"bang", "loudness_spike"),
    (r"sigh\w*", "low_energy"),
    (r"pause", "long_pause"),
    (r"silence", "silence"),
)

#: Bracketed or asterisked annotations: ``[laughs]``, ``(laughs)``, ``*laughs*``.
_ANNOTATION = re.compile(r"[\[(*]\s*([^\])*]{1,40}?)\s*[\])*]")

#: Confidence for a marker somebody actually wrote down. High, but not 1.0 --
#: ASR-generated markers are sometimes wrong, and a human transcript can carry
#: a stage direction that is not a sound.
MARKER_CONFIDENCE = 0.85


def marker_type(text: str) -> Optional[str]:
    """The audio event type a marker implies, or None if it is not a marker."""
    cleaned = str(text or "").strip().lower()
    if not cleaned:
        return None
    for pattern, kind in _MARKERS:
        if re.fullmatch(rf"\w*\s*{pattern}\w*", cleaned):
            return kind
    for pattern, kind in _MARKERS:
        if re.search(rf"\b{pattern}\b", cleaned):
            return kind
    return None


def find_annotations(text: str) -> list[tuple[str, str]]:
    """Every ``[annotation]`` in a line, paired with the type it implies.

    Annotations that name no known sound are dropped rather than guessed at --
    ``[crosstalk]`` and ``[00:12]`` are not audio events.
    """
    found: list[tuple[str, str]] = []
    for match in _ANNOTATION.finditer(str(text or "")):
        body = match.group(1).strip()
        kind = marker_type(body)
        if kind is not None:
            found.append((body, kind))
    return found


def detect_markers(
    entries: Sequence[TranscriptEntry],
    *,
    source_file: str = "",
    asset_id: str = "",
) -> list[AudioEvent]:
    """Build audio events from the markers written into a transcript.

    The event spans the cue that contains the marker. That is coarser than the
    sound itself, but it is honest: the transcript says the laughter happened
    somewhere in this line, and pretending to know exactly where inside it
    would be inventing precision.
    """
    events: list[AudioEvent] = []
    for entry in entries or []:
        for body, kind in find_annotations(entry.text):
            events.append(AudioEvent(
                event_id="au_" + short_hash(
                    asset_id or source_file, round(entry.start, 3),
                    round(entry.end, 3), kind, body,
                ),
                source_file=source_file,
                asset_id=asset_id,
                start=entry.start,
                end=max(entry.start, entry.end),
                type=kind,
                confidence=MARKER_CONFIDENCE,
                edit_value=AUDIO_VALUE_FOR_TYPE.get(kind, "unknown"),
                detection="transcript_marker",
                notes=f"transcript marker: [{body}]",
                evidence={"marker": body, "line": entry.text[:120]},
            ))
    events.sort(key=lambda event: (event.start, event.type))
    return events

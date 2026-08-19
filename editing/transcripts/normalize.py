"""Every transcript format in, one schema out.

The layer accepts SRT, WebVTT, CSV/TSV, JSON (several dialects) and timestamped
plain text, because those are what Premiere's Transcript panel exports and what
every speech-to-text tool emits. They all become a list of ``TranscriptEntry``
with real seconds on both ends.

One rule shapes the whole module: **timing is never invented**. A format that
carries no timestamps is rejected with an explanation, not spread evenly across
the runtime -- a fabricated timeline would align narration to the wrong visuals
and be impossible to spot downstream.

Everything here is pure text processing. No file is opened except by
``parse_file``, which is a thin reader in front of these functions.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Optional

from editing.errors import TranscriptError
from editing.schema import TranscriptEntry, parse_timecode

#: ``00:01:02,500``, ``00:01:02.500``, ``01:02.5`` and ``1:02``.
_TIMECODE = r"\d{1,3}:\d{1,2}(?::\d{1,2})?(?:[.,]\d{1,3})?"

_ARROW = re.compile(
    rf"(?P<start>{_TIMECODE})\s*(?:-->|->|—>|-|–|to)\s*(?P<end>{_TIMECODE})"
)
_BRACKETED = re.compile(rf"^[\[(<]\s*(?P<start>{_TIMECODE})\s*[\])>]\s*(?P<text>.*)$")
_LEADING_TIME = re.compile(rf"^(?P<start>{_TIMECODE})\s+(?P<text>\S.*)$")
#: Premiere's plain-text export puts the speaker and the time on their own line,
#: with the spoken text on the line(s) below.
_SPEAKER_TIME = re.compile(
    rf"^(?P<speaker>[^\t]{{1,60}}?)\s*[\t]+\s*(?P<start>{_TIMECODE})\s*$"
    rf"|^(?P<speaker2>[A-Za-z][\w .'-]{{0,40}}?)\s{{2,}}(?P<start2>{_TIMECODE})\s*$"
)
#: ``>> Alice: hello`` / ``Alice: hello`` at the head of a cue.
_INLINE_SPEAKER = re.compile(r"^\s*(?:>>\s*)?(?P<speaker>[A-Z][\w .'-]{0,30}):\s+(?P<text>\S.*)$")
_VTT_VOICE = re.compile(r"<v(?:\.[^ >]+)*\s+(?P<speaker>[^>]+)>(?P<text>.*?)(?:</v>)?$", re.S)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")

#: Cue-level noise that carries no information at all, and is dropped.
#:
#: Deliberately narrow. ``[laughs]``, ``[music]`` and ``[applause]`` are NOT
#: here: they are exactly the non-speech events the audio layer wants, and a
#: transcriber writing one is far stronger evidence than any loudness
#: heuristic. They survive normalisation so ``editing.audio.markers`` can find
#: them. Only genuinely contentless cues are discarded.
_NON_SPEECH = re.compile(
    r"^\s*[\[(](?:inaudible|unintelligible|indistinct|silence|noise|"
    r"background noise|no audio)[\])]\s*$",
    re.I,
)


def _seconds(text: str) -> float:
    value = parse_timecode(text)
    if value is None:
        raise TranscriptError(f"Unreadable timestamp: {text!r}")
    return value


def _clean(text: str) -> str:
    """Strip markup and collapse whitespace, keeping the words intact."""
    cleaned = _TAG.sub("", str(text or ""))
    cleaned = cleaned.replace("​", "").replace("&nbsp;", " ")
    cleaned = re.sub(r"&(amp|lt|gt|quot|#39);", lambda m: {
        "amp": "&", "lt": "<", "gt": ">", "quot": '"', "#39": "'",
    }[m.group(1)], cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


#: Words that open a sentence and are followed by a colon often enough to be
#: mistaken for a speaker label ("Okay: let's go"). Checked case-insensitively.
_NOT_SPEAKERS = frozenset({
    "okay", "ok", "so", "and", "but", "well", "now", "yeah", "yes", "no",
    "right", "alright", "anyway", "actually", "first", "second", "then",
    "wait", "oh", "look", "listen", "honestly", "basically", "obviously",
    "remember", "note", "warning", "tip", "step", "update", "edit",
})


def _split_speaker(text: str) -> tuple[str, str]:
    """Pull a leading ``Speaker:`` off a cue. Returns (speaker, text).

    Deliberately conservative. A false positive here invents a speaker that
    never existed and eats the first word of the line, so anything that could
    plausibly be ordinary prose is left alone: the label must be short, must
    look like a name (title case, all caps, or "Speaker 2"), and must not be
    one of the discourse words that habitually precede a colon.
    """
    match = _INLINE_SPEAKER.match(text)
    if not match:
        return "", text
    speaker = match.group("speaker").strip()
    if len(speaker.split()) > 3:
        return "", text
    if not (speaker.istitle() or speaker.isupper()):
        return "", text
    # "Speaker 1"/"SPEAKER 2" are always labels; a bare word might not be.
    words = [word.lower() for word in speaker.split()]
    if words[0] != "speaker" and any(word in _NOT_SPEAKERS for word in words):
        return "", text
    return speaker, match.group("text").strip()


# ---------------------------------------------------------------------------
# SRT / VTT
# ---------------------------------------------------------------------------

def parse_srt(text: str) -> list[TranscriptEntry]:
    """SubRip. Also parses WebVTT bodies, which share the cue layout."""
    entries: list[TranscriptEntry] = []
    body = text.lstrip("﻿")
    for block in re.split(r"\r?\n\s*\r?\n", body):
        entry = _parse_cue(block)
        if entry is not None:
            entries.append(entry)
    return entries


def parse_vtt(text: str) -> list[TranscriptEntry]:
    """WebVTT: the SRT cue parser plus header, NOTE and STYLE stripping."""
    body = text.lstrip("﻿")
    body = re.sub(r"^WEBVTT[^\n]*\n", "", body, count=1)
    # NOTE/STYLE/REGION blocks run until the next blank line.
    body = re.sub(r"(?m)^(?:NOTE|STYLE|REGION)\b.*?(?=\n\s*\n|\Z)", "", body, flags=re.S)
    return parse_srt(body)


def _parse_cue(block: str) -> Optional[TranscriptEntry]:
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return None

    timing_index = next(
        (i for i, line in enumerate(lines) if _ARROW.search(line)), None
    )
    if timing_index is None:
        return None

    timing = _ARROW.search(lines[timing_index])
    try:
        start = _seconds(timing.group("start"))
        end = _seconds(timing.group("end"))
    except TranscriptError:
        return None

    speaker = ""
    payload = "\n".join(lines[timing_index + 1:]).strip()
    if not payload:
        return None

    voice = _VTT_VOICE.search(payload)
    if voice:
        speaker = _clean(voice.group("speaker"))
        payload = voice.group("text")

    cleaned = _clean(payload)
    if not cleaned or _NON_SPEECH.match(cleaned):
        return None
    if not speaker:
        speaker, cleaned = _split_speaker(cleaned)
    if not cleaned:
        return None

    return TranscriptEntry(
        start=start, end=max(start, end), text=cleaned, speaker=speaker
    )


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

#: Keys that have held the cue list in transcripts seen in the wild.
_JSON_LIST_KEYS = (
    "entries", "segments", "results", "transcript", "transcripts", "cues",
    "items", "lines", "words", "data", "chunks", "utterances",
)


def parse_json(text: str) -> list[TranscriptEntry]:
    """Whisper, Deepgram, Adobe and hand-written JSON, without a dialect flag.

    Rather than branching on a format name that may be wrong, this walks the
    document for the first list whose members look like timed cues. That
    handles nesting (``{"results": {"channels": [{"alternatives": ...}]}}``)
    without hardcoding any one vendor's tree.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TranscriptError(
            "Transcript JSON could not be parsed",
            hint="Check the file is valid JSON (a trailing comma is the usual "
                 "culprit).",
            detail={"reason": str(exc)},
        ) from exc

    raw = _find_cue_list(document)
    if raw is None:
        raise TranscriptError(
            "No timed entries found in the JSON transcript",
            hint="Expected a list of objects with start/end (or start/duration) "
                 "and text, under one of: "
                 + ", ".join(_JSON_LIST_KEYS),
        )

    entries: list[TranscriptEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        entry = TranscriptEntry.from_dict(item)
        entry.text = _clean(entry.text)
        if not entry.text or _NON_SPEECH.match(entry.text):
            continue
        if not entry.speaker:
            entry.speaker, entry.text = _split_speaker(entry.text)
        entries.append(entry)
    return entries


def _looks_like_cues(value) -> bool:
    if not isinstance(value, list) or not value:
        return False
    timed = 0
    for item in value[:10]:
        if not isinstance(item, dict):
            return False
        has_text = any(k in item for k in ("text", "content", "value", "transcript"))
        has_time = any(
            k in item for k in ("start", "from", "begin", "startTime", "start_time")
        )
        if has_text and has_time:
            timed += 1
    return timed >= max(1, min(len(value), 10) // 2)


def _find_cue_list(document, depth: int = 0):
    """Breadth-first hunt for the cue list, preferring the named keys."""
    if depth > 8:
        return None
    if _looks_like_cues(document):
        return document
    if isinstance(document, dict):
        for key in _JSON_LIST_KEYS:
            if key in document:
                found = _find_cue_list(document[key], depth + 1)
                if found is not None:
                    return found
        for value in document.values():
            if isinstance(value, (dict, list)):
                found = _find_cue_list(value, depth + 1)
                if found is not None:
                    return found
    elif isinstance(document, list):
        for value in document:
            if isinstance(value, (dict, list)):
                found = _find_cue_list(value, depth + 1)
                if found is not None:
                    return found
    return None


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------

_CSV_START = ("start", "start time", "start_time", "in", "in point", "begin", "time")
_CSV_END = ("end", "end time", "end_time", "out", "out point", "finish")
_CSV_TEXT = ("text", "transcript", "content", "caption", "dialogue", "speech")
_CSV_SPEAKER = ("speaker", "speaker name", "name", "talent")


def parse_csv(text: str, *, delimiter: Optional[str] = None) -> list[TranscriptEntry]:
    """Premiere's Transcript panel CSV export, and anything shaped like it."""
    body = text.lstrip("﻿")
    if delimiter is None:
        head = body[:4096]
        delimiter = "\t" if head.count("\t") > head.count(",") else ","

    rows = list(csv.reader(io.StringIO(body), delimiter=delimiter))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return []

    header = [cell.strip().lower() for cell in rows[0]]
    columns = _csv_columns(header)
    if columns is None:
        raise TranscriptError(
            "CSV transcript has no recognisable start/text columns",
            hint="Expected a header row naming a start column (one of: "
                 f"{', '.join(_CSV_START)}) and a text column (one of: "
                 f"{', '.join(_CSV_TEXT)}).",
            detail={"header": header[:12]},
        )

    entries: list[TranscriptEntry] = []
    for row in rows[1:]:
        entry = _csv_row(row, columns)
        if entry is not None:
            entries.append(entry)
    return entries


def _csv_columns(header: list[str]) -> Optional[dict]:
    def find(names):
        for index, cell in enumerate(header):
            if cell in names:
                return index
        for index, cell in enumerate(header):
            if any(name in cell for name in names):
                return index
        return None

    start = find(_CSV_START)
    text = find(_CSV_TEXT)
    if start is None or text is None:
        return None
    return {
        "start": start,
        "end": find(_CSV_END),
        "text": text,
        "speaker": find(_CSV_SPEAKER),
    }


def _csv_row(row: list[str], columns: dict) -> Optional[TranscriptEntry]:
    def cell(key) -> str:
        index = columns.get(key)
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    text = _clean(cell("text"))
    if not text or _NON_SPEECH.match(text):
        return None
    try:
        start = _seconds(cell("start"))
    except TranscriptError:
        return None
    end_cell = cell("end")
    try:
        end = _seconds(end_cell) if end_cell else start
    except TranscriptError:
        end = start

    speaker = cell("speaker")
    if not speaker:
        speaker, text = _split_speaker(text)
    return TranscriptEntry(start=start, end=max(start, end), text=text, speaker=speaker)


# ---------------------------------------------------------------------------
# Plain text
# ---------------------------------------------------------------------------

def parse_txt(text: str) -> list[TranscriptEntry]:
    """Timestamped plain text, including Premiere's .txt transcript export.

    Recognises three shapes, which between them cover what the Transcript panel
    and common tools write::

        [00:01:02] spoken words
        00:01:02 - 00:01:06  spoken words
        Speaker 1   00:01:02
        spoken words on the following line(s)

    A file with no timestamps at all raises, because the alternative is
    inventing a timeline.
    """
    entries: list[TranscriptEntry] = []
    pending: Optional[dict] = None
    saw_any_time = False

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        body = _clean(" ".join(pending["lines"]))
        if body and not _NON_SPEECH.match(body):
            speaker = pending["speaker"]
            if not speaker:
                speaker, body = _split_speaker(body)
            entries.append(TranscriptEntry(
                start=pending["start"],
                end=max(pending["start"], pending["end"]),
                text=body,
                speaker=speaker,
            ))
        pending = None

    for line in text.lstrip("﻿").splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # "Speaker 1<tab>00:01:02" -- a header for the lines that follow.
        header = _SPEAKER_TIME.match(stripped)
        if header:
            saw_any_time = True
            flush()
            speaker = (header.group("speaker") or header.group("speaker2") or "").strip()
            start = _seconds(header.group("start") or header.group("start2"))
            pending = {"start": start, "end": start, "speaker": speaker, "lines": []}
            continue

        # "00:01:02 --> 00:01:06  words" (words optional, may follow below).
        ranged = _ARROW.search(stripped)
        if ranged:
            saw_any_time = True
            flush()
            start = _seconds(ranged.group("start"))
            end = _seconds(ranged.group("end"))
            rest = stripped[ranged.end():].strip(" \t-:")
            pending = {"start": start, "end": end, "speaker": "",
                       "lines": [rest] if rest else []}
            continue

        # "[00:01:02] words" or "00:01:02 words".
        single = _BRACKETED.match(stripped) or _LEADING_TIME.match(stripped)
        if single:
            saw_any_time = True
            flush()
            start = _seconds(single.group("start"))
            body = single.group("text").strip()
            pending = {"start": start, "end": start, "speaker": "",
                       "lines": [body] if body else []}
            continue

        if pending is not None:
            pending["lines"].append(stripped)

    flush()

    if not saw_any_time:
        raise TranscriptError(
            "The text transcript has no timestamps",
            hint="Export from Premiere's Text panel with timecode included, or "
                 "use the .srt / .vtt / .csv export instead. Untimed text "
                 "cannot be aligned to footage and will not be guessed at.",
        )
    return entries


# ---------------------------------------------------------------------------
# Normalisation and dispatch
# ---------------------------------------------------------------------------

def normalize_entries(
    entries: list[TranscriptEntry],
    *,
    default_gap: float = 2.0,
    max_duration: Optional[float] = None,
) -> list[TranscriptEntry]:
    """Put a parsed cue list into a state alignment can rely on.

    Sorts by start; gives zero-length cues an end (the next cue's start, capped
    at ``default_gap``, which is what a point-timestamp format actually means);
    clamps to the media duration when known; and merges cues that are exact
    duplicates at the same time, which subtitle exports produce when a line is
    held across a cue boundary.
    """
    usable = [
        entry for entry in entries
        if entry.text.strip() and entry.start >= 0.0
    ]
    usable.sort(key=lambda entry: (entry.start, entry.end))

    for index, entry in enumerate(usable):
        if entry.end <= entry.start:
            following = usable[index + 1].start if index + 1 < len(usable) else None
            if following is not None and following > entry.start:
                entry.end = min(following, entry.start + default_gap)
            else:
                entry.end = entry.start + default_gap
        if max_duration and max_duration > 0:
            entry.start = min(entry.start, max_duration)
            entry.end = min(entry.end, max_duration)

    merged: list[TranscriptEntry] = []
    for entry in usable:
        if entry.end <= entry.start:
            continue
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.text == entry.text
            and previous.speaker == entry.speaker
            and entry.start <= previous.end + 0.05
        ):
            previous.end = max(previous.end, entry.end)
            continue
        merged.append(entry)
    return merged


#: Extension -> parser. ``.txt`` is last-resort for anything unrecognised.
PARSERS = {
    ".srt": parse_srt,
    ".vtt": parse_vtt,
    ".webvtt": parse_vtt,
    ".json": parse_json,
    ".csv": parse_csv,
    ".tsv": parse_csv,
    ".txt": parse_txt,
    ".sbv": parse_srt,
}

#: Extension -> the ``Transcript.source`` value it produces.
SOURCE_FOR_SUFFIX = {
    ".srt": "srt", ".sbv": "srt",
    ".vtt": "vtt", ".webvtt": "vtt",
    ".json": "json",
    ".csv": "csv", ".tsv": "csv",
    ".txt": "txt",
}


def sniff_format(text: str, suffix: str = "") -> str:
    """Guess the format from the content when the extension is missing or lying.

    Users rename files. A ``.txt`` holding WebVTT should parse as WebVTT rather
    than fail, so content wins over extension when the content is unambiguous.
    """
    head = text.lstrip("﻿").lstrip()[:2000]
    if head.upper().startswith("WEBVTT"):
        return ".vtt"
    # A JSON array opens with an object or a string. ``[00:00:03] words`` is a
    # timestamped text line, not JSON, and must not be routed to the parser
    # that would reject the whole file.
    if head.startswith("{") or re.match(r"^\[\s*[{\"\[]", head):
        return ".json"
    if _ARROW.search(head) and re.search(r"(?m)^\s*\d+\s*$", head):
        return ".srt"
    if _ARROW.search(head):
        return ".vtt" if "." in head.split("\n")[0] else ".srt"
    suffix = (suffix or "").lower()
    if suffix in PARSERS:
        return suffix
    first_line = head.splitlines()[0] if head.splitlines() else ""
    if first_line.count(",") >= 2 or first_line.count("\t") >= 2:
        return ".csv"
    return ".txt"


def parse_text(text: str, *, suffix: str = "") -> tuple[list[TranscriptEntry], str]:
    """Parse transcript text, returning (entries, source-name)."""
    resolved = sniff_format(text, suffix)
    parser = PARSERS.get(resolved, parse_txt)
    entries = parser(text)
    return entries, SOURCE_FOR_SUFFIX.get(resolved, "unknown")


def parse_file(path: str | Path) -> tuple[list[TranscriptEntry], str]:
    """Read and parse a transcript file from disk."""
    target = Path(path).expanduser()
    if not target.exists():
        raise TranscriptError(
            f"Transcript file not found: {target}",
            hint="Export it from Premiere's Text panel (Transcript tab -> "
                 "Export -> Text/SRT) and pass the saved path.",
        )
    try:
        # utf-8-sig eats the BOM Windows tools add; errors=replace keeps a
        # stray cp1252 character from failing the whole import.
        raw = target.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise TranscriptError(
            f"Could not read {target}", detail={"reason": str(exc)}
        ) from exc

    if not raw.strip():
        raise TranscriptError(f"Transcript file is empty: {target}")

    entries, source = parse_text(raw, suffix=target.suffix.lower())
    if not entries:
        raise TranscriptError(
            f"No usable transcript entries in {target.name}",
            hint="The file parsed but contained no timed lines with text.",
            detail={"detected_format": source},
        )
    return entries, source

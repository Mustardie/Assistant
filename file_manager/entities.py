"""
Entity extraction for file manager natural language queries.

Pulls the *what/where* (filename, extension, folder, drive, date modifier,
size modifier, category, app/game name) out of a raw utterance. This module
does not decide what the user wants done with those entities -- that's
`intent.py`. Keeping these separate is what lets us fix "open my assistant
folder" without touching how "rename X to Y" is parsed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

EXTENSION_PATTERN = re.compile(
    r"\b[\w\-]+\.(pdf|docx?|xlsx?|pptx?|txt|md|csv|json|py|png|jpe?g|gif|webp|bmp|"
    r"mp4|mov|avi|mkv|webm|mp3|wav|flac|zip|rar|7z|tar|gz|exe|lnk)\b",
    re.IGNORECASE,
)
BARE_EXTENSION_PATTERN = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|txt|md|csv|json|py|png|jpe?g|gif|webp|bmp|"
    r"mp4|mov|avi|mkv|webm|mp3|wav|flac|zip|rar|7z|tar|gz|exe|lnk)\b",
    re.IGNORECASE,
)
QUOTED_PATTERN = re.compile(r"[\"'\u201c\u2018]([^\"'\u201d\u2019]+)[\"'\u201d\u2019]")
DRIVE_PATTERN = re.compile(r"\bon\s+([a-z])\s*(?::|drive)\b|\b([a-z])\s*:\s*(?:drive)?\b|\b([a-z])\s+drive\b", re.IGNORECASE)
SIZE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb)\b", re.IGNORECASE)

CATEGORY_EXTENSIONS = {
    "document": {".pdf", ".doc", ".docx", ".odt", ".txt", ".md", ".rtf"},
    "spreadsheet": {".xls", ".xlsx", ".csv"},
    "presentation": {".ppt", ".pptx"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "code": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".cs", ".html", ".css"},
    "executable": {".exe", ".lnk", ".msi", ".bat"},
}

CATEGORY_KEYWORDS = {
    "document": {"document", "doc", "worksheet", "assignment", "notes", "report", "letter", "pdf"},
    "spreadsheet": {"spreadsheet", "sheet", "excel"},
    "presentation": {"presentation", "ppt", "powerpoint", "slides", "slideshow"},
    "image": {"image", "photo", "picture", "pic", "wallpaper", "screenshot"},
    "video": {"video", "clip", "movie", "recording", "footage"},
    "audio": {"audio", "music", "song", "track"},
    "archive": {"archive", "zip", "rar", "compressed"},
    "code": {"code", "script", "python"},
}

# Order matters: longer/more specific phrases first so "recently created"
# doesn't get shadowed by a bare "recent" match.
DATE_MODIFIERS = [
    ("recently created", "created_latest"),
    ("newly created", "created_latest"),
    ("recently modified", "latest"),
    ("last modified", "latest"),
    ("most recent", "latest"),
    ("latest", "latest"),
    ("newest", "latest"),
    ("recent", "latest"),
    ("oldest", "oldest"),
]

SIZE_MODIFIERS = [
    ("biggest", "largest"),
    ("largest", "largest"),
    ("smallest", "smallest"),
    ("tiny", "smallest"),
]

STOPWORDS = {
    "open", "find", "search", "show", "list", "display", "launch", "look", "for",
    "my", "the", "a", "an", "please", "where", "is", "are", "it", "that", "this",
    "in", "on", "to", "of", "and", "or", "with", "from", "file", "files", "folder",
    "folders", "directory", "up", "me",
}

# Extension names that can appear as bare words in a query (e.g. the "pdf" in
# "delete asdkfjasldkfj.pdf" or just "find a pdf"). These are excluded from
# the generic keyword list because they're already captured properly via
# `entities.extension` / ranking's extension_match weight -- if they're also
# left in `keywords`, the ranker's whole-word matcher scores every file of
# that extension against them, since a file's own extension is always a
# token in its filename (e.g. "chemistry_homework.pdf" tokenizes to include
# "pdf"). That falsely turns "no match" queries into "clarify against every
# file of that type" ones.
EXTENSION_WORDS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv",
    "json", "py", "png", "jpg", "jpeg", "gif", "webp", "bmp", "mp4", "mov",
    "avi", "mkv", "webm", "mp3", "wav", "flac", "zip", "rar", "7z", "tar",
    "gz", "exe", "lnk",
}


@dataclass
class ExtractedEntities:
    raw_query: str
    filename: Optional[str] = None
    extension: Optional[str] = None
    folder_name: Optional[str] = None
    drive: Optional[str] = None
    category: Optional[str] = None
    date_modifier: Optional[str] = None
    size_modifier: Optional[str] = None
    min_size_bytes: Optional[int] = None
    keywords: List[str] = field(default_factory=list)
    app_or_game_name: Optional[str] = None
    wants_folder: bool = False


class EntityExtractor:
    """Pulls structured entities out of a raw natural-language file request."""

    def extract(self, query: str) -> ExtractedEntities:
        text = (query or "").strip()
        lowered = text.lower()
        entities = ExtractedEntities(raw_query=text)

        quoted = QUOTED_PATTERN.search(text)
        if quoted:
            entities.filename = quoted.group(1).strip()

        ext_match = EXTENSION_PATTERN.search(lowered)
        if ext_match:
            entities.filename = entities.filename or ext_match.group(0)
            entities.extension = "." + ext_match.group(1).lower()
        else:
            bare_ext = BARE_EXTENSION_PATTERN.search(lowered)
            if bare_ext:
                entities.extension = "." + bare_ext.group(1).lower()

        drive_match = DRIVE_PATTERN.search(lowered)
        if drive_match:
            letter = next(g for g in drive_match.groups() if g)
            entities.drive = letter.lower()

        for phrase, modifier in DATE_MODIFIERS:
            if phrase in lowered:
                entities.date_modifier = modifier
                break

        for phrase, modifier in SIZE_MODIFIERS:
            if phrase in lowered:
                entities.size_modifier = modifier
                break

        size_match = SIZE_PATTERN.search(lowered)
        if size_match:
            value = float(size_match.group(1))
            unit = size_match.group(2).lower()
            multiplier = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}[unit]
            entities.min_size_bytes = int(value * multiplier)

        for category, exts in CATEGORY_EXTENSIONS.items():
            if entities.extension and entities.extension in exts:
                entities.category = category
                break
        if not entities.category:
            for category, kws in CATEGORY_KEYWORDS.items():
                if any(kw in lowered for kw in kws):
                    entities.category = category
                    break

        if "folder" in lowered or "directory" in lowered:
            entities.wants_folder = True

        entities.folder_name = self._extract_folder_name(lowered)
        entities.app_or_game_name = self._extract_app_name(lowered)
        entities.keywords = self._extract_keywords(lowered)

        return entities

    def _extract_folder_name(self, lowered: str) -> Optional[str]:
        match = re.search(r"\b(?:my|the)\s+([\w\- ]+?)\s+folder\b", lowered)
        if match:
            return match.group(1).strip()
        match = re.search(r"\bfolder\s+(?:called|named)\s+([\w\- ]+)", lowered)
        if match:
            return match.group(1).strip()
        return None

    def _extract_app_name(self, lowered: str) -> Optional[str]:
        match = re.search(r"\bwhere\s+is\s+([\w\- ]+?)\s+installed\b", lowered)
        if match:
            return match.group(1).strip()
        match = re.search(r"\bis\s+([\w\- ]+?)\s+installed\b", lowered)
        if match:
            return match.group(1).strip()
        match = re.search(r"\binstall(?:ation)?\s+(?:folder|directory|path)\s+(?:of|for)\s+([\w\- ]+)", lowered)
        if match:
            return match.group(1).strip()
        return None

    def _extract_keywords(self, lowered: str) -> List[str]:
        cleaned = re.sub(r"[^\w\s]", " ", lowered)
        modifier_words = set()
        for phrase, _ in DATE_MODIFIERS + SIZE_MODIFIERS:
            modifier_words.update(phrase.split())
        words = [
            w for w in cleaned.split()
            if w not in STOPWORDS and w not in modifier_words
            and w not in EXTENSION_WORDS and len(w) > 2
        ]
        return list(dict.fromkeys(words))
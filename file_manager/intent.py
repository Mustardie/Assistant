"""
Intent classification for file manager natural-language requests.

Deliberately separate from entity extraction: this module only decides
*what the user wants done* (open a file, open a folder, rename, move,
find a game install, etc). What/where the target is comes from
`entities.EntityExtractor`. Mixing the two together in one giant
if/elif chain (as the old indexer did) is what caused folder requests
to get treated as file requests and vice versa.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .entities import EntityExtractor, ExtractedEntities


class Intent(str, Enum):
    OPEN_FILE = "open_file"
    OPEN_FOLDER = "open_folder"
    FIND_INSTALLATION = "find_installation"
    RENAME = "rename"
    MOVE = "move"
    COPY = "copy"
    DELETE = "delete"
    RESTORE = "restore"
    EXTRACT_ARCHIVE = "extract_archive"
    COMPRESS_ARCHIVE = "compress_archive"
    CREATE_FOLDER = "create_folder"
    CREATE_FILE = "create_file"
    REVEAL_IN_EXPLORER = "reveal_in_explorer"
    SHOW_PROPERTIES = "show_properties"
    LIST_FOLDER = "list_folder"
    SEARCH = "search"


# Checked in this order -- first match wins. Multi-word phrases before
# single words so e.g. "reveal in explorer" doesn't fall through and get
# classified by some other keyword first.
ACTION_KEYWORDS = [
    (Intent.CREATE_FOLDER, ["create folder", "make folder", "new folder"]),
    (Intent.CREATE_FILE, ["create file", "make file", "new file"]),
    (Intent.EXTRACT_ARCHIVE, ["extract", "unzip", "decompress", "unpack"]),
    (Intent.COMPRESS_ARCHIVE, ["compress", "zip up", "make a zip", "create a zip", "zip this", "zip that"]),
    (Intent.REVEAL_IN_EXPLORER, ["reveal in explorer", "show in explorer", "show in folder", "open containing folder", "reveal in file explorer"]),
    (Intent.SHOW_PROPERTIES, ["properties", "file info", "how big is", "file size of"]),
    (Intent.RESTORE, ["restore", "undelete", "recover from trash", "recover from recycle"]),
    (Intent.RENAME, ["rename"]),
    (Intent.MOVE, ["move"]),
    (Intent.COPY, ["copy", "duplicate"]),
    (Intent.DELETE, ["delete", "remove", "trash it", "get rid of"]),
    (Intent.FIND_INSTALLATION, ["installed", "installation", "install folder", "install path", "game folder", "program folder", "where is"]),
]

OPEN_KEYWORDS = ["open", "show", "display", "launch", "pull up", "bring up"]
LIST_KEYWORDS = ["list", "what's in", "whats in", "contents of"]

GAME_APP_HINTS = {
    "steam", "epic", "gog", "ubisoft", "xbox", "game", "app", "program",
    "cyberpunk", "minecraft", "fortnite", "valorant", "league of legends",
}


@dataclass
class ParsedRequest:
    intent: Intent
    entities: ExtractedEntities
    target_is_folder: bool
    confidence_hint: float  # 0-1, classifier's own confidence, independent of ranking


class IntentClassifier:
    def __init__(self, entity_extractor: Optional[EntityExtractor] = None):
        self.entity_extractor = entity_extractor or EntityExtractor()

    def classify(self, query: str) -> ParsedRequest:
        text = (query or "").lower().strip()
        entities = self.entity_extractor.extract(query)

        for intent, phrases in ACTION_KEYWORDS:
            if any(phrase in text for phrase in phrases):
                target_is_folder = intent == Intent.CREATE_FOLDER or (
                    intent == Intent.FIND_INSTALLATION and self._looks_like_game_or_app(text)
                )
                return ParsedRequest(intent, entities, target_is_folder, 0.85)

        wants_folder = entities.wants_folder or bool(entities.folder_name)

        if any(kw in text for kw in LIST_KEYWORDS):
            return ParsedRequest(Intent.LIST_FOLDER, entities, True, 0.8)

        if any(kw in text for kw in OPEN_KEYWORDS):
            if wants_folder:
                return ParsedRequest(Intent.OPEN_FOLDER, entities, True, 0.8)
            return ParsedRequest(Intent.OPEN_FILE, entities, False, 0.75)

        if wants_folder:
            return ParsedRequest(Intent.OPEN_FOLDER, entities, True, 0.6)

        return ParsedRequest(Intent.SEARCH, entities, False, 0.5)

    def _looks_like_game_or_app(self, text: str) -> bool:
        return any(hint in text for hint in GAME_APP_HINTS)

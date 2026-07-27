"""
Weighted ranking engine for file/folder search results.

Scoring tiers, highest weight to lowest:
  1. Exact filename / folder-name match
  2. Whole-word keyword matches in the name
  3. Fuzzy / partial text matches (name, path)
  4. Metadata matches (extension/category, drive, size)
  5. Recency (modified/created time) -- tiebreaker only, never dominant

Cache/build/temp-style folders are penalized heavily so they can never
outrank a real user file or a genuine install directory, no matter how
many keyword hits they happen to pick up.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List

from .entities import CATEGORY_EXTENSIONS
from .intent import Intent, ParsedRequest

NOISE_PATH_MARKERS = [
    ".gradle", "node_modules", "__pycache__", ".git", ".venv", "venv",
    "site-packages", "dist-packages", ".tox", "appdata\\local\\temp",
    "appdata/local/temp", "\\temp\\", "/temp/", "$recycle.bin",
    "system volume information", "\\build\\", "/build/", "\\dist\\", "/dist/",
    ".cache", "\\cache\\", "/cache/", "\\bin\\obj\\", "/bin/obj/",
    "target\\debug", "target/debug", "target\\release", "target/release",
]

# Signals that a folder is save data / config / logs / benchmarks rather
# than an actual game or program installation.
NON_INSTALL_MARKERS = [
    "documents", "appdata", "saved games", "benchmarkresults", "logs",
    "\\cache\\", "/cache/", "\\temp\\", "/temp/", "screenshots", "config",
    "\\save\\", "/save/",
]

INSTALL_MARKERS = [
    "steamapps\\common", "steamapps/common", "program files", "program files (x86)",
    "epic games", "gog galaxy", "ubisoft game launcher\\games",
    "xboxgames", "windowsapps",
]

WEIGHTS = {
    "exact_name": 100.0,
    "whole_word": 45.0,
    "fuzzy_high": 25.0,
    "fuzzy_low": 10.0,
    "extension_match": 35.0,
    "extension_mismatch": -55.0,
    "category_match": 30.0,
    "category_mismatch": -55.0,
    "drive_mismatch": -150.0,
    "noise_penalty": -100.0,
    "non_install_penalty": -65.0,
    "install_bonus": 55.0,
    "folder_intent_is_dir": 55.0,
    "folder_intent_is_file": -60.0,
    "file_intent_is_dir": -50.0,
    "recency_bonus_max": 8.0,
}


@dataclass
class ScoredResult:
    record: Dict[str, Any]
    score: float
    reasons: List[str]


class Ranker:
    def score(self, record: Dict[str, Any], parsed: ParsedRequest) -> ScoredResult:
        reasons: List[str] = []
        score = 0.0
        entities = parsed.entities

        path = str(record.get("path") or "")
        path_lower = path.lower()
        filename = str(record.get("filename") or record.get("name") or "")
        filename_lower = filename.lower()
        stem = os.path.splitext(filename_lower)[0]
        is_dir = bool(record.get("is_dir"))
        extension = (record.get("extension") or "").lower()

        # Tier 1: exact match
        target_name = (entities.filename or entities.folder_name or "").lower().strip()
        if target_name and (target_name == filename_lower or target_name == stem):
            score += WEIGHTS["exact_name"]
            reasons.append("exact name match")

        # Tier 2: whole-word keyword matches
        name_words = set(re.findall(r"[a-z0-9]+", filename_lower))
        for keyword in entities.keywords:
            if keyword in name_words:
                score += WEIGHTS["whole_word"]
                reasons.append(f"whole word '{keyword}' in name")

        # Tier 3: fuzzy matches (only matter once tiers 1-2 have had their say)
        if entities.keywords:
            joined_keywords = " ".join(entities.keywords)
            ratio = SequenceMatcher(None, joined_keywords, stem).ratio()
            if ratio >= 0.6:
                score += WEIGHTS["fuzzy_high"] * ratio
                reasons.append(f"fuzzy match ({ratio:.2f})")
            elif ratio >= 0.3:
                score += WEIGHTS["fuzzy_low"] * ratio
            for keyword in entities.keywords:
                if keyword in path_lower and keyword not in name_words:
                    score += 4.0

        # Tier 4: metadata
        if entities.extension:
            if extension == entities.extension:
                score += WEIGHTS["extension_match"]
            else:
                score += WEIGHTS["extension_mismatch"]

        if entities.category:
            allowed = CATEGORY_EXTENSIONS.get(entities.category, set())
            if allowed:
                if extension in allowed:
                    score += WEIGHTS["category_match"]
                elif not is_dir:
                    score += WEIGHTS["category_mismatch"]

        if entities.drive:
            record_drive = path.split(":")[0].lower() if ":" in path else ""
            if record_drive and record_drive != entities.drive:
                score += WEIGHTS["drive_mismatch"]
                reasons.append("wrong drive")

        if entities.min_size_bytes and not is_dir:
            size = record.get("size") or 0
            if size < entities.min_size_bytes:
                score -= 20.0

        # Folder vs file intent
        if parsed.target_is_folder:
            score += WEIGHTS["folder_intent_is_dir"] if is_dir else WEIGHTS["folder_intent_is_file"]
        elif parsed.intent in {Intent.OPEN_FILE, Intent.SEARCH} and not entities.wants_folder:
            if is_dir:
                score += WEIGHTS["file_intent_is_dir"]

        # Installation lookup (used mainly as a fallback -- primary path is
        # GameInstallationLocator in installations.py, which checks the
        # registry/Steam directly instead of relying on the file index)
        if parsed.intent == Intent.FIND_INSTALLATION:
            if any(marker in path_lower for marker in INSTALL_MARKERS):
                score += WEIGHTS["install_bonus"]
                reasons.append("install path marker")
            if any(marker in path_lower for marker in NON_INSTALL_MARKERS):
                score += WEIGHTS["non_install_penalty"]
                reasons.append("non-install marker (config/save/log)")
            if extension in {".exe", ".lnk"}:
                score += 30.0

        # Noise penalty applies regardless of intent
        if any(marker in path_lower for marker in NOISE_PATH_MARKERS):
            score += WEIGHTS["noise_penalty"]
            reasons.append("cache/build folder penalty")

        # Tier 5: recency, tiebreaker only
        if entities.date_modifier in {"latest", "created_latest"} and record.get("modified_time"):
            score += WEIGHTS["recency_bonus_max"]

        return ScoredResult(record=record, score=round(score, 2), reasons=reasons)

    def rank(self, records: List[Dict[str, Any]], parsed: ParsedRequest) -> List[ScoredResult]:
        scored = [self.score(record, parsed) for record in records]
        scored = [s for s in scored if s.score > 0]
        scored.sort(key=lambda s: s.score, reverse=True)

        if parsed.entities.date_modifier == "oldest":
            scored.sort(key=lambda s: s.record.get("modified_time") or 0)
        elif parsed.entities.size_modifier == "largest":
            scored.sort(key=lambda s: s.record.get("size") or 0, reverse=True)
        elif parsed.entities.size_modifier == "smallest":
            scored.sort(key=lambda s: s.record.get("size") or 0)

        return scored

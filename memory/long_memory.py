import json
import logging
import re
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Optional

from config.settings import settings
from llm.gemini_client import GeminiClient
from llm.ollama_client import OllamaClient
from llm.openrouter_client import OpenRouterClient, OpenRouterConfigurationError

logger = logging.getLogger(__name__)

MIN_IMPORTANCE_TO_SAVE = 4
DECAY_THRESHOLD = 2.0
DECAY_MAX_AGE_DAYS = 60
PROFILE_MAX_ITEMS = 5
RELEVANT_MAX_ITEMS = 5

DEFAULT_LONG_MEMORY = {
    "onboarded": False,
    "communication_style": {
        "tone": "",
        "length": "",
        "preferences": [],
    },
    "interests": [],
    "dislikes": [],
    "goals": [],
    "facts_about_user": [],
}

_MEMORY_EXTRACTION_PROMPT = """
You extract structured long-term memory from what a user tells an AI assistant
about themselves.

Given the user's text, return ONLY valid JSON in exactly this shape:

{
  "communication_style": {"tone": "", "length": "", "preferences": []},
  "interests": [],
  "dislikes": [],
  "goals": [],
  "facts_about_user": []
}

Rules:
- Put each distinct piece of information in exactly one category -- do not
  duplicate the same item across categories.
- "tone" and "length" are single short strings (e.g. "casual", "concise").
  Leave them as empty strings if not mentioned.
- Every other field is a list of short, self-contained strings, lightly
  cleaned up from the user's own words -- not full sentences copied verbatim
  if avoidable.
- Do not invent anything the user did not say. Leave lists empty rather than
  guessing.
- Output ONLY the JSON object. No markdown, no commentary.
"""


class MemoryItem:
    __slots__ = ("id", "text", "category", "importance", "created_at", "updated_at", "access_count", "last_accessed")

    def __init__(self, text: str, category: str = "fact", importance: int = 6):
        self.id = str(uuid.uuid4())
        self.text = text.strip()
        self.category = category
        self.importance = max(0, min(10, importance))
        now = time.time()
        self.created_at = now
        self.updated_at = now
        self.access_count = 0
        self.last_accessed = now

    def score(self) -> float:
        now = time.time()
        days_since_access = (now - self.last_accessed) / 86400
        importance_weight = self.importance * 0.5
        recency_weight = max(0.0, 1.0 - (days_since_access / 30.0)) * 0.3
        frequency_weight = min(self.access_count / 5.0, 1.0) * 0.2
        return importance_weight + recency_weight + frequency_weight

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d: dict):
        item = cls.__new__(cls)
        for s in cls.__slots__:
            setattr(item, s, d.get(s, "" if s in ("id", "text", "category") else 0))
        return item


_IMPORTANCE_LADDER = [
    (8, ["always", "never", "critical", "must", "require", "essential", "important", "crucial"]),
    (7, ["prefer", "love", "hate", "project", "building", "working on", "developing"]),
    (6, ["like", "dislike", "use", "want", "need", "goal", "learning", "studying"]),
    (5, ["sometimes", "occasionally", "might", "could", "maybe"]),
]


def _estimate_importance(text: str, category: str) -> int:
    if category in ("project", "skill", "goal"):
        return 7
    text_lower = text.lower()
    for score, keywords in _IMPORTANCE_LADDER:
        if any(kw in text_lower for kw in keywords):
            return score
    if category == "preference":
        return 6
    return 5


def _semantic_overlap(a: str, b: str) -> float:
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    common = words_a & words_b
    smaller = min(len(words_a), len(words_b))
    return len(common) / smaller if smaller else 0.0


class LongTermMemory:
    def __init__(self, storage_path: Path | str | None = None):
        self.storage_path = Path(storage_path) if storage_path else self._default_path()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._scored_items: list[MemoryItem] = []
        self._legacy_memory = deepcopy(DEFAULT_LONG_MEMORY)
        self._llm_client = None
        self._load()

    # ------------------------------------------------------------------ #
    # Public API (backward-compatible)
    # ------------------------------------------------------------------ #

    def exists(self) -> bool:
        return self.storage_path.exists()

    def is_onboarded(self) -> bool:
        return bool(self._legacy_memory.get("onboarded"))

    def is_empty(self) -> bool:
        return len(self._scored_items) == 0

    def load_memory(self) -> dict:
        return deepcopy(self._legacy_memory)

    def save_memory(self) -> None:
        self._save()

    def to_dict(self) -> dict:
        return self._build_profile()

    def create_memory(self, raw_text: str = "") -> None:
        self._legacy_memory = deepcopy(DEFAULT_LONG_MEMORY)
        raw_text = str(raw_text).strip()
        if raw_text:
            extracted = self._classify_with_llm(raw_text)
            if extracted is not None:
                self._merge_llm_extraction(extracted)
                self._add_extracted_as_items(extracted)
            else:
                for clause in self._split_into_clauses(raw_text):
                    category, text = self._simple_classify(clause)
                    self._add_scored(text, category)
                    self._classify_and_store(clause)
        self._legacy_memory["onboarded"] = True
        self._save()

    def update_memory(self, category: str, value: str) -> None:
        value = str(value).strip()
        if not value:
            return
        self._add_scored(value, category if category in ("preference", "fact", "project", "skill", "goal", "pattern") else "fact")
        self._append_legacy(category, value)

    def remember(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        extracted = self._classify_with_llm(text)
        if extracted is not None and self._extraction_has_content(extracted):
            self._merge_llm_extraction(extracted)
            self._add_extracted_as_items(extracted)
        else:
            category, clean = self._simple_classify(text)
            self._add_scored(clean, category)
            self._classify_and_store(text)
        self._save()

    def forget(self, text: str) -> bool:
        text = str(text).strip()
        if not text:
            return False
        lowered = text.lower()
        before = len(self._scored_items)
        self._scored_items = [item for item in self._scored_items if lowered not in item.text.lower()]
        removed = len(self._scored_items) != before
        if removed:
            self._save()
        legacy_removed = self._forget_legacy(text)
        return removed or legacy_removed

    def count(self) -> int:
        return len(self._scored_items)

    # ------------------------------------------------------------------ #
    # New: query-aware retrieval
    # ------------------------------------------------------------------ #

    def get_relevant(self, query: str = "", limit: int = RELEVANT_MAX_ITEMS) -> list[dict]:
        with self._lock:
            if not self._scored_items:
                return []
            if not query:
                scored = [(item, item.score()) for item in self._scored_items]
            else:
                query_lower = query.lower()
                query_words = set(query_lower.split())
                stopwords = {"the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "for", "on", "with", "my", "i", "me", "my", "and", "or"}
                query_words -= stopwords
                scored = []
                for item in self._scored_items:
                    item_lower = item.text.lower()
                    relevance = 0.0
                    if query_lower in item_lower:
                        relevance += 0.3
                    common = query_words & set(item_lower.split())
                    if common:
                        relevance += min(len(common) * 0.1, 0.3)
                    item.access_count += 1
                    item.last_accessed = time.time()
                    scored.append((item, item.score() + relevance))
            scored.sort(key=lambda x: x[1], reverse=True)
            result = [item.to_dict() for item, _ in scored[:limit]]
            if scored and any(s[1] > 0 for s in scored[:limit]):
                self._save()
            return result

    def cleanup(self) -> None:
        with self._lock:
            now = time.time()
            before = len(self._scored_items)
            survivors = []
            for item in self._scored_items:
                age_days = (now - item.created_at) / 86400
                if age_days > DECAY_MAX_AGE_DAYS and item.score() < DECAY_THRESHOLD:
                    continue
                survivors.append(item)
            self._scored_items = self._deduplicate(survivors)
            if len(self._scored_items) != before:
                logger.info("Memory cleanup: %d -> %d items", before, len(self._scored_items))
                self._save()

    # ------------------------------------------------------------------ #
    # Internal: scored item management
    # ------------------------------------------------------------------ #

    def _add_scored(self, text: str, category: str = "fact", importance: int | None = None) -> bool:
        text = str(text).strip()
        if not text:
            return False
        if importance is None:
            importance = _estimate_importance(text, category)
        if importance < MIN_IMPORTANCE_TO_SAVE:
            return False
        with self._lock:
            existing = self._find_similar(text)
            if existing is not None:
                existing.text = text
                existing.updated_at = time.time()
                existing.importance = max(existing.importance, importance)
                existing.access_count += 1
                existing.last_accessed = time.time()
                return True
            item = MemoryItem(text, category, importance)
            self._scored_items.append(item)
            return True

    def _find_similar(self, text: str) -> MemoryItem | None:
        text_lower = text.lower()
        for item in self._scored_items:
            if text_lower == item.text.lower():
                return item
        for item in self._scored_items:
            overlap = _semantic_overlap(text, item.text)
            if overlap >= 0.5:
                return item
        return None

    def _deduplicate(self, items: list[MemoryItem]) -> list[MemoryItem]:
        kept: list[MemoryItem] = []
        for item in items:
            duplicate = False
            for existing in kept:
                if item.id == existing.id:
                    continue
                if _semantic_overlap(item.text, existing.text) >= 0.5:
                    duplicate = True
                    if item.importance > existing.importance or item.access_count > existing.access_count:
                        existing.text = item.text
                        existing.importance = max(existing.importance, item.importance)
                        existing.access_count += item.access_count
                        existing.updated_at = time.time()
                    break
            if not duplicate:
                kept.append(item)
        return kept

    def _build_profile(self) -> dict:
        prefs = []
        goals = []
        facts = []
        interests = []
        projects = []
        for item in sorted(self._scored_items, key=lambda i: i.score(), reverse=True):
            c = item.category
            if c == "preference" and len(prefs) < 3:
                prefs.append(item.text)
            elif c == "goal" and len(goals) < 3:
                goals.append(item.text)
            elif c == "project" and len(projects) < 3:
                projects.append(item.text)
            elif c == "interest" and len(interests) < 3:
                interests.append(item.text)
            elif c == "fact" and len(facts) < PROFILE_MAX_ITEMS:
                facts.append(item.text)
        legacy = self._legacy_memory
        comm = deepcopy(legacy.get("communication_style", {}))
        for p in comm.get("preferences") or []:
            if p and p not in prefs:
                prefs.append(p)
        for item in legacy.get("interests") or []:
            if item not in interests:
                interests.append(item)
        for item in legacy.get("goals") or []:
            if item not in goals:
                goals.append(item)
        for item in legacy.get("facts_about_user") or []:
            if item not in facts:
                facts.append(item)
        return {
            "communication_style": comm,
            "preferences": prefs,
            "interests": interests,
            "goals": goals,
            "projects": projects,
            "facts_about_user": facts,
            "memory_count": len(self._scored_items),
        }

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self.storage_path.exists():
            self._scored_items = []
            self._legacy_memory = deepcopy(DEFAULT_LONG_MEMORY)
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._legacy_memory = deepcopy(DEFAULT_LONG_MEMORY)
                for key in DEFAULT_LONG_MEMORY:
                    if key in data:
                        self._legacy_memory[key] = deepcopy(data[key])
                raw_items = data.get("scored_items", []) if isinstance(data, dict) else []
            elif isinstance(data, list):
                self._legacy_memory = deepcopy(DEFAULT_LONG_MEMORY)
                raw_items = data
            else:
                self._scored_items = []
                self._legacy_memory = deepcopy(DEFAULT_LONG_MEMORY)
                return
            self._scored_items = [MemoryItem.from_dict(d) for d in raw_items if isinstance(d, dict)]
            logger.info("Loaded %d scored memory items + legacy profile", len(self._scored_items))
        except Exception:
            logger.exception("Failed to load memory, starting fresh")
            self._scored_items = []
            self._legacy_memory = deepcopy(DEFAULT_LONG_MEMORY)

    def _save(self) -> None:
        with self._lock:
            data = {
                "version": 2,
                "scored_items": [item.to_dict() for item in self._scored_items],
            }
            data.update(deepcopy(self._legacy_memory))
            tmp = self.storage_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            tmp.replace(self.storage_path)

    @staticmethod
    def _default_path() -> Path:
        return Path(__file__).resolve().parents[1] / "data" / "long_memory.json"

    # ------------------------------------------------------------------ #
    # Legacy: rule-based classification (kept from original)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _split_into_clauses(text: str) -> list[str]:
        parts = re.split(r"[.;\n]+|(?<!\bI)(?<!\bi)\s*,\s*and\s+|,\s+and\s+", text)
        return [part.strip(" ,") for part in parts if part.strip(" ,")]

    def _classify_and_store(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        if self._looks_like_preference(text):
            self._append_legacy("communication_style", text)
        elif self._looks_like_interest(text):
            self._append_legacy("interests", text)
        elif self._looks_like_dislike(text):
            self._append_legacy("dislikes", text)
        elif self._looks_like_goal(text):
            self._append_legacy("goals", text)
        else:
            self._append_legacy("facts_about_user", text)

    def _simple_classify(self, text: str) -> tuple[str, str]:
        t = str(text).strip()
        if self._looks_like_preference(t):
            return "preference", t
        if self._looks_like_interest(t):
            return "interest", t
        if self._looks_like_dislike(t):
            return "dislike", t
        if self._looks_like_goal(t):
            return "goal", t
        return "fact", t

    def _add_extracted_as_items(self, extracted: dict) -> None:
        for pref in extracted.get("communication_style", {}).get("preferences", []) or []:
            self._add_scored(str(pref), "preference")
        for item in extracted.get("interests", []) or []:
            self._add_scored(str(item), "interest")
        for item in extracted.get("dislikes", []) or []:
            self._add_scored(str(item), "dislike")
        for item in extracted.get("goals", []) or []:
            self._add_scored(str(item), "goal")
        for item in extracted.get("facts_about_user", []) or []:
            self._add_scored(str(item), "fact")

    @staticmethod
    def _extraction_has_content(extracted: dict) -> bool:
        for item in extracted.get("interests", []) or []:
            if str(item).strip():
                return True
        for item in extracted.get("dislikes", []) or []:
            if str(item).strip():
                return True
        for item in extracted.get("goals", []) or []:
            if str(item).strip():
                return True
        for item in extracted.get("facts_about_user", []) or []:
            if str(item).strip():
                return True
        for pref in extracted.get("communication_style", {}).get("preferences", []) or []:
            if str(pref).strip():
                return True
        return False

    # ------------------------------------------------------------------ #
    # LLM-backed classification
    # ------------------------------------------------------------------ #

    def _get_llm_client(self):
        if self._llm_client is None:
            provider = settings.llm_provider
            if provider == "ollama":
                self._llm_client = OllamaClient()
            elif provider == "gemini":
                self._llm_client = GeminiClient()
            else:
                self._llm_client = OpenRouterClient()
            logger.info("Long-term memory using provider=%s client=%s", provider, self._llm_client.__class__.__name__)
        return self._llm_client

    def _classify_with_llm(self, raw_text: str) -> dict | None:
        raw_text = raw_text.strip()
        if not raw_text:
            return None
        try:
            client = self._get_llm_client()
            extracted = client.chat_json(_MEMORY_EXTRACTION_PROMPT, raw_text)
        except OpenRouterConfigurationError:
            logger.warning("OpenRouter not configured -- falling back to rule-based classification.")
            return None
        except Exception:
            logger.exception("LLM memory classification failed -- falling back to rule-based classification.")
            return None
        if not isinstance(extracted, dict):
            logger.warning("LLM returned non-dict -- falling back.")
            return None
        return extracted

    def _merge_llm_extraction(self, extracted: dict) -> None:
        comm = extracted.get("communication_style", {})
        if isinstance(comm, dict):
            tone = str(comm.get("tone", "")).strip()
            length = str(comm.get("length", "")).strip()
            if tone:
                self._legacy_memory["communication_style"]["tone"] = tone
            if length:
                self._legacy_memory["communication_style"]["length"] = length
            for pref in comm.get("preferences", []) or []:
                self._append_legacy("communication_style", str(pref))
        for category in ("interests", "dislikes", "goals", "facts_about_user"):
            for item in extracted.get(category, []) or []:
                text = str(item).strip()
                if text:
                    self._append_legacy(category, text)

    def _append_legacy(self, category: str, value: str) -> None:
        value = str(value).strip()
        if not value:
            return
        if category == "communication_style":
            prefs = self._legacy_memory["communication_style"]["preferences"]
            if not any(value.lower() == p.lower() for p in prefs):
                prefs.append(value)
            return
        target = self._legacy_memory.get(category)
        if isinstance(target, list) and not any(value.lower() == p.lower() for p in target):
            target.append(value)

    def _forget_legacy(self, text: str) -> bool:
        lowered = text.lower()
        removed = False
        for category, value in self._legacy_memory.items():
            if isinstance(value, list):
                before = len(value)
                value[:] = [item for item in value if lowered not in item.lower()]
                removed = removed or len(value) != before
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, list):
                        before = len(sub_value)
                        sub_value[:] = [item for item in sub_value if lowered not in item.lower()]
                        removed = removed or len(sub_value) != before
                    elif isinstance(sub_value, str) and lowered in sub_value.lower():
                        self._legacy_memory[category][sub_key] = ""
                        removed = True
        return removed

    @staticmethod
    def _looks_like_preference(text: str) -> bool:
        return bool(re.search(r"\b(casual\w*|formal\w*|friendly|professional\w*|concise\w*|detail\w*|short(er|ly)?|long(er|ly)?|polite\w*|direct\w*|warm\w*|humorous\w*|serious\w*|humor\w*|tone\w*|style\w*|voice\w*|talk to me|speak to me|respond)\b", text, re.I))

    @staticmethod
    def _looks_like_interest(text: str) -> bool:
        return bool(re.search(r"\b(i like|i love|my favorite|i enjoy|i prefer|i'm interested in|i am interested in|favorite|interested in)\b", text, re.I))

    @staticmethod
    def _looks_like_dislike(text: str) -> bool:
        return bool(re.search(r"\b(i dislike|i don'?t like|do not like|hate|avoid|not a fan|don't enjoy)\b", text, re.I))

    @staticmethod
    def _looks_like_goal(text: str) -> bool:
        return bool(re.search(r"\b(goal|want to|aim to|plan to|wish to|working on|need to|trying to)\b", text, re.I))

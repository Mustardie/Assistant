import json
import logging
import re
from copy import deepcopy
from pathlib import Path

from llm.openrouter_client import OpenRouterClient, OpenRouterConfigurationError

logger = logging.getLogger(__name__)


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


class LongTermMemory:
    def __init__(self, storage_path: Path | str | None = None):
        self.storage_path = Path(storage_path) if storage_path else self._default_path()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory = self.load_memory()
        self._llm_client = None

    @staticmethod
    def _default_path() -> Path:
        return Path(__file__).resolve().parents[1] / "data" / "long_memory.json"

    def exists(self) -> bool:
        return self.storage_path.exists()

    def load_memory(self) -> dict:
        if not self.exists():
            return deepcopy(DEFAULT_LONG_MEMORY)

        try:
            with open(self.storage_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return deepcopy(DEFAULT_LONG_MEMORY)

        return self._normalize_memory(data)

    def is_onboarded(self) -> bool:
        """The single source of truth for 'has onboarding already happened'.

        This is a persisted flag, not a guess based on whether any field
        happens to be non-empty -- that heuristic is what let onboarding
        get silently skipped (a stray/test data file looks "non-empty")
        or silently re-triggered every run (a genuinely blank answer looks
        "empty") in the previous implementation.
        """
        return bool(self.memory.get("onboarded"))

    def is_empty(self) -> bool:
        memory = self.memory
        communication = memory.get("communication_style", {})
        if communication.get("tone") or communication.get("length") or communication.get("preferences"):
            return False
        if memory.get("interests"):
            return False
        if memory.get("dislikes"):
            return False
        if memory.get("goals"):
            return False
        if memory.get("facts_about_user"):
            return False
        return True

    @staticmethod
    def _normalize_memory(data: dict) -> dict:
        if not isinstance(data, dict):
            return deepcopy(DEFAULT_LONG_MEMORY)

        memory = deepcopy(DEFAULT_LONG_MEMORY)
        for key, value in data.items():
            if key not in memory:
                continue
            if isinstance(memory[key], list) and isinstance(value, list):
                memory[key] = [str(item).strip() for item in value if str(item).strip()]
            elif isinstance(memory[key], dict) and isinstance(value, dict):
                memory[key] = {
                    sub_key: [str(item).strip() for item in value.get(sub_key, []) if str(item).strip()]
                    if isinstance(memory[key][sub_key], list)
                    else str(value.get(sub_key, "")).strip()
                    for sub_key in memory[key]
                }
            elif key == "onboarded":
                memory[key] = bool(value)
            else:
                memory[key] = value

        return memory

    def save_memory(self) -> None:
        with open(self.storage_path, "w", encoding="utf-8") as handle:
            json.dump(self.memory, handle, indent=2, ensure_ascii=False)

    def create_memory(self, raw_text: str = "") -> None:
        """Initialize long-term memory from the user's onboarding answer.

        The onboarding answer is often one sentence covering several
        categories at once (e.g. "talk casually, I like hiking, I'm
        trying to learn Spanish"). We try OpenRouter first for a single,
        holistic structured extraction; if that's unavailable or fails,
        we fall back to the regex clause-by-clause classifier so
        onboarding never silently loses the user's answer.
        """
        self.memory = deepcopy(DEFAULT_LONG_MEMORY)
        raw_text = str(raw_text).strip()
        if raw_text:
            extracted = self._classify_with_llm(raw_text)
            if extracted is not None:
                self._merge_llm_extraction(extracted)
            else:
                for clause in self._split_into_clauses(raw_text):
                    self._classify_and_store(clause)
        self.memory["onboarded"] = True
        self.save_memory()

    @staticmethod
    def _split_into_clauses(text: str) -> list[str]:
        # Split on sentence terminators and common list connectors so a
        # single run-on onboarding answer still gets categorized clause
        # by clause rather than as one giant fact.
        parts = re.split(r"[.;\n]+|(?<!\bI)(?<!\bi)\s*,\s*and\s+|,\s+and\s+", text)
        return [part.strip(" ,") for part in parts if part.strip(" ,")]

    def _classify_and_store(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        if self._looks_like_preference(text):
            self._append_preference(text)
        elif self._looks_like_interest(text):
            self._append_unique("interests", text)
        elif self._looks_like_dislike(text):
            self._append_unique("dislikes", text)
        elif self._looks_like_goal(text):
            self._append_unique("goals", text)
        else:
            self._append_fact(text)

    def update_memory(self, category: str, value: str) -> None:
        if category not in self.memory:
            return
        if not isinstance(self.memory[category], list):
            return
        value = str(value).strip()
        if not value:
            return
        if value not in self.memory[category]:
            self.memory[category].append(value)
            self.save_memory()

    def remember(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        extracted = self._classify_with_llm(text)
        if extracted is not None:
            self._merge_llm_extraction(extracted)
        else:
            self._classify_and_store(text)
        self.save_memory()

    def forget(self, text: str) -> bool:
        text = str(text).strip()
        if not text:
            return False

        lowered = text.lower()
        removed = False

        for category, value in self.memory.items():
            if isinstance(value, list):
                before = len(value)
                value[:] = [item for item in value if lowered not in item.lower()]
                removed = removed or len(value) != before
                self.memory[category] = value
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, list):
                        before = len(sub_value)
                        sub_value[:] = [item for item in sub_value if lowered not in item.lower()]
                        removed = removed or len(sub_value) != before
                        self.memory[category][sub_key] = sub_value
                    elif isinstance(sub_value, str) and lowered in sub_value.lower():
                        self.memory[category][sub_key] = ""
                        removed = True

        if removed:
            self.save_memory()
        return removed

    def to_dict(self) -> dict:
        return deepcopy(self.memory)

    # ------------------------------------------------------------------ #
    # OpenRouter-backed classification
    # ------------------------------------------------------------------ #

    def _get_llm_client(self) -> OpenRouterClient:
        if self._llm_client is None:
            self._llm_client = OpenRouterClient()
        return self._llm_client

    def _classify_with_llm(self, raw_text: str) -> dict | None:
        raw_text = raw_text.strip()
        if not raw_text:
            return None

        try:
            client = self._get_llm_client()
            extracted = client.chat_json(_MEMORY_EXTRACTION_PROMPT, raw_text)
        except OpenRouterConfigurationError:
            logger.warning(
                "OpenRouter not configured -- falling back to rule-based memory classification."
            )
            return None
        except Exception:
            logger.exception(
                "LLM memory classification failed -- falling back to rule-based classification."
            )
            return None

        if not isinstance(extracted, dict):
            logger.warning("LLM memory classification returned non-dict output -- falling back.")
            return None

        return extracted

    def _merge_llm_extraction(self, extracted: dict) -> None:
        comm = extracted.get("communication_style", {})
        if isinstance(comm, dict):
            tone = str(comm.get("tone", "")).strip()
            length = str(comm.get("length", "")).strip()
            if tone:
                self.memory["communication_style"]["tone"] = tone
            if length:
                self.memory["communication_style"]["length"] = length
            for pref in comm.get("preferences", []) or []:
                self._append_preference(str(pref))

        for category in ("interests", "dislikes", "goals", "facts_about_user"):
            for item in extracted.get(category, []) or []:
                text = str(item).strip()
                if text:
                    self._append_unique(category, text)

    # ------------------------------------------------------------------ #
    # Regex fallback classifier (used only if OpenRouter is unavailable)
    # ------------------------------------------------------------------ #

    def _append_unique(self, category: str, text: str) -> None:
        target = self.memory.get(category)
        if not isinstance(target, list):
            return
        text = text.strip()
        if not text or any(text.lower() == existing.lower() for existing in target):
            return
        target.append(text)

    def _append_fact(self, text: str) -> None:
        self._append_unique("facts_about_user", text)

    def _append_preference(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        preferences = self.memory["communication_style"]["preferences"]
        if not any(text.lower() == existing.lower() for existing in preferences):
            preferences.append(text)

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
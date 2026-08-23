import logging
import re
from pathlib import Path

from config.settings import settings
from .long_memory import LongTermMemory
from .short_memory import ShortTermMemory

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, storage_dir: Path | str | None = None):
        if storage_dir:
            self.storage_dir = Path(storage_dir)
        else:
            self.storage_dir = Path(settings.memory_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.long_memory = LongTermMemory(self.storage_dir / "long_memory.json")
        self.short_memory = ShortTermMemory(self.storage_dir / "conversation_memory.json")

    def initialize(self, prompt_fn=input) -> None:
        if self.long_memory.is_onboarded():
            return
        print(
            "Before we start, what should I permanently remember about you?"
        )
        print(
            "Tell me your preferences, interests, personality, goals, and how you want me to respond."
        )
        try:
            response = prompt_fn("You: ").strip()
        except (EOFError, OSError):
            # Construction must remain safe in services, tests, and other
            # non-interactive hosts. The user can add memories later through
            # the normal "remember" command.
            logger.info("Interactive memory onboarding unavailable; starting with an empty profile")
            response = ""
        self.long_memory.create_memory(response)

    def get_user_memory(self) -> dict:
        return self.long_memory.to_dict()

    def get_relevant_memories(self, query: str = "", limit: int = 5) -> list[dict]:
        return self.long_memory.get_relevant(query=query, limit=limit)

    def get_planning_context(self, query: str, *, enabled: bool, limit: int = 3) -> list[dict]:
        """Return only memories with lexical evidence that they fit this task.

        Long-term memory ranking also considers importance and recency, which is
        useful for profile views but used to inject unrelated high-importance
        facts into every planner call.  Planning context requires at least one
        meaningful query token and keeps only a compact summary field set.
        """
        if not enabled or not query.strip():
            return []
        stopwords = {"the", "a", "an", "is", "are", "to", "of", "in", "for", "on", "with", "my", "i", "me", "and", "or", "please"}
        query_words = set(re.findall(r"[a-z0-9_]+", query.lower())) - stopwords
        if not query_words:
            return []
        candidates = self.get_relevant_memories(query=query, limit=max(limit * 3, limit))
        selected = []
        for item in candidates:
            text = str(item.get("text", ""))
            memory_words = set(re.findall(r"[a-z0-9_]+", text.lower())) - stopwords
            overlap = query_words & memory_words
            if not overlap:
                continue
            selected.append({
                "category": item.get("category", "fact"),
                "text": text[:240],
                "matched_terms": sorted(overlap)[:5],
            })
            if len(selected) >= limit:
                break
        return selected

    def get_conversation_history(self) -> list:
        return self.short_memory.retrieve_history()

    def add_message(self, role: str, content: str, metadata: dict | None = None) -> None:
        self.short_memory.add_message(role, content, metadata)

    def update_user_memory(self, action: str, text: str) -> bool:
        action = action.lower().strip()
        if action == "remember":
            self.long_memory.remember(text)
            return True
        if action == "forget":
            return self.long_memory.forget(text)
        return False

    def clear_conversation(self) -> None:
        self.short_memory.clear_conversation()

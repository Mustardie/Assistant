import logging
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
        response = prompt_fn("You: ").strip()
        self.long_memory.create_memory(response)

    def get_user_memory(self) -> dict:
        return self.long_memory.to_dict()

    def get_relevant_memories(self, query: str = "", limit: int = 5) -> list[dict]:
        return self.long_memory.get_relevant(query=query, limit=limit)

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

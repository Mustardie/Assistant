import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_TOOL_CONTENT_CHARS = 300
_MAX_CONVERSATION_CHARS = 8000
_MAX_RECENT_MESSAGES = 15
_MAX_SUMMARY_CHARS = 1500


def _compress_tool_content(content: str, tool_name: str = "") -> str:
    try:
        data = json.loads(content) if isinstance(content, str) else content
        if isinstance(data, dict):
            parts = []
            if "success" in data:
                parts.append("OK" if data["success"] else "FAILED")
            for key in ("path", "count", "indexed", "error", "status", "matches", "total"):
                val = data.get(key)
                if val is not None:
                    parts.append(f"{key}={val}")
            if parts:
                compressed = f"[{tool_name}] {' '.join(parts)}"
                if len(compressed) > _MAX_TOOL_CONTENT_CHARS:
                    compressed = compressed[:_MAX_TOOL_CONTENT_CHARS] + "..."
                return compressed
        if isinstance(data, list) and len(data) > 3:
            return f"[{tool_name}] {len(data)} results (first: {str(data[0])[:100]})"
    except (json.JSONDecodeError, TypeError):
        pass
    content_str = str(content)
    if len(content_str) > _MAX_TOOL_CONTENT_CHARS:
        content_str = content_str[:_MAX_TOOL_CONTENT_CHARS] + "..."
    return content_str


class ShortTermMemory:
    def __init__(
        self,
        storage_path: Path | str | None = None,
        max_entries: int = 80,
        max_chars: int = _MAX_CONVERSATION_CHARS,
        recent_messages: int = _MAX_RECENT_MESSAGES,
        max_summary_chars: int = _MAX_SUMMARY_CHARS,
    ):
        self.storage_path = Path(storage_path) if storage_path else self._default_path()
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self.max_chars = max_chars
        self.recent_messages = recent_messages
        self.max_summary_chars = max_summary_chars
        self._history = None
        self._dirty = False

    def _get_history(self):
        if self._history is None:
            self._history = self.load_history()
        return self._history

    @staticmethod
    def _default_path() -> Path:
        return Path(__file__).resolve().parents[1] / "data" / "conversation_memory.json"

    def load_history(self) -> list:
        if not self.storage_path.exists():
            return []
        try:
            with open(self.storage_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return []
        if isinstance(data, list):
            return [self._normalize_message(message) for message in data]
        return []

    def save_history(self) -> None:
        if not self._dirty:
            return
        history = self._get_history()
        with open(self.storage_path, "w", encoding="utf-8") as handle:
            json.dump(history, handle, indent=2, ensure_ascii=False)
        self._dirty = False

    def add_message(self, role: str, content: str, metadata: dict | None = None) -> None:
        content = str(content).strip()
        if not content:
            return
        compressed = content
        if role == "tool":
            tool_name = (metadata or {}).get("tool", "")
            compressed = _compress_tool_content(content, tool_name)
        message = {"role": role, "content": compressed}
        if metadata and role == "tool":
            message["tool"] = metadata.get("tool", "")
        history = self._get_history()
        history.append(message)
        self._enforce_limits()
        self._dirty = True

    def retrieve_history(self) -> list:
        if self._dirty:
            self.save_history()
        return [self._normalize_message(message) for message in self._get_history()]

    def clear_conversation(self) -> None:
        self._history = []
        self._dirty = True
        self.save_history()

    def _enforce_limits(self) -> None:
        history = self._get_history()
        if not history:
            return
        if len(history) <= self.max_entries and self._total_chars() <= self.max_chars:
            return
        recent = history[-self.recent_messages:]
        old_messages = history[:max(0, len(history) - len(recent))]
        summary = self.summarize_messages(old_messages)
        self._history = [{"role": "system", "content": summary}] + recent

    def _total_chars(self) -> int:
        return sum(len(str(message.get("content", ""))) for message in self._get_history())

    def summarize_messages(self, messages: list) -> str:
        if not messages:
            return ""
        summary_lines = []
        max_messages = min(len(messages), 10)
        tail = messages[-max_messages:]
        for message in tail:
            content = str(message.get("content", "")).replace("\n", " ").strip()
            if not content:
                continue
            role = message.get("role", "assistant")
            if role == "user":
                label = "User"
            elif role == "assistant":
                label = "Assistant"
            elif role == "tool":
                tool_name = message.get("tool") or "Tool"
                label = f"Tool[{tool_name}]"
            elif role == "system":
                label = "Summary"
            else:
                label = role.capitalize()
            if len(content) > 200:
                content = content[:197].rsplit(" ", 1)[0] + "..."
            summary_lines.append(f"{label}: {content}")
        prefix = "Earlier conversation summary:" if len(messages) <= max_messages else "Earlier conversation summary (older content condensed):"
        summary = f"{prefix} {' '.join(summary_lines)}"
        if len(summary) > self.max_summary_chars:
            summary = summary[: self.max_summary_chars].rsplit(" ", 1)[0] + "..."
        return summary

    @staticmethod
    def _normalize_message(message: dict) -> dict:
        if not isinstance(message, dict):
            return {"role": "assistant", "content": str(message)}
        return {
            "role": str(message.get("role", "assistant")),
            "content": str(message.get("content", "")),
            **{k: v for k, v in message.items() if k not in {"role", "content"}},
        }

import logging
import threading
from pathlib import Path

from assistant import Assistant
from brain.agent_loop import AgentLoop, normalize_user_input
from brain.brain import Brain
from memory.memory_manager import MemoryManager
from recommendation.errors import RecommendationConfigurationError
from tools.tool_registry import file_manager_service, run_tool

logger = logging.getLogger(__name__)


def safe_print(value):
    try:
        print(value)
    except UnicodeEncodeError:
        print(str(value).encode("utf-8", errors="replace").decode("utf-8"))


# Multi-argument operations (rename/move/copy/delete/zip) need a second
# piece of information -- a new name or a destination -- that a single
# sentence often doesn't reliably contain. Rather than guessing, we ask.
OPERATIONS_NEEDING_A_SECOND_ARGUMENT = {"rename", "move", "copy", "delete", "extract_archive", "compress_archive"}

# Single-word replies to a pending gmail_reply preview, mapped to the
# `command` gmail_reply/GmailSkill.reply() expects. "send" maps confirm=True;
# everything else is handled by GmailSkill.reply()'s own pending-state logic.
GMAIL_REPLY_CONTROL_WORDS = {
    "send": "send",
    "yes": "send",
    "y": "send",
    "confirm": "send",
    "ok": "send",
    "go ahead": "send",
    "cancel": "cancel",
    "no": "cancel",
    "n": "cancel",
    "discard": "cancel",
    "edit": "edit",
    "regenerate": "regenerate",
}


class Agent:

    def __init__(self):
        self.assistant = Assistant()
        self.memory_manager = MemoryManager()
        self.memory_manager.initialize()
        self.brain = Brain(self.memory_manager)
        self._pending_candidates = None
        self._pending_intent = None
        self._pending_user_goal = None
        self._last_file_action = None
        self._voice_callback = None
        self._event_callback = None
        # Set by AgentLoop.run() whenever it pauses mid-task (a genuine
        # clarifying question, a recovery dead-end, or the iteration budget
        # running out) instead of truly finishing. When set, the NEXT user
        # message resumes this same goal instead of being treated as an
        # unrelated fresh request -- see _handle_pending_agent_task.
        self._pending_agent_task = None
        self._agent_loop = AgentLoop(
            self.brain,
            speak=self._speak,
            record_tool=self._record_tool_result,
            on_file_search_result=self._on_file_search_result,
            track_file_action=self._track_file_action,
            fallback=self._run_intent_fallback,
            emit_event=self._emit_event,
        )
        self._run_lock = threading.Lock()

    def set_voice_callback(self, callback):
        """Register a function to be called immediately when Nova speaks,
        so TTS can start without waiting for agent.run() to finish."""
        self._voice_callback = callback

    def set_event_callback(self, callback):
        """Receive structured, best-effort runtime events for rich clients.

        The agent never depends on this callback, so a closed or faulty UI
        cannot break task execution.
        """
        self._event_callback = callback

    def _emit_event(self, event_type: str, payload: dict | None = None) -> None:
        callback = getattr(self, "_event_callback", None)
        if callback is None:
            return
        try:
            callback(event_type, dict(payload or {}))
        except Exception:
            logger.exception("Agent event callback failed for %s", event_type)

    def _is_youtube_request(self, user: str) -> bool:
        normalized = user.lower().strip()
        if not normalized:
            return False

        if self._is_file_request(user):
            return False

        youtube_keywords = [
            "youtube", "yt", "watch", "play", "stream", "online", "music", "song",
            "trailer", "podcast", "documentary", "gameplay", "tutorial", "speedrun",
            "mrbeast", "minecraft", "shorts", "movie", "film", "clip", "funny", "fun",
        ]
        if any(keyword in normalized for keyword in youtube_keywords):
            return True

        return "video" in normalized and any(
            cue in normalized
            for cue in ["play", "watch", "stream", "funny", "fun", "minecraft", "tutorial", "music", "documentary", "podcast", "movie", "film", "clip"]
        )

    def _is_file_request(self, user: str) -> bool:
        normalized = user.lower().strip()
        if not normalized:
            return False

        action_words = [
            "open", "find", "search", "show", "list", "organize", "rename", "move",
            "copy", "delete", "restore", "inspect", "locate", "look for", "extract",
            "unzip", "compress", "zip", "reveal", "properties", "installed", "installation",
            "where is",
        ]
        if not any(word in normalized for word in action_words):
            return False

        file_indicators = [
            "pdf", "word", "docx", "doc", "excel", "sheet", "ppt", "powerpoint",
            "image", "png", "jpg", "jpeg", "gif", "webp", "bmp", "folder", "directory",
            "archive", "zip", "rar", "tar", "code", "python", "project", "notes",
            "download", "downloads", "homework", "resume", "chemistry", "world",
            "screenshot", "picture", "photo", "text", "file manager", "recording",
            "capture", "document", "worksheet", "audio", "music file", "my downloads",
            "my notes", "my pictures", "my photos", "game", "app", "program",
        ]
        if any(indicator in normalized for indicator in file_indicators):
            return True

        ownership_markers = ["my ", "mine ", "my latest", "my newest", "my recent"]
        personal_context_terms = ["latest", "newest", "recent", "recording", "screenshot", "photo", "picture", "video", "audio", "document", "notes", "file", "folder"]
        if any(marker in normalized for marker in ownership_markers) and any(term in normalized for term in personal_context_terms):
            return True

        return False

    def _classify_intent(self, user: str) -> str:
        if self._is_file_request(user):
            return "local_file"
        if self._is_youtube_request(user):
            return "youtube"
        return "other"

    def _describe_opening_text(self, user: str) -> str:
        normalized = user.lower().strip()
        if "latest" in normalized or "newest" in normalized or "recent" in normalized:
            if "pdf" in normalized:
                return "Opening your latest PDF."
            if "document" in normalized or "doc" in normalized:
                return "Opening the newest document."
            if "video" in normalized:
                return "Opening the latest video."
            if "image" in normalized or "photo" in normalized or "picture" in normalized:
                return "Opening the newest image."
            return "Opening the newest file."
        if "biggest" in normalized or "largest" in normalized:
            if "video" in normalized:
                return "Opening the largest video."
            return "Opening the largest file."
        if "smallest" in normalized or "tiny" in normalized:
            return "Opening the smallest file."
        if "oldest" in normalized:
            return "Opening the oldest file."
        if "installed" in normalized or "installation" in normalized or "where is" in normalized:
            return "Opening the installation folder."
        if "folder" in normalized or "directory" in normalized:
            return "Opening the folder."
        if "image" in normalized or "photo" in normalized or "picture" in normalized:
            return "Opening the image."
        return "Opening the file."

    def _normalize_gmail_search_query(self, user: str) -> str:
        normalized = user.strip()
        for phrase in ["reply to", "respond to", "answer", "reply"]:
            if normalized.lower().startswith(phrase):
                normalized = normalized[len(phrase):].strip()
                break

        if normalized.lower().startswith("re:"):
            normalized = normalized[3:].strip()

        normalized = normalized.strip(" .,:;!?\"")
        if normalized.lower().endswith(" email"):
            normalized = normalized[:-6].strip()
        if normalized.lower().endswith(" mail"):
            normalized = normalized[:-4].strip()
        if normalized.lower().startswith("the "):
            normalized = normalized[4:].strip()

        if normalized.lower().startswith("gheekster"):
            return "gheekster"
        return normalized

    def _handle_gmail_reply_request(self, user: str):
        normalized = user.lower().strip()

        if normalized in GMAIL_REPLY_CONTROL_WORDS:
            command = GMAIL_REPLY_CONTROL_WORDS[normalized]
            confirm = command == "send"
            response = run_tool(
                "gmail_reply",
                {"command": command, "draft_mode": False, "confirm": confirm},
            )[1]
            self._record_tool_result("gmail_reply", response)
            self._speak(str(response))
            return response

        reply_keywords = ["reply to", "reply", "re:", "respond to", "answer"]
        if not any(keyword in normalized for keyword in reply_keywords):
            return None

        search_query = self._normalize_gmail_search_query(user)
        success, search_result = run_tool("gmail_search", {"query": search_query, "limit": 5})
        if not success:
            self._speak(str(search_result))
            return str(search_result)

        if isinstance(search_result, list) and search_result:
            first_match = search_result[0]
            message_id = first_match.get("message_id") if isinstance(first_match, dict) else None
            thread_id = first_match.get("thread_id") if isinstance(first_match, dict) else None
            if message_id or thread_id:
                self._speak("I found a matching email. I’ll show you a preview before I send anything.")
                result = run_tool(
                    "gmail_reply",
                    {"command": user, "draft_mode": False, "confirm": False},
                )[1]
                self._record_tool_result("gmail_reply", result)
                return result

        self._speak("I couldn’t find a matching email to reply to.")
        return "I couldn’t find a matching email to reply to."

    def _speak(self, message: str) -> None:
        self._emit_event("assistant_speaking", {"text": str(message)})
        try:
            self.assistant.speak(message)
        except UnicodeEncodeError:
            safe_message = str(message).encode("utf-8", errors="replace").decode("utf-8")
            self.assistant.speak(safe_message)
        if hasattr(self, "memory_manager"):
            self.memory_manager.add_message("assistant", message)
        # Fire voice callback immediately so TTS starts without waiting
        # for agent.run() to finish.
        if getattr(self, "_voice_callback", None):
            try:
                self._voice_callback(message)
            except Exception:
                logger.exception("Voice callback failed in _speak")

    def _record_tool_result(self, tool_name: str, result) -> None:
        if not hasattr(self, "memory_manager"):
            return
        self.memory_manager.add_message(
            "tool",
            str(result),
            metadata={"tool": tool_name},
        )

    def _track_file_action(self, tool: str, arguments: dict) -> None:
        self._last_file_action = {
            "tool": tool,
            "source": arguments.get("source"),
            "destination": arguments.get("destination"),
        }

    def _build_intent_hint(self, user: str) -> str | None:
        if self._is_file_request(user):
            return (
                "This looks like a local file or folder request. "
                "Use file_search first to locate the target — never guess paths."
            )
        if self._is_youtube_request(user):
            return (
                "This looks like a YouTube or online video request. "
                "Use youtube_recommend(request) to search, rank, and play."
            )
        return None

    def _on_file_search_result(self, user: str, response: dict) -> bool:
        """Handle structured file_search results. Returns True if the loop should stop."""
        status = response.get("status")

        if status == "no_match":
            self._speak("I couldn't find a matching file or folder.")
            safe_print(response)
            return True

        if status == "clarify":
            candidates = response.get("candidates", [])
            self._pending_candidates = candidates
            self._pending_intent = response.get("intent")
            self._pending_user_goal = user
            lines = [f"{i + 1}. {c.get('path')}" for i, c in enumerate(candidates)]
            self._speak(
                "I found a few close matches, but I'm not confident enough to guess. "
                "Which one did you mean?"
            )
            safe_print("Which one did you mean?\n" + "\n".join(lines))
            return True

        if status == "ok":
            result = response.get("result", {})
            intent = response.get("intent")
            self._act_on_result(user, intent, result)
            return True

        return False

    def _run_intent_fallback(self, user: str, intent_hint: str | None) -> bool:
        """Fast-path fallback when the planner returns no step on the first turn."""
        if self._is_youtube_request(user):
            safe_print("\n[youtube_request fallback]")
            try:
                success, result = run_tool("youtube_recommend", {"request": user})
            except RecommendationConfigurationError as error:
                self._speak(str(error))
                safe_print(error)
                return True

            self._record_tool_result("youtube_recommend", result)
            safe_print(result)
            return True

        if self._is_file_request(user):
            self._handle_file_request(user)
            return True

        return False

    def _is_memory_command(self, user: str) -> bool:
        normalized = user.strip().lower()
        return normalized.startswith(
            (
                "remember ",
                "remember that ",
                "save this ",
                "don't forget ",
                "do not forget ",
                "forget ",
            )
        )

    def _handle_memory_command(self, user: str) -> bool:
        normalized = user.strip()
        lower = normalized.lower()

        remember_prefixes = ["remember that ", "remember ", "save this ", "don't forget ", "do not forget "]
        for prefix in remember_prefixes:
            if lower.startswith(prefix):
                content = normalized[len(prefix) :].strip()
                if not content:
                    self._speak("What would you like me to remember?")
                    return True
                self.memory_manager.update_user_memory("remember", content)
                self._speak(f"Okay, I will remember that: {content}")
                return True

        forget_prefix = "forget "
        if lower.startswith(forget_prefix):
            content = normalized[len(forget_prefix) :].strip()
            if not content:
                self._speak("What should I forget?")
                return True
            removed = self.memory_manager.update_user_memory("forget", content)
            if removed:
                self._speak(f"I forgot: {content}")
            else:
                self._speak(f"I couldn't find anything matching '{content}' to forget.")
            return True

        return False

    # ------------------------------------------------------------------ #
    # File-request handling
    # ------------------------------------------------------------------ #

    def _handle_pending_clarification(self, user: str):
        """If we previously asked 'which one did you mean', try to resolve
        this message as that answer. Returns the selected candidate if
        resolved (and acts on it), or None if this isn't a clarification
        reply (in which case the pending state is cleared and normal
        handling proceeds for a fresh request)."""
        if not self._pending_candidates:
            return None

        normalized = user.strip()
        selected = None

        if normalized.isdigit():
            index = int(normalized) - 1
            if 0 <= index < len(self._pending_candidates):
                selected = self._pending_candidates[index]
        else:
            lowered = normalized.lower()
            for candidate in self._pending_candidates:
                path = str(candidate.get("path", ""))
                if lowered in path.lower() or Path(path).name.lower() == lowered:
                    selected = candidate
                    break

        if selected is None:
            self._pending_candidates = None
            self._pending_intent = None
            return None

        intent = self._pending_intent
        self._pending_candidates = None
        self._pending_intent = None
        self._speak(f"Got it, using {selected.get('path')}.")
        self._act_on_result(user, intent, selected)
        return selected

    def _act_on_result(self, user: str, intent: str, result: dict):
        path = result.get("path")

        if intent in {"open_file", "open_folder", "find_installation", "search"}:
           original_goal = self._pending_user_goal or user
           self._pending_user_goal = None
           normalized = original_goal.lower()
           is_extract = any(kw in normalized for kw in ("unzip", "extract", "decompress", "unpack"))
           if is_extract and path and any(path.lower().endswith(ext) for ext in (".zip", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".rar")):
               self._speak(f"Extracting {Path(path).name} to the same folder...")
               tool = "unzip_archive" if path.lower().endswith(".zip") else "extract_archive"
               extract_result = run_tool(tool, {"archive_path": path, "destination": str(Path(path).parent)})
               self._record_tool_result("extract_archive", extract_result)
               safe_print("\n[extract_archive]")
               safe_print(extract_result)
               return
           spoken_text = self._describe_opening_text(user)
           self._speak(spoken_text)
           open_result = run_tool("file_open", {"path": path})
           self._record_tool_result("file_open", open_result)
           safe_print("\n[file_open]")
           safe_print(open_result)
           return

        if intent == "list_folder":
           list_result = run_tool("file_list", {"path": path})
           self._record_tool_result("file_list", list_result)
           safe_print("\n[file_list]")
           safe_print(list_result)
           return

        if intent == "reveal_in_explorer":
           reveal_result = run_tool("reveal_in_explorer", {"path": path})
           self._record_tool_result("reveal_in_explorer", reveal_result)
           safe_print("\n[reveal_in_explorer]")
           safe_print(reveal_result)
           return

        if intent == "show_properties":
           props = run_tool("show_properties", {"path": path})
           self._record_tool_result("show_properties", props)
           safe_print("\n[show_properties]")
           safe_print(props)
           return

        if intent == "extract_archive":
            self._speak(f"Extracting {Path(path).name}...")
            extract_result = run_tool("unzip_archive" if str(path).lower().endswith(".zip") else "extract_archive", {"archive_path": path, "destination": str(Path(path).parent)})
            self._record_tool_result("extract_archive", extract_result)
            safe_print("\n[extract_archive]")
            safe_print(extract_result)
            return

        if intent in OPERATIONS_NEEDING_A_SECOND_ARGUMENT:
            self._speak(
                f"I found {path}. This action needs one more detail — a new name, "
                "a destination, or where to extract it — that I can't reliably read "
                "off a single sentence. What should I use?"
            )
            safe_print(f"\n[{intent}] pending_target={path}")
            return

        safe_print(result)

    def _handle_file_request(self, user: str):
        safe_print("\n[file_request]")
        try:
            success, response = run_tool("file_search", {"query": user, "limit": 10})
            self._record_tool_result("file_search", response)
            if not success:
                self._speak(str(response))
                safe_print(response)
                return

            status = response.get("status")

            if status == "no_match":
                self.assistant.speak("I couldn’t find a matching file or folder.")
                safe_print(response)
                return

            if status == "clarify":
                candidates = response.get("candidates", [])
                self._pending_candidates = candidates
                self._pending_intent = response.get("intent")
                self._pending_user_goal = user
                lines = [f"{i + 1}. {c.get('path')}" for i, c in enumerate(candidates)]
                self.assistant.speak(
                    "I found a few close matches, but I’m not confident enough to guess. Which one did you mean?"
                )
                safe_print("Which one did you mean?\n" + "\n".join(lines))
                return

            result = response.get("result", {})
            self._act_on_result(user, response.get("intent"), result)
            return
        except Exception as error:
           self._speak(str(error))
           safe_print(error)
           return

    def _is_rename_correction(self, user: str) -> bool:
        normalized = user.lower()
        if ("don't move" in normalized or "do not move" in normalized) and "rename" in normalized:
            return True
        return False

    def _handle_rename_correction(self, user: str) -> bool:
        if not self._is_rename_correction(user):
            return False

        if not self._last_file_action or self._last_file_action.get("tool") not in {"file_move", "file_rename"}:
            return False

        source = self._last_file_action.get("source")
        destination = self._last_file_action.get("destination")
        if not source or not destination:
            return False

        src = Path(source).expanduser().resolve()
        naming = Path(destination).name
        if not naming:
            return False

        new_destination = src.parent / naming
        if not new_destination.suffix:
            new_destination = new_destination.with_suffix(src.suffix)
        elif new_destination.suffix.lower() != src.suffix.lower():
            new_destination = new_destination.with_suffix(src.suffix)

        success, result = run_tool(
            "file_rename",
            {"source": str(src), "destination": str(new_destination)},
        )
        self._record_tool_result("file_rename", result)
        self._last_file_action = {
            "tool": "file_rename",
            "source": str(src),
            "destination": str(new_destination),
        }

        if success:
            self._speak(f"I renamed the file without moving it to {new_destination.name}.")
        else:
            self._speak(
                "I tried to rename it without moving it, but something went wrong. "
                f"{result.get('error', 'Unknown error')}"
            )
        return True

    def run(self, user):
        if not self._run_lock.acquire(blocking=False):
            logger.warning("Agent.run() called while already running; dropping duplicate call for: %s", user[:80])
            return
        try:
            self._emit_event("assistant_thinking", {"goal": str(user)})
            self._run_inner(user)
        finally:
            self._emit_event("idle", {})
            self._run_lock.release()

    def _run_inner(self, user):
        user = normalize_user_input(user)
        self.memory_manager.add_message("user", user)
        try:
            memories = self.memory_manager.get_planning_context(user, enabled=True, limit=5)
        except Exception:
            logger.exception("Unable to prepare memory event")
            memories = []
        self._emit_event("memory_retrieved", {
            "query": user,
            "memories": memories,
            "used": bool(memories),
        })

        if self._handle_rename_correction(user):
            return

        if self._handle_memory_command(user):
            return

        clarified = self._handle_pending_clarification(user)
        if clarified is not None:
            return

        direct_result = self._handle_gmail_reply_request(user)
        if direct_result is not None:
            safe_print("\n[gmail_reply]")
            safe_print(direct_result)
            return

        pending = self._pending_agent_task
        self._pending_agent_task = None  # clear eagerly; run() will re-set it if still pending

        if pending:
            intent_hint = self._build_intent_hint(pending["goal"])
            self._pending_agent_task = self._agent_loop.run(
                user,
                intent_hint=intent_hint,
                resume_goal=pending["goal"],
                resume_observations=pending["observations"],
                resume_state=pending.get("task_state"),
            )
            return

        intent_hint = self._build_intent_hint(user)
        self._pending_agent_task = self._agent_loop.run(user, intent_hint=intent_hint)

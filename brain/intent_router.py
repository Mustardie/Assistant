"""Structured, deterministic first-pass routing for general JARVIS requests.

The router deliberately does not execute anything. Its job is to keep the
planner from seeing every request as an excuse to call a tool and to surface
missing information before execution starts.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum


class Intent(str, Enum):
    CONVERSATION = "normal_conversation"
    QUESTION = "answer_question"
    LOCAL_TASK = "local_file_task"
    APP_CONTROL = "app_tool_control"
    CONNECTOR = "connector_request"
    MEMORY = "memory_request"
    SKILL_EXECUTION = "skill_execution"
    SKILL_RECORDING = "skill_recording"
    AUTOMATION = "automation_watch_request"
    CODING = "coding_dev_request"
    AMBIGUOUS = "ambiguous_needs_clarification"


@dataclass(frozen=True)
class IntentDecision:
    intent: Intent
    confidence: float
    missing_info: list[str] = field(default_factory=list)
    likely_required_tools: list[str] = field(default_factory=list)
    clarification_needed: bool = False
    can_answer_directly: bool = False
    safety_notes: list[str] = field(default_factory=list)
    memory_relevant: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["intent"] = self.intent.value
        return data

    def prompt_hint(self) -> str:
        tools = ", ".join(self.likely_required_tools) or "none"
        missing = ", ".join(self.missing_info) or "none"
        safety = "; ".join(self.safety_notes) or "none"
        return (
            f"intent={self.intent.value}; confidence={self.confidence:.2f}; "
            f"can_answer_directly={str(self.can_answer_directly).lower()}; "
            f"clarification_needed={str(self.clarification_needed).lower()}; "
            f"memory_relevant={str(self.memory_relevant).lower()}; "
            f"likely_tools={tools}; missing_info={missing}; safety={safety}"
        )


class IntentRouter:
    _SKILL_RECORD = re.compile(
        r"\b(watch me|learn this|record(?:ing)? (?:a |this )?skill|stop recording|"
        r"pause (?:the )?recording|resume recording)\b",
        re.I,
    )
    _SKILL_RUN = re.compile(
        r"\b(run|play|do|execute|list|show|export|import|rename|delete|duplicate)\b.*\bskills?\b",
        re.I,
    )
    _MEMORY = re.compile(
        r"^(remember|remember that|forget|don't forget|do not forget|what do you remember)\b",
        re.I,
    )
    _AUTOMATION = re.compile(
        r"\b(remind me|every (?:day|week|month|hour)|monitor|watch for|notify me|"
        r"when .* happens|schedule|automation)\b",
        re.I,
    )
    _CONNECTORS = {
        "gmail": ("gmail_",),
        "email": ("gmail_",),
        "calendar": ("calendar_",),
        "drive": ("drive_",),
        "dropbox": ("dropbox_",),
        "github": ("github_",),
        "notion": ("notion_",),
        "slack": ("slack_",),
    }
    _LOCAL = re.compile(
        r"\b(file|folder|directory|document|pdf|docx|spreadsheet|workbook|image|photo|"
        r"archive|zip|downloads?|desktop|path|project files?|screenshot|assignment|homework|"
        r"notes?|videos?|audio|recording|logs?|crash report|settings|config(?:uration)?|"
        r"build artifacts?|model weights?)\b",
        re.I,
    )
    _LOCAL_ACTION = re.compile(
        r"\b(find|search|locate|open|list|create|write|rename|move|copy|delete|restore|"
        r"extract|compress|inspect|show|reveal|organize|save|summarize|identify|"
        r"classify|understand)\b",
        re.I,
    )
    _APP = re.compile(
        r"\b(open|launch|close|switch to|click|type|press|scroll in)\b.*\b(app|application|"
        r"browser|chrome|edge|spotify|calculator|notepad|settings|youtube|website|site)\b",
        re.I,
    )
    _CODING = re.compile(
        r"\b(code|coding|debug|bug|fix|refactor|implement|repository|repo|git|tests?|"
        r"python|javascript|typescript|api|function|class|module)\b",
        re.I,
    )
    _DESTRUCTIVE = re.compile(r"\b(delete|remove|erase|send|submit|purchase|buy|shutdown|restart)\b", re.I)
    _VAGUE = re.compile(r"^(do it|fix it|open it|that one|the thing|help)$", re.I)

    def route(self, request: str) -> IntentDecision:
        text = (request or "").strip()
        if not text or self._VAGUE.fullmatch(text):
            return IntentDecision(
                Intent.AMBIGUOUS,
                0.98,
                missing_info=["the target or desired outcome"],
                clarification_needed=True,
            )

        if self._SKILL_RECORD.search(text):
            return IntentDecision(
                Intent.SKILL_RECORDING,
                0.98,
                likely_required_tools=["skill_record_start", "skill_record_stop"],
                memory_relevant=True,
                safety_notes=["playback must pause before risky recorded actions"],
            )

        if self._SKILL_RUN.search(text):
            missing = []
            if re.search(r"\b(run|play|do|execute)\b", text, re.I) and not re.search(r"\b(?:the|my|a)\s+[\w -]+\s+skill\b", text, re.I):
                missing = ["skill name"]
            return IntentDecision(
                Intent.SKILL_EXECUTION,
                0.94,
                missing_info=missing,
                likely_required_tools=["skill_list"] if "list" in text.lower() else [],
                clarification_needed=bool(missing),
                memory_relevant=True,
                safety_notes=["respect the skill's confirmation rules"],
            )

        if self._MEMORY.search(text):
            needs_value = text.lower().strip() in {"remember", "remember that", "forget", "don't forget"}
            return IntentDecision(
                Intent.MEMORY,
                0.99,
                missing_info=["what to remember or forget"] if needs_value else [],
                clarification_needed=needs_value,
                can_answer_directly=not needs_value,
                memory_relevant=True,
            )

        if self._AUTOMATION.search(text):
            missing = []
            if "remind me" in text.lower() and not re.search(r"\b(at|on|in|every|when)\b", text, re.I):
                missing.append("when the reminder should run")
            return IntentDecision(
                Intent.AUTOMATION,
                0.91,
                missing_info=missing,
                likely_required_tools=["automation"],
                clarification_needed=bool(missing),
                memory_relevant=True,
                safety_notes=["confirm external side effects configured by the automation"],
            )

        lowered = text.lower()
        connector = next((name for name in self._CONNECTORS if re.search(rf"\b{re.escape(name)}\b", lowered)), None)
        if connector:
            prefixes = self._CONNECTORS[connector]
            notes = ["check connector authentication and capabilities before execution"]
            if self._DESTRUCTIVE.search(text):
                notes.append("sending, deleting, or submitting requires confirmation")
            return IntentDecision(
                Intent.CONNECTOR,
                0.93,
                likely_required_tools=[f"{prefix}*" for prefix in prefixes],
                memory_relevant=True,
                safety_notes=notes,
            )

        if re.search(r"\b(what should i commit|should not commit|shouldn't commit|git status|untracked|safe(?:ly)? stage|stage only)\b", lowered):
            tools = ["file_git_summary"]
            if "stage" in lowered:
                tools.append("file_safe_stage")
            return IntentDecision(
                Intent.LOCAL_TASK,
                0.97,
                likely_required_tools=tools,
                memory_relevant=False,
                safety_notes=["Git inspection is read-only", "never stage without an explicit reviewed path list"],
            )

        if self._CODING.search(text):
            return IntentDecision(
                Intent.CODING,
                0.88,
                likely_required_tools=["file_search", "file_open"],
                memory_relevant=True,
                safety_notes=["inspect before editing and verify with tests"],
            )

        if self._LOCAL.search(text) and self._LOCAL_ACTION.search(text):
            notes = ["locate the target before acting"]
            if self._DESTRUCTIVE.search(text):
                notes.append("destructive changes require confirmation")
            return IntentDecision(
                Intent.LOCAL_TASK,
                0.92,
                likely_required_tools=["file_intent_search"] if re.search(
                    r"\b(crash|worksheet|settings|reference|render|junk|purpose|summarize|identify|classify|understand)\b",
                    lowered,
                ) else ["file_search"],
                memory_relevant=True,
                safety_notes=notes,
            )

        if self._APP.search(text):
            return IntentDecision(
                Intent.APP_CONTROL,
                0.88,
                likely_required_tools=["launch_app", "browser_open"],
                safety_notes=["inspect current app state before interaction when uncertain"],
            )

        if "?" in text or re.match(r"^(what|why|how|who|when|where|which|explain|tell me)\b", text, re.I):
            current_info = bool(re.search(r"\b(latest|today|current|right now|price|weather|news)\b", text, re.I))
            return IntentDecision(
                Intent.QUESTION,
                0.90,
                likely_required_tools=["google_search"] if current_info else [],
                can_answer_directly=not current_info,
                memory_relevant=bool(re.search(r"\b(my|me|I)\b", text)),
            )

        return IntentDecision(Intent.CONVERSATION, 0.82, can_answer_directly=True)

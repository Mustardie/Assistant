"""Catalog of current and future JARVIS widgets."""

from __future__ import annotations

from ui.jarvis.models import WidgetSpec


class WidgetRegistry:
    def __init__(self):
        self._specs: dict[str, WidgetSpec] = {}

    def register(self, spec: WidgetSpec) -> None:
        if not spec.widget_type:
            raise ValueError("widget_type is required")
        if spec.widget_type in self._specs:
            raise ValueError(f"Widget '{spec.widget_type}' is already registered")
        self._specs[spec.widget_type] = spec

    def get(self, widget_type: str) -> WidgetSpec:
        try:
            return self._specs[widget_type]
        except KeyError as exc:
            raise KeyError(f"Unknown widget type: {widget_type}") from exc

    def all(self) -> list[WidgetSpec]:
        return list(self._specs.values())

    def available(self, backends: set[str] | None = None) -> list[WidgetSpec]:
        backends = backends or set()
        return [spec for spec in self.all() if not spec.required_backend or spec.required_backend in backends]


def _spec(widget_type, title, purpose, *, backend=None, actions=(), size=(360, 280), minimum=(260, 170), implemented=True, demo=None, disabled=None, singleton=True):
    return WidgetSpec(
        widget_type=widget_type,
        title=title,
        purpose=purpose,
        required_backend=backend,
        supported_actions=tuple(actions),
        default_size=size,
        min_size=minimum,
        implemented=implemented,
        demo_payload=dict(demo or {}),
        disabled_message=disabled or (f"{title} checks the {backend} service live and reports exact failures." if backend else "Local JARVIS module ready."),
        singleton=singleton,
    )


def build_default_registry() -> WidgetRegistry:
    registry = WidgetRegistry()
    specs = [
        _spec("chat", "JARVIS Console", "Typed conversation and detailed task control", actions=("send", "interrupt", "inspect_plan"), size=(470, 420), minimum=(340, 260), implemented=True),
        _spec("task_progress", "Mission Progress", "Goal, plan steps, tools, retries, and completion", actions=("cancel", "inspect"), size=(390, 330), implemented=True),
        _spec("confirmation", "Authorization Required", "Approve or deny risky actions", actions=("approve", "deny"), size=(390, 270), implemented=True),
        _spec("system_status", "System Matrix", "Model, voice, connector, and runtime health", actions=("refresh",), size=(350, 290), implemented=True),
        _spec("weather", "Atmospherics", "Current weather, forecast, and warnings", backend="weather", actions=("refresh",), size=(360, 250), implemented=True, demo={"connected": False, "location": ""}, disabled="Live keyless weather is fetched on demand; choose a location."),
        _spec("video_player", "Media Playback", "Local video playback and review", backend="local_media", actions=("open", "play", "pause", "seek", "volume"), size=(520, 360), minimum=(380, 260), implemented=True, disabled="Choose a local video path to enable playback."),
        _spec("file_search", "File Intelligence", "Search results, metadata, preview, and open actions", backend="file_search", actions=("search", "open", "reveal"), size=(440, 350), implemented=True),
        _spec("memory_recall", "Memory Recall", "Relevant memories used for the current task", backend="memory", actions=("recall", "forget", "inspect_source"), size=(380, 280), implemented=True),
        _spec("calendar", "Schedule", "Events, free time, deadlines, and meetings", backend="calendar", actions=("refresh", "open_event")),
        _spec("reminders", "Tasks & Reminders", "Create, complete, and snooze reminders", backend="automation", actions=("create", "complete", "snooze")),
        _spec("notes", "Notes", "Scratchpad, summaries, and generated notes", actions=("save", "copy")),
        _spec("web_results", "Web Intelligence", "Search results, summaries, and citations", backend="browser", actions=("open", "continue_research")),
        _spec("audio_player", "Audio", "Music, local audio, volume, and queue", backend="audio", actions=("play", "pause", "skip")),
        _spec("app_launcher", "Applications", "Installed and frequently used applications", backend="app_index", actions=("launch",)),
        _spec("clipboard", "Clipboard", "Recent copied content and follow-up actions", backend="clipboard", actions=("summarize", "save", "paste")),
        _spec("transfers", "Transfers", "Downloads, imports, exports, and file operations", backend="downloads", actions=("pause", "cancel")),
        _spec("notifications", "Notifications", "Alerts, completions, failures, and warnings", actions=("dismiss", "open")),
        _spec("email", "Inbox", "Email summaries, search results, and drafts", backend="gmail", actions=("search", "draft", "archive")),
        _spec("messaging", "Messages", "Connected messaging services", backend="messaging", actions=("draft", "send")),
        _spec("connectors", "Connections", "Account authentication, permissions, and sync", backend="connectors", actions=("connect", "refresh")),
        _spec("tool_inspector", "Tool Inspector", "Tool arguments, results, retries, and verification", actions=("retry", "copy")),
        _spec("plan_inspector", "Plan Inspector", "Assumptions, risks, next step, and success criteria", actions=("pause", "cancel")),
        _spec("automation", "Automations", "Watchers, schedules, triggers, and run history", backend="automation", actions=("enable", "disable", "run")),
        _spec("skills", "Skills", "Recorded skills, triggers, test mode, and history", backend="skills", actions=("run", "test", "edit")),
        _spec("voice_transcript", "Voice Transcript", "Live transcription, confidence, and corrections", backend="stt", actions=("correct", "copy")),
        _spec("command_palette", "Command Palette", "Fast widget and assistant actions", actions=("run",), size=(480, 300)),
        _spec("system_monitor", "System Monitor", "CPU, GPU, memory, disks, and model runtime", backend="system_metrics", actions=("refresh",)),
        _spec("code_task", "Development Task", "Changed files, tests, diffs, and errors", backend="developer", actions=("run_tests", "open_file"), size=(500, 380)),
        _spec("terminal", "Command Output", "Safe command execution output", backend="terminal", actions=("stop", "copy"), size=(520, 340)),
        _spec("media_review", "Media Review", "Review local media and keep structured feedback notes", backend="local_media", actions=("open", "feedback"), size=(520, 360)),
        _spec("study", "Study Mode", "Questions, explanations, formulas, quizzes, and flashcards", actions=("quiz", "explain", "save")),
        _spec("quick_answer", "Quick Answer", "Compact facts, calculations, definitions, and conversions", actions=("copy",), size=(330, 200)),
        _spec("error_debug", "Diagnostics", "Failure cause, logs, retry, and report actions", actions=("retry", "copy_report"), size=(430, 300)),
        _spec("settings", "JARVIS Settings", "Theme, voice, model, connectors, permissions, and layout", actions=("save", "reset"), implemented=True, size=(440, 620), minimum=(380, 420)),
        _spec("activity", "Activity Timeline", "Recent requests, tools, files, and confirmations", actions=("filter", "clear")),
        _spec("desktop_context", "Desktop Context", "Safe current app, windows, files, widgets, and connector summary", actions=("refresh",), size=(400, 330)),
        _spec("current_mode", "Current Mode", "Evidence-backed work-mode prediction and feedback", actions=("refresh", "mark_wrong"), size=(380, 300)),
        _spec("routine_suggestions", "Routine Suggestions", "Dismissible suggestions and review-only skill plans", actions=("refresh", "accept", "dismiss", "disable", "create_skill"), size=(430, 340)),
        _spec("privacy_monitoring", "Privacy & Monitoring", "Visible desktop awareness status and controls", actions=("refresh", "start", "stop", "pause", "resume", "privacy"), size=(400, 320)),
        _spec("startup_status", "Startup Status", "Opt-in Windows startup status and controls", actions=("refresh", "enable", "disable"), size=(390, 290)),
        _spec("inbox_item", "Inbox Attachments", "Downloaded, imported, or connector attachment candidates with honest source context", backend="inbox", actions=("scan", "ingest"), size=(470, 350)),
        _spec("assignment_analysis", "Assignment Analysis", "Extracted instructions, questions, deadlines, subjects, confidence, and gaps", backend="inbox", actions=("analyze", "plan"), size=(500, 390)),
        _spec("assignment_plan", "Assignment Plan", "Review-first output structure, tasks, sources, assumptions, and missing input", backend="inbox", actions=("refresh", "draft"), size=(500, 390)),
        _spec("assignment_draft", "Assignment Draft", "Reviewable generated response; submission is always a separate confirmed action", backend="inbox", actions=("generate", "export"), size=(560, 440)),
        _spec("source_files", "Assignment Sources", "Files and message context used to derive the assignment", backend="inbox", actions=("refresh", "open"), size=(430, 320)),
    ]
    for spec in specs:
        registry.register(spec)
    return registry

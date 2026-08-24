"""Deterministic routine learning, mode prediction, and quiet suggestions."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from tools.desktop_context_models import (
    ContextPrediction, DesktopContextSnapshot, ProactiveSuggestion,
    RoutinePattern, SkillSuggestionPlan, WorkMode, utc_now,
)


def _time(value: str | None) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _contains(apps: Iterable[str], *terms: str) -> bool:
    text = " | ".join(str(app).lower() for app in apps)
    return any(term.lower() in text for term in terms)


_WIDGETS = {
    WorkMode.CODING: ("code_task", "terminal", "system_status"),
    WorkMode.EDITING: ("media_review", "system_status", "source_files"),
    WorkMode.STUDY: ("study", "notes", "reminders"),
    WorkMode.RECORDING: ("system_monitor", "activity", "file_search"),
    WorkMode.RESEARCH: ("web_results", "notes", "file_search"),
    WorkMode.COMMUNICATION: ("messaging", "notifications"),
    WorkMode.ASSIGNMENT: ("inbox_item", "assignment_analysis", "source_files"),
}

_ACTIONS = {
    WorkMode.CODING: ("Show Git summary", "Open coding dashboard"),
    WorkMode.EDITING: ("Open editing dashboard", "Show project files"),
    WorkMode.STUDY: ("Start study timer", "Open notes"),
    WorkMode.RECORDING: ("Prepare recording mode", "Open clips folder"),
    WorkMode.RESEARCH: ("Open research notes",),
    WorkMode.COMMUNICATION: ("Open communication widget",),
    WorkMode.ASSIGNMENT: ("Analyze recent assignment file",),
}


class ContextPredictor:
    def predict(self, snapshot: DesktopContextSnapshot,
                recent_events: Iterable[dict[str, Any]] = ()) -> ContextPrediction:
        context = snapshot.context
        apps = set(context.important_running_apps)
        if context.active_app:
            apps.add(context.active_app)
        evidence: dict[WorkMode, list[str]] = {mode: [] for mode in WorkMode}
        scores: Counter[WorkMode] = Counter()

        idle = context.idle_seconds
        if idle is not None and idle >= 600:
            scores[WorkMode.IDLE] += 10
            evidence[WorkMode.IDLE].append(f"no user input for {int(idle)} seconds")

        def add(mode: WorkMode, points: float, reason: str):
            scores[mode] += points
            evidence[mode].append(reason)

        if _contains(apps, "visual studio code", "code.exe", "vscode"):
            add(WorkMode.CODING, 3, "VS Code is running")
        if _contains(apps, "terminal", "powershell"):
            add(WorkMode.CODING, 1.5, "a terminal is running")
        if _contains(apps, "lm studio"):
            add(WorkMode.CODING, 1.5, "LM Studio is running")
        if _contains(apps, "premiere", "davinci", "resolve"):
            add(WorkMode.EDITING, 4, "a video editor is running")
        minecraft = _contains(apps, "minecraft")
        obs = _contains(apps, "obs")
        if minecraft:
            add(WorkMode.RECORDING, 2, "Minecraft is running")
        if obs:
            add(WorkMode.RECORDING, 2.5, "OBS is running")
        if minecraft and obs:
            add(WorkMode.RECORDING, 2, "Minecraft and OBS are running together")
        if _contains(apps, "discord", "whatsapp"):
            add(WorkMode.COMMUNICATION, 3, "a communication app is running")
        if _contains(apps, "chrome", "edge"):
            add(WorkMode.RESEARCH, 1.5, "a browser is active")
        if _contains(context.widgets_open, "code_task", "terminal"):
            add(WorkMode.CODING, 1, "coding widgets are open")
        if _contains(context.widgets_open, "study", "assignment_analysis"):
            add(WorkMode.STUDY, 1, "study or assignment widgets are open")

        files = [*context.recently_opened_files, *context.recent_downloads]
        extensions = {str(item.get("extension") or Path(str(item.get("path") or "")).suffix).lower() for item in files}
        if extensions & {".pdf", ".doc", ".docx", ".ppt", ".pptx"}:
            add(WorkMode.STUDY, 2, "recent document activity")
        command_terms = []
        for event in recent_events:
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            command_terms.extend(metadata.get("intent_terms") or [])
        if any(term in {"assignment", "worksheet", "homework", "teacher"} for term in command_terms):
            add(WorkMode.ASSIGNMENT, 5, "recent JARVIS assignment command")
        if any(term in {"coding", "code", "git"} for term in command_terms):
            add(WorkMode.CODING, 2, "recent explicit coding command")
        if any(term in {"editing", "premiere"} for term in command_terms):
            add(WorkMode.EDITING, 2, "recent explicit editing command")
        if any(term in {"study", "research"} for term in command_terms):
            add(WorkMode.STUDY, 1.5, "recent explicit study command")
        if any(term in {"minecraft", "recording"} for term in command_terms):
            add(WorkMode.RECORDING, 2, "recent explicit recording command")
        if context.recent_downloads and extensions & {".pdf", ".doc", ".docx"}:
            add(WorkMode.ASSIGNMENT, 1.5, "a document was recently downloaded")
        local_hour = _time(snapshot.timestamp).astimezone().hour
        if extensions & {".pdf", ".doc", ".docx", ".pptx"} and 14 <= local_hour <= 22:
            add(WorkMode.STUDY, 0.5, "document work during the usual afternoon/evening study window")

        if not scores:
            return ContextPrediction(WorkMode.UNKNOWN, 0.2, ("not enough safe context",))
        mode, top = scores.most_common(1)[0]
        runner_up = scores.most_common(2)[1][1] if len(scores) > 1 else 0
        confidence = min(0.97, max(0.35, 0.45 + top * 0.08 + max(0, top - runner_up) * 0.03))
        return ContextPrediction(
            mode, round(confidence, 2), tuple(evidence[mode]),
            _WIDGETS.get(mode, ()), _ACTIONS.get(mode, ()),
            (f"Start {mode.value.replace('_', ' ')} mode",) if mode not in {WorkMode.IDLE, WorkMode.UNKNOWN} else (),
        )


class HabitLearner:
    def __init__(self, predictor: ContextPredictor | None = None, *, session_gap_minutes: int = 30):
        self.predictor = predictor or ContextPredictor()
        self.session_gap = timedelta(minutes=session_gap_minutes)

    def detect(self, events: Iterable[dict[str, Any]], existing: Iterable[dict[str, Any]] = ()) -> list[RoutinePattern]:
        values = sorted((dict(item) for item in events), key=lambda item: _time(item.get("timestamp")))
        sessions: list[list[dict[str, Any]]] = []
        for event in values:
            if not sessions or _time(event.get("timestamp")) - _time(sessions[-1][-1].get("timestamp")) > self.session_gap:
                sessions.append([])
            sessions[-1].append(event)
        existing_by_id = {str(item.get("id")): item for item in existing}
        grouped: dict[WorkMode, list[dict[str, Any]]] = {}
        for session in sessions:
            apps: set[str] = set()
            files: set[str] = set()
            actions: set[str] = set()
            for event in session:
                if event.get("app_name"):
                    apps.add(str(event["app_name"]))
                metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
                apps.update(str(item) for item in metadata.get("apps") or [])
                extension = metadata.get("extension")
                if extension:
                    files.add(str(extension).lower())
                if metadata.get("action"):
                    actions.add(str(metadata["action"]))
            mode = self._mode_for_apps(apps, files)
            if mode is WorkMode.UNKNOWN:
                continue
            grouped.setdefault(mode, []).append({
                "timestamp": session[-1].get("timestamp") or utc_now(),
                "apps": sorted(apps), "files": sorted(files), "actions": sorted(actions),
            })

        patterns = []
        for mode, examples in grouped.items():
            if len(examples) < 2:
                continue
            app_counts = Counter(app for example in examples for app in example["apps"])
            threshold = max(1, math.ceil(len(examples) / 2))
            apps = tuple(sorted(app for app, count in app_counts.items() if count >= threshold))
            files = tuple(sorted({item for example in examples for item in example["files"]}))
            actions = tuple(sorted({item for example in examples for item in example["actions"]}))
            digest = hashlib.sha256(f"{mode.value}|{'|'.join(apps)}".encode()).hexdigest()[:12]
            habit_id = f"routine-{digest}"
            prior = existing_by_id.get(habit_id, {})
            name = f"{mode.value.replace('_', ' ').title()} routine"
            patterns.append(RoutinePattern(
                habit_id, name,
                (f"apps commonly appear together: {', '.join(apps)}", "observed in separate local sessions"),
                apps, files, actions, len(examples), min(0.95, round(0.5 + len(examples) * 0.1, 2)),
                str(examples[-1]["timestamp"]), tuple(examples[-3:]),
                f"Suggest {mode.value.replace('_', ' ')} mode",
                f"Start {mode.value.replace('_', ' ')} mode",
                bool(prior.get("auto_suggest_allowed", True)), True, bool(prior.get("disabled", False)),
            ))
        return sorted(patterns, key=lambda item: (-item.confidence, item.name))

    @staticmethod
    def _mode_for_apps(apps: set[str], files: set[str]) -> WorkMode:
        if _contains(apps, "minecraft") and _contains(apps, "obs"):
            return WorkMode.RECORDING
        if _contains(apps, "premiere", "davinci", "resolve"):
            return WorkMode.EDITING
        if _contains(apps, "visual studio code", "terminal", "powershell", "lm studio"):
            return WorkMode.CODING
        if _contains(apps, "discord", "whatsapp"):
            return WorkMode.COMMUNICATION
        if files & {".pdf", ".doc", ".docx", ".pptx"}:
            return WorkMode.STUDY
        if _contains(apps, "chrome", "edge"):
            return WorkMode.RESEARCH
        return WorkMode.UNKNOWN

    @staticmethod
    def skill_plan(routine: dict[str, Any]) -> SkillSuggestionPlan:
        name = str(routine.get("suggested_skill") or f"Start {routine.get('name') or 'desktop'}")
        steps: list[dict[str, Any]] = []
        for app in routine.get("apps") or []:
            steps.append({"tool": "app_open", "arguments": {"query": app}, "expected": f"{app} is verified open or focused"})
        mode = str(routine.get("name") or "routine").replace(" routine", "").lower().replace(" ", "_")
        widget = _WIDGETS.get(next((item for item in WorkMode if item.value == mode), WorkMode.UNKNOWN), ())
        if widget:
            steps.append({"action": "widget_open", "widget_type": widget[0], "expected": "mode dashboard is visible"})
        return SkillSuggestionPlan(
            str(routine.get("id") or ""), name,
            f"Reviewable skill plan learned from {int(routine.get('frequency') or 0)} safe desktop sessions.",
            (name.lower(), f"run the {name.lower()} skill"), tuple(steps),
            tuple(sorted({str(step.get("tool")) for step in steps if step.get("tool")})),
        )


class SuggestionEngine:
    def __init__(self, store):
        self.store = store

    def generate(self, snapshot: DesktopContextSnapshot, prediction: ContextPrediction,
                 routines: Iterable[dict[str, Any]] = ()) -> list[ProactiveSuggestion]:
        config = self.store.config()
        if snapshot.privacy_mode.value == "strict" or snapshot.monitoring_state.value != "running":
            return []
        disabled = set(config.get("disabled_suggestion_types") or [])
        if prediction.mode in {WorkMode.UNKNOWN, WorkMode.IDLE}:
            return []
        if _contains(snapshot.context.important_running_apps, "minecraft") and not config.get("allow_suggestions_during_gaming", False):
            return []
        suggestion_type = f"mode:{prediction.mode.value}"
        if suggestion_type in disabled:
            return []
        now = datetime.now(timezone.utc)
        cooldown = timedelta(minutes=max(1, int(config.get("suggestion_cooldown_minutes") or 60)))
        prior = [item for item in self.store.suggestions() if item.get("suggestion_type") == suggestion_type]
        if prior:
            latest = max(prior, key=lambda item: _time(item.get("created_at")))
            dismissals = sum(max(1, int(item.get("dismiss_count") or 0)) for item in prior if item.get("dismissed"))
            if dismissals >= 2:
                return []
            if now - _time(latest.get("created_at")) < cooldown:
                return []
        routine = next((item for item in routines if prediction.mode.value.replace("_", " ") in str(item.get("name", "")).lower()), None)
        if routine and (routine.get("disabled") or not routine.get("auto_suggest_allowed", True)):
            return []
        suggestion_id = f"suggestion-{hashlib.sha256(f'{suggestion_type}|{now.isoformat()}'.encode()).hexdigest()[:12]}"
        title = f"{prediction.mode.value.replace('_', ' ').title()} context detected"
        action = prediction.suggested_actions[0] if prediction.suggested_actions else "Open the relevant dashboard"
        suggestion = ProactiveSuggestion(
            suggestion_id, suggestion_type, title,
            f"{action}?", now.isoformat(timespec="seconds"),
            (now + timedelta(hours=4)).isoformat(timespec="seconds"), prediction.confidence,
            prediction.evidence, ({"action": action, "execute_automatically": False},),
            requires_confirmation=False, routine_id=routine.get("id") if routine else None,
        )
        self.store.save_suggestion(suggestion.to_dict())
        return [suggestion]

    def dismiss(self, suggestion_id: str) -> bool:
        match = next((item for item in self.store.suggestions() if item.get("id") == suggestion_id), None)
        if not match:
            return False
        count = int(match.get("dismiss_count") or 0) + 1
        return self.store.update_suggestion(suggestion_id, dismissed=True, dismiss_count=count, dismissed_at=utc_now())

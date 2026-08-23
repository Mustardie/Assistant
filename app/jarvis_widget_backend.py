"""Concrete backend operations for JARVIS widgets.

The UI sends semantic actions here.  Read-only local actions execute directly;
reasoning requests return a prompt for the agent; risky actions return a
confirmation request instead of executing.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from config.paths import get_nova_app_file


class JarvisWidgetBackend:
    def __init__(self, *, agent=None):
        self.agent = agent
        self.notes_file = get_nova_app_file("jarvis_notes.json")
        self.reminders_file = get_nova_app_file("jarvis_reminders.json")
        self.calendar_file = get_nova_app_file("jarvis_calendar.json")
        self.activity_file = get_nova_app_file("jarvis_activity.json")
        self.media_feedback_file = get_nova_app_file("jarvis_media_feedback.json")

    @staticmethod
    def _load(path: Path, default):
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
            return value
        except Exception:
            return default

    @staticmethod
    def _save(path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _ok(data: dict | None = None, *, notice: str = "Ready") -> dict:
        return {"success": True, "data": dict(data or {}), "notice": notice}

    @staticmethod
    def _error(message: str) -> dict:
        return {"success": False, "error": str(message)}

    @staticmethod
    def _selected(payload: dict) -> Any:
        return payload.get("selected")

    def perform(self, widget_type: str, action: str, payload: dict | None = None, state_data: dict | None = None) -> dict:
        payload = dict(payload or {})
        state_data = dict(state_data or {})
        try:
            handler = getattr(self, f"_{widget_type}", None)
            if handler:
                result = handler(action, payload, state_data)
                if widget_type != "activity":
                    self._record_activity(widget_type, action, bool(result.get("success")), result.get("error"))
                return result
            return self._error(f"No backend route for {widget_type}.{action}")
        except Exception as exc:
            return self._error(f"{widget_type} action failed: {exc}")

    def _record_activity(self, widget_type: str, action: str, success: bool, error: str | None = None) -> None:
        items = self._load(self.activity_file, [])
        items.append({
            "title": f"{widget_type.replace('_', ' ').title()} · {action}",
            "status": "complete" if success else "failed",
            "detail": str(error or ""),
            "time": datetime.now().isoformat(timespec="seconds"),
        })
        self._save(self.activity_file, items[-200:])

    def _weather(self, action, payload, state):
        from tools.weather import current_weather
        location = payload.get("location") or state.get("location") or state.get("query")
        result = current_weather(str(location or ""))
        if not result.get("success"):
            return self._error(result.get("error") or "Weather request failed")
        result.pop("success", None)
        return self._ok(result, notice="Live weather updated")

    def _calendar(self, action, payload, state):
        events = self._load(self.calendar_file, [])
        if action == "create":
            text = str(payload.get("text") or payload.get("query") or "").strip()
            if not text:
                return self._error("Describe the event to add.")
            events.append({"title": text, "time": "Unscheduled", "created": datetime.now().isoformat(timespec="seconds")})
            self._save(self.calendar_file, events)
        return self._ok({"events": events}, notice=f"{len(events)} local event(s)")

    def _reminders(self, action, payload, state):
        reminders = self._load(self.reminders_file, [])
        if action == "create":
            text = str(payload.get("text") or payload.get("query") or "").strip()
            if not text:
                return self._error("Enter a reminder first.")
            reminders.append({"title": text, "status": "open", "created": datetime.now().isoformat(timespec="seconds")})
        elif action == "complete":
            selected = self._selected(payload)
            title = selected.get("title") if isinstance(selected, dict) else str(selected or "")
            for item in reminders:
                if item.get("title") == title:
                    item["status"] = "complete"
        self._save(self.reminders_file, reminders)
        return self._ok({"items": reminders}, notice=f"{len(reminders)} reminder(s)")

    def _notes(self, action, payload, state):
        text = str(payload.get("text") or state.get("text") or "")
        if action == "save":
            notes = self._load(self.notes_file, [])
            notes.append({"text": text, "updated": datetime.now().isoformat(timespec="seconds")})
            self._save(self.notes_file, notes[-100:])
            return self._ok({"text": text, "status": "Saved locally"}, notice="Note saved locally")
        notes = self._load(self.notes_file, [])
        latest = notes[-1].get("text", "") if notes else text
        return self._ok({"text": latest, "status": "Latest local note loaded" if notes else "New local note"})

    def _video_player(self, action, payload, state):
        return self._ok({"path": payload.get("path") or state.get("path"), "status": f"Playback {action}"})

    def _audio_player(self, action, payload, state):
        return self._ok({"path": payload.get("path") or state.get("path"), "status": f"Playback {action}"})

    def _web_results(self, action, payload, state):
        query = str(payload.get("query") or "").strip()
        if action == "search" and query:
            return {"success": True, "prompt": f"Search the web for {query}. Summarize the best results with source links."}
        selected = self._selected(payload)
        return self._ok({"selected": selected})

    def _app_launcher(self, action, payload, state):
        from tools.app_launcher import launch_app, load_apps
        if action == "launch":
            selected = self._selected(payload)
            query = selected.get("name") if isinstance(selected, dict) else str(selected or payload.get("query") or "")
            success, name = launch_app(query)
            return self._ok({"status": f"Opened {name}"}, notice=f"Opened {name}") if success else self._error(f"Application not found: {query}")
        try:
            apps = list(load_apps().values())
        except FileNotFoundError:
            from tools.app_discovery import build_database
            build_database()
            apps = list(load_apps().values())
        query = str(payload.get("query") or "").lower().strip()
        if query:
            apps = [item for item in apps if query in str(item.get("name", "")).lower()]
        return self._ok({"apps": apps[:60]}, notice=f"{len(apps[:60])} application(s)")

    def _transfers(self, action, payload, state):
        items = list(state.get("transfers") or [])
        if action == "cancel":
            selected = self._selected(payload)
            for item in items:
                if item == selected and isinstance(item, dict):
                    item["status"] = "cancelled"
        return self._ok({"transfers": items}, notice="Transfer queue synchronized")

    def _notifications(self, action, payload, state):
        items = list(state.get("notifications") or state.get("items") or [])
        if action == "dismiss":
            selected = self._selected(payload)
            items = [item for item in items if item != selected]
        return self._ok({"notifications": items}, notice=f"{len(items)} notification(s)")

    def _email(self, action, payload, state):
        from connectors.defaults import default_registry
        registry = default_registry()
        if action in {"refresh", "search"}:
            query = str(payload.get("query") or "").strip()
            capability = "search" if query else "read"
            arguments = {"query": query, "limit": 15} if query else {"limit": 15}
            result = registry.execute("gmail", capability, arguments, retries=1)
            if not result.success:
                return self._error(result.error or "Gmail is unavailable")
            values = result.data if isinstance(result.data, list) else [result.data]
            return self._ok({"emails": values}, notice=f"{len(values)} email(s)")
        return self._ok()

    def _connectors(self, action, payload, state):
        from connectors.defaults import default_registry
        registry = default_registry()
        if action == "connect":
            selected = self._selected(payload)
            name = selected.get("name") if isinstance(selected, dict) else str(selected or "")
            if name == "gmail":
                from youtube_auth import ensure_youtube_auth
                ensure_youtube_auth()
            elif name:
                return self._error(f"No sign-in flow is registered for {name}")
            else:
                return self._error("Choose a connector first.")
        values = []
        for name in registry.names():
            values.append({"name": name, "status": registry.status(name).value, "description": f"{len(registry.capabilities(name))} capabilities"})
        return self._ok({"connectors": values}, notice=f"{len(values)} connector(s) discovered")

    def _tool_inspector(self, action, payload, state):
        if action == "retry":
            selected = self._selected(payload)
            if selected:
                return {"success": True, "prompt": f"Retry this failed tool step with corrected arguments and verify the result: {selected}"}
            return self._error("Choose a tool call to retry.")
        return self._ok({"tool_calls": state.get("tool_calls") or []}, notice="Tool history synchronized")

    def _plan_inspector(self, action, payload, state):
        return self._ok({"steps": state.get("steps") or [], "goal": state.get("goal") or ""}, notice="Plan state synchronized")

    def _automation(self, action, payload, state):
        if action == "run" and self._selected(payload):
            return {"success": True, "prompt": f"Run this automation safely: {self._selected(payload)}"}
        return self._ok({"automations": state.get("automations") or []}, notice="No background scheduler is registered" if not state.get("automations") else "Automations synchronized")

    def _skills(self, action, payload, state):
        from skills.manager import SkillManager
        manager = getattr(self, "_skill_manager", None)
        if manager is None:
            manager = self._skill_manager = SkillManager()
        if action == "run":
            selected = self._selected(payload)
            name = selected.get("name") if isinstance(selected, dict) else str(selected or payload.get("query") or "")
            if not name:
                return self._error("Choose a skill to run.")
            result = manager.play(name)
            if result.get("success") is False or result.get("status") == "failed":
                return self._error(result.get("message") or result.get("speak") or "Skill failed")
            return self._ok({"status": result.get("speak") or "Skill started"}, notice="Skill started")
        return self._ok({"skills": manager.list_skills()}, notice="Saved skills loaded")

    def _command_palette(self, action, payload, state):
        command = str(payload.get("query") or payload.get("text") or "").strip()
        return {"success": True, "prompt": command} if command else self._error("Enter a command first.")

    def _system_monitor(self, action, payload, state):
        metrics = []
        try:
            import psutil
            metrics.extend([
                {"name": "CPU", "value": f"{psutil.cpu_percent(interval=0.05):.0f}%"},
                {"name": "RAM", "value": f"{psutil.virtual_memory().percent:.0f}%"},
                {"name": "DISK", "value": f"{psutil.disk_usage(str(Path.cwd().anchor)).percent:.0f}%"},
            ])
        except Exception:
            usage = shutil.disk_usage(Path.cwd().anchor)
            metrics.append({"name": "DISK", "value": f"{usage.used / usage.total:.0%}"})
        metrics.extend([
            {"name": "OS", "value": platform.platform()},
            {"name": "PYTHON", "value": platform.python_version()},
        ])
        return self._ok({"metrics": metrics}, notice="Live local metrics")

    def _code_task(self, action, payload, state):
        query = str(payload.get("query") or "").strip()
        if action == "run_tests":
            return {"success": True, "prompt": f"Inspect the current development task and run the relevant safe tests. {query}".strip()}
        return self._ok({"files": state.get("files") or []})

    def _activity(self, action, payload, state):
        items = self._load(self.activity_file, [])
        if action == "clear":
            items = []
            self._save(self.activity_file, items)
        return self._ok({"activity": items[-100:]}, notice=f"{len(items[-100:])} activity event(s)")

    def _clipboard(self, action, payload, state):
        text = str(payload.get("text") or state.get("text") or "")
        if action == "summarize" and text:
            return {"success": True, "prompt": f"Summarize this clipboard content:\n\n{text}"}
        return self._ok({"text": text})

    def _messaging(self, action, payload, state):
        text = str(payload.get("text") or "").strip()
        if action == "draft":
            return {"success": True, "prompt": f"Improve this message draft without sending it:\n\n{text}"}
        if action == "send":
            return {
                "success": True,
                "confirmation": {"action": "Send message", "risk": "Sends content to an external recipient", "target": text or "Unspecified message"},
                "pending_prompt": f"Send this message using the appropriate connected messaging service: {text}",
            }
        return self._ok({"draft": text})

    def _terminal(self, action, payload, state):
        command = str(payload.get("text") or "").strip()
        if action == "run":
            if not command:
                return self._error("Enter a command first.")
            return {"success": True, "confirmation": {"action": "Run command", "risk": "Commands can change files and system state", "target": command}, "pending_prompt": f"Run this command only after confirmation: {command}"}
        return self._ok({"command": command, "report": state.get("report") or ""})

    def _media_review(self, action, payload, state):
        text = str(payload.get("text") or "").strip()
        if action == "feedback":
            items = self._load(self.media_feedback_file, [])
            items.append({"path": state.get("path"), "feedback": text, "time": datetime.now().isoformat(timespec="seconds")})
            self._save(self.media_feedback_file, items[-100:])
        return self._ok({"feedback": text, "status": "Feedback saved locally for this review"})

    def _study(self, action, payload, state):
        text = str(payload.get("text") or "").strip()
        if not text:
            return self._error("Enter a topic or question first.")
        mode = "quiz me interactively on" if action == "quiz" else "explain clearly"
        return {"success": True, "prompt": f"{mode}: {text}"}

    def _quick_answer(self, action, payload, state):
        text = str(payload.get("text") or "").strip()
        return {"success": True, "prompt": f"Answer concisely: {text}"} if text else self._error("Enter a question first.")

    def _error_debug(self, action, payload, state):
        report = str(payload.get("text") or state.get("report") or state.get("error") or "")
        if action == "retry":
            return {"success": True, "prompt": f"Retry or recover from this failure without claiming false success: {report}"}
        return self._ok({"report": report})

    def _memory_recall(self, action, payload, state):
        query = str(payload.get("query") or "").strip()
        if self.agent is None:
            return self._error("Agent memory is unavailable")
        values = self.agent.memory_manager.get_relevant_memories(query=query, limit=8)
        return self._ok({"query": query, "memories": values, "used": bool(values)}, notice=f"{len(values)} memory item(s)")

"""Intent-to-UI routing and assistant event integration."""

from __future__ import annotations

import re
from typing import Any

from ui.jarvis.events import JarvisEventBus, JarvisEventType
from ui.jarvis.models import JarvisState
from ui.jarvis.manager import WidgetManager


class JarvisUIController:
    """Turns assistant/task events into useful, non-cluttered widgets."""

    def __init__(self, manager: WidgetManager, event_bus: JarvisEventBus | None = None):
        self.manager = manager
        self.events = event_bus or manager.events
        self.state = JarvisState.IDLE
        self.current_goal = ""
        self.auto_widgets = True

    def set_state(self, state: str | JarvisState, *, detail: str = "") -> JarvisState:
        try:
            normalized = JarvisState.normalize(state)
        except ValueError:
            normalized = JarvisState.ERROR
            detail = detail or f"Unknown assistant state: {state}"
        self.state = normalized
        self.events.publish(
            JarvisEventType.JARVIS_STATE_CHANGED,
            {"state": normalized.value, "detail": detail},
            source="ui_controller",
        )
        return normalized

    def route_request(self, request: str) -> list[str]:
        """Open only widgets that materially help with this request."""
        text = (request or "").strip()
        lower = text.lower()
        self.current_goal = text
        created: list[str] = []

        def ensure(widget_type: str, **kwargs):
            state = self.manager.create(widget_type, **kwargs)
            if state.widget_id not in created:
                created.append(state.widget_id)
            return state

        if re.search(r"\b(weather|forecast|temperature|rain|storm)\b", lower):
            ensure("weather", data={"query": text, "connected": False})
        if re.search(r"\b(play|watch|review|open)\b.*\b(video|clip|movie|render|\.mp4|\.mov|\.mkv)\b", lower):
            path_match = re.search(r"([A-Za-z]:[\\/][^\n\r\"']+\.(?:mp4|mov|mkv|avi|webm))", text, re.I)
            ensure("video_player", data={"path": path_match.group(1).strip() if path_match else None})
        if re.search(r"\b(find|search|locate|show)\b.*\b(file|folder|document|pdf|notes?|assignment)\b", lower):
            ensure("file_search", data={"query": text, "results": [], "loading": True})
        if re.search(r"\b(remember|memory|recall|what do you know about me|forget)\b", lower):
            ensure("memory_recall", data={"query": text, "memories": [], "used": False})
        if re.search(r"\b(calendar|schedule|meeting|appointments?|events?)\b", lower):
            ensure("calendar")
        if re.search(r"\b(remind|reminder|todo|to-do|task list)\b", lower):
            ensure("reminders")
        if re.search(r"\b(note|scratchpad|write this down)\b", lower):
            ensure("notes")
        if re.search(r"\b(search the web|web search|research online|look online)\b", lower):
            ensure("web_results", data={"query": text})
        if re.search(r"\b(open|launch|start)\b.*\b(app|application|chrome|discord|spotify|calculator)\b", lower):
            ensure("app_launcher", data={"query": text})
        if re.search(r"\b(clipboard|copied|copy buffer)\b", lower):
            ensure("clipboard")
        if re.search(r"\b(skill|watch me do|record this workflow)\b", lower):
            ensure("skills")
        if re.search(r"\b(system monitor|cpu|gpu|ram|memory usage|disk usage)\b", lower):
            ensure("system_monitor")
        if re.search(r"\b(plan|progress|what are you doing|status of|long task|research|build|implement)\b", lower):
            ensure("task_progress", data={"goal": text, "steps": [], "status": "planning"})
        if re.search(r"\b(code|debug|developer|diff|tests?|terminal|command)\b", lower):
            ensure("chat")
            ensure("task_progress", data={"goal": text, "steps": [], "status": "planning"})
        if re.search(r"\b(email|gmail|inbox)\b", lower):
            ensure("email", data={"query": text})
        if re.search(r"\b(connector|account|connection|sign in)\b", lower):
            ensure("connectors")
        return created

    def assistant_message(self, text: str) -> None:
        self.events.publish(
            JarvisEventType.CHAT_MESSAGE,
            {"role": "assistant", "text": str(text)},
            source="assistant",
        )
        self.set_state(JarvisState.SPEAKING)

    def user_message(self, text: str, *, voice: bool = False) -> None:
        if self.auto_widgets:
            self.route_request(text)
        self.events.publish(
            JarvisEventType.CHAT_MESSAGE,
            {"role": "user", "text": str(text), "voice": voice},
            source="user",
        )
        self.events.publish(
            JarvisEventType.TRANSCRIPT_UPDATED,
            {"text": str(text), "final": True, "source": "voice" if voice else "chat"},
            source="user",
        )
        self.set_state(JarvisState.THINKING)
        transcript = self.manager.find_type("voice_transcript")
        if transcript:
            self.manager.update(transcript.widget_id, data={"text": str(text), "final": True, "source": "voice" if voice else "chat"})

    def confirmation_required(self, action: str, risk: str, target: str, *, confirmation_id: str = "") -> str:
        state = self.manager.create(
            "confirmation",
            data={
                "confirmation_id": confirmation_id,
                "action": action,
                "risk": risk,
                "target": target,
                "resolved": False,
            },
        )
        self.set_state(JarvisState.WAITING_FOR_CONFIRMATION)
        return state.widget_id

    def handle_agent_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        payload = dict(payload or {})
        if event_type in {"assistant_thinking", "thinking"}:
            goal = str(payload.get("goal") or "").strip()
            if goal:
                self.current_goal = goal
                if self.auto_widgets:
                    self.route_request(goal)
            self.set_state(JarvisState.THINKING, detail=payload.get("detail", ""))
            return
        if event_type == "tool_started":
            self.set_state(JarvisState.EXECUTING_TOOL, detail=payload.get("tool", ""))
            progress = self.manager.create("task_progress", data={"goal": self.current_goal})
            calls = list(progress.data.get("tool_calls") or [])
            calls.append({**payload, "status": "running"})
            self.manager.update(progress.widget_id, data={"tool_calls": calls, "status": "executing"})
            inspector = self.manager.find_type("tool_inspector")
            if inspector:
                history = list(inspector.data.get("tool_calls") or [])
                history.append({**payload, "status": "running"})
                self.manager.update(inspector.widget_id, data={"tool_calls": history})
            return
        if event_type in {"tool_finished", "tool_failed"}:
            progress = self.manager.find_type("task_progress")
            if progress:
                calls = list(progress.data.get("tool_calls") or [])
                calls.append({**payload, "status": "failed" if event_type == "tool_failed" else "complete"})
                self.manager.update(progress.widget_id, data={"tool_calls": calls, "status": "recovering" if event_type == "tool_failed" else "running"})
            if event_type == "tool_failed":
                if payload.get("tool") == "file_search":
                    file_widget = self.manager.find_type("file_search")
                    if file_widget:
                        self.manager.update(file_widget.widget_id, loading=False, error=str(payload.get("error") or "File search failed"))
                self.set_state(JarvisState.ERROR, detail=str(payload.get("error") or "Tool failed"))
                self.manager.create("error_debug", data=payload)
                notifications = self.manager.find_type("notifications")
                if notifications:
                    items = list(notifications.data.get("notifications") or [])
                    items.append({"title": f"{payload.get('tool') or 'Tool'} failed", "status": "error", "detail": str(payload.get("error") or "Unknown failure")})
                    self.manager.update(notifications.widget_id, data={"notifications": items})
            else:
                if payload.get("tool") == "file_search":
                    file_widget = self.manager.find_type("file_search")
                    if file_widget:
                        raw = payload.get("result") or {}
                        results = []
                        if isinstance(raw, dict):
                            if raw.get("status") == "clarify":
                                results = raw.get("candidates") or []
                            elif raw.get("status") == "ok" and raw.get("result"):
                                results = [raw.get("result")]
                        self.manager.update(file_widget.widget_id, loading=False, empty=not bool(results), data={"results": results})
                self.set_state(JarvisState.THINKING)
            inspector = self.manager.find_type("tool_inspector")
            if inspector:
                history = list(inspector.data.get("tool_calls") or [])
                history.append({**payload, "status": "failed" if event_type == "tool_failed" else "complete"})
                self.manager.update(inspector.widget_id, data={"tool_calls": history})
            return
        if event_type == "memory_retrieved":
            if payload.get("used") or self.manager.find_type("memory_recall"):
                memory_widget = self.manager.create("memory_recall")
                self.manager.update(memory_widget.widget_id, data=payload, loading=False, empty=not bool(payload.get("memories")))
            return
        if event_type == "confirmation_required":
            self.confirmation_required(
                payload.get("action") or payload.get("tool") or "Sensitive action",
                payload.get("risk") or payload.get("reason") or "This action changes external state.",
                payload.get("target") or str(payload.get("arguments") or "Not specified"),
                confirmation_id=payload.get("confirmation_id", ""),
            )
            return
        if event_type == "task_updated":
            progress = self.manager.create("task_progress")
            self.manager.update(progress.widget_id, data=payload)
            inspector = self.manager.find_type("plan_inspector")
            if inspector:
                self.manager.update(inspector.widget_id, data=payload)
            if str(payload.get("status") or "").lower() == "complete":
                notifications = self.manager.find_type("notifications")
                if notifications:
                    items = list(notifications.data.get("notifications") or [])
                    items.append({"title": payload.get("goal") or "Task complete", "status": "complete"})
                    self.manager.update(notifications.widget_id, data={"notifications": items})
            return
        if event_type == "assistant_speaking":
            self.set_state(JarvisState.SPEAKING)
            return
        if event_type == "idle":
            self.set_state(JarvisState.IDLE)

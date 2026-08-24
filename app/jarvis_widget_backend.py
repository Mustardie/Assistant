"""Concrete backend operations for JARVIS widgets.

The UI sends semantic actions here.  Read-only local actions execute directly;
reasoning requests return a prompt for the agent; risky actions return a
confirmation request instead of executing.
"""

from __future__ import annotations

import json
import platform
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from config.paths import get_nova_app_file


class JarvisWidgetBackend:
    def __init__(self, *, agent=None, desktop_service=None):
        self.agent = agent
        self.desktop_service = desktop_service
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
            if not payload.get("confirm"):
                return {
                    "success": True,
                    "confirmation": {"action": "Create calendar event", "risk": "Adds an event to your JARVIS calendar", "target": text},
                    "pending_prompt": f"Create this calendar event only after confirmation: {text}",
                    "created": False,
                }
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

    def _desktop(self):
        if self.desktop_service is not None:
            return self.desktop_service
        from tools.desktop_control import get_desktop_service
        return get_desktop_service()

    def _system_status(self, action, payload, state):
        desktop = self._desktop().get_state().to_dict()
        active = desktop.get("active_window") or {}
        values = {
            **state,
            "desktop": "Ready" if desktop.get("supported") else "Unavailable",
            "active_app": active.get("app_name") or "No visible foreground app",
            "active_window": active,
            "windows": desktop.get("windows") or [],
            "desktop_error": desktop.get("error"),
        }
        warning = desktop.get("error") or f"Desktop awareness · {len(values['windows'])} visible window(s)"
        values["warning"] = warning
        return self._ok(values, notice=warning)

    def _file_search(self, action, payload, state):
        path = payload.get("path") or payload.get("selected")
        if isinstance(path, dict):
            path = path.get("path")
        if action == "open":
            result = self._desktop().open_file(str(path or "")).to_dict()
        elif action == "reveal":
            result = self._desktop().show_in_folder(str(path or "")).to_dict()
        else:
            return self._ok(state)
        data = {**state, "app_action": result, "status": result.get("status"), "path": path}
        if result.get("requires_confirmation"):
            confirmation = dict(result.get("confirmation") or {})
            return {"success": False, "error": result.get("error"), "data": data,
                    "confirmation": confirmation,
                    "pending_prompt": (
                        f"The user approved the exact desktop file-open plan. Call app_open_file "
                        f"with path={path!r}, confirmation_id={confirmation.get('confirmation_id')!r}, "
                        "and confirm=true; then report only the verified result."
                    )}
        return self._ok(data, notice=f"{action.title()} verified") if result.get("success") else {
            "success": False, "error": result.get("error") or "Desktop action was not verified", "data": data,
        }

    def _app_launcher(self, action, payload, state):
        if action == "launch":
            selected = self._selected(payload)
            query = selected.get("name") if isinstance(selected, dict) else str(selected or payload.get("query") or "")
            from tools.desktop_models import AppLaunchRequest
            result = self._desktop().open_app(AppLaunchRequest(query)).to_dict()
            data = {"app_action": result, "status": result.get("status"), "target_app": query}
            return self._ok(data, notice=f"{result.get('status')}: {query}") if result.get("success") else {
                "success": False, "error": result.get("error") or f"Application not opened: {query}", "data": data,
            }
        apps = [item.to_dict() for item in self._desktop().discovery.discover()]
        query = str(payload.get("query") or "").lower().strip()
        if query:
            apps = [item for item in apps if query in str(item.get("name", "")).lower() or query in " ".join(item.get("aliases") or []).lower()]
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
            return self._ok({"emails": values, "attachment_profiles": list(result.file_profiles)}, notice=f"{len(values)} email(s)")
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
            elif name in {"discord", "whatsapp"}:
                details = registry.describe(name)
                if not any(item.get("name") == "connection_info" and item.get("available") for item in details.get("capabilities", [])):
                    return {
                        "success": True,
                        "data": {"connectors": [details]},
                        "notice": f"Configure {details.get('display_name') or name} credentials in JARVIS Settings → Connectors.",
                        "open_settings": True,
                    }
                result = registry.execute(name, "connection_info", {})
                if not result.success:
                    return self._error(result.error or f"Unable to connect {name}")
                return self._ok({"connectors": [{**details, "connection": result.data}]}, notice=f"{details.get('display_name') or name} connected")
            elif name:
                return self._error(f"No sign-in flow is registered for {name}")
            else:
                return self._error("Choose a connector first.")
        values = []
        for name in registry.names():
            capabilities = registry.capabilities(name)
            unavailable = [item.get("unavailable_reason") for item in capabilities if not item.get("available") and item.get("unavailable_reason")]
            values.append({
                "name": name,
                "status": registry.status(name).value,
                "description": f"{sum(1 for item in capabilities if item.get('available'))}/{len(capabilities)} capabilities available",
                "limitation": unavailable[0] if unavailable else "",
            })
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
        if action in {"open", "open_file"}:
            selected = payload.get("selected")
            path = payload.get("path") or (selected.get("path") if isinstance(selected, dict) else selected)
            result = self._desktop().open_file(str(path or "")).to_dict()
            data = {**state, "app_action": result, "status": result.get("status"), "path": path}
            if result.get("requires_confirmation"):
                confirmation = dict(result.get("confirmation") or {})
                return {"success": False, "error": result.get("error"), "data": data,
                        "confirmation": confirmation,
                        "pending_prompt": (
                            f"The user approved the exact desktop file-open plan. Call app_open_file "
                            f"with path={path!r}, confirmation_id={confirmation.get('confirmation_id')!r}, "
                            "and confirm=true; then report only the verified result."
                        )}
            return self._ok(data, notice="File open verified") if result.get("success") else {
                "success": False, "error": result.get("error") or "File open was not verified", "data": data,
            }
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
            if not text:
                return self._error("Enter a message and recipient first.")
            return {"success": True, "prompt": f"Send this message now using the explicitly named connected messaging service and recipient. Do not ask for a second confirmation: {text}"}
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

    def _inbox_item(self, action, payload, state):
        from tools.tool_registry import inbox_ingest_file, inbox_scan_downloads

        if action in {"scan", "refresh"}:
            query = str(payload.get("query") or state.get("query") or "assignment worksheet homework")
            source = str(payload.get("source") or state.get("source") or "downloads")
            result = inbox_scan_downloads(query=query, days=3, limit=15, source=source)
            return self._ok(result, notice=f"{len(result.get('candidates') or [])} candidate(s)") if result.get("success") else self._error(result.get("error") or "Inbox scan failed")
        if action == "ingest":
            selected = self._selected(payload)
            path = selected.get("path") if isinstance(selected, dict) else str(selected or "")
            if not path:
                return self._error("Choose an attachment candidate first.")
            result = inbox_ingest_file(path, source=payload.get("source") or state.get("source") or "downloads", message=payload.get("message") or "")
            return self._ok(result, notice="Attachment ingested for review") if result.get("success") else self._error(result.get("error") or "Attachment ingestion failed")
        return self._ok(state)

    def _assignment_analysis(self, action, payload, state):
        from tools.tool_registry import assignment_plan, inbox_ingest_file

        assignment_id = str(payload.get("assignment_id") or state.get("assignment_id") or "")
        if action == "analyze" and payload.get("path"):
            result = inbox_ingest_file(payload["path"], source=payload.get("source") or "manual_import", message=payload.get("message") or "")
            return self._ok(result, notice="Assignment analyzed") if result.get("success") else self._error(result.get("error") or "Analysis failed")
        if action == "plan":
            if not assignment_id:
                return self._error("Ingest an attachment before creating a plan.")
            result = assignment_plan(assignment_id)
            return self._ok(result.get("plan") or result, notice="Assignment plan created") if result.get("success") else self._error(result.get("error") or "Planning failed")
        return self._ok(state)

    def _assignment_plan(self, action, payload, state):
        from tools.tool_registry import assignment_draft, assignment_plan

        assignment_id = str(payload.get("assignment_id") or state.get("assignment_id") or "")
        if not assignment_id:
            return self._error("No assignment id is available.")
        if action == "draft":
            result = assignment_draft(assignment_id)
            return self._ok(result.get("output") or result, notice="Draft created; review required") if result.get("success") else self._error(result.get("error") or "Draft generation failed")
        result = assignment_plan(assignment_id)
        return self._ok(result.get("plan") or result, notice="Assignment plan refreshed") if result.get("success") else self._error(result.get("error") or "Planning failed")

    def _assignment_draft(self, action, payload, state):
        from tools.tool_registry import assignment_draft, assignment_export

        assignment_id = str(payload.get("assignment_id") or state.get("assignment_id") or (state.get("output") or {}).get("assignment_id") or "")
        if not assignment_id:
            return self._error("No assignment id is available.")
        if action == "export":
            result = assignment_export(assignment_id, output_format="docx")
            return self._ok(result, notice="DOCX exported; not submitted") if result.get("success") else self._error(result.get("error") or "Export failed")
        result = assignment_draft(assignment_id, response_text=payload.get("text") or "")
        if not result.get("success"):
            return self._error(result.get("error") or "Draft generation failed")
        output = result.get("output") or {}
        draft_path = Path(str(output.get("draft_path") or ""))
        draft = draft_path.read_text(encoding="utf-8") if draft_path.is_file() else ""
        return self._ok({**output, "draft": draft, "assignment_id": assignment_id}, notice="Draft generated; review before submission")

    def _source_files(self, action, payload, state):
        selected = self._selected(payload)
        if action == "open" and selected:
            path = selected.get("local_path") or selected.get("path") if isinstance(selected, dict) else str(selected)
            target = Path(str(path or ""))
            if not target.is_file():
                return self._error("Choose an existing local source file.")
            result = self._desktop().open_file(str(target)).to_dict()
            data = {"files": state.get("files") or [], "app_action": result, "opened": str(target) if result.get("success") else None}
            if result.get("requires_confirmation"):
                confirmation = dict(result.get("confirmation") or {})
                return {"success": False, "error": result.get("error"), "data": data,
                        "confirmation": confirmation,
                        "pending_prompt": (
                            f"The user approved the exact desktop file-open plan. Call app_open_file "
                            f"with path={str(target)!r}, confirmation_id={confirmation.get('confirmation_id')!r}, "
                            "and confirm=true; then report only the verified result."
                        )}
            return self._ok(data, notice="Source open verified") if result.get("success") else {
                "success": False, "error": result.get("error") or "Source open was not verified", "data": data,
            }
        return self._ok({"files": state.get("files") or state.get("attachments") or []}, notice="Assignment sources synchronized")

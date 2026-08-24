"""Visible, reversible Windows Startup-folder integration for JARVIS."""

from __future__ import annotations

import os
import platform
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.desktop_context_models import StartupConfig


class DesktopStartupManager:
    ENTRY_NAME = "JARVIS Background Assistant.cmd"

    def __init__(self, *, startup_dir: Path | str | None = None,
                 python_executable: Path | str | None = None,
                 project_root: Path | str | None = None,
                 platform_name: str | None = None):
        appdata = os.environ.get("APPDATA", "")
        self.startup_dir = Path(startup_dir) if startup_dir else Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        self.python_executable = Path(python_executable or sys.executable)
        pythonw = self.python_executable.with_name("pythonw.exe")
        if pythonw.is_file():
            self.python_executable = pythonw
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1])
        self.platform_name = platform_name or platform.system()
        self._pending: dict[str, dict] = {}

    @property
    def entry_path(self) -> Path:
        return self.startup_dir / self.ENTRY_NAME

    def status(self) -> StartupConfig:
        supported = self.platform_name.lower() == "windows" and bool(str(self.startup_dir))
        enabled = supported and self.entry_path.is_file()
        evidence = []
        if enabled:
            evidence.append("JARVIS entry exists in the current user's Windows Startup folder")
        else:
            evidence.append("no JARVIS Startup-folder entry found")
        return StartupConfig(enabled, supported, str(self.entry_path) if supported else None,
                             evidence=tuple(evidence),
                             error=None if supported else f"Windows Startup integration is unavailable on {self.platform_name}.")

    def enable_plan(self) -> dict:
        status = self.status()
        if not status.supported:
            return {"success": False, "error": status.error, "status": status.to_dict()}
        token = uuid.uuid4().hex
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        self._pending[token] = {"expires": expires, "target": str(self.entry_path)}
        return {
            "success": True,
            "plan": {
                "confirmation_id": token,
                "action": "enable_start_with_windows",
                "target": str(self.entry_path),
                "method": "current-user Windows Startup folder",
                "command_summary": "launch JARVIS minimized with no hidden recording or input capture",
                "risk": "persistent background startup",
                "requires_confirmation": True,
                "expires_at": expires.isoformat(),
                "reversible_by": "desktop_startup_disable",
            },
        }

    def enable_confirmed(self, confirmation_id: str, *, confirm: bool = False) -> dict:
        pending = self._pending.get(str(confirmation_id))
        if not confirm or not pending or pending["expires"] < datetime.now(timezone.utc):
            self._pending.pop(str(confirmation_id), None)
            return {"success": False, "status": "confirmation_required", "requires_confirmation": True,
                    "error": "A valid, unexpired startup plan and explicit confirmation are required."}
        self._pending.pop(str(confirmation_id), None)
        try:
            content = self._entry_content()
            self.startup_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.entry_path.with_suffix(".cmd.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(self.entry_path)
        except Exception as exc:
            return {"success": False, "error": f"Could not enable JARVIS startup: {exc}", "status": self.status().to_dict()}
        status = self.status()
        return {"success": status.enabled, "status": status.to_dict(),
                "evidence": list(status.evidence), "error": None if status.enabled else "Startup entry could not be verified."}

    def disable(self, *, confirm: bool = False) -> dict:
        if not confirm:
            return {"success": False, "status": "confirmation_required", "requires_confirmation": True,
                    "error": "Disabling persistent startup requires confirmation.",
                    "confirmation": {"action": "Disable start with Windows", "target": str(self.entry_path), "risk": "changes persistent startup behavior"}}
        try:
            existed = self.entry_path.is_file()
            if existed:
                self.entry_path.unlink()
        except Exception as exc:
            return {"success": False, "error": f"Could not disable JARVIS startup: {exc}", "status": self.status().to_dict()}
        status = self.status()
        return {"success": not status.enabled, "removed": existed, "status": status.to_dict()}

    def _entry_content(self) -> str:
        values = (str(self.python_executable.resolve()), str(self.project_root.resolve()))
        if any(any(character in value for character in ('\r', '\n', '%', '"')) for value in values):
            raise ValueError("Startup command paths contain characters unsafe for a Windows command file")
        python, root = values
        return (
            "@echo off\n"
            f"cd /d \"{root}\"\n"
            f"start \"\" /min \"{python}\" -m app.jarvis_ui --start-minimized\n"
        )


_startup_manager: DesktopStartupManager | None = None


def get_startup_manager() -> DesktopStartupManager:
    global _startup_manager
    if _startup_manager is None:
        _startup_manager = DesktopStartupManager()
    return _startup_manager


def set_startup_manager(manager: DesktopStartupManager | None) -> None:
    global _startup_manager
    _startup_manager = manager


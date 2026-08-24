"""Read-only installed-app discovery and launch connector."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

from connectors.base import Connector, ConnectorCapability, ConnectorResult, ConnectorStatus


_COMMON = {
    "chrome": ["chrome.exe", "chrome"],
    "edge": ["msedge.exe", "msedge"],
    "discord": ["Discord.exe", "discord"],
    "whatsapp": ["WhatsApp.exe", "whatsapp"],
    "premiere": ["Adobe Premiere Pro.exe"],
    "vs code": ["Code.exe", "code"],
    "spotify": ["Spotify.exe", "spotify"],
}


def discover_common_apps() -> dict[str, dict]:
    values: dict[str, dict] = {}
    try:
        from tools.app_launcher import load_apps

        for key, item in load_apps().items():
            if isinstance(item, dict) and item.get("target"):
                values[str(key).lower()] = dict(item)
    except Exception:
        pass
    program_files = Path(os.getenv("PROGRAMFILES") or "C:/Program Files")
    program_files_x86 = Path(os.getenv("PROGRAMFILES(X86)") or "C:/Program Files (x86)")
    local_app_data = Path(os.getenv("LOCALAPPDATA") or "")
    app_data = Path(os.getenv("APPDATA") or "")
    known_paths = {
        "chrome": [program_files / "Google/Chrome/Application/chrome.exe", program_files_x86 / "Google/Chrome/Application/chrome.exe"],
        "edge": [program_files / "Microsoft/Edge/Application/msedge.exe", program_files_x86 / "Microsoft/Edge/Application/msedge.exe"],
        "discord": [local_app_data / "Discord/Discord.exe"],
        "whatsapp": [local_app_data / "WhatsApp/WhatsApp.exe"],
        "vs code": [local_app_data / "Programs/Microsoft VS Code/Code.exe", program_files / "Microsoft VS Code/Code.exe"],
        "spotify": [app_data / "Spotify/Spotify.exe"],
    }
    for name, executables in _COMMON.items():
        if name in values:
            continue
        target = next((shutil.which(exe) for exe in executables if shutil.which(exe)), None)
        if target:
            values[name] = {"name": name.title(), "target": target, "source": "path"}
            continue
        candidates = list(known_paths.get(name, ()))
        if name == "premiere":
            try:
                candidates.extend(program_files.glob("Adobe/Adobe Premiere Pro */Adobe Premiere Pro.exe"))
            except OSError:
                pass
        target_path = next((path for path in candidates if path.is_file()), None)
        if target_path:
            values[name] = {"name": name.title(), "target": str(target_path), "source": "common_path"}
    return values


class AppLauncherConnector(Connector):
    name = "app_launcher"
    display_name = "Application Launcher"

    def __init__(self, apps: dict[str, dict] | None = None, *, launcher: Callable[[str], object] | None = None):
        self.apps = {str(key).lower(): dict(value) for key, value in (apps if apps is not None else discover_common_apps()).items()}
        if launcher is None:
            from tools.app_launcher import launch_app

            launcher = launch_app
        self._launcher = launcher

    def status(self) -> ConnectorStatus:
        return ConnectorStatus.READY if self.apps else ConnectorStatus.UNAVAILABLE

    def capabilities(self) -> list[ConnectorCapability]:
        available = bool(self.apps)
        reason = "No installed applications were discovered in the local app index or common paths" if not available else ""
        return [
            ConnectorCapability("list_apps", "List discovered local applications", available=available, unavailable_reason=reason),
            ConnectorCapability("search_apps", "Search discovered local applications", available=available, unavailable_reason=reason, input_schema={"required": ["query"]}),
            ConnectorCapability("open_app", "Launch a discovered local application", available=available, unavailable_reason=reason, input_schema={"required": ["query"]}),
        ]

    def _matches(self, query: str) -> list[dict]:
        lowered = str(query or "").lower().strip()
        values = []
        for key, item in self.apps.items():
            label = str(item.get("name") or key)
            aliases = " ".join(str(value) for value in item.get("aliases") or [])
            if not lowered or lowered in key or lowered in label.lower() or lowered in aliases.lower():
                values.append({"id": key, "name": label, "target": item.get("target"), "source": item.get("source") or "local_index"})
        return values

    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        if capability in {"list_apps", "search_apps"}:
            query = str(arguments.get("query") or "") if capability == "search_apps" else ""
            values = self._matches(query)
            return ConnectorResult(True, {"apps": values, "count": len(values)}, connector=self.name, capability=capability)
        if capability == "open_app":
            query = str(arguments.get("query") or "").strip()
            matches = self._matches(query)
            if not matches:
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Application not found: {query}"})
            raw = self._launcher(matches[0]["id"])
            success, launched_name = raw if isinstance(raw, tuple) and len(raw) >= 2 else (bool(raw), matches[0]["name"])
            if not success:
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Application launch failed: {matches[0]['name']}"})
            return ConnectorResult(True, {"name": launched_name or matches[0]["name"], "launch_requested": True, "target": matches[0]["target"]}, connector=self.name, capability=capability)
        return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Unsupported capability: {capability}"})

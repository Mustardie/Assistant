"""Safe local connector for files downloaded by browsers and desktop apps."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterable

from connectors.base import (
    Connector,
    ConnectorActionPlan,
    ConnectorCapability,
    ConnectorRequest,
    ConnectorResult,
    ConnectorStatus,
)
from tools.file_intelligence import FileCategory, FileSource, profile_file, search_file_intent


def discover_download_roots() -> list[Path]:
    candidates = [
        os.getenv("JARVIS_DOWNLOADS_DIR"),
        os.getenv("DOWNLOADS_DIR"),
        str(Path.home() / "Downloads"),
        str(Path.home() / "OneDrive" / "Downloads"),
    ]
    roots: list[Path] = []
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser()
        try:
            path = path.resolve()
        except OSError:
            continue
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _recent_files(roots: Iterable[Path], *, days: float = 7, limit: int = 200, max_entries: int = 5000) -> list[Path]:
    cutoff = time.time() - max(0.0, float(days)) * 86400
    found: list[tuple[float, Path]] = []
    scanned = 0
    stack = [Path(root) for root in roots]
    while stack and scanned < max_entries:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned > max_entries:
                break
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    modified = entry.stat().st_mtime
                    if modified >= cutoff:
                        found.append((modified, entry))
            except OSError:
                continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _modified, path in found[: max(1, min(int(limit), 1000))]]


class BrowserDownloadsConnector(Connector):
    name = "browser_downloads"
    display_name = "Browser Downloads"

    def __init__(self, roots: Iterable[str | Path] | None = None, *, opener: Callable[[str], object] | None = None):
        self.roots = [Path(root).expanduser().resolve() for root in roots] if roots is not None else discover_download_roots()
        self.roots = [root for root in self.roots if root.is_dir()]
        self._opener = opener or getattr(os, "startfile", None)

    def status(self) -> ConnectorStatus:
        return ConnectorStatus.READY if self.roots else ConnectorStatus.UNAVAILABLE

    def capabilities(self) -> list[ConnectorCapability]:
        available = bool(self.roots)
        reason = "No local Downloads folder was found" if not available else ""
        return [
            ConnectorCapability("list_recent", "List recent local downloads", available=available, unavailable_reason=reason, input_schema={"properties": {"days": {"type": "number"}, "limit": {"type": "integer"}}}),
            ConnectorCapability("search_intent", "Search downloads by likely purpose", available=available, unavailable_reason=reason, input_schema={"required": ["query"], "properties": {"query": {"type": "string"}, "days": {"type": "number"}}}),
            ConnectorCapability("profile", "Profile one downloaded file", available=available, unavailable_reason=reason, input_schema={"required": ["path"], "properties": {"path": {"type": "string"}}}),
            ConnectorCapability("open", "Open a downloaded file with its registered application", available=available and self._opener is not None, unavailable_reason="No safe local opener is available" if self._opener is None else reason, risk_level="medium", input_schema={"required": ["path"]}),
            ConnectorCapability("reveal", "Show a downloaded file in its containing folder", available=available and self._opener is not None, unavailable_reason="No safe local opener is available" if self._opener is None else reason, input_schema={"required": ["path"]}),
        ]

    def plan_action(self, request: ConnectorRequest) -> ConnectorActionPlan:
        plan = super().plan_action(request)
        if request.capability == "open" and not plan.missing_inputs:
            profile = profile_file(request.arguments["path"], source=FileSource.BROWSER_DOWNLOAD, include_git=False)
            executable = profile.category == FileCategory.INSTALLER or profile.extension in {".exe", ".msi", ".msix", ".bat", ".cmd", ".ps1"}
            if executable:
                return ConnectorActionPlan(
                    request,
                    plan.status,
                    plan.supported,
                    requires_confirmation=True,
                    may_retry=False,
                    reason="Opening this download can execute code and requires confirmation" if not request.confirmed else "Ready",
                    risk_level="high",
                    expected_result=plan.expected_result,
                    fallback="Reveal the file and inspect its profile instead of running it.",
                )
        return plan

    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        if capability in {"list_recent", "search_intent"}:
            days = float(arguments.get("days", 7))
            limit = max(1, min(int(arguments.get("limit", 50)), 200))
            files = _recent_files(self.roots, days=days, limit=max(limit, 100 if capability == "search_intent" else limit))
            if capability == "search_intent":
                records = [{"path": str(path), "filename": path.name, "modified_time": path.stat().st_mtime} for path in files]
                results = search_file_intent(str(arguments.get("query") or ""), records, limit=limit)
            else:
                results = [profile_file(path, source=FileSource.BROWSER_DOWNLOAD, include_git=False).to_dict() for path in files]
            return ConnectorResult(True, {"roots": [str(root) for root in self.roots], "results": results, "count": len(results)}, connector=self.name, capability=capability)
        path = Path(str(arguments.get("path") or "")).expanduser().resolve()
        if not path.is_file():
            return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Downloaded file does not exist: {path}", "error_code": "not_found"})
        if capability == "profile":
            return ConnectorResult(True, profile_file(path, source=FileSource.BROWSER_DOWNLOAD, include_git=False).to_dict(), connector=self.name, capability=capability)
        if capability in {"open", "reveal"}:
            target = path if capability == "open" else path.parent
            if self._opener is None:
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "No safe local opener is available"})
            self._opener(str(target))
            return ConnectorResult(True, {"path": str(path), "opened": str(target), "executed_file": capability == "open"}, connector=self.name, capability=capability)
        return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Unsupported capability: {capability}"})

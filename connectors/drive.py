"""Google Drive adapter with an honest local-sync fallback."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable

from connectors.base import Connector, ConnectorCapability, ConnectorResult, ConnectorStatus
from tools.file_intelligence import FileSource, profile_file, search_file_intent


def discover_drive_roots() -> list[Path]:
    candidates = [
        os.getenv("GOOGLE_DRIVE_SYNC_DIR"),
        str(Path.home() / "Google Drive"),
        str(Path.home() / "My Drive"),
        str(Path.home() / "Drive"),
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


def _walk_records(roots: Iterable[Path], *, max_entries: int = 5000) -> list[dict]:
    records: list[dict] = []
    stack = [Path(root) for root in roots]
    while stack and len(records) < max_entries:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.is_file():
                    stat = entry.stat()
                    records.append({"path": str(entry), "filename": entry.name, "modified_time": stat.st_mtime, "size": stat.st_size})
            except OSError:
                continue
            if len(records) >= max_entries:
                break
    return records


class GoogleDriveConnector(Connector):
    name = "google_drive"
    display_name = "Google Drive"

    def __init__(
        self,
        backend=None,
        *,
        auth_check: Callable[[], bool] | None = None,
        local_roots: Iterable[str | Path] | None = None,
        opener: Callable[[str], object] | None = None,
    ):
        self.backend = backend
        self._auth_check = auth_check or (lambda: backend is not None)
        self.local_roots = [Path(root).expanduser().resolve() for root in local_roots] if local_roots is not None else discover_drive_roots()
        self.local_roots = [root for root in self.local_roots if root.is_dir()]
        self._opener = opener or getattr(os, "startfile", None)

    def status(self) -> ConnectorStatus:
        if self.local_roots:
            return ConnectorStatus.READY
        if self.backend is not None:
            return ConnectorStatus.READY if self._auth_check() else ConnectorStatus.AUTH_REQUIRED
        return ConnectorStatus.UNAVAILABLE

    def capabilities(self) -> list[ConnectorCapability]:
        local = bool(self.local_roots)
        api = self.backend is not None and bool(self._auth_check())
        searchable = local or api
        unavailable = "Google Drive API is not configured and no local synced Drive folder was found"
        return [
            ConnectorCapability("search_files", "Search Drive API or local synced Drive files by purpose", requires_auth=not local, available=searchable, unavailable_reason=unavailable if not searchable else "", input_schema={"required": ["query"], "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}}),
            ConnectorCapability("read_metadata", "Read Drive/local file metadata", requires_auth=not local, available=searchable, unavailable_reason=unavailable if not searchable else "", input_schema={"required": ["path_or_id"]}),
            ConnectorCapability("open_local", "Open a local synced Drive file", available=local and self._opener is not None, unavailable_reason="No local synced Drive file opener is available" if not (local and self._opener is not None) else "", input_schema={"required": ["path"]}),
            ConnectorCapability("download", "Download a configured Drive API file", requires_auth=True, available=api and hasattr(self.backend, "download"), unavailable_reason="Drive API download backend is not configured", risk_level="medium", input_schema={"required": ["file_id", "destination"]}),
            ConnectorCapability("upload", "Upload a file to Google Drive", mutating=True, requires_confirmation=True, idempotent=False, risk_level="high", requires_auth=True, available=api and hasattr(self.backend, "upload"), unavailable_reason="Drive API upload backend is not configured", input_schema={"required": ["path"]}),
        ]

    def execute(self, capability: str, arguments: dict, *, confirmed: bool = False) -> ConnectorResult:
        if capability == "search_files":
            query = str(arguments.get("query") or "")
            limit = max(1, min(int(arguments.get("limit", 20)), 100))
            local_results = search_file_intent(query, _walk_records(self.local_roots), limit=limit) if self.local_roots else []
            api_results = []
            if self.backend is not None and self._auth_check() and hasattr(self.backend, "search_files"):
                api_results = self.backend.search_files(query=query, limit=limit) or []
            return ConnectorResult(True, {"results": (local_results + list(api_results))[:limit], "local_sync": bool(self.local_roots), "api_configured": bool(self.backend)}, connector=self.name, capability=capability)
        if capability == "read_metadata":
            value = str(arguments.get("path_or_id") or "")
            path = Path(value).expanduser()
            if path.is_file():
                return ConnectorResult(True, profile_file(path, source=FileSource.GOOGLE_DRIVE, include_git=False).to_dict(), connector=self.name, capability=capability)
            if self.backend is not None and self._auth_check() and hasattr(self.backend, "get_metadata"):
                return ConnectorResult.normalize(self.name, capability, self.backend.get_metadata(value))
            return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "Drive item was not found locally and the Drive API is not configured"})
        if capability == "open_local":
            path = Path(str(arguments.get("path") or "")).expanduser().resolve()
            if not path.is_file() or not any(root == path.parent or root in path.parents for root in self.local_roots):
                return ConnectorResult.normalize(self.name, capability, {"success": False, "error": "Path is not an existing file inside a configured local Drive root"})
            self._opener(str(path))
            return ConnectorResult(True, {"path": str(path), "launch_requested": True}, connector=self.name, capability=capability)
        if capability in {"download", "upload"} and self.backend is not None and self._auth_check():
            function = getattr(self.backend, capability, None)
            if function is not None:
                return ConnectorResult.normalize(self.name, capability, function(**arguments))
        return ConnectorResult.normalize(self.name, capability, {"success": False, "error": f"Capability '{capability}' is unavailable"})

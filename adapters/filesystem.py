"""FileSystem adapter -- local file capabilities for the universal layer.

Lets workflows read documents, search files, and list a watched folder
without any app connection."""

from __future__ import annotations

import logging
from pathlib import Path

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)


class FileSystemAdapter(BaseAdapter):
    name = "filesystem"
    display_name = "File System"
    description = ("Local files and folders: list, search, read and organize "
                   "documents in Nova's workspace. No setup needed.")
    authentication = "none"
    capabilities = ["read_document", "search", "list_files", "summarize"]

    def __init__(self, root: str | Path = ""):
        super().__init__()
        self.root = Path(root).expanduser() if root else Path.home()

    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        return {"status": "connected",
                "message": f"File system is available (root: {self.root})."}

    def connect(self) -> dict:
        return self._ok(message="File system connected.")

    # ------------------------------------------------------------------ #
    def read_document(self, path, **kwargs):
        from tools.document_tool import extract_text
        return extract_text(path=path)

    def list_files(self, folder=None, **kwargs):
        folder = Path(folder) if folder else self.root
        folder = folder.expanduser()
        if not folder.is_dir():
            return self._fail(f"Not a folder: {folder}")
        files = []
        for child in sorted(folder.iterdir()):
            try:
                if child.is_file():
                    files.append({"name": child.name,
                                  "path": str(child),
                                  "size": child.stat().st_size,
                                  "modified": child.stat().st_mtime})
            except OSError:
                continue
        return self._ok(files=files, count=len(files))

    def search(self, query, folder=None, limit=25, **kwargs):
        folder = Path(folder) if folder else self.root
        matches = []
        for child in folder.rglob("*"):
            if not child.is_file():
                continue
            try:
                name = child.name.lower()
                text = _peek(child)
            except OSError:
                continue
            if query.lower() in name or (text and query.lower() in text.lower()):
                matches.append({"name": child.name, "path": str(child)})
            if len(matches) >= limit:
                break
        return self._ok(results=matches, count=len(matches))

    def summarize(self, path, **kwargs):
        from tools.document_tool import summarize_document
        return summarize_document(path=path)


def _peek(path: Path, limit: int = 4096) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except Exception:
        return ""
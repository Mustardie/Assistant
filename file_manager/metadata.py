from pathlib import Path
from typing import Any, Dict


class FileMetadata:
    def __init__(self, service):
        self.service = service

    def metadata(self, path: str) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return {"success": False, "error": "Path not found"}
        stat = target.stat()
        return {
            "success": True,
            "path": str(target),
            "name": target.name,
            "suffix": target.suffix.lower(),
            "size": stat.st_size,
            "is_file": target.is_file(),
            "is_dir": target.is_dir(),
        }

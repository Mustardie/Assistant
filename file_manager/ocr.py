from pathlib import Path
from typing import Any, Dict


class OcrProcessor:
    def extract(self, path: str) -> Dict[str, Any]:
        target = Path(path).expanduser().resolve()
        return {"success": True, "path": str(target), "text": ""}

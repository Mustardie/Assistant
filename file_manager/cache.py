import json
from pathlib import Path
from typing import Any, Dict, Optional


class FileManagerCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def set(self, key: str, value: Dict[str, Any]) -> None:
        path = self.cache_dir / f"{key}.json"
        path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")

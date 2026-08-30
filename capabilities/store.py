from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable

from config.paths import get_nova_data_dir
from .models import Capability, LearnedSkill


class CapabilityStore:
    """Atomic JSON persistence for learned records and temporary adapters."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else get_nova_data_dir() / "capabilities" / "store.json"
        self._lock = threading.RLock()
        self._data = self._read()

    def _empty(self) -> dict:
        return {"schema_version": self.SCHEMA_VERSION, "capabilities": {}, "skills": {}}

    def _read(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return self._empty()
            value.setdefault("capabilities", {})
            value.setdefault("skills", {})
            return value
        except Exception:
            return self._empty()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def save_capability(self, capability: Capability) -> Capability:
        with self._lock:
            capability.touch()
            self._data["capabilities"][capability.id] = capability.to_dict()
            self._write()
        return capability

    def get_capability(self, capability_id: str) -> Capability | None:
        value = self._data["capabilities"].get(capability_id)
        return Capability.from_dict(value) if value else None

    def capabilities(self) -> list[Capability]:
        return [Capability.from_dict(v) for v in self._data["capabilities"].values()]

    def delete_capability(self, capability_id: str) -> bool:
        with self._lock:
            existed = self._data["capabilities"].pop(capability_id, None) is not None
            if existed:
                self._write()
            return existed

    def save_skill(self, skill: LearnedSkill) -> LearnedSkill:
        with self._lock:
            self._data["skills"][skill.id] = skill.to_dict()
            self._write()
        return skill

    def get_skill(self, skill_id: str) -> LearnedSkill | None:
        value = self._data["skills"].get(skill_id)
        return LearnedSkill.from_dict(value) if value else None

    def skills(self) -> list[LearnedSkill]:
        return [LearnedSkill.from_dict(v) for v in self._data["skills"].values()]

    def replace_capabilities(self, records: Iterable[Capability]) -> None:
        with self._lock:
            self._data["capabilities"] = {item.id: item.to_dict() for item in records}
            self._write()

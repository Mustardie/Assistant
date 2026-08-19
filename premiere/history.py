"""Operation history, revision tracking and undo bookkeeping.

An autonomous editor makes many changes quickly and unattended, so three
guarantees matter more than they would for a human-driven tool:

* **Traceability** -- every executed operation is logged with its parameters,
  result and the timeline revision it produced, so a bad edit can be found.
* **Revision protection** -- a plan can declare the revision it was written
  against. If the timeline moved since (the user dragged a clip, another plan
  ran), execution is refused rather than applied to a state the model never
  saw.
* **Undo** -- executed batches are recorded with their operation counts so
  ``history.undo`` can drive Premiere's own undo stack by the right number of
  steps instead of guessing.

The log lives on disk next to the repo's other state (``data/``) so it
survives a restart, and is capped so it cannot grow without bound.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger("nova.premiere.history")

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "premiere_history.json",
)

MAX_ENTRIES = 500


class History:
    """Append-only log of executed batches, with revision bookkeeping."""

    def __init__(self, path: str = _DEFAULT_PATH, max_entries: int = MAX_ENTRIES):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self._entries: list = []
        self._checkpoints: dict = {}
        self._revision: Optional[str] = None
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._entries = data.get("entries", [])[-self.max_entries:]
            self._checkpoints = data.get("checkpoints", {})
            self._revision = data.get("revision")
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt log must never block editing -- it is diagnostics, not
            # a source of truth. Start clean and say so.
            logger.warning("Premiere history unreadable (%s); starting fresh", exc)
            self._entries, self._checkpoints, self._revision = [], {}, None

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "entries": self._entries[-self.max_entries:],
                        "checkpoints": self._checkpoints,
                        "revision": self._revision,
                    },
                    handle, indent=2,
                )
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("Could not persist Premiere history: %s", exc)

    # ------------------------------------------------------------------
    # Revisions
    # ------------------------------------------------------------------

    @property
    def revision(self) -> Optional[str]:
        with self._lock:
            self._load()
            return self._revision

    def set_revision(self, revision: Optional[str]) -> None:
        with self._lock:
            self._load()
            if revision and revision != self._revision:
                self._revision = revision
                self._save()

    def check_revision(self, expected: Optional[str], actual: Optional[str]) -> None:
        """Refuse to run a plan written against a stale timeline.

        ``actual`` is what the host reports right now. A mismatch means the
        timeline changed after the model looked at it, so clip indices in the
        plan may point at different clips than intended -- exactly the
        situation that produces silent, hard-to-find damage.
        """
        if not expected:
            return
        if actual and expected != actual:
            from premiere.errors import RevisionError
            raise RevisionError(
                "The timeline changed since this plan was written",
                hint="Re-read the timeline with timeline.snapshot and rebuild the "
                     "plan against the current state",
                detail={"plan_revision": expected, "current_revision": actual},
            )

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        ops: list,
        results: list,
        revision: Optional[str] = None,
        label: str = "",
        status: str = "ok",
        undo_steps: int = 0,
    ) -> str:
        """Append one executed batch. Returns its entry id."""
        with self._lock:
            self._load()
            entry_id = uuid.uuid4().hex[:12]
            self._entries.append({
                "id": entry_id,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": label,
                "status": status,
                "revision": revision,
                "undo_steps": undo_steps,
                "ops": [
                    {
                        "op": op.get("op"),
                        "note": op.get("note", ""),
                        "params": _trim(op.get("params", {})),
                    }
                    for op in ops
                ],
                "results": [_trim(r) for r in results],
            })
            if revision:
                self._revision = revision
            del self._entries[:-self.max_entries]
            self._save()
            return entry_id

    def recent(self, limit: int = 25) -> list:
        with self._lock:
            self._load()
            return list(reversed(self._entries[-limit:]))

    def last_undoable(self) -> Optional[dict]:
        """Most recent entry that actually changed the timeline."""
        with self._lock:
            self._load()
            for entry in reversed(self._entries):
                if entry.get("undo_steps"):
                    return entry
            return None

    def pop_undoable(self, count: int = 1) -> list:
        """Take the last ``count`` mutating entries off the log for undo."""
        with self._lock:
            self._load()
            taken = []
            for index in range(len(self._entries) - 1, -1, -1):
                if len(taken) >= count:
                    break
                if self._entries[index].get("undo_steps"):
                    taken.append(self._entries.pop(index))
            if taken:
                self._save()
            return taken

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    def save_checkpoint(self, name: str, payload: dict) -> None:
        with self._lock:
            self._load()
            self._checkpoints[name] = {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                **payload,
            }
            self._save()

    def get_checkpoint(self, name: str) -> Optional[dict]:
        with self._lock:
            self._load()
            return self._checkpoints.get(name)

    def list_checkpoints(self) -> dict:
        with self._lock:
            self._load()
            return dict(self._checkpoints)

    def clear(self) -> None:
        with self._lock:
            self._loaded = True
            self._entries, self._checkpoints, self._revision = [], {}, None
            self._save()


def _trim(value, *, limit: int = 900):
    """Keep the log readable -- a baked 600-keyframe op must not fill the file."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(item, list) and len(item) > 12:
                out[key] = f"<{len(item)} items>"
            else:
                out[key] = _trim(item, limit=limit)
        return out
    if isinstance(value, list):
        if len(value) > 12:
            return f"<{len(value)} items>"
        return [_trim(v, limit=limit) for v in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


history = History()

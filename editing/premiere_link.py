"""Read-only view of the open Premiere project.

Everything here is a *question*, never a change. The structure layer must be
safe to run against a project the user is actively editing, so this module
issues only non-mutating operations from ``premiere.catalog`` and never
activates a sequence, imports, or touches the timeline.

The whole module degrades to "no project information" rather than failing:
Premiere not running, no project open, or the bridge panel closed are all
ordinary states for a footage-analysis run, and each one produces a ``note``
explaining what is missing instead of an exception.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from editing.fingerprint import normalise_path

logger = logging.getLogger("nova.editing.premiere")


@dataclass
class ProjectSnapshot:
    """What the open project contains, indexed for path lookup."""

    available: bool = False
    project_name: str = ""
    project_path: str = ""
    premiere_version: str = ""
    #: normalised media path -> the asset entry Premiere reported
    by_path: dict = field(default_factory=dict)
    #: normalised media path -> sequences confirmed to contain it
    sequences_by_path: dict = field(default_factory=dict)
    sequences: list = field(default_factory=list)
    note: str = ""

    def lookup(self, path: str) -> Optional[dict]:
        return self.by_path.get(normalise_path(path))

    def sequences_for(self, path: str) -> list:
        return list(self.sequences_by_path.get(normalise_path(path), ()))

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "premiere_version": self.premiere_version,
            "asset_count": len(self.by_path),
            "sequences": list(self.sequences),
            "note": self.note,
        }


def _unavailable(note: str) -> ProjectSnapshot:
    return ProjectSnapshot(available=False, note=note)


def snapshot_project(bridge=None, *, include_active_sequence: bool = True) -> ProjectSnapshot:
    """Ask Premiere what it has open.

    ``include_active_sequence`` additionally reads the active sequence's clips
    so assets can be told apart by whether they are already on a timeline.
    Only the *active* sequence is read: confirming membership in every sequence
    would require activating each one in turn, which visibly changes what the
    user is looking at. Non-membership is therefore not proven -- an empty
    sequence list means "not confirmed", not "unused".
    """
    if bridge is None:
        try:
            from premiere.bridge import bridge as default_bridge
        except ImportError as exc:  # pragma: no cover - premiere always ships here
            return _unavailable(f"Premiere layer unavailable: {exc}")
        bridge = default_bridge

    try:
        from premiere.errors import PremiereError
    except ImportError:  # pragma: no cover
        PremiereError = Exception  # type: ignore[assignment]

    health = bridge.health()
    if not health.get("connected"):
        return _unavailable(
            "Premiere bridge not reachable -- footage will be described from "
            "disk only. Open Premiere with the Nova Premiere Bridge panel to "
            "map files to project items."
        )
    if not health.get("project_open", True):
        return _unavailable("Premiere is running but no project is open.")

    snapshot = ProjectSnapshot(available=True)

    try:
        info = bridge.call("project.info", {}) or {}
        snapshot.project_name = str(info.get("name") or "")
        snapshot.project_path = str(info.get("path") or "")
        snapshot.premiere_version = str(info.get("version") or "")
    except PremiereError as exc:
        logger.debug("project.info failed: %s", exc)

    try:
        assets = (bridge.call("project.assets", {"recursive": True}) or {}).get("assets", [])
    except PremiereError as exc:
        return _unavailable(f"Could not read the project's assets: {exc}")

    for entry in assets:
        path = str(entry.get("path") or "")
        if not path:
            continue
        snapshot.by_path[normalise_path(path)] = entry

    try:
        listed = (bridge.call("sequence.list", {}) or {}).get("sequences", [])
        snapshot.sequences = [
            {"name": s.get("name"), "id": s.get("id"), "active": s.get("active")}
            for s in listed
        ]
    except PremiereError as exc:
        logger.debug("sequence.list failed: %s", exc)

    if include_active_sequence:
        _read_active_sequence(bridge, snapshot, PremiereError)

    return snapshot


def _read_active_sequence(bridge, snapshot: ProjectSnapshot, PremiereError) -> None:
    """Record which media the active sequence already uses."""
    try:
        timeline = bridge.call(
            "timeline.snapshot", {"include_effects": False, "include_keyframes": False}
        ) or {}
    except PremiereError as exc:
        logger.debug("timeline.snapshot failed: %s", exc)
        return

    sequence_name = str(timeline.get("sequence") or "")
    if not sequence_name:
        return
    for track in timeline.get("tracks") or []:
        for clip in track.get("clips") or []:
            source = str(clip.get("source_path") or "")
            if not source:
                continue
            key = normalise_path(source)
            names = snapshot.sequences_by_path.setdefault(key, [])
            if sequence_name not in names:
                names.append(sequence_name)


def describe(asset_path: str, snapshot: ProjectSnapshot):
    """Build the ``PremiereRef`` for one file against a project snapshot."""
    from editing.schema import PremiereRef

    if not snapshot.available:
        return PremiereRef(matched=False, note=snapshot.note or "Premiere not consulted")

    entry = snapshot.lookup(asset_path)
    if entry is None:
        return PremiereRef(
            matched=False,
            project=snapshot.project_name,
            note="Not imported into the open Premiere project",
        )
    return PremiereRef(
        matched=True,
        project=snapshot.project_name,
        item_name=str(entry.get("name") or ""),
        bin=str(entry.get("bin") or ""),
        media_type=str(entry.get("media_type") or ""),
        sequences=snapshot.sequences_for(asset_path),
        note="",
    )

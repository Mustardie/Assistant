"""What a review package is, as data.

A run produces a run folder with six sub-directories, forty JSON files and two
reports. That is inspectable and unusable: knowing where the retention
comparison lives is a thing you have to learn. This package is the answer --
one folder per run, holding the small readable things and pointing at the large
ones, with an index that reads top to bottom.

## What goes in it, and what only gets pointed at

**Copied in**: every text and JSON report small enough to be worth having
beside the index. They are already derived and disposable, so a duplicate costs
nothing and makes the folder self-contained.

**Pointed at**: the video. A proxy is hundreds of megabytes and copying one to
make a folder tidy would be an unkind thing to do to a disk.

## What the index is for

Five questions, in this order, because that is the order somebody asks them:

1. what video was produced?
2. what changed in this edit?
3. what should I watch for?
4. where are the weak points?
5. what needs me to decide?

Every one of those is a list on the package, filled by ``build.py`` from what
the run actually recorded. Nothing in here is generated prose about quality.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from editing.schema import _slug, as_float, as_text_list

#: What kind of thing an item is. Drives how the index groups them.
ITEM_KINDS = (
    "video",       # the thing to watch
    "subtitles",   # the caption sidecar, to load beside it
    "notes",       # timestamped review notes
    "report",      # a readable text report
    "plan",        # machine-readable JSON
    "log",         # the run log
)

#: Said on every review package.
NOT_A_VERDICT = (
    "Nothing in this package says the edit is good. It says what was done, "
    "what was refused, and what is worth checking. The only way to know "
    "whether the edit works is to watch it."
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


@dataclass
class ReviewItem:
    """One file worth opening, and why."""

    name: str = ""
    title: str = ""
    kind: str = "report"
    #: Where the file actually is.
    path: str = ""
    #: Its copy inside the review folder, when it was small enough to copy.
    copied_to: str = ""
    exists: bool = False
    size_bytes: int = 0
    #: One line saying what a person would open this for.
    note: str = ""

    @property
    def open_path(self) -> str:
        """The path to give somebody: the local copy when there is one."""
        return self.copied_to or self.path

    def to_dict(self) -> dict:
        data = asdict(self)
        data["open_path"] = self.open_path
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewItem":
        data = data or {}
        kind = _slug(data.get("kind"))
        return cls(
            name=_text(data.get("name"), 80),
            title=_text(data.get("title"), 200),
            kind=kind if kind in ITEM_KINDS else "report",
            path=_text(data.get("path"), 500),
            copied_to=_text(data.get("copied_to"), 500),
            exists=bool(data.get("exists")),
            size_bytes=int(as_float(data.get("size_bytes"))),
            note=_text(data.get("note"), 400),
        )


@dataclass
class ReviewPackage:
    """Everything one run left behind, gathered into one folder."""

    run_id: str = ""
    style: str = ""
    footage_folder: str = ""
    sequence_name: str = ""
    run_status: str = ""
    created_at: str = ""
    #: Where the package itself lives.
    folder: str = ""

    #: The video, when there is one.
    video: str = ""
    video_exists: bool = False
    video_duration: float = 0.0
    video_size_mb: float = 0.0

    items: list[ReviewItem] = field(default_factory=list)

    #: The five lists the index is built from.
    headline: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    watch_for: list[str] = field(default_factory=list)
    weak_points: list[str] = field(default_factory=list)
    decisions_needed: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)
    #: The reliability report's own summary, so the index can lead with it.
    checks: dict = field(default_factory=dict)
    #: How to get back to the underlying commands.
    commands: list[str] = field(default_factory=list)
    schema_version: int = 1

    def item(self, name: str) -> Optional[ReviewItem]:
        for entry in self.items:
            if entry.name == name:
                return entry
        return None

    def of_kind(self, *kinds: str) -> list[ReviewItem]:
        wanted = set(kinds)
        return [entry for entry in self.items if entry.kind in wanted]

    @property
    def present(self) -> list[ReviewItem]:
        return [entry for entry in self.items if entry.exists]

    @property
    def missing(self) -> list[ReviewItem]:
        return [entry for entry in self.items if not entry.exists]

    def stats(self) -> dict:
        return {
            "items": len(self.items),
            "present": len(self.present),
            "missing": len(self.missing),
            "has_video": bool(self.video_exists),
            "watch_for": len(self.watch_for),
            "weak_points": len(self.weak_points),
            "decisions_needed": len(self.decisions_needed),
            "checks_status": self.checks.get("status", "unknown"),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "style": self.style,
            "footage_folder": self.footage_folder,
            "sequence_name": self.sequence_name,
            "run_status": self.run_status,
            "created_at": self.created_at,
            "folder": self.folder,
            "video": self.video,
            "video_exists": self.video_exists,
            "video_duration": round(self.video_duration, 2),
            "video_size_mb": round(self.video_size_mb, 2),
            "stats": self.stats(),
            "not_a_verdict": NOT_A_VERDICT,
            "headline": list(self.headline),
            "changed": list(self.changed),
            "watch_for": list(self.watch_for),
            "weak_points": list(self.weak_points),
            "decisions_needed": list(self.decisions_needed),
            "warnings": list(self.warnings),
            "checks": dict(self.checks),
            "commands": list(self.commands),
            "items": [entry.to_dict() for entry in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewPackage":
        data = data or {}
        return cls(
            run_id=_text(data.get("run_id"), 120),
            style=_text(data.get("style"), 80),
            footage_folder=_text(data.get("footage_folder"), 500),
            sequence_name=_text(data.get("sequence_name"), 200),
            run_status=_text(data.get("run_status"), 40),
            created_at=_text(data.get("created_at"), 40),
            folder=_text(data.get("folder"), 500),
            video=_text(data.get("video"), 500),
            video_exists=bool(data.get("video_exists")),
            video_duration=as_float(data.get("video_duration")),
            video_size_mb=as_float(data.get("video_size_mb")),
            items=[
                ReviewItem.from_dict(entry)
                for entry in (data.get("items") or [])
                if isinstance(entry, dict)
            ],
            headline=as_text_list(data.get("headline"), limit=40),
            changed=as_text_list(data.get("changed"), limit=40),
            watch_for=as_text_list(data.get("watch_for"), limit=40),
            weak_points=as_text_list(data.get("weak_points"), limit=40),
            decisions_needed=as_text_list(
                data.get("decisions_needed"), limit=40),
            warnings=as_text_list(data.get("warnings"), limit=80),
            checks=dict(data.get("checks") or {}),
            commands=as_text_list(data.get("commands"), limit=40),
        )

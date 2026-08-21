"""What a local asset library is, as data.

Session 5 produces placeholders: *a whoosh belongs at 41.2s*, *a tension bed
belongs under this stretch*. Every one of them is honest and none of them makes
a sound. This package is the bridge, and its whole design follows from one
sentence in the brief:

    Bad silence is better than random annoying SFX.

So every record here is built to make **refusing** cheap and legible. An
``AssetMatch`` carries the reasons it scored what it scored *and* the reasons
candidates were thrown out. An ``AssetPlacement`` has five possible outcomes,
four of which place nothing, and each one names which rule stopped it. A
library with no files in it produces a complete, valid, entirely empty plan
rather than an error — because "you have no impact sounds" is a useful report,
and a crash is not.

Three invariants:

* **An asset is never modified.** Everything here reads files and writes JSON
  about them. Trimming, gain and fades are expressed as Premiere operations on
  a *placed clip*, never as edits to the source file.
* **Provenance survives.** A tag knows whether it came from a folder name, a
  filename or a sidecar, so ``assets show`` can explain why a match happened.
* **Unreadable metadata is a flag, not a failure.** A malformed sidecar marks
  its asset ``needs_review`` and takes it out of automatic placement. The
  indexer never raises because a user typed bad JSON.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from editing.schema import _slug, as_float, as_str_list, clamp01, short_hash

#: What kind of file this is, as far as placement is concerned.
MEDIA_TYPES = ("audio", "image", "video", "mogrt", "unknown")

#: What the asset is *for*. Inferred from the folder, overridable by sidecar.
CATEGORIES = (
    "music", "sfx", "ambience", "callout", "title", "transition", "other",
)

INTENSITIES = ("low", "medium", "high")

#: Where a tag came from. Kept because "why did this match?" is the question
#: users ask first, and "the folder is called sfx/impacts" is a better answer
#: than a bare list of strings.
TAG_SOURCES = ("folder", "filename", "sidecar", "probe", "manual")

#: File extensions the indexer will pick up, by media type.
AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm")
MOGRT_EXTENSIONS = (".mogrt",)

SUPPORTED_EXTENSIONS = (
    AUDIO_EXTENSIONS + IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + MOGRT_EXTENSIONS
)

#: How an individual placement ended up.
PLACEMENT_STATUSES = (
    "placed",        # a real asset, real operations
    "marker_only",   # honest placeholder: no asset, or none good enough
    "missing",       # the library has nothing of this kind at all
    "rejected",      # candidates existed and every one was ruled out
    "unsafe",        # a good match, refused by a placement safety rule
)

#: Statuses that put nothing on the timeline but a note.
NON_PLACING = frozenset({"marker_only", "missing", "rejected", "unsafe"})

#: Why a placement was refused or softened.
PLACEMENT_RISKS = (
    "no_asset", "low_score", "not_safe_for_auto", "needs_review",
    "sfx_spam", "stacked_audio", "over_dialogue", "hud_risk",
    "too_many_overlays", "duration_mismatch", "style_excluded",
    "repeated_use", "file_missing", "unsupported_media",
)


def _coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def _coerce_many(value: Any, allowed: Sequence[str]) -> list[str]:
    out: list[str] = []
    for item in as_str_list(value, limit=60):
        token = _slug(item)
        if token in allowed and token not in out:
            out.append(token)
    return out


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssetTag:
    """One tag, and where it came from.

    Frozen and hashable so a tag set deduplicates naturally while keeping the
    strongest provenance for each name.
    """

    name: str
    source: str = "filename"
    confidence: float = 0.6

    def to_dict(self) -> dict:
        return {"name": self.name, "source": self.source,
                "confidence": round(self.confidence, 3)}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["AssetTag"]:
        if isinstance(data, str):
            name = _slug(data)
            return cls(name=name) if name else None
        if not isinstance(data, dict):
            return None
        name = _slug(data.get("name"))
        if not name:
            return None
        return cls(
            name=name,
            source=_coerce_one(data.get("source"), TAG_SOURCES, "manual"),
            confidence=clamp01(data.get("confidence", 0.6), 0.6),
        )


def merge_tags(*groups: Sequence[AssetTag]) -> list[AssetTag]:
    """Combine tag groups, keeping the most confident source for each name."""
    best: dict = {}
    for group in groups:
        for tag in group:
            existing = best.get(tag.name)
            if existing is None or tag.confidence > existing.confidence:
                best[tag.name] = tag
    return [best[name] for name in sorted(best)]


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

@dataclass
class AssetItem:
    """One file in the library, and everything known about it."""

    asset_id: str
    path: str
    filename: str
    media_type: str = "unknown"
    category: str = "other"
    tags: list[AssetTag] = field(default_factory=list)

    duration: Optional[float] = None
    loudness_db: Optional[float] = None
    bpm: Optional[float] = None
    loopable: bool = False
    intensity: str = "medium"
    moods: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)

    #: Style presets this asset is meant for / must never be used in.
    preferred_styles: list[str] = field(default_factory=list)
    avoid_styles: list[str] = field(default_factory=list)

    #: Source trim, from the sidecar. Applied as in/out on the placed clip.
    start_offset: float = 0.0
    end_offset: Optional[float] = None
    #: Level correction for this specific file, in dB.
    volume_adjust_db: Optional[float] = None

    usage_notes: str = ""
    license_notes: str = ""
    notes: str = ""

    #: False takes the asset out of automatic placement entirely. The default
    #: is True for ordinary files and forced False by anything the indexer
    #: could not read, so "unknown" always means "do not place".
    safe_for_auto: bool = True
    #: True when a sidecar was unreadable or a required field made no sense.
    needs_review: bool = False
    review_reason: str = ""
    #: True when the file has gone since the last index.
    missing: bool = False

    size_bytes: int = 0
    fingerprint: str = ""
    indexed_at: str = ""
    #: True when a ``.asset.json`` was found and parsed.
    has_sidecar: bool = False

    @property
    def tag_names(self) -> frozenset:
        """The flat set matching works against."""
        return frozenset(tag.name for tag in self.tags)

    @property
    def is_audio(self) -> bool:
        return self.media_type == "audio"

    @property
    def is_visual(self) -> bool:
        return self.media_type in ("image", "video")

    @property
    def usable(self) -> bool:
        """Whether this may be placed automatically at all.

        Three separate ways to be unusable, and they are kept separate on
        purpose: the report tells a user whether to fix a sidecar, restore a
        file, or flip a flag.
        """
        return (
            self.safe_for_auto and not self.needs_review and not self.missing
        )

    @property
    def effective_duration(self) -> Optional[float]:
        """Duration after the sidecar's trim, when the duration is known."""
        if self.duration is None:
            return None
        end = self.end_offset if self.end_offset is not None else self.duration
        return max(0.0, min(end, self.duration) - max(0.0, self.start_offset))

    def has_any_tag(self, wanted: Sequence[str]) -> bool:
        return bool(self.tag_names & set(wanted))

    def matching_tags(self, wanted: Sequence[str]) -> list[str]:
        return sorted(self.tag_names & set(wanted))

    def summary(self) -> str:
        marks = "".join([
            "!" if self.needs_review else "",
            "x" if self.missing else "",
            "-" if not self.safe_for_auto else "",
        ]) or "+"
        length = f"{self.duration:6.2f}s" if self.duration is not None else "   ?   "
        return (
            f"{marks} {self.category:<10} {self.media_type:<6} {length} "
            f"{self.intensity:<6} {self.filename[:38]:<38} "
            f"{','.join(sorted(self.tag_names)[:5])}"
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["tags"] = [tag.to_dict() for tag in self.tags]
        data["tag_names"] = sorted(self.tag_names)
        data["usable"] = self.usable
        data["effective_duration"] = (
            round(self.effective_duration, 3)
            if self.effective_duration is not None else None
        )
        for key in ("duration", "loudness_db", "bpm", "volume_adjust_db",
                    "end_offset"):
            value = getattr(self, key)
            data[key] = round(value, 3) if value is not None else None
        data["start_offset"] = round(self.start_offset, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AssetItem":
        tags = [AssetTag.from_dict(entry) for entry in (data.get("tags") or [])]
        return cls(
            asset_id=str(data.get("asset_id") or ""),
            path=str(data.get("path") or ""),
            filename=str(data.get("filename") or ""),
            media_type=_coerce_one(data.get("media_type"), MEDIA_TYPES, "unknown"),
            category=_coerce_one(data.get("category"), CATEGORIES, "other"),
            tags=[tag for tag in tags if tag is not None],
            duration=_opt_float(data.get("duration")),
            loudness_db=_opt_float(data.get("loudness_db")),
            bpm=_opt_float(data.get("bpm")),
            loopable=bool(data.get("loopable")),
            intensity=_coerce_one(data.get("intensity"), INTENSITIES, "medium"),
            moods=as_str_list(data.get("moods"), limit=30),
            styles=as_str_list(data.get("styles"), limit=30),
            preferred_styles=as_str_list(data.get("preferred_styles"), limit=20),
            avoid_styles=as_str_list(data.get("avoid_styles"), limit=20),
            start_offset=max(0.0, as_float(data.get("start_offset"))),
            end_offset=_opt_float(data.get("end_offset")),
            volume_adjust_db=_opt_float(data.get("volume_adjust_db")),
            usage_notes=str(data.get("usage_notes") or "")[:600],
            license_notes=str(data.get("license_notes") or "")[:600],
            notes=str(data.get("notes") or "")[:600],
            safe_for_auto=bool(data.get("safe_for_auto", True)),
            needs_review=bool(data.get("needs_review")),
            review_reason=str(data.get("review_reason") or "")[:600],
            missing=bool(data.get("missing")),
            size_bytes=int(as_float(data.get("size_bytes"))),
            fingerprint=str(data.get("fingerprint") or ""),
            indexed_at=str(data.get("indexed_at") or ""),
            has_sidecar=bool(data.get("has_sidecar")),
        )


def asset_id_for(path: str) -> str:
    """Stable identity for a library file, from its normalised path."""
    from editing.fingerprint import normalise_path

    return "as_" + short_hash(normalise_path(path))


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------

@dataclass
class AssetLibrary:
    """Everything indexed under one asset root."""

    root: str = ""
    items: list[AssetItem] = field(default_factory=list)
    generated_at: str = ""
    #: Folders scanned, by category.
    folders: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Files seen but not indexed, with the reason. Kept so a user can find out
    #: why their .aiff is not showing up without reading the source.
    skipped: list[dict] = field(default_factory=list)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.items)

    def by_id(self, asset_id: str) -> Optional[AssetItem]:
        for item in self.items:
            if item.asset_id == asset_id:
                return item
        return None

    def find(self, needle: str) -> list[AssetItem]:
        """Assets whose id, filename or tags match a substring."""
        token = (needle or "").strip().lower()
        if not token:
            return list(self.items)
        exact = [
            item for item in self.items
            if item.asset_id == needle or item.filename.lower() == token
        ]
        if exact:
            return exact
        return [
            item for item in self.items
            if token in item.filename.lower()
            or token in item.path.lower()
            or any(token in name for name in item.tag_names)
        ]

    def of_category(self, *categories: str) -> list[AssetItem]:
        wanted = set(categories)
        return [item for item in self.items if item.category in wanted]

    def usable(self) -> list[AssetItem]:
        return [item for item in self.items if item.usable]

    def needing_review(self) -> list[AssetItem]:
        return [item for item in self.items if item.needs_review]

    def missing(self) -> list[AssetItem]:
        return [item for item in self.items if item.missing]

    def stats(self) -> dict:
        by_category: dict = {}
        by_media: dict = {}
        for item in self.items:
            by_category[item.category] = by_category.get(item.category, 0) + 1
            by_media[item.media_type] = by_media.get(item.media_type, 0) + 1
        known = [item.duration for item in self.items if item.duration is not None]
        return {
            "total": len(self.items),
            "usable": len(self.usable()),
            "needs_review": len(self.needing_review()),
            "missing": len(self.missing()),
            "with_sidecar": sum(1 for item in self.items if item.has_sidecar),
            "with_duration": len(known),
            "by_category": by_category,
            "by_media_type": by_media,
            "skipped": len(self.skipped),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "root": self.root,
            "folders": dict(self.folders),
            "stats": self.stats(),
            "warnings": list(self.warnings),
            "skipped": [dict(entry) for entry in self.skipped],
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AssetLibrary":
        return cls(
            root=str(data.get("root") or ""),
            items=[AssetItem.from_dict(entry) for entry in (data.get("items") or [])],
            generated_at=str(data.get("generated_at") or ""),
            folders=dict(data.get("folders") or {}),
            warnings=as_str_list(data.get("warnings"), limit=200),
            skipped=[
                dict(entry) for entry in (data.get("skipped") or [])
                if isinstance(entry, dict)
            ],
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass
class AssetMatch:
    """One asset considered for one placeholder, and how it scored.

    Losers are kept as well as winners. "Why did it pick that whoosh?" and
    "why did it not pick my whoosh?" are the same question asked from two
    sides, and only keeping both sides answers it.
    """

    asset_id: str
    filename: str = ""
    score: float = 0.0
    #: Named, signed contributions -- ``[("tag overlap: impact, boom", 0.2)]``.
    reasons: list = field(default_factory=list)
    #: Why it was ruled out entirely. Non-empty means score is meaningless.
    rejected: str = ""
    rank: int = 0

    @property
    def accepted(self) -> bool:
        return not self.rejected

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "filename": self.filename,
            "score": round(self.score, 3),
            "reasons": [
                {"why": why, "delta": round(float(delta), 3)}
                for why, delta in self.reasons
            ],
            "rejected": self.rejected,
            "rank": self.rank,
            "accepted": self.accepted,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AssetMatch":
        return cls(
            asset_id=str(data.get("asset_id") or ""),
            filename=str(data.get("filename") or ""),
            score=as_float(data.get("score")),
            reasons=[
                (str(entry.get("why") or ""), as_float(entry.get("delta")))
                for entry in (data.get("reasons") or [])
                if isinstance(entry, dict)
            ],
            rejected=str(data.get("rejected") or "")[:600],
            rank=int(as_float(data.get("rank"))),
        )


# ---------------------------------------------------------------------------
# Placements
# ---------------------------------------------------------------------------

@dataclass
class AssetPlacement:
    """What happens at one Session 5 placeholder, and why.

    Exactly one of five outcomes, and four of them place nothing. The whole
    point of the record is that the four quiet outcomes are as legible as the
    loud one.
    """

    placement_id: str
    #: The Session 5 layer item this realises.
    item_id: str = ""
    kind: str = "impact_sfx"
    layer: str = "audio"
    start: float = 0.0
    end: float = 0.0
    #: The chosen asset, when one was chosen.
    library_asset_id: str = ""
    asset_path: str = ""
    asset_filename: str = ""
    track: str = ""
    status: str = "marker_only"
    reason: str = ""
    risks: list[str] = field(default_factory=list)
    #: Everything considered, best first. Kept even when nothing was chosen.
    candidates: list[AssetMatch] = field(default_factory=list)
    premiere_ops: list[dict] = field(default_factory=list)
    #: Placement detail: gain, fades, loop count, source in/out, position.
    payload: dict = field(default_factory=dict)
    style: str = ""
    notes: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_placed(self) -> bool:
        return self.status == "placed" and bool(self.premiere_ops)

    @property
    def is_marker(self) -> bool:
        return bool(self.premiere_ops) and all(
            str(op.get("op")) == "marker.add" for op in self.premiere_ops
        )

    @property
    def best(self) -> Optional[AssetMatch]:
        return self.candidates[0] if self.candidates else None

    def refuse(
        self,
        status: str,
        reason: str,
        *,
        risk: str = "",
        keep_asset: bool = False,
    ) -> "AssetPlacement":
        """Fall back to a note. Never silently drop the placeholder.

        ``keep_asset`` is for the cases where an asset *was* chosen and
        deliberately not placed -- a markers-only pass, or a template this
        system cannot drive. The marker then names the file it would have
        used, which is the entire value of those modes; clearing it would
        leave a note saying only that something was wanted.
        """
        self.status = status if status in PLACEMENT_STATUSES else "rejected"
        self.reason = reason
        if not keep_asset:
            self.library_asset_id = ""
            self.asset_path = ""
            self.asset_filename = ""
        self.track = ""
        self.premiere_ops = []
        if risk and risk not in self.risks:
            self.risks.append(risk)
        return self

    def summary(self) -> str:
        marks = {"placed": "+", "marker_only": "~", "missing": "?",
                 "rejected": "-", "unsafe": "!"}
        detail = self.asset_filename or (self.payload.get("placeholder") or "")
        return (
            f"{marks.get(self.status, '?')} [{self.start:8.2f}] "
            f"{self.kind:<16} {self.status:<12} {str(detail)[:30]:<30} "
            f"{self.reason[:60]}"
        )

    def to_dict(self) -> dict:
        return {
            "placement_id": self.placement_id,
            "item_id": self.item_id,
            "kind": self.kind,
            "layer": self.layer,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "library_asset_id": self.library_asset_id,
            "asset_path": self.asset_path,
            "asset_filename": self.asset_filename,
            "track": self.track,
            "status": self.status,
            "reason": self.reason,
            "risks": list(self.risks),
            "candidates": [match.to_dict() for match in self.candidates],
            "premiere_ops": [dict(op) for op in self.premiere_ops],
            "payload": dict(self.payload),
            "style": self.style,
            "notes": self.notes,
            "is_placed": self.is_placed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AssetPlacement":
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        return cls(
            placement_id=str(data.get("placement_id") or ""),
            item_id=str(data.get("item_id") or ""),
            kind=str(data.get("kind") or "impact_sfx"),
            layer=str(data.get("layer") or "audio"),
            start=start,
            end=end,
            library_asset_id=str(data.get("library_asset_id") or ""),
            asset_path=str(data.get("asset_path") or ""),
            asset_filename=str(data.get("asset_filename") or ""),
            track=str(data.get("track") or ""),
            status=_coerce_one(
                data.get("status"), PLACEMENT_STATUSES, "marker_only"
            ),
            reason=str(data.get("reason") or "")[:600],
            risks=_coerce_many(data.get("risks"), PLACEMENT_RISKS),
            candidates=[
                AssetMatch.from_dict(entry)
                for entry in (data.get("candidates") or [])
            ],
            premiere_ops=[
                dict(op) for op in (data.get("premiere_ops") or [])
                if isinstance(op, dict)
            ],
            payload=dict(data.get("payload") or {}),
            style=str(data.get("style") or ""),
            notes=str(data.get("notes") or "")[:600],
        )


def placement_id_for(item_id: str, kind: str, start: float) -> str:
    return "ap_" + short_hash(item_id, kind, round(float(start), 3))


@dataclass
class AssetPlacementPlan:
    """Every placeholder resolved, plus the operations that realise them.

    Same dry-run contract as the rough cut, the critic and the style pass: the
    plan is built and validated offline, ``dry_run_passed`` starts False, and
    ``executed`` is written only by the executor.
    """

    sequence_name: str = ""
    style: str = ""
    placements: list[AssetPlacement] = field(default_factory=list)
    ops: list[dict] = field(default_factory=list)
    generated_at: str = ""
    dry_run_passed: bool = False
    dry_run_error: Optional[dict] = None
    explanation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    on_scratch: bool = True
    roughcut_executed: bool = False
    executed: bool = False
    cut_duration: float = 0.0
    #: Where the assets came from, and how many were available.
    library_root: str = ""
    library_stats: dict = field(default_factory=dict)
    #: The tracks this plan writes to. Never V1/A1.
    tracks: dict = field(default_factory=dict)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.placements)

    @property
    def operation_count(self) -> int:
        return len(self.ops)

    def of_status(self, *statuses: str) -> list[AssetPlacement]:
        wanted = set(statuses)
        return [p for p in self.placements if p.status in wanted]

    def placed(self) -> list[AssetPlacement]:
        return [p for p in self.placements if p.is_placed]

    def marker_only(self) -> list[AssetPlacement]:
        return self.of_status("marker_only")

    def missing(self) -> list[AssetPlacement]:
        return self.of_status("missing")

    def rejected(self) -> list[AssetPlacement]:
        return self.of_status("rejected")

    def unsafe(self) -> list[AssetPlacement]:
        return self.of_status("unsafe")

    def deferred(self) -> list[AssetPlacement]:
        """Everything that placed nothing, whatever the reason."""
        return [p for p in self.placements if p.status in NON_PLACING]

    def assets_used(self) -> list[str]:
        return sorted({
            p.asset_path for p in self.placements if p.is_placed and p.asset_path
        })

    def as_edit_plan(self, *, dry_run: bool = True) -> dict:
        plan: dict = {
            "ops": list(self.ops),
            "on_error": "abort",
            "label": (
                f"editing-brain-v1 assets [{self.style}]: {self.sequence_name}"
            ),
        }
        if dry_run:
            plan["dry_run"] = True
        return plan

    def stats(self) -> dict:
        by_status: dict = {}
        by_kind: dict = {}
        by_risk: dict = {}
        for placement in self.placements:
            by_status[placement.status] = by_status.get(placement.status, 0) + 1
            by_kind[placement.kind] = by_kind.get(placement.kind, 0) + 1
            for risk in placement.risks:
                by_risk[risk] = by_risk.get(risk, 0) + 1
        by_op: dict = {}
        for op in self.ops:
            name = str(op.get("op") or "?")
            by_op[name] = by_op.get(name, 0) + 1
        return {
            "placeholders": len(self.placements),
            "placed": len(self.placed()),
            "marker_only": len(self.marker_only()),
            "missing": len(self.missing()),
            "rejected": len(self.rejected()),
            "unsafe": len(self.unsafe()),
            "distinct_assets": len(self.assets_used()),
            "operations": len(self.ops),
            "by_status": by_status,
            "by_kind": by_kind,
            "by_risk": by_risk,
            "by_operation": by_op,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sequence_name": self.sequence_name,
            "style": self.style,
            "on_scratch": self.on_scratch,
            "roughcut_executed": self.roughcut_executed,
            "executed": self.executed,
            "dry_run_passed": self.dry_run_passed,
            "dry_run_error": self.dry_run_error,
            "explanation": list(self.explanation),
            "warnings": list(self.warnings),
            "library_root": self.library_root,
            "library_stats": dict(self.library_stats),
            "tracks": dict(self.tracks),
            "cut_duration": round(self.cut_duration, 2),
            "stats": self.stats(),
            "assets_used": self.assets_used(),
            "placements": [p.to_dict() for p in self.placements],
            "plan": self.as_edit_plan(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AssetPlacementPlan":
        return cls(
            sequence_name=str(data.get("sequence_name") or ""),
            style=str(data.get("style") or ""),
            placements=[
                AssetPlacement.from_dict(entry)
                for entry in (data.get("placements") or [])
            ],
            ops=[
                dict(op) for op in ((data.get("plan") or {}).get("ops") or [])
                if isinstance(op, dict)
            ],
            generated_at=str(data.get("generated_at") or ""),
            dry_run_passed=bool(data.get("dry_run_passed")),
            dry_run_error=data.get("dry_run_error"),
            explanation=as_str_list(data.get("explanation"), limit=500),
            warnings=as_str_list(data.get("warnings"), limit=200),
            on_scratch=bool(data.get("on_scratch", True)),
            roughcut_executed=bool(data.get("roughcut_executed")),
            executed=bool(data.get("executed")),
            cut_duration=as_float(data.get("cut_duration")),
            library_root=str(data.get("library_root") or ""),
            library_stats=dict(data.get("library_stats") or {}),
            tracks=dict(data.get("tracks") or {}),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )

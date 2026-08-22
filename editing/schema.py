"""Record types for the structure layer.

Everything the layer produces is one of the dataclasses below, and every one of
them round-trips through ``to_dict``/``from_dict`` without loss. That is the
whole contract: outputs are JSON, caches are JSON, and a later creative layer
reads JSON. No object here holds a file handle, a model or a bridge.

The vocabularies (environment, action, importance) are closed sets, because a
downstream planner that has to pattern-match free text is a planner that breaks
on the day the model says "cavern" instead of "cave". ``coerce_*`` maps the
model's phrasing onto the closed set and keeps the original wording in
``raw_*`` so nothing the model said is thrown away.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

ENVIRONMENTS = (
    "cave", "mineshaft", "stronghold", "nether", "nether_fortress", "bastion",
    "end", "village", "forest", "jungle", "swamp", "desert", "plains",
    "mountains", "snow", "ocean", "underwater", "river", "base", "farm",
    "structure", "menu", "unknown",
)

PLAYER_ACTIONS = (
    "mining", "building", "fighting", "looting", "exploring", "escaping",
    "crafting", "dying", "travelling", "farming", "searching", "trading",
    "enchanting", "brewing", "redstone", "eating", "idle", "talking",
    "unknown",
)

IMPORTANCE_LEVELS = (
    "boring", "setup", "tension", "payoff", "funny", "danger", "reveal",
)

#: Rough "would a viewer care" weighting, used to rank segments. Deliberately
#: coarse -- this layer ranks candidates, it does not decide the edit.
IMPORTANCE_WEIGHT = {
    "boring": 0.05,
    "setup": 0.35,
    "tension": 0.75,
    "danger": 0.80,
    "funny": 0.85,
    "reveal": 0.90,
    "payoff": 1.00,
}

CAMERA_MOTIONS = (
    "static", "pan", "tilt", "orbit", "walk", "run", "fly", "fall",
    "shake", "swing", "erratic", "unknown",
)

ALIGNMENT_KINDS = ("match", "contrast", "neutral", "unknown")

# ---------------------------------------------------------------------------
# Audio vocabularies
# ---------------------------------------------------------------------------

#: What an audio event is. Every name that involves interpreting a *sound* --
#: rather than measuring one -- is prefixed ``possible_``, because that is the
#: honest label for what a loudness heuristic can actually tell you. Silence
#: and clipping are measured; laughter and screaming are guessed at.
AUDIO_EVENT_TYPES = (
    "silence",              # measured: below the floor for long enough
    "long_pause",           # measured: a gap between transcript lines
    "loudness_spike",       # measured: a jump above the local baseline
    "sudden_reaction",      # measured: a spike out of near-silence
    "speech_dense",         # measured: words-per-second well above normal
    "speech_sparse",        # measured: words-per-second well below normal
    "low_energy",           # measured: sustained quiet, but not silence
    "clipping",             # measured: peak at or above 0 dBFS
    "possible_laughter",    # guessed
    "possible_scream",      # guessed
    "music_region",         # guessed: sustained energy with little speech
    "unknown",
)

#: What an audio event is worth to an editor.
AUDIO_EDIT_VALUES = (
    "boring", "tension", "comedy", "impact", "pause", "transition",
    "emphasis", "unknown",
)

#: How the event was found. This is not decoration -- the safety pass weighs a
#: transcript marker (a human or ASR literally wrote "[laughs]") far more
#: heavily than a loudness heuristic, so the provenance has to survive.
AUDIO_DETECTION_METHODS = (
    "heuristic", "transcript_marker", "model", "manual", "unknown",
)

#: Default editing value per event type, before any context is applied.
AUDIO_VALUE_FOR_TYPE = {
    "silence": "pause",
    "long_pause": "pause",
    "loudness_spike": "impact",
    "sudden_reaction": "impact",
    "speech_dense": "emphasis",
    "speech_sparse": "boring",
    "low_energy": "boring",
    "clipping": "unknown",
    "possible_laughter": "comedy",
    "possible_scream": "tension",
    "music_region": "transition",
    "unknown": "unknown",
}

TRANSCRIPT_SOURCES = (
    "premiere", "srt", "vtt", "txt", "json", "csv", "manual",
    # Produced locally by editing.transcribe. Kept distinct from "json"
    # because a machine transcription and one a person exported by hand
    # deserve different amounts of trust -- the audio layer already weights a
    # human [laughs] marker above its own guess, and this is the same rule.
    "whisper",
    "unknown",
)

#: Model phrasings that mean one of the closed values above. Only genuinely
#: unambiguous mappings belong here; anything doubtful should fall through to
#: "unknown" and stay visible in the raw field.
_SYNONYMS = {
    # environments
    "cavern": "cave", "caves": "cave", "underground": "cave",
    "ravine": "cave", "deepslate": "cave", "deep_dark": "cave",
    "abandoned_mineshaft": "mineshaft", "mine": "mineshaft",
    "mineshafts": "mineshaft", "abandoned_mine": "mineshaft",
    "the_nether": "nether", "hell": "nether", "nether_wastes": "nether",
    "soul_sand_valley": "nether", "crimson_forest": "nether",
    "warped_forest": "nether", "basalt_deltas": "nether",
    "fortress": "nether_fortress", "nether_fort": "nether_fortress",
    "the_end": "end", "end_dimension": "end", "ender_dragon_fight": "end",
    "end_city": "end",
    "woods": "forest", "woodland": "forest", "taiga": "forest",
    "birch_forest": "forest", "dark_forest": "forest",
    "grassland": "plains", "field": "plains", "meadow": "plains",
    "savanna": "plains", "badlands": "desert", "mesa": "desert",
    "sea": "ocean", "beach": "ocean", "shore": "ocean", "coast": "ocean",
    "sea_floor": "underwater", "ocean_monument": "underwater",
    "hills": "mountains", "mountain": "mountains", "peak": "mountains",
    "snowy": "snow", "tundra": "snow", "ice": "snow", "glacier": "snow",
    "home": "base", "house": "base", "shelter": "base", "hideout": "base",
    "storage_room": "base", "villager": "village", "villagers": "village",
    "farmland": "farm", "crops": "farm", "farming_area": "farm",
    "temple": "structure", "pyramid": "structure", "outpost": "structure",
    "ruins": "structure", "dungeon": "structure", "shipwreck": "structure",
    "trial_chamber": "structure", "ancient_city": "structure",
    "main_menu": "menu", "title_screen": "menu", "pause_menu": "menu",
    "loading": "menu", "loading_screen": "menu",
    # actions
    "traveling": "travelling", "travel": "travelling", "walking": "travelling",
    "running": "travelling", "sprinting": "travelling", "riding": "travelling",
    "flying": "travelling", "boating": "travelling", "swimming": "travelling",
    "digging": "mining", "strip_mining": "mining", "breaking_blocks": "mining",
    "placing_blocks": "building", "construction": "building",
    "combat": "fighting", "attacking": "fighting", "battling": "fighting",
    "pvp": "fighting", "killing": "fighting", "hitting": "fighting",
    "fleeing": "escaping", "running_away": "escaping", "retreating": "escaping",
    "escape": "escaping",
    "opening_chest": "looting", "chest_looting": "looting", "chest": "looting",
    "loot": "looting", "loots": "looting",
    "collecting": "looting", "gathering": "looting", "picking_up": "looting",
    "crafting_table": "crafting", "smelting": "crafting", "smithing": "crafting",
    "died": "dying", "death": "dying", "respawning": "dying",
    "exploring_cave": "exploring", "caving": "exploring", "spelunking": "exploring",
    "wandering": "exploring", "scouting": "exploring",
    "looking_for": "searching", "hunting": "searching", "finding": "searching",
    "harvesting": "farming", "planting": "farming", "breeding": "farming",
    "villager_trading": "trading", "bartering": "trading",
    "enchanting_table": "enchanting", "potion": "brewing", "brewing_stand": "brewing",
    "wiring": "redstone", "circuit": "redstone",
    "afk": "idle", "standing": "idle", "waiting": "idle", "nothing": "idle",
    "commentary": "talking", "narrating": "talking", "speaking": "talking",
    # importance
    "exciting": "payoff", "climax": "payoff", "success": "payoff",
    "win": "payoff", "achievement": "payoff", "victory": "payoff",
    "intro": "setup", "introduction": "setup", "explanation": "setup",
    "preparation": "setup", "context": "setup", "transition": "setup",
    "suspense": "tension", "buildup": "tension", "close_call": "tension",
    "scary": "danger", "dangerous": "danger", "threat": "danger",
    "near_death": "danger", "low_health": "danger",
    "comedy": "funny", "humour": "funny", "humor": "funny", "joke": "funny",
    "fail": "funny", "blooper": "funny",
    "discovery": "reveal", "found": "reveal", "surprise": "reveal",
    "first_time": "reveal",
    "dull": "boring", "filler": "boring", "uneventful": "boring",
    "nothing_happening": "boring", "walking_around": "boring",
    # camera
    "still": "static", "stationary": "static", "steady": "static",
    "panning": "pan", "turning": "pan", "looking_around": "pan",
    "tilting": "tilt", "orbiting": "orbit", "circling": "orbit",
    "walking_forward": "walk", "moving": "walk",
    "sprinting_forward": "run", "falling": "fall", "dropping": "fall",
    "shaking": "shake", "shaky": "shake", "jittery": "erratic",
    "chaotic": "erratic", "fast": "erratic", "spinning": "erratic",
}


def _slug(value: Any) -> str:
    """Normalise free text to a comparable token: 'The Nether!' -> 'the_nether'."""
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    return text.strip("_")


def _coerce(value: Any, allowed: Iterable[str], default: str = "unknown") -> str:
    """Map a model's wording onto a closed vocabulary.

    Tries, in order: exact token, known synonym, then a containment match in
    either direction ("in_a_cave" -> "cave"). The containment pass is what
    absorbs the model answering in a phrase rather than a label; it is last so
    an exact answer always wins.
    """
    token = _slug(value)
    if not token:
        return default
    allowed = tuple(allowed)
    if token in allowed:
        return token
    mapped = _SYNONYMS.get(token)
    if mapped in allowed:
        return mapped
    for candidate in allowed:
        if candidate == "unknown":
            continue
        if candidate in token or token in candidate:
            return candidate
    for synonym, mapped in _SYNONYMS.items():
        if mapped in allowed and (synonym in token or token in synonym):
            return mapped

    # Word-level pass. A phrase answer like "opening a chest" contains neither
    # an allowed value nor a synonym as a substring, because the filler word
    # breaks both; matching its individual words and adjacent pairs catches it.
    words = [word for word in token.split("_") if word]
    pieces = words + [
        f"{first}_{second}" for first, second in zip(words, words[1:])
    ]
    for piece in pieces:
        if piece in allowed:
            return piece
        mapped = _SYNONYMS.get(piece)
        if mapped in allowed:
            return mapped
    return default


def coerce_environment(value: Any) -> str:
    return _coerce(value, ENVIRONMENTS)


def coerce_action(value: Any) -> str:
    return _coerce(value, PLAYER_ACTIONS)


def coerce_importance(value: Any) -> str:
    return _coerce(value, IMPORTANCE_LEVELS, default="setup")


def coerce_camera_motion(value: Any) -> str:
    return _coerce(value, CAMERA_MOTIONS)


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------

def as_float(value: Any, default: float = 0.0) -> float:
    """Parse a number the way a model might have written it.

    Accepts ``12``, ``"12.5"``, ``"12.5s"``, ``"00:01:02.5"`` and ``"1:02"``.
    NaN and infinities become the default -- they poison every later comparison.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        if ":" in text:
            parsed = parse_timecode(text)
            if parsed is not None:
                return parsed
        match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", "."))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return default
    return default


def parse_timecode(text: str) -> Optional[float]:
    """``HH:MM:SS[.mmm]``, ``MM:SS[.mmm]`` or ``HH:MM:SS,mmm`` to seconds."""
    cleaned = str(text).strip().replace(",", ".")
    parts = cleaned.split(":")
    if not 2 <= len(parts) <= 3:
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if any(math.isnan(n) or math.isinf(n) for n in numbers):
        return None
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60.0 + number
    return seconds


def clamp01(value: Any, default: float = 0.5) -> float:
    return max(0.0, min(1.0, as_float(value, default)))


def as_str_list(value: Any, limit: int = 40) -> list[str]:
    """Coerce a model's answer into a clean list of short strings.

    Models answer this field as a list, a comma-joined string, or the literal
    word "none". All three arrive here and all three must leave as a list.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("none", "n/a", "nothing", "-", "null"):
            return []
        items: list[Any] = re.split(r"\s*[,;]\s*|\s+and\s+", text)
    elif isinstance(value, dict):
        items = list(value.keys())
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]

    out: list[str] = []
    seen = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("label") or item.get("type") or ""
        text = str(item).strip().strip(".")
        if not text or text.lower() in ("none", "n/a", "nothing", "null"):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text[:80])
        if len(out) >= limit:
            break
    return out


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on", "visible", "open")


def short_hash(*parts: Any, length: int = 12) -> str:
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8"))
    return digest.hexdigest()[:length]


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

@dataclass
class PremiereRef:
    """Where a file sits inside the open Premiere project, if it does.

    ``matched`` is the field callers should branch on. A ref with
    ``matched=False`` means "we looked and it is not in the project", which is
    different information from "we never looked" (no ref at all).
    """

    matched: bool = False
    project: str = ""
    item_name: str = ""
    bin: str = ""
    media_type: str = ""
    sequences: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "PremiereRef":
        data = data or {}
        return cls(
            matched=as_bool(data.get("matched")),
            project=str(data.get("project") or ""),
            item_name=str(data.get("item_name") or ""),
            bin=str(data.get("bin") or ""),
            media_type=str(data.get("media_type") or ""),
            sequences=as_str_list(data.get("sequences")),
            note=str(data.get("note") or ""),
        )


@dataclass
class MediaAsset:
    """One discovered media file plus everything needed to re-identify it."""

    asset_id: str
    path: str
    filename: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    audio_channels: int = 0
    size_bytes: int = 0
    mtime: float = 0.0
    content_hash: str = ""
    container: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    premiere: PremiereRef = field(default_factory=PremiereRef)
    probe_error: str = ""

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width and self.height else ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["premiere"] = self.premiere.to_dict()
        data["resolution"] = self.resolution
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "MediaAsset":
        return cls(
            asset_id=str(data.get("asset_id") or ""),
            path=str(data.get("path") or ""),
            filename=str(data.get("filename") or ""),
            duration=as_float(data.get("duration")),
            width=int(as_float(data.get("width"))),
            height=int(as_float(data.get("height"))),
            fps=as_float(data.get("fps")),
            has_audio=as_bool(data.get("has_audio")),
            audio_channels=int(as_float(data.get("audio_channels"))),
            size_bytes=int(as_float(data.get("size_bytes"))),
            mtime=as_float(data.get("mtime")),
            content_hash=str(data.get("content_hash") or ""),
            container=str(data.get("container") or ""),
            video_codec=str(data.get("video_codec") or ""),
            audio_codec=str(data.get("audio_codec") or ""),
            premiere=PremiereRef.from_dict(data.get("premiere")),
            probe_error=str(data.get("probe_error") or ""),
        )


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

@dataclass
class TranscriptEntry:
    """One spoken line, normalised out of whatever format it arrived in."""

    start: float
    end: float
    text: str
    speaker: str = ""
    confidence: float = 1.0
    #: Which clip or sequence this line came from, when the source knew.
    source_ref: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def overlaps(self, start: float, end: float) -> float:
        """Seconds of overlap with ``[start, end)``. 0.0 when disjoint."""
        return max(0.0, min(self.end, end) - max(self.start, start))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptEntry":
        start = as_float(data.get("start", data.get("from", data.get("begin"))))
        end = as_float(data.get("end", data.get("to", data.get("stop"))), start)
        # A zero/absent end with a duration is common in JSON transcripts.
        if end <= start and data.get("duration") is not None:
            end = start + as_float(data.get("duration"))
        text = str(
            data.get("text") or data.get("content") or data.get("value") or ""
        ).strip()
        return cls(
            start=max(0.0, start),
            end=max(max(0.0, start), end),
            text=text,
            speaker=str(data.get("speaker") or data.get("speaker_name") or "").strip(),
            confidence=clamp01(data.get("confidence", 1.0), 1.0),
            source_ref=str(data.get("source_ref") or data.get("source") or "").strip(),
        )


@dataclass
class Transcript:
    """A normalised transcript for one asset, whatever produced it."""

    asset_id: str
    source: str = "unknown"
    source_path: str = ""
    language: str = ""
    entries: list[TranscriptEntry] = field(default_factory=list)
    created_at: str = ""
    note: str = ""

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def duration(self) -> float:
        return max((entry.end for entry in self.entries), default=0.0)

    @property
    def text(self) -> str:
        return " ".join(entry.text for entry in self.entries if entry.text)

    def entries_between(self, start: float, end: float) -> list[TranscriptEntry]:
        return [e for e in self.entries if e.overlaps(start, end) > 0.0]

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "source": self.source,
            "source_path": self.source_path,
            "language": self.language,
            "created_at": self.created_at,
            "note": self.note,
            "entry_count": len(self.entries),
            "duration": round(self.duration, 3),
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        source = _slug(data.get("source"))
        return cls(
            asset_id=str(data.get("asset_id") or ""),
            source=source if source in TRANSCRIPT_SOURCES else "unknown",
            source_path=str(data.get("source_path") or ""),
            language=str(data.get("language") or ""),
            entries=[
                TranscriptEntry.from_dict(entry)
                for entry in (data.get("entries") or [])
            ],
            created_at=str(data.get("created_at") or ""),
            note=str(data.get("note") or ""),
        )


# ---------------------------------------------------------------------------
# Visual events
# ---------------------------------------------------------------------------

@dataclass
class UIState:
    """Minecraft HUD/screen state read off the frames.

    Deliberately a fixed set of booleans plus free text: an editor needs to
    know "is a death screen on screen" reliably, and knowing the exact
    coordinate string is a bonus rather than something to depend on.
    """

    inventory_open: bool = False
    crafting_open: bool = False
    chest_open: bool = False
    death_screen: bool = False
    achievement_toast: bool = False
    low_health: bool = False
    chat_open: bool = False
    map_open: bool = False
    coordinates: str = ""
    hotbar: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    @property
    def any_screen_open(self) -> bool:
        """True when a full-screen UI is covering the game -- usually unusable."""
        return bool(
            self.inventory_open or self.crafting_open
            or self.chest_open or self.map_open
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["any_screen_open"] = self.any_screen_open
        return data

    @classmethod
    def from_dict(cls, data: Optional[Any]) -> "UIState":
        # The model sometimes answers this field as a list of visible elements
        # ("inventory", "low health") instead of the object it was asked for.
        if isinstance(data, (list, tuple, str)):
            flags = as_str_list(data)
            joined = " ".join(flags).lower()
            return cls(
                inventory_open="inventor" in joined,
                crafting_open="craft" in joined,
                chest_open="chest" in joined,
                death_screen="death" in joined or "respawn" in joined,
                achievement_toast="achievement" in joined or "advancement" in joined
                or "toast" in joined,
                low_health="low health" in joined or "low_health" in joined,
                chat_open="chat" in joined,
                map_open="map" in joined,
                other=flags,
            )
        data = data or {}
        return cls(
            inventory_open=as_bool(data.get("inventory_open", data.get("inventory"))),
            crafting_open=as_bool(data.get("crafting_open", data.get("crafting"))),
            chest_open=as_bool(data.get("chest_open", data.get("chest"))),
            death_screen=as_bool(data.get("death_screen", data.get("death"))),
            achievement_toast=as_bool(
                data.get("achievement_toast",
                         data.get("achievement", data.get("toast")))
            ),
            low_health=as_bool(data.get("low_health", data.get("health_low"))),
            chat_open=as_bool(data.get("chat_open", data.get("chat"))),
            map_open=as_bool(data.get("map_open", data.get("map"))),
            coordinates=str(data.get("coordinates") or data.get("coords") or "").strip(),
            hotbar=as_str_list(data.get("hotbar") or data.get("hotbar_items"), limit=12),
            other=as_str_list(data.get("other") or data.get("notes"), limit=12),
        )


@dataclass
class CameraMotion:
    motion: str = "unknown"
    intensity: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Any]) -> "CameraMotion":
        if isinstance(data, str):
            return cls(motion=coerce_camera_motion(data), intensity=0.0)
        if isinstance(data, (int, float)):
            return cls(motion="unknown", intensity=clamp01(data, 0.0))
        data = data or {}
        return cls(
            motion=coerce_camera_motion(
                data.get("motion") or data.get("type") or data.get("camera")
            ),
            intensity=clamp01(data.get("intensity", data.get("amount", 0.0)), 0.0),
        )


@dataclass
class TimeRange:
    start: float = 0.0
    end: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "duration": round(self.duration, 3)}

    @classmethod
    def from_dict(cls, data: Optional[Any]) -> "TimeRange":
        if isinstance(data, (list, tuple)) and len(data) >= 2:
            start, end = as_float(data[0]), as_float(data[1])
        else:
            data = data or {}
            start = as_float(data.get("start", data.get("from")))
            end = as_float(data.get("end", data.get("to")), start)
        start = max(0.0, start)
        return cls(start=start, end=max(start, end))


@dataclass
class VisualEvent:
    """What the vision model saw in one window of one file."""

    event_id: str
    source_file: str
    asset_id: str = ""
    start: float = 0.0
    end: float = 0.0
    confidence: float = 0.5
    environment: str = "unknown"
    raw_environment: str = ""
    actions: list[str] = field(default_factory=list)
    raw_actions: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    threats: list[str] = field(default_factory=list)
    ui: UIState = field(default_factory=UIState)
    camera: CameraMotion = field(default_factory=CameraMotion)
    importance: str = "setup"
    raw_importance: str = ""
    suggested_range: TimeRange = field(default_factory=TimeRange)
    notes: str = ""
    #: Provenance: which model, which sampling, which frames. Without this a
    #: cached event cannot be argued with.
    model: str = ""
    frame_times: list[float] = field(default_factory=list)
    dense: bool = False
    motion_score: float = 0.0
    error: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def primary_action(self) -> str:
        return self.actions[0] if self.actions else "unknown"

    @property
    def weight(self) -> float:
        """Importance weight scaled by how sure the model was."""
        return IMPORTANCE_WEIGHT.get(self.importance, 0.3) * max(0.2, self.confidence)

    def overlaps(self, start: float, end: float) -> float:
        return max(0.0, min(self.end, end) - max(self.start, start))

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "source_file": self.source_file,
            "asset_id": self.asset_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "confidence": round(self.confidence, 3),
            "environment": self.environment,
            "raw_environment": self.raw_environment,
            "actions": list(self.actions),
            "raw_actions": list(self.raw_actions),
            "entities": list(self.entities),
            "threats": list(self.threats),
            "ui": self.ui.to_dict(),
            "camera": self.camera.to_dict(),
            "importance": self.importance,
            "raw_importance": self.raw_importance,
            "suggested_range": self.suggested_range.to_dict(),
            "notes": self.notes,
            "model": self.model,
            "frame_times": [round(t, 3) for t in self.frame_times],
            "dense": self.dense,
            "motion_score": round(self.motion_score, 4),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VisualEvent":
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        raw_env = data.get("raw_environment") or data.get("environment") or ""
        raw_actions = as_str_list(
            data.get("raw_actions") or data.get("actions")
            or data.get("player_action") or data.get("action")
        )
        raw_importance = (
            data.get("raw_importance") or data.get("importance")
            or data.get("scene_importance") or ""
        )
        suggested = TimeRange.from_dict(
            data.get("suggested_range") or data.get("usable_range")
        )
        if suggested.duration <= 0.0:
            suggested = TimeRange(start=start, end=end)

        return cls(
            event_id=str(data.get("event_id") or short_hash(
                data.get("source_file"), start, end)),
            source_file=str(data.get("source_file") or ""),
            asset_id=str(data.get("asset_id") or ""),
            start=start,
            end=end,
            confidence=clamp01(data.get("confidence", 0.5), 0.5),
            environment=coerce_environment(
                data.get("environment") or data.get("location")
            ),
            raw_environment=str(raw_env)[:200],
            actions=[
                action for action in dict.fromkeys(
                    coerce_action(item) for item in raw_actions
                ) if action != "unknown"
            ] or ["unknown"],
            raw_actions=raw_actions,
            entities=as_str_list(
                data.get("entities") or data.get("mobs") or data.get("visible_entities")
            ),
            threats=as_str_list(data.get("threats") or data.get("dangers")),
            ui=UIState.from_dict(data.get("ui") or data.get("ui_state")),
            camera=CameraMotion.from_dict(
                data.get("camera") or data.get("camera_motion")
            ),
            importance=coerce_importance(raw_importance),
            raw_importance=str(raw_importance)[:200],
            suggested_range=suggested,
            notes=str(data.get("notes") or data.get("description") or "")[:2000],
            model=str(data.get("model") or ""),
            frame_times=[as_float(t) for t in (data.get("frame_times") or [])],
            dense=as_bool(data.get("dense")),
            motion_score=clamp01(data.get("motion_score", 0.0), 0.0),
            error=str(data.get("error") or ""),
        )


# ---------------------------------------------------------------------------
# Combined timeline
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Audio events
# ---------------------------------------------------------------------------

@dataclass
class AudioEvent:
    """Something audible worth an editor's attention.

    Deliberately modest about what it claims. ``silence``, ``clipping`` and
    ``loudness_spike`` are *measurements* and carry high confidence.
    ``possible_laughter`` and ``possible_scream`` are *guesses* from a loudness
    envelope, and are named and scored to say so -- a heuristic that has not
    seen a transcript marker will not exceed ~0.45 confidence, and downstream
    layers are built to treat that as weak evidence rather than fact.

    ``loudness_db`` is dBFS (negative; 0.0 is full scale) and ``baseline_db``
    is the file's own median level, because "loud" only means anything
    relative to the rest of the recording.
    """

    event_id: str
    source_file: str
    asset_id: str = ""
    start: float = 0.0
    end: float = 0.0
    type: str = "unknown"
    confidence: float = 0.5
    loudness_db: float = 0.0
    peak_db: float = 0.0
    baseline_db: float = 0.0
    #: Words per second across this event, when a transcript was available.
    speech_density: Optional[float] = None
    edit_value: str = "unknown"
    detection: str = "heuristic"
    notes: str = ""
    #: Free-form measurements behind the verdict, for debugging a bad call.
    evidence: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_measured(self) -> bool:
        """True when the verdict is a measurement rather than an inference."""
        return not self.type.startswith("possible_") and self.type != "music_region"

    @property
    def relative_db(self) -> float:
        """How far above (or below) the file's own baseline this event sits."""
        return self.loudness_db - self.baseline_db

    def overlaps(self, start: float, end: float) -> float:
        return max(0.0, min(self.end, end) - max(self.start, start))

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "source_file": self.source_file,
            "asset_id": self.asset_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "type": self.type,
            "confidence": round(self.confidence, 3),
            "loudness_db": round(self.loudness_db, 2),
            "peak_db": round(self.peak_db, 2),
            "baseline_db": round(self.baseline_db, 2),
            "relative_db": round(self.relative_db, 2),
            "speech_density": (
                round(self.speech_density, 3)
                if self.speech_density is not None else None
            ),
            "edit_value": self.edit_value,
            "detection": self.detection,
            "is_measured": self.is_measured,
            "notes": self.notes,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AudioEvent":
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        kind = _slug(data.get("type"))
        if kind not in AUDIO_EVENT_TYPES:
            kind = _coerce(kind, AUDIO_EVENT_TYPES)

        value = _slug(data.get("edit_value"))
        if value not in AUDIO_EDIT_VALUES:
            value = AUDIO_VALUE_FOR_TYPE.get(kind, "unknown")

        detection = _slug(data.get("detection") or data.get("method"))
        if detection not in AUDIO_DETECTION_METHODS:
            detection = "unknown"

        density = data.get("speech_density")
        return cls(
            event_id=str(data.get("event_id") or short_hash(
                data.get("source_file"), start, end, kind)),
            source_file=str(data.get("source_file") or ""),
            asset_id=str(data.get("asset_id") or ""),
            start=start,
            end=end,
            type=kind,
            confidence=clamp01(data.get("confidence", 0.5), 0.5),
            loudness_db=as_float(data.get("loudness_db")),
            peak_db=as_float(data.get("peak_db")),
            baseline_db=as_float(data.get("baseline_db")),
            speech_density=(
                as_float(density) if density is not None else None
            ),
            edit_value=value,
            detection=detection,
            notes=str(data.get("notes") or "")[:1000],
            evidence=dict(data.get("evidence") or {}),
        )


@dataclass
class TimelineSegment:
    """One stretch of one file with both channels of information attached."""

    segment_id: str
    asset_id: str
    source_file: str
    start: float
    end: float
    said: str = ""
    speech_entries: list[TranscriptEntry] = field(default_factory=list)
    events: list[VisualEvent] = field(default_factory=list)
    audio_events: list[AudioEvent] = field(default_factory=list)
    #: "match" / "contrast" / "neutral" / "unknown" -- see ``editing.align``.
    alignment: str = "unknown"
    alignment_reason: str = ""
    usefulness: float = 0.0
    usable: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def has_speech(self) -> bool:
        return bool(self.said.strip())

    @property
    def has_audio_events(self) -> bool:
        return bool(self.audio_events)

    def audio_types(self) -> set:
        return {event.type for event in self.audio_events}

    @property
    def is_dead_air(self) -> bool:
        """Silence or a long pause covering most of the segment, with no speech.

        "Most" rather than "all" because a 0.4s cough inside eight seconds of
        nothing does not make the stretch worth keeping.
        """
        if self.has_speech or not self.audio_events:
            return False
        quiet = sum(
            event.duration for event in self.audio_events
            if event.type in ("silence", "long_pause", "low_energy")
        )
        return self.duration > 0 and quiet >= self.duration * 0.6

    @property
    def audio_reaction(self) -> Optional["AudioEvent"]:
        """The strongest reaction-style audio event here, if any.

        This is what makes a visually ordinary moment interesting: a scream or
        a burst of laughter over footage the vision model called "setup".
        """
        reactions = [
            event for event in self.audio_events
            if event.type in ("sudden_reaction", "possible_laughter",
                              "possible_scream", "loudness_spike")
        ]
        if not reactions:
            return None
        return max(reactions, key=lambda event: event.confidence)

    @property
    def importance(self) -> str:
        """The strongest importance among this segment's visual events."""
        if not self.events:
            return "boring"
        return max(
            self.events,
            key=lambda event: IMPORTANCE_WEIGHT.get(event.importance, 0.0),
        ).importance

    def summary(self) -> str:
        """One human-readable line, for `timeline show` and debugging."""
        event = self.events[0] if self.events else None
        visual = (
            f"{event.environment}/{event.primary_action}" if event else "no visual data"
        )
        spoken = (self.said[:60] + "...") if len(self.said) > 60 else self.said
        return (
            f"[{self.start:7.2f}-{self.end:7.2f}] {self.importance:<8} "
            f"{visual:<26} {self.alignment:<8} {spoken or '(silence)'}"
        )

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "asset_id": self.asset_id,
            "source_file": self.source_file,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "said": self.said,
            "speech_entries": [entry.to_dict() for entry in self.speech_entries],
            "events": [event.to_dict() for event in self.events],
            "audio_events": [event.to_dict() for event in self.audio_events],
            "audio_types": sorted(self.audio_types()),
            "is_dead_air": self.is_dead_air,
            "importance": self.importance,
            "alignment": self.alignment,
            "alignment_reason": self.alignment_reason,
            "usefulness": round(self.usefulness, 3),
            "usable": self.usable,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TimelineSegment":
        start = as_float(data.get("start"))
        end = max(start, as_float(data.get("end"), start))
        alignment = _slug(data.get("alignment"))
        return cls(
            segment_id=str(data.get("segment_id") or ""),
            asset_id=str(data.get("asset_id") or ""),
            source_file=str(data.get("source_file") or ""),
            start=start,
            end=end,
            said=str(data.get("said") or ""),
            speech_entries=[
                TranscriptEntry.from_dict(entry)
                for entry in (data.get("speech_entries") or [])
            ],
            events=[
                VisualEvent.from_dict(event) for event in (data.get("events") or [])
            ],
            audio_events=[
                AudioEvent.from_dict(event)
                for event in (data.get("audio_events") or [])
            ],
            alignment=alignment if alignment in ALIGNMENT_KINDS else "unknown",
            alignment_reason=str(data.get("alignment_reason") or ""),
            usefulness=as_float(data.get("usefulness")),
            usable=as_bool(data.get("usable")),
            reasons=as_str_list(data.get("reasons")),
        )


@dataclass
class StructureTimeline:
    """The deliverable: every asset, every segment, and how it was produced."""

    segments: list[TimelineSegment] = field(default_factory=list)
    assets: list[MediaAsset] = field(default_factory=list)
    generated_at: str = ""
    model: str = ""
    sampling: dict = field(default_factory=dict)
    #: Per-asset record of where the transcript came from (or why there is none).
    transcript_sources: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.segments)

    def segments_for(self, asset_id: str) -> list[TimelineSegment]:
        return [s for s in self.segments if s.asset_id == asset_id]

    def highlights(self, limit: int = 20) -> list[TimelineSegment]:
        """The most likely-useful segments, best first."""
        ranked = sorted(self.segments, key=lambda s: s.usefulness, reverse=True)
        return [segment for segment in ranked if segment.usable][:limit]

    def stats(self) -> dict:
        by_importance: dict[str, int] = {}
        by_alignment: dict[str, int] = {}
        for segment in self.segments:
            by_importance[segment.importance] = by_importance.get(segment.importance, 0) + 1
            by_alignment[segment.alignment] = by_alignment.get(segment.alignment, 0) + 1
        covered = sum(segment.duration for segment in self.segments)
        with_speech = sum(1 for segment in self.segments if segment.has_speech)
        return {
            "assets": len(self.assets),
            "segments": len(self.segments),
            "usable_segments": sum(1 for s in self.segments if s.usable),
            "segments_with_speech": with_speech,
            "covered_seconds": round(covered, 2),
            "by_importance": by_importance,
            "by_alignment": by_alignment,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "model": self.model,
            "sampling": dict(self.sampling),
            "transcript_sources": dict(self.transcript_sources),
            "warnings": list(self.warnings),
            "stats": self.stats(),
            "assets": [asset.to_dict() for asset in self.assets],
            "segments": [segment.to_dict() for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StructureTimeline":
        return cls(
            segments=[
                TimelineSegment.from_dict(segment)
                for segment in (data.get("segments") or [])
            ],
            assets=[
                MediaAsset.from_dict(asset) for asset in (data.get("assets") or [])
            ],
            generated_at=str(data.get("generated_at") or ""),
            model=str(data.get("model") or ""),
            sampling=dict(data.get("sampling") or {}),
            transcript_sources=dict(data.get("transcript_sources") or {}),
            warnings=as_str_list(data.get("warnings"), limit=200),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )

"""The track layout: which layer of the edit lives on which Premiere track.

Every pass in this system writes to the timeline, and until now each one chose
its own destination in its own module. That produced a real collision -- the
asset pass and the visual pass both defaulted to ``V3`` -- and it made the
question "what does the finished timeline look like?" unanswerable without
reading five files. One table answers it.

The layout, bottom to top:

===== ===================================================================
V1    the programme. The rough cut's clips, and nothing else, ever.
V2    captions, cards and labels: text the viewer reads.
V3    visual treatments: freezes, callouts, shapes, emphasis graphics.
V4    additional picture: b-roll, overlays, facecam, picture-in-picture.
A1    dialogue. The rough cut's own audio, and nothing else, ever.
A2    sound effects: one-shots, whooshes, impacts, transition sounds.
A3    music and ambience beds.
===== ===================================================================

Two rules make this safe rather than merely tidy:

**V1 and A1 belong to the cut.** Nothing above the rough cut may write to them.
That is what keeps every later pass reversible by deleting tracks, and it is
enforced structurally by each pass's executor, not just documented here.

**Nothing is hardcoded to one project.** :class:`TrackLayout` is a value, so a
project that already uses V2 for a facecam can be given a layout that moves the
captions up, and every pass follows. The default below is the layout this
system builds when it creates its own sequence -- not an assumption about
somebody else's.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

#: Roles a track can play. Ordered as they stack in the timeline.
VIDEO_ROLES = ("programme", "captions", "treatments", "overlay")
AUDIO_ROLES = ("dialogue", "sfx", "music")
ROLES = VIDEO_ROLES + AUDIO_ROLES

#: Roles the rough cut owns. Writing to these from a later pass would make that
#: pass unremovable and could move a clip another pass has already measured.
CUT_ROLES = frozenset({"programme", "dialogue"})


def _track_index(track: str) -> int:
    """``"V3" -> 3``. Zero when the name is not a track name."""
    text = str(track or "").strip().upper()
    if len(text) < 2 or text[0] not in "VA" or not text[1:].isdigit():
        return 0
    return int(text[1:])


def is_video(track: str) -> bool:
    return str(track or "").strip().upper().startswith("V")


@dataclass(frozen=True)
class TrackLayout:
    """Which Premiere track each layer of the edit writes to."""

    programme: str = "V1"
    captions: str = "V2"
    treatments: str = "V3"
    overlay: str = "V4"
    dialogue: str = "A1"
    sfx: str = "A2"
    music: str = "A3"

    def track_for(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError(
                f"Unknown track role {role!r}. Known roles: {', '.join(ROLES)}"
            )
        return getattr(self, role)

    def role_for(self, track: str) -> str:
        """Which role a track name plays here, or "" for one outside the layout."""
        wanted = str(track or "").strip().upper()
        for role in ROLES:
            if getattr(self, role).upper() == wanted:
                return role
        return ""

    @property
    def protected(self) -> frozenset:
        """Tracks no pass above the rough cut may write to."""
        return frozenset({self.programme.upper(), self.dialogue.upper()})

    def is_protected(self, track: str) -> bool:
        return str(track or "").strip().upper() in self.protected

    @property
    def video_tracks_needed(self) -> int:
        """How many video tracks the sequence must have for this layout."""
        return max(_track_index(getattr(self, role)) for role in VIDEO_ROLES)

    @property
    def audio_tracks_needed(self) -> int:
        return max(_track_index(getattr(self, role)) for role in AUDIO_ROLES)

    def ensure_ops(self, *, existing_video: int = 1,
                   existing_audio: int = 1) -> list[dict]:
        """Operations that make the sequence tall enough for this layout.

        Emitted rather than assumed: a sequence created from a single source
        clip has one video and one audio track, so every overlay in this system
        lands on a track that does not exist yet unless something adds it.
        Returns an empty list when the sequence is already tall enough, which
        makes the call idempotent and safe to include in every plan.
        """
        video_short = max(0, self.video_tracks_needed - max(0, int(existing_video)))
        audio_short = max(0, self.audio_tracks_needed - max(0, int(existing_audio)))
        if not (video_short or audio_short):
            return []
        # One operation, not two: ``track.add`` takes both counts, and adding
        # video and audio in a single call keeps them in one undo step.
        op = {"op": "track.add",
              "note": f"make room for {', '.join(self.describe())}"}
        if video_short:
            op["video"] = video_short
        if audio_short:
            op["audio"] = audio_short
        return [op]

    def describe(self) -> list[str]:
        return [
            f"{self.track_for(role):<3} {role}" for role in ROLES
        ]

    def to_dict(self) -> dict:
        return {role: getattr(self, role) for role in ROLES}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "TrackLayout":
        data = data or {}
        known = {role: str(data[role]) for role in ROLES if data.get(role)}
        return cls(**known)

    def with_overrides(self, **overrides) -> "TrackLayout":
        """A layout with some roles moved. Unknown roles are refused loudly."""
        unknown = set(overrides) - set(ROLES)
        if unknown:
            raise ValueError(
                "Unknown track role(s): " + ", ".join(sorted(unknown))
            )
        return replace(self, **{k: str(v) for k, v in overrides.items() if v})


#: The layout this system builds when it creates its own sequence.
DEFAULT_LAYOUT = TrackLayout()


@dataclass
class TrackUsage:
    """What actually landed on each track, for the run report.

    Kept separate from the layout because it answers a different question: the
    layout is the intention, this is the evidence. A report that says "captions
    on V2" is worth nothing next to one that says "V2: 7 clips".
    """

    layout: TrackLayout = field(default_factory=lambda: DEFAULT_LAYOUT)
    counts: dict = field(default_factory=dict)

    def record(self, track: str, count: int = 1) -> None:
        key = str(track or "").strip().upper()
        if not key:
            return
        self.counts[key] = self.counts.get(key, 0) + int(count)

    def record_ops(self, ops) -> None:
        """Count every operation in a plan against the track it names."""
        for op in ops or ():
            track = op.get("track") or (op.get("clip") or {}).get("track")
            if track:
                self.record(track)

    @property
    def used_roles(self) -> list[str]:
        return [
            role for role in ROLES
            if self.counts.get(self.layout.track_for(role).upper())
        ]

    def to_dict(self) -> dict:
        return {
            "layout": self.layout.to_dict(),
            "counts": dict(sorted(self.counts.items())),
            "roles_used": self.used_roles,
            "tracks_used": len([k for k, v in self.counts.items() if v]),
        }

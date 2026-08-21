"""Choosing an asset for a placeholder.

Session 5 says *a whoosh belongs at 41.2s*. This decides which whoosh, or that
none of them will do. The second answer is the important one, and the module is
built around making it cheap to reach:

* **Category is a hard requirement**, not a score. An impact placeholder never
  gets a music track, however many tags happen to overlap.
* **A rejection reason is recorded per candidate.** "Why did it not use my
  whoosh?" is answerable from the plan without re-running anything, because
  every loser is kept with the rule that removed it.
* **Below the threshold is a refusal, not a best effort.** A weak match placed
  is worse than a marker, because a marker costs a viewer nothing and a wrong
  sound costs them the moment.

The scoring itself is deliberately small and additive, in the same shape as the
Session 5 caption scorer: every contribution is named, signed and visible in
the output, so a surprising choice can be read rather than guessed at.

**The repeated-use penalty is the one stateful rule.** A library with one
impact sound will place that sound every time and should — but if there are
three, using the same one nine times is the thing that makes an edit sound
cheap. So each prior use costs a fixed amount, which lets a lone asset keep
winning while a varied library naturally rotates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from editing.assets.schema import AssetItem, AssetLibrary, AssetMatch

#: Below this, nothing is placed. Tuned so that category + a tag hit + a
#: plausible duration clears it, and category alone does not: a random music
#: track is not a tension bed just because it is music.
DEFAULT_MIN_SCORE = 0.5

#: What each prior use of the same asset costs. Two uses still beat an
#: unsuitable alternative; five do not.
REPEAT_PENALTY = 0.18

#: Cap on tag-overlap credit, so a file named
#: ``impact_boom_hit_slam_heavy_crash.wav`` cannot win on keyword stuffing.
MAX_TAG_CREDIT = 0.30


@dataclass(frozen=True)
class Requirement:
    """What a placeholder kind needs from an asset.

    One of these per kind, in ``REQUIREMENTS``. Keeping them as data rather
    than as branches means the whole matching policy is readable in one screen,
    and ``assets report`` can print it.
    """

    #: Categories that can satisfy this kind. First is preferred.
    categories: tuple
    #: Media types that can satisfy it.
    media: tuple = ("audio",)
    #: Tags that earn credit. Not required -- a file can match on category and
    #: duration alone, just not strongly.
    tags: tuple = ()
    #: Longest sensible asset for this kind, in seconds. None means no limit.
    max_duration: Optional[float] = None
    #: Shortest sensible asset.
    min_duration: Optional[float] = None
    #: Intensities that fit. Others score lower but are not excluded.
    intensities: tuple = ("low", "medium", "high")
    #: True when the asset has to loop cleanly (a bed under a long stretch).
    needs_loop: bool = False
    #: Human phrase for the report.
    label: str = ""


#: The whole matching policy, as data.
REQUIREMENTS = {
    "impact_sfx": Requirement(
        categories=("sfx", "transition"),
        tags=("impact", "boom", "hit", "slam", "crash", "stinger", "thud",
              "punch", "explosion"),
        max_duration=3.0,
        intensities=("medium", "high"),
        label="a short, hard one-shot",
    ),
    "comedic_sfx": Requirement(
        categories=("sfx",),
        tags=("funny", "comedy", "cartoon", "pop", "boing", "slide", "quirky",
              "silly", "record", "scratch", "honk"),
        max_duration=3.0,
        label="a short comedic one-shot",
    ),
    "whoosh": Requirement(
        categories=("sfx", "transition"),
        tags=("whoosh", "swoosh", "swish", "transition", "sweep", "pass"),
        max_duration=2.5,
        label="a short transition whoosh",
    ),
    "tension_bed": Requirement(
        categories=("music", "ambience"),
        tags=("tension", "bed", "drone", "pad", "dark", "suspense", "loop"),
        min_duration=4.0,
        intensities=("low", "medium"),
        needs_loop=True,
        label="a loopable low-intensity bed",
    ),
    "music_start": Requirement(
        categories=("music",),
        tags=("theme", "track", "music", "intro", "main"),
        min_duration=8.0,
        label="a music track long enough to sit under a section",
    ),
    "music_rise": Requirement(
        categories=("music", "sfx"),
        tags=("riser", "rise", "build", "swell", "uplifter", "tension"),
        max_duration=20.0,
        label="a riser or build",
    ),
    "ambience": Requirement(
        categories=("ambience",),
        tags=("ambience", "ambient", "room", "wind", "rain", "cave", "crowd",
              "tone", "loop"),
        min_duration=4.0,
        needs_loop=True,
        label="loopable atmosphere",
    ),
    "visual_callout": Requirement(
        categories=("callout",),
        media=("image", "video"),
        tags=("arrow", "circle", "highlight", "pointer", "ring", "box"),
        label="a callout graphic",
    ),
    "callout_label": Requirement(
        categories=("callout",),
        media=("image", "video"),
        tags=("label", "tag", "name", "banner", "arrow"),
        label="a label graphic",
    ),
    "title_card": Requirement(
        categories=("title",),
        # ``mogrt`` is matched deliberately, even though nothing can place one.
        # A user with a title template should be told "found it, cannot drive
        # it, here is why" rather than "you have no title backgrounds".
        media=("image", "video", "mogrt"),
        tags=("title", "card", "plate", "background", "intro"),
        label="a title background",
    ),
    "chapter_card": Requirement(
        categories=("title",),
        media=("image", "video", "mogrt"),
        tags=("chapter", "card", "plate", "background", "section"),
        label="a chapter background",
    ),
}

#: Placeholder kinds this session deliberately leaves alone. Each one is a note
#: about the edit rather than a thing to play, so there is no asset that could
#: satisfy it and pretending otherwise would be noise.
NOT_ASSET_BACKED = frozenset({
    "silence_hold",      # the absence of sound is the point
    "duck_narration",    # an instruction about the bed, not a sound
    "beat_marker",       # an anchor for a human to cut against
    "audio_fade_in",     # already a real operation in Session 5
    "audio_fade_out",
    "structure_marker", "pacing_marker", "polish_marker",
    "reveal_marker", "danger_marker", "funny_marker",
    "reaction_caption", "key_phrase", "danger_text",
    "punch_in", "slow_push_in", "freeze_frame",
})


def requirement_for(kind: str) -> Optional[Requirement]:
    return REQUIREMENTS.get(kind)


def rank_candidates(
    kind: str,
    library: AssetLibrary,
    *,
    style: str = "",
    slot_duration: float = 0.0,
    used: Optional[dict] = None,
    min_score: float = DEFAULT_MIN_SCORE,
    allow_unsafe: bool = False,
) -> list[AssetMatch]:
    """Every asset considered for ``kind``, best first.

    ``used`` maps asset id to how many times it has already been placed in this
    plan; it is what makes the pass rotate through a varied library instead of
    leaning on whichever file happens to sort first.

    Returns matches for *every* asset in a plausible category, accepted and
    rejected alike. The caller decides what to do with the top of the list;
    keeping the rest is what makes the decision auditable.
    """
    requirement = requirement_for(kind)
    if requirement is None:
        return []

    used = used or {}
    pool = [
        item for item in library.items
        if item.category in requirement.categories
    ]

    # Scored once with no repeat penalty, so "would this asset do at all?" is
    # answered before "have we leaned on it already".
    scored: list[tuple] = []
    for item in pool:
        match = _score(
            item, requirement, style=style, slot_duration=slot_duration,
            allow_unsafe=allow_unsafe,
        )
        scored.append((item, match))

    # The repeat penalty is about *rotation*, not rationing, and it has to be
    # measured against the assets that could actually take the job. Measuring
    # it against the whole category meant an unused impact sound made repeating
    # the only whoosh look expensive -- so a third whoosh placeholder placed
    # nothing, with two perfectly good whooshes sitting in the library.
    viable = [
        item for item, match in scored
        if match.accepted and match.score >= min_score
    ]
    floor = min(
        (used.get(item.asset_id, 0) for item in viable), default=0
    )

    matches: list[AssetMatch] = []
    for item, match in scored:
        repeats = max(0, used.get(item.asset_id, 0) - floor)
        if repeats and not match.rejected:
            penalty = REPEAT_PENALTY * repeats
            match.score = max(0.0, match.score - penalty)
            match.reasons.append((
                f"already used {used.get(item.asset_id, 0)} time(s) in this "
                f"plan, while something else suitable has not been",
                -penalty,
            ))
        matches.append(match)

    matches.sort(key=lambda m: (m.rejected != "", -m.score, m.filename))
    for index, match in enumerate(matches):
        match.rank = index
        if not match.rejected and match.score < min_score:
            match.rejected = (
                f"scored {match.score:.2f}, below the {min_score:.2f} needed "
                f"to place {requirement.label} automatically"
            )
    return matches


def best_match(matches: Sequence[AssetMatch]) -> Optional[AssetMatch]:
    for match in matches:
        if match.accepted:
            return match
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(
    item: AssetItem,
    requirement: Requirement,
    *,
    style: str,
    slot_duration: float,
    allow_unsafe: bool,
) -> AssetMatch:
    match = AssetMatch(asset_id=item.asset_id, filename=item.filename)
    reasons: list = []

    # -- hard requirements, cheapest first ------------------------------
    if item.missing:
        match.rejected = "the file is no longer on disk; re-index the library."
        return match
    if item.needs_review:
        match.rejected = (
            f"it needs review: {item.review_reason or 'unreadable metadata'}"
        )
        return match
    if not item.safe_for_auto and not allow_unsafe:
        match.rejected = (
            "its sidecar sets safe_for_auto to false, so it is only ever "
            "placed by hand."
        )
        return match
    if item.media_type not in requirement.media:
        match.rejected = (
            f"it is {item.media_type} and this needs "
            + " or ".join(requirement.media) + "."
        )
        return match
    if style and style in item.avoid_styles:
        match.rejected = f"its sidecar excludes the {style} style."
        return match
    if requirement.needs_loop and not item.loopable:
        match.rejected = (
            "it is not marked loopable, and this slot needs something that "
            "can run under a whole stretch without a seam."
        )
        return match

    score = 0.0

    # Category is a requirement rather than a bonus, but the *preferred*
    # category still counts for something: an ambience file can serve as a
    # tension bed, and a music file is the better answer when there is one.
    if item.category == requirement.categories[0]:
        score += 0.30
        reasons.append((f"in the preferred category ({item.category})", 0.30))
    else:
        score += 0.18
        reasons.append((f"in an acceptable category ({item.category})", 0.18))

    hits = item.matching_tags(requirement.tags)
    if hits:
        credit = min(MAX_TAG_CREDIT, 0.14 * len(hits))
        score += credit
        reasons.append((f"tags match: {', '.join(hits)}", credit))
    else:
        reasons.append(("no tag matched this kind", 0.0))

    # The canonical word for the kind, if the requirement names one. A file
    # called `whoosh_fast_01.wav` answering a `whoosh` placeholder is the
    # strongest evidence this system ever gets, and scoring it the same as any
    # other tag hit meant that on a machine without ffprobe -- where nothing
    # has a duration and nothing earns the duration credit -- it landed just
    # under the threshold and placed nothing at all.
    if requirement.tags and requirement.tags[0] in item.tag_names:
        score += 0.10
        reasons.append((
            f"its name says '{requirement.tags[0]}', which is exactly what "
            "this kind is", 0.10,
        ))

    duration_delta, duration_why = _duration_credit(
        item, requirement, slot_duration
    )
    score += duration_delta
    reasons.append((duration_why, duration_delta))

    if item.intensity in requirement.intensities:
        credit = 0.12 if len(requirement.intensities) < 3 else 0.06
        score += credit
        reasons.append((f"{item.intensity} intensity suits this", credit))
    else:
        score -= 0.12
        reasons.append((
            f"{item.intensity} intensity is not what this wants "
            f"({' or '.join(requirement.intensities)})", -0.12,
        ))

    if style and style in item.preferred_styles:
        score += 0.15
        reasons.append((f"its sidecar prefers the {style} style", 0.15))

    if item.has_sidecar:
        score += 0.05
        reasons.append(("described by a sidecar, so this is not guesswork", 0.05))

    match.score = max(0.0, min(1.0, score))
    match.reasons = reasons
    return match


def _duration_credit(
    item: AssetItem, requirement: Requirement, slot_duration: float
) -> tuple:
    """How well the asset's length suits the slot.

    An unknown duration is explicitly *neutral*, not a penalty. Without ffprobe
    nothing has a duration, and treating that as a poor fit would make an
    ffmpeg-less machine place nothing at all -- which is a worse failure than
    placing something slightly long.
    """
    length = item.effective_duration
    if length is None:
        return 0.0, "length unknown (no probe and no sidecar), so not judged"

    if requirement.max_duration is not None and length > requirement.max_duration:
        # Long is a real problem for a one-shot: a four-second "impact" is a
        # small piece of music landing on a cut.
        over = length - requirement.max_duration
        penalty = min(0.35, 0.12 + over * 0.05)
        return -penalty, (
            f"{length:.2f}s is longer than the {requirement.max_duration:g}s "
            "this kind should be"
        )

    if requirement.min_duration is not None and length < requirement.min_duration:
        if item.loopable:
            return 0.04, (
                f"{length:.2f}s is short for this, but it loops, so it can be "
                "tiled to fill the slot"
            )
        return -0.20, (
            f"{length:.2f}s is shorter than the {requirement.min_duration:g}s "
            "this kind needs, and it does not loop"
        )

    if slot_duration > 0 and requirement.needs_loop is False:
        if length > slot_duration * 2.5 and requirement.max_duration is None:
            return 0.02, (
                f"{length:.2f}s is well over the {slot_duration:.1f}s slot; it "
                "will be trimmed"
            )

    return 0.15, f"{length:.2f}s fits this kind"


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def coverage(library: AssetLibrary, *, style: str = "") -> dict:
    """How many usable assets exist per placeholder kind.

    What ``assets report`` prints, and the answer to "what should I go and
    find?" -- a kind with zero usable assets will never place anything, however
    good the rest of the library is.
    """
    out: dict = {}
    for kind, requirement in REQUIREMENTS.items():
        candidates = [
            item for item in library.items
            if item.category in requirement.categories
            and item.media_type in requirement.media
            and item.usable
            and not (style and style in item.avoid_styles)
            and not (requirement.needs_loop and not item.loopable)
        ]
        tagged = [item for item in candidates if item.has_any_tag(requirement.tags)]
        out[kind] = {
            "label": requirement.label,
            "candidates": len(candidates),
            "well_tagged": len(tagged),
            "categories": list(requirement.categories),
            "needs_loop": requirement.needs_loop,
        }
    return out

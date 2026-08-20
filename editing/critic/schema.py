"""What a criticism of a rough cut is, as data.

Two record types, deliberately kept apart:

``CriticFinding``
    What the model said about one frame. It is an *observation*: an issue type,
    how sure the model is, and the pixels it says show it. A finding never
    proposes a Premiere operation, and nothing downstream may act on one
    directly.

``RevisionRecommendation``
    What the system proposes doing about a finding. It carries the fix, the
    risk of applying it, and a ``status`` that decides whether it may become an
    operation at all.

Keeping them separate is what makes the "unsafe findings stay
recommendations" rule enforceable rather than aspirational: the conversion
from one to the other is a single function with explicit rules, and a finding
that fails those rules still lands in the output as a recommendation a human
can read.

Three invariants the schema enforces:

* **A closed issue vocabulary.** Anything the model invents coerces to
  ``needs_human_review`` rather than being dropped -- the system must be able
  to say "the critic saw something I do not understand here".
* **Nothing is executed by being written down.** ``RevisionPlan`` defaults
  ``dry_run_passed`` to False and carries its unconvertible list, exactly like
  ``RoughCutPlan``.
* **Confidence and severity are separate.** A high-severity issue the model is
  unsure about is not the same thing as a certain small one, and the planner
  gates on both.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from editing.schema import _slug, as_float, as_str_list, clamp01, short_hash

#: Everything the critic is allowed to report. Closed on purpose: a free-text
#: issue field would make the planner's safety rules unenforceable, because
#: there would always be a new phrasing that no rule covers.
ISSUE_TYPES = (
    "bad_crop",                 # the framing itself is wrong
    "hud_hidden",               # health/hunger/hotbar cropped or covered
    "action_hidden",            # the mob/item/player action is out of frame
    "zoom_too_strong",          # the punch-in went too far
    "text_unreadable",          # too small, too low contrast, or motion-blurred
    "text_placed_badly",        # readable, but in the wrong part of the frame
    "caption_covers_gameplay",  # the caption sits on what matters
    "too_dark",
    "too_bright",
    "boring_too_long",          # a dull stretch survived the cut
    "cut_too_early",            # the clip ends before the beat resolves
    "cut_too_late",             # the clip runs past the beat
    "marker_mismatch",          # the marker does not describe this frame
    "callout_needed",           # the viewer will not find the thing unaided
    "hold_longer",              # the moment needs more room
    "remove_edit",              # the edit here is doing harm
    "needs_human_review",       # the critic is unsure; a person must look
)

#: Issues where the critic is explicitly declining to judge. These can never
#: become an automatic fix, only a marker.
UNCERTAIN_ISSUES = frozenset({"needs_human_review"})

SEVERITIES = ("low", "medium", "high")

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

#: What a revision proposes doing. ``none`` means "recorded, nothing proposed".
FIXES = (
    "remove_zoom",             # drop the zoom entirely, back to 100%
    "reduce_zoom",             # keep it, smaller
    "move_text_placeholder",   # re-site a text/caption placeholder marker
    "color_marker",            # a brightness/colour note for the human
    "extend_hold",             # give the moment slightly more room
    "trim_dead_air",           # take a little dead air off an edge
    "callout_marker",          # flag where a callout graphic belongs
    "review_marker",           # "a person needs to look at this"
    "shorten_section",         # a dull stretch should be cut down
    "reframe",                 # the shot needs re-composing
    "none",
)

#: The fix proposed for each issue when the critic does not name one itself.
#: Everything not listed here falls back to ``review_marker`` -- the honest
#: answer for an issue with no safe automatic response.
DEFAULT_FIX = {
    "zoom_too_strong": "reduce_zoom",
    "hud_hidden": "remove_zoom",
    "action_hidden": "remove_zoom",
    "bad_crop": "reframe",
    "text_unreadable": "move_text_placeholder",
    "text_placed_badly": "move_text_placeholder",
    "caption_covers_gameplay": "move_text_placeholder",
    "too_dark": "color_marker",
    "too_bright": "color_marker",
    "boring_too_long": "shorten_section",
    "cut_too_early": "extend_hold",
    "cut_too_late": "trim_dead_air",
    "marker_mismatch": "review_marker",
    "callout_needed": "callout_marker",
    "hold_longer": "extend_hold",
    "remove_edit": "remove_zoom",
    "needs_human_review": "review_marker",
}

#: Severity assumed when the critic does not give one. Chosen per issue rather
#: than a flat "medium": a hidden HUD is a real defect, a callout suggestion is
#: taste.
DEFAULT_SEVERITY = {
    "hud_hidden": "high",
    "action_hidden": "high",
    "zoom_too_strong": "medium",
    "bad_crop": "medium",
    "text_unreadable": "medium",
    "text_placed_badly": "low",
    "caption_covers_gameplay": "medium",
    "too_dark": "medium",
    "too_bright": "medium",
    "boring_too_long": "low",
    "cut_too_early": "medium",
    "cut_too_late": "low",
    "marker_mismatch": "low",
    "callout_needed": "low",
    "hold_longer": "low",
    "remove_edit": "high",
    "needs_human_review": "low",
}

#: Fixes the planner is allowed to turn into draft operations. Everything else
#: stays a recommendation with a reason, however confident the critic was.
#:
#: ``reframe`` and ``shorten_section`` are deliberately absent: re-composing a
#: shot is not something this system can do, and shortening a section is a
#: timing change big enough that a person should make it.
SAFE_FIXES = frozenset({
    "remove_zoom", "reduce_zoom", "move_text_placeholder", "color_marker",
    "extend_hold", "trim_dead_air", "callout_marker", "review_marker",
})

#: What could go wrong with applying a revision. Present on every one,
#: including accepted ones.
REVISION_RISKS = (
    "low_confidence",       # the critic was not sure
    "model_uncertainty",    # the critic explicitly asked for a human
    "changes_timing",       # the fix moves later clips
    "removes_an_edit",      # the fix undoes something the planner wanted
    "annotation_only",      # the "fix" is a note, not a change
    "hides_gameplay",       # applying it could cover something
    "unnecessary",          # it may be fixing a non-problem
    "not_verifiable",       # nothing on the timeline confirms the premise
)

REVISION_STATUSES = ("accepted", "rejected", "needs_human_review")


def _coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def coerce_issue(value: Any) -> str:
    """Map whatever the model said onto the issue vocabulary.

    Unrecognised issues become ``needs_human_review`` rather than being
    dropped: the critic noticing *something* is information, even when the
    phrasing is not one this system knows how to act on.
    """
    token = _slug(value)
    if token in ISSUE_TYPES:
        return token
    return _ISSUE_SYNONYMS.get(token, "needs_human_review")


#: Phrasings small VLMs reach for that mean one of our issues. Kept short and
#: literal -- this is a spelling correction, not an interpretation layer.
_ISSUE_SYNONYMS = {
    "crop": "bad_crop", "cropped": "bad_crop", "badly_cropped": "bad_crop",
    "framing": "bad_crop", "bad_framing": "bad_crop",
    "hud_cut_off": "hud_hidden", "hud_obscured": "hud_hidden",
    "hud_covered": "hud_hidden", "ui_hidden": "hud_hidden",
    "action_obscured": "action_hidden", "subject_hidden": "action_hidden",
    "mob_hidden": "action_hidden", "player_hidden": "action_hidden",
    "zoom_too_much": "zoom_too_strong", "over_zoomed": "zoom_too_strong",
    "too_zoomed_in": "zoom_too_strong", "excessive_zoom": "zoom_too_strong",
    "text_too_small": "text_unreadable", "unreadable_text": "text_unreadable",
    "text_bad_position": "text_placed_badly",
    "caption_blocks_gameplay": "caption_covers_gameplay",
    "dark": "too_dark", "underexposed": "too_dark",
    "bright": "too_bright", "overexposed": "too_bright", "blown_out": "too_bright",
    "boring": "boring_too_long", "too_long": "boring_too_long",
    "dead_air": "boring_too_long", "slow": "boring_too_long",
    "early_cut": "cut_too_early", "cut_early": "cut_too_early",
    "late_cut": "cut_too_late", "cut_late": "cut_too_late",
    "marker_wrong": "marker_mismatch", "wrong_marker": "marker_mismatch",
    "needs_callout": "callout_needed", "callout": "callout_needed",
    "hold": "hold_longer", "needs_longer_hold": "hold_longer",
    "remove": "remove_edit", "undo_edit": "remove_edit",
    "uncertain": "needs_human_review", "unsure": "needs_human_review",
    "unclear": "needs_human_review", "human_review": "needs_human_review",
}


def coerce_severity(value: Any, *, issue: str = "") -> str:
    token = _slug(value)
    if token in SEVERITIES:
        return token
    return DEFAULT_SEVERITY.get(issue, "medium")


def coerce_fix(value: Any, *, issue: str = "") -> str:
    token = _slug(value)
    if token in FIXES:
        return token
    return _FIX_SYNONYMS.get(token) or DEFAULT_FIX.get(issue, "review_marker")


_FIX_SYNONYMS = {
    "reduce_zoom_amount": "reduce_zoom", "smaller_zoom": "reduce_zoom",
    "zoom_out": "reduce_zoom",
    "remove_the_zoom": "remove_zoom", "no_zoom": "remove_zoom",
    "disable_zoom": "remove_zoom", "remove_edit": "remove_zoom",
    "move_text": "move_text_placeholder", "move_caption": "move_text_placeholder",
    "reposition_text": "move_text_placeholder",
    "brighten": "color_marker", "darken": "color_marker",
    "colour_marker": "color_marker", "color_correct": "color_marker",
    "extend": "extend_hold", "hold_longer": "extend_hold",
    "trim": "trim_dead_air", "cut_dead_air": "trim_dead_air",
    "add_callout": "callout_marker",
    "flag_for_review": "review_marker", "add_marker": "review_marker",
    "human_review": "review_marker",
    "shorten": "shorten_section", "cut_down": "shorten_section",
    "recompose": "reframe", "reframe_shot": "reframe",
}


# ---------------------------------------------------------------------------
# Critic findings
# ---------------------------------------------------------------------------

@dataclass
class CriticFinding:
    """One thing the critic says is wrong with one frame.

    ``raw_issue`` and ``raw_fix`` keep the model's own wording. When a finding
    comes back as ``needs_human_review`` only because the vocabulary did not
    cover it, those fields are the only place the actual observation survives,
    and the report prints them.
    """

    finding_id: str
    frame_id: str
    issue: str = "needs_human_review"
    severity: str = "medium"
    confidence: float = 0.5
    #: What in the picture the critic says shows this.
    evidence: str = ""
    #: The fix the critic proposed, coerced onto ``FIXES``.
    suggested_fix: str = "review_marker"
    notes: str = ""
    raw_issue: str = ""
    raw_fix: str = ""
    #: Frame position on the sequence, denormalised so a finding reads alone.
    sequence_time: float = 0.0
    placement_id: str = ""
    #: True when this came from the mock critic rather than a real model.
    mock: bool = False

    @property
    def is_uncertain(self) -> bool:
        return self.issue in UNCERTAIN_ISSUES

    def to_dict(self) -> dict:
        data = asdict(self)
        data["confidence"] = round(self.confidence, 3)
        data["sequence_time"] = round(self.sequence_time, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CriticFinding":
        """Coerce one raw finding object from the model.

        Every field is optional in the input. A finding with nothing but an
        issue string is still a usable finding; one with nothing at all becomes
        ``needs_human_review`` at low confidence, which is the correct reading
        of an empty object.
        """
        raw_issue = str(data.get("issue") or data.get("type")
                        or data.get("problem") or "")
        issue = coerce_issue(raw_issue)
        raw_fix = str(data.get("suggested_fix") or data.get("fix")
                      or data.get("suggestion") or "")
        frame_id = str(data.get("frame_id") or "")
        evidence = str(
            data.get("evidence") or data.get("reason") or data.get("why") or ""
        )[:600]
        return cls(
            finding_id=str(data.get("finding_id") or "") or (
                "cf_" + short_hash(frame_id, issue, evidence[:80])
            ),
            frame_id=frame_id,
            issue=issue,
            severity=coerce_severity(
                data.get("severity") or data.get("importance"), issue=issue
            ),
            confidence=clamp01(
                data.get("confidence", data.get("score", 0.5)), 0.5
            ),
            evidence=evidence,
            suggested_fix=coerce_fix(raw_fix, issue=issue),
            notes=str(data.get("notes") or "")[:600],
            raw_issue=raw_issue[:120],
            raw_fix=raw_fix[:120],
            sequence_time=max(0.0, as_float(data.get("sequence_time"))),
            placement_id=str(data.get("placement_id") or ""),
            mock=bool(data.get("mock")),
        )


@dataclass
class CriticReport:
    """Everything the critic said about one rough cut."""

    sequence_name: str = ""
    findings: list[CriticFinding] = field(default_factory=list)
    model: str = ""
    generated_at: str = ""
    frames_examined: int = 0
    frames_clean: int = 0
    frames_failed: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    elapsed: float = 0.0
    warnings: list[str] = field(default_factory=list)
    #: True when any finding came from the mock critic. Surfaced everywhere so
    #: a mock pass can never be mistaken for a real one.
    mock: bool = False
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.findings)

    def by_severity(self, severity: str) -> list[CriticFinding]:
        return [f for f in self.findings if f.severity == severity]

    def for_frame(self, frame_id: str) -> list[CriticFinding]:
        return [f for f in self.findings if f.frame_id == frame_id]

    def stats(self) -> dict:
        by_issue: dict = {}
        by_severity: dict = {}
        for finding in self.findings:
            by_issue[finding.issue] = by_issue.get(finding.issue, 0) + 1
            by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        return {
            "findings": len(self.findings),
            "frames_examined": self.frames_examined,
            "frames_clean": self.frames_clean,
            "frames_failed": self.frames_failed,
            "frames_with_findings": len({f.frame_id for f in self.findings}),
            "uncertain": sum(1 for f in self.findings if f.is_uncertain),
            "by_issue": by_issue,
            "by_severity": by_severity,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sequence_name": self.sequence_name,
            "model": self.model,
            "mock": self.mock,
            "elapsed": round(self.elapsed, 2),
            "cache": {"hits": self.cache_hits, "misses": self.cache_misses},
            "stats": self.stats(),
            "warnings": list(self.warnings),
            "findings": [f.to_dict() for f in self.findings],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CriticReport":
        stats = data.get("stats") or {}
        cache = data.get("cache") or {}
        return cls(
            sequence_name=str(data.get("sequence_name") or ""),
            findings=[
                CriticFinding.from_dict(f) for f in (data.get("findings") or [])
            ],
            model=str(data.get("model") or ""),
            generated_at=str(data.get("generated_at") or ""),
            frames_examined=int(as_float(stats.get("frames_examined"))),
            frames_clean=int(as_float(stats.get("frames_clean"))),
            frames_failed=int(as_float(stats.get("frames_failed"))),
            cache_hits=int(as_float(cache.get("hits"))),
            cache_misses=int(as_float(cache.get("misses"))),
            elapsed=as_float(data.get("elapsed")),
            warnings=as_str_list(data.get("warnings"), limit=200),
            mock=bool(data.get("mock")),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )


# ---------------------------------------------------------------------------
# Revision recommendations
# ---------------------------------------------------------------------------

@dataclass
class RevisionRecommendation:
    """One proposed change to the rough cut, with everything needed to judge it.

    ``status`` is the gate. Only ``accepted`` reaches the operation plan, and
    the planner is the only thing that sets it -- a finding arrives here as
    ``needs_human_review`` and has to earn its way up.
    """

    revision_id: str
    #: The Session 2 recommendation this revises, when the frame was placed by
    #: one. Empty when the critic found something nothing proposed.
    source_recommendation_id: str = ""
    #: The critic finding behind it.
    finding_id: str = ""
    frame_id: str = ""
    placement_id: str = ""
    #: Position on the *rough cut sequence*, not in source time.
    start: float = 0.0
    end: float = 0.0
    issue: str = "needs_human_review"
    severity: str = "medium"
    confidence: float = 0.5
    #: What the critic saw. Free text from the model, kept verbatim.
    visual_evidence: str = ""
    transcript_evidence: str = ""
    audio_evidence: list[str] = field(default_factory=list)
    suggested_fix: str = "review_marker"
    #: One sentence a human can act on.
    fix_detail: str = ""
    risks: list[str] = field(default_factory=list)
    status: str = "needs_human_review"
    status_reason: str = ""
    #: Draft Premiere operations. Empty unless the fix was safely convertible.
    premiere_ops: list[dict] = field(default_factory=list)
    notes: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def is_actionable(self) -> bool:
        """Whether this may reach the revision plan."""
        return self.status == "accepted" and bool(self.premiere_ops)

    @property
    def needs_human(self) -> bool:
        return self.status == "needs_human_review"

    def accept(self, reason: str, ops: Sequence[dict]) -> "RevisionRecommendation":
        self.status = "accepted"
        self.status_reason = reason
        self.premiere_ops = [dict(op) for op in ops]
        return self

    def defer(self, reason: str) -> "RevisionRecommendation":
        """Keep it as a recommendation: real, but not safely automatable.

        Deferring never clears the evidence, and never pretends the issue went
        away. It is the honest output for most of what a critic finds.
        """
        self.status = "needs_human_review"
        self.status_reason = reason
        self.premiere_ops = []
        if "model_uncertainty" not in self.risks:
            self.risks.append("model_uncertainty")
        return self

    def reject(self, reason: str) -> "RevisionRecommendation":
        self.status = "rejected"
        self.status_reason = reason
        self.premiere_ops = []
        return self

    def summary(self) -> str:
        marks = {"accepted": "+", "rejected": "-", "needs_human_review": "?"}
        return (
            f"{marks.get(self.status, '?')} [{self.start:7.2f}] "
            f"{self.issue:<24} {self.severity:<6} c={self.confidence:.2f}  "
            f"{self.suggested_fix:<22} {self.visual_evidence[:60]}"
        )

    def to_dict(self) -> dict:
        return {
            "revision_id": self.revision_id,
            "source_recommendation_id": self.source_recommendation_id,
            "finding_id": self.finding_id,
            "frame_id": self.frame_id,
            "placement_id": self.placement_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "issue": self.issue,
            "severity": self.severity,
            "confidence": round(self.confidence, 3),
            "visual_evidence": self.visual_evidence,
            "transcript_evidence": self.transcript_evidence,
            "audio_evidence": list(self.audio_evidence),
            "suggested_fix": self.suggested_fix,
            "fix_detail": self.fix_detail,
            "risks": list(self.risks),
            "status": self.status,
            "status_reason": self.status_reason,
            "premiere_ops": [dict(op) for op in self.premiere_ops],
            "notes": self.notes,
            "is_actionable": self.is_actionable,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RevisionRecommendation":
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        issue = coerce_issue(data.get("issue"))
        return cls(
            revision_id=str(data.get("revision_id") or "") or (
                "rv_" + short_hash(data.get("frame_id"), issue, start)
            ),
            source_recommendation_id=str(
                data.get("source_recommendation_id") or ""
            ),
            finding_id=str(data.get("finding_id") or ""),
            frame_id=str(data.get("frame_id") or ""),
            placement_id=str(data.get("placement_id") or ""),
            start=start,
            end=end,
            issue=issue,
            severity=coerce_severity(data.get("severity"), issue=issue),
            confidence=clamp01(data.get("confidence", 0.5), 0.5),
            visual_evidence=str(data.get("visual_evidence") or "")[:600],
            transcript_evidence=str(data.get("transcript_evidence") or "")[:600],
            audio_evidence=as_str_list(data.get("audio_evidence"), limit=20),
            suggested_fix=coerce_fix(data.get("suggested_fix"), issue=issue),
            fix_detail=str(data.get("fix_detail") or "")[:600],
            risks=[
                risk for risk in as_str_list(data.get("risks"), limit=20)
                if risk in REVISION_RISKS
            ],
            status=_coerce_one(
                data.get("status"), REVISION_STATUSES, "needs_human_review"
            ),
            status_reason=str(data.get("status_reason") or "")[:600],
            premiere_ops=[
                dict(op) for op in (data.get("premiere_ops") or [])
                if isinstance(op, dict)
            ],
            notes=str(data.get("notes") or "")[:600],
        )


@dataclass
class RevisionSet:
    """Every revision proposed for one rough cut."""

    sequence_name: str = ""
    revisions: list[RevisionRecommendation] = field(default_factory=list)
    generated_at: str = ""
    #: Carried through from the critic report so the provenance never splits.
    model: str = ""
    mock: bool = False
    warnings: list[str] = field(default_factory=list)
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.revisions)

    def accepted(self) -> list[RevisionRecommendation]:
        return [r for r in self.revisions if r.status == "accepted"]

    def actionable(self) -> list[RevisionRecommendation]:
        return [r for r in self.revisions if r.is_actionable]

    def needing_human(self) -> list[RevisionRecommendation]:
        return [r for r in self.revisions if r.needs_human]

    def rejected(self) -> list[RevisionRecommendation]:
        return [r for r in self.revisions if r.status == "rejected"]

    def by_severity(self, severity: str) -> list[RevisionRecommendation]:
        return [r for r in self.revisions if r.severity == severity]

    def ranked(self) -> list[RevisionRecommendation]:
        """Worst first: severity, then confidence, then position."""
        return sorted(
            self.revisions,
            key=lambda r: (
                -SEVERITY_ORDER.get(r.severity, 0), -r.confidence, r.start
            ),
        )

    def stats(self) -> dict:
        by_status: dict = {}
        by_issue: dict = {}
        by_fix: dict = {}
        by_severity: dict = {}
        for entry in self.revisions:
            by_status[entry.status] = by_status.get(entry.status, 0) + 1
            by_issue[entry.issue] = by_issue.get(entry.issue, 0) + 1
            by_fix[entry.suggested_fix] = by_fix.get(entry.suggested_fix, 0) + 1
            by_severity[entry.severity] = by_severity.get(entry.severity, 0) + 1
        return {
            "total": len(self.revisions),
            "accepted": len(self.accepted()),
            "actionable": len(self.actionable()),
            "needs_human_review": len(self.needing_human()),
            "rejected": len(self.rejected()),
            "by_status": by_status,
            "by_issue": by_issue,
            "by_fix": by_fix,
            "by_severity": by_severity,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sequence_name": self.sequence_name,
            "model": self.model,
            "mock": self.mock,
            "stats": self.stats(),
            "warnings": list(self.warnings),
            "revisions": [r.to_dict() for r in self.revisions],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RevisionSet":
        return cls(
            sequence_name=str(data.get("sequence_name") or ""),
            revisions=[
                RevisionRecommendation.from_dict(r)
                for r in (data.get("revisions") or [])
            ],
            generated_at=str(data.get("generated_at") or ""),
            model=str(data.get("model") or ""),
            mock=bool(data.get("mock")),
            warnings=as_str_list(data.get("warnings"), limit=200),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )


@dataclass
class NotApplied:
    """A revision that did not become an operation, and why.

    The mirror of ``roughcut.Unconverted``: "what could this not fix" is one of
    the questions the session brief asks the system to answer out loud.
    """

    revision_id: str
    issue: str
    fix: str
    start: float
    reason: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = round(self.start, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "NotApplied":
        return cls(
            revision_id=str(data.get("revision_id") or ""),
            issue=coerce_issue(data.get("issue")),
            fix=coerce_fix(data.get("fix")),
            start=as_float(data.get("start")),
            reason=str(data.get("reason") or "")[:600],
        )


@dataclass
class RevisionPlan:
    """The operations one revision pass would apply, ready to validate.

    Deliberately the same shape as ``RoughCutPlan`` where it can be: the same
    dry-run contract, the same "executed is never implicit" rule, the same
    ``as_edit_plan`` handoff to ``premiere.validator``. A reviewer who has read
    one can read the other.
    """

    sequence_name: str = ""
    ops: list[dict] = field(default_factory=list)
    #: Revision IDs realised by ``ops``, in the order they were applied.
    revision_ids: list[str] = field(default_factory=list)
    not_applied: list[NotApplied] = field(default_factory=list)
    generated_at: str = ""
    dry_run_passed: bool = False
    dry_run_error: Optional[dict] = None
    explanation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Whether the sequence this revises is the rough cut's scratch sequence.
    on_scratch: bool = True
    #: Whether the rough cut it revises was actually executed into Premiere.
    #: False means the sequence probably does not exist yet.
    roughcut_executed: bool = False
    #: Always False here; execution writes its own report.
    executed: bool = False
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.ops)

    @property
    def operation_count(self) -> int:
        return len(self.ops)

    def as_edit_plan(self, *, dry_run: bool = True) -> dict:
        plan: dict = {
            "ops": list(self.ops),
            "on_error": "abort",
            "label": f"editing-brain-v1 revision: {self.sequence_name}",
        }
        if dry_run:
            plan["dry_run"] = True
        return plan

    def stats(self) -> dict:
        by_op: dict = {}
        for op in self.ops:
            name = str(op.get("op") or "?")
            by_op[name] = by_op.get(name, 0) + 1
        by_reason: dict = {}
        for entry in self.not_applied:
            by_reason[entry.fix] = by_reason.get(entry.fix, 0) + 1
        return {
            "operations": len(self.ops),
            "revisions_applied": len(self.revision_ids),
            "not_applied": len(self.not_applied),
            "by_operation": by_op,
            "not_applied_by_fix": by_reason,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "sequence_name": self.sequence_name,
            "on_scratch": self.on_scratch,
            "roughcut_executed": self.roughcut_executed,
            "executed": self.executed,
            "dry_run_passed": self.dry_run_passed,
            "dry_run_error": self.dry_run_error,
            "explanation": list(self.explanation),
            "warnings": list(self.warnings),
            "stats": self.stats(),
            "revision_ids": list(self.revision_ids),
            "not_applied": [n.to_dict() for n in self.not_applied],
            "plan": self.as_edit_plan(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RevisionPlan":
        return cls(
            sequence_name=str(data.get("sequence_name") or ""),
            ops=[
                dict(op) for op in ((data.get("plan") or {}).get("ops") or [])
                if isinstance(op, dict)
            ],
            revision_ids=as_str_list(data.get("revision_ids"), limit=500),
            not_applied=[
                NotApplied.from_dict(n) for n in (data.get("not_applied") or [])
            ],
            generated_at=str(data.get("generated_at") or ""),
            dry_run_passed=bool(data.get("dry_run_passed")),
            dry_run_error=data.get("dry_run_error"),
            explanation=as_str_list(data.get("explanation"), limit=500),
            warnings=as_str_list(data.get("warnings"), limit=200),
            on_scratch=bool(data.get("on_scratch", True)),
            roughcut_executed=bool(data.get("roughcut_executed")),
            executed=bool(data.get("executed")),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )


def revision_id_for(frame_id: str, issue: str, start: float) -> str:
    return "rv_" + short_hash(frame_id, issue, round(float(start), 3))


def finding_id_for(frame_id: str, issue: str, evidence: str) -> str:
    return "cf_" + short_hash(frame_id, issue, str(evidence)[:80])

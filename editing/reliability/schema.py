"""Reliability gates: what a run has to be true about itself.

Not to be confused with ``editing/auto/gates.py``, which is about *permission*
-- what may be executed against Premiere. These gates are about *validity*: is
the thing this run produced something a person can use, and if not, exactly
which part of it is wrong.

## Three statuses and one question

``pass``    the check looked and found nothing wrong
``warn``    something is worth knowing and the output is still usable
``fail``    the output is not valid for this reason
``skipped`` the check does not apply -- the pass it is about did not run

The question every gate answers on top of that is ``can_continue``: whether the
run's result is still worth looking at. A warning never stops anything. A
failure stops only when the output is *clearly* invalid -- no footage at all, a
render that claims a video and has none, a cut with no duration. Everything
else is a warning with a fix attached, because a pipeline that refuses to
finish over a caption density is a pipeline nobody will run twice.

## Every gate carries its evidence

A gate that says "the transcript confidence is low" and does not say what the
confidence was, over how many words, is an opinion. ``evidence`` is what the
check actually measured, and ``suggested_fix`` is the command or the change
that would address it. Both are required by convention; a gate with neither is
a gate that will be ignored.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from editing.schema import _slug, as_float, as_text_list

STATUSES = ("pass", "warn", "fail", "skipped")

#: Statuses that mean the check found nothing to act on.
CLEAN = frozenset({"pass", "skipped"})

#: Worst-first, for sorting a report and for picking the run's own status.
SEVERITY = {"fail": 0, "warn": 1, "pass": 2, "skipped": 3}

#: Every gate, in the order a report prints them: inputs first, then the
#: decisions, then the output. Reading top to bottom follows the pipeline.
GATE_NAMES = (
    "footage",
    "transcript",
    "transcript_confidence",
    "hook",
    "director",
    "retention_length",
    "cold_open_duplicate",
    "story_warnings",
    "compression",
    "caption_density",
    "sfx_density",
    "missing_assets",
    "render_output",
    "render_size",
    "output_duration",
)

#: One line per gate, for ``show-checks`` and for the report's index.
GATE_TITLES = {
    "footage": "Footage was found and probed",
    "transcript": "The episode has words in it",
    "transcript_confidence": "The transcript is confident enough to build on",
    "hook": "An opening hook was found",
    "director": "The director's decisions survived the rules",
    "retention_length": "The reshaped cut is still an episode",
    "cold_open_duplicate": "The cold open does not play twice",
    "story_warnings": "Setups and payoffs are still paired",
    "compression": "The story was not compressed away",
    "caption_density": "Captions are punctuation, not subtitles",
    "sfx_density": "Sound effects mark moments rather than fill them",
    "missing_assets": "Every placed sound exists",
    "render_output": "The render produced the file it claims",
    "render_size": "The rendered file is big enough to be a video",
    "output_duration": "The output runtime is plausible",
}

#: Said on every reliability report.
NOT_A_QUALITY_JUDGEMENT = (
    "These checks look at shape, not at taste. A run that passes every gate "
    "can still be a bad edit, and the only way to find that out is to watch "
    "it."
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


@dataclass
class GateResult:
    """One check, and everything a person needs to act on it."""

    name: str = ""
    title: str = ""
    status: str = "skipped"
    #: What the check concluded, in one sentence.
    reason: str = ""
    #: What it actually measured. Numbers, not adjectives.
    evidence: dict = field(default_factory=dict)
    #: The command or the change that would address it.
    suggested_fix: str = ""
    #: Whether the run's output is still worth looking at despite this.
    can_continue: bool = True
    schema_version: int = 1

    @property
    def ok(self) -> bool:
        return self.status in CLEAN

    def line(self) -> str:
        mark = {"pass": "+", "warn": "!", "fail": "x", "skipped": "."}.get(
            self.status, "?")
        return f"{mark} {self.name:<22} {self.status:<8} {self.reason[:96]}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["ok"] = self.ok
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "GateResult":
        data = data or {}
        status = _slug(data.get("status"))
        return cls(
            name=_text(data.get("name"), 60),
            title=_text(data.get("title"), 200),
            status=status if status in STATUSES else "skipped",
            reason=_text(data.get("reason"), 600),
            evidence=dict(data.get("evidence") or {}),
            suggested_fix=_text(data.get("suggested_fix"), 400),
            can_continue=bool(data.get("can_continue", True)),
        )


def gate(
    name: str,
    status: str,
    reason: str,
    *,
    evidence: Optional[dict] = None,
    fix: str = "",
    can_continue: bool = True,
) -> GateResult:
    """Build one result. The only constructor the checks use."""
    return GateResult(
        name=name,
        title=GATE_TITLES.get(name, name.replace("_", " ")),
        status=status if status in STATUSES else "skipped",
        reason=reason,
        evidence=dict(evidence or {}),
        suggested_fix=fix,
        # A passing or skipped gate can never block, whatever a caller passes.
        can_continue=True if status in CLEAN else bool(can_continue),
    )


def skipped(name: str, reason: str) -> GateResult:
    return gate(name, "skipped", reason)


@dataclass
class GateReport:
    """Every check for one run, and what they add up to."""

    run_id: str = ""
    gates: list[GateResult] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.gates)

    def get(self, name: str) -> Optional[GateResult]:
        for result in self.gates:
            if result.name == name:
                return result
        return None

    def of_status(self, *statuses: str) -> list[GateResult]:
        wanted = set(statuses)
        return [result for result in self.gates if result.status in wanted]

    @property
    def failures(self) -> list[GateResult]:
        return self.of_status("fail")

    @property
    def warnings(self) -> list[GateResult]:
        return self.of_status("warn")

    @property
    def blocking(self) -> list[GateResult]:
        """Failures that mean the output is not worth looking at."""
        return [result for result in self.failures if not result.can_continue]

    @property
    def status(self) -> str:
        """The worst status any gate reported."""
        if not self.gates:
            return "skipped"
        return min(
            (result.status for result in self.gates),
            key=lambda status: SEVERITY.get(status, 9),
        )

    @property
    def usable(self) -> bool:
        """Whether anything is left worth reviewing."""
        return not self.blocking

    def stats(self) -> dict:
        by_status: dict = {}
        for result in self.gates:
            by_status[result.status] = by_status.get(result.status, 0) + 1
        return {
            "gates": len(self.gates),
            "passed": len(self.of_status("pass")),
            "warned": len(self.warnings),
            "failed": len(self.failures),
            "skipped": len(self.of_status("skipped")),
            "blocking": len(self.blocking),
            "status": self.status,
            "usable": self.usable,
            "by_status": by_status,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "stats": self.stats(),
            "not_a_quality_judgement": NOT_A_QUALITY_JUDGEMENT,
            "gates": [result.to_dict() for result in self.gates],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GateReport":
        data = data or {}
        return cls(
            run_id=_text(data.get("run_id"), 120),
            gates=[
                GateResult.from_dict(item)
                for item in (data.get("gates") or [])
                if isinstance(item, dict)
            ],
            generated_at=_text(data.get("generated_at"), 40),
        )


@dataclass
class GateInputs:
    """Everything the checks read, gathered once.

    A plain bundle rather than a live view over the run, for two reasons: the
    checks stay pure functions of numbers (so a test can state a situation in
    six lines instead of building a pipeline), and one place is responsible for
    knowing where each number lives.

    Every field is optional. A run that skipped a pass leaves its fields at the
    defaults, and the gates about that pass report ``skipped`` rather than
    inventing a judgement about something that did not happen.
    """

    run_id: str = ""
    style: str = ""
    footage_folder: str = ""

    # -- what ran ---------------------------------------------------------
    ran: dict = field(default_factory=dict)          # stage -> status
    #: Stage-level warnings, already prefixed with their stage.
    stage_warnings: list = field(default_factory=list)

    # -- inputs -----------------------------------------------------------
    footage_files: int = 0
    footage_seconds: float = 0.0
    probe_errors: int = 0

    transcribed: bool = False
    transcript_words: int = 0
    transcript_files: int = 0
    #: Timeline segments carrying speech. Filled when the words came from the
    #: timeline rather than from the transcription stage, which is the case
    #: for a transcript that arrived as an ``.srt`` beside the footage.
    speech_segments: int = 0
    transcript_failed: int = 0
    transcript_mock: bool = False
    #: Mean ASR confidence, when the backend reported one. -1 means unknown.
    transcript_confidence: float = -1.0

    # -- decisions --------------------------------------------------------
    director_enabled: bool = False
    director_ran: bool = False
    director_decisions: int = 0
    director_accepted: int = 0
    director_mock: bool = False

    retention_enabled: bool = False
    retention_ran: bool = False
    retention_applied: bool = False
    cold_open: bool = False
    cold_open_seconds: float = 0.0
    duplicate_seconds: float = 0.0
    unresolved_warnings: int = 0
    hooks_found: int = 0

    base_duration: float = 0.0
    cut_duration: float = 0.0
    source_duration: float = 0.0
    clips: int = 0

    # -- polish -----------------------------------------------------------
    captions_enabled: bool = False
    captions_placed: int = 0
    captions_per_minute: float = 0.0
    caption_ceiling: float = 0.0
    longest_caption: float = 0.0

    audio_enabled: bool = False
    cues_placed: int = 0
    #: Accepted cues that count against the effect ceiling. A bed and a
    #: silence are cues and are not effects, so the two numbers differ and the
    #: density check has to read this one.
    effects_placed: int = 0
    sfx_per_minute: float = 0.0
    sfx_ceiling: float = 0.0
    missing_assets: int = 0
    audio_mode: str = "off"

    # -- output -----------------------------------------------------------
    render_enabled: bool = False
    render_ran: bool = False
    render_mock: bool = False
    render_claimed: bool = False
    render_path: str = ""
    render_exists: bool = False
    render_size_mb: float = 0.0
    render_duration: float = 0.0
    render_planned_duration: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "GateInputs":
        data = data or {}
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in data.items() if k in known}
        clean["stage_warnings"] = as_text_list(
            clean.get("stage_warnings"), limit=200)
        clean["ran"] = dict(clean.get("ran") or {})
        for key in ("transcript_confidence",):
            if key in clean:
                clean[key] = as_float(clean[key], -1.0)
        return cls(**clean)

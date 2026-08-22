"""What an automated run is, as data.

Six sessions produced about forty commands. Each one is individually
inspectable and collectively unusable: nobody remembers that `review plan`
needs `review critique`, which needs `review export-frames`, which needs a
rough cut that has been *executed* rather than merely built. This package is
the thing that remembers.

It is orchestration and nothing else. No stage here makes an editing decision
that the underlying pass would not have made on its own — the value is entirely
in ordering, checkpointing, and telling you the exact next command when
something stops.

Three invariants:

* **Planning and execution are different things.** A run builds plans and
  validates them offline. Every operation that touches Premiere is behind a
  named gate, executed one stage at a time, each needing its own ``--yes``.
  There is deliberately no "do everything" switch.
* **A failure is a record, not an exception.** ``AutoFailure`` carries what
  failed, why, whether the run can resume, and the command to try next. A
  traceback is a bug in this package, not a user-facing outcome.
* **A checkpoint is only reusable if its artifacts still are.** Recording that
  a stage passed is not enough; the files it produced have to still exist,
  still match their fingerprints, and still have been built from the same
  configuration.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from editing.schema import _slug, as_float, as_str_list, short_hash

#: Every stage, in dependency order. The order in this tuple *is* the run
#: order -- there is no separate graph, because a linear pipeline with explicit
#: prerequisites is easier to reason about than a scheduler, and this pipeline
#: is genuinely linear.
STAGE_ORDER = (
    "doctor",
    "discover",
    "transcribe",
    "analyze",
    "recommend",
    "roughcut_build",
    "roughcut_dry_run",
    "review_export_frames",
    "review_critique",
    "review_plan",
    "review_dry_run",
    "layers_build",
    "layers_dry_run",
    "assets_index",
    "assets_plan",
    "assets_dry_run",
    "episode_memory",
    "retention_plan",
    "feedback_start",
    "feedback_queue",
    "feedback_report",
    "report",
)

STATUSES = ("pending", "running", "passed", "failed", "skipped", "blocked")

#: Statuses that mean the stage's outputs exist and can be depended on.
SATISFIED = frozenset({"passed", "skipped"})

#: The four things that can be executed against Premiere, and the stage whose
#: dry run has to have passed first.
GATE_STAGES = {
    "roughcut": "roughcut_dry_run",
    "review": "review_dry_run",
    "layers": "layers_dry_run",
    "assets": "assets_dry_run",
}

#: How dangerous each operation is, worst first. Used to answer "what is the
#: riskiest thing this plan would do?" in one line, which is the question a
#: person actually has before typing ``--yes``.
OPERATION_RISK = (
    ("clip.remove", "deletes a clip"),
    ("clip.insert", "inserts and ripples every later clip"),
    ("clip.trim", "changes clip timing and ripples"),
    ("clip.speed", "retimes a clip and ripples"),
    ("clip.move", "moves a clip"),
    ("gap.remove", "closes a gap and ripples"),
    ("project.save", "saves the project"),
    ("sequence.create", "creates a new sequence"),
    ("clip.overwrite", "places a clip, overwriting whatever is under it"),
    ("clip.append", "appends a clip to a track"),
    ("track.remove", "removes a track"),
    ("property.reset", "clears a parameter's keyframes"),
    ("animate", "writes keyframes on a parameter"),
    ("audio.duck", "writes level keyframes under speech"),
    ("audio.gain", "sets a clip's level"),
    ("audio.fade", "writes fade keyframes"),
    ("text.create", "draws a text overlay"),
    ("graphic.image", "places an image overlay"),
    ("project.import", "imports media into the project"),
    ("track.add", "adds an empty track"),
    ("marker.remove", "removes a marker"),
    ("marker.add", "adds a marker"),
    ("sequence.activate", "makes a sequence active"),
)

_RISK_RANK = {name: index for index, (name, _why) in enumerate(OPERATION_RISK)}
_RISK_WHY = dict(OPERATION_RISK)


def riskiest(ops: Sequence[dict]) -> tuple:
    """The most consequential operation in a plan, as ``(name, why)``.

    Unknown operations sort worst on purpose: something this table has not
    heard of is exactly the thing a person should be told about before
    approving a batch.
    """
    worst_name, worst_rank = "", -1
    for op in ops or ():
        name = str(op.get("op") or "")
        if not name:
            continue
        rank = _RISK_RANK.get(name, -1)
        if worst_rank < 0 or rank < worst_rank or (
            rank == -1 and worst_rank != -1
        ):
            worst_name, worst_rank = name, rank
    if not worst_name:
        return "", ""
    return worst_name, _RISK_WHY.get(worst_name, "an operation this system "
                                                 "does not recognise")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class AutoRunConfig:
    """Everything that decides what a run does.

    Serialised whole into ``config.json`` so a run can be resumed, inspected
    and compared without guessing what it was invoked with. Fields that change
    a stage's *output* also feed that stage's config fingerprint, which is what
    makes "the style changed, so the layers checkpoint is stale" automatic
    rather than something a user has to notice.
    """

    footage_folder: str = ""
    style: str = "minimal_clean"
    name: str = "structure"
    #: Where the asset library lives. Empty means the default under model_dir.
    asset_library: str = ""

    # -- safety switches ---------------------------------------------------
    #: Mock the vision model and the critic. No GPU, no server.
    mock: bool = False
    #: Never talk to Premiere at all, in any stage.
    no_premiere: bool = False
    #: Style and asset passes record every choice instead of drawing/playing it.
    markers_only: bool = False

    # -- analysis controls -------------------------------------------------
    max_windows: Optional[int] = None
    recursive: bool = True
    keep_frames: bool = False
    use_motion: bool = True

    # -- pass controls -----------------------------------------------------
    #: Skip the critic entirely (it is the slowest and least certain pass).
    skip_review: bool = False
    #: Skip the asset pass.
    skip_assets: bool = False
    #: Skip the episode/retention pass. It is on by default because it is pure
    #: Python -- no model, no FFmpeg, no Premiere -- and costs about a second;
    #: the switch exists because a planning layer nobody reads is still noise
    #: in the report.
    skip_episode: bool = False

    #: Produce transcripts with local Whisper before analysing. Off by
    #: default because it loads a speech model and takes minutes per episode --
    #: but it is the difference between a story layer that works and one that
    #: silently has nothing to read, so the run report says when it would have
    #: helped.
    transcribe: bool = False
    #: Whisper size for that stage. Empty means the configured default.
    transcribe_model: str = ""
    #: Which backend produces the transcripts. Empty means the configured
    #: default (faster_whisper). ``mock`` fabricates text and stamps every
    #: artifact as fake -- for exercising the pipeline, never for an edit.
    transcribe_backend: str = ""
    #: ISO language code, or empty to auto-detect.
    transcribe_language: str = ""

    #: Open a feedback session and build its review queue at the end of the
    #: run. Off by default, and the *only* pass in this pipeline that defaults
    #: to off: every other stage produces a file, while this one starts a
    #: review that a person is then expected to finish. Creating one nobody
    #: asked for would leave a trail of abandoned sessions, so the run report
    #: says how to start a review instead of starting one.
    feedback: bool = False

    created_at: str = ""
    schema_version: int = 1

    def fingerprint_for(self, keys: Sequence[str]) -> str:
        """A short hash of the config fields a stage actually depends on."""
        return short_hash(*[repr(getattr(self, key, None)) for key in keys])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AutoRunConfig":
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in (data or {}).items() if k in known}
        clean["style"] = str(clean.get("style") or "minimal_clean")
        return cls(**clean)


def run_id_for(config: AutoRunConfig, *, when: Optional[float] = None) -> str:
    """A run ID that is readable, sortable and effectively unique.

    ``<timestamp>-<folder hash>-<style>``. The timestamp sorts and disambiguates,
    the folder hash groups runs over the same footage, and the style is there
    because it is the field a person most often wants to tell two runs apart by.
    """
    from editing.fingerprint import normalise_path

    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(when or time.time()))
    folder = config.footage_folder or "nofolder"
    try:
        digest = short_hash(normalise_path(folder), length=6)
    except Exception:  # noqa: BLE001 - a bad path must not stop a run starting
        digest = short_hash(folder, length=6)
    return f"{stamp}-{digest}-{_slug(config.style) or 'style'}"


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AutoStage:
    """One step, and everything the orchestrator needs to know about it.

    Kept as data rather than as branches so the whole pipeline is readable in
    one screen and ``auto status`` can print the requirements of a stage that
    has not run yet.
    """

    name: str
    summary: str
    #: Stages that must be satisfied first.
    requires: tuple = ()
    #: Config fields whose change invalidates this stage's checkpoint.
    config_keys: tuple = ()
    #: Artifacts this stage produces, relative to the run's artifact root.
    #: Checkpoint reuse requires every one of them to still be there.
    artifacts: tuple = ()
    requires_premiere: bool = False
    requires_model: bool = False
    requires_ffmpeg: bool = False
    requires_assets: bool = False
    #: False for stages that must always re-run (cheap, or state-dependent).
    resumable: bool = True
    #: True when a failure here should stop the run.
    critical: bool = True
    #: True when only a person can unblock it.
    requires_user_action: bool = False
    #: The command that runs this one stage by hand, for the failure message.
    manual_command: str = ""

    @property
    def safe_to_continue_after_failure(self) -> bool:
        return not self.critical

    def to_dict(self) -> dict:
        data = asdict(self)
        data["requires"] = list(self.requires)
        data["config_keys"] = list(self.config_keys)
        data["artifacts"] = list(self.artifacts)
        data["safe_to_continue_after_failure"] = (
            self.safe_to_continue_after_failure
        )
        return data


@dataclass
class AutoFailure:
    """Why a stage stopped, and what to do about it.

    Every field exists because a bare exception string answers none of them.
    ``next_command`` is the one that matters: the difference between a tool a
    person can recover from and one they abandon is whether the error tells
    them what to type.
    """

    stage: str = ""
    what: str = ""
    why: str = ""
    #: Machine-readable, so a caller can branch without parsing English.
    code: str = "stage_failed"
    can_resume: bool = True
    next_command: str = ""
    #: Where to look for detail.
    log_path: str = ""
    report_path: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["AutoFailure"]:
        if not data:
            return None
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def render(self) -> str:
        lines = [
            f"Stage {self.stage} {self.what}",
            f"  why    : {self.why}",
            f"  resume : {'yes' if self.can_resume else 'no'}",
        ]
        if self.next_command:
            lines.append(f"  next   : {self.next_command}")
        if self.log_path:
            lines.append(f"  log    : {self.log_path}")
        if self.report_path:
            lines.append(f"  report : {self.report_path}")
        return "\n".join(lines)


@dataclass
class AutoCheckpoint:
    """Proof that a stage finished, and that its outputs are still good.

    The fingerprints are the point. "This stage passed once" is not a reason to
    skip it if the file it produced has since been deleted or rebuilt by hand,
    and silently reusing a stale artifact is how a pipeline starts producing
    results that do not correspond to anything.
    """

    stage: str
    completed_at: str = ""
    #: ``{relative path: fingerprint}`` for every artifact.
    artifacts: dict = field(default_factory=dict)
    #: Hash of the config fields this stage depends on.
    config_fingerprint: str = ""
    #: Free-form facts a later stage or the report may want.
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AutoCheckpoint":
        return cls(
            stage=str(data.get("stage") or ""),
            completed_at=str(data.get("completed_at") or ""),
            artifacts=dict(data.get("artifacts") or {}),
            config_fingerprint=str(data.get("config_fingerprint") or ""),
            summary=dict(data.get("summary") or {}),
        )


@dataclass
class AutoStageResult:
    """What happened to one stage in one run."""

    stage: str
    status: str = "pending"
    started_at: str = ""
    ended_at: str = ""
    elapsed: float = 0.0
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failure: Optional[AutoFailure] = None
    #: True when this stage was satisfied from a checkpoint rather than run.
    from_checkpoint: bool = False
    #: One line for the status table.
    note: str = ""
    #: Facts worth carrying into the report (counts, densities, names).
    summary: dict = field(default_factory=dict)
    next_command: str = ""

    @property
    def ok(self) -> bool:
        return self.status in SATISFIED

    def to_dict(self) -> dict:
        data = asdict(self)
        data["failure"] = self.failure.to_dict() if self.failure else None
        data["elapsed"] = round(self.elapsed, 3)
        data["ok"] = self.ok
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AutoStageResult":
        return cls(
            stage=str(data.get("stage") or ""),
            status=str(data.get("status") or "pending"),
            started_at=str(data.get("started_at") or ""),
            ended_at=str(data.get("ended_at") or ""),
            elapsed=as_float(data.get("elapsed")),
            inputs=as_str_list(data.get("inputs"), limit=200),
            outputs=as_str_list(data.get("outputs"), limit=200),
            warnings=as_str_list(data.get("warnings"), limit=200),
            errors=as_str_list(data.get("errors"), limit=200),
            failure=AutoFailure.from_dict(data.get("failure")),
            from_checkpoint=bool(data.get("from_checkpoint")),
            note=str(data.get("note") or "")[:400],
            summary=dict(data.get("summary") or {}),
            next_command=str(data.get("next_command") or ""),
        )


# ---------------------------------------------------------------------------
# Execution gates
# ---------------------------------------------------------------------------

@dataclass
class AutoExecutionGate:
    """One thing that could be executed against Premiere, and whether it may be.

    A gate is computed, never stored as a decision: it is read fresh from the
    plan and the run state every time it is asked for, because the answer
    depends on files that can change between the question and the answer.
    """

    stage: str
    label: str = ""
    #: The dry-run stage whose pass is required.
    dry_run_stage: str = ""
    plan_path: str = ""
    plan_exists: bool = False
    dry_run_passed: bool = False
    sequence_name: str = ""
    operation_count: int = 0
    riskiest_operation: str = ""
    riskiest_why: str = ""
    on_scratch: bool = False
    scratch_reason: str = ""
    #: True only when every precondition this system can check offline is met.
    ready: bool = False
    blocked_reason: str = ""
    command: str = ""
    #: Set once this gate has actually been executed in this run.
    executed: bool = False
    executed_at: str = ""
    operations_succeeded: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AutoExecutionGate":
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in (data or {}).items() if k in known}
        clean["stage"] = str(clean.get("stage") or "")
        return cls(**clean)

    def render(self) -> str:
        mark = "+" if self.ready else ("=" if self.executed else "-")
        lines = [
            f"{mark} {self.stage:<10} {self.label}",
            f"    plan       : {self.plan_path or '(none)'}"
            + ("" if self.plan_exists else "   MISSING"),
            f"    dry run    : {'passed' if self.dry_run_passed else 'NOT PASSED'}",
            f"    sequence   : {self.sequence_name or '(unknown)'}",
            f"    operations : {self.operation_count}",
        ]
        if self.riskiest_operation:
            lines.append(
                f"    riskiest   : {self.riskiest_operation} "
                f"-- {self.riskiest_why}"
            )
        lines.append(
            f"    on scratch : {self.on_scratch}"
            + (f"   ({self.scratch_reason})" if self.scratch_reason else "")
        )
        if self.executed:
            lines.append(
                f"    EXECUTED   : {self.executed_at} "
                f"({self.operations_succeeded} operation(s) succeeded)"
            )
        elif self.ready:
            lines.append(f"    run        : {self.command}")
        else:
            lines.append(f"    BLOCKED    : {self.blocked_reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

@dataclass
class AutoRunState:
    """The durable record of one run.

    Written after every stage, so a process killed halfway leaves a resumable
    run rather than a mystery.
    """

    run_id: str = ""
    config: AutoRunConfig = field(default_factory=AutoRunConfig)
    stages: list[AutoStageResult] = field(default_factory=list)
    gates: list[AutoExecutionGate] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    #: "running" / "complete" / "failed" / "blocked"
    status: str = "running"
    warnings: list[str] = field(default_factory=list)
    #: Where the run lives. Absolute, so a moved run reports honestly.
    run_dir: str = ""
    artifacts_dir: str = ""
    schema_version: int = 1

    def stage(self, name: str) -> Optional[AutoStageResult]:
        for result in self.stages:
            if result.stage == name:
                return result
        return None

    def satisfied(self, name: str) -> bool:
        result = self.stage(name)
        return bool(result and result.ok)

    def gate(self, name: str) -> Optional[AutoExecutionGate]:
        for gate in self.gates:
            if gate.stage == name:
                return gate
        return None

    def first_failure(self) -> Optional[AutoStageResult]:
        for result in self.stages:
            if result.status == "failed":
                return result
        return None

    def of_status(self, *statuses: str) -> list[AutoStageResult]:
        wanted = set(statuses)
        return [r for r in self.stages if r.status in wanted]

    def stats(self) -> dict:
        by_status: dict = {}
        for result in self.stages:
            by_status[result.status] = by_status.get(result.status, 0) + 1
        return {
            "stages": len(self.stages),
            "passed": len(self.of_status("passed")),
            "skipped": len(self.of_status("skipped")),
            "failed": len(self.of_status("failed")),
            "blocked": len(self.of_status("blocked")),
            "from_checkpoint": sum(
                1 for r in self.stages if r.from_checkpoint
            ),
            "by_status": by_status,
            "gates_ready": sum(1 for g in self.gates if g.ready),
            "gates_executed": sum(1 for g in self.gates if g.executed),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_dir": self.run_dir,
            "artifacts_dir": self.artifacts_dir,
            "config": self.config.to_dict(),
            "stats": self.stats(),
            "warnings": list(self.warnings),
            "stages": [result.to_dict() for result in self.stages],
            "gates": [gate.to_dict() for gate in self.gates],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AutoRunState":
        return cls(
            run_id=str(data.get("run_id") or ""),
            config=AutoRunConfig.from_dict(data.get("config") or {}),
            stages=[
                AutoStageResult.from_dict(entry)
                for entry in (data.get("stages") or [])
            ],
            gates=[
                AutoExecutionGate.from_dict(entry)
                for entry in (data.get("gates") or [])
            ],
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            status=str(data.get("status") or "running"),
            warnings=as_str_list(data.get("warnings"), limit=200),
            run_dir=str(data.get("run_dir") or ""),
            artifacts_dir=str(data.get("artifacts_dir") or ""),
            schema_version=int(as_float(data.get("schema_version"), 1)),
        )


@dataclass
class AutoRunReport:
    """The readable summary of a run, in one object.

    Built from the state plus whatever the artifacts say, so it can be
    regenerated at any time without re-running anything.
    """

    run_id: str = ""
    status: str = ""
    config: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    stages: list[dict] = field(default_factory=list)
    gates: list[dict] = field(default_factory=list)
    #: Per-pass summaries lifted out of the artifacts.
    roughcut: dict = field(default_factory=dict)
    critic: dict = field(default_factory=dict)
    layers: dict = field(default_factory=dict)
    assets: dict = field(default_factory=dict)
    #: How much of this run is worth a human review, and how to start one.
    #: Always filled, whether or not the feedback stages ran.
    feedback: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    check_in_premiere: list[str] = field(default_factory=list)
    next_commands: list[str] = field(default_factory=list)
    generated_at: str = ""
    run_dir: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

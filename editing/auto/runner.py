"""The orchestrator.

    AutoRunConfig -> stage sequence -> checkpointed results -> AutoRunReport

Runs the twenty-three stages in order, reuses what it can prove is still valid, and
stops on the first failure that matters with the exact command to try next.
It makes no editing decisions of its own.

Four rules do most of the work:

**A checkpoint is a claim, verified before it is trusted.** "This stage passed"
is not enough: the artifacts it named have to still exist, still match their
fingerprints, and still have been built from the same configuration. Anything
else and the stage runs again. Changing ``--style`` therefore invalidates the
layer and asset stages automatically, and leaves the expensive analysis alone.

**A missing tool is a blocked stage, not a crashed run.** The review pass needs
FFmpeg and a model server; neither is guaranteed. When one is absent the review
stages go ``blocked`` with a reason, the run continues to the style and asset
passes, and the report says what was lost.

**Nothing here executes anything.** Every stage builds a plan or validates one
offline. The four things that could touch Premiere are computed as *gates* and
executed one at a time, elsewhere, each behind its own ``--yes``.

**Every run is hermetic.** The pipeline's ``output_dir`` is the run's own
``artifacts/`` folder, so two runs cannot overwrite each other. The one shared
thing is the analysis cache, because paying hundreds of model calls twice is
the worst thing this package could do to an afternoon.
"""
from __future__ import annotations

import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional, Sequence

from editing.auto import gates as gates_module
from editing.auto import stages as stages_module
from editing.auto import store
from editing.auto.schema import (
    STAGE_ORDER, AutoCheckpoint, AutoFailure, AutoRunConfig, AutoRunState,
    AutoStageResult, run_id_for,
)
from editing.auto.stages import RUNNERS, StageBlocked
from editing.cache import Cache
from editing.config import AudioConfig, EditingConfig, SamplingConfig
from editing.errors import EditingError
from editing.pipeline import Pipeline, build_pipeline

Reporter = Callable[[str], None]


def _quiet(_message: str) -> None:
    return None


def build_run_pipeline(
    config: EditingConfig,
    run_id: str,
    run: AutoRunConfig,
    *,
    sampling: Optional[SamplingConfig] = None,
    audio: Optional[AudioConfig] = None,
    say: Reporter = _quiet,
    bridge=None,
) -> Pipeline:
    """A pipeline whose outputs land in this run, sharing the global cache.

    The split matters: artifacts are per-run so nothing collides, and the cache
    is shared so a second run over the same footage is nearly free.
    """
    scoped = store.run_config(config, run_id)
    if run.mock:
        scoped = replace(scoped, vision_backend="mock")
    if run.no_premiere:
        scoped = replace(scoped, use_premiere=False)

    pipeline = build_pipeline(
        scoped,
        (sampling or SamplingConfig.from_env()).validated(),
        (audio or AudioConfig.from_env()).validated(),
        say=say,
        bridge=bridge,
    )
    # Point the cache back at the shared root rather than this run's folder.
    pipeline.cache = Cache(root=config.cache_dir)
    return pipeline


class AutoRunner:
    """Runs, resumes and reports on one automated pass.

    ``pipeline`` is injectable so tests can drive the whole path with stubbed
    frame extraction, a mock model and no Premiere -- the same pattern the
    executors use for ``engine``.
    """

    def __init__(
        self,
        config: EditingConfig,
        *,
        sampling: Optional[SamplingConfig] = None,
        audio: Optional[AudioConfig] = None,
        say: Reporter = _quiet,
        bridge=None,
        pipeline: Optional[Pipeline] = None,
    ):
        self.config = config
        self.sampling = sampling
        self.audio = audio
        self.say = say
        self.bridge = bridge
        self._pipeline = pipeline

    # -- lifecycle -------------------------------------------------------

    def start(
        self, run: AutoRunConfig, *, force_new_run: bool = False
    ) -> AutoRunState:
        """Create a run folder and its state."""
        run_id = run_id_for(run)
        if force_new_run:
            # The timestamp already differs per second; nudge until unique so
            # two --force-new-run invocations in the same second cannot collide.
            while store.run_dir(self.config, run_id).exists():
                time.sleep(1.0)
                run_id = run_id_for(run)
        return store.create(self.config, run, run_id, force=force_new_run)

    def load(self, run_id: str) -> AutoRunState:
        return store.load(self.config, run_id)

    def latest_run_id(self) -> Optional[str]:
        runs = store.list_runs(self.config, limit=1)
        return runs[0]["run_id"] if runs else None

    def resolve(self, run_id: Optional[str]) -> AutoRunState:
        """A run by ID, or the most recent one."""
        if run_id:
            return self.load(run_id)
        latest = self.latest_run_id()
        if not latest:
            raise EditingError(
                "No automated runs exist yet",
                hint="Start one with `auto run --folder <folder> "
                     "--style <preset>`.",
            )
        return self.load(latest)

    def pipeline_for(self, state: AutoRunState) -> Pipeline:
        if self._pipeline is not None:
            return self._pipeline
        return build_run_pipeline(
            self.config, state.run_id, state.config,
            sampling=self.sampling, audio=self.audio,
            say=self._logged_say(state), bridge=self.bridge,
        )

    def _logged_say(self, state: AutoRunState) -> Reporter:
        def report(message: str) -> None:
            self.say(message)
            store.append_log(self.config, state.run_id, message)
        return report

    # -- the run ---------------------------------------------------------

    def run(
        self,
        state: AutoRunState,
        *,
        refresh: Sequence[str] = (),
        only: Optional[Sequence[str]] = None,
    ) -> AutoRunState:
        """Execute every stage that is not already satisfied.

        ``refresh`` names stages to re-run even if checkpointed; their
        dependents are refreshed with them, because a stage rebuilt from new
        inputs makes everything downstream of it stale by definition.
        """
        pipeline = self.pipeline_for(state)
        context: dict = {}
        self._seed_context(context, state)

        forced = set(refresh)
        for name in list(forced):
            forced.update(stages_module.dependents(name))
        for name in forced:
            store.clear_checkpoint(self.config, state.run_id, name)

        wanted = set(only) if only else None
        state.status = "running"
        stopped = False

        for name in STAGE_ORDER:
            stage = stages_module.stage(name)
            result = self._result_for(state, name)

            if stopped:
                self._mark(result, "blocked",
                           note="an earlier stage failed, so this did not run")
                continue
            if wanted is not None and name not in wanted:
                self._mark(result, "skipped", note="not requested by --only")
                continue

            skip_reason = self._skip_reason(state.config, name)
            if skip_reason:
                self._mark(result, "skipped", note=skip_reason)
                store.save(self.config, state)
                continue

            missing = self._unsatisfied(state, stage)
            if missing:
                self._mark(
                    result, "blocked",
                    note=f"needs {', '.join(missing)}, which did not pass",
                )
                store.save(self.config, state)
                continue

            if name == "report":
                self._run_report(state, pipeline, result)
                store.save(self.config, state)
                continue

            checkpoint = self._valid_checkpoint(state, stage)
            if checkpoint is not None:
                result.warnings = []
                result.errors = []
                result.failure = None
                self._mark(result, "passed", note="reused a valid checkpoint")
                result.from_checkpoint = True
                result.outputs = sorted(checkpoint.artifacts)
                result.summary = dict(checkpoint.summary)
                store.save(self.config, state)
                continue

            self._execute_stage(state, pipeline, stage, result, context)
            store.save(self.config, state)

            if result.status == "failed" and stage.critical:
                stopped = True

        state.gates = gates_module.compute_gates(self.config, state)
        state.status = self._final_status(state)
        store.save(self.config, state)
        return state

    def resume(self, state: AutoRunState, **kwargs) -> AutoRunState:
        """Continue a run. Failed and blocked stages are retried.

        A blocked stage is retried on purpose: the usual reason a stage blocks
        is a missing tool or an unreachable server, and the usual reason
        somebody types ``resume`` is that they have just fixed it.
        """
        for result in state.stages:
            if result.status in ("failed", "blocked", "running"):
                result.status = "pending"
                result.failure = None
                result.errors = []
        return self.run(state, **kwargs)

    # -- one stage -------------------------------------------------------

    def _execute_stage(
        self, state: AutoRunState, pipeline: Pipeline, stage, result, context
    ) -> None:
        runner = RUNNERS.get(stage.name)
        if runner is None:  # pragma: no cover - RUNNERS and STAGES agree
            self._mark(result, "skipped", note="no runner is defined")
            return

        # Clear anything left over from a previous attempt. Without this a
        # stage that was blocked and then passed keeps the old blocked note,
        # so the status table says "needs X, which did not pass" next to a
        # green tick.
        result.note = ""
        result.warnings = []
        result.errors = []
        result.outputs = []
        result.summary = {}
        result.failure = None
        result.from_checkpoint = False
        result.ended_at = ""

        result.status = "running"
        result.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        started = time.time()
        store.append_log(self.config, state.run_id, f"stage {stage.name}: start")
        self.say(f"[{stage.name}] {stage.summary}...")

        try:
            outputs, summary, warnings = runner(pipeline, state.config, context)
        except StageBlocked as exc:
            self._fail(
                state, stage, result, started,
                what=exc.what, why=exc.why, code=exc.code,
                next_command=exc.next_command or stage.manual_command,
                detail=exc.detail,
                status="failed" if stage.critical else "blocked",
            )
            return
        except EditingError as exc:
            self._fail(
                state, stage, result, started,
                what="failed", why=exc.message, code=exc.code,
                next_command=exc.hint or stage.manual_command,
                detail={"hint": exc.hint, "detail": exc.detail},
                status="failed" if stage.critical else "blocked",
            )
            return
        except Exception as exc:  # noqa: BLE001 - a bug is still a run outcome
            store.append_log(
                self.config, state.run_id,
                f"stage {stage.name}: unexpected error\n{traceback.format_exc()}",
            )
            self._fail(
                state, stage, result, started,
                what="hit an unexpected error",
                why=f"{type(exc).__name__}: {exc}",
                code="internal_error",
                next_command=stage.manual_command,
                detail={"traceback": traceback.format_exc()[-2000:]},
                status="failed",
            )
            return

        result.outputs = [str(path) for path in outputs]
        result.summary = dict(summary or {})
        result.warnings = [str(w) for w in (warnings or [])][:40]
        result.next_command = stage.manual_command
        self._mark(result, "passed")
        result.elapsed = time.time() - started
        result.ended_at = time.strftime("%Y-%m-%dT%H:%M:%S")

        self._write_checkpoint(state, stage, result)
        store.append_log(
            self.config, state.run_id,
            f"stage {stage.name}: passed in {result.elapsed:.1f}s",
        )

    def _fail(
        self, state, stage, result, started, *, what, why, code,
        next_command, detail, status,
    ) -> None:
        json_report, text_report = store.report_paths(self.config, state.run_id)
        result.failure = AutoFailure(
            stage=stage.name,
            what=what,
            why=why,
            code=code,
            can_resume=True,
            next_command=next_command,
            log_path=str(store.log_path(self.config, state.run_id)),
            report_path=str(text_report),
            detail=detail or {},
        )
        result.errors = [why]
        # The one-line status table reads ``note``; without this a blocked
        # stage shows an empty reason and the user has to go digging.
        self._mark(result, status, note=why[:200])
        result.elapsed = time.time() - started
        result.ended_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        result.next_command = next_command
        store.append_log(
            self.config, state.run_id, f"stage {stage.name}: {status} -- {why}"
        )
        self.say(f"[{stage.name}] {status}: {why}")

    def _run_report(self, state: AutoRunState, pipeline, result) -> None:
        from editing.auto import report as report_module

        started = time.time()
        result.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        state.gates = gates_module.compute_gates(self.config, state)
        try:
            paths = report_module.write_reports(self.config, state, pipeline)
        except Exception as exc:  # noqa: BLE001 - a report is never fatal
            self._mark(result, "blocked",
                       note=f"the report could not be written: {exc}")
            return
        result.outputs = [str(path) for path in paths]
        self._mark(result, "passed")
        result.elapsed = time.time() - started
        result.ended_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    # -- checkpoints -----------------------------------------------------

    def _valid_checkpoint(
        self, state: AutoRunState, stage
    ) -> Optional[AutoCheckpoint]:
        """A checkpoint only if everything it claims is still true."""
        if not stage.resumable:
            return None
        checkpoint = store.read_checkpoint(
            self.config, state.run_id, stage.name
        )
        if checkpoint is None:
            return None

        expected = state.config.fingerprint_for(stage.config_keys)
        if checkpoint.config_fingerprint != expected:
            store.append_log(
                self.config, state.run_id,
                f"stage {stage.name}: checkpoint is stale (configuration "
                "changed); re-running",
            )
            return None

        if not checkpoint.artifacts:
            return None
        for path, fingerprint in checkpoint.artifacts.items():
            target = Path(path)
            if not target.exists():
                store.append_log(
                    self.config, state.run_id,
                    f"stage {stage.name}: checkpoint names a missing artifact "
                    f"({path}); re-running",
                )
                return None
            if store.fingerprint_file(target) != fingerprint:
                store.append_log(
                    self.config, state.run_id,
                    f"stage {stage.name}: {path} changed since the checkpoint; "
                    "re-running",
                )
                return None
        return checkpoint

    def _write_checkpoint(self, state: AutoRunState, stage, result) -> None:
        if not stage.resumable:
            return
        artifacts: dict = {}
        for path in result.outputs:
            target = Path(path)
            if target.exists():
                artifacts[str(target)] = store.fingerprint_file(target)

        # A stage that declares artifacts but produced none has not really
        # finished, whatever it returned. Not writing a checkpoint means the
        # next run does it again, which is the safe direction.
        if stage.artifacts and not artifacts:
            return

        store.write_checkpoint(self.config, state.run_id, AutoCheckpoint(
            stage=stage.name,
            completed_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            artifacts=artifacts,
            config_fingerprint=state.config.fingerprint_for(stage.config_keys),
            summary=dict(result.summary),
        ))

    # -- helpers ---------------------------------------------------------

    def _seed_context(self, context: dict, state: AutoRunState) -> None:
        """Anything a stage can usefully borrow from outside the run."""
        # A stage runner is handed the run *config*, which deliberately does
        # not know its own ID -- two runs over the same footage and style share
        # a config and differ only by timestamp. The feedback stages need the
        # ID to tie a review to the run it is about, so it is seeded here
        # rather than widening every runner's signature.
        context["run_id"] = state.run_id
        try:
            shared = build_pipeline(
                self.config, (self.sampling or SamplingConfig()).validated(),
            ).load_asset_library(root=state.config.asset_library or None)
        except Exception:  # noqa: BLE001 - a missing shared index is normal
            return
        # Seeds the asset index so a per-run scan does not re-probe every file.
        context["shared_asset_library"] = shared

    def _result_for(self, state: AutoRunState, name: str) -> AutoStageResult:
        existing = state.stage(name)
        if existing is not None:
            return existing
        result = AutoStageResult(stage=name)
        state.stages.append(result)
        return result

    @staticmethod
    def _mark(result: AutoStageResult, status: str, *, note: str = "") -> None:
        result.status = status
        if note:
            result.note = note
        if not result.ended_at:
            result.ended_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    @staticmethod
    def _skip_reason(run: AutoRunConfig, name: str) -> str:
        if run.skip_review and name in stages_module.REVIEW_STAGES:
            return "--skip-review was set"
        if run.skip_assets and name in stages_module.ASSET_STAGES:
            return "--skip-assets was set"
        if run.skip_episode and name in stages_module.EPISODE_STAGES:
            return "--skip-episode was set"
        # Inverted, like feedback below: transcription is opt-in because it
        # loads a speech model and takes minutes per episode.
        if not run.transcribe and name in stages_module.TRANSCRIBE_STAGES:
            return "--transcribe was not set"
        # Inverted too: rendering is opt-in because it is the only stage
        # that costs minutes of CPU and hundreds of megabytes of disk.
        if not run.render_proxy and name in stages_module.RENDER_STAGES:
            return "--render-proxy was not set"
        # Inverted as well: feedback is opt-in
        # because it starts a review a person has to finish.
        if not run.feedback and name in stages_module.FEEDBACK_STAGES:
            return "--feedback was not set"
        return ""

    @staticmethod
    def _unsatisfied(state: AutoRunState, stage) -> list[str]:
        return [
            name for name in stage.requires if not state.satisfied(name)
        ]

    @staticmethod
    def _final_status(state: AutoRunState) -> str:
        if state.of_status("failed"):
            return "failed"
        if state.of_status("blocked"):
            return "blocked"
        return "complete"

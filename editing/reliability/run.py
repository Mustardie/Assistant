"""Gathering what the checks read, and running them.

One module knows where every number lives. The checks themselves are pure
functions of :class:`GateInputs`, so adding a gate never means teaching another
file about stage summaries -- and a test can state a situation in six
assignments instead of building a pipeline.

**Nothing here raises.** A gate report that failed to build because one
optional artifact was unreadable would be strictly worse than a report with
that gate skipped, so every read is defensive and an unreadable input becomes
``skipped`` with the reason.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from editing.config import EditingConfig
from editing.reliability.checks import CHECKS
from editing.reliability.schema import (
    GATE_NAMES, GateInputs, GateReport, now, skipped,
)

logger = logging.getLogger("nova.editing.reliability.run")


def evaluate(inputs: GateInputs) -> GateReport:
    """Run every check over one bundle of inputs. Never raises.

    A check that throws is reported as a skipped gate naming the exception,
    because one broken check must not cost the other fourteen.
    """
    report = GateReport(run_id=inputs.run_id, generated_at=now())
    for name in GATE_NAMES:
        check = CHECKS.get(name)
        if check is None:  # pragma: no cover - GATE_NAMES and CHECKS agree
            continue
        try:
            report.gates.append(check(inputs))
        except Exception as exc:  # noqa: BLE001 - a check is never fatal
            logger.debug("Gate %s raised: %s", name, exc)
            report.gates.append(skipped(
                name, f"this check could not run: {type(exc).__name__}"))
    return report


def collect(
    config: EditingConfig,
    state,
    *,
    caption_plan=None,
    audio_plan=None,
) -> GateInputs:
    """Everything the checks need, read off a run state and its artifacts.

    ``state`` is an ``AutoRunState``. Stage summaries carry almost all of it,
    which is deliberate: they are written as each stage finishes, so a report
    built from them describes the run that happened rather than re-deriving it
    from files that may since have changed.
    """
    run = state.config
    inputs = GateInputs(
        run_id=state.run_id,
        style=run.style,
        footage_folder=run.footage_folder,
        ran={result.stage: result.status for result in state.stages},
        stage_warnings=[
            f"[{result.stage}] {warning}"
            for result in state.stages for warning in result.warnings
        ][:200],
    )

    _from_discover(inputs, _summary(state, "discover"))
    _from_transcribe(inputs, state)
    _from_director(inputs, state, run)
    _from_roughcut(inputs, state)
    _from_retention(inputs, state, run)
    _from_render(inputs, state, run)
    _from_polish(inputs, state, run, caption_plan, audio_plan)
    _from_conform(inputs, config, state, run)
    return inputs


def _from_conform(inputs, config, state, run) -> None:
    """What the conform pass built, executed and delivered.

    Read partly from the stage summary and partly from the files on disk: the
    execution and the delivery happen *after* the run, behind their own gates,
    so a summary written when the stage finished cannot know about them.
    """
    import json

    summary = _summary(state, "conform_build")
    inputs.conform_enabled = getattr(run, "conform", "off") != "off"
    inputs.conform_ran = _ok(state, "conform_build")
    inputs.conform_operations = int(summary.get("operations") or 0)
    inputs.conform_unconverted = int(summary.get("unconverted") or 0)
    inputs.conform_contributions = dict(summary.get("contributions") or {})

    gate_record = state.gate("conform")
    if gate_record is not None:
        inputs.conform_executed = bool(gate_record.executed)
        inputs.conform_applied = int(gate_record.operations_succeeded or 0)

    # The run's own artifacts folder, not the shared output root. Every
    # artifact a run produces is hermetic to that run, and reading the shared
    # root here would report on whichever run happened to write last.
    from editing.auto import store as auto_store

    artifacts = Path(state.artifacts_dir or auto_store.artifacts_dir(
        config, state.run_id))
    delivery = _read_json(artifacts / "conform" / f"{run.name}.delivery.json")
    if not delivery:
        return
    inputs.delivery_path = str(delivery.get("output_path") or "")
    inputs.delivery_error = str((delivery.get("error") or {}).get("error", ""))
    inputs.delivery_duration = float(delivery.get("duration") or 0.0)
    inputs.delivery_size_mb = float(delivery.get("size_bytes") or 0.0) / (
        1024 * 1024)
    # Confirmed against the file system, not trusted from the record: the
    # whole point of this check is that a run can claim a video it no longer
    # has.
    exists = False
    if inputs.delivery_path:
        try:
            target = Path(inputs.delivery_path)
            exists = target.is_file() and target.stat().st_size > 0
        except OSError:
            exists = False
    inputs.delivered = exists


def _read_json(path) -> dict:
    try:
        import json

        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a missing record is not an error
        return {}


def _summary(state, stage: str) -> dict:
    result = state.stage(stage)
    return dict(result.summary) if result is not None and result.summary else {}


def _ok(state, stage: str) -> bool:
    result = state.stage(stage)
    return bool(result is not None and result.ok)


def _from_discover(inputs: GateInputs, summary: dict) -> None:
    inputs.footage_files = int(summary.get("files") or 0)
    inputs.footage_seconds = float(summary.get("total_seconds") or 0.0)


def _from_transcribe(inputs: GateInputs, state) -> None:
    summary = _summary(state, "transcribe")
    inputs.transcribed = bool(state.config.transcribe)

    # Words as the timeline actually saw them, first. A transcript can arrive
    # three ways -- Whisper, Premiere, or an .srt beside the footage -- and
    # reading only the Whisper stage reported "this run has no transcript"
    # over an episode whose every line had been read from a sidecar file.
    analyzed = _summary(state, "analyze")
    if analyzed.get("transcript_words"):
        inputs.transcript_words = int(analyzed["transcript_words"] or 0)
        inputs.speech_segments = int(analyzed.get("segments_with_speech") or 0)
        inputs.transcribed = True

    if not summary:
        return
    inputs.transcript_words = max(
        inputs.transcript_words, int(summary.get("words") or 0))
    inputs.transcript_files = int(summary.get("files") or 0)
    inputs.transcript_failed = int(summary.get("failed") or 0)
    inputs.transcript_mock = bool(summary.get("mock"))
    if summary.get("confidence") is not None:
        try:
            inputs.transcript_confidence = float(summary["confidence"])
        except (TypeError, ValueError):
            inputs.transcript_confidence = -1.0


def _from_director(inputs: GateInputs, state, run) -> None:
    inputs.director_enabled = bool(run.director)
    summary = _summary(state, "director_plan")
    if not summary:
        return
    inputs.director_ran = _ok(state, "director_plan")
    inputs.director_decisions = int(summary.get("decisions") or 0)
    inputs.director_accepted = int(summary.get("accepted") or 0)
    inputs.director_mock = bool(summary.get("mock"))


def _from_roughcut(inputs: GateInputs, state) -> None:
    summary = _summary(state, "roughcut_build")
    inputs.clips = int(summary.get("clips") or 0)
    inputs.cut_duration = float(summary.get("cut_duration") or 0.0)
    inputs.source_duration = float(summary.get("source_duration") or 0.0)


def _from_retention(inputs: GateInputs, state, run) -> None:
    inputs.retention_enabled = bool(run.retention_cut)
    plan = _summary(state, "retention_plan")
    inputs.hooks_found = int(plan.get("hooks") or 0)

    summary = _summary(state, "retention_cut")
    if not summary:
        return
    inputs.retention_ran = _ok(state, "retention_cut")
    inputs.retention_applied = bool(summary.get("applied"))
    inputs.cold_open = bool(summary.get("cold_open"))
    inputs.cold_open_seconds = float(summary.get("cold_open_seconds") or 0.0)
    inputs.unresolved_warnings = int(summary.get("unresolved") or 0)
    inputs.duplicate_seconds = float(summary.get("duplicate_seconds") or 0.0)
    inputs.base_duration = float(summary.get("base_duration") or 0.0)
    if summary.get("cut_duration"):
        # The retention cut is the cut this run actually produced, so it is
        # what every later gate should measure. Leaving the rough cut's figure
        # here would report the length of a cut nobody rendered.
        inputs.cut_duration = float(summary["cut_duration"])


def _from_render(inputs: GateInputs, state, run) -> None:
    inputs.render_enabled = bool(run.render_proxy)
    inputs.render_planned_duration = inputs.cut_duration
    summary = _summary(state, "render_proxy")
    if not summary:
        return
    inputs.render_ran = _ok(state, "render_proxy")
    inputs.render_mock = bool(summary.get("mock"))
    inputs.render_claimed = bool(summary.get("rendered"))
    inputs.render_path = str(summary.get("video") or "")
    inputs.render_size_mb = float(summary.get("size_mb") or 0.0)
    inputs.render_duration = float(summary.get("duration") or 0.0)
    if inputs.render_path:
        try:
            target = Path(inputs.render_path)
            inputs.render_exists = target.exists()
            if inputs.render_exists and not inputs.render_size_mb:
                inputs.render_size_mb = target.stat().st_size / (1024 * 1024)
        except OSError:
            inputs.render_exists = False


def _from_polish(
    inputs: GateInputs, state, run, caption_plan, audio_plan
) -> None:
    inputs.captions_enabled = str(getattr(run, "captions", "off")) != "off"
    inputs.audio_enabled = str(getattr(run, "audio_polish", "off")) != "off"
    inputs.audio_mode = str(getattr(run, "audio_polish", "off"))

    captions = _summary(state, "caption_polish")
    inputs.captions_placed = int(captions.get("accepted") or 0)
    inputs.captions_per_minute = float(
        captions.get("captions_per_minute") or 0.0)
    inputs.caption_ceiling = float(captions.get("ceiling") or 0.0)
    inputs.longest_caption = float(captions.get("longest_seconds") or 0.0)

    audio = _summary(state, "audio_polish")
    inputs.cues_placed = int(audio.get("accepted") or 0)
    inputs.effects_placed = int(audio.get("effects") or 0)
    inputs.sfx_per_minute = float(audio.get("sfx_per_minute") or 0.0)
    inputs.sfx_ceiling = float(audio.get("ceiling") or 0.0)
    inputs.missing_assets = int(audio.get("missing_assets") or 0)

    # The plans themselves win when they were handed in: a caller that has
    # just built one is holding better numbers than a summary written earlier.
    if caption_plan is not None:
        stats = caption_plan.stats()
        inputs.captions_placed = stats["accepted"]
        inputs.captions_per_minute = stats["captions_per_minute"]
        inputs.longest_caption = stats["longest_seconds"]
        inputs.caption_ceiling = caption_plan.config.max_per_minute
        inputs.captions_enabled = caption_plan.config.enabled
    if audio_plan is not None:
        stats = audio_plan.stats()
        inputs.cues_placed = stats["accepted"]
        inputs.effects_placed = stats["effects"]
        inputs.sfx_per_minute = stats["sfx_per_minute"]
        inputs.missing_assets = stats["missing_assets"]
        inputs.sfx_ceiling = audio_plan.config.max_sfx_per_minute
        inputs.audio_enabled = audio_plan.config.enabled
        inputs.audio_mode = audio_plan.config.mode


def check_run(
    config: EditingConfig,
    state,
    *,
    caption_plan=None,
    audio_plan=None,
) -> tuple:
    """Collect and evaluate in one call. Returns ``(report, inputs)``."""
    inputs = collect(
        config, state, caption_plan=caption_plan, audio_plan=audio_plan)
    return evaluate(inputs), inputs


def report_path(config: EditingConfig, run_id: str) -> Optional[Path]:
    """Where a run's gate report is written, when it has a run folder."""
    from editing.auto import store as auto_store

    if not run_id:
        return None
    return auto_store.run_dir(config, run_id) / "reports" / "checks.json"

"""The run report: what happened, what did not, and what to type next.

Written to both ``report.json`` and ``report.txt`` in the run's ``reports/``
folder, and regenerable at any time from the state plus the artifacts.

The report is organised around the questions people actually ask after an
automated run, in the order they ask them:

1. did it work?
2. what did it produce?
3. **what did it deliberately not do, and why?**
4. what do I type next?

Point three gets the most space, because this pipeline is refusal-heavy by
design and a report that only lists successes would misrepresent it. A run
where the critic was mocked, the asset library was empty and nothing was
executed is a *normal* run — and the report has to say all three of those
plainly rather than presenting a wall of green.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional
from editing.auto import store
from editing.auto.schema import STAGE_ORDER, AutoRunReport, AutoRunState
from editing.config import EditingConfig

_RULE = "=" * 78
_THIN = "-" * 78

#: Things that are true of this system no matter how well a run went. Stated
#: on every report because a person reading one may not have read the README,
#: and every one of these has surprised somebody.
LIMITATIONS = (
    "Nothing here has been verified against a real Premiere host. Every plan "
    "is validated offline for operation shape, not for runtime behaviour.",
    "The rough cut assembles onto V1 only. Overlays, B-roll and "
    "picture-in-picture need a track model this does not have.",
    "Caption selection is a keyword heuristic, English-only, tuned for "
    "Minecraft commentary. It will miss sarcasm and running jokes.",
    "The critic judges single stills and has no memory across the episode.",
    "Asset matching reads tags and folders. Nothing analyses audio content, "
    "and loudness is never measured.",
    "Re-running a pass places its work again rather than replacing the "
    "previous run's. Delete the added tracks first, or start a fresh cut.",
)


def build_report(
    config: EditingConfig, state: AutoRunState, pipeline=None
) -> AutoRunReport:
    """Everything worth saying about a run, gathered into one object."""
    report = AutoRunReport(
        run_id=state.run_id,
        status=state.status,
        config=state.config.to_dict(),
        stats=state.stats(),
        stages=[result.to_dict() for result in state.stages],
        gates=[gate.to_dict() for gate in state.gates],
        warnings=list(state.warnings),
        limitations=list(LIMITATIONS),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        run_dir=state.run_dir,
    )

    for name, target in (
        ("roughcut_build", "roughcut"), ("review_critique", "critic"),
        ("layers_build", "layers"), ("assets_plan", "assets"),
    ):
        result = state.stage(name)
        if result is not None and result.summary:
            setattr(report, target, dict(result.summary))

    review_plan = state.stage("review_plan")
    if review_plan is not None and review_plan.summary:
        report.critic.update({
            f"revisions_{key}": value
            for key, value in review_plan.summary.items()
        })

    for result in state.stages:
        for warning in result.warnings:
            entry = f"[{result.stage}] {warning}"
            if entry not in report.warnings:
                report.warnings.append(entry)

    report.feedback = _feedback_section(config, state, pipeline)
    report.check_in_premiere = _check_list(state)
    report.next_commands = _next_commands(state, report)
    return report


def _feedback_section(
    config: EditingConfig, state: AutoRunState, pipeline=None
) -> dict:
    """How much of this run is worth reviewing, and how to start.

    Filled whether or not the feedback stages ran, because the useful thing to
    tell someone reading a finished run is *that a review is possible* and how
    big it would be. Estimating costs one pass over artifacts already on disk
    and creates nothing.

    Never raises. A run report that fails because the optional review section
    could not be built would be a strictly worse report than one without it.
    """
    from editing.feedback import store as feedback_store

    # A run's feedback lives under *its own* artifacts directory, because each
    # run is hermetic. ``config`` here is the shared one the runner was built
    # with, so the pipeline's config is the authority whenever there is one.
    config = getattr(pipeline, "config", None) or config

    section = {
        "enabled": bool(state.config.feedback),
        "session_id": "",
        "items": 0,
        "questions": 0,
        "worth_reviewing": 0,
        "saved_to": str(feedback_store.sessions_root(config)),
        "start_command": (
            f"python -m editing.cli feedback start --run {state.run_id}"),
        "queue_command": (
            f"python -m editing.cli feedback queue --run {state.run_id} "
            "--limit 20"),
        "trains_nothing": (
            "Collects human review only. Nothing in this system trains on it, "
            "and nothing reads the preferences it produces."
        ),
    }

    for name, keys in (
        ("feedback_start", ("session_id",)),
        ("feedback_queue", ("questions", "worth_reviewing", "high_impact",
                            "uncertain", "risky_automatic", "structural",
                            "retention_risk")),
        ("feedback_report", ("items",)),
    ):
        result = state.stage(name)
        if result is not None and result.summary:
            for key in keys:
                if key in result.summary:
                    section[key] = result.summary[key]

    try:
        session = feedback_store.latest_session(config, run_id=state.run_id)
        if session is not None:
            section["session_id"] = session.session_id
            section["items"] = (session.counts or {}).get(
                "items", section["items"])
            section["saved_to"] = str(
                feedback_store.session_dir(config, session.session_id))
            # Both flags: --run scopes the command to this run's artifacts,
            # --session picks the review inside it. Printing only --session
            # would send the reader at the shared output directory, where
            # this session does not exist.
            section["queue_command"] = (
                f"python -m editing.cli feedback queue --run {state.run_id} "
                f"--session {session.session_id}"
            )
            section["report_command"] = (
                f"python -m editing.cli feedback report --run {state.run_id} "
                f"--session {session.session_id}"
            )
    except Exception:  # noqa: BLE001 - an optional section, never a failure
        pass

    if not section["worth_reviewing"] and pipeline is not None:
        try:
            estimate = pipeline.feedback_estimate(name=state.config.name)
            section["worth_reviewing"] = estimate.get("worth_reviewing", 0)
            section["suggested_limit"] = estimate.get("suggested_limit", 0)
            for key in ("high_impact", "uncertain", "risky_automatic",
                        "structural", "retention_risk"):
                section[key] = estimate.get(key, 0)
        except Exception:  # noqa: BLE001
            pass
    return section


def _check_list(state: AutoRunState) -> list[str]:
    """What a person should look at by hand once something has been executed."""
    out: list[str] = []
    for gate in state.gates:
        if not gate.executed:
            continue
        if gate.stage == "roughcut":
            out.append(
                f"Open '{gate.sequence_name}' and check the clip order and "
                "lengths against `roughcut placements`. Speed ripple is "
                "assumed by this system, not verified."
            )
        elif gate.stage == "review":
            out.append(
                "Check any marker that sits after a trim: Premiere's markers "
                "do not ripple with clips, so pre-existing ones may now "
                "describe the wrong frame."
            )
        elif gate.stage == "layers":
            out.append(
                "Check captions and cards on V2 for readability, and that "
                "nothing covers the health bar or the hotbar."
            )
        elif gate.stage == "assets":
            out.append(
                "Listen to A2 and A3: check the levels against the "
                "commentary, and that any looped bed does not have an "
                "audible seam."
            )
    if not out:
        out.append(
            "Nothing has been executed, so there is nothing in Premiere to "
            "check yet."
        )
    return out


def _next_commands(
    state: AutoRunState, report: Optional[AutoRunReport] = None
) -> list[str]:
    """The shortest path forward from wherever the run stopped."""
    out: list[str] = []
    failure = state.first_failure()
    if failure is not None and failure.failure is not None:
        if failure.failure.next_command:
            out.append(failure.failure.next_command)
        out.append(f"python -m editing.cli auto resume --run {state.run_id}")
        return out

    blocked = state.of_status("blocked")
    if blocked:
        out.append(
            f"python -m editing.cli auto explain-failure --run {state.run_id}"
        )

    ready = [gate for gate in state.gates if gate.ready]
    if ready:
        out.append(ready[0].command)
    elif not blocked:
        out.append(f"python -m editing.cli auto show-gates --run {state.run_id}")

    # Reviewing costs nothing and unblocks nothing, so it goes last -- but it
    # is the only next step that exists when every gate is shut, which is the
    # normal state of a --no-premiere run.
    feedback = (report.feedback if report is not None else {}) or {}
    if feedback.get("worth_reviewing") or feedback.get("session_id"):
        out.append(
            feedback.get("queue_command") if feedback.get("session_id")
            else feedback.get("start_command", "")
        )
    return [command for command in out if command]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(state: AutoRunState, report: AutoRunReport) -> str:
    lines: list[str] = []
    add = lines.append
    run = state.config
    stats = report.stats

    add(_RULE)
    add(f"AUTO RUN -- {state.run_id}")
    add(_RULE)
    add(f"status     : {state.status}")
    add(f"style      : {run.style}")
    add(f"footage    : {run.footage_folder or '(none)'}")
    add(f"assets     : {run.asset_library or '(default library)'}")
    add(f"run folder : {state.run_dir}")
    modes = [
        name for name, on in (
            ("mock", run.mock), ("no-premiere", run.no_premiere),
            ("markers-only", run.markers_only),
            ("skip-review", run.skip_review), ("skip-assets", run.skip_assets),
        ) if on
    ]
    add(f"modes      : {', '.join(modes) if modes else 'none'}")
    add("")

    # -- the honest headline ---------------------------------------------
    add(_THIN)
    add("WHAT THIS RUN DID AND DID NOT DO")
    add(_THIN)
    for line in _headlines(state, report):
        add(f"  {line}")
    add("")

    # -- stages ------------------------------------------------------------
    add(_THIN)
    add(f"STAGES  ({stats['passed']} passed, {stats['skipped']} skipped, "
        f"{stats['blocked']} blocked, {stats['failed']} failed)")
    add(_THIN)
    marks = {"passed": "+", "skipped": ".", "blocked": "?", "failed": "x",
             "pending": " ", "running": ">"}
    for name in STAGE_ORDER:
        result = state.stage(name)
        if result is None:
            add(f"    {name:<22} (never reached)")
            continue
        mark = marks.get(result.status, "?")
        detail = result.note or _one_line(result.summary)
        cached = " [checkpoint]" if result.from_checkpoint else ""
        add(f"  {mark} {name:<22} {result.status:<8} {detail[:38]}{cached}")
        if result.failure is not None:
            add(f"      why  : {result.failure.why[:150]}")
            if result.failure.next_command:
                add(f"      next : {result.failure.next_command[:150]}")
    add("")

    # -- what each pass produced ------------------------------------------
    for heading, data, keys in (
        ("ROUGH CUT", report.roughcut,
         ("sequence", "clips", "cut_duration", "source_duration", "markers",
          "operations", "unconverted")),
        ("CRITIC", report.critic,
         ("model", "mock", "frames_examined", "findings",
          "revisions_revisions", "revisions_accepted",
          "revisions_needs_human_review")),
        ("STYLE LAYERS", report.layers,
         ("style", "planned", "deferred", "marker_only", "operations",
          "edits_per_minute", "captions_per_minute", "zooms_per_minute")),
        ("ASSETS", report.assets,
         ("placeholders", "placed", "missing", "rejected", "unsafe",
          "marker_only", "distinct_assets", "operations")),
    ):
        if not data:
            continue
        add(_THIN)
        add(heading)
        add(_THIN)
        for key in keys:
            if key in data:
                add(f"  {key.replace('_', ' '):<26}: {data[key]}")
        add("")

    # -- gates -------------------------------------------------------------
    add(_THIN)
    add("EXECUTION GATES")
    add(_THIN)
    add("  Nothing below has run unless it says EXECUTED. Each one is a "
        "separate")
    add("  decision and needs its own --yes.")
    add("")
    for gate in state.gates:
        for line in gate.render().splitlines():
            add(f"  {line}")
        add("")

    # -- review ------------------------------------------------------------
    feedback = report.feedback or {}
    add(_THIN)
    add("WORTH A HUMAN LOOK")
    add(_THIN)
    if feedback.get("worth_reviewing"):
        add(f"  {feedback['worth_reviewing']} decision(s) in this run are "
            "worth reviewing:")
        for key, label in (
            ("high_impact", "a viewer would notice if it were wrong"),
            ("uncertain", "the system was not sure"),
            ("risky_automatic", "decided automatically"),
            ("structural", "hook, peak or ending"),
            ("retention_risk", "flagged as a place the episode may sag"),
        ):
            if feedback.get(key):
                add(f"    {feedback[key]:>4}  {label}")
    else:
        add("  Nothing was found to review, which usually means no artifacts "
            "were built.")
    if feedback.get("session_id"):
        add(f"  Review session : {feedback['session_id']} "
            f"({feedback.get('items', 0)} rating(s) so far)")
    else:
        add("  No review has been started for this run.")
        add(f"    start: {feedback.get('start_command', '')}")
    add(f"  Queue          : {feedback.get('queue_command', '')}")
    add(f"  Feedback lands : {feedback.get('saved_to', '')}")
    add(f"  {feedback.get('trains_nothing', '')}")
    add("")

    # -- warnings ----------------------------------------------------------
    if report.warnings:
        add(_THIN)
        add(f"WARNINGS ({len(report.warnings)})")
        add(_THIN)
        for warning in report.warnings[:40]:
            add(f"  ! {warning[:160]}")
        if len(report.warnings) > 40:
            add(f"  ... and {len(report.warnings) - 40} more.")
        add("")

    # -- what to do next ---------------------------------------------------
    add(_THIN)
    add("NEXT")
    add(_THIN)
    for command in report.next_commands:
        add(f"  {command}")
    add("")
    add("  Check by hand in Premiere:")
    for line in report.check_in_premiere:
        add(f"    - {line}")
    add("")

    add(_THIN)
    add("LIMITATIONS")
    add(_THIN)
    for line in report.limitations:
        add(f"  - {line}")
    add("")
    add(_RULE)
    return "\n".join(lines)


def _headlines(state: AutoRunState, report: AutoRunReport) -> list[str]:
    """The four or five sentences somebody would want read aloud."""
    out: list[str] = []
    run = state.config

    roughcut = report.roughcut
    if roughcut:
        executed = any(
            g.stage == "roughcut" and g.executed for g in state.gates
        )
        out.append(
            f"Rough cut plan built ({roughcut.get('clips', 0)} clip(s), "
            f"{roughcut.get('cut_duration', 0)}s) and "
            + ("EXECUTED in Premiere." if executed else "NOT executed.")
        )
    else:
        out.append("No rough cut was produced.")

    critic = report.critic
    if state.satisfied("review_critique") and critic:
        out.append(
            "Review critic used MOCK mode: findings come from frame metadata, "
            "not from the pictures."
            if critic.get("mock") else
            f"Review critic ran with {critic.get('model', 'a model')} over "
            f"{critic.get('frames_examined', 0)} frame(s)."
        )
    else:
        blocked = state.stage("review_critique")
        reason = (blocked.note or (
            blocked.failure.why if blocked and blocked.failure else ""
        )) if blocked else ""
        out.append(f"Review pass did not run{': ' + reason if reason else '.'}")

    layers = report.layers
    if layers:
        out.append(
            f"Style '{layers.get('style', run.style)}' planned "
            f"{layers.get('planned', 0)} item(s) at "
            f"{layers.get('edits_per_minute', 0)} active edits/min"
            + (" (markers only)." if run.markers_only else ".")
        )

    assets = report.assets
    if assets:
        if assets.get("placed"):
            out.append(
                f"Assets: {assets['placed']} placed from "
                f"{assets.get('distinct_assets', 0)} file(s), "
                f"{assets.get('missing', 0)} missing."
            )
        else:
            out.append(
                "Asset library empty or nothing matched; every placeholder is "
                "a marker. `auto report` lists what to go and find."
            )

    if run.no_premiere:
        out.append(
            "This run was created with --no-premiere, so no execution gate "
            "can ever be opened from it."
        )
    else:
        ready = [g for g in state.gates if g.ready]
        executed = [g for g in state.gates if g.executed]
        if executed:
            out.append(
                f"{len(executed)} stage(s) have been executed against "
                "Premiere."
            )
        if ready:
            out.append(
                f"{len(ready)} gate(s) are ready to execute; each needs its "
                "own --yes."
            )
        elif not executed:
            blocked = [g for g in state.gates if g.blocked_reason]
            if blocked:
                out.append(
                    f"No gate is ready. First blocker: "
                    f"{blocked[0].blocked_reason[:120]}"
                )
    return out


def _one_line(summary: dict) -> str:
    if not summary:
        return ""
    parts = []
    for key, value in list(summary.items())[:3]:
        if isinstance(value, (dict, list)):
            continue
        parts.append(f"{key}={value}")
    return ", ".join(parts)


def write_reports(
    config: EditingConfig, state: AutoRunState, pipeline=None
) -> tuple:
    """Write both reports and return their paths."""
    report = build_report(config, state, pipeline)
    json_path, text_path = store.report_paths(config, state.run_id)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    text_path.write_text(render(state, report), encoding="utf-8")
    return json_path, text_path


def render_status(state: AutoRunState) -> str:
    """The short form: one line per stage, plus where the run stands."""
    stats = state.stats()
    lines = [
        f"{state.run_id}  [{state.status}]  style={state.config.style}",
        f"  folder : {state.config.footage_folder or '(none)'}",
        f"  run    : {state.run_dir}",
        f"  stages : {stats['passed']} passed, {stats['skipped']} skipped, "
        f"{stats['blocked']} blocked, {stats['failed']} failed "
        f"({stats['from_checkpoint']} from checkpoints)",
        "",
    ]
    marks = {"passed": "+", "skipped": ".", "blocked": "?", "failed": "x",
             "pending": " ", "running": ">"}
    for name in STAGE_ORDER:
        result = state.stage(name)
        if result is None:
            lines.append(f"    {name:<22} pending")
            continue
        note = result.note or _one_line(result.summary)
        lines.append(
            f"  {marks.get(result.status, '?')} {name:<22} "
            f"{result.status:<8} {note[:44]}"
        )
    lines.append("")

    failure = state.first_failure()
    if failure is not None and failure.failure is not None:
        lines.append(failure.failure.render())
        lines.append("")
    ready = [gate for gate in state.gates if gate.ready]
    if ready:
        lines.append(f"  {len(ready)} gate(s) ready. See: "
                     f"python -m editing.cli auto show-gates "
                     f"--run {state.run_id}")
    return "\n".join(lines)


def render_failure(state: AutoRunState) -> str:
    """Everything that went wrong, with the command for each.

    Blocked stages are included, not just failed ones: a blocked stage is
    usually the more actionable of the two, because it means a tool or a
    server is missing rather than something being broken.
    """
    failed = state.of_status("failed")
    blocked = state.of_status("blocked")
    lines = [f"Run {state.run_id} [{state.status}]", ""]

    if not failed and not blocked:
        lines.append("  Nothing failed or was blocked.")
        ready = [gate for gate in state.gates if gate.ready]
        if ready:
            lines.append(f"  Next: {ready[0].command}")
        return "\n".join(lines)

    for result in failed:
        lines.append(f"FAILED  {result.stage}")
        if result.failure is not None:
            for line in result.failure.render().splitlines()[1:]:
                lines.append(f"  {line}")
        else:
            for error in result.errors[:3]:
                lines.append(f"    {error}")
        lines.append("")

    for result in blocked:
        lines.append(f"BLOCKED {result.stage}")
        why = result.note or (
            result.failure.why if result.failure else "no reason recorded"
        )
        lines.append(f"    why  : {why}")
        command = (
            result.failure.next_command if result.failure
            else result.next_command
        )
        if command:
            lines.append(f"    next : {command}")
        lines.append("")

    lines.append(f"  Log    : {Path(state.run_dir) / 'logs' / 'run.log'}")
    lines.append(
        f"  Resume : python -m editing.cli auto resume --run {state.run_id}"
    )
    return "\n".join(lines)

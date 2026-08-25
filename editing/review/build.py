"""Assembling one run's review package.

Reads the run state and whatever artifacts exist, copies the small readable
things into the review folder, points at the video, and fills the five lists
the index is built from.

**Never raises.** A review package is the thing a person opens *after*
something went wrong as often as after it went right, so a missing artifact is
an item marked absent rather than an exception. The one thing it will not do is
claim a file exists when it does not.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from editing.auto import store as auto_store
from editing.config import EditingConfig
from editing.reliability import report as gate_report
from editing.reliability.schema import GateReport
from editing.review.schema import ReviewItem, ReviewPackage, now
from editing.review import store

logger = logging.getLogger("nova.editing.review.build")

#: Files bigger than this are pointed at rather than copied. A report is
#: kilobytes; anything past this is not a report.
MAX_COPY_BYTES = 4 * 1024 * 1024


def build_package(
    config: EditingConfig,
    state,
    *,
    checks: Optional[GateReport] = None,
    caption_plan=None,
    audio_plan=None,
    visual_plan=None,
    final_edit=None,
    copy_files: bool = True,
) -> ReviewPackage:
    """Gather one run into a review package. Writes nothing by itself."""
    run = state.config
    folder = store.package_dir(config, state.run_id)

    package = ReviewPackage(
        run_id=state.run_id,
        style=run.style,
        footage_folder=run.footage_folder,
        run_status=state.status,
        created_at=now(),
        folder=str(folder),
    )

    roughcut = _summary(state, "roughcut_build")
    package.sequence_name = str(roughcut.get("sequence") or "")

    _video(package, state)
    _items(config, package, state, folder, copy_files=copy_files)
    _checks(package, checks)
    _visuals(package, state, visual_plan, final_edit)
    _headline(package, state, caption_plan, audio_plan)
    _changed(package, state, caption_plan, audio_plan, visual_plan)
    _watch_for(package, state, caption_plan, audio_plan, visual_plan)
    _weak_points(package, state, checks)
    _decisions(package, state, checks)
    _commands(package)

    package.warnings = [
        f"[{result.stage}] {warning}"
        for result in state.stages for warning in result.warnings
    ][:60]
    return package


def write_package(
    config: EditingConfig,
    state,
    *,
    checks: Optional[GateReport] = None,
    caption_plan=None,
    audio_plan=None,
    visual_plan=None,
    final_edit=None,
) -> tuple:
    """Build the package, copy what belongs in it, write the index.

    Returns ``(package, [paths written])``.
    """
    from editing.review import index as index_module

    package = build_package(
        config, state, checks=checks,
        caption_plan=caption_plan, audio_plan=audio_plan,
        visual_plan=visual_plan, final_edit=final_edit,
    )
    written = [store.save_package(config, package)]
    written.append(store.save_index(
        config, package.run_id, index_module.render_index(package)))
    if checks is not None:
        written.append(store.save_checks(config, package.run_id, checks))
        written.append(store.save_text(
            store.package_dir(config, package.run_id) / "checks.txt",
            gate_report.render(checks),
        ))
    return package, written


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------

def _summary(state, stage: str) -> dict:
    result = state.stage(stage)
    return dict(result.summary) if result is not None and result.summary else {}


def _video(package: ReviewPackage, state) -> None:
    render = _summary(state, "render_proxy")
    package.video = str(render.get("video") or "")
    package.video_duration = float(render.get("duration") or 0.0)
    package.video_size_mb = float(render.get("size_mb") or 0.0)
    if package.video:
        try:
            package.video_exists = Path(package.video).exists()
        except OSError:
            package.video_exists = False
    # A mocked render wrote a placeholder file. It exists and it is not a
    # video, and saying otherwise here would send somebody to double-click it.
    if render.get("mock"):
        package.video_exists = False


#: name, title, relative-to-run path, kind, note. Everything a run can produce
#: that is worth opening, in the order the index lists them.
CANDIDATES = (
    ("run_report", "The run report", "reports/report.txt", "report",
     "what ran, what was refused, and what to type next"),
    ("run_report_json", "The run report, as JSON", "reports/report.json",
     "plan", "the same thing, for a script"),
    ("render_notes", "Timestamped review notes", "", "notes",
     "what to look at, minute by minute, while the proxy plays"),
    ("captions", "Caption polish report", "artifacts/polish/{name}.captions.txt",
     "report", "which lines earned a caption, and which were refused"),
    ("captions_json", "Caption plan", "artifacts/polish/{name}.captions.json",
     "plan", "every line considered, with its reason"),
    ("subtitles", "Caption sidecar", "artifacts/polish/{name}.captions.srt",
     "subtitles", "load this beside the proxy to see the captions"),
    ("audio", "Audio polish report", "artifacts/polish/{name}.audio.txt",
     "report", "which sounds were planned, and what is missing"),
    ("visuals", "Visual layer report",
     "artifacts/visuals/{name}.visuals.txt", "report",
     "which moments earned emphasis, and which were refused"),
    ("visuals_json", "Visual plan",
     "artifacts/visuals/{name}.visuals.json", "plan",
     "every moment and every treatment, refusals included"),
    ("visual_markers", "Visual markers",
     "artifacts/visuals/{name}.visuals.md", "notes",
     "where each effect would land, while you watch the proxy"),
    ("final_edit", "Final edit plan",
     "artifacts/visuals/{name}.final.txt", "report",
     "the cut, the captions, the sound and the visuals, clip by clip"),
    ("final_edit_json", "Final edit plan, as JSON",
     "artifacts/visuals/{name}.final.json", "plan",
     "the same thing, for a script"),
    ("visual_premiere", "Premiere visual plan",
     "artifacts/visuals/{name}.premiere.json", "plan",
     "the operations Premiere could run. Nothing has been executed"),
    ("visual_compare", "Visual comparison",
     "artifacts/visuals/{name}.compare.json", "plan",
     "the visual layer against the cut without it"),
    ("audio_json", "Audio polish plan", "artifacts/polish/{name}.audio.json",
     "plan", "every cue considered, with its reason"),
    ("director", "Director report", "artifacts/director/{name}.plan.txt",
     "report", "what the model decided and what the rules refused"),
    ("director_json", "Director plan", "artifacts/director/{name}.plan.json",
     "plan", "every decision, accepted and rejected"),
    ("retention", "Retention report", "artifacts/retention/{name}.plan.txt",
     "report", "the cold open, the compression, the protections"),
    ("retention_json", "Retention plan", "artifacts/retention/{name}.plan.json",
     "plan", "every retention decision, accepted and rejected"),
    ("comparison", "Retention comparison",
     "artifacts/retention/{name}.compare.json", "plan",
     "the reshaped cut against the cut it was built from"),
    ("episode", "Episode memory", "artifacts/episode/{name}.memory.json",
     "plan", "beats, objectives, setups, payoffs, open loops"),
    ("retention_plan", "Retention findings",
     "artifacts/episode/{name}.retention.json", "plan",
     "risks, hooks, a peak and an ending"),
    ("roughcut", "The cut", "artifacts/roughcut/{name}.json", "plan",
     "every clip, in order, with why it was kept"),
    ("layers", "Style layers", "artifacts/layers/{name}.json", "plan",
     "what the style pass planned on top of the cut"),
    ("transcripts", "Transcripts", "artifacts/transcripts", "plan",
     "what was heard, per clip"),
    ("log", "The run log", "logs/run.log", "log",
     "every stage, in order, with its timings"),
)


def _items(
    config: EditingConfig,
    package: ReviewPackage,
    state,
    folder: Path,
    *,
    copy_files: bool,
) -> None:
    run_dir = Path(state.run_dir) if state.run_dir else auto_store.run_dir(
        config, state.run_id)
    name = state.config.name or "structure"

    for key, title, relative, kind, note in CANDIDATES:
        if key == "render_notes":
            path = Path(_summary(state, "render_proxy").get("notes") or "")
            if not str(path):
                package.items.append(ReviewItem(
                    name=key, title=title, kind=kind, path="", note=note))
                continue
        else:
            path = run_dir / relative.format(name=name)

        item = ReviewItem(
            name=key, title=title, kind=kind, path=str(path), note=note)
        try:
            item.exists = path.exists()
            if item.exists and path.is_file():
                item.size_bytes = path.stat().st_size
        except OSError:
            item.exists = False
        if (copy_files and item.exists and path.is_file()
                and item.size_bytes <= MAX_COPY_BYTES):
            copied = _copy(path, folder, key)
            if copied is not None:
                item.copied_to = str(copied)
        package.items.append(item)

    if package.video:
        package.items.insert(0, ReviewItem(
            name="video",
            title="The proxy",
            kind="video",
            path=package.video,
            exists=package.video_exists,
            note="the cut, as a watchable file. Nothing else in this package "
                 "is in it",
        ))


def _copy(path: Path, folder: Path, key: str) -> Optional[Path]:
    """Copy one small file into the review folder.

    Named after the item rather than the source, so ``captions.txt`` in a
    review folder is the caption report whatever the run called it internally.
    """
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{key}{path.suffix or '.txt'}"
        shutil.copyfile(path, target)
        return target
    except OSError as exc:  # noqa: BLE001 - a copy is never worth failing over
        logger.debug("Could not copy %s into the review package: %s", path, exc)
        return None


def _checks(package: ReviewPackage, checks: Optional[GateReport]) -> None:
    if checks is None:
        package.checks = {"status": "not run", "summary": [
            "The reliability checks were not run for this package."]}
        return
    package.checks = {
        **checks.stats(),
        "summary": gate_report.summary_lines(checks),
        "warnings": [
            {"name": r.name, "reason": r.reason, "fix": r.suggested_fix}
            for r in checks.warnings
        ],
        "failures": [
            {"name": r.name, "reason": r.reason, "fix": r.suggested_fix,
             "can_continue": r.can_continue}
            for r in checks.failures
        ],
    }


def _visuals(package: ReviewPackage, state, visual_plan, final_edit) -> None:
    """The creative visual layer, as the six questions the index asks.

    Read from the plan object when one was handed in, and from the stage
    summary otherwise -- a package rebuilt days later still has the stage
    summary, and half an answer beats none.
    """
    summary = _summary(state, "visual_plan")
    composed = _summary(state, "final_edit_plan")
    enabled = str(getattr(state.config, "visual_layer", "off")) != "off"

    section = {
        "enabled": enabled,
        "ran": bool(summary),
        "layer": summary.get("layer",
                             getattr(state.config, "visual_layer", "off")),
        "accepted": int(summary.get("accepted") or 0),
        "rejected": int(summary.get("rejected") or 0),
        "moments": int(summary.get("moments") or 0),
        "untreated_moments": int(summary.get("untreated_moments") or 0),
        "effects_per_minute": float(summary.get("effects_per_minute") or 0.0),
        "callouts_per_minute": float(
            summary.get("callouts_per_minute") or 0.0),
        "ceiling": float(summary.get("ceiling") or 0.0),
        "by_effect": dict(summary.get("by_effect") or {}),
        "by_family": dict(summary.get("by_family") or {}),
        "by_moment_kind": dict(summary.get("by_moment_kind") or {}),
        "by_reject_reason": dict(summary.get("by_reject_reason") or {}),
        "placeholder_only": int(summary.get("placeholder_only") or 0),
        "premiere_operations": int(composed.get("premiere_operations") or 0),
        "premiere_unsupported": int(composed.get("premiere_unsupported") or 0),
        "busy_segments": int(composed.get("busy_segments") or 0),
        # Loud, and always present: a plan of intentions must never read as a
        # video with effects in it.
        "rendered": False,
        "executed": False,
        "not_rendered": (
            "No effect in this plan has been drawn, rendered or executed. The "
            "Premiere operations are proposals validated offline; the FFmpeg "
            "side is a capability statement and a marker file."
        ),
    }

    # What FFmpeg could and could not show. Read off the preview plan rather
    # than re-derived, so the review folder and the visual report cannot
    # disagree about which effects are invisible in a proxy.
    preview = final_edit.execution.preview if final_edit is not None else None
    if preview is not None:
        preview_stats = preview.stats()
        section["preview"] = {
            "burnable": preview_stats["burnable"],
            "sidecar_only": preview_stats["sidecar_only"],
            "invisible": preview_stats["invisible"],
            "burned_in": False,
            "sidecar_path": preview.sidecar_path,
            "note": preview.burn_in_note,
            "limitations": [
                f"{item.effect}: {item.reason}"
                for item in preview.items if item.reason
            ][:20],
        }
    else:
        section["preview"] = {
            "burnable": 0, "sidecar_only": 0, "invisible": 0,
            "burned_in": False, "sidecar_path": "",
            "note": "No preview plan was built for this run "
                    "(--visual-mode proxy_preview builds one).",
            "limitations": [],
        }

    if visual_plan is not None:
        from editing.visuals import report as visual_report

        premiere = None
        if final_edit is not None:
            premiere = final_edit.execution.premiere
        built = visual_report.build_report(
            visual_plan, premiere=premiere, preview=preview)
        section.update({
            "answers": built.answers,
            "overdone_risks": built.overdone_risks,
            "manual_checks": built.manual_checks,
        })
        stats = visual_plan.stats()
        section.update({
            "callouts_per_minute": stats["callouts_per_minute"],
            "by_family": stats["by_family"],
            "by_moment_kind": stats["by_moment_kind"],
            "by_effect": stats["by_effect"],
            "by_reject_reason": stats["by_reject_reason"],
            "ceiling": visual_plan.config.max_effects_per_minute,
        })
    elif enabled:
        section["answers"] = [{
            "question": "What visual effects were added?",
            "answer": (
                f"{section['accepted']} treatment(s) from "
                f"{section['moments']} moment(s); {section['rejected']} were "
                "refused. The visual report has the detail."),
        }]
    package.visuals = section


def _headline(package: ReviewPackage, state, caption_plan, audio_plan) -> None:
    """What was produced, in four or five sentences."""
    run = state.config
    roughcut = _summary(state, "roughcut_build")
    out: list[str] = []

    if package.video_exists:
        out.append(
            f"A proxy was rendered: {package.video_duration:.0f}s, "
            f"{package.video_size_mb:.0f} MB, at {package.video}"
        )
    elif package.video:
        out.append(
            "A render was attempted and there is no watchable file. "
            "The checks below say why."
        )
    else:
        out.append(
            "No video was rendered, so nothing here has been watched. "
            "--render-proxy on the next run produces one."
        )

    if roughcut:
        out.append(
            f"The cut is {roughcut.get('clips', 0)} clip(s), "
            f"{roughcut.get('cut_duration', 0):.0f}s, drawn from "
            f"{roughcut.get('source_duration', 0):.0f}s of footage, chosen by "
            f"the {roughcut.get('selection', 'heuristic')} selector."
        )
    else:
        out.append("No cut was produced by this run.")

    out.append(f"Style: {run.style}. Footage: {run.footage_folder or '(none)'}.")

    # The transcript, in one line. Every layer that reads words -- the story
    # layer, the retention planner, the captions -- is only as good as this,
    # so "there were no words" belongs at the top rather than in a warning.
    analyzed = _summary(state, "analyze")
    transcribed = _summary(state, "transcribe")
    words = int(analyzed.get("transcript_words") or 0)
    if words:
        source = (
            f"local Whisper ({transcribed.get('model', '?')})"
            if transcribed.get("transcribed") else
            "transcripts found beside the footage"
        )
        out.append(
            f"Transcript: {words} word(s) across "
            f"{analyzed.get('segments_with_speech', 0)} segment(s), from "
            f"{source}."
            + ("  MOCK: these words were fabricated and nothing heard the "
               "footage." if transcribed.get("mock") else "")
        )
    else:
        out.append(
            "Transcript: none. Every layer that reads words -- the story "
            "layer, the retention planner, the captions -- worked blind."
        )

    if caption_plan is not None and caption_plan.config.enabled:
        stats = caption_plan.stats()
        out.append(
            f"Captions: {stats['accepted']} placed out of "
            f"{stats['considered']} line(s) considered "
            f"({stats['captions_per_minute']:.2f} a minute). They are not in "
            "the video -- load the sidecar beside it."
        )
    if audio_plan is not None and audio_plan.config.enabled:
        stats = audio_plan.stats()
        out.append(
            f"Audio polish: {stats['accepted']} cue(s) planned, "
            f"{stats['placed']} from the library, "
            f"{stats['missing_assets']} with nothing behind them. None of it "
            "is in the video."
        )
    package.headline = out


def _changed(package: ReviewPackage, state, caption_plan, audio_plan,
             visual_plan=None) -> None:
    """What this edit did to the footage, as counts."""
    out: list[str] = []
    retention = _summary(state, "retention_cut")
    director = _summary(state, "director_plan")

    if director:
        out.append(
            f"The director proposed {director.get('decisions', 0)} decision(s); "
            f"{director.get('accepted', 0)} were accepted and "
            f"{director.get('rejected', 0)} refused by the rules."
            + ("  It ran in MOCK mode, so those came from fixed rules rather "
               "than a model." if director.get("mock") else "")
        )
    if retention.get("applied"):
        out.append(
            f"The cut was reshaped: {retention.get('base_duration', 0):.0f}s "
            f"-> {retention.get('cut_duration', 0):.0f}s."
        )
        if retention.get("cold_open"):
            out.append(
                f"It opens on a {retention.get('cold_open_type', '?')} "
                f"({retention.get('cold_open_seconds', 0):.0f}s) lifted from "
                "later in the episode."
            )
        out.append(
            f"{retention.get('zones_compressed', 0)} sagging zone(s) "
            f"compressed, {retention.get('seconds_removed', 0):.0f}s removed, "
            f"{retention.get('dead_air_cut', 0)} stretch(es) of dead air "
            "trimmed."
        )
        out.append(
            f"{retention.get('setups_protected', 0)} setup(s) and "
            f"{retention.get('payoffs_protected', 0)} payoff(s) were "
            f"protected; {retention.get('refused', 0)} action(s) were refused."
        )
    elif retention:
        out.append(
            "The retention pass decided everything and changed nothing "
            "(report-only mode)."
        )

    if caption_plan is not None and caption_plan.accepted:
        for decision in caption_plan.accepted[:6]:
            out.append(
                f'A caption at {decision.start:.0f}s: "{decision.text}" '
                f"({decision.moment})"
            )
    if audio_plan is not None and audio_plan.accepted:
        kinds = audio_plan.by_kind()
        out.append(
            "Sound planned: "
            + ", ".join(f"{count} x {kind}" for kind, count in kinds.items())
            + ". None of it plays in the proxy."
        )
    if visual_plan is not None and visual_plan.accepted:
        out.append(
            "Visual treatments planned: "
            + ", ".join(f"{count} x {effect.replace('_', ' ')}"
                        for effect, count in sorted(
                            visual_plan.by_effect().items(),
                            key=lambda kv: -kv[1])[:6])
            + f". {len(visual_plan.rejected)} were refused, and none of it is "
            "in any video."
        )
    if not out:
        out.append(
            "Nothing was reshaped, captioned or scored. This is the "
            "rule-based cut as it came out."
        )
    package.changed = out


def _watch_for(package: ReviewPackage, state, caption_plan, audio_plan,
               visual_plan=None) -> None:
    """What a person should be looking at while the proxy plays."""
    out: list[str] = []
    retention = _summary(state, "retention_cut")

    if retention.get("cold_open"):
        out.append(
            f"The first {retention.get('cold_open_seconds', 0):.0f}s. It was "
            "lifted from later in the episode, and the opening is the single "
            "change most likely to be wrong."
        )
    if retention.get("zones_compressed"):
        out.append(
            f"The {retention.get('zones_compressed', 0)} compressed "
            "stretch(es). Compression is where the story most often stops "
            "following."
        )
    if retention.get("dead_air_cut"):
        out.append(
            f"{retention.get('dead_air_cut', 0)} silence(s) were trimmed. "
            "Listen for clipped speech at the start and end of lines."
        )
    if package.video_exists:
        out.append(
            "Every cut point. Speed ripple in the proxy is setpts/atempo "
            "rather than Premiere's retime, so timing is close and not exact."
        )
    if caption_plan is not None:
        from editing.polish import report as polish_report
        out.extend(polish_report.caption_checks(caption_plan))
    if audio_plan is not None:
        from editing.polish import report as polish_report
        out.extend(polish_report.audio_checks(audio_plan))
    if visual_plan is not None and visual_plan.accepted:
        from editing.visuals import report as visual_report
        out.extend(visual_report.manual_checks(visual_plan))
    if not out:
        out.append(
            "Watch the whole thing once without stopping. This run made no "
            "structural change worth singling out."
        )
    package.watch_for = out


def _weak_points(package: ReviewPackage, state, checks) -> None:
    """Where this run already knows it is on thin ice."""
    out: list[str] = []
    if checks is not None:
        for result in checks.failures + checks.warnings:
            out.append(f"{result.name}: {result.reason}")

    for result in state.stages:
        if result.status in ("failed", "blocked"):
            why = result.note or (
                result.failure.why if result.failure else "no reason recorded")
            out.append(f"{result.stage} did not complete: {why[:160]}")

    if not out:
        out.append(
            "No check failed and no stage was blocked. That says the shape is "
            "right, not that the edit is good."
        )
    package.weak_points = out[:40]


def _decisions(package: ReviewPackage, state, checks) -> None:
    """What only a person can settle."""
    out: list[str] = []
    retention = _summary(state, "retention_cut")
    feedback = _summary(state, "feedback_queue")

    if retention.get("cold_open"):
        out.append(
            "Keep the cold open, or put the episode back in order? "
            "`retention show-cold-open` lists what it passed over."
        )
    if retention.get("unresolved"):
        out.append(
            f"{retention['unresolved']} unresolved setup/payoff warning(s). "
            "Only somebody who watched the footage can say whether they "
            "matter."
        )
    if feedback.get("worth_reviewing"):
        out.append(
            f"{feedback['worth_reviewing']} decision(s) are queued for "
            "review: `feedback queue --run "
            f"{package.run_id}`."
        )
    if checks is not None and checks.blocking:
        out.append(
            "The output failed a check that says it is not usable. Fix that "
            "before reviewing anything else."
        )
    visuals = package.visuals or {}
    if visuals.get("accepted"):
        out.append(
            f"Decide whether the {visuals['accepted']} visual treatment(s) "
            "are worth executing. Nothing has been drawn, and the Premiere "
            "plan is inspectable before anything runs."
        )
    if visuals.get("placeholder_only"):
        out.append(
            f"{visuals['placeholder_only']} treatment(s) are notes and "
            "nothing else. Do them by hand or drop them."
        )
    if not package.video_exists and package.video:
        out.append(
            "There is no watchable file. Decide whether to re-render or to "
            "review the plans as they are."
        )
    if not out:
        out.append(
            "Nothing needs a decision before you watch it. Everything else "
            "follows from what you think of the cut."
        )
    package.decisions_needed = out


def _commands(package: ReviewPackage) -> None:
    run_id = package.run_id
    package.commands = [
        f"python -m editing.cli auto report --run {run_id}",
        f"python -m editing.cli auto show-checks --run {run_id}",
        f"python -m editing.cli review summary --run {run_id}",
        f"python -m editing.cli feedback queue --run {run_id} --limit 20",
    ]
    if package.video_exists:
        package.commands.insert(
            0, f"python -m editing.cli render open --run {run_id}")

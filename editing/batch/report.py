"""The batch summary: what happened to forty folders, on one page.

Failures first, then what needs review, then the full list. A batch summary
that opens with thirty ticks is a batch summary nobody reads to the end, and
the two folders that broke are exactly what somebody came for.
"""
from __future__ import annotations

from editing.batch.schema import BatchSummary

_RULE = "=" * 78
_THIN = "-" * 78

#: Plain-English names for the skip codes.
SKIP_REASONS = {
    "already_completed": "a completed run already exists",
    "already_failed": "an unfinished run exists",
    "limit_reached": "--limit was reached",
    "no_video_files": "no video files",
    "not_new": "--only-new, and this folder has been run before",
    "excluded": "excluded",
}


def render(summary: BatchSummary, *, limit: int = 60) -> str:
    """The whole batch, readable."""
    lines: list[str] = []
    add = lines.append
    stats = summary.stats()
    config = summary.config

    add(_RULE)
    add(f"BATCH -- {summary.batch_id}")
    add(_RULE)
    add(f"status   : {summary.status}")
    add(f"root     : {config.root}")
    add(f"style    : {config.style}")
    modes = [
        name for name, on in (
            ("dry-run", config.dry_run), ("director", config.director),
            ("retention-cut", config.retention_cut),
            ("render-proxy", config.render_proxy),
            ("no-premiere", config.no_premiere), ("mock", config.mock),
            ("transcribe", config.transcribe),
            ("only-new", config.only_new), ("resume", config.resume),
            ("force", config.force),
        ) if on
    ]
    if config.captions != "off":
        modes.append(f"captions={config.captions}")
    if config.audio_polish != "off":
        modes.append(f"audio={config.audio_polish}")
    if config.visual_layer != "off":
        modes.append(f"visuals={config.visual_layer}")
    add(f"modes    : {', '.join(modes) if modes else 'none'}")
    add(f"folders  : {stats['folders']} "
        f"({stats['completed']} completed, {stats['failed']} failed, "
        f"{stats['skipped']} skipped, {stats['planned']} planned)")
    add(f"videos   : {stats['videos']}")
    add(f"elapsed  : {stats['elapsed']:.0f}s")
    add("")

    if config.dry_run:
        add(_THIN)
        add("DRY RUN -- NOTHING WAS CREATED")
        add(_THIN)
        add("  Every folder below shows what would have happened. No run")
        add("  folder was made and no footage was read.")
        add("")

    if summary.failed:
        add(_THIN)
        add(f"FAILED ({len(summary.failed)})")
        add(_THIN)
        for entry in summary.failed:
            add(f"  x {entry.label}")
            add(f"      why : {entry.reason[:200]}")
            for warning in entry.warnings[:2]:
                add(f"      note: {warning[:160]}")
            if entry.run_id:
                add(f"      next: python -m editing.cli auto explain-failure "
                    f"--run {entry.run_id}")
        add("")

    needs_review = [
        entry for entry in summary.completed
        if entry.checks_blocking or entry.stages_failed
    ]
    if needs_review:
        add(_THIN)
        add(f"COMPLETED, BUT WORTH LOOKING AT ({len(needs_review)})")
        add(_THIN)
        for entry in needs_review:
            add(f"  ! {entry.label}   run {entry.run_id}")
            if entry.stages_failed:
                add(f"      {entry.stages_failed} stage(s) failed, "
                    f"{entry.stages_blocked} blocked")
            if entry.checks_blocking:
                add(f"      {entry.checks_blocking} reliability check(s) say "
                    "the output is not usable")
            if entry.review_index:
                add(f"      review: {entry.review_index}")
        add("")

    watchable = [entry for entry in summary.completed if entry.video_path]
    if watchable:
        add(_THIN)
        add(f"WATCHABLE ({len(watchable)})")
        add(_THIN)
        for entry in watchable[:limit]:
            add(f"  {entry.label}")
            add(f"      {entry.video_path}")
            if entry.review_index:
                add(f"      {entry.review_index}")
        add("")

    add(_THIN)
    add("EVERY FOLDER")
    add(_THIN)
    for entry in summary.entries[:limit]:
        add(f"  {entry.line()}")
    if len(summary.entries) > limit:
        add(f"  ... and {len(summary.entries) - limit} more.")
    add("")

    if stats["by_skip_reason"]:
        add(_THIN)
        add("WHY FOLDERS WERE SKIPPED")
        add(_THIN)
        for code, count in sorted(
            stats["by_skip_reason"].items(), key=lambda kv: -kv[1]
        ):
            add(f"  {count:>4}  {code:<20} {SKIP_REASONS.get(code, '')}")
        add("")

    if summary.warnings:
        add(_THIN)
        add(f"WARNINGS ({len(summary.warnings)})")
        add(_THIN)
        for warning in summary.warnings[:20]:
            add(f"  ! {warning}")
        add("")

    add(_THIN)
    add("NEXT")
    add(_THIN)
    for command in next_commands(summary):
        add(f"  {command}")
    add("")
    add("  Nothing in this batch has been watched. A completed run means "
        "every")
    add("  stage finished, not that the edit is any good.")
    add(_RULE)
    return "\n".join(lines)


def next_commands(summary: BatchSummary) -> list[str]:
    """The shortest path forward from wherever the batch got to."""
    out: list[str] = []
    if summary.config.dry_run:
        out.append("Re-run without --dry-run to process these folders.")
        return out
    if summary.failed:
        out.append(
            "python -m editing.cli auto batch --root "
            f"{summary.config.root} --resume   (retry the failures)"
        )
    reviewable = [e for e in summary.completed if e.review_index]
    if reviewable:
        out.append(f"start here: {reviewable[0].review_index}")
    out.append("python -m editing.cli review open-latest")
    out.append("python -m editing.cli auto list-runs")
    return out


def render_short(summary: BatchSummary) -> str:
    """One line per folder, for a terminal that is already busy."""
    stats = summary.stats()
    lines = [
        f"{summary.batch_id}  [{summary.status}]  "
        f"{stats['completed']} completed, {stats['failed']} failed, "
        f"{stats['skipped']} skipped",
        "",
    ]
    lines.extend(f"  {entry.line()}" for entry in summary.entries)
    return "\n".join(lines)

"""``review_index.md``: the one file to open.

Five sections in the order somebody asks the questions:

1. **Watch this** -- the video, and what to load beside it
2. **What changed** -- counts, not claims
3. **Watch for** -- the specific moments most likely to be wrong
4. **Weak points** -- what the run already knows is thin
5. **Needs you** -- what only a person can settle

Then the files, then the commands. Markdown because it renders in every editor
and reads fine as plain text when it does not.
"""
from __future__ import annotations

from editing.review.schema import NOT_A_VERDICT, ReviewPackage

#: Section headings for the file list, in the order they are useful.
KIND_HEADINGS = (
    ("video", "The video"),
    ("subtitles", "Subtitles"),
    ("notes", "Review notes"),
    ("report", "Readable reports"),
    ("plan", "Machine-readable plans"),
    ("log", "Logs"),
)


def render_index(package: ReviewPackage) -> str:
    """The review index, as Markdown."""
    lines: list[str] = []
    add = lines.append
    stats = package.stats()

    add(f"# Review — {package.run_id}")
    add("")
    add(f"*{package.created_at}*  ·  style **{package.style}**  ·  run "
        f"status **{package.run_status}**")
    add("")
    add(f"> {NOT_A_VERDICT}")
    add("")

    # -- 1. watch this ----------------------------------------------------
    add("## 1. Watch this")
    add("")
    if package.video_exists:
        add(f"**`{package.video}`**")
        add("")
        add(f"{package.video_duration:.0f} seconds, "
            f"{package.video_size_mb:.0f} MB.")
        subtitles = package.item("subtitles")
        if subtitles is not None and subtitles.exists:
            add("")
            add(f"Load `{subtitles.open_path}` beside it to see the captions "
                "— they are not burned into the video.")
    elif package.video:
        add("**There is no watchable file.** A render was attempted and what "
            "it left is not a video. See §4.")
    else:
        add("**Nothing was rendered.** This run produced plans only. To get a "
            "watchable proxy:")
        add("")
        add("```")
        add(f"python -m editing.cli render roughcut --run {package.run_id}")
        add("```")
    add("")
    for line in package.headline:
        add(f"- {line}")
    add("")

    # -- 2. what changed ---------------------------------------------------
    add("## 2. What changed")
    add("")
    for line in package.changed:
        add(f"- {line}")
    add("")

    # -- 3. what to watch for ---------------------------------------------
    add("## 3. What to watch for")
    add("")
    for line in package.watch_for:
        add(f"- {line}")
    add("")

    # -- 3b. the visual layer ----------------------------------------------
    visuals = package.visuals or {}
    if visuals.get("enabled"):
        add("## 3b. What the edit points at")
        add("")
        for entry in visuals.get("answers") or []:
            add(f"**{entry['question']}**")
            add("")
            add(entry["answer"])
            add("")
        # The sixth answer is already the first risk. Only list the rest,
        # so a plan with one concern does not print it twice.
        risks = list(visuals.get("overdone_risks") or [])[1:]
        if risks:
            add("The rest of what might be overdone:")
            add("")
            for line in risks:
                add(f"- {line}")
            add("")

        add("**Density**")
        add("")
        add(f"- {visuals.get('accepted', 0)} treatment(s) at "
            f"{visuals.get('effects_per_minute', 0):.2f} a minute "
            f"(ceiling {visuals.get('ceiling', 0):.2f}), "
            f"{visuals.get('callouts_per_minute', 0):.2f} callouts a minute")
        if visuals.get("by_family"):
            add("- by family: " + ", ".join(
                f"{count} {family}" for family, count in
                sorted(visuals["by_family"].items(), key=lambda kv: -kv[1])))
        if visuals.get("by_moment_kind"):
            add("- on: " + ", ".join(
                f"{count} {kind.replace('_', ' ')}" for kind, count in
                sorted(visuals["by_moment_kind"].items(),
                       key=lambda kv: -kv[1])[:8]))
        add(f"- {visuals.get('untreated_moments', 0)} of "
            f"{visuals.get('moments', 0)} moment(s) earned nothing")
        add("")

        preview = visuals.get("preview") or {}
        add("**What a proxy can and cannot show**")
        add("")
        add(f"- {preview.get('burnable', 0)} could be burned into a preview "
            "render — and none was")
        add(f"- {preview.get('sidecar_only', 0)} can only be a marker beside "
            "the video")
        add(f"- {preview.get('invisible', 0)} FFmpeg cannot show in any form")
        if preview.get("sidecar_path"):
            add(f"- markers: `{preview['sidecar_path']}`")
        add("")
        for line in (preview.get("limitations") or [])[:6]:
            add(f"  - {line}")
        if preview.get("limitations"):
            add("")
        add(f"> {preview.get('note', '')}")
        add("")

        checks = visuals.get("manual_checks") or []
        if checks:
            add("**Watch for, in the visual layer**")
            add("")
            for line in checks:
                add(f"- {line}")
            add("")

        add(f"> {visuals.get('not_rendered', '')}")
        add("")

    # -- 4. weak points ----------------------------------------------------
    add("## 4. Weak points")
    add("")
    checks = package.checks or {}
    for line in checks.get("summary") or []:
        add(f"**{line}**")
        add("")
    for line in package.weak_points:
        add(f"- {line}")
    add("")
    fixes = [
        entry for entry in
        (checks.get("failures") or []) + (checks.get("warnings") or [])
        if entry.get("fix")
    ]
    if fixes:
        add("Fixes the checks suggested:")
        add("")
        for entry in fixes[:12]:
            add(f"- `{entry['name']}` — {entry['fix']}")
        add("")

    # -- 5. decisions ------------------------------------------------------
    add("## 5. Needs you")
    add("")
    for line in package.decisions_needed:
        add(f"- {line}")
    add("")

    # -- files -------------------------------------------------------------
    add("## Files")
    add("")
    add(f"{stats['present']} of {stats['items']} are present.")
    add("")
    for kind, heading in KIND_HEADINGS:
        items = [item for item in package.of_kind(kind) if item.exists]
        if not items:
            continue
        add(f"### {heading}")
        add("")
        for item in items:
            add(f"- **{item.title}** — {item.note}")
            add(f"  `{item.open_path}`")
        add("")

    absent = [item for item in package.missing if item.kind != "video"]
    if absent:
        add("### Not produced by this run")
        add("")
        for item in absent:
            add(f"- {item.title}")
        add("")

    # -- warnings ----------------------------------------------------------
    if package.warnings:
        add(f"## Warnings ({len(package.warnings)})")
        add("")
        for warning in package.warnings[:30]:
            add(f"- {warning}")
        if len(package.warnings) > 30:
            add(f"- …and {len(package.warnings) - 30} more, in the run "
                "report.")
        add("")

    # -- commands ----------------------------------------------------------
    add("## Commands")
    add("")
    add("```")
    for command in package.commands:
        add(command)
    add("```")
    add("")
    return "\n".join(lines)


def render_summary(package: ReviewPackage) -> str:
    """The short form, for a terminal."""
    lines = [
        f"Review — {package.run_id}  [{package.run_status}]",
        f"  style   : {package.style}",
        f"  footage : {package.footage_folder or '(none)'}",
        f"  video   : "
        + (f"{package.video}  ({package.video_duration:.0f}s, "
           f"{package.video_size_mb:.0f} MB)"
           if package.video_exists else "(none)"),
        f"  checks  : {(package.checks or {}).get('status', 'not run')}",
        "",
    ]
    for heading, entries in (
        ("What changed", package.changed),
        ("Watch for", package.watch_for),
        ("Weak points", package.weak_points),
        ("Needs you", package.decisions_needed),
    ):
        lines.append(f"{heading}:")
        for line in entries[:6]:
            lines.append(f"  - {line[:150]}")
        if len(entries) > 6:
            lines.append(f"  ... and {len(entries) - 6} more.")
        lines.append("")

    index = package.item("run_report")
    lines.append(f"  Index : {package.folder}")
    if index is not None and index.exists:
        lines.append(f"  Report: {index.open_path}")
    return "\n".join(lines)

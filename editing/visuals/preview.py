"""What FFmpeg could and could not show of a visual plan.

The honest answer for this session is: **nothing is burned in**. The proxy
renderer encodes each segment and joins them with the concat demuxer, which is
what makes it survive a folder of mismatched game capture. Overlaying anything
would mean a second full re-encode of the joined file with a filtergraph — a
different render strategy, with its own failure modes, and one this session
does not build.

So what this module produces is three things:

1. a **capability statement** per treatment: could a preview render show this,
   with what filter, and if not, why not;
2. a **sidecar marker file** written beside the proxy, so a person watching it
   can see where each effect would land;
3. ``burned_in = False``, everywhere, with no code path that sets it True.

The filter fragments are recorded rather than run. They are there so that a
later session wiring a real preview render does not have to re-derive them, and
so a reader can see exactly what was and was not claimed. Writing a filter
string into a plan is not the same as running it, and this module is careful
never to blur the two.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from editing.visuals.execution import (
    FFmpegVisualPreviewPlan, PreviewItem,
)
from editing.visuals.schema import (
    PREVIEW_NOTE, VisualLayerPlan, VisualTreatment, now,
)

logger = logging.getLogger("nova.editing.visuals.preview")

#: Effect -> how well FFmpeg could show it in a preview render.
#:
#: ``burn_in``  a documented filter exists and would be clean on one segment
#: ``sidecar``  representable only as a marker beside the video
#: ``none``     FFmpeg has no way to express it at all
#:
#: "Clean" is doing real work in that first row. ``drawtext`` and ``drawbox``
#: over a single re-encoded segment are well-trodden; a keyframed scale ramp
#: across a concat boundary is not, and it is listed as ``sidecar`` for that
#: reason rather than because it is impossible in principle.
PREVIEW_SUPPORT_MAP = {
    # -- text and plates: drawtext / drawbox, per segment -------------------
    "title_card": ("burn_in", "drawbox + drawtext over a held frame"),
    "objective_card": ("burn_in", "drawbox + drawtext"),
    "chapter_card": ("burn_in", "drawbox + drawtext"),
    "recap_card": ("burn_in", "drawbox + drawtext"),
    "later_card": ("burn_in", "drawbox + drawtext"),
    "progress_card": ("burn_in", "drawbox + drawtext"),
    "setup_payoff_card": ("burn_in", "drawbox + drawtext"),
    "build_progress_card": ("burn_in", "drawbox + drawtext"),
    "label_tag": ("burn_in", "drawtext with a box"),
    "objective_label": ("burn_in", "drawtext with a box"),
    "danger_warning_label": ("burn_in", "drawtext with a box"),
    "hardcore_warning": ("burn_in", "drawtext with a box"),
    "totem_reminder": ("burn_in", "drawtext with a box"),
    "day_counter": ("burn_in", "drawtext with a box"),
    "coordinates_card": ("burn_in", "drawtext with a box"),
    "progression_counter": ("burn_in", "drawtext with a box"),
    "health_emphasis": ("burn_in", "drawtext with a box"),
    "villager_danger_meter": ("burn_in", "drawtext with a box"),
    # -- simple geometry ----------------------------------------------------
    "box_highlight": ("burn_in", "drawbox, unfilled"),
    "letterbox": ("burn_in", "two drawbox bars"),
    "impact_flash": ("burn_in", "a short white drawbox at high opacity"),
    "freeze_frame": ("burn_in", "a still segment held with -loop"),
    "freeze_frame_label": ("burn_in", "a held still plus drawtext"),
    # -- the picture, moving ------------------------------------------------
    "zoom_punch": ("sidecar",
                   "a keyframed scale needs zoompan across a concat boundary, "
                   "which is where this renderer's strategy stops being safe"),
    "quick_punch_in": ("sidecar", "same as zoom_punch"),
    "slow_zoom_hold": ("sidecar", "same as zoom_punch"),
    "crop_pan": ("sidecar", "same as zoom_punch"),
    "speed_ramp": ("sidecar",
                   "a variable rate needs setpts on a curve; the renderer "
                   "applies one flat rate per segment"),
    "screen_shake": ("sidecar",
                     "a per-frame position wobble has no single filter and "
                     "would need a generated expression"),
    # -- shapes FFmpeg has no primitive for ---------------------------------
    "arrow_callout": ("none",
                      "FFmpeg has no arrow primitive; drawing one means "
                      "generating an image first"),
    "circle_highlight": ("none",
                         "FFmpeg has no ellipse primitive; drawbox is the "
                         "only shape, and a box is not a circle"),
    "entity_callout": ("none",
                       "the target's position on screen is not known, so "
                       "there is nowhere to draw it"),
    # -- notes rather than pictures -----------------------------------------
    "replay_marker": ("sidecar", "a marker, by design"),
    "montage_marker": ("sidecar", "a marker, by design"),
    "dramatic_pause": ("sidecar", "a marker, by design"),
    "instant_replay": ("none",
                       "replaying footage means re-cutting the segment list, "
                       "which is a change to the cut and not an overlay"),
}


def support_for(effect: str) -> tuple:
    """``(support, reason)`` for one effect."""
    return PREVIEW_SUPPORT_MAP.get(
        effect, ("none", "this effect has no FFmpeg representation"))


def target_for(effect: str) -> str:
    """Where a treatment can end up, from the two capability maps.

    Premiere first: it is the path that can express the most, so an effect it
    can do is a ``premiere_plan`` item even when FFmpeg could also draw a
    rough version. An effect neither can do is a note, and says so.
    """
    from editing.visuals.premiere import can_express

    if can_express(effect):
        return "premiere_plan"
    if support_for(effect)[0] == "burn_in":
        return "ffmpeg_preview"
    return "placeholder_only"


def build_preview_plan(
    plan: VisualLayerPlan, *, name: str = "structure"
) -> FFmpegVisualPreviewPlan:
    """What a proxy render could show of this plan, and what it could not."""
    preview = FFmpegVisualPreviewPlan(
        name=name, generated_at=now(), burn_in_note=PREVIEW_NOTE)

    for treatment in plan.accepted:
        support, reason = support_for(treatment.effect)
        preview.items.append(PreviewItem(
            treatment_id=treatment.treatment_id,
            effect=treatment.effect,
            start=treatment.start,
            end=treatment.end,
            support=support,
            filter_fragment=(_fragment(treatment) if support == "burn_in"
                             else ""),
            reason="" if support == "burn_in" else reason,
            marker_text=_marker_text(treatment),
        ))

    burnable = len(preview.burnable)
    if burnable:
        preview.warnings.append(
            f"{burnable} treatment(s) could be burned into a preview render "
            "and were not: this system has no preview render, and the filter "
            "each one would need is recorded rather than run."
        )
    if preview.invisible:
        preview.warnings.append(
            f"{len(preview.invisible)} treatment(s) cannot be shown by FFmpeg "
            "in any form. They are in the marker file and nowhere else."
        )
    return preview


def _fragment(treatment: VisualTreatment) -> str:
    """The filter that *would* burn this in. Recorded, never run.

    Deliberately written as a real fragment rather than a description: a
    description would be a guess about feasibility, and a fragment is something
    a later session can paste into a command and find out.
    """
    start, end = treatment.start, treatment.end
    enable = f"between(t\\,{start:.3f}\\,{end:.3f})"
    text = str(treatment.payload.get("text") or "").replace("'", "")

    if treatment.effect == "letterbox":
        bars = float(treatment.payload.get("bars") or 0.11)
        return (f"drawbox=x=0:y=0:w=iw:h=ih*{bars:.3f}:color=black@1:t=fill:"
                f"enable='{enable}',"
                f"drawbox=x=0:y=ih*(1-{bars:.3f}):w=iw:h=ih*{bars:.3f}:"
                f"color=black@1:t=fill:enable='{enable}'")

    if treatment.effect == "impact_flash":
        opacity = float(treatment.payload.get("opacity") or 60.0) / 100.0
        return (f"drawbox=x=0:y=0:w=iw:h=ih:color=white@{opacity:.2f}:t=fill:"
                f"enable='{enable}'")

    if treatment.effect == "box_highlight":
        return (f"drawbox=x=iw*0.35:y=ih*0.35:w=iw*0.3:h=ih*0.3:"
                f"color=yellow@0.9:t=4:enable='{enable}'")

    if treatment.effect in ("freeze_frame", "freeze_frame_label"):
        held = float(treatment.payload.get("hold_at") or start)
        fragment = f"# a still of the frame at {held:.3f}s, held {treatment.duration:.2f}s"
        if text:
            fragment += (f" then drawtext=text='{text}':x=(w-tw)/2:"
                         f"y=h*0.82:fontsize=h/18:fontcolor=white:box=1:"
                         f"boxcolor=black@0.6:boxborderw=12")
        return fragment

    # Everything else in the burn-in set is a plate with words on it.
    subtitle = str(treatment.payload.get("subtitle") or "").replace("'", "")
    fragment = (
        f"drawbox=x=0:y=ih*0.38:w=iw:h=ih*0.24:color=black@0.55:t=fill:"
        f"enable='{enable}',"
        f"drawtext=text='{text}':x=(w-tw)/2:y=h*0.44:fontsize=h/14:"
        f"fontcolor=white:enable='{enable}'"
    )
    if subtitle:
        fragment += (f",drawtext=text='{subtitle}':x=(w-tw)/2:y=h*0.55:"
                     f"fontsize=h/28:fontcolor=white@0.8:enable='{enable}'")
    return fragment


def _marker_text(treatment: VisualTreatment) -> str:
    """The line this treatment contributes to the sidecar marker file."""
    detail = str(treatment.payload.get("text")
                 or treatment.payload.get("target")
                 or treatment.payload.get("note") or "")
    label = treatment.effect.replace("_", " ").upper()
    if detail:
        return f'{label}: "{detail[:70]}"'
    return f"{label} ({treatment.intensity})"


# ---------------------------------------------------------------------------
# The sidecar
# ---------------------------------------------------------------------------

def render_markers(plan: VisualLayerPlan,
                   preview: FFmpegVisualPreviewPlan) -> str:
    """The marker file, as Markdown.

    Written beside the proxy so a person watching it can see where each effect
    would land. Deliberately not a subtitle file: subtitles are what the
    caption pass writes, and a viewer loading both at once would see the
    episode's captions competing with a debug track.
    """
    lines: list[str] = []
    add = lines.append

    add(f"# Visual markers — {plan.name}")
    add("")
    add(f"*{plan.layer} layer, {plan.style} style, "
        f"{len(plan.accepted)} treatment(s)*")
    add("")
    add(f"> {PREVIEW_NOTE}")
    add("")

    if not plan.accepted:
        add("No visual treatment was planned for this cut.")
        add("")
        return "\n".join(lines)

    add("| time | effect | what | shown in the proxy |")
    add("|---|---|---|---|")
    by_id = {item.treatment_id: item for item in preview.items}
    for treatment in sorted(plan.accepted, key=lambda t: t.start):
        item = by_id.get(treatment.treatment_id)
        support = item.support if item else "none"
        shown = {
            "burn_in": "no — could be, and is not",
            "sidecar": "no — this line is the only sign of it",
            "none": "no — FFmpeg cannot show it at all",
        }[support]
        add(f"| {_stamp(treatment.start)} | `{treatment.effect}` | "
            f"{_marker_text(treatment)} | {shown} |")
    add("")

    add("## Why each one is there")
    add("")
    for treatment in sorted(plan.accepted, key=lambda t: t.start):
        add(f"- **{_stamp(treatment.start)} {treatment.effect}** — "
            f"{treatment.reason}")
        for note in treatment.safety_notes[:2]:
            add(f"  - {note}")
    add("")
    return "\n".join(lines)


def _stamp(seconds: float) -> str:
    total = max(0.0, float(seconds))
    minutes, secs = divmod(total, 60.0)
    return f"{int(minutes):02d}:{secs:05.2f}"


def write_markers(plan: VisualLayerPlan, preview: FFmpegVisualPreviewPlan,
                  path) -> Optional[Path]:
    """Write the marker file and record its location on the preview plan."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_markers(plan, preview), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - a sidecar is never worth failing
        logger.debug("Could not write visual markers at %s: %s", target, exc)
        return None
    preview.sidecar_path = str(target)
    return target


def markers_beside(plan: VisualLayerPlan, preview: FFmpegVisualPreviewPlan,
                   video_path: str) -> Optional[Path]:
    """Write the marker file next to a rendered video.

    Named after the video, so the two travel together. Returns None when there
    is nothing to write -- an empty marker file beside a proxy would suggest
    effects were tried and failed rather than that none were earned.
    """
    if not video_path or not plan.accepted:
        return None
    target = Path(video_path).with_suffix(".visuals.md")
    return write_markers(plan, preview, target)

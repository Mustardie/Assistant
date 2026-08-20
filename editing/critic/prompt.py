"""The prompt the critic is given for one review frame.

The analysis prompt in ``editing.visual.prompt`` asks *what is happening*. This
one asks a different question -- *what is wrong with this as an edit* -- and
the difference shapes everything about it:

* **The model is told what the cut did.** A frame in isolation cannot show that
  it is mid-punch-in, sped to 2x, or two frames before an outgoing cut. Without
  that, a critic either says nothing useful or invents problems. So every known
  edit at this moment is stated plainly in the prompt.
* **Finding nothing is a valid answer, and is made easy to give.** A critic
  rewarded for producing findings will produce findings. The shape puts
  ``"issues": []`` first in the instructions and says so explicitly, because
  most frames of a decent rough cut genuinely are fine.
* **The vocabulary is closed and shown in full.** Same reasoning as the
  analysis prompt: a model picking from a list is far more consistent than one
  corrected afterwards -- and here the coercion target matters more, because
  the issue type decides which automatic fix is even considered.
* **Confidence gates the fix.** The model is told, in the prompt, that a
  low-confidence finding becomes a note for a human rather than an edit. That
  is true (see ``editing.critic.revise``), and telling it is what makes
  "answer uncertain when uncertain" a useful instruction rather than a slogan.
"""
from __future__ import annotations

import json
from typing import Optional

from editing.critic.schema import FIXES, ISSUE_TYPES

#: Bumped when a change here would make a cached critic answer wrong. Part of
#: every critic cache key.
PROMPT_VERSION = 1

SYSTEM_PROMPT = (
    "You are a picture-quality critic for a Minecraft YouTube edit. You are "
    "shown ONE still frame taken from an automatically assembled rough cut, "
    "together with what the editing system knows about that moment. Your job "
    "is to say whether this frame is a problem, and only that. You do not "
    "describe the gameplay, you do not praise the edit, and you do not "
    "suggest creative improvements.\n\n"
    "Most frames are fine. Returning an empty issue list is the expected "
    "answer for a frame with nothing wrong, and is better than inventing a "
    "borderline complaint.\n\n"
    "Answer with a single JSON object and nothing else: no prose before or "
    "after, no markdown fence. Use only the issue and fix values you are "
    "given. If you are unsure whether something is wrong, report "
    "\"needs_human_review\" with your real confidence instead of guessing at "
    "a specific issue -- a finding below 0.6 confidence becomes a note for a "
    "person rather than an automatic edit, so an honest low number costs "
    "nothing and a confident wrong one changes the edit."
)

_RESPONSE_SHAPE = {
    "looks_ok": "true when you found nothing wrong, false otherwise",
    "issues": [
        {
            "issue": "one of the issue values",
            "severity": "low, medium or high",
            "confidence": "0.0 to 1.0 -- how sure you are this is real",
            "evidence": "what in THIS frame shows it, concretely",
            "suggested_fix": "one of the fix values",
            "notes": "anything the fix needs to know, or \"\"",
        }
    ],
    "notes": "one sentence about the frame overall, or \"\"",
}

_ISSUE_GUIDE = """\
What each issue means. Judge the picture, not the gameplay:

bad_crop                 The framing itself is wrong: the subject is half out
                         of frame, or the shot is composed on nothing.
hud_hidden               Health, hunger, hotbar or the item count is cropped
                         off the edge or covered. Only when the HUD should be
                         visible and is not.
action_hidden            The thing the moment is about -- the mob, the item,
                         the player's action -- is out of frame or obscured.
zoom_too_strong          The frame is scaled up far enough to soften the image
                         or push content off the edges.
text_unreadable          On-screen text is too small, too low-contrast or too
                         blurred to read at a glance.
text_placed_badly        Text is readable but sits somewhere it should not:
                         over the HUD, over the crosshair, or off-balance.
caption_covers_gameplay  A caption or subtitle sits on top of what the viewer
                         needs to see.
too_dark                 Detail is lost in shadow; a viewer cannot tell what is
                         there. Night-time footage that still reads clearly is
                         NOT too dark.
too_bright               Highlights are blown out and detail is lost.
boring_too_long          Nothing is happening in this frame and the context
                         says the stretch is long. Use the clip length given
                         below rather than guessing.
cut_too_early            This is the last frame of a clip and the action is
                         plainly mid-motion, unresolved.
cut_too_late             This is the last frame of a clip and the moment
                         finished a while ago: the frame is dead air.
marker_mismatch          A marker at this moment claims something the picture
                         does not show.
callout_needed           There is a thing the viewer needs pointed out and no
                         callout is planned here.
hold_longer              A strong moment is passing too fast for it to land.
remove_edit              An edit applied here is actively making the frame
                         worse and should not be there at all.
needs_human_review       Something is off but you cannot name it, or you
                         cannot judge from a still.
"""

_RULES = """\
Rules that override your instincts:

* Judge only what this frame can show. Timing questions (too early, too late,
  too long) are answerable ONLY from the context block below; if the context
  does not support the claim, do not make it.
* A dark cave, a night scene or a nether biome is not "too_dark". Only call it
  that when detail a viewer needs is genuinely lost.
* An open inventory, chest or crafting screen is normal Minecraft footage, not
  a bad crop. It is only a problem if an edit is covering or cropping it.
* If no zoom is listed in the context, do not report zoom_too_strong. You
  cannot tell scale from a single frame without being told.
* If no text, caption or callout is listed in the context, do not report a text
  or caption issue. Nothing has been placed yet.
* Report at most three issues. If there are more, report the worst three.
"""


def build_user_prompt(frame, *, sequence_name: str = "") -> str:
    """The per-frame user prompt, with the moment's full context inlined.

    ``frame`` is a ``roughcut.review.ReviewFrame``. Read duck-typed rather than
    imported for typing so this module stays importable without pulling the
    rough cut package in.
    """
    lines = [
        f"Frame {getattr(frame, 'frame_id', '?')} from the rough cut"
        + (f" '{sequence_name}'" if sequence_name else "") + ".",
        "",
        "Context for this moment:",
    ]
    lines.extend(context_lines(frame))
    lines.extend([
        "",
        "Allowed issue values -- use these exact strings:",
        "  " + ", ".join(ISSUE_TYPES),
        "",
        "Allowed suggested_fix values -- use these exact strings:",
        "  " + ", ".join(FIXES),
        "",
        _ISSUE_GUIDE,
        "",
        _RULES,
        "",
        "Return exactly this JSON shape:",
        json.dumps(_RESPONSE_SHAPE, indent=2),
    ])
    return "\n".join(lines)


def context_lines(frame) -> list[str]:
    """The context block, as lines. Shared with the report so both agree.

    Only facts go in here. Where the pipeline does not know something -- no
    transcript, no audio pass, no timeline -- the line is simply absent, rather
    than present and empty. An empty field reads to a model as an assertion
    that there is nothing there, which is a different claim from not knowing.
    """
    out: list[str] = []
    add = out.append

    why = getattr(frame, "reason", "") or getattr(frame, "frame_kind", "")
    add(f"  Why this frame was picked: {why}")
    add(f"  Position in the cut: {getattr(frame, 'sequence_time', 0.0):.2f}s")

    kind = getattr(frame, "frame_kind", "")
    if kind == "clip_end":
        add("  This is the LAST part of a clip -- the next thing the viewer "
            "sees is a cut.")
    elif kind == "clip_start":
        add("  This is the FIRST part of a clip -- the viewer just cut to it.")

    keep_reason = getattr(frame, "keep_reason", "")
    if keep_reason and keep_reason != "unknown":
        add(f"  Why the clip was kept: {keep_reason}")

    duration = _clip_duration(frame)
    if duration:
        add(f"  This clip runs {duration:.1f}s on the timeline.")

    edits = list(getattr(frame, "applied_edits", []) or [])
    if edits:
        add("  Edits applied at this moment:")
        for edit in edits:
            detail = edit.get("detail") or ""
            add(f"    - {edit.get('kind')}: {detail}".rstrip(": "))
    else:
        add("  No edit has been applied at this moment -- this is the raw "
            "footage as cut.")

    markers = list(getattr(frame, "marker_names", []) or [])
    if markers:
        add(f"  Markers inside this clip: {', '.join(markers)}")

    importance = getattr(frame, "importance", "")
    if importance:
        add(f"  The analysis pass rated this moment: {importance}")

    environment = getattr(frame, "environment", "")
    actions = list(getattr(frame, "actions", []) or [])
    if environment or actions:
        add(f"  Analysis saw: {environment or 'unknown environment'}"
            + (f", {', '.join(actions)}" if actions else ""))

    entities = list(getattr(frame, "entities", []) or [])
    if entities:
        add(f"  Entities expected on screen: {', '.join(entities)}")
    threats = list(getattr(frame, "threats", []) or [])
    if threats:
        add(f"  Active threats: {', '.join(threats)}")

    ui_flags = list(getattr(frame, "ui_flags", []) or [])
    if ui_flags:
        add(f"  HUD state from the analysis pass: {', '.join(ui_flags)}")

    transcript = getattr(frame, "transcript", "")
    if transcript:
        add(f"  Being said here: \"{transcript[:300]}\"")

    audio_types = list(getattr(frame, "audio_types", []) or [])
    if audio_types:
        add(f"  Audio around this moment: {', '.join(audio_types)}")

    if getattr(frame, "protected", False):
        add("  This clip is a protected hold: the planner decided to leave the "
            "footage alone here.")

    speed = float(getattr(frame, "speed", 1.0) or 1.0)
    if speed != 1.0:
        add(f"  The clip is retimed to {speed:g}x, so motion reads faster than "
            "it was shot.")

    return out


def _clip_duration(frame) -> Optional[float]:
    """Clip length on the timeline, when the frame carries enough to say."""
    for name in ("clip_duration", "sequence_duration"):
        value = getattr(frame, name, None)
        if value:
            return float(value)
    edits = getattr(frame, "applied_edits", None) or []
    for edit in edits:
        if edit.get("kind") == "clip" and edit.get("duration"):
            return float(edit["duration"])
    return None

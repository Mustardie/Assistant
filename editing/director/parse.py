"""Turning what the model said into decisions the system can check.

This is the boundary between "a language model produced some text" and
"the system holds a record". Everything on this side of it is untrusted, and
the job here is to convert what can be converted, discard what cannot with a
reason, and never invent anything.

## Resolution, not trust

The important function is ``_resolve``. A decision arrives naming segment IDs;
its times come from **the context's segments**, not from any number the model
wrote. That single choice removes a whole class of failure:

* a hallucinated segment ID resolves to nothing -> discarded, with a reason
* a range that drifted by twelve seconds cannot happen, because the model does
  not supply the range
* ``shorten`` is the one action that takes numbers from the model, and they
  are clamped inside the resolved range before anything sees them

## Repair, within limits

Models produce near-JSON. A markdown fence, a trailing sentence, ``'`` instead
of ``"``, a decision whose ``segment_ids`` is a bare string rather than a
list. Those are formatting accidents and are repaired -- the answer is still
the model's.

What is never repaired is *meaning*. An unknown action is not guessed at, an
absent reason is not written for it, and a response with no usable decisions
produces a failure rather than an empty plan that looks like a considered
decision to keep everything.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from editing.errors import ModelError
from editing.director.schema import (
    ACTIONS, SINGLE_CHANNEL_CAP, ContextSegment, DirectorConfig,
    DirectorContext, DirectorDecision, DirectorReason, decision_id_for,
)
from editing.visual.qwen import extract_json

logger = logging.getLogger("nova.editing.director.parse")

#: Keys a model might use for the decision list, in the order tried. The first
#: is what the prompt asks for; the rest are what models actually send.
DECISION_KEYS = ("decisions", "edits", "cuts", "plan", "results", "items")

#: Trailing commas: the single most common way a long JSON answer breaks.
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def parse_response(
    text: str,
    context: DirectorContext,
    *,
    config: Optional[DirectorConfig] = None,
) -> tuple:
    """``(decisions, approach, discarded, warnings)`` from a model answer.

    Raises ``ModelError`` when the answer is not usable at all -- an empty
    response, prose with no JSON in it, or JSON with no decision list. Those
    are different failures with different fixes, and the caller turns each
    into a typed ``DirectorFailure``.
    """
    config = (config or DirectorConfig()).validated()
    payload = _load(text)

    raw = None
    for key in DECISION_KEYS:
        if isinstance(payload.get(key), list):
            raw = payload[key]
            break
    if raw is None:
        # A model that returned a bare list rather than an object.
        raise ModelError(
            "The director's answer had no decision list in it",
            hint="The model returned JSON without a 'decisions' array. Lower "
                 "the temperature, or check the served model follows "
                 "instructions.",
            detail={"keys": sorted(str(k) for k in payload)[:20]},
        )

    decisions: list[DirectorDecision] = []
    discarded: list[dict] = []
    warnings: list[str] = []
    seen: set = set()

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            discarded.append({
                "index": index,
                "why": "not an object",
                "raw": str(item)[:200],
            })
            continue
        decision, problem = _one(item, context, config)
        if decision is None:
            discarded.append({"index": index, "why": problem,
                              "raw": json.dumps(item, default=str)[:300]})
            continue
        if decision.decision_id in seen:
            # The same call twice. Keeping both would double-count it against
            # every ceiling the safety pass measures.
            discarded.append({
                "index": index,
                "why": f"duplicate of {decision.decision_id}",
                "raw": json.dumps(item, default=str)[:200],
            })
            continue
        seen.add(decision.decision_id)
        decisions.append(decision)

    if not decisions:
        raise ModelError(
            "The director produced no usable decisions",
            hint="Every decision was discarded. The most common cause is "
                 "invented segment ids; `director show-rejected` lists what "
                 "was thrown away and why.",
            detail={"discarded": discarded[:10]},
        )

    if discarded:
        warnings.append(
            f"{len(discarded)} decision(s) from the model could not be used. "
            "See the discarded list for each reason."
        )
    invented = sum(1 for item in discarded
                   if "no known segment" in str(item.get("why", "")))
    if invented:
        warnings.append(
            f"{invented} decision(s) named segment ids that do not exist. The "
            "model is inventing ranges; lower the temperature."
        )

    approach = str(payload.get("approach")
                   or payload.get("summary") or "")[:2000]
    return decisions, approach, discarded, warnings


def _load(text: str) -> dict:
    """The JSON object out of a model answer, repaired where it is safe to."""
    if not isinstance(text, str) or not text.strip():
        raise ModelError(
            "The director model returned an empty response",
            hint="Check the model server is running and did not run out of "
                 "context.",
        )
    try:
        return extract_json(text)
    except ModelError:
        # One repair pass, for the failure that is always formatting: a
        # trailing comma somewhere in a 200-decision array.
        repaired = _TRAILING_COMMA.sub(r"\1", text)
        if repaired != text:
            try:
                payload = extract_json(repaired)
                logger.debug("Repaired a trailing comma in the director's JSON")
                return payload
            except ModelError:
                pass
        raise


def _one(
    item: dict, context: DirectorContext, config: DirectorConfig
) -> tuple:
    """One raw decision, resolved. ``(decision, None)`` or ``(None, why)``."""
    action = str(item.get("action") or "").strip().lower().replace("-", "_")
    if action not in ACTIONS:
        return None, f"unknown action {action!r}"

    ids = _as_ids(item.get("segment_ids") or item.get("segment_id")
                  or item.get("ids"))
    if not ids:
        return None, "no segment ids"

    resolved = [context.segment(segment_id) for segment_id in ids]
    known = [entry for entry in resolved if entry is not None]
    if not known:
        return None, f"no known segment among {ids[:5]}"

    unknown = [
        ids[index] for index, entry in enumerate(resolved) if entry is None
    ]
    # One asset per decision. A range spanning two files is not a range.
    asset_id = known[0].asset_id
    same_asset = [entry for entry in known if entry.asset_id == asset_id]
    crossed = len(same_asset) != len(known)

    start = min(entry.start for entry in same_asset)
    end = max(entry.end for entry in same_asset)
    decision = DirectorDecision(
        decision_id=decision_id_for(
            action, [entry.segment_id for entry in same_asset]),
        action=action,
        segment_ids=[entry.segment_id for entry in same_asset],
        asset_id=asset_id,
        source_file=same_asset[0].source_file,
        start=start,
        end=end,
        out_start=start,
        out_end=end,
        confidence=_float(item.get("confidence"), 0.5),
        priority=_float(item.get("priority"), 0.5),
        reason=DirectorReason.from_dict(item.get("reason")),
        evidence=_as_ids(item.get("evidence"), limit=40),
        viewer_effect=_effect(item.get("viewer_effect")),
        beat_id=_str(item.get("beat_id")),
        open_loop_id=_str(item.get("open_loop_id")),
        setup_id=_str(item.get("setup_id")),
        payoff_id=_str(item.get("payoff_id")),
        suggestion_id=_str(item.get("suggestion_id")),
        recommendation_ids=_as_ids(item.get("recommendation_ids"), limit=20),
        origin="model",
        order=int(_float(item.get("order"), 100.0)),
    )

    _apply_speed(decision, item, config)
    _apply_shorten(decision, item, same_asset)
    _cap_confidence(decision, same_asset)

    if unknown:
        decision.safety_notes.append(
            "ignored unknown segment id(s): " + ", ".join(unknown[:5]))
    if crossed:
        decision.safety_notes.append(
            "spanned more than one source file; only the first file's "
            "segments were used")
    return decision, None


def _apply_speed(
    decision: DirectorDecision, item: dict, config: DirectorConfig
) -> None:
    """The retime, if the action asks for one.

    A ``speed_up`` with no speed gets the configured default rather than 1x:
    a decision that says "speed this up" and then does not is worse than one
    that picks a sane number and records that it did.
    """
    if decision.action != "speed_up":
        decision.speed = 1.0
        return
    given = _float(item.get("speed") or item.get("rate"), 0.0)
    if given <= 0:
        decision.speed = config.default_speed
        decision.safety_notes.append(
            f"no speed given; used the default {config.default_speed:g}x")
    else:
        decision.speed = given


def _apply_shorten(
    decision: DirectorDecision, item: dict, segments: list
) -> None:
    """The sub-range for ``shorten``, clamped inside the resolved range.

    The only place a number from the model becomes a time, and it is bounded
    on both sides by the segments the decision named -- so the worst a wrong
    number can do is choose a different part of footage that genuinely exists.
    """
    if decision.action != "shorten":
        return
    out_start = _float(item.get("out_start"), decision.start)
    out_end = _float(item.get("out_end"), decision.end)

    # Some models answer with an offset into the range rather than an absolute
    # source time. Both are unambiguous as long as we check.
    if 0.0 <= out_start < decision.duration and out_end <= decision.duration \
            and out_end > out_start and decision.start > 0:
        out_start += decision.start
        out_end += decision.start
        decision.safety_notes.append(
            "read out_start/out_end as offsets into the range")

    clamped_start = max(decision.start, min(out_start, decision.end))
    clamped_end = max(clamped_start, min(out_end, decision.end))
    if clamped_end - clamped_start <= 0.05:
        # A shorten to nothing is a cut, and saying so is more honest than
        # silently producing a zero-length clip.
        decision.action = "cut"
        decision.safety_notes.append(
            "the requested sub-range was empty; read as a cut")
        decision.out_start = decision.start
        decision.out_end = decision.end
        return
    if (clamped_start, clamped_end) != (out_start, out_end):
        decision.safety_notes.append(
            "the requested sub-range was clamped inside the named segments")
    decision.out_start = clamped_start
    decision.out_end = clamped_end


def _cap_confidence(decision: DirectorDecision, segments: list) -> None:
    """Apply the single-channel ceiling to a model's self-reported confidence.

    Session 8's rule, and it belongs here for the same reason: a judgement
    resting on one channel cannot be as certain as one where two agreed. The
    model has no way to know how many channels it read, so the cap is applied
    from the *context*, which does.
    """
    channels = 0
    if any(entry.said for entry in segments):
        channels += 1
    if any(entry.audio for entry in segments):
        channels += 1
    if any(entry.importance not in ("", "unknown") for entry in segments):
        channels += 1
    if channels <= 1 and decision.confidence > SINGLE_CHANNEL_CAP:
        decision.safety_notes.append(
            f"confidence capped at {SINGLE_CHANNEL_CAP} -- only one channel "
            "of evidence covers this range"
        )
        decision.confidence = SINGLE_CHANNEL_CAP


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------

def _as_ids(value: Any, limit: int = 80) -> list[str]:
    """A list of ids, from whatever shape the model used.

    A bare string is one id, not a list of characters -- which is the bug this
    function exists to prevent, and the one that would otherwise produce
    eleven decisions about segments named ``s``, ``e``, ``g``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
        return [part for part in parts if part][:limit]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                found = item.get("id") or item.get("segment_id")
                if found:
                    out.append(str(found).strip())
            elif item is not None:
                out.append(str(item).strip())
        return [item for item in out if item][:limit]
    return [str(value).strip()][:limit]


def _float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or abs(number) == float("inf"):
        return default
    return number


def _str(value: Any, limit: int = 60) -> str:
    return str(value or "").strip()[:limit]


def _effect(value: Any) -> str:
    from editing.director.schema import VIEWER_EFFECTS

    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return token if token in VIEWER_EFFECTS else "none_stated"

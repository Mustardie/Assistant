"""Skill parser: raw capture -> semantic actions.

Takes the recorder's raw event stream and produces a reusable timeline:

    raw events            -> grouped into actions (clicks, typing, hotkeys,
                             scrolling, window switches)
    + vision UI frames    -> every action references a UI OBJECT ("Login
                             button") with coordinates only as fallback
    + LLM (when available)-> high-level step descriptions

Also responsible for:
    - variable detection (Phase 3): typed values become {{Variable}} steps
    - secret masking (Safety): passwords/OTPs/card numbers/tokens are never
      written to disk -- not even in the raw event log
"""

import logging
import re
from pathlib import Path

from skills.matcher import element_at
from skills.vision import UIObject

logger = logging.getLogger(__name__)

KEYBOARD_MODIFIERS = {
    "ctrl", "left ctrl", "right ctrl", "alt", "left alt", "right alt",
    "shift", "left shift", "right shift", "win", "left win", "right win",
    "cmd", "left cmd", "right cmd", "alt gr",
}

_HOTKEY_MODIFIERS = KEYBOARD_MODIFIERS - {"shift", "left shift", "right shift"}

# keyboard-lib event names -> printable characters (US layout + common extras).
_NAMED_CHARS = {
    "space": " ", "period": ".", "comma": ",", "slash": "/", "backslash": "\\",
    "minus": "-", "equals": "=", "equal": "=", "semicolon": ";",
    "apostrophe": "'", "bracket left": "[", "bracket right": "]",
    "grave": "`", "backquote": "`", "decimal point": ".",
    "num divide": "/", "num multiply": "*", "num minus": "-", "num plus": "+",
    "num comma": ",", "num period": ".",
}

_SHIFTED = {
    "`": "~", "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^",
    "7": "&", "8": "*", "9": "(", "0": ")", "-": "_", "=": "+", "[": "{",
    "]": "}", "\\": "|", ";": ":", "'": '"', ",": "<", ".": ">", "/": "?",
}

_SECRET_PATTERNS = {
    "card": re.compile(r"^(?:\d{4}[ -]?){3}\d{4}$|^(?:\d{4}[ -]?){2}\d{4,}$"),
    "otp": re.compile(r"^\d{4,8}$"),
    "token": re.compile(r"^[A-Za-z0-9_\-]{32,}$"),
    "date": re.compile(
        r"^(?:\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}[-/.]\d{1,2}[-/.]\d{1,4})$"
    ),
}

_PASSWORD_FIELD_HINTS = ("password", "passwd", "pwd", "secret", "token", "pin", "otp",
                         "verification code", "2fa", "auth code", "authentication")

_NAVIGATION_FIXED = {
    "https://www.google.com", "https://google.com", "https://mail.google.com",
    "https://gmail.com", "https://www.youtube.com", "https://youtube.com",
    "https://chat.openai.com", "https://chatgpt.com", "https://docs.google.com",
    "https://github.com", "https://www.bing.com", "https://duckduckgo.com",
}

_TYPING_IDLE_GAP = 1.5        # seconds of quiet -> typing burst ends
_CLICK_FIELD_WINDOW = 4.0     # a click that long ago is still this typing's field
_WAIT_THRESHOLD = 2.0         # gap >= this becomes an explicit wait step
_MAX_TYPING_CHARS = 200


# --------------------------------------------------------------------- #
# Value classification (variables + secrets)
# --------------------------------------------------------------------- #

def classify_value(text: str, field_label: str = "") -> str | None:
    """Return the variable type for a typed value, or None if it should
    stay a literal. Types: text, number, date, path, folder, url,
    password (secret)."""
    value = (text or "").strip()
    if not value:
        return None
    label = (field_label or "").lower()

    if any(hint in label for hint in _PASSWORD_FIELD_HINTS):
        return "password"
    if _SECRET_PATTERNS["card"].match(value):
        return "password"
    if _SECRET_PATTERNS["token"].match(value) and len(value) >= 32:
        return "password"
    if _SECRET_PATTERNS["otp"].match(value) and not label:
        return "password"
    if _looks_like_strong_secret(value, label):
        return "password"

    if re.match(r"^https?://", value, re.IGNORECASE) or value.lower().startswith("www."):
        if value.lower() in _NAVIGATION_FIXED:
            return None
        return "url"
    if _SECRET_PATTERNS["date"].match(value):
        return "date"
    if "\\" in value or ("/" in value and not value.startswith(("http", "www"))):
        path = Path(value)
        if path.suffix:
            return "path"
        if path.exists() or value.endswith(("/", "\\")):
            return "folder"
        if "\\" in value:
            return "folder"  # Windows path without an extension
    if re.match(r"^[-+]?\d+(?:[.,]\d+)?$", value):
        return "number"
    if _SECRET_PATTERNS["otp"].match(value) and label:
        return "password"
    return "text"


def _looks_like_strong_secret(value: str, label: str) -> bool:
    """Heuristic: long mixed-case alphanumerics with symbols, or a
    short numeric code typed right after a 'code' field label."""
    if len(value) >= 8 and re.search(r"[a-z]", value) and re.search(r"[A-Z]", value) \
            and re.search(r"\d", value) and re.search(r"[^A-Za-z0-9]", value):
        return True
    return False


def is_secret(value: str, label: str = "") -> bool:
    return classify_value(value, label) == "password"


def mask_text(value: str) -> str:
    return "•" * max(6, min(len(value), 20))


def suggest_variable_name(label: str, value: str, index: int, var_type: str = "text") -> str:
    words = re.findall(r"[A-Za-z0-9]+", label or "")
    words = [w for w in words if w.lower() not in _PASSWORD_FIELD_HINTS]
    if words:
        return "".join(w[:1].upper() + w[1:] for w in words)
    # No usable field label (vision couldn't read it, or the click->type
    # window missed it). Fall back to a name derived from the value's
    # detected type ("Date", "Url", ...) instead of a generic "Value1" --
    # that generic name otherwise leaks verbatim into the playback
    # question ("What should the Value1 be?"), which sounds broken.
    type_names = {
        "number": "Number", "date": "Date", "url": "URL",
        "path": "File", "folder": "Folder", "text": "Text",
    }
    base = type_names.get(var_type, "Value")
    return base if index == 0 else f"{base}{index + 1}"


# --------------------------------------------------------------------- #
# Event grouping
# --------------------------------------------------------------------- #

def _key_to_char(name: str, shift: bool) -> str | None:
    base = (name or "").strip()
    if len(base) == 1:
        char = base
    elif base in _NAMED_CHARS:
        char = _NAMED_CHARS[base]
    elif len(base) > 1 and base.lower().isalpha():
        # Some layouts report named letters for non-ASCII keys.
        char = base.lower()
    else:
        return None
    if shift:
        if char.isalpha():
            return char.upper()
        return _SHIFTED.get(char, char)
    return char


def _flush_typing(typing: list, shift_pressed: bool) -> str:
    if not typing:
        return ""
    text = "".join(_key_to_char(name, shift) for name, shift in typing)
    return text[: _MAX_TYPING_CHARS]


def group_actions(events: list[dict]) -> list[dict]:
    """Collapse the raw event stream into ordered, higher-level actions:
    click / double_click / type / press / hotkey / scroll / window."""
    actions: list[dict] = []
    typing: list = []          # (key name, shift) pending characters
    typing_start_ts = 0.0
    shift_pressed = False
    last_click = None
    last_click_ts = 0.0
    last_move = None
    index = 0
    total = len(events)
    time_now = 0.0
    last_typing_ts = 0.0

    def flush() -> None:
        nonlocal typing
        text = _flush_typing(typing, shift_pressed)
        typing = []
        if not text:
            return
        anchor = last_click if (last_click and last_click_ts and
                                time_now - last_click_ts <= _CLICK_FIELD_WINDOW) else last_move
        actions.append({
            "kind": "type", "text": text, "t": typing_start_ts,
            "x": anchor[0] if anchor else None, "y": anchor[1] if anchor else None,
        })

    while index < total:
        event = events[index]
        time_now = event.get("t", time_now)
        kind = event.get("type")
        index += 1

        if kind == "mouse_move":
            last_move = (event.get("x"), event.get("y"))
            continue

        if kind in ("pause", "resume"):
            flush()
            continue

        if kind == "window":
            if actions and actions[-1].get("kind") == "window" and \
                    actions[-1].get("title") == event.get("title"):
                continue  # dedupe consecutive same-window events
            flush()
            actions.append({
                "kind": "window", "t": time_now,
                "title": event.get("title", ""), "app": event.get("app", ""),
            })
            continue

        if kind == "scroll":
            flush()
            actions.append({
                "kind": "scroll", "t": time_now, "amount": event.get("amount", 0),
            })
            continue

        if kind == "click":
            flush()
            point = (event.get("x"), event.get("y"))
            button = event.get("button", "left")
            count = 1
            end_ts = event.get("t", time_now)
            while index < total and events[index].get("type") == "click" and \
                    events[index].get("button") == button and \
                    events[index].get("group") == event.get("group"):
                end_ts = events[index].get("t", end_ts)
                count += 1
                index += 1
            click_kind = "double_click" if count >= 2 and end_ts - event.get("t", end_ts) < 0.5 \
                else "click"
            actions.append({
                "kind": click_kind, "button": button,
                "x": point[0], "y": point[1], "count": count, "t": event.get("t", time_now),
            })
            last_click = point
            last_click_ts = event.get("t", time_now)
            continue

        if kind != "key":
            continue

        name = event.get("name", "")
        action = event.get("action", "down")
        lower = name.lower()

        if action == "up":
            if lower in ("shift", "left shift", "right shift"):
                shift_pressed = False
            continue

        if lower in KEYBOARD_MODIFIERS:
            if lower in ("shift", "left shift", "right shift"):
                shift_pressed = True
                continue
            # Look ahead for a hotkey combo: modifier + key within 1.2s.
            if index < total:
                nxt = events[index]
                if nxt.get("type") == "key" and nxt.get("action") == "down" and \
                        nxt.get("name", "").lower() not in KEYBOARD_MODIFIERS and \
                        nxt.get("t", time_now) - event.get("t", time_now) <= 1.2:
                    flush()
                    combo = [lower.replace("left ", "").replace("right ", "")]
                    key_name = nxt.get("name", "").lower()
                    combo.append(key_name.replace("left ", "").replace("right ", ""))
                    actions.append({
                        "kind": "hotkey", "keys": combo, "t": nxt.get("t", time_now),
                    })
                    index += 1
                    continue
            # Modifier pressed alone (e.g. ctrl released by itself) -- ignore.
            continue

        if lower in ("enter", "tab", "esc"):
            flush()
            actions.append({"kind": "press", "key": lower, "t": time_now})
            continue

        if lower in ("backspace", "delete", "del"):
            if typing:
                typing.pop()
            continue

        char = _key_to_char(name, shift_pressed)
        if char is None:
            continue
        # A long quiet gap ends the previous typing burst.
        if typing and time_now - last_typing_ts > _TYPING_IDLE_GAP:
            flush()
        if not typing:
            typing_start_ts = time_now
        typing.append((name, shift_pressed))
        last_typing_ts = time_now

    flush()
    return actions


# --------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------- #

def _resolve_ui_object(action: dict, frames: dict, scale: float) -> dict | None:
    """Attach the UI object under the action's point, using the vision
    frame closest in time. Click coordinates are logical (pyautogui);
    frames are physical pixels (mss) -- convert before the hit test."""
    if action.get("x") is None or action.get("y") is None:
        return None
    frame = closest_frame(frames, action.get("t", 0))
    if not frame:
        return None
    physical = (action["x"] * scale, action["y"] * scale)
    obj = element_at(frame.get("objects") or [], physical)
    if obj:
        return {"text": obj.get("text", ""), "type": obj.get("type", "button")}
    return None


def closest_frame(frames: dict, ts: float, window: float = 3.0) -> dict | None:
    best, best_delta = None, None
    for key, frame in (frames or {}).items():
        try:
            delta = abs(float(key) - ts)
        except (TypeError, ValueError):
            continue
        if delta <= window and (best_delta is None or delta < best_delta):
            best_delta, best = delta, frame
    return best


def build_steps(actions: list[dict], frames: dict, screen: dict | None) -> list[dict]:
    """Convert grouped actions into timeline steps with variables and
    semantic targets. Returns (steps, variables)."""
    steps: list[dict] = []
    variables: list[dict] = []
    previous_end_ts = None
    scale = (screen or {}).get("scale") or 1.0
    frame_w = None
    frame_h = None
    for _, frame in (frames or {}).items():
        if frame.get("width") and frame.get("height"):
            frame_w, frame_h = frame["width"], frame["height"]
            break

    click_target = None
    click_target_ts = 0.0

    for index, action in enumerate(actions):
        kind = action["kind"]
        ts = action.get("t", 0)

        if kind == "window":
            steps.append({
                "type": "ensure_app", "window": {"title": action.get("title", ""),
                                                 "app": action.get("app", "")},
                "t": ts,
            })
            continue

        # Insert an explicit wait when the user paused between actions.
        if previous_end_ts is not None and ts - previous_end_ts >= _WAIT_THRESHOLD:
            steps.append({
                "type": "wait", "seconds": round(ts - previous_end_ts, 1), "t": ts,
            })
        previous_end_ts = ts

        if kind in ("click", "double_click", "right_click"):
            step_type = kind
            obj = _resolve_ui_object(action, frames, scale)
            label = obj["text"] if obj and obj["text"] else ""
            relative = None
            if action.get("x") is not None and frame_w and frame_h:
                relative = [
                    round(action["x"] * scale / frame_w, 4),
                    round(action["y"] * scale / frame_h, 4),
                ]
            steps.append({
                "type": step_type,
                "button": action.get("button", "left"),
                "target": label,
                "ui_object": obj or {"text": "", "type": "button"},
                "coords": [action["x"], action["y"]] if action.get("x") is not None else None,
                "relative": relative,
                "t": ts,
            })
            click_target = obj
            click_target_ts = ts
            continue

        if kind == "type":
            raw_text = action["text"]
            field_label = ""
            if click_target and ts - click_target_ts <= _CLICK_FIELD_WINDOW:
                field_label = click_target.get("text", "") if click_target else ""
            var_type = classify_value(raw_text, field_label)
            if var_type == "password":
                name = "Password"
                steps.append({
                    "type": "type", "text": "{{Password}}", "is_variable": True,
                    "variable": {"name": name, "type": "password"},
                    "target": field_label or "password field", "masked": True, "t": ts,
                })
                variables.append({
                    "name": name, "type": "password", "step": len(steps) - 1,
                    "example": mask_text(raw_text),
                })
                continue
            if var_type is not None:
                name = suggest_variable_name(field_label, raw_text, len(variables), var_type)
                steps.append({
                    "type": "type", "text": f"{{{{{name}}}}}", "is_variable": True,
                    "variable": {"name": name, "type": var_type},
                    "target": field_label or "text field", "t": ts,
                })
                variables.append({
                    "name": name, "type": var_type, "step": len(steps) - 1,
                    "example": raw_text,
                })
                continue
            steps.append({
                "type": "type", "text": raw_text, "is_variable": False,
                "target": field_label or "text field", "t": ts,
            })
            continue

        if kind == "press":
            steps.append({"type": "press", "key": action["key"], "t": ts})
            continue

        if kind == "hotkey":
            steps.append({"type": "hotkey", "keys": list(action["keys"]), "t": ts})
            continue

        if kind == "scroll":
            steps.append({"type": "scroll", "amount": int(action["amount"]), "t": ts})
            continue

    return steps, variables


# --------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------- #

def describe_steps(steps: list[dict]) -> list[str]:
    lines = []
    for step in steps:
        stype = step["type"]
        if stype == "ensure_app":
            app = (step.get("window") or {}).get("app") or (step.get("window") or {}).get("title")
            lines.append(f"Open or focus {app or 'the application'}")
        elif stype in ("click", "double_click", "right_click"):
            target = step.get("target") or "an element"
            lines.append(f"{stype.replace('_', ' ')} {target}")
        elif stype == "type":
            lines.append(f"Type {'the variable ' + step.get('text', '') if step.get('is_variable') else repr(step.get('text', ''))}")
        elif stype == "press":
            lines.append(f"Press {step.get('key', '')}")
        elif stype == "hotkey":
            lines.append(f"Press {'+'.join(step.get('keys', []))}")
        elif stype == "scroll":
            lines.append(f"Scroll {'up' if (step.get('amount') or 0) > 0 else 'down'}")
        elif stype == "wait":
            lines.append(f"Wait {step.get('seconds', 0)}s")
    return lines


def build_metadata(steps: list[dict], screen: dict | None) -> dict:
    apps = set()
    windows = set()
    permissions = set()
    for step in steps:
        if step["type"] == "ensure_app":
            window = step.get("window") or {}
            if window.get("app"):
                apps.add(window["app"])
            if window.get("title"):
                windows.add(window["title"])
        elif step["type"] in ("click", "double_click", "right_click"):
            permissions.add("mouse_input")
        elif step["type"] in ("type", "press", "hotkey"):
            permissions.add("keyboard_input")
        elif step["type"] == "scroll":
            permissions.add("mouse_scroll")
    if screen:
        permissions.add("screen_capture")

    count = len([s for s in steps if s["type"] != "wait"])
    if count <= 5:
        difficulty = "easy"
    elif count <= 12:
        difficulty = "medium"
    else:
        difficulty = "hard"

    return {
        "applications": sorted(a for a in apps if a),
        "windows": sorted(w for w in windows if w),
        "permissions": sorted(permissions),
        "difficulty": difficulty,
        "estimated_duration_s": sum(
            s.get("seconds", 0) for s in steps if s["type"] == "wait"
        ),
    }


# --------------------------------------------------------------------- #
# LLM refinement (optional)
# --------------------------------------------------------------------- #

def _llm_descriptions(llm, steps: list[dict], windows: list[str],
                      instructions: list[str] | None = None) -> list[str] | None:
    """Ask the LLM to rewrite the steps as high-level semantic actions.
    Returns a description per step, or None on any failure."""
    if llm is None:
        return None
    try:
        summary = [
            {"type": s["type"],
             "target": s.get("target", ""),
             "text": s.get("text", ""),
             "key": s.get("key", ""),
             "keys": s.get("keys", []),
             "window": s.get("window", {})}
            for s in steps
        ]
        instruction_block = ""
        if instructions:
            instruction_block = (
                "\n\nSpoken instructions the user gave while demonstrating:\n"
                + "\n".join(f"- {i}" for i in instructions)
                + "\nUse them to clarify what each action is for."
            )
        data = llm(
            "You convert low-level desktop automation actions into clear, "
            "reusable, high-level steps. One short description per action. "
            "Return JSON: {\"descriptions\": [\"...\"]} with exactly "
            f"{len(steps)} entries.",
            f"Windows used: {', '.join(windows) or 'unknown'}.\n"
            f"Actions: {summary}{instruction_block}",
        )
        descriptions = (data or {}).get("descriptions")
        if isinstance(descriptions, list) and len(descriptions) == len(steps):
            return [str(d).strip() for d in descriptions]
    except Exception as exc:
        logger.warning("LLM step description failed: %s", exc)
    return None


def llm_skill_profile(llm, steps: list[dict], fallback_name: str,
                      instructions: list[str] | None = None) -> dict:
    """Ask the LLM for a skill name/description/tags. Degrades to a
    sensible default profile when the LLM is unavailable."""
    try:
        instruction_block = ""
        if instructions:
            instruction_block = (
                "\n\nThe user explained during the demo:\n"
                + "\n".join(f"- {i}" for i in instructions)
            )
        data = llm(
            "You help name and describe a reusable desktop automation skill. "
            'Return JSON: {"name": "...", "description": "...", "tags": ["..."]} '
            "with a short name (2-5 words), a one-sentence description, and 3-6 "
            "keyword tags.",
            f"Steps:\n" + "\n".join(describe_steps(steps)) + instruction_block,
        )
        name = str((data or {}).get("name", "")).strip()
        description = str((data or {}).get("description", "")).strip()
        tags = [str(tag).strip() for tag in (data or {}).get("tags", []) if str(tag).strip()]
        return {
            "name": name or fallback_name,
            "description": description or f"Reusable workflow with {len(steps)} steps.",
            "tags": tags or [],
        }
    except Exception as exc:
        logger.warning("LLM skill profile failed: %s", exc)
        return {
            "name": fallback_name,
            "description": f"Reusable workflow with {len(steps)} steps.",
            "tags": [],
        }


# --------------------------------------------------------------------- #
# Narration (spoken instructions captured during the demo)
# --------------------------------------------------------------------- #

def collect_narrations(events: list[dict]) -> list[dict]:
    """Extract spoken-instruction events from a raw capture, in the order
    they were said, each with the timestamp of when it was spoken."""
    narrations = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "narration":
            continue
        text = str(event.get("text", "")).strip()
        if not text:
            continue
        narrations.append({"t": event.get("t", 0.0), "text": text})
    return narrations


# --------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------- #

def parse_recording(recording: dict, *, llm=None) -> dict:
    """Turn a recorder capture into a complete timeline + metadata.

    Returns:
        {
          "semantic": [steps...],
          "variables": [...],
          "raw": [masked raw events...],
          "meta": {...},
          "screen": {...},
        }
    """
    events = recording.get("events") or []
    frames = recording.get("vision") or {}
    screen = recording.get("screen") or {}

    narrations = collect_narrations(events)
    instruction_texts = [n["text"] for n in narrations]

    actions = group_actions(events)
    steps, variables = build_steps(actions, frames, screen)
    descriptions = _llm_descriptions(
        llm, steps, [w for w in (recording.get("windows") or [])], instruction_texts
    )
    for step, description in zip(steps, descriptions or []):
        step["description"] = description
    for step in steps:
        if "description" not in step:
            step["description"] = describe_steps([step])[0]

    meta = build_metadata(steps, screen)
    meta["raw_event_count"] = len(events)
    meta["action_count"] = len(actions)
    if narrations:
        meta["instructions"] = instruction_texts

    return {
        "semantic": steps,
        "variables": variables,
        "raw": _mask_raw_events(events, steps),
        "meta": meta,
        "screen": screen,
        "narrations": narrations,
        "instructions": instruction_texts,
    }


def _mask_raw_events(events: list[dict], steps: list[dict]) -> list[dict]:
    """Scrub raw key events that belong to masked (secret) steps so no
    secret ever reaches disk, even in the debugging log."""
    masked = [s for s in steps if s.get("masked")]
    if not masked:
        return events
    result = []
    for event in events:
        event = dict(event)
        for step in masked:
            start = step.get("t", 0)
            end = start + _CLICK_FIELD_WINDOW
            if event.get("t") and start <= event.get("t") <= end:
                if event.get("type") == "key" and event.get("name", "").lower() not in KEYBOARD_MODIFIERS:
                    event["name"] = "•"
                break
        result.append(event)
    return result

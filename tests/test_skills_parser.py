"""Parser tests: raw events -> semantic actions, typing reconstruction,
variable detection, secret masking, metadata."""

from skills import parser

FRAME = {
    "width": 1920, "height": 1080,
    "objects": [
        {"id": "o0", "type": "input", "text": "Title", "bbox": [90, 40, 200, 30],
         "cx": 190, "cy": 55, "confidence": 1.0},
        {"id": "o1", "type": "button", "text": "Save", "bbox": [500, 300, 100, 40],
         "cx": 550, "cy": 320, "confidence": 1.0},
    ],
    "text": "Title Save",
}


def _events(*items):
    return list(items)


def test_group_actions_basic_flow():
    events = _events(
        {"t": 1.0, "type": "window", "title": "Word", "app": "WINWORD.EXE"},
        {"t": 2.0, "type": "click", "button": "left", "x": 100, "y": 50, "group": 5},
        {"t": 2.1, "type": "key", "action": "down", "name": "h"},
        {"t": 2.2, "type": "key", "action": "down", "name": "i"},
        {"t": 2.3, "type": "key", "action": "down", "name": "enter"},
        {"t": 3.0, "type": "scroll", "amount": 1},
        {"t": 4.0, "type": "key", "action": "down", "name": "ctrl"},
        {"t": 4.1, "type": "key", "action": "down", "name": "s"},
    )
    actions = parser.group_actions(events)
    kinds = [a["kind"] for a in actions]
    assert kinds == ["window", "click", "type", "press", "scroll", "hotkey"]
    assert actions[2]["text"] == "hi"
    assert actions[5]["keys"] == ["ctrl", "s"]


def test_typing_with_shift_and_backspace():
    events = _events(
        {"t": 1.0, "type": "key", "action": "down", "name": "shift"},
        {"t": 1.1, "type": "key", "action": "down", "name": "h"},
        {"t": 1.2, "type": "key", "action": "down", "name": "i"},
        {"t": 1.3, "type": "key", "action": "up", "name": "shift"},
        {"t": 1.4, "type": "key", "action": "down", "name": "backspace"},
        {"t": 1.5, "type": "key", "action": "down", "name": "o"},
        {"t": 1.6, "type": "click", "button": "left", "x": 10, "y": 10, "group": 1},
    )
    actions = parser.group_actions(events)
    assert actions[0]["kind"] == "type"
    assert actions[0]["text"] == "Ho"


def test_symbols_and_named_chars():
    events = _events(
        {"t": 1.0, "type": "key", "action": "down", "name": "shift"},
        {"t": 1.1, "type": "key", "action": "down", "name": "1"},
        {"t": 1.2, "type": "key", "action": "up", "name": "shift"},
        {"t": 1.3, "type": "key", "action": "down", "name": "space"},
        {"t": 1.4, "type": "key", "action": "down", "name": "period"},
        {"t": 1.5, "type": "click", "button": "left", "x": 0, "y": 0, "group": 1},
    )
    actions = parser.group_actions(events)
    assert actions[0]["text"] == "! ."


def test_double_click_grouping():
    events = _events(
        {"t": 1.0, "type": "click", "button": "left", "x": 50, "y": 60, "group": 3},
        {"t": 1.1, "type": "click", "button": "left", "x": 50, "y": 60, "group": 3},
    )
    actions = parser.group_actions(events)
    assert actions[0]["kind"] == "double_click"


def test_build_steps_resolves_ui_targets_and_variables():
    events = _events(
        {"t": 1.0, "type": "window", "title": "Word", "app": "WINWORD.EXE"},
        {"t": 2.0, "type": "click", "button": "left", "x": 100, "y": 50, "group": 5},
        {"t": 2.1, "type": "key", "action": "down", "name": "p"},
        {"t": 2.2, "type": "key", "action": "down", "name": "h"},
        {"t": 2.3, "type": "key", "action": "down", "name": "y"},
        {"t": 2.4, "type": "key", "action": "down", "name": "s"},
        {"t": 2.5, "type": "key", "action": "down", "name": "i"},
        {"t": 2.6, "type": "key", "action": "down", "name": "c"},
        {"t": 2.7, "type": "key", "action": "down", "name": "s"},
        {"t": 3.0, "type": "click", "button": "left", "x": 550, "y": 320, "group": 9},
    )
    actions = parser.group_actions(events)
    steps, variables = parser.build_steps(
        actions, {"2.5": FRAME}, {"width": 1920, "height": 1080, "scale": 1.0}
    )
    types = [s["type"] for s in steps]
    assert types == ["ensure_app", "click", "type", "click"]

    # The click inside the Title field resolves to the "Title" input object.
    assert steps[1]["ui_object"]["text"] == "Title"
    assert steps[1]["relative"] == [round(100 / 1920, 4), round(50 / 1080, 4)]

    # "physics" typed into the Title field becomes a variable.
    assert steps[2]["is_variable"] is True
    assert steps[2]["text"] == "{{Title}}"
    assert variables[0]["name"] == "Title"
    assert variables[0]["type"] == "text"
    assert variables[0]["example"] == "physics"

    # Click on the Save button resolves its label.
    assert steps[3]["ui_object"]["text"] == "Save"


def test_secret_typing_is_masked_everywhere():
    events = _events(
        {"t": 1.0, "type": "click", "button": "left", "x": 100, "y": 50, "group": 5},
        {"t": 1.1, "type": "key", "action": "down", "name": "m"},
        {"t": 1.2, "type": "key", "action": "down", "name": "y"},
        {"t": 1.3, "type": "key", "action": "down", "name": "p"},
        {"t": 1.4, "type": "key", "action": "down", "name": "a"},
        {"t": 1.5, "type": "key", "action": "down", "name": "s"},
        {"t": 1.6, "type": "key", "action": "down", "name": "s"},
        {"t": 1.7, "type": "key", "action": "down", "name": "1"},
        {"t": 1.8, "type": "key", "action": "down", "name": "2"},
        {"t": 1.9, "type": "key", "action": "down", "name": "3"},
    )
    recording = {
        "events": events,
        "vision": {"1.0": {"width": 1920, "height": 1080, "objects": [
            {"id": "o", "type": "input", "text": "Password", "bbox": [90, 40, 200, 30],
             "cx": 190, "cy": 55, "confidence": 1.0},
        ], "text": "Password"}},
        "screen": {"width": 1920, "height": 1080, "scale": 1.0},
    }
    parsed = parser.parse_recording(recording)
    typed = [s for s in parsed["semantic"] if s["type"] == "type"]
    assert typed, "expected a type step"
    assert typed[0]["masked"] is True
    assert typed[0]["text"] == "{{Password}}"
    assert "mypassword123" not in str(parsed)
    # Raw events scrubbed too -- no secret reaches disk.
    raw_text = "".join(e.get("name", "") for e in parsed["raw"]
                       if e.get("type") == "key")
    assert "mypassword123" not in raw_text


def test_classify_value_types():
    assert parser.classify_value("42") == "number"
    assert parser.classify_value("2026-08-02") == "date"
    assert parser.classify_value("https://github.com/foo") == "url"
    assert parser.classify_value("C:\\Users\\nadel\\notes.txt") == "path"
    assert parser.classify_value("C:\\Users\\nadel\\Projects") == "folder"
    assert parser.classify_value("123456", "verification code") == "password"
    assert parser.classify_value("123456") == "password"  # OTP-like
    assert parser.classify_value("hunter2secret", "Password") == "password"
    assert parser.classify_value("https://www.google.com") is None  # fixed nav
    assert parser.classify_value("hello world") == "text"


def test_metadata():
    steps = [
        {"type": "ensure_app", "window": {"title": "Word", "app": "WINWORD.EXE"}, "t": 1},
        {"type": "click", "t": 2},
        {"type": "type", "t": 3},
        {"type": "wait", "seconds": 2.5, "t": 4},
    ]
    meta = parser.build_metadata(steps, {"width": 1920})
    assert meta["applications"] == ["WINWORD.EXE"]
    assert meta["windows"] == ["Word"]
    assert meta["permissions"] == ["keyboard_input", "mouse_input", "screen_capture"]
    assert meta["difficulty"] == "easy"
    assert meta["estimated_duration_s"] == 2.5


def test_describe_steps():
    steps = [
        {"type": "click", "target": "Login", "t": 1},
        {"type": "type", "text": "{{Title}}", "is_variable": True, "t": 2},
        {"type": "hotkey", "keys": ["ctrl", "s"], "t": 3},
    ]
    lines = parser.describe_steps(steps)
    assert "click Login" in lines[0]
    assert "{{Title}}" in lines[1]
    assert "ctrl+s" in lines[2]

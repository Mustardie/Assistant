"""Unit tests for BrowserAgent's text-lookup fallbacks and fill_form.

The Edge Extension backend addresses elements by CSS selector; users (and
the planner) describe them by visible text. browser_agent must: try the
description as a selector first, then resolve it by text (find_element)
and retry with the returned selector. Also verifies the semantic
fill_form payload.
"""

from tools.browser.browser_agent import BrowserAgent


def _agent(monkeypatch, send_impl):
    agent = BrowserAgent()
    agent._legacy = None
    calls = []

    def send(command_type, payload=None):
        calls.append((command_type, payload or {}))
        return send_impl(command_type, payload or {}, calls)

    monkeypatch.setattr(agent, "_ext_send", send)
    return agent, calls


def _ok(**kw):
    return {"success": True, **kw}


def test_click_uses_selector_directly_when_it_works(monkeypatch):
    agent, calls = _agent(monkeypatch, lambda t, p, c: _ok())
    result = agent.click("#submit")

    assert result["success"] is True
    assert calls == [("click", {"selector": "#submit", "options": {}})]


def test_click_falls_back_to_text_lookup(monkeypatch):
    def impl(t, p, c):
        if t == "click":
            if p["selector"].startswith("[data-nova-target"):
                return _ok()
            return {"success": False, "error": f"Element not found: {p['selector']}"}
        if t == "find_element":
            assert p["description"] == "the 'Blank document' button"
            return _ok(best={"selector": "[data-nova-target='abc']", "tag": "div"},
                       elements=[{"selector": "[data-nova-target='abc']", "tag": "div"}])
        raise AssertionError(f"unexpected {t}")

    agent, calls = _agent(monkeypatch, impl)
    result = agent.click("the 'Blank document' button")

    assert result["success"] is True
    assert calls[0][0] == "click"
    assert calls[1] == ("find_element", {"description": "the 'Blank document' button"})
    assert calls[2] == ("click", {"selector": "[data-nova-target='abc']", "options": {}})


def test_click_reports_failure_when_text_lookup_finds_nothing(monkeypatch):
    def impl(t, p, c):
        if t == "click":
            return {"success": False, "error": "Element not found: zzz"}
        return {"success": False, "error": "No element matches: zzz"}

    agent, calls = _agent(monkeypatch, impl)
    result = agent.click("zzz")

    assert result["success"] is False
    assert "Could not find anything to click" in result["error"]


def test_type_text_falls_back_to_lookup_then_keyboard_mode(monkeypatch):
    def impl(t, p, c):
        if t == "type_text":
            if p.get("keyboard"):
                return _ok(mode="keyboard", typed=3)
            return {"success": False, "error": f"Element not found: {p['selector']}"}
        if t == "find_element":
            return _ok(best={"selector": "[data-nova-target='x']", "tag": "input"})
        raise AssertionError(f"unexpected {t}")

    agent, calls = _agent(monkeypatch, impl)
    result = agent.type_text("Search box", "abc")

    assert result["success"] is True
    assert result["mode"] == "keyboard"
    payloads = [p for t, p in calls if t == "type_text"]
    assert payloads[-1]["keyboard"] is True
    assert payloads[-1]["selector"] == "[data-nova-target='x']"


def test_type_text_press_enter_sends_key_after_success(monkeypatch):
    def impl(t, p, c):
        if t == "type_text":
            return _ok(mode="input")
        if t == "press_key":
            return _ok()
        raise AssertionError(f"unexpected {t}")

    agent, calls = _agent(monkeypatch, impl)
    result = agent.type_text("input#q", "hello", press_enter=True)

    assert result["success"] is True
    assert calls[-1] == ("press_key", {"key": "Enter"})


def test_fill_form_sends_semantic_fields(monkeypatch):
    def impl(t, p, c):
        assert t == "fill_form"
        return _ok(filled=2, results=[
            {"field": "email", "success": True},
            {"field": "password", "success": True},
        ])

    agent, calls = _agent(monkeypatch, impl)
    result = agent.fill_form({"email": "me@example.com", "password": "s3cret"})

    assert result["success"] is True
    assert calls[0][0] == "fill_form"
    payload = calls[0][1]["fields"]
    assert payload == [
        {"field": "email", "value": "me@example.com"},
        {"field": "password", "value": "s3cret"},
    ]


def test_fill_form_rejects_empty_fields(monkeypatch):
    agent, _ = _agent(monkeypatch, lambda t, p, c: _ok())
    result = agent.fill_form({})
    assert result["success"] is False

import logging

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .element_finder import ElementNotFoundError, resolve

logger = logging.getLogger(__name__)


def _run(page, description, role, action_name, do):
    try:
        resolved = resolve(page, description, role=role)
    except ElementNotFoundError as exc:
        return {"success": False, "action": action_name, "description": description, "error": str(exc)}

    try:
        resolved.locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    try:
        do(resolved.locator)
    except PlaywrightTimeoutError as exc:
        return {
            "success": False, "action": action_name, "description": description,
            "strategy": resolved.strategy, "error": f"Found the element but the action timed out: {exc}",
        }
    except Exception as exc:
        return {
            "success": False, "action": action_name, "description": description,
            "strategy": resolved.strategy, "error": str(exc),
        }

    return {"success": True, "action": action_name, "description": description, "strategy": resolved.strategy}


def click(page, description: str, timeout: int = 10000) -> dict:
    return _run(page, description, None, "click", lambda loc: loc.click(timeout=timeout))


def double_click(page, description: str, timeout: int = 10000) -> dict:
    return _run(page, description, None, "double_click", lambda loc: loc.dblclick(timeout=timeout))


def right_click(page, description: str, timeout: int = 10000) -> dict:
    return _run(page, description, None, "right_click", lambda loc: loc.click(button="right", timeout=timeout))


def hover(page, description: str, timeout: int = 10000) -> dict:
    return _run(page, description, None, "hover", lambda loc: loc.hover(timeout=timeout))


def type_text(page, description: str, text: str, clear_first: bool = True, timeout: int = 10000) -> dict:
    def do(loc):
        if clear_first:
            loc.fill(text, timeout=timeout)
        else:
            loc.type(text, timeout=timeout)
    result = _run(page, description, None, "type_text", do)
    result["text_length"] = len(text)
    return result


def press_key(page, key: str) -> dict:
    try:
        page.keyboard.press(key)
        return {"success": True, "action": "press_key", "key": key}
    except Exception as exc:
        return {"success": False, "action": "press_key", "key": key, "error": str(exc)}


def scroll(page, amount: int = 600, description: str | None = None) -> dict:
    if description:
        return _run(page, description, None, "scroll_to", lambda loc: loc.scroll_into_view_if_needed(timeout=5000))
    try:
        page.mouse.wheel(0, amount)
        return {"success": True, "action": "scroll", "amount": amount}
    except Exception as exc:
        return {"success": False, "action": "scroll", "amount": amount, "error": str(exc)}


def select_dropdown(page, description: str, option: str, timeout: int = 10000) -> dict:
    result = _run(page, description, "combobox", "select_dropdown", lambda loc: loc.select_option(label=option, timeout=timeout))
    result["option"] = option
    return result


def set_checkbox(page, description: str, checked: bool = True, timeout: int = 10000) -> dict:
    action = (lambda loc: loc.check(timeout=timeout)) if checked else (lambda loc: loc.uncheck(timeout=timeout))
    result = _run(page, description, "checkbox", "set_checkbox", action)
    result["checked"] = checked
    return result


def click_radio(page, description: str, timeout: int = 10000) -> dict:
    return _run(page, description, "radio", "click_radio", lambda loc: loc.check(timeout=timeout))


def drag_and_drop(page, source_description: str, target_description: str, timeout: int = 10000) -> dict:
    try:
        source = resolve(page, source_description)
        target = resolve(page, target_description)
    except ElementNotFoundError as exc:
        return {"success": False, "action": "drag_and_drop", "error": str(exc)}

    try:
        source.locator.drag_to(target.locator, timeout=timeout)
    except Exception as exc:
        return {"success": False, "action": "drag_and_drop", "error": str(exc)}

    return {
        "success": True, "action": "drag_and_drop",
        "source": source_description, "target": target_description,
    }


def upload_file(page, description: str, file_path: str, timeout: int = 10000) -> dict:
    result = _run(page, description, None, "upload_file", lambda loc: loc.set_input_files(file_path, timeout=timeout))
    result["file_path"] = file_path
    return result

"""Universal tool facade -- bridges the capability-dispatched universal
tools and the app adapters into the flat string-keyed tool registry.

These wrappers keep the agent loop's run_tool() contract (function that
returns a dict or (bool, result)) while exposing the new architecture's
universal capabilities to the LLM prompt.
"""

from tools.communication_tool import (
    read_messages as _read_messages,
    search_messages as _search_messages,
    send_message as _send_message,
    reply_to_message as _reply_to_message,
    identify_sender as _identify_sender,
    inspect_attachment as _inspect_attachment,
    download_attachment as _download_attachment,
)
from tools.calendar_task_tool import (
    create_event as _create_event,
    update_event as _update_event,
    delete_event as _delete_event,
    list_events as _list_events,
    create_task as _create_task,
    update_task as _update_task,
    list_tasks as _list_tasks,
    create_reminder as _create_reminder,
)
from tools.media_tool import (
    play_media as _play_media,
    pause_media as _pause_media,
    skip_media as _skip_media,
    search_media as _search_media,
    control_volume as _control_volume,
    now_playing as _now_playing,
    set_volume as _set_volume,
    toggle_shuffle as _toggle_shuffle,
    toggle_repeat as _toggle_repeat,
    save_media as _save_media,
    add_to_queue as _add_to_queue,
)


# ------------------------ Communication ------------------------ #

def read_messages(source=None, limit=20, **kw):
    return _read_messages(source=source, limit=limit, **kw)


def search_messages(query, source=None, limit=20, **kw):
    return _search_messages(query=query, source=source, limit=limit, **kw)


def send_message(recipient, text, source=None, confirm=False, **kw):
    """HIGH risk: sending requires confirmation unless automation policy
    permits it. If confirm=True is passed by the agent loop's policy
    layer, dispatch; otherwise return a requires_confirmation result."""
    from brain.policy import risk_level, requires_confirmation  # type: ignore

    if risk_level("send_message") == "high" and requires_confirmation("send_message", confirm=confirm):
        return {"success": False, "requires_confirmation": True,
                "message": f"Sending a message to '{recipient}' needs your confirmation."}
    return _send_message(recipient=recipient, text=text, source=source, **kw)


def reply_to_message(message_id, text, source=None, **kw):
    return _reply_to_message(message_id=message_id, text=text, source=source, **kw)


def identify_sender(message, source=None, **kw):
    return _identify_sender(message=message, source=source, **kw)


def inspect_attachment(message, index=0, source=None, **kw):
    return _inspect_attachment(message=message, index=index, source=source, **kw)


def download_attachment(message, index=0, destination=None, source=None, **kw):
    return _download_attachment(message=message, index=index, destination=destination, source=source, **kw)


# ------------------------ Calendar / Tasks ------------------------ #

def create_event(summary, start=None, end=None, source=None, confirm=False, **kw):
    from brain.policy import risk_level, requires_confirmation  # type: ignore

    if risk_level("create_event") == "medium" and requires_confirmation("create_event", confirm=confirm):
        return {"success": False, "requires_confirmation": True,
                "message": f"Creating the event '{summary}' needs your confirmation."}
    return _create_event(summary=summary, start=start, end=end, source=source, **kw)


def update_event(event_id, source=None, **kw):
    return _update_event(event_id=event_id, source=source, **kw)


def delete_event(event_id, source=None, confirm=False, **kw):
    from brain.policy import risk_level, requires_confirmation  # type: ignore

    if risk_level("delete_event") == "high" and requires_confirmation("delete_event", confirm=confirm):
        return {"success": False, "requires_confirmation": True,
                "message": "Deleting a calendar event needs your confirmation."}
    return _delete_event(event_id=event_id, source=source, **kw)


def list_events(start=None, end=None, limit=20, source=None, **kw):
    return _list_events(start=start, end=end, limit=limit, source=source, **kw)


def create_task(title, due=None, source=None, confirm=False, **kw):
    from brain.policy import risk_level, requires_confirmation  # type: ignore

    if risk_level("create_task") == "medium" and requires_confirmation("create_task", confirm=confirm):
        return {"success": False, "requires_confirmation": True,
                "message": f"Creating the task '{title}' needs your confirmation."}
    return _create_task(title=title, due=due, source=source, **kw)


def update_task(task_id, source=None, **kw):
    return _update_task(task_id=task_id, source=source, **kw)


def list_tasks(limit=50, source=None, **kw):
    return _list_tasks(limit=limit, source=source, **kw)


def create_reminder(text, when=None, source=None, confirm=False, **kw):
    from brain.policy import risk_level, requires_confirmation  # type: ignore

    if risk_level("create_reminder") == "medium" and requires_confirmation("create_reminder", confirm=confirm):
        return {"success": False, "requires_confirmation": True,
                "message": "Creating a reminder needs your confirmation."}
    return _create_reminder(text=text, when=when, source=source, **kw)


# ------------------------ Media ------------------------ #

def play_media(query=None, source=None, **kw):
    return _play_media(query=query, source=source, **kw)


def pause_media(source=None, **kw):
    return _pause_media(source=source, **kw)


def skip_media(direction="next", source=None, **kw):
    return _skip_media(direction=direction, source=source, **kw)


def search_media(query, source=None, **kw):
    return _search_media(query=query, source=source, **kw)


def control_volume(direction, **kw):
    return _control_volume(direction)


def now_playing(source=None, **kw):
    return _now_playing(source=source, **kw)


def set_volume(percent, source=None, **kw):
    return _set_volume(percent=percent, source=source, **kw)


def toggle_shuffle(source=None, **kw):
    return _toggle_shuffle(source=source, **kw)


def toggle_repeat(state="context", source=None, **kw):
    return _toggle_repeat(state=state, source=source, **kw)


def save_media(uri=None, query=None, source=None, **kw):
    return _save_media(uri=uri, query=query, source=source, **kw)


def add_to_queue(uri, source=None, **kw):
    return _add_to_queue(uri=uri, source=source, **kw)


# ------------------------ Computer / UI (mapped names) ------------------------ #

from tools import computer_tool  # noqa: E402
from tools import uiautomation_tool  # noqa: E402


def open_app(query=None):
    from tools.app_launcher import launch_app
    return launch_app(query)


def close_app(name=None):
    if not name:
        return {"success": False, "error": "Missing 'name'."}
    from tools.uiautomation_tool import close_window
    return close_window(title=name)


def focus_app(name=None):
    if not name:
        return {"success": False, "error": "Missing 'name'."}
    from tools.uiautomation_tool import focus_window
    return focus_window(title=name)


def get_active_window():
    from tools.uiautomation_tool import get_active_window as _gaw
    return _gaw()


def inspect_window(hwnd=None, max_depth=2):
    from tools.uiautomation_tool import inspect_window as _iw
    return _iw(hwnd=hwnd, max_depth=max_depth)


def inspect_accessibility_tree(title=None, hwnd=None, max_depth=4, max_nodes=250):
    from tools.uiautomation_tool import inspect_accessibility_tree as _iat
    return _iat(title=title, hwnd=hwnd, max_depth=max_depth, max_nodes=max_nodes)


def inspect_ui_elements(hwnd=None, max_depth=2):
    from tools.uiautomation_tool import inspect_window as _iw
    result = _iw(hwnd=hwnd, max_depth=max_depth)
    if result.get("success"):
        return {"success": True, "elements": result.get("controls", [])}
    return result


def read_visible_text(hwnd=None, max_chars=4000):
    from tools.uiautomation_tool import read_visible_text as _rvt
    text = _rvt(hwnd=hwnd, max_chars=max_chars)
    return {"success": bool(text), "text": text}


def locate_ui_element(description, hwnd=None):
    from tools.uiautomation_tool import locate_ui_element as _lue
    return _lue(description, hwnd=hwnd)


def interact_with_ui_element(description, action="click", text=None):
    from tools.uiautomation_tool import interact_with_ui_element as _iwue
    return _iwue(description, action=action, text=text)


def click(x=None, y=None):
    if x is not None and y is not None:
        computer_tool.move_mouse(x, y)
    computer_tool.left_click()
    return {"success": True}


def type_text(text):
    if not text:
        return {"success": False, "error": "Missing 'text'."}
    computer_tool.type_text(text)
    return {"success": True}


def press_key(key):
    if not key:
        return {"success": False, "error": "Missing 'key'."}
    computer_tool.press_key(key)
    return {"success": True}


def hotkey(keys):
    if isinstance(keys, str):
        keys = [keys]
    computer_tool.hotkey(*keys)
    return {"success": True}


def scroll(amount=300, direction="down"):
    if direction == "up":
        amount = -abs(amount or 0)
    elif direction == "down":
        amount = abs(amount or 0)
    computer_tool.scroll(amount)
    return {"success": True}


def select_text_copy():
    from tools.system_tool import select_text_copy as _stc
    return _stc()


def copy(text=None):
    if text is not None:
        from tools.system_tool import set_clipboard
        return set_clipboard(text)
    return select_text_copy()


def paste():
    from tools.system_tool import paste as _paste
    return _paste()


def wait_for_ui(description, timeout=10):
    from tools.uiautomation_tool import wait_for_ui as _wfu
    return _wfu(description, timeout=timeout)


def detect_ui_change(description, baseline=None):
    from tools.uiautomation_tool import detect_ui_change as _duc
    return _duc(description, baseline=baseline)


# ------------------------ Documents ------------------------ #

from tools import document_tool  # noqa: E402


def read_document(path, max_chars=20000):
    """Universal document reader (auto-detects PDF/DOCX/XLSX/PPTX/text)."""
    return document_tool.extract_text(path, max_chars=max_chars)


def read_pdf(path, max_chars=20000):
    return document_tool.read_pdf(path, max_chars=max_chars)


def read_docx(path, max_chars=20000):
    return document_tool.read_docx(path, max_chars=max_chars)


def read_xlsx(path, max_chars=20000):
    return document_tool.read_xlsx(path, max_chars=max_chars)


def read_pptx(path, max_chars=20000):
    return document_tool.read_pptx(path, max_chars=max_chars)


def extract_text(path, max_chars=20000):
    return document_tool.extract_text(path, max_chars=max_chars)


def summarize_document(path, max_chars=20000, length="short"):
    return document_tool.summarize_document(path, max_chars=max_chars, length=length)


# ------------------------ System ------------------------ #

from tools import system_tool  # noqa: E402


def get_system_info():
    return system_tool.get_system_info()


def get_running_apps(include_paths=True, limit=200):
    return system_tool.get_running_apps(include_paths=include_paths, limit=limit)


def launch_process(command, cwd=None):
    return system_tool.launch_process(command, cwd=cwd)


def terminate_process(pid=None, name=None):
    return system_tool.terminate_process(pid=pid, name=name)


def get_notifications():
    return system_tool.get_notifications()


def get_clipboard():
    return system_tool.get_clipboard()


def set_volume(direction):
    return system_tool.set_volume(direction)


def get_volume():
    return system_tool.get_volume()
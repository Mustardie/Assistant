"""Universal computer / UI tools built on Windows user32 (ctypes) and the
existing desktop-control helpers (pyautogui).

Design notes:
- Window & control inspection uses the Win32 API (EnumWindows /
  EnumChildWindows / GetWindowText) via ctypes -- no extra dependencies.
- If the optional `uiautomation` / `comtypes` package is installed, the
  richer accessibility-tree tools activate; otherwise they degrade to the
  Win32 control tree and report a graceful hint.
- Input actions (click/type/hotkey/scroll) reuse the existing helpers from
  tools/computer_tool.py so there is exactly one implementation.
- Screenshots are NEVER taken implicitly; vision is only a fallback.
"""

import ctypes
import ctypes.wintypes
import logging
import sys
import time

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------- #
# Win32 constants
# --------------------------------------------------------------------- #
_WM_GETTEXT = 0x000D
_GW_HWNDNEXT = 2
_GW_CHILD = 5

if _IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _GetForegroundWindow = _user32.GetForegroundWindow
    _GetWindowTextW = _user32.GetWindowTextW
    _GetWindowTextLengthW = _user32.GetWindowTextLengthW
    _IsWindowVisible = _user32.IsWindowVisible
    _EnumWindows = _user32.EnumWindows
    _EnumChildWindows = _user32.EnumChildWindows
    _GetClassNameW = _user32.GetClassNameW
    _GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
    _IsWindow = _user32.IsWindow
    _GetParent = _user32.GetParent
    _SetForegroundWindow = _user32.SetForegroundWindow
    _ShowWindow = _user32.ShowWindow
    _GetWindowLongW = _user32.GetWindowLongW
    _PostMessageW = _user32.PostMessageW
    _GetWindowRect = _user32.GetWindowRect
    _IsIconic = _user32.IsIconic
    _SendMessageW = _user32.SendMessageW
    _OpenProcess = ctypes.windll.kernel32.OpenProcess
    _QueryFullProcessImageNameW = ctypes.windll.kernel32.QueryFullProcessImageNameW
    _GetExitCodeProcess = ctypes.windll.kernel32.GetExitCodeProcess
    _CloseHandle = ctypes.windll.kernel32.CloseHandle

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001
    SW_RESTORE = 9

    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def _window_title(hwnd) -> str:
    if not _IS_WINDOWS or not hwnd:
        return ""
    length = _GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_class(hwnd) -> str:
    if not _IS_WINDOWS or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    _GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_pid(hwnd) -> int:
    if not _IS_WINDOWS or not hwnd:
        return -1
    pid = ctypes.wintypes.DWORD()
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_path(pid: int) -> str:
    if not _IS_WINDOWS or pid <= 0:
        return ""
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(len(buf))
        if _QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        _CloseHandle(handle)
    return ""


# --------------------------------------------------------------------- #
# Window enumeration
# --------------------------------------------------------------------- #


def get_active_window() -> dict:
    """Return the foreground window's title, class, pid and process path."""
    if not _IS_WINDOWS:
        return {"success": False, "error": "Active-window inspection requires Windows."}
    hwnd = _GetForegroundWindow()
    if not hwnd:
        return {"success": True, "hwnd": 0, "title": "", "class": "", "pid": -1}
    return {
        "success": True,
        "hwnd": int(hwnd),
        "title": _window_title(hwnd),
        "class": _window_class(hwnd),
        "pid": _window_pid(hwnd),
        "process": _process_path(_window_pid(hwnd)),
    }


def list_windows(*, top_level_only: bool = True, min_title_len: int = 1) -> list[dict]:
    """List open top-level windows (title, hwnd, pid, visible, process)."""
    if not _IS_WINDOWS:
        return []
    results: list[dict] = []

    def callback(hwnd, _lparam):
        title = _window_title(hwnd)
        if not title or len(title) < min_title_len:
            return True
        if top_level_only and _GetParent(hwnd):
            return True
        results.append({
            "hwnd": int(hwnd),
            "title": title,
            "class": _window_class(hwnd),
            "pid": _window_pid(hwnd),
            "visible": bool(_IsWindowVisible(hwnd)),
            "process": _process_path(_window_pid(hwnd)),
        })
        return True

    _EnumWindows(_WNDENUMPROC(callback), 0)
    return results


def list_controls(hwnd: int, max_depth: int = 3) -> list[dict]:
    """Inspect the child controls of a window (Win32 control tree)."""
    if not _IS_WINDOWS:
        return []
    if not hwnd or not _IsWindow(hwnd):
        return []
    results: list[dict] = []
    _MAX = 300

    def walk(hwnd, depth):
        if depth > max_depth or len(results) >= _MAX:
            return
        children = []

        def collect(child, _lparam):
            children.append(int(child))
            return True

        _EnumChildWindows(hwnd, _WNDENUMPROC(collect), 0)
        for child in children:
            title = _window_title(child)
            cls = _window_class(child)
            results.append({
                "hwnd": int(child),
                "title": title,
                "class": cls,
                "pid": _window_pid(child),
                "parent": int(hwnd),
            })
            walk(child, depth + 1)

    walk(int(hwnd), 1)
    return results


def read_visible_text(hwnd: int | None = None, *, max_chars: int = 4000) -> str:
    """Return the visible text of a window (its title + child control
    labels). This is a cheap accessibility-oriented read -- not OCR, and
    not a screenshot."""
    if not _IS_WINDOWS:
        return ""
    if hwnd is None:
        hwnd = _GetForegroundWindow()
    hwnd = int(hwnd)
    parts: list[str] = []
    seen: set[str] = set()

    def add(text: str):
        text = (text or "").strip()
        if text and text not in seen:
            seen.add(text)
            parts.append(text)

    add(_window_title(hwnd))
    for control in list_controls(hwnd, max_depth=2):
        if control.get("title"):
            add(control["title"])
        if control.get("class") and not control.get("title"):
            add(f"[{control['class']}]")
        if len("\n".join(parts)) >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


def inspect_window(hwnd: int | None = None, *, max_depth: int = 2) -> dict:
    """Structured inspection of a window: identity + control tree."""
    if not _IS_WINDOWS:
        return {"success": False, "error": "Window inspection requires Windows."}
    if hwnd is None:
        hwnd = _GetForegroundWindow()
    hwnd = int(hwnd)
    if not hwnd or not _IsWindow(hwnd):
        return {"success": False, "error": f"Invalid window handle: {hwnd}"}
    return {
        "success": True,
        "hwnd": hwnd,
        "title": _window_title(hwnd),
        "class": _window_class(hwnd),
        "pid": _window_pid(hwnd),
        "process": _process_path(_window_pid(hwnd)),
        "controls": list_controls(hwnd, max_depth=max_depth),
    }


# --------------------------------------------------------------------- #
# Window actions
# --------------------------------------------------------------------- #


def focus_window(title: str | None = None, hwnd: int | None = None) -> dict:
    """Bring a window to the foreground by title substring or hwnd."""
    if not _IS_WINDOWS:
        return {"success": False, "error": "Window focus requires Windows."}
    target = None
    if hwnd:
        target = int(hwnd)
        if not _IsWindow(target):
            return {"success": False, "error": f"Invalid window handle: {hwnd}"}
    elif title:
        for win in list_windows():
            if title.lower() in win["title"].lower():
                target = win["hwnd"]
                break
        if target is None:
            return {"success": False, "error": f"No open window matches '{title}'."}
    else:
        return {"success": False, "error": "Provide either title or hwnd."}

    if _IsIconic(target):
        _ShowWindow(target, SW_RESTORE)
    _SetForegroundWindow(target)
    time.sleep(0.15)
    current = _GetForegroundWindow()
    return {
        "success": True,
        "hwnd": int(current),
        "focused": bool(current == target),
        "title": _window_title(current),
    }


def close_window(title: str | None = None, hwnd: int | None = None) -> dict:
    """Post a WM_CLOSE to a window (graceful close request)."""
    if not _IS_WINDOWS:
        return {"success": False, "error": "Window close requires Windows."}
    if hwnd:
        target = int(hwnd)
    elif title:
        target = None
        for win in list_windows():
            if title.lower() in win["title"].lower():
                target = win["hwnd"]
                break
        if target is None:
            return {"success": False, "error": f"No open window matches '{title}'."}
    else:
        return {"success": False, "error": "Provide either title or hwnd."}
    if not _IsWindow(target):
        return {"success": False, "error": f"Invalid window handle: {target}"}
    _PostMessageW(target, 0x0010, 0, 0)  # WM_CLOSE
    return {"success": True, "hwnd": int(target), "message": "Close requested."}


# --------------------------------------------------------------------- #
# Optional accessibility tree (uiautomation) -- richer than Win32 tree
# --------------------------------------------------------------------- #


def _uiautomation_available() -> bool:
    try:
        import uiautomation  # noqa: F401
        return True
    except Exception:
        return False


def inspect_accessibility_tree(
    title: str | None = None,
    hwnd: int | None = None,
    *,
    max_depth: int = 4,
    max_nodes: int = 250,
) -> dict:
    """Inspect the Windows UI Automation (accessibility) tree for a
    window. Falls back to the Win32 control tree when the optional
    `uiautomation` package is unavailable."""
    if not _IS_WINDOWS:
        return {"success": False, "error": "Accessibility inspection requires Windows."}
    if _uiautomation_available():
        try:
            import uiautomation as auto

            if hwnd:
                root = auto.ControlFromHandle(int(hwnd))
            else:
                root = auto.GetForegroundControl()
            if root is None:
                return {"success": False, "error": "Could not access a UI Automation root."}

            nodes: list[dict] = []
            count = [0]

            def walk(control, depth):
                if depth > max_depth or count[0] >= max_nodes:
                    return
                count[0] += 1
                name = ""
                ctype = ""
                try:
                    name = control.Name or ""
                except Exception:
                    pass
                try:
                    ctype = control.ControlTypeName or ""
                except Exception:
                    pass
                nodes.append({"name": name, "type": ctype, "depth": depth})
                try:
                    children = control.GetChildren()
                except Exception:
                    children = []
                for child in children:
                    walk(child, depth + 1)

            walk(root, 0)
            return {"success": True, "engine": "uiautomation", "nodes": nodes,
                    "count": len(nodes)}
        except Exception as exc:
            logger.warning("[UIA] uiautomation failed (%s) -- falling back to Win32 tree", exc)

    win = hwnd or (_GetForegroundWindow() if _IS_WINDOWS else None)
    controls = list_controls(win, max_depth=min(max_depth, 3))
    return {
        "success": True,
        "engine": "win32_control_tree",
        "nodes": controls,
        "count": len(controls),
        "hint": (
            "Richer accessibility data is available when the optional "
            "'uiautomation' package is installed."
        ),
    }


def locate_ui_element(description: str, *, hwnd: int | None = None) -> dict:
    """Find a UI element by its visible label within a window's control
    tree. Returns hwnd + best candidate matches."""
    if not description:
        return {"success": False, "error": "Missing 'description'."}
    if hwnd is None:
        win = _GetForegroundWindow() if _IS_WINDOWS else None
    else:
        win = int(hwnd)
    controls = list_controls(win, max_depth=4)
    needle = description.lower()
    matches = [c for c in controls if needle in (c.get("title") or "").lower()]
    if not matches:
        return {"success": False, "matches": [], "error": f"No UI element labeled '{description}'."}
    return {"success": True, "matches": matches[:10], "count": len(matches)}


def interact_with_ui_element(description: str, action: str = "click", *, text: str | None = None) -> dict:
    """Interact with a UI element by label. action: click | double_click |
    right_click | type | focus. Uses window rect + the pyautogui helper so
    it works on real desktops without a screen reader."""
    from tools.computer_tool import (
        move_mouse,
        left_click,
        double_click,
        right_click,
        type_text,
    )

    located = locate_ui_element(description)
    if not located.get("success"):
        return located
    matches = located.get("matches") or []
    target = matches[0]
    hwnd = int(target["hwnd"])
    rect = ctypes.wintypes.RECT()
    if not _GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"success": False, "error": "Could not read the element's bounds."}
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2

    if not _IsWindow(hwnd):
        return {"success": False, "error": "Element handle is no longer valid."}
    _SetForegroundWindow(hwnd)
    time.sleep(0.1)

    if action == "type":
        if not text:
            return {"success": False, "error": "'text' is required for action='type'."}
        move_mouse(cx, cy)
        time.sleep(0.1)
        left_click()
        time.sleep(0.1)
        type_text(text)
        return {"success": True, "action": "type", "hwnd": hwnd, "text": text}
    if action == "double_click":
        move_mouse(cx, cy)
        double_click()
    elif action == "right_click":
        move_mouse(cx, cy)
        right_click()
    else:
        move_mouse(cx, cy)
        left_click()
    return {"success": True, "action": action, "hwnd": hwnd, "element": description}


def detect_ui_change(description: str, *, baseline: dict | None = None) -> dict:
    """Detect whether a UI element appeared/disappeared since a baseline
    snapshot. Call inspect_once to build the baseline, then call again."""
    located = locate_ui_element(description)
    present = located.get("success")
    current = {"present": present, "count": located.get("count", 0)}
    if baseline is None:
        return {"success": True, "changed": None, "current": current, "note": "Baseline recorded -- call again to compare."}
    changed = current != baseline.get("current")
    return {"success": True, "changed": changed, "current": current, "baseline": baseline.get("current")}


def wait_for_ui(description: str, *, timeout: int = 10) -> dict:
    """Wait up to timeout seconds for a UI element to appear."""
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        located = locate_ui_element(description)
        if located.get("success"):
            return {"success": True, "waited": round(time.time() - started, 2),
                    "matches": located.get("count")}
        time.sleep(0.4)
    return {"success": False, "error": f"UI element '{description}' did not appear within {timeout}s."}
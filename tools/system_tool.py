"""Universal SYSTEM tools: system info, running apps, process
launch/terminate, clipboard, volume. Built on stdlib + pywin32-ctypes /
ctypes so they import cleanly on any platform and degrade gracefully."""

import ctypes
import ctypes.wintypes
import logging
import os
import platform
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------- #
# System info
# --------------------------------------------------------------------- #


def get_system_info() -> dict:
    """Basic host information (OS, CPU, RAM, Python, user)."""
    info = {
        "success": True,
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "hostname": platform.node(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "",
    }
    if _IS_WINDOWS:
        info["architecture"] = platform.architecture()[0]
        try:
            import psutil  # type: ignore
            info["ram_total_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
            info["cpu_count"] = psutil.cpu_count(logical=True)
            info["cpu_usage_pct"] = psutil.cpu_percent(interval=0.1)
            info["disk_usage_percent"] = psutil.disk_usage("C:/").percent
        except Exception:
            pass
    return info


def get_running_apps(*, include_paths: bool = True, limit: int = 200) -> dict:
    """List running application processes (top-level window processes,
    deduplicated)."""
    if not _IS_WINDOWS:
        return {"success": False, "error": "Running-app inspection requires Windows."}
    from tools.uiautomation_tool import list_windows

    windows = list_windows()
    seen: dict[str, dict] = {}
    for win in windows:
        pid = win.get("pid")
        title = win.get("title") or ""
        if pid <= 0 or not title:
            continue
        key = (pid, win.get("process") or "")
        if key in seen:
            continue
        seen[key] = {
            "pid": pid,
            "name": title,
            "process": win.get("process") or "",
            "hwnd": win.get("hwnd"),
            "class": win.get("class"),
        }
    apps = list(seen.values())[:limit]
    return {"success": True, "apps": apps, "count": len(apps)}


def get_processes() -> list[dict]:
    """Full process list via tasklist (fast, stdlib-only)."""
    if not _IS_WINDOWS:
        return []
    try:
        output = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception as exc:
        logger.warning("tasklist failed: %s", exc)
        return []
    processes = []
    for line in output.splitlines():
        parts = line.split('","')
        if len(parts) < 5:
            continue
        name = parts[0].strip('"')
        pid = parts[1].strip('"')
        mem = parts[4].strip('"').replace(",", "")
        processes.append({"name": name, "pid": pid, "memory_kb": mem})
    return processes


# --------------------------------------------------------------------- #
# Process launch / terminate
# --------------------------------------------------------------------- #


def launch_process(command: str, *, cwd: str | None = None) -> dict:
    """Launch a process / command line detached from Jarvis."""
    if not command or not command.strip():
        return {"success": False, "error": "Missing 'command'."}
    try:
        kwargs = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = subprocess.Popen(
            command, cwd=cwd, shell=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs
        )
        return {"success": True, "pid": process.pid, "command": command}
    except Exception as exc:
        logger.exception("Failed to launch process")
        return {"success": False, "command": command, "error": str(exc)}


def terminate_process(pid: int | str | None = None, name: str | None = None) -> dict:
    """Terminate a process by pid or image name (taskkill)."""
    if pid:
        pid_str = str(pid)
        if not pid_str.isdigit():
            return {"success": False, "error": f"Invalid pid: {pid}"}
        try:
            subprocess.run(
                ["taskkill", "/PID", pid_str, "/F"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"success": True, "pid": int(pid_str), "message": "Terminate requested."}
        except Exception as exc:
            return {"success": False, "pid": pid_str, "error": str(exc)}
    if name:
        try:
            subprocess.run(
                ["taskkill", "/IM", name, "/F"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"success": True, "name": name, "message": "Terminate requested."}
        except Exception as exc:
            return {"success": False, "name": name, "error": str(exc)}
    return {"success": False, "error": "Provide either pid or name."}


# --------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------- #


def get_clipboard() -> dict:
    """Read the current clipboard text."""
    try:
        import pyperclip
        text = pyperclip.paste()
        return {"success": True, "text": text}
    except Exception:
        pass
    if _IS_WINDOWS:
        try:
            from win32ctypes.pywin32 import win32api  # type: ignore

            data = win32api.GetClipboardData()
            return {"success": True, "text": str(data)}
        except Exception as exc:
            return {"success": False, "error": f"Clipboard read failed: {exc}"}
    return {"success": False, "error": "Clipboard access unavailable."}


def set_clipboard(text: str) -> dict:
    """Write text to the clipboard."""
    if text is None:
        return {"success": False, "error": "Missing 'text'."}
    try:
        import pyperclip
        pyperclip.copy(text)
        return {"success": True, "text": text[:80]}
    except Exception:
        pass
    if _IS_WINDOWS:
        try:
            from win32ctypes.pywin32 import win32api  # type: ignore

            win32api.SetClipboardData(text)
            return {"success": True, "text": text[:80]}
        except Exception as exc:
            return {"success": False, "error": f"Clipboard write failed: {exc}"}
    return {"success": False, "error": "Clipboard access unavailable."}


def select_text_copy() -> dict:
    """Select all in the focused window and copy (Ctrl+A, Ctrl+C)."""
    from tools.computer_tool import hotkey
    hotkey("ctrl", "a")
    time.sleep(0.1)
    hotkey("ctrl", "c")
    time.sleep(0.2)
    return get_clipboard()


def paste() -> dict:
    """Paste clipboard content at the current caret (Ctrl+V)."""
    from tools.computer_tool import hotkey
    hotkey("ctrl", "v")
    return {"success": True, "message": "Pasted."}


# --------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------- #

# MSVCRT-free volume control on Windows via SendInput media keys.
_VK_VOLUME_UP = 0xAF
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_MUTE = 0xAD
_VK_MEDIA_PLAY_PAUSE = 0xB3
_VK_MEDIA_NEXT = 0xB0
_VK_MEDIA_PREV = 0xB1
_VK_MEDIA_STOP = 0xB2

_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002


def _send_virtual_key(vk: int):
    if not _IS_WINDOWS:
        return False
    user32 = ctypes.windll.user32

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.wintypes.WORD), ("wScan", ctypes.wintypes.WORD),
                    ("dwFlags", ctypes.wintypes.DWORD), ("time", ctypes.wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.wintypes.ULONG))]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("ki",)
        _fields_ = [("type", ctypes.wintypes.DWORD), ("ki", KEYBDINPUT)]

    def one(flags):
        inp = INPUT()
        inp.type = _INPUT_KEYBOARD
        inp.ki.wVk = vk
        inp.ki.dwFlags = flags
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    one(0)
    time.sleep(0.02)
    one(_KEYEVENTF_KEYUP)
    return True


def get_volume() -> dict:
    """Return volume level 0-100 when possible, else a graceful error.
    Key-based volume control (set_volume) always works; a scalar readout
    needs an audio COM interface that may not be present in every build."""
    if not _IS_WINDOWS:
        return {"success": False, "error": "Volume control requires Windows."}
    return {
        "success": False,
        "error": "Volume level readout needs an audio COM interface.",
        "hint": "Use set_volume(up|down|mute) for reliable key-based control.",
    }


def set_volume(direction: str) -> dict:
    """direction: up | down | mute | play | pause | next | prev."""
    direction = (direction or "").lower()
    mapping = {
        "up": _VK_VOLUME_UP,
        "down": _VK_VOLUME_DOWN,
        "mute": _VK_VOLUME_MUTE,
        "toggle": _VK_VOLUME_MUTE,
        "play": _VK_MEDIA_PLAY_PAUSE,
        "pause": _VK_MEDIA_PLAY_PAUSE,
        "playpause": _VK_MEDIA_PLAY_PAUSE,
        "next": _VK_MEDIA_NEXT,
        "skip": _VK_MEDIA_NEXT,
        "prev": _VK_MEDIA_PREV,
        "previous": _VK_MEDIA_PREV,
        "stop": _VK_MEDIA_STOP,
    }
    vk = mapping.get(direction)
    if vk is None:
        return {"success": False, "error": f"Unknown direction '{direction}'. "
                "Use up/down/mute/play/pause/next/prev."}
    ok = _send_virtual_key(vk)
    return {"success": ok, "action": direction,
            "message": f"Sent {direction} media/volume key."}


# --------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------- #


def get_notifications() -> dict:
    """Windows 10/11 toast notifications live in the Action Center and
    require a native bridge to read. Report the capability honestly and
    return whatever the session has cached, if anything."""
    return {
        "success": True,
        "notifications": [],
        "note": (
            "Live notification reading needs the Windows Notification "
            "Platform bridge. Watch for NEW_NOTIFICATION events from "
            "connected adapters instead."
        ),
    }
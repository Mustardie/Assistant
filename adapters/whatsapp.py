"""WhatsApp Desktop adapter -- local UI automation of the installed
WhatsApp Desktop app. No unofficial API; messages are read via the
accessibility tree and replies are typed into the chat box."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from adapters.base import LocalAppAdapter

logger = logging.getLogger(__name__)

try:
    from tools.uiautomation_tool import list_windows, list_controls, read_visible_text, focus_window, interact_with_ui_element, locate_ui_element
    _UI = True
except Exception:  # pragma: no cover - optional
    _UI = False


class WhatsAppAdapter(LocalAppAdapter):
    name = "whatsapp"
    display_name = "WhatsApp"
    description = ("WhatsApp Desktop on this machine, driven through Windows "
                   "accessibility APIs (no unofficial API). Keep WhatsApp "
                   "running and Nova can read chats and type replies.")
    executable_names = ["WhatsApp.exe"]
    process_aliases = ["whatsapp"]
    launch_paths = [os.path.expandvars(r"%LOCALAPPDATA%\WhatsApp\WhatsApp.exe")]
    capabilities = [
        "read_messages", "send_message", "reply_to_message",
        "identify_sender",
    ]

    # ------------------------------------------------------------------ #
    def detect_app(self) -> dict:
        return _detect_process("WhatsApp.exe", ["whatsapp"])

    def status(self) -> dict:
        return super().status()

    def connect(self) -> dict:
        return super().connect()

    # ------------------------------------------------------------------ #
    def _window(self) -> dict | None:
        if not _UI:
            return None
        for win in list_windows():
            title = (win.get("title") or "").lower()
            if "whatsapp" in title:
                return win
        return None

    def read_messages(self, limit=20, **kwargs):
        window = self._window()
        if not window:
            return self._fail("WhatsApp isn't running or its window isn't visible.")
        focus_window(window.get("hwnd") or window.get("handle"))
        text = read_visible_text()
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        messages = [{"content": ln} for ln in lines[:limit]]
        return self._ok(messages=messages, count=len(messages))

    def send_message(self, recipient, text, **kwargs):
        window = self._window()
        if not window:
            return self._fail("WhatsApp isn't running.")
        hwnd = window.get("hwnd") or window.get("handle")
        focus_window(hwnd=hwnd)
        if recipient:
            # Open the search box and type the contact name.
            search = locate_ui_element("Search or start a new chat")
            if not search.get("success"):
                return self._fail("Could not find the WhatsApp search box.")
            typed = interact_with_ui_element(
                "Search or start a new chat", action="type", text=recipient)
            if not typed.get("success"):
                return typed
            time.sleep(0.6)  # let the contact list render
            contact = locate_ui_element(recipient)
            if not contact.get("success"):
                return self._fail(
                    f"No WhatsApp contact named '{recipient}' was found.")
            clicked = interact_with_ui_element(recipient)
            if not clicked.get("success"):
                return clicked
            time.sleep(0.3)
        # Type into the message box and press Enter to send.
        msg_box = locate_ui_element("Type a message")
        if not msg_box.get("success"):
            return self._fail("Could not find the message input.")
        typed = interact_with_ui_element("Type a message", action="type",
                                         text=text)
        if not typed.get("success"):
            return typed
        from tools.computer_tool import press_key
        press_key("enter")
        return self._ok(recipient=recipient)

    def reply_to_message(self, message_id, text, **kwargs):
        return self.send_message(recipient=None, text=text)

    def identify_sender(self, message, **kwargs):
        return self._ok(sender=(message or {}).get("sender"))


def _detect_process(executable: str, aliases: list[str]) -> dict:
    """Check whether an app is installed (a matching process has run before)
    and currently running."""
    installed = _is_installed(executable, aliases)
    running = _is_running(executable, aliases)
    return {"installed": installed, "running": running, "path": executable}


def _is_installed(executable: str, aliases: list[str]) -> bool:
    import shutil
    if shutil.which(executable):
        return True
    # WhatsApp Desktop installs per-user to %LOCALAPPDATA%\WhatsApp and the
    # executable is NOT on PATH, so 'which' alone reports it as missing.
    base = os.environ.get("LOCALAPPDATA", "")
    if base and (Path(base) / "WhatsApp" / executable).exists():
        return True
    # Microsoft Store variant: enumerate the AppX package registry. The
    # WindowsApps folder itself is ACL-protected, so path listing fails --
    # the AppX store keys are the reliable non-elevated source.
    try:
        import winreg
        for hive, subkey in (
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Appx\AppxAllUserStore\Applications"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"Software\Microsoft\Windows\CurrentVersion\Appx\AppxAllUserStore\Applications"),
        ):
            try:
                key = winreg.OpenKey(hive, subkey)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(key)[0]):
                if any(alias in winreg.EnumKey(key, i).lower()
                       for alias in aliases):
                    return True
    except OSError:
        pass
    # A process matching the app is proof of an install.
    return _is_running(executable, aliases)


def _is_running(executable: str, aliases: list[str]) -> bool:
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {executable}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.lower()
        if executable.lower() in out:
            return True
        # Process names differ per build (WhatsApp.exe, WhatsApp.Root.exe,
        # WhatsAppDesktop.exe) -- scan the full process list for aliases.
        full = subprocess.run(
            ["tasklist"], capture_output=True, text=True, timeout=10,
        ).stdout.lower()
        if any(alias in full for alias in aliases):
            return True
    except Exception:
        pass
    # A visible WhatsApp window counts as running even if the process name
    # differs (e.g. the Store variant).
    if _UI:
        try:
            for win in list_windows():
                if any(alias in (win.get("title") or "").lower()
                       for alias in aliases):
                    return True
        except Exception:
            pass
    return False
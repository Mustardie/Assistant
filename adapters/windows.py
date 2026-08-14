"""Windows adapter -- exposes local OS capabilities to the universal tool
layer. Always 'connected' on a Windows machine; it never needs auth."""

from __future__ import annotations

import logging

from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)


class WindowsAdapter(BaseAdapter):
    name = "windows"
    display_name = "Windows"
    description = ("Built-in access to this PC: window management, running "
                   "apps, system info, screenshots and media keys. No setup "
                   "needed.")
    authentication = "none"
    capabilities = [
        "get_running_apps", "launch_process", "terminate_process",
        "get_clipboard", "set_clipboard", "get_volume", "set_volume",
        "get_notifications", "get_active_window", "list_windows",
        "read_visible_text", "get_system_info",
    ]

    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        return {"status": "connected",
                "message": "Windows is available for local automation."}

    def connect(self) -> dict:
        return self._ok(message="Windows connected.")

    # ------------------------------------------------------------------ #
    def get_system_info(self, **kwargs):
        from tools.system_tool import get_system_info
        return get_system_info()

    def get_running_apps(self, **kwargs):
        from tools.system_tool import get_running_apps
        return get_running_apps()

    def launch_process(self, target, **kwargs):
        from tools.system_tool import launch_process
        return launch_process(target)

    def terminate_process(self, process_id, **kwargs):
        from tools.system_tool import terminate_process
        return terminate_process(process_id)

    def get_clipboard(self, **kwargs):
        from tools.system_tool import get_clipboard
        return get_clipboard()

    def set_clipboard(self, text, **kwargs):
        from tools.system_tool import set_clipboard
        return set_clipboard(text)

    def get_volume(self, **kwargs):
        from tools.system_tool import get_volume
        return get_volume()

    def set_volume(self, direction, **kwargs):
        from tools.system_tool import set_volume
        return set_volume(direction)

    def get_notifications(self, **kwargs):
        from tools.system_tool import get_notifications
        return get_notifications()

    def get_active_window(self, **kwargs):
        from tools.uiautomation_tool import get_active_window
        return get_active_window()

    def list_windows(self, **kwargs):
        from tools.uiautomation_tool import list_windows
        return list_windows()

    def read_visible_text(self, **kwargs):
        from tools.uiautomation_tool import read_visible_text
        return read_visible_text()
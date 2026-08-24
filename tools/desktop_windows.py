"""Thin, mockable Win32 window adapter. No keyboard or mouse automation."""

from __future__ import annotations

import ctypes
import os
import platform
from ctypes import wintypes
from pathlib import Path

from tools.app_catalog import resolve_known_app
from tools.desktop_models import AppStatus, WindowInfo


SW_HIDE = 0
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
WM_CLOSE = 0x0010


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _app_name(executable_path: str | None, title: str) -> str:
    executable = Path(executable_path).name if executable_path else ""
    known = resolve_known_app(executable) or resolve_known_app(title)
    if known:
        return known.canonical_name
    if executable:
        return Path(executable).stem
    suffix = title.rsplit(" - ", 1)[-1].strip()
    return suffix or "Unknown application"


class WindowsWindowBackend:
    def __init__(self, user32=None, kernel32=None):
        self.supported = platform.system().lower() == "windows"
        self._user32 = user32
        self._kernel32 = kernel32
        if self.supported and self._user32 is None:
            try:
                self._user32 = ctypes.windll.user32
                self._kernel32 = ctypes.windll.kernel32
            except (AttributeError, OSError):
                self.supported = False
        if self.supported:
            self._configure_prototypes()

    def _configure_prototypes(self) -> None:
        """Prevent 64-bit HWND/HANDLE truncation in ctypes calls."""
        try:
            prototypes = {
                "GetForegroundWindow": ([], wintypes.HWND),
                "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
                "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
                "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
                "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
                "IsIconic": ([wintypes.HWND], wintypes.BOOL),
                "IsZoomed": ([wintypes.HWND], wintypes.BOOL),
                "IsWindow": ([wintypes.HWND], wintypes.BOOL),
                "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
                "BringWindowToTop": ([wintypes.HWND], wintypes.BOOL),
                "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
                "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            }
            for name, (argtypes, restype) in prototypes.items():
                function = getattr(self._user32, name)
                function.argtypes = argtypes
                function.restype = restype
            self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self._kernel32.OpenProcess.restype = wintypes.HANDLE
            self._kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self._kernel32.CloseHandle.restype = wintypes.BOOL
            self._user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
            self._user32.GetLastInputInfo.restype = wintypes.BOOL
            if hasattr(self._kernel32, "GetTickCount64"):
                self._kernel32.GetTickCount64.argtypes = []
                self._kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        except (AttributeError, TypeError):
            # Injectable fakes do not expose ctypes function attributes.
            return

    def _process_path(self, process_id: int) -> str | None:
        if not self.supported or not self._kernel32 or not process_id:
            return None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return None
        finally:
            self._kernel32.CloseHandle(handle)

    def list_windows(self) -> list[WindowInfo]:
        if not self.supported or not self._user32:
            return []
        foreground = int(self._user32.GetForegroundWindow() or 0)
        values: list[WindowInfo] = []
        callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

        @callback_type(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(handle, _):
            if not self._user32.IsWindowVisible(handle):
                return True
            length = int(self._user32.GetWindowTextLengthW(handle) or 0)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(handle, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            process_id = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            executable = self._process_path(int(process_id.value))
            numeric_handle = int(handle)
            active = numeric_handle == foreground
            values.append(WindowInfo(
                app_name=_app_name(executable, title), executable_path=executable,
                process_id=int(process_id.value) or None, window_title=title,
                window_handle=numeric_handle, status=AppStatus.RUNNING,
                active=active, focused=active,
                minimized=bool(self._user32.IsIconic(handle)),
                maximized=bool(self._user32.IsZoomed(handle)), visible=True,
                confidence=0.98 if executable else 0.78,
                evidence=("visible top-level Win32 window", "foreground window" if active else "not foreground"),
            ))
            return True

        if not self._user32.EnumWindows(callback, 0):
            return []
        return values

    def active_window(self) -> WindowInfo | None:
        return next((window for window in self.list_windows() if window.active), None)

    def idle_seconds(self) -> float | None:
        """Return time since input, never the input itself."""
        if not self.supported or not self._user32 or not self._kernel32:
            return None
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        try:
            ticks = int(self._kernel32.GetTickCount64())
        except AttributeError:
            ticks = int(self._kernel32.GetTickCount())
        return max(0.0, (ticks - int(info.dwTime)) / 1000.0)

    def focus(self, handle: int) -> bool:
        if not self.supported or not self._user32 or not self._user32.IsWindow(handle):
            return False
        if self._user32.IsIconic(handle):
            self._user32.ShowWindow(handle, SW_RESTORE)
        self._user32.BringWindowToTop(handle)
        self._user32.SetForegroundWindow(handle)
        return int(self._user32.GetForegroundWindow() or 0) == int(handle)

    def minimize(self, handle: int) -> bool:
        if not self.supported or not self._user32 or not self._user32.IsWindow(handle):
            return False
        self._user32.ShowWindow(handle, SW_MINIMIZE)
        return bool(self._user32.IsIconic(handle))

    def maximize(self, handle: int) -> bool:
        if not self.supported or not self._user32 or not self._user32.IsWindow(handle):
            return False
        self._user32.ShowWindow(handle, SW_MAXIMIZE)
        return bool(self._user32.IsZoomed(handle))

    def restore(self, handle: int) -> bool:
        if not self.supported or not self._user32 or not self._user32.IsWindow(handle):
            return False
        self._user32.ShowWindow(handle, SW_RESTORE)
        return not bool(self._user32.IsIconic(handle)) and not bool(self._user32.IsZoomed(handle))

    def close(self, handle: int) -> bool:
        """Request a normal close; never force-terminate the process."""
        if not self.supported or not self._user32 or not self._user32.IsWindow(handle):
            return False
        return bool(self._user32.PostMessageW(handle, WM_CLOSE, 0, 0))

    def window_exists(self, handle: int) -> bool:
        return bool(self.supported and self._user32 and self._user32.IsWindow(handle))


def windows_dependency_status() -> tuple[bool, str]:
    backend = WindowsWindowBackend()
    if backend.supported:
        return True, "Win32 window APIs available through Python ctypes"
    return False, f"Desktop awareness requires Windows; current platform is {platform.system() or os.name}"

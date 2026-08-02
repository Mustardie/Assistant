"""Skill recorder: captures a live demonstration as SEMANTIC input events.

What is captured:
    - mouse movement / clicks (poll thread + global hooks)
    - keyboard presses (global hook)
    - active window title + application executable (ctypes, no deps)
    - screenshots every 300-500 ms (mss, async thread, compressed JPEG)
    - OCR text + UI element locations (async vision worker, cached)

Raw coordinates ARE stored -- but only as a debugging/fallback layer. The
primary representation is events (click/key/window/scroll) plus UI frames
that the parser later turns into semantic actions referencing UI objects.

Design constraints:
    - recording must not lag the system: hooks are cheap callbacks, the
      mouse is polled at ~20 Hz, screenshots and vision run on their own
      threads, frames are saved as quality-70 JPEGs
    - nothing blocking happens on the assistant thread
    - the clipboard is NEVER touched (no secrets can leak via it)
    - pause/resume/cancel are thread-safe
"""

import logging
import threading
import time
from pathlib import Path

from config.settings import settings
from skills import matcher
from skills.storage import write_json

logger = logging.getLogger(__name__)

MOUSE_MOVE_MIN_DISTANCE = 4          # px -- ignore sub-pixel jitter
MOUSE_MOVE_MAX_FREQUENCY = 0.12      # s -- at most ~8 movement events/sec
CLICK_GROUP_WINDOW = 0.35            # s
KEYBOARD_MODIFIERS = {
    "ctrl", "left ctrl", "right ctrl", "alt", "left alt", "right alt",
    "shift", "left shift", "right shift", "win", "left win", "right win",
    "cmd", "left cmd", "right cmd", "alt gr",
}


# --------------------------------------------------------------------- #
# Window introspection (ctypes only -- no extra dependencies)
# --------------------------------------------------------------------- #

def active_window_info() -> dict:
    """Current foreground window: {"title", "app", "exe"}. Empty strings
    when no window is available (e.g. a lock screen)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"title": "", "app": "", "exe": ""}
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value or ""

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if handle:
            size = wintypes.DWORD(2048)
            exe_buffer = ctypes.create_unicode_buffer(2048)
            if kernel32.QueryFullProcessImageNameW(handle, 0, exe_buffer, ctypes.byref(size)):
                exe = exe_buffer.value or ""
            kernel32.CloseHandle(handle)
        app = Path(exe).name if exe else ""
        return {"title": title, "app": app, "exe": exe}
    except Exception:
        return {"title": "", "app": "", "exe": ""}


def open_windows() -> list[dict]:
    """Visible windows (title + app) for playback's window-recovery step."""
    windows = []
    try:
        import pygetwindow

        for win in pygetwindow.getAllWindows():
            title = win.title
            if not title:
                continue
            windows.append({"title": title, "app": ""})
    except Exception:
        logger.debug("pygetwindow unavailable -- window recovery limited", exc_info=True)
    return windows


def activate_window(title_fragment: str) -> bool:
    """Bring the first window whose title contains the fragment to the
    foreground. Returns True on success."""
    try:
        import pygetwindow

        matches = pygetwindow.getWindowsWithTitle(title_fragment)
        if not matches:
            return False
        window = matches[0]
        try:
            window.restore()
        except Exception:
            pass
        window.activate()
        time.sleep(0.3)
        return True
    except Exception:
        logger.debug("Could not activate window %r", title_fragment, exc_info=True)
        return False


def current_screen_size() -> dict:
    """Physical pixel size of the primary monitor."""
    try:
        import mss

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            return {"width": monitor["width"], "height": monitor["height"]}
    except Exception:
        return {"width": 0, "height": 0}


# --------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------- #

class Recorder:
    """A recording session. Call start(), then stop()/pause()/resume()/
    cancel(). All capture happens on daemon threads."""

    def __init__(
        self,
        session_dir: Path,
        *,
        capture_interval_ms: int | None = None,
        screenshot_quality: int | None = None,
        vision_enabled: bool = True,
    ):
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.capture_interval = (capture_interval_ms or settings.skills_capture_interval_ms) / 1000.0
        self.quality = screenshot_quality or settings.skills_screenshot_quality

        self._events: list[dict] = []
        self._events_lock = threading.Lock()
        self._screenshots: list[dict] = []
        self._vision_index: dict[str, dict] = {}   # ts -> analysis frame
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self._recording = False

        self._vision_thread = None
        self._capture_thread = None
        self._mouse_thread = None
        self._hook_handle = None
        self._mouse_pos = (0, 0)
        self._last_move_time = 0.0
        self._last_click_time = 0.0
        self._last_window = None
        self._frame_counter = 0

    # -- lifecycle ---------------------------------------------------- #

    def start(self) -> None:
        if self._recording:
            return
        self._recording = True
        self._stop_event.clear()
        self._paused.clear()
        self._events.clear()
        self._screenshots.clear()
        self._frame_counter = 0

        initial_window = active_window_info()
        self._last_window = initial_window
        self._append_event({
            "t": time.time(), "type": "window",
            "title": initial_window.get("title", ""),
            "app": initial_window.get("app", ""),
        })

        self._vision_thread = threading.Thread(
            target=self._vision_loop, name="skills-vision", daemon=True
        )
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="skills-capture", daemon=True
        )
        self._mouse_thread = threading.Thread(
            target=self._mouse_loop, name="skills-mouse", daemon=True
        )
        self._vision_thread.start()
        self._capture_thread.start()
        self._mouse_thread.start()
        self._install_keyboard_hook()
        logger.info("Recording started (session dir: %s)", self.session_dir)

    def stop(self) -> dict:
        """Stop recording and return the full capture. Safe to call when
        already stopped."""
        if not self._recording:
            return self._collect()
        self._recording = False
        self._stop_event.set()
        self._paused.clear()  # ensure threads can exit even if paused
        for thread in (self._capture_thread, self._mouse_thread, self._vision_thread):
            if thread is not None:
                thread.join(timeout=3.0)
        self._uninstall_keyboard_hook()
        return self._collect()

    def cancel(self) -> None:
        self.stop()

    def pause(self) -> None:
        if self._recording:
            self._paused.set()
            self._append_event({"t": time.time(), "type": "pause"})

    def resume(self) -> None:
        if self._recording:
            self._paused.clear()
            self._append_event({"t": time.time(), "type": "resume"})

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_paused(self) -> bool:
        return self._recording and self._paused.is_set()

    # -- event log ---------------------------------------------------- #

    def _append_event(self, event: dict) -> None:
        with self._events_lock:
            self._events.append(event)

    def _collect(self) -> dict:
        with self._events_lock:
            events = list(self._events)
            screenshots = list(self._screenshots)
        return {
            "events": events,
            "screenshots": screenshots,
            "vision": dict(self._vision_index),
            "screen": {
                "width": (screenshots[-1].get("width") if screenshots else 0)
                or current_screen_size().get("width", 0),
                "height": (screenshots[-1].get("height") if screenshots else 0)
                or current_screen_size().get("height", 0),
                "scale": matcher.monitor_scaling(),
            },
            "session_dir": str(self.session_dir),
            "duration_s": round(time.time() - (events[0]["t"] if events else time.time()), 2),
        }

    # -- keyboard / mouse hooks ---------------------------------------- #

    def _install_keyboard_hook(self) -> None:
        try:
            import keyboard

            self._hook_handle = keyboard.hook(self._on_keyboard_event)
        except Exception as exc:
            logger.warning("Could not install input hook: %s", exc)

    def _uninstall_keyboard_hook(self) -> None:
        if self._hook_handle is None:
            return
        try:
            import keyboard

            keyboard.unhook(self._hook_handle)
        except Exception:
            pass
        self._hook_handle = None

    def _on_keyboard_event(self, event) -> None:
        if not self._recording or self._paused.is_set():
            return
        name = (event.name or "").strip()
        if not name:
            return
        event_type = getattr(event, "event_type", "down")
        if event_type not in ("down", "up"):
            return
        lower = name.lower()

        if "button" in lower:
            self._record_mouse_button(lower, event_type)
            return
        if lower.startswith("scroll "):
            if event_type == "down":
                self._append_event({
                    "t": time.time(), "type": "scroll",
                    "amount": 1 if "up" in lower else -1,
                })
            return

        with self._events_lock:
            self._events.append({
                "t": time.time(), "type": "key",
                "action": event_type, "name": name,
            })

    def _record_mouse_button(self, name: str, event_type: str) -> None:
        button = {"left button": "left", "right button": "right",
                  "middle button": "middle"}.get(name, name)
        if event_type == "down":
            x, y = self._mouse_pos
            now = time.time()
            group = int(round(now / CLICK_GROUP_WINDOW))
            self._append_event({
                "t": now, "type": "click", "button": button,
                "x": x, "y": y, "group": group,
            })
            self._last_click_time = now

    def _mouse_loop(self) -> None:
        try:
            import pyautogui
        except ImportError:
            logger.warning("pyautogui unavailable -- mouse movement not recorded")
            return
        interval = 1.0 / max(1, settings.skills_mouse_poll_hz)
        while not self._stop_event.is_set():
            if not self._paused.is_set():
                try:
                    x, y = pyautogui.position()
                except Exception:
                    x, y = 0, 0
                self._mouse_pos = (x, y)
                now = time.time()
                if (
                    x != 0 or y != 0
                ) and now - self._last_move_time >= MOUSE_MOVE_MAX_FREQUENCY:
                    last = self._events[-1] if self._events else None
                    if (
                        last is None
                        or last.get("type") != "mouse_move"
                        or abs(last.get("x", x) - x) + abs(last.get("y", y) - y) > MOUSE_MOVE_MIN_DISTANCE
                    ):
                        self._append_event({
                            "t": now, "type": "mouse_move", "x": x, "y": y,
                        })
                        self._last_move_time = now
            self._stop_event.wait(interval)

    # -- screenshots ---------------------------------------------------- #

    def _capture_loop(self) -> None:
        try:
            import mss
        except ImportError:
            logger.warning("mss unavailable -- screenshots not recorded")
            return
        try:
            from PIL import Image
        except ImportError:
            return
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            while not self._stop_event.is_set():
                if not self._paused.is_set():
                    try:
                        shot = sct.grab(monitor)
                        image = Image.frombytes("RGB", shot.size, shot.rgb)
                        self._save_frame(image)
                    except Exception as exc:
                        logger.debug("Screenshot capture failed: %s", exc)
                self._stop_event.wait(self.capture_interval)

    def _save_frame(self, image) -> None:
        self._frame_counter += 1
        ts = time.time()
        filename = f"{self._frame_counter:06d}.jpg"
        path = self.session_dir / "screenshots" / filename
        try:
            image.save(path, "JPEG", quality=self.quality, optimize=True)
        except Exception as exc:
            logger.debug("Could not save frame: %s", exc)
            return
        self._screenshots.append({
            "t": ts, "file": f"screenshots/{filename}",
            "width": image.width, "height": image.height,
        })

    # -- async vision worker -------------------------------------------- #

    def _vision_loop(self) -> None:
        from skills.vision import screen_vision

        # Track which screenshots still need analysis.
        analyzed = 0
        while not self._stop_event.is_set():
            pending = self._screenshots[analyzed:]
            if not pending:
                self._stop_event.wait(0.25)
                continue
            entry = pending[0]
            image = self._load_frame(entry)
            if image is not None:
                try:
                    frame = screen_vision.analyze(image)
                except Exception as exc:
                    logger.debug("Vision analysis failed: %s", exc)
                    frame = {"width": image.width, "height": image.height,
                             "objects": [], "text": ""}
                frame.setdefault("width", image.width)
                frame.setdefault("height", image.height)
                ts_key = f"{entry['t']:.3f}"
                self._vision_index[ts_key] = frame
                vision_file = self.session_dir / "vision" / (
                    Path(entry["file"]).stem + ".json"
                )
                write_json(vision_file, {"ts": entry["t"], **frame})
            analyzed += 1
            if analyzed % 4 == 0:
                self._stop_event.wait(0.01)

    @staticmethod
    def _load_frame(entry: dict):
        try:
            from PIL import Image

            path = Path(entry.get("file", ""))
            return Image.open(path).convert("RGB")
        except Exception:
            return None

    # -- helpers for the parser ----------------------------------------- #

    def vision_frame_near(self, ts: float, window: float = 2.5) -> dict | None:
        """Best vision analysis available around a timestamp: the closest
        frame within `window` seconds."""
        best = None
        best_delta = None
        for key, frame in self._vision_index.items():
            delta = abs(float(key) - ts)
            if delta <= window and (best_delta is None or delta < best_delta):
                best_delta = delta
                best = frame
        return best

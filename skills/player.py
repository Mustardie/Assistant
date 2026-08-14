"""Skill player: replays a recorded skill with vision-guided adaptation.

Principles:
    - NEVER blindly replay coordinates. Every interactive step first looks
      up its expected UI object (text + type + relative position) in the
      current screen's detected objects.
    - Adaptation ladder when the UI shifted or the resolution changed:
        1. direct match (text+type+position)
        2. expanded nearby search (position weight dropped)
        3. vision-based locate ("find the Login button")
        4. recorded coordinates (scaled) -- final option, always logged
        5. pause and ask the user (missing element / unexpected screen)
    - Continuous verification: after actions, the screen is re-checked for
      the next expected UI; unexpected windows/dialogs pause playback.
    - Every step is logged with expected UI, detected UI, confidence,
      recovery strategy and time taken (debugging-friendly audit trail).

The session is a resumable state machine: step_until_blocked() returns
"need_input" whenever the user's input is required (variable values,
confirmations), and answer() feeds the reply back in.
"""

import logging
import re
import time
from pathlib import Path

from config.settings import settings
from skills import matcher, storage
from skills.vision import screen_vision

logger = logging.getLogger(__name__)

# Answer keywords for confirmation prompts.
_SKIP_WORDS = ("skip", "skip it", "next", "skip this", "continue anyway")
_STOP_WORDS = ("stop", "cancel", "abort", "quit", "no")

_BROWSER_EXES = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe")


class PlaybackError(Exception):
    """Non-recoverable playback failure."""


class PlaybackSession:
    """One skill playback. Create via SkillManager.play()."""

    def __init__(self, name: str, *, verify: bool = True):
        self.name = name
        self.verify = verify
        self.timeline = storage.load_timeline(name) or {}
        self.metadata = storage.load_metadata(name) or {}
        self.skill = storage.load_skill(name) or {"name": name}
        self.screen = self.timeline.get("screen") or {}
        self.recorded_steps = self.timeline.get("semantic") or []
        self.variables = self.timeline.get("variables") or []
        self.instructions = list(self.timeline.get("instructions") or [])

        self.status = "running"
        self._step_index = 0
        self._values: dict[str, str] = {}
        self._pending_question: str | None = None
        self._pending_step_index: int | None = None
        self._current_analysis: dict = {}
        self._current_image = None
        self._prev_point = None
        self._last_click_ts = 0.0
        self._actual_steps: list[dict] = []
        self._step_logs: list[dict] = []
        self._started_at = time.time()
        self.improvement_offer = False
        self.failure_reason: str | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def pending_question(self) -> str | None:
        return self._pending_question

    @property
    def step_index(self) -> int:
        return self._step_index

    @property
    def total_steps(self) -> int:
        return len(self.recorded_steps)

    def step_until_blocked(self) -> str:
        """Run steps until the user's input is required or playback ends.
        Returns one of: done | need_input | failed | cancelled."""
        if self.status == "cancelled":
            return "cancelled"
        if self.status == "failed":
            return "failed"
        while self._step_index < len(self.recorded_steps):
            step = self.recorded_steps[self._step_index]

            if self._needs_variable_input(step):
                self._pending_question = self._build_variable_question(step)
                self._pending_step_index = self._step_index
                self.status = "need_input"
                return "need_input"

            outcome = self._execute_step(step)
            self._step_logs.append(self._make_log(step, outcome))
            self._actual_steps.append(dict(step))
            self._step_index += 1
            self._pending_question = None
            self._pending_step_index = None

            if outcome.get("interrupted"):
                self.status = "need_input"
                return "need_input"
            if outcome.get("skipped"):
                self._step_logs[-1]["skipped"] = True

        self._finish()
        return self.status

    def answer(self, text: str) -> None:
        """Feed the user's reply to a pending question and resume."""
        if self.status != "need_input" or not self._pending_question:
            return
        text = (text or "").strip()
        step = None
        if self._pending_step_index is not None and 0 <= self._pending_step_index < len(self.recorded_steps):
            step = self.recorded_steps[self._pending_step_index]

        if self._pending_input_kind == "variable":
            variable = step.get("variable") or {}
            value = self._normalize_variable_value(variable, text)
            if value is None:
                self._pending_question = (
                    f"That doesn't look like a {variable.get('type', 'value')}. "
                    + self._build_variable_question(step)
                )
                return
            self._values[variable.get("name")] = value
        elif self._pending_input_kind == "confirm":
            lowered = text.lower()
            if any(word in lowered for word in _STOP_WORDS):
                self.status = "cancelled"
            elif any(word in lowered for word in _SKIP_WORDS):
                self._skip_requested = True
            else:
                self._continue_requested = True

        self.status = "running"
        self._pending_question = None
        self._pending_input_kind = None
        self._pending_step_index = None

    def cancel(self) -> None:
        if self.status == "running":
            self.status = "cancelled"
            self._pending_question = None

    def resolve_text(self, step: dict) -> str:
        """Substitute {{Variable}} placeholders in a step's text."""
        text = step.get("text", "")
        for name, value in self._values.items():
            text = text.replace("{{" + name + "}}", value)
        return text

    # ------------------------------------------------------------------ #
    # Variable input
    # ------------------------------------------------------------------ #

    def _needs_variable_input(self, step: dict) -> bool:
        if not step.get("is_variable"):
            return False
        variable = step.get("variable") or {}
        return bool(variable.get("name")) and variable["name"] not in self._values

    # Names that are just a type fallback ("Value", "Text", "Number2", ...)
    # rather than a real field label -- these should never be echoed back
    # to the user verbatim (e.g. "What should the Text be?").
    _GENERIC_VARIABLE_NAMES = re.compile(
        r"^(?:Value|Text|Number|Date|URL|File|Folder)\d*$"
    )

    def _variable_subject(self, step: dict, variable: dict) -> str:
        """A human-friendly name for what we're asking about: the real
        field label when we have one, otherwise the on-screen target text,
        otherwise a neutral fallback -- never a generic internal name like
        'Value1'."""
        name = (variable.get("name") or "").strip()
        if name and not self._GENERIC_VARIABLE_NAMES.match(name):
            return name
        target = (step.get("target") or "").strip()
        if target and target not in ("text field", "password field"):
            return target
        return "value"

    def _build_variable_question(self, step: dict) -> str:
        variable = step.get("variable") or {}
        vtype = variable.get("type", "text")
        example = variable.get("example") or ""
        if vtype == "password":
            field_label = (step.get("target") or "").strip()
            subject = field_label if field_label else "password"
            return (
                f"For this skill I need the {subject} value that was entered during "
                f"recording. What should I type? I will not store it."
            )
        subject = self._variable_subject(step, variable)
        prompts = {
            "text": f"What should the {subject} be?",
            "number": f"What number should the {subject} be?",
            "date": f"What date should the {subject} be?",
            "url": f"What URL should the {subject} be?",
            "path": f"Which file should I use for the {subject}?",
            "folder": f"Which folder should I use for the {subject}?",
        }
        question = prompts.get(vtype, prompts["text"])
        if example:
            question += f" (last time: {example})"
        return question

    def _normalize_variable_value(self, variable: dict, text: str) -> str | None:
        if not text:
            return None
        vtype = variable.get("type")
        if vtype == "number" and not text.replace(".", "", 1).isdigit():
            return None
        if vtype == "url" and not (text.startswith("http://") or text.startswith("https://")):
            if "." not in text and "localhost" not in text:
                return None
            text = "https://" + text
        if vtype in ("path", "folder"):
            text = str(Path(text).expanduser())
        return text

    # ------------------------------------------------------------------ #
    # Step execution
    # ------------------------------------------------------------------ #

    def _execute_step(self, step: dict) -> dict:
        started = time.perf_counter()
        self._skip_requested = False
        self._continue_requested = False
        self._pending_input_kind = None

        stype = step.get("type", "")
        try:
            if stype == "ensure_app":
                outcome = self._ensure_app(step)
            elif stype in ("click", "double_click", "right_click"):
                outcome = self._click_step(step)
            elif stype == "type":
                outcome = self._type_step(step)
            elif stype == "press":
                outcome = self._press_step(step)
            elif stype == "hotkey":
                outcome = self._hotkey_step(step)
            elif stype == "scroll":
                outcome = self._scroll_step(step)
            elif stype == "wait":
                outcome = self._wait_step(step)
            else:
                outcome = {"success": True, "strategy": "noop"}
            outcome["time_taken_s"] = round(time.perf_counter() - started, 3)
            return outcome
        except PlaybackError as exc:
            self.failure_reason = str(exc)
            self.status = "failed"
            return {"success": False, "strategy": "none", "interrupted": False,
                    "error": str(exc)}
        except Exception as exc:
            logger.exception("Skill step failed unexpectedly")
            self.failure_reason = str(exc)
            self.status = "failed"
            return {"success": False, "strategy": "none", "interrupted": False,
                    "error": str(exc)}

    # -- screen capture + analysis ------------------------------------- #

    def _capture_analysis(self) -> dict:
        """Capture the current screen and analyze it. Cached per step so
        repeated lookups inside one step are free."""
        if self._current_analysis:
            return self._current_analysis
        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                monitor = sct.monitors[1]
                shot = sct.grab(monitor)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
        except Exception as exc:
            raise PlaybackError(f"Could not capture the screen: {exc}")
        self._current_image = image
        self._current_analysis = screen_vision.analyze(image)
        return self._current_analysis

    def _reset_analysis(self) -> None:
        self._current_analysis = {}
        self._current_image = None

    def _current_size(self) -> dict:
        frame = self._current_analysis
        return {"width": frame.get("width") or 0, "height": frame.get("height") or 0}

    # -- window handling ------------------------------------------------ #

    def _ensure_app(self, step: dict) -> dict:
        from skills.recorder import activate_window, active_window_info

        expected = step.get("window") or {}
        expected_title = expected.get("title") or ""
        expected_app = expected.get("app") or ""
        current = active_window_info()

        if self._window_matches(current, expected):
            self._capture_analysis()
            return {"success": True, "strategy": "window_already_active",
                    "window": current}

        if expected_title:
            activated = activate_window(expected_title[:80])
            if activated:
                self._reset_analysis()
                time.sleep(0.4)
                return {"success": True, "strategy": "window_activated",
                        "window": active_window_info()}
        if expected_app:
            activated = activate_window(expected_app.rsplit(".", 1)[0][:80])
            if activated:
                self._reset_analysis()
                time.sleep(0.4)
                return {"success": True, "strategy": "window_activated_by_app",
                        "window": active_window_info()}

        return self._pause_for_input(
            f"I need to switch to {expected_title or expected_app or 'the recorded application'}, "
            "but I can't find that window. Please open it, then tell me to continue.",
            kind="confirm", step=step,
        )

    @staticmethod
    def _window_matches(current: dict, expected: dict) -> bool:
        if not current:
            return False
        if expected.get("app") and current.get("app"):
            return current["app"].lower() == expected["app"].lower()
        if expected.get("title") and current.get("title"):
            a = expected["title"].lower().strip()
            b = current["title"].lower().strip()
            return a in b or b in a
        return False

    # -- click ---------------------------------------------------------- #

    def _click_step(self, step: dict) -> dict:
        analysis = self._capture_analysis()
        objects = analysis.get("objects") or []
        frame_size = analysis
        expected = {
            "text": step.get("target", "") or (step.get("ui_object") or {}).get("text", ""),
            "type": (step.get("ui_object") or {}).get("type", "button"),
        }
        relative = tuple(step.get("relative") or ()) if step.get("relative") else None

        match = matcher.match_element(
            expected, objects,
            expected_rel=relative, frame_size=frame_size, prev_point=self._prev_point,
        )
        strategy = match["strategy"]
        confidence = match["confidence"]
        element = match["element"]

        if not element:
            nearby = matcher.find_nearby(
                expected, objects, point=relative, frame_size=frame_size,
                previous_element=self._prev_point,
            )
            if nearby["found"]:
                element, strategy, confidence = nearby["element"], nearby["strategy"], nearby["confidence"]

        if not element:
            located = screen_vision.locate(
                self._current_image,
                self._describe_expected(expected, step),
                context=" ".join(self.instructions[:3]) if self.instructions else None,
            )
            if located:
                element, strategy, confidence = located, "vision_locate", located.get("confidence", 0.6)

        if not element:
            return self._coordinate_fallback(step, expected, "click")

        if self._is_browser_channel(step):
            browser_outcome = self._browser_click(step, expected)
            if browser_outcome.get("success"):
                browser_outcome["strategy"] = strategy
                browser_outcome["confidence"] = confidence
                self._post_action_verify(step)
                return browser_outcome

        return self._desktop_click(element, step, strategy, confidence, expected)

    def _desktop_click(self, element: dict, step: dict, strategy: str, confidence: float,
                       expected: dict) -> dict:
        bbox = element.get("bbox")
        if not bbox:
            return self._coordinate_fallback(step, expected, "click")
        scale = matcher.monitor_scaling()
        cx, cy = bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2
        x, y = cx / scale, cy / scale
        self._perform_click(x, y, step.get("type", "click"))
        self._prev_point = (x, y)
        self._post_action_verify(step)
        return {"success": True, "strategy": strategy, "confidence": confidence,
                "point": [round(x, 1), round(y, 1)]}

    def _perform_click(self, x: float, y: float, click_type: str) -> None:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.moveTo(x, y, duration=0.15)
        time.sleep(0.05)
        if click_type == "double_click":
            pyautogui.doubleClick()
        elif click_type == "right_click":
            pyautogui.rightClick()
        else:
            pyautogui.click()
        self._last_click_ts = time.time()

    # -- coordinate fallback (last resort) ------------------------------- #

    def _coordinate_fallback(self, step: dict, expected: dict, action_label: str) -> dict:
        coords = step.get("coords")
        if not coords:
            return self._pause_for_input(
                self._missing_element_message(step, expected, action_label),
                kind="confirm", step=step,
            )
        recorded_scale = self.screen.get("scale") or 1.0
        current_scale = matcher.monitor_scaling()
        x = coords[0] * (current_scale / recorded_scale)
        y = coords[1] * (current_scale / recorded_scale)
        self._perform_click(x, y, step.get("type", "click"))
        self._prev_point = (x, y)
        self._post_action_verify(step)
        return {
            "success": True, "strategy": "coordinate_fallback",
            "confidence": 0.0, "point": [round(x, 1), round(y, 1)],
        }

    def _missing_element_message(self, step: dict, expected: dict, action_label: str) -> str:
        screen_hint = self._screen_summary()
        target = self._describe_expected(expected, step)
        instruction_hint = ""
        if self.instructions:
            instruction_hint = (
                "\nInstructions you gave when recording this skill: "
                + " ".join(self.instructions[:3])
            )
        return (
            f"I'm stuck on step {self._step_index + 1} of {self.total_steps}: "
            f"I need to {action_label} {target}, but I can't find it on the screen. "
            f"Right now I can see: {screen_hint}.{instruction_hint} "
            "Should I skip this step, stop, or will the element appear if I wait? "
            "(say 'skip' / 'stop' / 'wait a moment')"
        )

    def _screen_summary(self) -> str:
        analysis = self._current_analysis
        texts = []
        for obj in (analysis.get("objects") or [])[:8]:
            label = obj.get("text") or obj.get("type")
            if label and label not in texts:
                texts.append(label)
        if not texts:
            texts = ["an unfamiliar screen"]
        return ", ".join(texts[:6])

    # -- typing ---------------------------------------------------------- #

    def _type_step(self, step: dict) -> dict:
        text = self.resolve_text(step)
        expected = {
            "text": step.get("target", "") or "",
            "type": "input",
        }
        if self._is_browser_channel(step):
            browser_outcome = self._browser_type(step, text)
            if browser_outcome.get("success"):
                return browser_outcome

        # Find the field (only when we have a label; otherwise the field
        # was already focused by the preceding click step).
        if expected["text"]:
            analysis = self._capture_analysis()
            match = matcher.match_element(
                expected, analysis.get("objects") or [],
                expected_rel=tuple(step.get("relative") or ()) if step.get("relative") else None,
                frame_size=analysis,
            )
            if match["found"]:
                bbox = match["element"]["bbox"]
                scale = matcher.monitor_scaling()
                x, y = (bbox[0] + bbox[2] / 2) / scale, (bbox[1] + bbox[3] / 2) / scale
                self._perform_click(x, y, "click")
                self._prev_point = (x, y)

        self._type_text(text, step)
        self._post_action_verify(step)
        return {"success": True, "strategy": "typed", "confidence": 1.0}

    def _type_text(self, text: str, step: dict) -> None:
        import pyautogui

        try:
            pyautogui.write(text, interval=0.02)
        except Exception:
            # pyautogui cannot type characters outside its key map -- paste.
            try:
                import pyperclip

                previous = pyperclip.paste()
                pyperclip.copy(text)
                pyautogui.hotkey("ctrl", "v")
                if not (step.get("masked") or (step.get("variable") or {}).get("type") == "password"):
                    pyperclip.copy(previous)
            except Exception:
                pyautogui.write(str(text.encode("ascii", "ignore").decode()), interval=0.02)

    # -- keys ------------------------------------------------------------ #

    def _press_step(self, step: dict) -> dict:
        import pyautogui

        key = step.get("key", "enter")
        if self._is_browser_channel(step):
            try:
                from tools.tool_registry import run_tool

                ok, result = run_tool("browser_press_key", {"key": key})
                if ok and (not isinstance(result, dict) or result.get("success") is not False):
                    return {"success": True, "strategy": "browser"}
            except Exception:
                pass
        pyautogui.press(key)
        return {"success": True, "strategy": "press"}

    def _hotkey_step(self, step: dict) -> dict:
        import pyautogui

        keys = list(step.get("keys") or [])
        if not keys:
            return {"success": True, "strategy": "noop"}
        pyautogui.hotkey(*keys)
        return {"success": True, "strategy": "hotkey"}

    def _scroll_step(self, step: dict) -> dict:
        import pyautogui

        pyautogui.scroll(int(step.get("amount", 0)))
        return {"success": True, "strategy": "scroll"}

    def _wait_step(self, step: dict) -> dict:
        time.sleep(min(float(step.get("seconds", 0)), 30.0))
        return {"success": True, "strategy": "wait"}

    # -- browser channel -------------------------------------------------- #

    def _is_browser_channel(self, step: dict) -> bool:
        window = step.get("window") or {}
        app = (window.get("app") or "").lower()
        if app in _BROWSER_EXES:
            return True
        title = (window.get("title") or "").lower()
        return any(name in title for name in ("chrome", "edge", "firefox", "brave", "opera"))

    def _browser_click(self, step: dict, expected: dict) -> dict:
        try:
            from tools.tool_registry import run_tool

            description = self._describe_expected(expected, step)
            ok, result = run_tool("browser_click", {"description": description})
            if ok and (not isinstance(result, dict) or result.get("success") is not False):
                return {"success": True, "strategy": "browser"}
        except Exception:
            pass
        return {"success": False}

    def _browser_type(self, step: dict, text: str) -> dict:
        try:
            from tools.tool_registry import run_tool

            description = step.get("target") or "the field"
            ok, result = run_tool(
                "browser_type",
                {"description": description, "text": text, "clear_first": False},
            )
            if ok and (not isinstance(result, dict) or result.get("success") is not False):
                return {"success": True, "strategy": "browser"}
        except Exception:
            pass
        return {"success": False}

    # -- verification ----------------------------------------------------- #

    def _post_action_verify(self, step: dict) -> None:
        if not self.verify:
            return
        try:
            time.sleep(0.6)  # let the UI settle
            self._reset_analysis()
            analysis = self._capture_analysis()
            next_step = self.recorded_steps[self._step_index + 1] if self._step_index + 1 < len(self.recorded_steps) else None
            if next_step is None or next_step.get("type") not in ("click", "type"):
                return
            expected_text = (next_step.get("ui_object") or {}).get("text") or next_step.get("target") or ""
            if not expected_text:
                return
            match = matcher.match_element(
                {"text": expected_text, "type": "button"},
                analysis.get("objects") or [],
                expected_rel=tuple(next_step.get("relative") or ()) if next_step.get("relative") else None,
                frame_size=analysis,
            )
            if not match["found"]:
                self._pause_for_input(
                    f"I just performed a step, but I'm not seeing what I expected next "
                    f"({expected_text}). The screen shows: {self._screen_summary()}. "
                    "Should I continue anyway, skip this step, or stop?",
                    kind="confirm", step=next_step,
                )
        except Exception:
            logger.debug("Post-action verification skipped", exc_info=True)

    # -- user input ------------------------------------------------------- #

    def _pause_for_input(self, message: str, *, kind: str, step: dict | None = None) -> dict:
        self._pending_question = message
        self._pending_input_kind = kind
        self._pending_step_index = self._step_index
        self.status = "need_input"
        return {"success": True, "interrupted": True, "strategy": "paused_user"}

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _describe_expected(expected: dict, step: dict) -> str:
        target = expected.get("text") or ""
        if target:
            return f"the '{target}' element"
        coords = step.get("coords")
        if coords:
            return f"the element near ({int(coords[0])}, {int(coords[1])})"
        return "the next element"

    def _make_log(self, step: dict, outcome: dict) -> dict:
        expected_ui = None
        if step.get("type") in ("click", "double_click", "right_click", "type"):
            expected_ui = {
                "text": step.get("target", ""),
                "type": (step.get("ui_object") or {}).get("type", "button"),
                "relative": step.get("relative"),
            }
        return {
            "step_index": self._step_index,
            "step_type": step.get("type"),
            "expected_ui": expected_ui,
            "detected_ui": outcome.get("element"),
            "confidence": outcome.get("confidence"),
            "strategy": outcome.get("strategy"),
            "time_taken_s": outcome.get("time_taken_s"),
            "success": outcome.get("success"),
            "skipped": outcome.get("skipped", False),
        }

    def _finish(self) -> None:
        if self.status == "cancelled":
            result = "cancelled"
        else:
            result = "success"
            recorded_actions = [s for s in self.recorded_steps if s.get("type") not in ("wait", "ensure_app")]
            actual_actions = [s for s in self._actual_steps if s.get("type") not in ("wait", "ensure_app")]
            if len(actual_actions) < len(recorded_actions) and len(actual_actions) > 0:
                self.improvement_offer = True
        storage.update_last_used(self.name, outcome=result)
        storage.write_playback_log(self.name, {
            "skill": self.name,
            "result": result,
            "total_time_s": round(time.time() - self._started_at, 2),
            "steps_completed": self._step_index,
            "steps_total": len(self.recorded_steps),
            "screen": self._current_size() or None,
            "failure_reason": self.failure_reason,
            "improvement_offer": self.improvement_offer,
            "step_logs": self._step_logs,
        })
        logger.info("[Skills] Playback of '%s' finished with result=%s", self.name, result)

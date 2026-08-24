"""Deterministic safety planner for natural-language desktop requests."""

from __future__ import annotations

import re
from pathlib import Path

from tools.app_catalog import KNOWN_APPS, normalize_app_name
from tools.desktop_file_intent import choose_app_for_path
from tools.desktop_models import AppAction, DesktopActionPlan, DesktopRisk


_PATH = re.compile(r'(?:"([A-Za-z]:[\\/][^"]+)"|\b([A-Za-z]:[\\/][^\n]+?)(?=$|\s+(?:in|with|using)\s+))', re.I)


def _mentioned_app(text: str) -> str | None:
    normalized = f" {normalize_app_name(text)} "
    matches: list[tuple[int, str]] = []
    for app in KNOWN_APPS:
        for alias in (app.canonical_name, *app.aliases):
            key = normalize_app_name(alias)
            if f" {key} " in normalized:
                matches.append((len(key), app.canonical_name))
    return max(matches, default=(0, None))[1]


class DesktopActionPlanner:
    def plan(self, request: str, *, target_path: str | None = None) -> DesktopActionPlan:
        text = str(request or "").strip()
        lowered = text.lower()
        path_match = _PATH.search(text)
        path = target_path or (next((group for group in path_match.groups() if group), None) if path_match else None)
        app = _mentioned_app(text)
        common = {
            "request": text,
            "fallback_strategy": ("report the exact unavailable capability", "ask for a corrected target instead of guessing"),
        }

        if re.search(r"\b(what app am i using|active window|current window|what window)\b", lowered):
            return DesktopActionPlan(**common, intended_action=AppAction.GET_ACTIVE_WINDOW,
                                     expected_result="the foreground window and owning app",
                                     verification_method="read GetForegroundWindow and its process")
        if re.search(r"\b(list|show)\b.*\b(open |visible )?windows\b", lowered):
            return DesktopActionPlan(**common, intended_action=AppAction.LIST_WINDOWS, target_app=app,
                                     expected_result="normalized visible top-level windows",
                                     verification_method="enumerate visible Win32 windows")
        if re.search(r"\bkill\b", lowered):
            return DesktopActionPlan(**common, intended_action=AppAction.KILL_PROCESS, target_app=app,
                                     risk=DesktopRisk.HIGH, confirmation_needed=True, missing_target=not bool(app),
                                     expected_result="the exact confirmed process is no longer running",
                                     verification_method="confirm PID no longer exists",
                                     rationale=("forced process termination can lose unsaved work",))
        if re.search(r"\bclose\b", lowered):
            close_all = bool(re.search(r"\b(all|every)\b", lowered))
            return DesktopActionPlan(**common, intended_action=AppAction.CLOSE_ALL if close_all else AppAction.CLOSE,
                                     target_app=app, risk=DesktopRisk.HIGH, confirmation_needed=True,
                                     missing_target=not bool(app),
                                     expected_result="the exact confirmed window(s) close normally",
                                     verification_method="confirm target window handles disappear",
                                     rationale=("JARVIS cannot reliably detect unsaved work in arbitrary apps",))
        if re.search(r"\b(show|reveal)\b.*\b(folder|explorer)\b", lowered):
            return DesktopActionPlan(**common, intended_action=AppAction.SHOW_IN_FOLDER, target_file=path,
                                     missing_target=not bool(path), expected_result="File Explorer selects the target",
                                     verification_method="verify an Explorer window appears")
        if path and ("open" in lowered or app):
            choice = choose_app_for_path(path)
            selected_app = app or choice.app
            action = AppAction.OPEN_FOLDER if Path(path).is_dir() and not app else AppAction.OPEN_FILE
            return DesktopActionPlan(**common, intended_action=action, target_app=selected_app, target_file=path,
                                     risk=choice.risk, confirmation_needed=choice.requires_confirmation,
                                     expected_result=f"the target opens in {selected_app}",
                                     verification_method="verify the expected app window when available",
                                     rationale=(choice.reason,))
        if re.search(r"\b(focus|switch to|bring .*front)\b", lowered):
            return DesktopActionPlan(**common, intended_action=AppAction.FOCUS, target_app=app,
                                     missing_target=not bool(app), expected_result="the target window becomes foreground",
                                     verification_method="compare GetForegroundWindow with target handle")
        for word, action in (("minimize", AppAction.MINIMIZE), ("maximize", AppAction.MAXIMIZE), ("restore", AppAction.RESTORE)):
            if word in lowered:
                return DesktopActionPlan(**common, intended_action=action, target_app=app,
                                         missing_target=not bool(app), expected_result=f"the target window is {word}d",
                                         verification_method=f"read Win32 {word} state")
        if re.search(r"\b(open|launch|start)\b", lowered):
            return DesktopActionPlan(**common, intended_action=AppAction.OPEN, target_app=app,
                                     missing_target=not bool(app), expected_result="an existing window is focused or one app instance is launched",
                                     verification_method="verify a matching process/window after launch")
        return DesktopActionPlan(**common, intended_action=None, missing_target=True,
                                 expected_result="a specific desktop action and target",
                                 verification_method="none until the request is clarified",
                                 rationale=("No supported desktop action was explicit.",))


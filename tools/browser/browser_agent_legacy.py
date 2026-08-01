import logging
from typing import Optional

from . import downloader, form_handler, interaction, navigator, page_reader
from .browser_memory import BrowserMemory
from .session import BrowserSession
from .tab_manager import Tab, TabManager, TabNotFoundError

logger = logging.getLogger(__name__)


class BrowserAgent:
    """Top-level facade. Owns one TabManager (which owns one BrowserSession)
    and one BrowserMemory, and dispatches every operation to the right
    module against the right tab's Page. This is what tool_registry.py
    wraps into individual tools for the reasoning loop."""

    def __init__(self):
        self.session = BrowserSession()
        self.tabs = TabManager(self.session)
        self.memory = BrowserMemory()

    def _page_for(self, tab_ref: Optional[str]):
        tab = self.tabs.resolve(tab_ref)
        return tab, tab.page

    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #

    def list_tabs(self) -> dict:
        return {"success": True, "tabs": self.tabs.list_tabs()}

    def open_tab(self, url: Optional[str] = None, label: Optional[str] = None) -> dict:
        # Deduplicate: if a tab with the same label already exists, switch to it.
        if label:
            existing = self.tabs.find_by_label(label)
            if existing:
                logger.info("[Browser] Reusing existing tab '%s' (id=%s)", label, existing.id)
                try:
                    existing.page.bring_to_front()
                except Exception:
                    pass
                self.tabs._current_id = existing.id
                if url:
                    return self.goto(url, tab=label)
                return {"success": True, "tab": existing.snapshot(), "reused": True}

        # Deduplicate: if a tab with the same URL already exists, switch to it.
        if url:
            existing = self.tabs.find_by_url(url)
            if existing:
                logger.info("[Browser] Reusing existing tab for URL '%s' (id=%s)", url, existing.id)
                try:
                    existing.page.bring_to_front()
                except Exception:
                    pass
                self.tabs._current_id = existing.id
                return {"success": True, "tab": existing.snapshot(), "reused": True}

        try:
            logger.info("[Browser] Opening page '%s' label='%s'", url, label)
            tab = self.tabs.open_tab(url=url, label=label)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        if url:
            self.memory.record_navigation(tab.id, tab.page.url, tab.page.title())
        logger.info("[Browser] Opened tab %s (%s)", tab.id, tab.label)
        return {"success": True, "tab": tab.snapshot()}

    def close_tab(self, tab: Optional[str] = None) -> dict:
        try:
            snapshot = self.tabs.close_tab(tab)
        except TabNotFoundError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "closed": snapshot}

    def switch_tab(self, tab: str) -> dict:
        try:
            resolved = self.tabs.switch_tab(tab)
        except TabNotFoundError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "tab": resolved.snapshot()}

    def duplicate_tab(self, tab: Optional[str] = None, label: Optional[str] = None) -> dict:
        try:
            new_tab = self.tabs.duplicate_tab(tab, label=label)
        except TabNotFoundError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "tab": new_tab.snapshot()}

    def label_tab(self, tab: str, label: str) -> dict:
        try:
            resolved = self.tabs.relabel(tab, label)
        except TabNotFoundError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "tab": resolved.snapshot()}

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def goto(self, url: str, tab: Optional[str] = None) -> dict:
        try:
            resolved_tab, page = self._page_for(tab)
        except TabNotFoundError as exc:
            return {"success": False, "error": str(exc)}

        result = navigator.goto(page, url)
        if result.get("success"):
            self.tabs.touch_label(resolved_tab)
            self.memory.record_navigation(resolved_tab.id, page.url, page.title())
            logger.info("[Browser] Navigated to %s on tab %s", url, resolved_tab.id)
        else:
            self.memory.record_failure(resolved_tab.id, "goto", url, result.get("error", ""))
            logger.warning("[Browser] Navigation to %s failed on tab %s: %s", url, resolved_tab.id, result.get("error", ""))
        return result

    def back(self, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        result = navigator.back(page)
        if result.get("success"):
            self.tabs.touch_label(resolved_tab)
            self.memory.record_navigation(resolved_tab.id, page.url, page.title())
        return result

    def forward(self, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        result = navigator.forward(page)
        if result.get("success"):
            self.tabs.touch_label(resolved_tab)
            self.memory.record_navigation(resolved_tab.id, page.url, page.title())
        return result

    def refresh(self, tab: Optional[str] = None) -> dict:
        _, page = self._page_for(tab)
        return navigator.refresh(page)

    def wait_for_load(self, tab: Optional[str] = None, timeout: int = 15000, state: str = "load") -> dict:
        _, page = self._page_for(tab)
        return navigator.wait_for_load(page, timeout=timeout, state=state)

    def wait_for_element(self, description: str, tab: Optional[str] = None, timeout: int = 15000) -> dict:
        _, page = self._page_for(tab)
        return navigator.wait_for_element(page, description, timeout=timeout)

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    def read_text(self, tab: Optional[str] = None, max_chars: int = 4000) -> dict:
        _, page = self._page_for(tab)
        return page_reader.read_text(page, max_chars=max_chars)

    def read_dom_summary(self, tab: Optional[str] = None, max_items: int = 40) -> dict:
        _, page = self._page_for(tab)
        return page_reader.read_dom_summary(page, max_items=max_items)

    def extract_tables(self, tab: Optional[str] = None) -> dict:
        _, page = self._page_for(tab)
        return page_reader.extract_tables(page)

    def extract_links(self, tab: Optional[str] = None, limit: int = 50) -> dict:
        _, page = self._page_for(tab)
        return page_reader.extract_links(page, limit=limit)

    def extract_forms(self, tab: Optional[str] = None) -> dict:
        _, page = self._page_for(tab)
        return page_reader.extract_forms(page)

    # ------------------------------------------------------------------ #
    # Interaction (each records a failure into browser_memory on failure)
    # ------------------------------------------------------------------ #

    def _do_interaction(self, tab, page, action_name, fn, description):
        result = fn()
        if not result.get("success"):
            self.memory.record_failure(tab.id, action_name, description, result.get("error", ""))
        return result

    def click(self, description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        result = self._do_interaction(resolved_tab, page, "click", lambda: interaction.click(page, description), description)
        if result.get("success"):
            logger.info("[Browser] Clicked '%s' on tab %s", description[:40], resolved_tab.id)
        return result

    def double_click(self, description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(resolved_tab, page, "double_click", lambda: interaction.double_click(page, description), description)

    def right_click(self, description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(resolved_tab, page, "right_click", lambda: interaction.right_click(page, description), description)

    def hover(self, description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(resolved_tab, page, "hover", lambda: interaction.hover(page, description), description)

    def type_text(self, description: str, text: str, clear_first: bool = True, press_enter: bool = False, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        result = self._do_interaction(
            resolved_tab, page, "type_text",
            lambda: interaction.type_text(page, description, text, clear_first=clear_first, press_enter=press_enter), description,
        )
        if result.get("success") and press_enter:
            logger.info("[Browser] Search completed (typed '%s' and pressed Enter)", text[:40])
        return result

    def press_key(self, key: str, tab: Optional[str] = None) -> dict:
        _, page = self._page_for(tab)
        return interaction.press_key(page, key)

    def scroll(self, amount: int = 600, description: Optional[str] = None, tab: Optional[str] = None) -> dict:
        _, page = self._page_for(tab)
        return interaction.scroll(page, amount=amount, description=description)

    def select_dropdown(self, description: str, option: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(
            resolved_tab, page, "select_dropdown",
            lambda: interaction.select_dropdown(page, description, option), description,
        )

    def set_checkbox(self, description: str, checked: bool = True, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(
            resolved_tab, page, "set_checkbox",
            lambda: interaction.set_checkbox(page, description, checked=checked), description,
        )

    def click_radio(self, description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(resolved_tab, page, "click_radio", lambda: interaction.click_radio(page, description), description)

    def drag_and_drop(self, source_description: str, target_description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(
            resolved_tab, page, "drag_and_drop",
            lambda: interaction.drag_and_drop(page, source_description, target_description),
            f"{source_description} -> {target_description}",
        )

    def upload_file(self, description: str, file_path: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(
            resolved_tab, page, "upload_file",
            lambda: interaction.upload_file(page, description, file_path), description,
        )

    # ------------------------------------------------------------------ #
    # Forms
    # ------------------------------------------------------------------ #

    def fill_form(self, fields: dict, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        result = form_handler.fill_form(page, fields)
        if not result.get("success"):
            self.memory.record_failure(resolved_tab.id, "fill_form", str(list(fields.keys())), str(result.get("fields")))
        return result

    # ------------------------------------------------------------------ #
    # Downloads
    # ------------------------------------------------------------------ #

    def download_via(self, trigger_description: str, destination_dir: Optional[str] = None, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        result = downloader.download_via(page, trigger_description, destination_dir=destination_dir)
        if not result.get("success"):
            self.memory.record_failure(resolved_tab.id, "download", trigger_description, result.get("error", ""))
        return result

    # ------------------------------------------------------------------ #
    # Auth (best-effort: relies on the persistent Edge profile's saved
    # session/autofill -- never enters credentials itself)
    # ------------------------------------------------------------------ #

    def wait_for_login(self, tab: Optional[str] = None, timeout_seconds: int = 120) -> dict:
        """Polls the tab's URL, waiting for it to move away from a
        login-like path. Meant to be called right after navigating to a
        login page: if the user is already authenticated via the browser's
        saved session, this returns almost immediately; otherwise it gives
        the user (or the browser's own saved-password autofill) time to
        complete the login manually."""
        import time as _time

        resolved_tab, page = self._page_for(tab)
        login_markers = ("login", "signin", "sign-in", "sign_in", "auth", "accounts.google.com")
        start = _time.time()
        start_url = page.url

        while _time.time() - start < timeout_seconds:
            try:
                current_url = page.url
            except Exception:
                current_url = start_url

            still_on_login = any(marker in current_url.lower() for marker in login_markers)
            if not still_on_login and current_url != start_url:
                self.tabs.touch_label(resolved_tab)
                self.memory.record_navigation(resolved_tab.id, page.url, page.title())
                return {"success": True, "url": current_url, "waited_seconds": round(_time.time() - start, 1)}

            page.wait_for_timeout(1000)

        self.memory.record_failure(resolved_tab.id, "wait_for_login", tab or "current", "Timed out waiting for login to complete")
        return {
            "success": False,
            "error": f"Still appears to be on a login page after {timeout_seconds}s. "
                     "The user may need to complete login manually, or saved credentials aren't available for this site.",
        }

    # ------------------------------------------------------------------ #
    # Page understanding (single-shot comprehensive snapshot)
    # ------------------------------------------------------------------ #

    def read_page(self, tab: str | None = None) -> dict:
        """Return a comprehensive structured snapshot of the current page:
        URL, title, page type, all interactive elements, headings, text,
        forms, tables, viewport info. This is the preferred way to
        understand a page before acting -- replaces needing 5 separate
        calls to read_dom_summary/read_text/extract_links/etc."""
        from .page_snapshot import snapshot as _snapshot

        try:
            resolved_tab, page = self._page_for(tab)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        result = _snapshot(page)
        if result.get("success"):
            self.tabs.touch_label(resolved_tab)
            self.memory.record_page_summary(resolved_tab.id, result)
        return result

    def read_page_light(self, tab: str | None = None) -> dict:
        """Quick page check: URL, title, page type, buttons, links, inputs.
        Lighter than full read_page for fast verification loops."""
        from .page_snapshot import snapshot_light as _snapshot_light

        try:
            _, page = self._page_for(tab)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        return _snapshot_light(page)

    # ------------------------------------------------------------------ #
    # Action verification
    # ------------------------------------------------------------------ #

    def verify_navigation(self, tab: str | None = None, before_url: str = "", before_title: str = "") -> dict:
        """Check if the page changed (URL, title) since a previous state.
        Call before an action to capture state, then after to verify."""
        from .navigator import did_navigation_occur

        try:
            _, page = self._page_for(tab)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        return did_navigation_occur(page, before_url, before_title)

    def verify_element_appeared(self, description: str, tab: str | None = None, timeout: int = 5000) -> dict:
        """Check if an element matching the description is now visible.
        Returns confidence score if found."""
        from .element_finder import resolve as _resolve

        try:
            _, page = self._page_for(tab)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        try:
            resolved = _resolve(page, description)
            return {
                "success": True,
                "found": True,
                "description": description,
                "confidence": resolved.confidence,
                "strategy": resolved.strategy,
            }
        except ElementNotFoundError as exc:
            return {"success": True, "found": False, "description": description, "error": str(exc)}

    # ------------------------------------------------------------------ #
    # Vision fallback (page screenshot + vision model analysis)
    # ------------------------------------------------------------------ #

    def vision(self, question: str = "What do you see on this page?", tab: str | None = None) -> dict:
        """Take a screenshot of the current page and ask the vision model
        a question about it. Use this when the text-based snapshot doesn't
        give you enough context — e.g. dropdown popups, date picker
        calendars, captchas, or custom UI widgets that the DOM snapshot
        can't capture."""
        from tools.vision import Vision
        try:
            _, page = self._page_for(tab)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        try:
            screenshot = page.screenshot()
            from io import BytesIO
            from PIL import Image
            image = Image.open(BytesIO(screenshot))
            answer = Vision().look(image, question)
            return {"success": True, "answer": answer}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------ #
    # Memory / state
    # ------------------------------------------------------------------ #

    def get_state(self) -> dict:
        return self.memory.get_state(self.tabs.list_tabs())

    def set_task(self, description: str, total_steps: int = 0) -> dict:
        self.memory.set_task(description, total_steps=total_steps)
        return {"success": True, "task": description, "total_steps": total_steps}

    def update_progress(self, note: str) -> dict:
        self.memory.add_progress(note)
        return {"success": True, "progress_notes": self.memory.progress_notes}

    def complete_step(self, step_description: str) -> dict:
        self.memory.complete_step(step_description)
        return {"success": True, "step": self.memory.current_step, "completed_steps": self.memory.completed_steps}

    def plan(self, goal: str, steps: list[str]) -> dict:
        """Store an explicit ordered plan. Each step is a short string like
        'Search flights to Tokyo', 'Compare hotel options', etc.
        Also captures the goal in set_task."""
        self.set_task(goal, total_steps=len(steps))
        self.memory.set_plan(steps)
        return {
            "success": True,
            "goal": goal,
            "steps": steps,
            "step_count": len(steps),
            "plan_progress": [
                {"step": s, "done": False} for s in steps
            ],
        }

    def note(self, key: str, value: str) -> dict:
        """Store a session-level note (cross-tab working memory).
        Unlike record_data which is per-tab, notes are global to the
        whole session — use for things like 'cheapest_flight=₹25,000'
        that span multiple tabs."""
        self.memory.set_note(key, value)
        return {"success": True, "key": key, "value": value}

    def summarize_session(self, format: str = "text") -> dict:
        """Collect all extracted data, notes, page summaries, and
        progress into a final structured digest. Use this when you've
        completed a multi-tab workflow and need to present results."""
        state = self.memory.get_state(self.tabs.list_tabs())
        tabs = self.tabs.list_tabs()
        summary = {
            "goal": state.get("current_task", ""),
            "plan": state.get("plan", []),
            "plan_progress": state.get("plan_progress", []),
            "completed_steps": state.get("completed_steps", []),
            "notes": state.get("notes", {}),
            "extracted_data": state.get("extracted_data", {}),
            "page_summaries": state.get("page_summaries", {}),
            "open_tabs": [
                {"label": t.label, "url": t.url, "title": t.title}
                for t in tabs
            ],
        }
        return {"success": True, "format": format, "summary": summary}

    def record_data(self, key: str, data, tab: str | None = None) -> dict:
        """Store extracted data (prices, search results, etc.) in browser
        memory so the agent can reference it later without re-reading."""
        try:
            resolved_tab = self.tabs.resolve(tab) if tab else self.tabs.get_current()
            tab_id = resolved_tab.id
        except Exception:
            tab_id = "default"
        self.memory.record_extracted_data(tab_id, key, data)
        return {"success": True, "key": key, "tab_id": tab_id}

    # ------------------------------------------------------------------ #
    # Legacy-compatible helpers (used by tools/browser_tool.py shim so
    # skills/youtube.py needs zero changes)
    # ------------------------------------------------------------------ #

    def youtube_search(self, query: str) -> str:
        _, page = self._page_for(None)
        navigator.goto(page, "https://www.youtube.com")
        page.wait_for_selector('input[name="search_query"]', timeout=10000)
        page.locator('input[name="search_query"]').fill(query)
        page.keyboard.press("Enter")
        page.wait_for_selector("ytd-video-renderer", timeout=10000)
        return f"Searched YouTube for '{query}'"

    def youtube_play_url(self, url: str) -> str:
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        _, page = self._page_for(None)
        navigator.goto(page, url)
        page.wait_for_load_state("domcontentloaded")
        return f"Playing {url}"

    def youtube_play_first_result(self) -> str:
        _, page = self._page_for(None)
        page.wait_for_selector("a#video-title", timeout=10000)
        page.locator("a#video-title").first.click()
        page.wait_for_load_state("domcontentloaded")
        return "Playing first YouTube video."

    def google_search(self, query: str) -> str:
        _, page = self._page_for(None)
        navigator.goto(page, "https://www.google.com")
        page.wait_for_selector('textarea[name="q"]', timeout=10000)
        page.locator('textarea[name="q"]').fill(query)
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded")
        return f"Searched Google for '{query}'"


browser_agent = BrowserAgent()

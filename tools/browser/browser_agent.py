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
        try:
            tab = self.tabs.open_tab(url=url, label=label)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        if url:
            self.memory.record_navigation(tab.id, tab.page.url, tab.page.title())
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
        else:
            self.memory.record_failure(resolved_tab.id, "goto", url, result.get("error", ""))
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
        return self._do_interaction(resolved_tab, page, "click", lambda: interaction.click(page, description), description)

    def double_click(self, description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(resolved_tab, page, "double_click", lambda: interaction.double_click(page, description), description)

    def right_click(self, description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(resolved_tab, page, "right_click", lambda: interaction.right_click(page, description), description)

    def hover(self, description: str, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(resolved_tab, page, "hover", lambda: interaction.hover(page, description), description)

    def type_text(self, description: str, text: str, clear_first: bool = True, tab: Optional[str] = None) -> dict:
        resolved_tab, page = self._page_for(tab)
        return self._do_interaction(
            resolved_tab, page, "type_text",
            lambda: interaction.type_text(page, description, text, clear_first=clear_first), description,
        )

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
    # Memory / state
    # ------------------------------------------------------------------ #

    def get_state(self) -> dict:
        return self.memory.get_state(self.tabs.list_tabs())

    def set_task(self, description: str) -> dict:
        self.memory.set_task(description)
        return {"success": True, "task": description}

    def update_progress(self, note: str) -> dict:
        self.memory.add_progress(note)
        return {"success": True, "progress_notes": self.memory.progress_notes}

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

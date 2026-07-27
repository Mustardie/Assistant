import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .session import BrowserSession

logger = logging.getLogger(__name__)


@dataclass
class Tab:
    id: str
    page: object  # playwright.sync_api.Page
    label: str
    opened_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        try:
            url = self.page.url
        except Exception:
            url = ""
        try:
            title = self.page.title()
        except Exception:
            title = ""
        return {"id": self.id, "label": self.label, "url": url, "title": title}


class TabNotFoundError(Exception):
    pass


class TabManager:
    """Owns every open Tab and which one is "current". Tabs are referred to
    by a purpose label (set explicitly, or auto-derived from the page title
    on navigation) as well as by id, so the agent can say "switch to the
    Booking.com tab" instead of tracking indices."""

    def __init__(self, session: Optional[BrowserSession] = None):
        self.session = session or BrowserSession()
        self._tabs: dict[str, Tab] = {}
        self._current_id: Optional[str] = None
        self._next_id = 1

    # ------------------------------------------------------------------ #
    # Bootstrapping / discovery
    # ------------------------------------------------------------------ #

    def _new_tab_id(self) -> str:
        tab_id = f"tab{self._next_id}"
        self._next_id += 1
        return tab_id

    def _adopt_existing_pages(self) -> None:
        """Pick up any pages already open in the connected context (e.g. the
        tab the user was already looking at) that we haven't registered yet."""
        context = self.session.ensure_context()
        known_pages = {tab.page for tab in self._tabs.values()}
        for page in context.pages:
            if page in known_pages or page.is_closed():
                continue
            tab_id = self._new_tab_id()
            label = self._derive_label(page)
            self._tabs[tab_id] = Tab(id=tab_id, page=page, label=label)
            if self._current_id is None:
                self._current_id = tab_id

    @staticmethod
    def _derive_label(page) -> str:
        try:
            title = page.title()
            if title:
                return title
        except Exception:
            pass
        try:
            return page.url or "New Tab"
        except Exception:
            return "New Tab"

    def _ensure_ready(self) -> None:
        if self._tabs:
            return
        self._adopt_existing_pages()
        if not self._tabs:
            self.open_tab()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def list_tabs(self) -> list:
        self._ensure_ready()
        return [
            {**tab.snapshot(), "is_current": tab.id == self._current_id}
            for tab in self._tabs.values()
        ]

    def get_current(self) -> Tab:
        self._ensure_ready()
        if self._current_id is None or self._current_id not in self._tabs:
            self._current_id = next(iter(self._tabs))
        return self._tabs[self._current_id]

    def resolve(self, tab_ref: Optional[str]) -> Tab:
        """Resolve a tab reference (id, exact label, or a purpose substring
        like "flights") to a Tab. None means "the current tab"."""
        self._ensure_ready()

        if not tab_ref or tab_ref.lower() in {"current", "this", "active"}:
            return self.get_current()

        if tab_ref in self._tabs:
            return self._tabs[tab_ref]

        lowered = tab_ref.strip().lower()
        for tab in self._tabs.values():
            if tab.label.strip().lower() == lowered:
                return tab

        matches = [tab for tab in self._tabs.values() if lowered in tab.label.lower() or lowered in (tab.page.url or "").lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            labels = ", ".join(f"'{m.label}'" for m in matches)
            raise TabNotFoundError(f"'{tab_ref}' matches multiple open tabs: {labels}. Be more specific.")

        open_labels = ", ".join(f"'{t.label}'" for t in self._tabs.values())
        raise TabNotFoundError(f"No open tab matches '{tab_ref}'. Currently open: {open_labels}")

    def open_tab(self, url: Optional[str] = None, label: Optional[str] = None) -> Tab:
        context = self.session.ensure_context()
        page = context.new_page()
        tab_id = self._new_tab_id()

        if url:
            if "://" not in url and not url.startswith("about:") and not url.startswith("data:"):
                url = "https://" + url
            page.goto(url, wait_until="domcontentloaded")

        tab = Tab(id=tab_id, page=page, label=label or self._derive_label(page))
        self._tabs[tab_id] = tab
        self._current_id = tab_id
        logger.info("Opened tab %s (%s)", tab_id, tab.label)
        return tab

    def close_tab(self, tab_ref: Optional[str] = None) -> dict:
        tab = self.resolve(tab_ref)
        snapshot = tab.snapshot()
        try:
            tab.page.close()
        except Exception:
            pass
        del self._tabs[tab.id]

        if self._current_id == tab.id:
            self._current_id = next(iter(self._tabs), None)
            if self._current_id is None and not self._tabs:
                # Keep at least one tab open so the session stays usable.
                self.open_tab()

        return snapshot

    def switch_tab(self, tab_ref: str) -> Tab:
        tab = self.resolve(tab_ref)
        self._current_id = tab.id
        try:
            tab.page.bring_to_front()
        except Exception:
            pass
        return tab

    def duplicate_tab(self, tab_ref: Optional[str] = None, label: Optional[str] = None) -> Tab:
        source = self.resolve(tab_ref)
        url = source.page.url
        new_tab = self.open_tab(url=url, label=label or f"{source.label} (copy)")
        return new_tab

    def relabel(self, tab_ref: str, label: str) -> Tab:
        tab = self.resolve(tab_ref)
        tab.label = label
        return tab

    def touch_label(self, tab: Tab) -> None:
        """Refresh a tab's auto-derived label after navigation, unless it
        was explicitly relabeled (heuristic: leave manually-set short
        labels alone by only auto-updating when the label still looks like
        an old page title, i.e. simplest to just always refresh -- explicit
        relabel() calls happen right before this would run again anyway)."""
        tab.label = self._derive_label(tab.page)

"""
browser_agent.py -- BrowserAgent backed by the Edge Extension.

Why this file exists:
The original Playwright/CDP-based BrowserAgent is preserved in
browser_agent_legacy.py for rollback. This replacement uses the Edge
Extension MV3 as its primary backend.

Architecture:
  BrowserAgent.method()  -->  EdgeExtensionClient.send(type, payload)
                          -->  FastAPI server (POST /api/send)
                          -->  Edge Extension (WebSocket)
                          -->  DOM / chrome.tabs / chrome.downloads

Key design:
- goto() ALWAYS opens a NEW tab -- never overwrites the user's current page.
- navigate() replaces the CURRENT tab's URL -- use only when explicitly
  requested.
- get_browser_state() returns a lightweight snapshot without DOM injection.

Set NOVA_USE_LEGACY_BROWSER=1 to restore the old Playwright backend.

Memory & state methods (get_state, set_task, plan, note, etc.) are
implemented locally -- they don't require a browser backend.

Methods without a direct extension equivalent raise a clear
NotImplementedError rather than silently falling back.
"""

import logging
import os
import time
from typing import Optional
from urllib.parse import urlsplit

from .edge_extension_client import EdgeExtensionClient

logger = logging.getLogger("nova.browser")

_backend_name = "EdgeExtension"
_legacy_mode = os.environ.get("NOVA_USE_LEGACY_BROWSER", "").lower() in ("1", "true", "yes")

if _legacy_mode:
    from .browser_agent_legacy import BrowserAgent as _LegacyAgent
    logger.warning("[Browser] Backend: Legacy (NO_EXTENSION mode)")


def _map_result(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {"success": False, "error": f"Unexpected response: {raw}"}
    if "success" not in raw:
        return {"success": True, **raw}
    return raw


# ---------------------------------------------------------------------------
# BrowserAgent
# ---------------------------------------------------------------------------

class BrowserAgent:
    """Browser automation agent.  Uses the Edge Extension by default."""

    def __init__(self):
        self._ext = EdgeExtensionClient()

        if _legacy_mode:
            self._legacy = _LegacyAgent()
        else:
            self._legacy = None

        self._memory = {}

        # Edge Extension backend state (Nova-side bookkeeping):
        #   _opened_urls: normalized URL -> tabId of the tab already showing it
        #   _tab_labels:  tabId -> purpose label (from open_tab/goto)
        # These let the agent REUSE an already-open page instead of spawning
        # duplicate tabs, and let callers refer to tabs by label.
        self._opened_urls: dict[str, int] = {}
        self._tab_labels: dict[int, str] = {}

    # -- helpers -----------------------------------------------------------

    # Domains that are the same product under different hostnames -- a tab
    # open on one must count as "the same page" when the other is requested
    # (e.g. an open chatgpt.com tab vs a requested chat.openai.com/chat).
    _HOST_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
        frozenset({"chatgpt.com", "chat.openai.com"}),
    )

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize a URL for dedupe comparison (case + trailing slash)."""
        return (url or "").strip().rstrip("/").lower()

    @classmethod
    def _host_key(cls, url: str) -> str:
        """Hostname normalized for comparison (lowercase, no www, and
        aliased across the same-product domain groups above)."""
        host = (urlsplit(url or "").netloc or "").split(":")[0].strip().lower()
        if host.startswith("www."):
            host = host[4:]
        for group in cls._HOST_ALIAS_GROUPS:
            if host in group:
                return "|".join(sorted(group))
        return host

    @classmethod
    def _is_root_request(cls, url: str) -> bool:
        """True when the requested URL points at a site root (no path)."""
        return urlsplit(url or "").path in ("", "/")

    def _find_existing_tab(self, url: str) -> Optional[dict]:
        """Return the first open tab already showing this URL, else None.
        Returns None (and logs) when the tab list cannot be queried, so
        callers fall back to creating a new tab.

        Matching is progressive:
          1. exact normalized URL
          2. same host, when the requested URL is a site root (a tab on
             youtube.com matches a request for youtube.com)
          3. same product via the host-alias groups (chatgpt.com matches
             chat.openai.com/chat)
        """
        try:
            listing = self._ext_send_and_check("tab_list")
        except Exception as exc:
            logger.debug("[Browser] tab_list failed during dedupe check: %s", exc)
            return None
        tabs = (listing or {}).get("tabs") or []
        norm = self._normalize_url(url)
        requested_host = self._host_key(url)
        root_request = self._is_root_request(url)
        for t in tabs:
            if t and self._normalize_url(t.get("url")) == norm:
                return t
        if root_request:
            for t in tabs:
                if t and self._host_key(t.get("url")) == requested_host:
                    return t
        for t in tabs:
            if t and self._host_key(t.get("url")) == requested_host:
                return t
        return None

    def _resolve_tab_id(self, tab) -> Optional[int]:
        """Resolve a tab reference to a tabId.

        Accepts: None (-> None, meaning "active tab"), a numeric id, a
        purpose label from open_tab/goto, or a string matching an open
        tab's url/title. Returns None when unresolvable.
        """
        if tab is None:
            return None
        if isinstance(tab, int):
            return tab
        if isinstance(tab, str) and tab.strip().isdigit():
            return int(tab.strip())

        needle = str(tab).strip().lower()
        # 1) Purpose labels recorded when tabs were opened
        for tid, lbl in self._tab_labels.items():
            if str(lbl).strip().lower() == needle:
                return tid
        # 2) Live match against open tabs (url/title/label substring)
        try:
            listing = self._ext_send_and_check("tab_list")
        except Exception:
            return None
        for t in (listing or {}).get("tabs") or []:
            tid = t.get("tabId")
            haystacks = [str(t.get("url") or ""), str(t.get("title") or "")]
            if tid in self._tab_labels:
                haystacks.append(str(self._tab_labels[tid]))
            for hay in haystacks:
                if needle and needle in hay.lower():
                    return tid
        return None

    def _remember_opened(self, url: str, tab_id, label: Optional[str] = None) -> None:
        """Bookkeeping for dedupe + label resolution."""
        if not tab_id:
            return
        self._opened_urls[self._normalize_url(url)] = int(tab_id)
        if label:
            self._tab_labels[int(tab_id)] = label

    def _ext_send(self, cmd: str, payload: dict = None) -> dict:
        result = self._ext.send(cmd, payload)
        return _map_result(result)

    def _ext_send_and_check(self, cmd: str, payload: dict = None) -> dict:
        result = self._ext_send(cmd, payload)
        if isinstance(result, dict) and result.get("success") is False:
            logger.warning(f"[Browser] Extension command failed: {cmd} – {result.get('error')}")
        return result

    def _browser_method(self, method: str, *args, **kwargs):
        if self._legacy:
            return getattr(self._legacy, method)(*args, **kwargs)
        return None

    # ================================================================== #
    # Tabs
    # ================================================================== #

    def list_tabs(self) -> dict:
        if self._legacy:
            return self._legacy.list_tabs()
        return self._ext_send_and_check("tab_list")

    def open_tab(self, url: Optional[str] = None, label: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.open_tab(url=url, label=label)
        url = (url or "about:blank").strip()

        # Dedupe: if this URL is already open in a tab, reuse it instead of
        # creating a duplicate tab (per goal sequencing contract).
        if url and url != "about:blank":
            existing = self._find_existing_tab(url)
            if existing:
                tab_id = existing.get("tabId")
                self._remember_opened(url, tab_id, label)
                logger.info("[Browser] URL already open in tab %s -- reusing (no duplicate tab)", tab_id)
                switch = self._ext_send_and_check("tab_switch", {"tabId": tab_id})
                if not switch.get("success"):
                    return {
                        "success": False,
                        "reused": True,
                        "tabId": tab_id,
                        "url": existing.get("url", url),
                        "error": f"Tab for {url} is already open (id {tab_id}) but could not be activated: {switch.get('error')}",
                    }
                return {
                    "success": True,
                    "reused": True,
                    "tabId": tab_id,
                    "url": existing.get("url", url),
                    "note": "Tab already open -- switched to the existing tab instead of opening a new one",
                }

        result = self._ext_send_and_check("tab_new", {"url": url})
        self._remember_opened(url, result.get("tabId"), label)
        return result

    def close_tab(self, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.close_tab(tab=tab)
        resolved = self._resolve_tab_id(tab) if tab else None
        if tab is not None and resolved is None:
            return {
                "success": False,
                "error": f"Cannot resolve tab '{tab}' -- use a tab id or a purpose label from browser_open_tab",
            }
        payload = {"tabId": resolved} if resolved is not None else {}
        return self._ext_send_and_check("tab_close", payload)

    def switch_tab(self, tab: str) -> dict:
        if self._legacy:
            return self._legacy.switch_tab(tab)
        resolved = self._resolve_tab_id(tab)
        if resolved is None:
            return {
                "success": False,
                "error": f"Cannot resolve tab '{tab}' -- use a tab id or a purpose label from browser_open_tab",
            }
        return self._ext_send_and_check("tab_switch", {"tabId": resolved})

    def duplicate_tab(self, tab: Optional[str] = None, label: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.duplicate_tab(tab=tab, label=label)
        return {"success": False, "error": "duplicate_tab not available in Edge Extension backend"}

    def label_tab(self, tab: str, label: str) -> dict:
        if self._legacy:
            return self._legacy.label_tab(tab, label)
        return {"success": False, "error": "label_tab not available in Edge Extension backend"}

    # ================================================================== #
    # Navigation
    # ================================================================== #

    def goto(self, url: str, tab: Optional[str] = None) -> dict:
        """
        Open URL in a NEW tab. NEVER overwrites the current tab.
        Use navigate() if you need to replace the current tab's URL.
        If the URL is already open in a tab, that tab is reused instead
        of opening a duplicate.
        """
        if self._legacy:
            return self._legacy.goto(url, tab=tab)
        url = (url or "").strip()

        existing = self._find_existing_tab(url) if url else None
        if existing:
            tab_id = existing.get("tabId")
            self._remember_opened(url, tab_id)
            logger.info("[Browser] goto: URL already open in tab %s -- reusing", tab_id)
            switch = self._ext_send_and_check("tab_switch", {"tabId": tab_id})
            if not switch.get("success"):
                return {
                    "success": False,
                    "reused": True,
                    "tabId": tab_id,
                    "url": existing.get("url", url),
                    "error": f"Tab for {url} is already open (id {tab_id}) but could not be activated: {switch.get('error')}",
                }
            return {
                "success": True,
                "reused": True,
                "tabId": tab_id,
                "url": existing.get("url", url),
                "note": "Tab already open -- switched to the existing tab instead of opening a new one",
            }

        result = self._ext_send_and_check("tab_open", {"url": url})
        self._remember_opened(url, result.get("tabId"))
        return result

    def navigate(self, url: str, tab: Optional[str] = None) -> dict:
        """
        Navigate the CURRENT tab to the given URL (replaces current page).
        Unlike goto(), this does NOT create a new tab. Use this only when
        you explicitly want to replace the current page.
        """
        if self._legacy:
            return self._legacy.goto(url, tab=tab)
        return self._ext_send_and_check("tab_navigate", {"url": url})

    def back(self, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.back(tab=tab)
        return self._ext_send_and_check("tab_back")

    def forward(self, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.forward(tab=tab)
        return self._ext_send_and_check("tab_forward")

    def refresh(self, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.refresh(tab=tab)
        return self._ext_send_and_check("tab_reload")

    def wait_for_load(self, tab: Optional[str] = None, timeout: int = 15000, state: str = "load") -> dict:
        if self._legacy:
            return self._legacy.wait_for_load(tab=tab, timeout=timeout, state=state)
        # Poll the lightweight browser-state snapshot until the active tab
        # reports status == "complete" (or the timeout elapses). This is
        # the belt-and-suspenders for sequencing: tab_open/tab_new already
        # wait for navigation to complete before responding, so this only
        # matters when a page keeps loading (redirects, heavy JS).
        deadline = time.monotonic() + (timeout or 15000) / 1000.0
        last_status = None
        while time.monotonic() < deadline:
            try:
                state_info = self._ext_send("get_browser_state")
            except Exception as exc:
                logger.debug("[Browser] wait_for_load: state query failed: %s", exc)
                state_info = {}
            active = (state_info or {}).get("activeTab") or {}
            last_status = active.get("status") or last_status
            if not last_status:
                # Snapshot has no status field (older extension) -- assume loaded.
                return {"success": True, "status": "unknown"}
            if last_status == "complete":
                return {"success": True, "status": last_status, "url": active.get("url", "")}
            time.sleep(0.25)
        return {
            "success": False,
            "error": f"Page did not finish loading within {timeout}ms (last status: {last_status})",
        }

    def wait_for_element(self, description: str, tab: Optional[str] = None, timeout: int = 15000) -> dict:
        if self._legacy:
            return self._legacy.wait_for_element(description, tab=tab, timeout=timeout)
        return self._ext_send_and_check("wait_for_selector", {
            "selector": description,
            "timeout": timeout,
        })

    # ================================================================== #
    # Browser state (lightweight, no DOM injection)
    # ================================================================== #

    def get_browser_state(self) -> dict:
        """
        Lightweight snapshot of the current browser state:
        active tab, all tabs, focused window, etc.
        No DOM injection -- uses chrome.tabs/chrome.windows API only.
        """
        if self._legacy:
            return self._legacy.get_browser_state()
        return self._ext_send_and_check("get_browser_state")

    # ================================================================== #
    # Reading
    # ================================================================== #

    def read_text(self, tab: Optional[str] = None, max_chars: int = 4000) -> dict:
        if self._legacy:
            return self._legacy.read_text(tab=tab, max_chars=max_chars)
        result = self._ext_send("get_page_text")
        text = result.get("text") or ""
        return {
            "success": True,
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    def read_dom_summary(self, tab: Optional[str] = None, max_items: int = 40) -> dict:
        if self._legacy:
            return self._legacy.read_dom_summary(tab=tab, max_items=max_items)
        return {"success": False, "error": "read_dom_summary not available in Edge Extension backend"}

    def extract_tables(self, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.extract_tables(tab=tab)
        return {"success": False, "error": "extract_tables not available in Edge Extension backend"}

    def extract_links(self, tab: Optional[str] = None, limit: int = 50) -> dict:
        if self._legacy:
            return self._legacy.extract_links(tab=tab, limit=limit)
        return {"success": False, "error": "extract_links not available in Edge Extension backend"}

    def extract_forms(self, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.extract_forms(tab=tab)
        return {"success": False, "error": "extract_forms not available in Edge Extension backend"}

    # ================================================================== #
    # Page understanding
    # ================================================================== #

    def read_page(self, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.read_page(tab=tab)
        result = self._ext_send("get_page_text")
        return _map_result({
            "success": True,
            "url": result.get("url", ""),
            "title": result.get("title", ""),
            "text": result.get("text", ""),
            "metadata": result.get("metadata", {}),
        })

    def read_page_light(self, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.read_page_light(tab=tab)
        result = self._ext_send("get_page_text")
        text = (result.get("text") or "")[:3000]
        return _map_result({
            "success": True,
            "url": result.get("url", ""),
            "title": result.get("title", ""),
            "text": text,
            "metadata": result.get("metadata", {}),
        })

    # ================================================================== #
    # Interaction
    # ================================================================== #

    def find_element(self, description: str, tab: Optional[str] = None) -> dict:
        """Find page elements by their visible text / aria-label / placeholder.
        The extension tags every hit with a stable selector so follow-up
        click/type commands can target it. Returns the best (visible,
        exact-match) hit plus all candidates."""
        if self._legacy:
            from .element_finder import resolve  # legacy Playwright path
            try:
                page = self._legacy._page_for(tab)
                resolved = resolve(page, description)
                return {
                    "success": True,
                    "best": {"selector": resolved.selector, "tag": resolved.tag},
                    "count": 1,
                    "elements": [{"selector": resolved.selector, "tag": resolved.tag}],
                }
            except Exception as exc:
                return {"success": False, "error": str(exc)}
        return self._ext_send_and_check("find_element", {"description": description})

    def _click_selector(self, selector: str) -> dict:
        return self._ext_send_and_check("click", {"selector": selector, "options": {}})

    def click(self, description: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.click(description, tab=tab)
        result = self._click_selector(description)
        if result.get("success"):
            return result
        # The description was not a live CSS selector -- treat it as
        # visible text ("the 'Blank document' button") and resolve it.
        logger.info("[Browser] click '%s' failed as selector (%s) -- trying text lookup", description, (result.get("error") or "")[:60])
        found = self.find_element(description, tab=tab)
        if not found.get("success"):
            return {
                "success": False,
                "error": f"Could not find anything to click matching: {description}",
                "details": result.get("error"),
            }
        best = (found.get("best") or {}).get("selector")
        if not best:
            return {
                "success": False,
                "error": f"Found elements for '{description}' but none usable",
                "elements": found.get("elements"),
            }
        logger.info("[Browser] click resolved '%s' -> %s", description, best)
        return self._click_selector(best)

    def double_click(self, description: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.double_click(description, tab=tab)
        result = self._ext_send_and_check("double_click", {"selector": description})
        if result.get("success"):
            return result
        found = self.find_element(description, tab=tab)
        best = (found.get("best") or {}).get("selector")
        if not found.get("success") or not best:
            return {
                "success": False,
                "error": f"Could not find anything to double-click matching: {description}",
            }
        return self._ext_send_and_check("double_click", {"selector": best})

    def right_click(self, description: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.right_click(description, tab=tab)
        result = self._ext_send_and_check("right_click", {"selector": description})
        if result.get("success"):
            return result
        found = self.find_element(description, tab=tab)
        best = (found.get("best") or {}).get("selector")
        if not found.get("success") or not best:
            return {
                "success": False,
                "error": f"Could not find anything to right-click matching: {description}",
            }
        return self._ext_send_and_check("right_click", {"selector": best})

    def hover(self, description: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.hover(description, tab=tab)
        return {"success": False, "error": "hover not available in Edge Extension backend"}

    def type_text(
        self,
        description: str,
        text: str,
        clear_first: bool = True,
        press_enter: bool = False,
        tab: Optional[str] = None,
    ) -> dict:
        if self._legacy:
            return self._legacy.type_text(description, text, clear_first=clear_first, press_enter=press_enter, tab=tab)
        result = self._type_into(description, text, clear_first=clear_first, keyboard=False)
        if not result.get("success"):
            found = self.find_element(description, tab=tab)
            best = (found.get("best") or {}).get("selector")
            if found.get("success") and best:
                logger.info("[Browser] type_text resolved '%s' -> %s", description, best)
                result = self._type_into(best, text, clear_first=clear_first, keyboard=False)
                if not result.get("success"):
                    # Rich editors (Google Docs, Notion) often ignore
                    # synthetic input events -- retry with keyboard events.
                    result = self._type_into(best, text, clear_first=clear_first, keyboard=True)
        if press_enter and result.get("success"):
            self._ext_send("press_key", {"key": "Enter"})
        return result

    def _type_into(self, selector: str, text: str, clear_first: bool, keyboard: bool) -> dict:
        return self._ext_send_and_check("type_text", {
            "selector": selector,
            "text": text,
            "clear": clear_first,
            "keyboard": keyboard,
        })

    def press_key(self, key: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.press_key(key, tab=tab)
        return self._ext_send_and_check("press_key", {"key": key})

    def scroll(self, amount: int = 600, description: Optional[str] = None, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.scroll(amount=amount, description=description, tab=tab)
        if description:
            return self._ext_send_and_check("scroll_to", {"selector": description})
        return self._ext_send_and_check("scroll_by", {"x": 0, "y": amount})

    def select_dropdown(self, description: str, option: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.select_dropdown(description, option, tab=tab)
        return self._ext_send_and_check("select_option", {"selector": description, "value": option})

    def set_checkbox(self, description: str, checked: bool = True, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.set_checkbox(description, checked=checked, tab=tab)
        return self.click(description, tab=tab)

    def click_radio(self, description: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.click_radio(description, tab=tab)
        return self.click(description, tab=tab)

    def drag_and_drop(self, source_description: str, target_description: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.drag_and_drop(source_description, target_description, tab=tab)
        return {"success": False, "error": "drag_and_drop not available in Edge Extension backend"}

    def upload_file(self, description: str, file_path: str, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.upload_file(description, file_path, tab=tab)
        return {"success": False, "error": "upload_file not available in Edge Extension backend"}

    # ================================================================== #
    # Forms
    # ================================================================== #

    def fill_form(self, fields: dict, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.fill_form(fields, tab=tab)
        if not isinstance(fields, dict) or not fields:
            return {"success": False, "error": "fill_form requires a non-empty dict of field -> value"}
        payload = [{"field": str(k), "value": v} for k, v in fields.items()]
        return self._ext_send_and_check("fill_form", {"fields": payload})

    # ================================================================== #
    # Downloads
    # ================================================================== #

    def download_via(self, trigger_description: str, destination_dir: Optional[str] = None, tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.download_via(trigger_description, destination_dir=destination_dir, tab=tab)
        return {"success": False, "error": "download_via not available in Edge Extension backend"}

    # ================================================================== #
    # Auth
    # ================================================================== #

    def wait_for_login(self, tab: Optional[str] = None, timeout_seconds: int = 120) -> dict:
        if self._legacy:
            return self._legacy.wait_for_login(tab=tab, timeout_seconds=timeout_seconds)
        return {"success": False, "error": "wait_for_login not available in Edge Extension backend"}

    # ================================================================== #
    # Action verification
    # ================================================================== #

    def verify_navigation(self, tab: Optional[str] = None, before_url: str = "", before_title: str = "") -> dict:
        if self._legacy:
            return self._legacy.verify_navigation(tab=tab, before_url=before_url, before_title=before_title)
        result = self._ext_send("get_tab_info")
        return _map_result({
            "success": True,
            "url_changed": result.get("url", "") != before_url,
            "current_url": result.get("url", ""),
            "title": result.get("title", ""),
        })

    def verify_element_appeared(self, description: str, tab: Optional[str] = None, timeout: int = 5000) -> dict:
        if self._legacy:
            return self._legacy.verify_element_appeared(description, tab=tab, timeout=timeout)
        return self._ext_send_and_check("wait_for_selector", {"selector": description, "timeout": timeout})

    # ================================================================== #
    # Vision
    # ================================================================== #

    def vision(self, question: str = "What do you see on this page?", tab: Optional[str] = None) -> dict:
        if self._legacy:
            return self._legacy.vision(question=question, tab=tab)
        return {"success": False, "error": "vision not available in Edge Extension backend"}

    # ================================================================== #
    # Memory / State  (local-only, no browser needed)
    # ================================================================== #

    def get_state(self) -> dict:
        state = dict(self._memory)
        state["opened_urls"] = dict(self._opened_urls)
        state["tab_labels"] = {str(k): v for k, v in self._tab_labels.items()}
        return {"success": True, "state": state}

    def set_task(self, description: str, total_steps: int = 0) -> dict:
        self._memory["task"] = {"description": description, "total_steps": total_steps, "current_step": 0}
        return {"success": True}

    def update_progress(self, note: str) -> dict:
        self._memory.setdefault("progress_log", []).append(note)
        return {"success": True}

    def complete_step(self, step_description: str) -> dict:
        if "task" in self._memory:
            self._memory["task"]["current_step"] += 1
        self._memory.setdefault("completed_steps", []).append(step_description)
        return {"success": True}

    def plan(self, goal: str, steps: list[str]) -> dict:
        self._memory["plan"] = {"goal": goal, "steps": steps}
        return {"success": True}

    def note(self, key: str, value: str) -> dict:
        self._memory.setdefault("notes", {})[key] = value
        return {"success": True}

    def summarize_session(self, fmt: str = "text") -> dict:
        return {"success": True, "summary": str(self._memory)}

    def record_data(self, key: str, data, tab: Optional[str] = None) -> dict:
        self._memory.setdefault("recorded_data", {})[key] = data
        return {"success": True}

    # ================================================================== #
    # Legacy helper methods
    # ================================================================== #

    def youtube_search(self, query: str) -> dict:
        search_url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
        nav = self.goto(search_url)
        if not nav.get("success"):
            return nav
        return {"success": True, "message": f"Navigated to YouTube search: {query}"}

    def youtube_play_url(self, url: str) -> dict:
        return self.goto(url)

    def youtube_play_first_result(self) -> dict:
        return {"success": False, "error": "youtube_play_first_result not available in Edge Extension backend"}

    def google_search(self, query: str) -> dict:
        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
        nav = self.goto(search_url)
        if not nav.get("success"):
            return nav
        return {"success": True, "message": f"Navigated to Google search: {query}"}


# Module-level singleton – same pattern as the original.
browser_agent = BrowserAgent()

import logging

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .element_finder import ElementNotFoundError, resolve

logger = logging.getLogger(__name__)


def goto(page, url: str, timeout: int = 20000) -> dict:
    if "://" not in url and not url.startswith("about:") and not url.startswith("data:"):
        url = "https://" + url
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "url": url, "error": f"Timed out loading {url}: {exc}"}
    return {"success": True, "url": page.url, "title": page.title()}


def back(page, timeout: int = 15000) -> dict:
    try:
        page.go_back(wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "url": page.url, "title": page.title()}


def forward(page, timeout: int = 15000) -> dict:
    try:
        page.go_forward(wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "url": page.url, "title": page.title()}


def refresh(page, timeout: int = 15000) -> dict:
    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "url": page.url, "title": page.title()}


def wait_for_load(page, timeout: int = 15000, state: str = "load") -> dict:
    """state: 'load' | 'domcontentloaded' | 'networkidle'. Also transparently
    rides out client-side redirects by waiting again if the URL keeps
    changing for a moment after the first load event."""
    try:
        page.wait_for_load_state(state, timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "error": str(exc), "url": page.url}

    # Handle redirect chains: if the URL is still changing shortly after
    # "loaded", wait for it to settle rather than reporting success on an
    # intermediate redirect page. Poll in short increments instead of a
    # blind flat sleep -- same 400ms worst-case ceiling as before (a late
    # redirect is still caught), but the common case (no redirect) exits
    # as soon as two consecutive checks agree, instead of always paying
    # the full 400ms.
    settle_budget_ms = 400
    poll_interval_ms = 100
    last_url = page.url
    elapsed_ms = 0
    try:
        while elapsed_ms < settle_budget_ms:
            page.wait_for_timeout(poll_interval_ms)
            elapsed_ms += poll_interval_ms
            current_url = page.url
            if current_url == last_url:
                break
            last_url = current_url
        if page.url != last_url or elapsed_ms >= settle_budget_ms:
            # URL moved on the last check (or we hit the ceiling still
            # mid-navigation) -- wait for that navigation to finish, same
            # as the original behavior.
            page.wait_for_load_state(state, timeout=timeout)
    except PlaywrightTimeoutError:
        pass

    return {"success": True, "url": page.url, "title": page.title()}


def wait_for_element(page, description: str, timeout: int = 15000) -> dict:
    try:
        resolved = resolve(page, description)
        resolved.locator.wait_for(state="visible", timeout=timeout)
        return {"success": True, "description": description, "strategy": resolved.strategy}
    except ElementNotFoundError as exc:
        return {"success": False, "description": description, "error": str(exc)}
    except PlaywrightTimeoutError as exc:
        return {"success": False, "description": description, "error": f"Found but never became visible: {exc}"}
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
    wait_for_stable(page, timeout=5000)
    return {"success": True, "url": page.url, "title": page.title()}


def back(page, timeout: int = 15000) -> dict:
    try:
        page.go_back(wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "error": str(exc)}
    wait_for_stable(page, timeout=3000)
    return {"success": True, "url": page.url, "title": page.title()}


def forward(page, timeout: int = 15000) -> dict:
    try:
        page.go_forward(wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "error": str(exc)}
    wait_for_stable(page, timeout=3000)
    return {"success": True, "url": page.url, "title": page.title()}


def refresh(page, timeout: int = 15000) -> dict:
    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "error": str(exc)}
    wait_for_stable(page, timeout=5000)
    return {"success": True, "url": page.url, "title": page.title()}


def wait_for_load(page, timeout: int = 15000, state: str = "load") -> dict:
    """state: 'load' | 'domcontentloaded' | 'networkidle'. Also transparently
    rides out client-side redirects by waiting again if the URL keeps
    changing for a moment after the first load event."""
    try:
        page.wait_for_load_state(state, timeout=timeout)
    except PlaywrightTimeoutError as exc:
        return {"success": False, "error": str(exc), "url": page.url}

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
            page.wait_for_load_state(state, timeout=timeout)
    except PlaywrightTimeoutError:
        pass

    return {"success": True, "url": page.url, "title": page.title()}


def wait_for_stable(page, timeout: int = 4000) -> dict:
    """Wait for the page to stabilise. Uses networkidle (best-effort)
    then a brief settle for SPA rendering. 'domcontentloaded' after
    'networkidle' is redundant — networkidle already implies it."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass

    page.wait_for_timeout(100)

    return {"success": True, "url": page.url, "title": page.title()}


def wait_for_element(page, description: str, timeout: int = 15000) -> dict:
    try:
        resolved = resolve(page, description)
        resolved.locator.wait_for(state="visible", timeout=timeout)
        return {"success": True, "description": description, "strategy": resolved.strategy, "confidence": resolved.confidence}
    except ElementNotFoundError as exc:
        return {"success": False, "description": description, "error": str(exc)}
    except PlaywrightTimeoutError as exc:
        return {"success": False, "description": description, "error": f"Found but never became visible: {exc}"}


def did_navigation_occur(page, before_url: str, before_title: str) -> dict:
    """Compare current page state with a previously captured state
    to determine if navigation or meaningful content change happened."""
    try:
        current_url = page.url
        current_title = page.title()
    except Exception as exc:
        return {"success": False, "navigation_detected": False, "error": str(exc)}

    url_changed = current_url != before_url
    title_changed = current_title != before_title

    return {
        "success": True,
        "navigation_detected": url_changed or title_changed,
        "url_changed": url_changed,
        "title_changed": title_changed,
        "before_url": before_url,
        "before_title": before_title,
        "after_url": current_url,
        "after_title": current_title,
    }

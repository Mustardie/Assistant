"""
Backward-compatible facade over the new modular browser system in
tools/browser/. Existing code (skills/youtube.py) imports `browser` from
here and calls a handful of legacy method names -- those are preserved,
just delegating to BrowserAgent underneath, always against the current tab.

New code should use the browser_* tools registered in tool_registry.py
instead, which expose the full tab/navigation/interaction/reading/form/
download surface built in tools/browser/.
"""
from tools.browser.browser_agent import browser_agent


class _LegacyBrowserFacade:
    def browser_open(self, url):
        return browser_agent.goto(url)

    def youtube_search(self, query):
        return browser_agent.youtube_search(query)

    def youtube_play_url(self, url):
        return browser_agent.youtube_play_url(url)

    def youtube_play_first_result(self):
        return browser_agent.youtube_play_first_result()

    def google_search(self, query):
        return browser_agent.google_search(query)

    def read_title(self):
        _, page = browser_agent._page_for(None)
        return page.title()

    def read_text(self, tab=None, max_chars=4000):
        return browser_agent.read_text(tab=tab, max_chars=max_chars)


browser = _LegacyBrowserFacade()

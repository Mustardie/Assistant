import logging
import socket
import subprocess
import time

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE_PROFILE = r"F:\Assistant\EdgeProfile"
DEBUG_PORT = 9222


class BrowserSession:
    """Owns the single Playwright <-> Edge CDP connection. Everything else
    (tabs, navigation, interaction, reading) works against the
    playwright.sync_api.BrowserContext this exposes, rather than each
    reimplementing connection/launch logic like the old monolithic
    Browser class did."""

    def __init__(self, edge_path: str = EDGE_PATH, edge_profile: str = EDGE_PROFILE, debug_port: int = DEBUG_PORT):
        self.edge_path = edge_path
        self.edge_profile = edge_profile
        self.debug_port = debug_port

        self._playwright = None
        self._browser = None
        self.context = None

    def _debug_running(self) -> bool:
        s = socket.socket()
        try:
            s.connect(("127.0.0.1", self.debug_port))
            s.close()
            return True
        except OSError:
            return False

    def _launch_edge(self) -> None:
        logger.info("Launching Edge with remote debugging on port %s", self.debug_port)
        subprocess.Popen([
            self.edge_path,
            f"--remote-debugging-port={self.debug_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.edge_profile}",
        ])

        for _ in range(40):
            if self._debug_running():
                return
            time.sleep(0.25)

        raise RuntimeError("Edge debugging port never started.")

    def _connect(self) -> None:
        if self._playwright is None:
            self._playwright = sync_playwright().start()

        self._browser = self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self.debug_port}"
        )

        if not self._browser.contexts:
            self.context = self._browser.new_context(accept_downloads=True)
        else:
            self.context = self._browser.contexts[0]

    def ensure_context(self):
        """Returns a live BrowserContext, launching/connecting/reconnecting
        as needed. Safe to call before every operation."""
        try:
            if self.context and self.context.browser and self.context.browser.is_connected():
                return self.context
        except Exception:
            pass

        if not self._debug_running():
            self._launch_edge()

        self._connect()
        return self.context

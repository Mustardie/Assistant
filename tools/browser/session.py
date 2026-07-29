import logging
import socket
import subprocess
import time

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
EDGE_PATH = next((p for p in _EDGE_CANDIDATES if Path(p).exists()), _EDGE_CANDIDATES[0])
EDGE_PROFILE = str(_PROJECT_ROOT / "EdgeProfile")
DEBUG_PORT = 9222


class BrowserSession:
    """Owns the single Playwright <-> Edge CDP connection. Everything else
    (tabs, navigation, interaction, reading) works against the
    playwright.sync_api.BrowserContext this exposes, rather than each
    reimplementing connection/launch logic like the old monolithic
    Browser class did."""

    _CONTEXT_VALIDITY_CACHE_SECONDS = 10.0
    _LAUNCH_WAIT_LOOPS = 120
    _LAUNCH_WAIT_INTERVAL = 0.25

    def __init__(self, edge_path: str = EDGE_PATH, edge_profile: str = EDGE_PROFILE, debug_port: int = DEBUG_PORT):
        self.edge_path = edge_path
        self.edge_profile = edge_profile
        self.debug_port = debug_port

        self._playwright = None
        self._browser = None
        self.context = None
        self._context_validated_at = 0.0

    def _debug_running(self) -> bool:
        s = socket.socket()
        try:
            s.settimeout(0.2)
            s.connect(("127.0.0.1", self.debug_port))
            s.close()
            return True
        except OSError:
            return False

    def _kill_all_edge(self) -> None:
        """Kill EVERY msedge* process via PowerShell and wait until none remain.
        A pre-running Edge ignores --remote-debugging-port on relaunch, so we
        must fully nuke all instances before starting fresh."""
        import subprocess as _sp
        import pathlib as _pl

        # 1. Force-kill every msedge-related process via PowerShell
        ps_cmd = 'Get-Process msedge* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue'
        _sp.run(['powershell', '-Command', ps_cmd], capture_output=True, timeout=15)
        _sp.run(['powershell', '-Command',
                 'Get-Process edge_launcher,edge_updater,msedgewebview2* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue'],
                capture_output=True, timeout=10)

        # 2. Wait until ALL msedge.exe entries are gone from tasklist
        for _ in range(20):
            result = _sp.run(['tasklist', '/FI', 'IMAGENAME eq msedge.exe'],
                             capture_output=True, text=True, timeout=5)
            if 'msedge.exe' not in result.stdout:
                break
            time.sleep(0.25)

        # 3. Clear profile lock files
        profile = _pl.Path(self.edge_profile)
        if profile.is_dir():
            for lock_name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie',
                              'Last Session', 'Last Tabs', 'Last Active Tabs'):
                try:
                    (profile / lock_name).unlink(missing_ok=True)
                except Exception:
                    pass

        logger.info("Edge fully killed, profile locks cleared")

    def _launch_edge(self) -> None:
        logger.info("[Browser] Launching browser (Edge, remote debugging on port %s)", self.debug_port)
        self._kill_all_edge()
        subprocess.Popen([
            self.edge_path,
            f"--remote-debugging-port={self.debug_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.edge_profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-gpu",
            "--disable-features=TranslateUI,msHub,AutofillServerCommunication",
        ])

        for _ in range(self._LAUNCH_WAIT_LOOPS):
            if self._debug_running():
                return
            time.sleep(self._LAUNCH_WAIT_INTERVAL)

        # Diagnostic: capture what's on port 9222 and running Edge processes
        try:
            import subprocess as _sp
            netstat_out = _sp.check_output(['netstat', '-ano', '-p', 'tcp'],
                                           timeout=5, text=True, stderr=_sp.STDOUT)
            port_lines = [l.strip() for l in netstat_out.splitlines() if '9222' in l]
            logger.error("Launch failed. Netstat entries for port 9222: %s", port_lines)
            tasklist_out = _sp.check_output(['tasklist', '/FI', 'IMAGENAME eq msedge.exe', '/FO', 'CSV'],
                                            timeout=5, text=True, stderr=_sp.STDOUT)
            logger.error("Running msedge processes:\n%s", tasklist_out.strip())
        except Exception as diag_err:
            logger.error("Diagnostics also failed: %s", diag_err)

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
        as needed. Uses a time-based cache so the socket check only runs
        once every N seconds instead of before every browser operation."""
        now = time.time()
        if self.context and (now - self._context_validated_at) < self._CONTEXT_VALIDITY_CACHE_SECONDS:
            return self.context

        try:
            if self.context and self.context.browser and self.context.browser.is_connected():
                self._context_validated_at = now
                return self.context
        except Exception:
            pass

        if self._debug_running():
            if self._browser and self._browser.is_connected():
                self._context_validated_at = time.time()
                return self.context
            logger.info("Port %s is occupied but browser not connected — killing stale process", self.debug_port)
            self._kill_all_edge()
            time.sleep(0.5)

        self._launch_edge()
        self._connect()
        self._context_validated_at = time.time()
        logger.info("[Browser] Browser session ready")
        return self.context

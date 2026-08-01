"""Smoke test: main.py should start the embedded bridge and shut it down.

main.py calls app.exec() at module level, so we can't just `import main`
in a plain subprocess (it would block forever). Instead we patch
QApplication.exec BEFORE importing main so the event loop auto-quits after
4 seconds; the import then returns and we can assert bridge state before
and after the app shutdown path (aboutToQuit + atexit) runs.
"""
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

SMOKE_CODE = """
import sys
import time

# Patch exec BEFORE importing main: auto-quit the event loop after 4s so
# the module-level `app.exec()` (main.py) returns on its own.
from PySide6.QtWidgets import QApplication

_orig_exec = QApplication.exec

from backend.browser_server import is_bridge_running


def _exec_with_auto_quit(*args, **kwargs):
    from PySide6.QtCore import QTimer
    QTimer.singleShot(1500, lambda: print(
        f"[SMOKE] bridge_running_inside_app={is_bridge_running()}", flush=True))
    QTimer.singleShot(4000, QApplication.instance().quit)
    return _orig_exec()


QApplication.exec = _exec_with_auto_quit
QApplication.exec_ = _exec_with_auto_quit

import main  # runs _start_embedded_bridge(), builds Qt app, runs event loop

time.sleep(0.5)
print(f"[SMOKE] bridge_running_after_shutdown={is_bridge_running()}", flush=True)
"""


def main():
    print("=== Smoke test: python main.py (embedded bridge) ===")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", SMOKE_CODE],
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONUNBUFFERED": "1"},
        )
    except subprocess.TimeoutExpired as e:
        print("--- TIMED OUT after 90s ---")
        print(e.stdout or "(no stdout)")
        print("--- STDERR ---")
        print(e.stderr or "(no stderr)")
        print("SMOKE TEST: FAIL")
        return 1

    print("--- STDOUT ---")
    print(proc.stdout)
    print("--- STDERR (last 30 lines) ---")
    err_lines = proc.stderr.strip().splitlines()
    print("\n".join(err_lines[-30:]) if err_lines else "(empty)")
    print(f"--- Exit code: {proc.returncode} ---")

    ok = (
        proc.returncode == 0
        and "[SMOKE] bridge_running_inside_app=True" in proc.stdout
        and "[SMOKE] bridge_running_after_shutdown=False" in proc.stdout
    )
    print("SMOKE TEST:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

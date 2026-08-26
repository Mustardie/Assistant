"""Start, reload and wait for the Premiere bridge from the command line.

Exists because the real-host path is only worth anything if it is easy to
reach. Before this, exercising it meant: install the panel, quit Premiere,
start it, wait an unknown number of seconds, open a menu, and hope. That is
enough friction that the honest outcome is a bridge nobody ever runs.

    python -m scripts.premiere_host wait        # block until the bridge answers
    python -m scripts.premiere_host start       # launch Premiere, then wait
    python -m scripts.premiere_host restart     # reinstall the panel, restart
    python -m scripts.premiere_host reload      # re-evaluate the host script
    python -m scripts.premiere_host status
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time

from premiere.bridge import bridge

#: Where Premiere installs itself. Newest first: an editor with two versions
#: installed means the newer one, unless PREMIERE_EXE says otherwise.
_WINDOWS_ROOTS = (
    r"C:\Program Files\Adobe",
)


def premiere_executable() -> str:
    override = os.environ.get("PREMIERE_EXE", "")
    if override:
        return override
    if platform.system() == "Darwin":
        for name in ("Adobe Premiere Pro 2025", "Adobe Premiere Pro 2024"):
            path = f"/Applications/{name}/Adobe Premiere Pro.app"
            if os.path.isdir(path):
                return path
        return ""
    for root in _WINDOWS_ROOTS:
        if not os.path.isdir(root):
            continue
        candidates = sorted(
            (name for name in os.listdir(root)
             if name.startswith("Adobe Premiere Pro")),
            reverse=True,
        )
        for name in candidates:
            exe = os.path.join(root, name, "Adobe Premiere Pro.exe")
            if os.path.isfile(exe):
                return exe
    return ""


def is_running() -> bool:
    if platform.system() == "Windows":
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Adobe Premiere Pro.exe"],
            capture_output=True, text=True,
        ).stdout
        return "Adobe Premiere Pro.exe" in out
    out = subprocess.run(["pgrep", "-f", "Adobe Premiere Pro"],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def stop() -> bool:
    if not is_running():
        return False
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/IM", "Adobe Premiere Pro.exe", "/F"],
                       capture_output=True)
    else:
        subprocess.run(["pkill", "-f", "Adobe Premiere Pro"], capture_output=True)
    for _ in range(30):
        if not is_running():
            return True
        time.sleep(1.0)
    return True


def launch() -> str:
    exe = premiere_executable()
    if not exe:
        raise SystemExit(
            "Could not find Premiere Pro. Set PREMIERE_EXE to its executable."
        )
    if platform.system() == "Darwin":
        subprocess.Popen(["open", "-a", exe])
    else:
        subprocess.Popen([exe], close_fds=True)
    return exe


def wait_for_bridge(timeout: float = 180.0, *, quiet: bool = False) -> dict:
    """Block until the panel answers, or the timeout expires.

    Premiere takes tens of seconds to start and the panel only binds its port
    once CEP has loaded it, so the only reliable signal is the health endpoint
    itself. Polling it is the whole point of this function.
    """
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = bridge.health()
        if last.get("connected") and last.get("host_ready"):
            return last
        if not quiet:
            sys.stderr.write(".")
            sys.stderr.flush()
        time.sleep(2.0)
    if not quiet:
        sys.stderr.write("\n")
    return last


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["status", "wait", "start", "restart", "reload", "stop"],
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.action == "status":
        state = bridge.health()
        state["premiere_running"] = is_running()
        state["executable"] = premiere_executable()
        print(json.dumps(state, indent=2) if args.json else _summary(state))
        return 0 if state.get("connected") else 1

    if args.action == "stop":
        print("stopped" if stop() else "was not running")
        return 0

    if args.action == "reload":
        try:
            result = bridge.reload_host()
        except Exception as exc:  # noqa: BLE001 - the message is the answer
            print(f"reload failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 0

    if args.action == "restart":
        from premiere import install as installer

        stop()
        outcome = installer.install(force=True)
        if not outcome.get("success"):
            print(f"install failed: {outcome.get('error')}", file=sys.stderr)
            return 1
        launch()
    elif args.action == "start" and not is_running():
        launch()

    state = wait_for_bridge(args.timeout)
    print(json.dumps(state, indent=2) if args.json else _summary(state))
    return 0 if state.get("connected") else 1


def _summary(state: dict) -> str:
    if not state.get("connected"):
        return f"bridge unreachable: {state.get('error', 'no response')}"
    return (
        f"bridge ok  premiere {state.get('version')}  "
        f"project={state.get('project') or 'none'}  "
        f"sequence={state.get('sequence') or 'none'}  "
        f"served={state.get('operations_served')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())

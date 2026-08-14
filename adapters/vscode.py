"""VS Code adapter -- local UI automation for editing/opening code in the
installed VS Code application."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from adapters.base import LocalAppAdapter

logger = logging.getLogger(__name__)


class VSCodeAdapter(LocalAppAdapter):
    name = "vscode"
    display_name = "VS Code"
    description = ("VS Code on this machine: open files and folders, run "
                   "commands and read or edit code straight from Nova. "
                   "Connect launches VS Code if it's closed.")
    executable_names = ["Code.exe"]
    process_aliases = ["code", "vscode"]
    launch_paths = ["code", os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe")]
    capabilities = ["open_file", "open_folder", "run_command", "read_file", "edit_file"]

    # ------------------------------------------------------------------ #
    def detect_app(self) -> dict:
        return _detect_process("Code.exe", ["code", "vscode"])

    def _code_cli(self) -> str | None:
        """The command that opens VS Code: the `code` CLI when on PATH,
        otherwise the full Code.exe path."""
        import shutil
        cli = shutil.which("code")
        if cli:
            return cli
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            exe = Path(base) / "Programs" / "Microsoft VS Code" / "Code.exe"
            if exe.exists():
                return str(exe)
        exe = shutil.which("Code.exe")
        return exe

    # ------------------------------------------------------------------ #
    def open_file(self, path, **kwargs):
        import subprocess
        from pathlib import Path

        code = self._code_cli()
        if not code:
            return self._fail("VS Code wasn't found on this machine.")
        path = str(Path(path).expanduser())
        try:
            subprocess.Popen([code, path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return self._ok(path=path)
        except Exception as exc:
            return self._fail(f"Could not open {path} in VS Code: {exc}")

    def open_folder(self, path, **kwargs):
        return self.open_file(path)

    def run_command(self, command, **kwargs):
        import subprocess

        code = self._code_cli()
        if not code:
            return self._fail("VS Code wasn't found on this machine.")
        try:
            result = subprocess.run([code, "--command", command],
                                    capture_output=True, text=True, timeout=20)
            return self._ok(command=command, exit_code=result.returncode)
        except Exception as exc:
            return self._fail(f"Could not run VS Code command: {exc}")

    def read_file(self, path, **kwargs):
        from pathlib import Path
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
            return self._ok(path=path, text=text, chars=len(text))
        except Exception as exc:
            return self._fail(f"Could not read {path}: {exc}")

    def edit_file(self, path, new_text=None, **kwargs):
        from pathlib import Path
        try:
            target = Path(path).expanduser()
            if new_text is not None:
                target.write_text(new_text, encoding="utf-8")
                return self._ok(path=path, edited=True)
            return self._fail("edit_file needs 'new_text'.")
        except Exception as exc:
            return self._fail(f"Could not edit {path}: {exc}")


def _detect_process(executable: str, aliases: list[str]) -> dict:
    import shutil
    installed = shutil.which("code") is not None or shutil.which(executable) is not None
    if not installed:
        # VS Code's Code.exe also lives at %LOCALAPPDATA%\Programs\Microsoft VS Code.
        base = os.environ.get("LOCALAPPDATA", "")
        if base and (Path(base) / "Programs" / "Microsoft VS Code" / executable).exists():
            installed = True
    running = _is_running(executable, aliases)
    return {"installed": installed, "running": running, "path": executable}


def _is_running(executable: str, aliases: list[str]) -> bool:
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {executable}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.lower()
        if executable.lower() in out:
            return True
        # Code's window process is Code.exe; the helper processes (crashpad,
        # server) differ. Scan the full list for known names as a fallback --
        # matched at word boundaries so unrelated processes never count.
        import re
        full = subprocess.run(
            ["tasklist"], capture_output=True, text=True, timeout=10,
        ).stdout
        pattern = re.compile(r"(^|\s)(?:code|vscode)(?:\.exe)?\s", re.IGNORECASE)
        if pattern.search(full):
            return True
    except Exception:
        pass
    return False
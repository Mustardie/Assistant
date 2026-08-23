"""The one place this package talks to a subprocess.

Everything else builds commands, converts plans, writes notes and reports.
Only this module runs anything, which is what lets the whole render path be
tested on a machine with no FFmpeg: the tests inject a recording runner and
assert on the commands that would have run.

Two runners ship:

``FFmpegRunner``
    The real one. Reports whether FFmpeg is installed, which encoders it has,
    and its version -- the last of those goes into the render cache key, so
    upgrading FFmpeg correctly re-renders rather than serving a video the new
    build would not have produced.

``MockRunner``
    Runs nothing. Writes a small placeholder where the video would be, and
    stamps ``mock`` on everything downstream. It exists for tests and for
    exercising the pipeline on a machine with no FFmpeg, and it is never
    quiet about what it is -- the same rule Session 10A's mock transcription
    backend follows, for the same reason: an artifact that reads as real and
    is not is worse than a missing one.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from editing.errors import ToolMissingError
from editing.render.schema import INSTALL_HINT

logger = logging.getLogger("nova.editing.render.runner")

#: Enough of an MP4 header that a player says "unsupported" rather than
#: "corrupt", and small enough that nobody mistakes it for a render.
MOCK_PAYLOAD = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    b"MOCK RENDER -- no video was produced. "
    b"This file exists so the pipeline has something to point at."
)

_VERSION_RE = re.compile(r"ffmpeg version (\S+)")
_ENCODER_RE = re.compile(r"^\s*[A-Z.]{6}\s+(\S+)")


@dataclass
class CommandResult:
    """What one invocation did."""

    command: list[str] = field(default_factory=list)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "elapsed": round(self.elapsed, 3),
            # Only the tail: an FFmpeg failure says what went wrong in the last
            # few lines, and the rest is banner and stream descriptions.
            "stderr": self.stderr[-2000:],
        }


class FFmpegRunner:
    """Runs FFmpeg, and answers what it can do.

    ``version`` and ``encoders`` are probed once and remembered: a render with
    two hundred segments must not shell out four hundred extra times to ask
    the same question.
    """

    name = "ffmpeg"

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"):
        self.ffmpeg = ffmpeg or "ffmpeg"
        self.ffprobe = ffprobe or "ffprobe"
        self._version: Optional[str] = None
        self._encoders: Optional[set] = None

    # -- capability ------------------------------------------------------

    def available(self) -> bool:
        return _have(self.ffmpeg) and _have(self.ffprobe)

    def version(self) -> str:
        if self._version is None:
            self._version = self._read_version()
        return self._version

    def _read_version(self) -> str:
        try:
            result = self.run(
                [self.ffmpeg, "-hide_banner", "-version"], timeout=30.0)
        except ToolMissingError:
            return ""
        match = _VERSION_RE.search(result.stdout or "")
        if match:
            return match.group(1)[:60]
        first = (result.stdout or "").splitlines()
        return first[0][:60] if first else ""

    def encoders(self) -> set:
        """Every encoder this build has, by name.

        Used to fall back from a hardware encoder that is named in the config
        but not compiled in -- which is the normal state of a machine that has
        FFmpeg from a package manager and an NVIDIA GPU.
        """
        if self._encoders is None:
            self._encoders = self._read_encoders()
        return self._encoders

    def _read_encoders(self) -> set:
        try:
            result = self.run(
                [self.ffmpeg, "-hide_banner", "-encoders"], timeout=60.0)
        except ToolMissingError:
            return set()
        found: set = set()
        for line in (result.stdout or "").splitlines():
            match = _ENCODER_RE.match(line)
            if match:
                found.add(match.group(1))
        return found

    # -- running ---------------------------------------------------------

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: float = 1800.0,
        log_path: Optional[Path] = None,
    ) -> CommandResult:
        """Run one command. A missing binary is a typed error, not a trace."""
        started = time.time()
        try:
            completed = subprocess.run(
                [str(part) for part in command],
                capture_output=True,
                timeout=timeout,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise ToolMissingError(
                f"'{command[0]}' is not installed or not on PATH",
                hint=INSTALL_HINT,
                detail={"command": str(command[0])},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                command=[str(part) for part in command],
                returncode=124,
                stderr=f"timed out after {timeout:.0f}s",
                elapsed=time.time() - started,
            )
            logger.warning("FFmpeg timed out: %s", exc)
            _log(log_path, result)
            return result

        result = CommandResult(
            command=[str(part) for part in command],
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            elapsed=time.time() - started,
        )
        _log(log_path, result)
        return result

    def probe(self, path: str | Path, *, timeout: float = 120.0) -> dict:
        """Technical metadata for a finished render, flattened.

        Deliberately reuses ``editing.ffmpeg``'s flattener rather than parsing
        ffprobe JSON a second way: two parsers for one format is two places
        for rotation handling to disagree.
        """
        from editing import ffmpeg as ff
        from editing.render.commands import probe_command

        result = self.run(
            probe_command(path, ffprobe=self.ffprobe), timeout=timeout)
        if not result.ok:
            return {}
        try:
            import json
            raw = json.loads(result.stdout or "{}")
        except ValueError:
            return {}
        return ff._flatten_probe(raw)

    def health(self) -> dict:
        """Whether a render could run right now, and what to do if not."""
        ready = self.available()
        return {
            "backend": self.name,
            "ready": ready,
            "ffmpeg": self.ffmpeg,
            "ffprobe": self.ffprobe,
            "ffmpeg_found": _have(self.ffmpeg),
            "ffprobe_found": _have(self.ffprobe),
            "version": self.version() if ready else "",
            "hint": "" if ready else INSTALL_HINT,
        }


class MockRunner:
    """Pretends nothing.

    Every command is recorded and none is run. Where FFmpeg would have written
    a file, this writes a labelled placeholder, so the surrounding code has a
    real path to carry -- and every artifact it touches says ``mock``.
    """

    name = "mock"

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
                 *, fail_on: Sequence[str] = ()):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.commands: list[list[str]] = []
        #: Substrings that make a command "fail", for exercising error paths.
        self.fail_on = list(fail_on)

    def available(self) -> bool:
        return True

    def version(self) -> str:
        return "mock"

    def encoders(self) -> set:
        return {"libx264", "libx265", "aac", "pcm_s16le", "libmp3lame"}

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: float = 1800.0,
        log_path: Optional[Path] = None,
    ) -> CommandResult:
        parts = [str(part) for part in command]
        self.commands.append(parts)
        line = " ".join(parts)
        for needle in self.fail_on:
            if needle in line:
                result = CommandResult(
                    command=parts, returncode=1,
                    stderr=f"mock failure triggered by {needle!r}",
                )
                _log(log_path, result)
                return result

        # Informational invocations produce no file.
        if "-version" in parts or "-encoders" in parts:
            return CommandResult(command=parts, stdout="mock")

        target = Path(parts[-1])
        if target.suffix and not target.is_dir():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(MOCK_PAYLOAD)
            except OSError as exc:  # pragma: no cover - a full disk, in a mock
                return CommandResult(
                    command=parts, returncode=1, stderr=str(exc))
        result = CommandResult(command=parts, returncode=0)
        _log(log_path, result)
        return result

    def probe(self, path: str | Path, *, timeout: float = 120.0) -> dict:
        """A plausible shape, with nothing measured.

        Zeroed durations on purpose: a mock that reported the duration the
        plan expected would make ``duration_drift`` look verified when nothing
        has been verified at all.
        """
        return {
            "duration": 0.0, "width": 0, "height": 0, "fps": 0.0,
            "has_audio": False, "video_codec": "", "size_bytes": 0,
            "container": "mock",
        }

    def health(self) -> dict:
        return {
            "backend": self.name,
            "ready": True,
            "version": "mock",
            "hint": "",
            "note": "the mock runner writes placeholders and renders nothing",
        }


def build_runner(config, *, backend: str = "ffmpeg"):
    """The runner a render should use, given the editing config.

    One function so the choice is made in one place. ``backend`` comes from
    ``RenderConfig``, and anything unrecognised gets the real one -- failing
    loudly on a missing FFmpeg beats silently producing placeholders.
    """
    ffmpeg = getattr(config, "ffmpeg", "ffmpeg")
    ffprobe = getattr(config, "ffprobe", "ffprobe")
    if str(backend).strip().lower() == "mock":
        return MockRunner(ffmpeg, ffprobe)
    return FFmpegRunner(ffmpeg, ffprobe)


def check(config, *, backend: str = "ffmpeg") -> dict:
    """Could a render run right now? Loads nothing and writes nothing."""
    return build_runner(config, backend=backend).health()


def _have(name: str) -> bool:
    if shutil.which(name):
        return True
    candidate = Path(name)
    return candidate.is_file()


def _log(log_path: Optional[Path], result: CommandResult) -> None:
    """Append one invocation to the job's log. Never raises.

    A render that succeeded but could not write its log is still a successful
    render, and losing it over a locked file would be absurd.
    """
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(
                f"\n$ {' '.join(result.command)}\n"
                f"  exit {result.returncode} in {result.elapsed:.2f}s\n"
            )
            if result.stderr.strip():
                handle.write("  " + result.stderr.strip()[-4000:] + "\n")
    except OSError as exc:  # pragma: no cover - logging must never fail a run
        logger.debug("Could not write render log: %s", exc)

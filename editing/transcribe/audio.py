"""Getting decodable audio out of whatever the user pointed at.

Usually this module does nothing, and that is the design. faster-whisper
decodes through PyAV, which reads MP4, MKV, MOV and most other containers
directly -- so the fast path is handing it the video path and extracting
nothing.

Extraction exists for the cases where that fails: an exotic container, a codec
PyAV was not built with, or a file whose audio stream PyAV picks wrong. When it
is needed, FFmpeg writes a 16 kHz mono WAV -- exactly what Whisper resamples to
internally anyway, so nothing is lost by doing it a step earlier.

## Two rules

* **The source file is never touched.** Extraction only ever writes to the
  cache directory, never beside the footage. Someone pointing this at a folder
  of irreplaceable captures should get no new files in it.
* **A missing FFmpeg is explained, not swallowed.** It is only *needed* on the
  fallback path, so the error says that too -- "install FFmpeg, or the direct
  path may work" is more useful than "ffmpeg not found".
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from editing import ffmpeg as ff
from editing.errors import EditingError, ToolMissingError
from editing.transcribe.schema import (
    AUDIO_EXTENSIONS, MEDIA_EXTENSIONS, VIDEO_EXTENSIONS,
)

logger = logging.getLogger("nova.editing.transcribe.audio")

#: What Whisper works in. Extracting to anything else means it resamples again.
SAMPLE_RATE = 16000
CHANNELS = 1

#: Extraction of a 40-minute capture takes seconds; this is generous enough
#: for a slow external drive and short enough to fail rather than hang.
EXTRACT_TIMEOUT = 900.0


def is_media(path: str | Path) -> bool:
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


def is_audio_only(path: str | Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def find_media(
    root: str | Path, *, recursive: bool = True, limit: int = 2000
) -> list[Path]:
    """Every media file under ``root``, sorted, deduplicated by resolved path.

    Sorted so a batch is reproducible and its summary is comparable run to run.
    """
    base = Path(root).expanduser()
    if base.is_file():
        return [base] if is_media(base) else []
    if not base.is_dir():
        raise EditingError(
            f"'{base}' is not a file or a folder",
            hint="Point --folder at a directory of clips, or use "
                 "`transcribe file <path>` for one file.",
            detail={"path": str(base)},
        )

    pattern = "**/*" if recursive else "*"
    seen: set[str] = set()
    out: list[Path] = []
    for candidate in sorted(base.glob(pattern)):
        if not candidate.is_file() or not is_media(candidate):
            continue
        # A folder reached twice through a junction would otherwise transcribe
        # the same capture twice and write two jobs for it.
        key = str(candidate.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
        if len(out) >= limit:
            logger.warning("Stopping media scan at %d files", limit)
            break
    return out


def check_readable(path: str | Path) -> Path:
    """The path, or a typed error saying exactly what is wrong with it."""
    target = Path(path).expanduser()
    if not target.exists():
        raise EditingError(
            f"'{target}' does not exist",
            hint="Check the path. On Windows, wrap paths containing spaces "
                 "in quotes.",
            detail={"path": str(target)},
        )
    if target.is_dir():
        raise EditingError(
            f"'{target}' is a folder, not a file",
            hint="Use `transcribe folder <path>` for a whole directory.",
            detail={"path": str(target)},
        )
    if not is_media(target):
        raise EditingError(
            f"'{target.name}' is not a media file this can transcribe",
            hint="Supported: " + ", ".join(MEDIA_EXTENSIONS),
            detail={"path": str(target), "suffix": target.suffix},
        )
    try:
        if target.stat().st_size == 0:
            raise EditingError(
                f"'{target.name}' is empty",
                hint="The file is zero bytes -- check the copy or export "
                     "finished.",
                detail={"path": str(target)},
            )
    except OSError as exc:
        raise EditingError(
            f"'{target.name}' could not be read",
            hint="Check the drive is connected and the file is not locked by "
                 "another program.",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc
    return target


def extracted_path(cache_dir: str | Path, source: str | Path,
                   fingerprint_key: str = "") -> Path:
    """Where an extracted WAV for this source belongs.

    Under the cache directory, keyed by content, so two files with the same
    name in different folders cannot collide and a re-run reuses the WAV.
    """
    from editing.schema import short_hash

    name = Path(source).stem[:40] or "audio"
    digest = short_hash(str(source), fingerprint_key, length=10)
    return Path(cache_dir) / "audio" / f"{name}-{digest}.wav"


def extract_audio(
    source: str | Path,
    destination: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    timeout: float = EXTRACT_TIMEOUT,
    overwrite: bool = False,
) -> Path:
    """Write 16 kHz mono PCM audio for ``source``. Never touches the source.

    Returns the destination, which is reused when it already exists -- a batch
    re-run should not pay for extraction twice.
    """
    src = check_readable(source)
    target = Path(destination)
    if target.exists() and target.stat().st_size > 0 and not overwrite:
        logger.debug("Reusing extracted audio %s", target)
        return target

    if target.resolve() == src.resolve():
        raise EditingError(
            "Refusing to extract a file over itself",
            hint="This is a bug: extraction must write into the cache "
                 "directory.",
            detail={"path": str(src)},
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vn",                          # never re-encode the picture
        "-ac", str(CHANNELS),
        "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        "-y", str(target),
    ]

    try:
        completed = ff._run(command, timeout=timeout)
    except ToolMissingError as exc:
        raise ToolMissingError(
            "FFmpeg is needed to extract audio from this file, and is not "
            "installed",
            hint="Install FFmpeg and put it on PATH, or set EDITING_FFMPEG. "
                 "Most files transcribe without it -- this path is only used "
                 "when the container cannot be decoded directly.",
            detail={"path": str(src), "reason": exc.message},
        ) from exc

    if completed.returncode != 0 or not target.exists() \
            or target.stat().st_size == 0:
        stderr = (completed.stderr or "").strip()[-400:]
        hint = ("The file may have no audio track, or a codec FFmpeg cannot "
                "read. `ffprobe <file>` will say which.")
        if "does not contain any stream" in stderr.lower() \
                or "no audio" in stderr.lower():
            hint = "This file has no audio track, so there is nothing to " \
                   "transcribe."
        # A partial file is worse than none: a later run would reuse it.
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        raise EditingError(
            f"Could not extract audio from '{src.name}'",
            hint=hint,
            detail={"path": str(src), "returncode": completed.returncode,
                    "stderr": stderr},
        )
    return target


def prepare(
    source: str | Path,
    *,
    cache_dir: str | Path,
    force_extract: bool = False,
    fingerprint_key: str = "",
    ffmpeg: str = "ffmpeg",
) -> tuple[Path, bool]:
    """The path to hand a backend, and whether it was extracted.

    The default is to hand over the source and let the backend decode it. This
    only extracts when asked -- ``run`` falls back to calling it again with
    ``force_extract`` after a direct decode fails, which keeps the fast path
    fast and the slow path available.
    """
    src = check_readable(source)
    if not force_extract:
        return src, False
    destination = extracted_path(cache_dir, src, fingerprint_key)
    return extract_audio(src, destination, ffmpeg=ffmpeg), True


def cleanup_extracted(path: Optional[Path]) -> None:
    """Remove an extracted WAV. Never raises, and never touches a source."""
    if path is None:
        return
    try:
        target = Path(path)
        if target.exists() and target.suffix.lower() == ".wav" \
                and "audio" in target.parts:
            target.unlink()
    except OSError as exc:  # noqa: BLE001 - tidying is best-effort
        logger.debug("Could not remove %s: %s", path, exc)

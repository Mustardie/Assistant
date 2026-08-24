"""Map local file intent to an application without executing anything."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tools.desktop_models import DesktopRisk


EXECUTABLE_EXTENSIONS = {".exe", ".com", ".scr", ".msi", ".msix", ".appx"}
SCRIPT_EXTENSIONS = {".bat", ".cmd", ".ps1", ".vbs", ".js", ".wsf", ".reg"}
CODE_EXTENSIONS = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".html",
    ".css", ".scss", ".json", ".yaml", ".yml", ".toml", ".xml", ".sql",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}


@dataclass(frozen=True)
class FileAppChoice:
    app: str
    reason: str
    risk: DesktopRisk = DesktopRisk.LOW
    requires_confirmation: bool = False
    use_shell_default: bool = False

    def to_dict(self) -> dict:
        return {
            "app": self.app, "reason": self.reason, "risk": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
            "use_shell_default": self.use_shell_default,
        }


def _risky_location(path: Path) -> bool:
    raw = str(path)
    if raw.startswith("\\\\"):
        return True
    lowered = raw.lower()
    temp_roots = [os.environ.get("TEMP", ""), os.environ.get("TMP", "")]
    return any(root and lowered.startswith(str(Path(root)).lower()) for root in temp_roots) or any(
        marker in lowered for marker in ("\\appdata\\local\\temp\\", "\\inetcache\\", "\\browser cache\\")
    )


def choose_app_for_path(path: Path | str) -> FileAppChoice:
    target = Path(path)
    if target.is_dir():
        return FileAppChoice("File Explorer", "folders open in File Explorer")
    extension = target.suffix.lower()
    if extension in EXECUTABLE_EXTENSIONS:
        action = "installer" if extension in {".msi", ".msix", ".appx"} or "setup" in target.stem.lower() or "install" in target.stem.lower() else "executable"
        return FileAppChoice("Windows Shell", f"{action} files can execute code", DesktopRisk.HIGH, True, True)
    if extension in SCRIPT_EXTENSIONS:
        return FileAppChoice("Visual Studio Code", "script files are opened for inspection, not execution", DesktopRisk.MEDIUM, _risky_location(target))
    if extension == ".prproj":
        return FileAppChoice("Adobe Premiere Pro", "Premiere project extension")
    if extension == ".blend":
        return FileAppChoice("Blender", "Blender project extension")
    if extension in CODE_EXTENSIONS:
        return FileAppChoice("Visual Studio Code", "source/configuration file extension")
    if extension == ".pdf":
        return FileAppChoice("Microsoft Edge", "PDF document extension")
    if extension in {".log", ".txt", ".md", ".csv"}:
        return FileAppChoice("Notepad", "plain-text file extension")
    if extension in IMAGE_EXTENSIONS:
        return FileAppChoice("Microsoft Photos", "image file extension", use_shell_default=True)
    if extension in VIDEO_EXTENSIONS:
        return FileAppChoice("Default video player", "video file extension", use_shell_default=True)
    if extension in AUDIO_EXTENSIONS:
        return FileAppChoice("Default audio player", "audio file extension", use_shell_default=True)
    if _risky_location(target):
        return FileAppChoice("Windows Shell", "unknown file type in a temporary, cache, or network location", DesktopRisk.MEDIUM, True, True)
    return FileAppChoice("Windows Shell", "no explicit mapping; use the registered Windows file association", use_shell_default=True)


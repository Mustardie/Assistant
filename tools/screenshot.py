"""Screenshot capture tool.

Provides a single user-facing function `capture_screenshot()` that:
- Captures the full primary monitor
- Saves as a timestamped PNG
- Returns the file path
- Allows an optional custom directory
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def capture_screenshot(directory: Optional[str] = None) -> Dict[str, Any]:
    """Capture a full-screen screenshot, save it to disk, and return the path.

    Args:
        directory: Optional save directory (default: user's Pictures/Screenshots or Desktop).

    Returns:
        {"success": True, "path": "...", "size": ..., "width": ..., "height": ...}
        or {"success": False, "error": "..."}
    """
    try:
        from PIL import Image
        import mss
    except ImportError as exc:
        return {"success": False, "error": f"Missing dependency: {exc}. Install pillow and mss."}

    # Determine save directory
    save_dir = _resolve_dir(directory)
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return {"success": False, "error": f"Cannot create save directory: {exc}"}

    # Capture
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # primary monitor
            screenshot = sct.grab(monitor)
            image = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
    except Exception as exc:
        logger.exception("Screenshot capture failed")
        return {"success": False, "error": f"Screenshot capture failed: {exc}"}

    # Save with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"screenshot_{timestamp}.png"
    save_path = save_dir / filename

    try:
        image.save(save_path, "PNG")
    except Exception as exc:
        return {"success": False, "error": f"Failed to save screenshot: {exc}"}

    width, height = image.size
    file_size = save_path.stat().st_size

    logger.info("Screenshot saved: %s (%dx%d, %d bytes)", save_path, width, height, file_size)

    return {
        "success": True,
        "path": str(save_path),
        "filename": filename,
        "width": width,
        "height": height,
        "size": file_size,
        "size_display": _format_size(file_size),
    }


def _resolve_dir(directory: Optional[str]) -> Path:
    if directory:
        return Path(directory).expanduser().resolve()

    pictures = Path.home() / "Pictures" / "Screenshots"
    if pictures.parent.exists():
        return pictures
    return Path.home() / "Desktop"


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / 1024 ** 2:.1f} MB"

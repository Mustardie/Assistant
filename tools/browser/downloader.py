import logging
from pathlib import Path

from .element_finder import ElementNotFoundError, resolve

logger = logging.getLogger(__name__)

DEFAULT_DOWNLOAD_DIR = Path("F:/Assistant/data/downloads")


def download_via(page, trigger_description: str, destination_dir: str | None = None, timeout: int = 30000) -> dict:
    """Clicks the given trigger element and waits for the resulting download
    to finish, saving it to destination_dir (default: a fixed downloads
    folder) and returning where it actually landed."""
    try:
        resolved = resolve(page, trigger_description)
    except ElementNotFoundError as exc:
        return {"success": False, "error": str(exc)}

    target_dir = Path(destination_dir).expanduser().resolve() if destination_dir else DEFAULT_DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with page.expect_download(timeout=timeout) as download_info:
            resolved.locator.click(timeout=8000)
        download = download_info.value
    except Exception as exc:
        return {"success": False, "error": f"Download did not start or complete: {exc}"}

    suggested_name = download.suggested_filename
    save_path = target_dir / suggested_name

    try:
        download.save_as(str(save_path))
    except Exception as exc:
        return {"success": False, "error": f"Download completed but could not be saved: {exc}"}

    if not save_path.exists():
        return {"success": False, "error": f"Expected downloaded file at {save_path} but it isn't there."}

    return {
        "success": True,
        "filename": suggested_name,
        "path": str(save_path),
        "size_bytes": save_path.stat().st_size,
    }

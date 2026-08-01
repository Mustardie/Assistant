"""Content fetching: visit pages, download images and PDFs, robustly."""

import logging
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
_TIMEOUT = 25


def fetch_page(url: str) -> str:
    """Fetch a page and return its text content. Returns "" on failure."""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response.text
    except Exception as exc:
        logger.warning("Could not fetch %s: %s", url, exc)
        return ""


def _strip_html(html: str) -> str:
    """Very small HTML-to-text: strip tags, collapse whitespace, keep some
    paragraph breaks. Not a full parser — good enough for snippets."""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</p>", "\n\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", html)
    return text.strip()


def extract_text(html: str, max_chars: int = 6000) -> str:
    text = _strip_html(html)
    return text[:max_chars]


def extract_images(html: str, base_url: str, limit: int = 8) -> list:
    """Pull image URLs out of a page's HTML (best effort)."""
    urls = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    found = []
    for src in urls:
        if src.startswith("data:") or src.startswith("blob:"):
            continue
        if not src.lower().endswith(_IMAGE_EXTENSIONS):
            continue
        absolute = requests.compat.urljoin(base_url, src)
        found.append(absolute)
        if len(found) >= limit:
            break
    return found


def download_image(url: str, destination: Path) -> bool:
    """Download one image into destination (already a full path)."""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
            stream=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "image" not in content_type:
            logger.warning("Skipping non-image content at %s (%s)", url, content_type)
            return False
        with open(destination, "wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)
        if destination.stat().st_size < 2048:
            logger.warning("Image too small (likely broken): %s (%s bytes)", url, destination.stat().st_size)
            destination.unlink(missing_ok=True)
            return False
        logger.info("Downloading image... %s", destination.name)
        return True
    except Exception as exc:
        logger.warning("Image download failed for %s: %s", url, exc)
        if destination.exists():
            destination.unlink(missing_ok=True)
        return False


def download_pdf(url: str, destination: Path) -> bool:
    """Download a PDF to destination. Returns False if not a real PDF."""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
            stream=True,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        looks_pdf = "pdf" in content_type or url.lower().endswith(".pdf")
        if not looks_pdf:
            logger.warning("Skipping non-PDF at %s (%s)", url, content_type)
            return False
        with open(destination, "wb") as handle:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    handle.write(chunk)
        logger.info("Downloading paper... %s", destination.name)
        return True
    except Exception as exc:
        logger.warning("PDF download failed for %s: %s", url, exc)
        return False

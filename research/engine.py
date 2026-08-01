"""Research engine: orchestrates the full pipeline.

User -> search (provider) -> fetch pages -> extract -> collect images
-> collect papers -> generate DOCX -> save -> return folder path.
"""

import logging
import re
import threading
from datetime import datetime
from pathlib import Path

import requests

from config.settings import settings
from research import fetcher
from research.docx_report import DocxReportBuilder
from research.models import ImageAsset, PaperInfo, ResearchBundle, SourceInfo
from research.providers import get_provider
from research.sources import write_sources_txt

logger = logging.getLogger(__name__)

_ACADEMIC_DOMAINS = (
    "arxiv.org",
    "ieee.org",
    "acm.org",
    "nature.com",
    "springer.com",
    "elsevier.com",
    "sciencedirect.com",
    "semanticscholar.org",
    "jstor.org",
)


def _slugify(topic: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", topic).strip()
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug or "research"


def _build_queries(topic: str) -> list:
    """A small set of queries that together cover the topic well."""
    return [
        topic,
        f"{topic} overview and history",
        f"{topic} latest research and developments",
        f"{topic} applications and future",
    ]


class ResearchEngine:
    """One-shot research run for a single topic."""

    def __init__(self, topic: str, max_sources: int | None = None):
        self.topic = topic.strip()
        self.max_sources = max_sources or settings.research_max_sources
        self.bundle = ResearchBundle(topic=self.topic)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # folder structure
    # ------------------------------------------------------------------ #

    def _create_structure(self) -> dict:
        base = Path(settings.research_dir)
        folder = base / _slugify(self.topic)
        structure = {
            "research": folder / "Research",
            "images": folder / "Images",
            "sources": folder / "Sources",
        }
        for path in structure.values():
            path.mkdir(parents=True, exist_ok=True)
        self.bundle.folder = str(folder)
        logger.info("Research folder: %s", folder)
        return structure

    # ------------------------------------------------------------------ #
    # search
    # ------------------------------------------------------------------ #

    def _search_all(self, provider, max_sources: int) -> list:
        collected = []
        seen_urls = set()
        for query in _build_queries(self.topic):
            logger.info("Searching Tavily... query=%r", query)
            try:
                results = provider.search(query, max_results=min(10, max_sources))
            except Exception as exc:
                logger.warning("Search failed for %r: %s", query, exc)
                continue
            for result in results:
                if result.url in seen_urls:
                    continue
                seen_urls.add(result.url)
                collected.append(result)
                if len(collected) >= max_sources:
                    break
            if len(collected) >= max_sources:
                break
        logger.info("Found %d sources", len(collected))
        return collected

    def _search_papers(self, provider, limit: int) -> list:
        """Search academic venues specifically for research papers."""
        papers = []
        for query in (f"{self.topic} paper", f"{self.topic} arXiv"):
            logger.info("Searching Tavily for papers... query=%r", query)
            try:
                results = provider.search(query, max_results=limit * 2)
            except Exception as exc:
                logger.warning("Paper search failed for %r: %s", query, exc)
                continue
            for result in results:
                if not any(domain in result.url for domain in _ACADEMIC_DOMAINS):
                    continue
                if result.url.startswith("https://arxiv.org/abs/"):
                    result.url = result.url.replace("/abs/", "/pdf/")
                papers.append(result)
                if len(papers) >= limit:
                    break
            if len(papers) >= limit:
                break
        return papers

    # ------------------------------------------------------------------ #
    # images
    # ------------------------------------------------------------------ #

    def _collect_images(self, provider, structure, limit: int) -> list:
        """Try Tavily image search first, then fall back to scraping
        <img> tags from the collected source pages."""
        images = []
        try:
            response = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.tavily_api_key,
                    "query": self.topic,
                    "search_type": "images",
                    "max_results": limit,
                },
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            for raw in payload.get("images", [])[:limit]:
                url = raw.get("url", "")
                if not url:
                    continue
                images.append(ImageAsset(url=url, alt=raw.get("description", "")))
        except Exception as exc:
            logger.warning("Tavily image search failed: %s", exc)

        # Fallback: scrape images from the source pages we already found.
        if not images:
            logger.info("Tavily image search empty; scraping source pages for images")
            for source in self.bundle.sources[:4]:
                html = fetcher.fetch_page(source.url)
                if not html:
                    continue
                for img_url in fetcher.extract_images(html, source.url, limit=3):
                    images.append(ImageAsset(url=img_url, alt=source.title))
                    if len(images) >= limit:
                        break
                if len(images) >= limit:
                    break

        for index, image in enumerate(images):
            ext = Path(image.url.split("?")[0]).suffix or ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
                ext = ".jpg"
            destination = structure["images"] / f"image{index + 1}{ext}"
            if fetcher.download_image(image.url, destination):
                image.path = str(destination)
                image.caption = image.alt or f"{self.topic} — image {index + 1}"
        return [img for img in images if img.path]

    # ------------------------------------------------------------------ #
    # papers
    # ------------------------------------------------------------------ #

    def _collect_papers(self, provider, structure, limit: int) -> list:
        candidates = self._search_papers(provider, limit=limit)
        papers = []
        for index, candidate in enumerate(candidates):
            paper = PaperInfo(
                title=candidate.title,
                url=candidate.url,
                publication=_publication_name(candidate.url),
                abstract=candidate.snippet[:1200] if candidate.snippet else "",
            )
            paper.authors = _extract_authors(candidate.title)
            destination = structure["research"] / f"Paper {index + 1}.pdf"
            if candidate.url.lower().endswith(".pdf") or "/pdf/" in candidate.url:
                if fetcher.download_pdf(candidate.url, destination):
                    paper.pdf_path = str(destination)
                    paper.downloaded = True
            papers.append(paper)
            if len(papers) >= limit:
                break
        return papers

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    def run(self) -> ResearchBundle:
        provider = get_provider()
        structure = self._create_structure()

        max_sources = self.max_sources
        results = self._search_all(provider, max_sources)
        if not results:
            logger.error("No sources found for topic %r", self.topic)
            raise RuntimeError(f"No search results found for '{self.topic}'.")

        today = datetime.now().strftime("%Y-%m-%d")
        self.bundle.sources = [
            SourceInfo(
                title=r.title or r.url,
                url=r.url,
                source_type=r.source_type,
                accessed_at=today,
                snippet=r.snippet,
            )
            for r in results
        ]

        logger.info("Collecting images...")
        self.bundle.images = self._collect_images(
            provider, structure, settings.research_max_images
        )

        logger.info("Collecting research papers...")
        self.bundle.papers = self._collect_papers(
            provider, structure, settings.research_max_papers
        )

        logger.info("Writing sources manifest...")
        write_sources_txt(self.bundle, structure["sources"])

        logger.info("Building DOCX report...")
        report_path = (
            structure["research"] / f"{_slugify(self.topic)} Research.docx"
        )
        builder = DocxReportBuilder(self.bundle)
        self.bundle.report_path = str(builder.build(report_path))

        logger.info("Completed.")
        return self.bundle


def _publication_name(url: str) -> str:
    lowered = url.lower()
    for name in (
        "arxiv.org",
        "ieee.org",
        "acm.org",
        "nature.com",
        "springer.com",
        "sciencedirect.com",
        "semanticscholar.org",
        "jstor.org",
    ):
        if name in lowered:
            return name.replace(".org", "").replace(".com", "").title()
    return "Academic"


def _extract_authors(title: str) -> str:
    """Best-effort: authors rarely appear in a search title; we return
    an empty string and let the report show '—'."""
    return ""

"""Tavily research provider."""

import logging

from research.providers.base import ResearchProvider, ResearchProviderError
from research.models import SearchResult

logger = logging.getLogger(__name__)

# Domains we prefer when the model gives us a choice or for re-ranking.
_PREFERRED_DOMAINS = (
    "wikipedia.org",
    ".edu",
    ".gov",
    "arxiv.org",
    "nature.com",
    "ieee.org",
    "acm.org",
    "springer.com",
    "elsevier.com",
    "scholar.google.com",
)


class TavilyProvider(ResearchProvider):
    """Search provider backed by the Tavily API (https://tavily.com).

    Returns rich results (title/url/snippet + content) and keeps URLs
    through the whole pipeline so every statement can be traced.
    """

    name = "tavily"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from tavily import TavilyClient

                self._client = TavilyClient(api_key=self.api_key)
            except Exception as exc:
                raise ResearchProviderError(
                    f"Could not initialise Tavily client: {exc}"
                ) from exc
        return self._client

    def search(self, query: str, max_results: int = 10) -> list:
        client = self._get_client()
        try:
            response = client.search(
                query=query,
                max_results=max_results,
                include_answer=False,
                include_raw_content=False,
                search_depth="advanced",
            )
        except Exception as exc:
            raise ResearchProviderError(
                f"Tavily search failed for {query!r}: {exc}"
            ) from exc

        results = []
        for raw in response.get("results", []):
            url = raw.get("url", "")
            if not url:
                continue
            results.append(
                SearchResult(
                    title=raw.get("title", "").strip(),
                    url=url,
                    snippet=(raw.get("content") or raw.get("raw_content") or "").strip(),
                    source_type=self._classify(url),
                )
            )
        logger.info("Tavily returned %d results for %r", len(results), query)
        return results

    @staticmethod
    def _classify(url: str) -> str:
        lowered = url.lower()
        if "wikipedia.org" in lowered:
            return "encyclopedia"
        if ".edu" in lowered:
            return "education"
        if ".gov" in lowered:
            return "government"
        if any(d in lowered for d in ("arxiv.org", "nature.com", "ieee.org", "acm.org", "springer.com", "elsevier.com")):
            return "academic"
        if any(d in lowered for d in ("scholar.google.com", "semanticscholar.org", "doi.org")):
            return "scholarly"
        return "web"

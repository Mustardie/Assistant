"""Research provider abstraction. A provider turns a query into a list of
SearchResult objects with titles, URLs, snippets, and (optionally) full
page content. Tavily is the primary provider; future providers (Brave,
Google CSE, etc.) implement the same interface."""

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class ResearchProvider(ABC):
    """Interface every research provider must implement."""

    name = "base"

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> list:
        """Run one search query. Returns a list of SearchResult."""
        raise NotImplementedError


class ResearchProviderError(Exception):
    """Raised when a research provider fails in a non-recoverable way."""

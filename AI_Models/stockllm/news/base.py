"""News provider abstraction (V3).

A provider supplies historical news-like items for a ticker with exact
publication timestamps.  Adapters (EDGAR for US, NSE for India) implement
`NewsProvider`; the service layer (news.service) handles windowing, retries,
rate limits, caching and point-in-time enforcement on top.

Documented limitation of both providers: they expose *company disclosures*
(SEC filings / NSE corporate announcements) with titles and metadata only --
no general media coverage and no full article text.  Nothing is synthesized;
an item's publication timestamp is the source's own timestamp.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Article:
    ticker: str
    ts: pd.Timestamp          # publication timestamp (UTC, naive)
    title: str
    url: str                  # stable identifier / permalink
    source: str               # domain / provider
    snippet: str = ""
    identifier: str = ""      # provider-specific stable id (e.g. accession)
    fetched_at: str = ""      # set by the service at ingestion time


class NewsProvider(ABC):
    """Interface every news source adapter must implement."""

    name: str = "base"

    @abstractmethod
    def supports(self, ticker: str) -> bool:
        """Whether this provider can serve the ticker."""

    @abstractmethod
    def query_for(self, ticker: str) -> str:
        """Human-readable query string used for logging/audit."""

    @abstractmethod
    def fetch(self, ticker: str, start: pd.Timestamp,
              end: pd.Timestamp) -> list[Article]:
        """All articles published in [start, end) for the ticker.

        Must never return items outside the window and must never raise for
        missing data (return [] instead); transport errors may raise and the
        service layer retries them.
        """

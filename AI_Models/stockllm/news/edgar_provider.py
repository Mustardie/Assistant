"""EDGAR news provider: SEC filings as corporate-news events (US tickers).

Source: SEC EDGAR submissions API (https://data.sec.gov/submissions/CIK*.json),
no API key required.  Every 8-K / 10-K / 10-Q filing carries its exact filing
date, an accession number (stable identifier) and a permanent archive URL.

What this is / is not: regulatory disclosures with titles and metadata only --
no media coverage, no article body.  The filing date is the availability
timestamp (point-in-time exact).
"""
from __future__ import annotations

import json
import urllib.request

import pandas as pd

import config
from news.base import Article, NewsProvider
from utils.logging import get_logger

log = get_logger(__name__)

_UA = {"User-Agent": "StockLLM Research mailto:stockllm-research@example.com"}
_FORMS = tuple(config.EDGAR_NEWS_FORMS)


def cik_for_ticker(ticker: str) -> int | None:
    """CIK lookup via the EDGAR browse API (None when the ticker is not US)."""
    if "." in ticker:
        return None
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={ticker}&type=10-K&dateb=&owner=include&count=1&output=atom")
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=config.NEWS_REQUEST_TIMEOUT) as resp:
            body = resp.read(200_000).decode("utf-8", errors="ignore")
        marker = "<cik>"
        idx = body.find(marker)
        if idx < 0:
            return None
        end = body.find("</cik>", idx)
        return int(body[idx + len(marker):end].strip())
    except Exception as exc:
        log.warning("%s: CIK lookup failed (%s)", ticker, str(exc)[:100])
        return None


class EdgarNewsProvider(NewsProvider):
    name = "edgar"

    def __init__(self, cik: int | None = None):
        self._cik = cik

    def supports(self, ticker: str) -> bool:
        return "." not in ticker

    def query_for(self, ticker: str) -> str:
        return f"cik={self._cik or cik_for_ticker(ticker)}"

    def _payload(self) -> dict | None:
        if self._cik is None:
            return None
        url = f"https://data.sec.gov/submissions/CIK{self._cik:010d}.json"
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=config.NEWS_REQUEST_TIMEOUT) as resp:
            return json.load(resp)

    def fetch(self, ticker: str, start: pd.Timestamp,
              end: pd.Timestamp) -> list[Article]:
        if self._cik is None:
            return []
        payload = self._payload()
        if payload is None:
            return []
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        docs = recent.get("primaryDocument", [])
        descs = recent.get("primaryDocDescription", [])
        out: list[Article] = []
        for form, date, accn, doc, desc in zip(forms, dates, accns, docs, descs):
            if form not in _FORMS:
                continue
            ts = pd.Timestamp(date)
            if not (start <= ts < end):
                continue
            accn_clean = accn.replace("-", "")
            url = (f"https://www.sec.gov/Archives/edgar/data/{self._cik}/"
                   f"{accn_clean}/{doc}")
            title = (desc or form) if isinstance(desc, str) and desc else form
            out.append(Article(
                ticker=ticker, ts=ts, title=title, url=url,
                source="sec.gov", snippet=title,
                identifier=f"CIK{self._cik}-{accn}",
            ))
        return out

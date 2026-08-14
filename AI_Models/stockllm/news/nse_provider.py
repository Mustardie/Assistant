"""NSE news provider: corporate announcements as news events (Indian tickers).

Source: NSE India corporate-announcements API (public website data, no API
key).  Every announcement carries an exact timestamp (`an_dt`, IST), a
subject line and a permanent attachment URL on nsearchives.nseindia.com.

Access pattern: NSE's anti-bot layer rejects plain urllib (HTTP 403) based on
TLS fingerprint, so this provider uses `curl_cffi` with Chrome impersonation
plus a homepage cookie bootstrap.  The service layer retries whole calls with
backoff when NSE still refuses (failures are reported, never silent).

What this is / is not: exchange-filed corporate disclosures (results, media
releases, board meetings, allotments...) with title/metadata only -- no media
coverage, no article body.  Timestamps are converted IST -> UTC.
"""
from __future__ import annotations

import json

import pandas as pd

import config
from news.base import Article, NewsProvider
from utils.logging import get_logger

log = get_logger(__name__)

_HOME = "https://www.nseindia.com"
_API = ("https://www.nseindia.com/api/corporate-announcements"
        "?index=equities&symbol={symbol}&from_date={start}&to_date={end}")


def _parse_ist(an_dt: str) -> pd.Timestamp | None:
    """Parse NSE '30-Jun-2023 20:28:36' (IST) into UTC-naive Timestamp."""
    try:
        local = pd.to_datetime(an_dt, format="%d-%b-%Y %H:%M:%S")
    except ValueError:
        return None
    return local.tz_localize("Asia/Kolkata").tz_convert("UTC").tz_localize(None)


class NseNewsProvider(NewsProvider):
    name = "nse"

    def supports(self, ticker: str) -> bool:
        return ticker.endswith(".NS") and ticker[:-3] in config.NSE_SYMBOLS

    def query_for(self, ticker: str) -> str:
        return f"symbol={ticker[:-3]}"

    def _session(self):
        """Chrome-impersonating session (overridable in tests)."""
        from curl_cffi import requests as creq
        return creq.Session(impersonate="chrome")

    def _announcements(self, symbol: str, start: str, end: str) -> list[dict]:
        session = self._session()
        try:
            session.get(_HOME, timeout=config.NEWS_REQUEST_TIMEOUT)
            url = _API.format(symbol=symbol, start=start, end=end)
            resp = session.get(url, timeout=config.NEWS_REQUEST_TIMEOUT)
            payload = resp.json()
        finally:
            session.close()
        return payload if isinstance(payload, list) else []

    def fetch(self, ticker: str, start: pd.Timestamp,
              end: pd.Timestamp) -> list[Article]:
        symbol = ticker[:-3]
        payload = self._announcements(
            symbol,
            start.strftime("%d-%m-%Y"), (end - pd.Timedelta(days=1)).strftime("%d-%m-%Y"))
        out: list[Article] = []
        for row in payload:
            ts = _parse_ist(row.get("an_dt", ""))
            if ts is None:
                continue
            if not (start <= ts < end):
                continue
            text = row.get("attchmntText") or ""
            url = row.get("attchmntFile") or ""
            out.append(Article(
                ticker=ticker, ts=ts, title=text[:300], url=url,
                source="nseindia.com", snippet=text,
                identifier=url or f"{symbol}-{row.get('an_dt', '')}",
            ))
        return out


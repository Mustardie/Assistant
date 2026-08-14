"""Provider adapter tests: EDGAR and NSE parsing with mocked HTTP."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

from news.edgar_provider import EdgarNewsProvider, cik_for_ticker
from news.nse_provider import NseNewsProvider, _parse_ist


# ---------------------------------------------------------------------------
# EDGAR provider
# ---------------------------------------------------------------------------

def _edgar_payload(rows):
    """rows: (form, filingDate, accession, primaryDocument, desc)."""
    return {
        "filings": {"recent": {
            "form": [r[0] for r in rows],
            "filingDate": [r[1] for r in rows],
            "accessionNumber": [r[2] for r in rows],
            "primaryDocument": [r[3] for r in rows],
            "primaryDocDescription": [r[4] for r in rows],
        }}
    }


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_edgar_parses_articles(monkeypatch):
    rows = [
        ("8-K", "2023-06-27", "0001045810-23-000123", "nvda8k.htm", "Press release"),
        ("10-Q", "2023-08-30", "0001045810-23-000456", "nvda10q.htm", "10-Q"),
        ("8-K", "2024-01-01", "0001045810-24-000001", "a.htm", "x"),  # outside window
        ("SC 13D", "2023-06-10", "0001045810-23-000001", "b.htm", "x"),  # not a news form
    ]
    provider = EdgarNewsProvider(cik=1045810)
    monkeypatch.setattr("news.edgar_provider.urllib.request.urlopen",
                        lambda req, timeout: _Resp(_edgar_payload(rows)))
    arts = provider.fetch("NVDA", pd.Timestamp("2023-01-01"), pd.Timestamp("2024-01-01"))
    assert len(arts) == 2
    a = arts[0]
    assert a.ts == pd.Timestamp("2023-06-27")
    assert a.title == "Press release"
    assert "000104581023000123" in a.url
    assert a.url.startswith("https://www.sec.gov/Archives/edgar/data/1045810/")
    assert a.source == "sec.gov"
    assert a.identifier.endswith("0001045810-23-000123")


def test_edgar_empty_payload(monkeypatch):
    provider = EdgarNewsProvider(cik=1045810)
    monkeypatch.setattr("news.edgar_provider.urllib.request.urlopen",
                        lambda req, timeout: _Resp(_edgar_payload([])))
    assert provider.fetch("NVDA", pd.Timestamp("2023-01-01"), pd.Timestamp("2024-01-01")) == []


def test_edgar_missing_cik_returns_empty():
    provider = EdgarNewsProvider(cik=None)
    assert provider.fetch("NVDA", pd.Timestamp("2023-01-01"),
                          pd.Timestamp("2024-01-01")) == []


def test_cik_lookup(monkeypatch):
    body = b"""<?xml version="1.0"?><feed><entry><cik>0001045810</cik>"
               <name>NVIDIA CORP</name></entry></feed>"""

    class _RawResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return body

    monkeypatch.setattr("news.edgar_provider.urllib.request.urlopen",
                        lambda req, timeout: _RawResp())
    assert cik_for_ticker("NVDA") == 1045810
    assert cik_for_ticker("RELIANCE.NS") is None


# ---------------------------------------------------------------------------
# NSE provider
# ---------------------------------------------------------------------------

def _nse_row(an_dt, text="Announcement about results", url="https://nsearchives.nseindia.com/x.pdf"):
    return {"an_dt": an_dt, "attchmntText": text, "attchmntFile": url}


class _NseResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _FakeNseSession:
    """Fake curl_cffi session: homepage bootstrap + scripted API payload."""

    def __init__(self, payload, fail_api=False):
        self._payload = payload
        self.fail_api = fail_api
        self.closed = False
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if url == "https://www.nseindia.com":
            return _NseResp({"__html__": True}) if self.fail_api else _NseResp("html")
        if self.fail_api:
            raise RuntimeError("HTTP 403 Forbidden")
        return _NseResp(self._payload)

    def close(self):
        self.closed = True


def _install_fake_nse(monkeypatch, payload, fail_api=False):
    fake = _FakeNseSession(payload, fail_api=fail_api)
    monkeypatch.setattr("news.nse_provider.NseNewsProvider._session",
                        lambda self: fake)
    return fake


def test_nse_parses_articles_ist_to_utc(monkeypatch):
    _install_fake_nse(monkeypatch, [_nse_row("30-Jun-2023 20:28:36"),
                                    _nse_row("29-Jun-2023 18:00:00",
                                             url="https://nsearchives.nseindia.com/y.pdf")])
    provider = NseNewsProvider()
    arts = provider.fetch("RELIANCE.NS", pd.Timestamp("2023-06-01"),
                          pd.Timestamp("2023-07-01"))
    assert len(arts) == 2
    a = arts[0]
    # 20:28 IST == 14:58 UTC
    assert a.ts == pd.Timestamp("2023-06-30 14:58:36")
    assert a.title.startswith("Announcement about results")
    assert a.url.startswith("https://nsearchives.nseindia.com")
    assert a.source == "nseindia.com"


def test_nse_window_filtering(monkeypatch):
    _install_fake_nse(monkeypatch, [
        _nse_row("30-Jun-2023 20:28:36"),
        _nse_row("31-May-2023 20:00:00"),   # before window
        _nse_row("01-Jul-2023 10:00:00"),   # after window
    ])
    provider = NseNewsProvider()
    arts = provider.fetch("RELIANCE.NS", pd.Timestamp("2023-06-01"),
                          pd.Timestamp("2023-07-01"))
    assert len(arts) == 1
    assert arts[0].ts == pd.Timestamp("2023-06-30 14:58:36")


def test_nse_missing_data_returns_empty(monkeypatch):
    _install_fake_nse(monkeypatch, [])
    provider = NseNewsProvider()
    assert provider.fetch("RELIANCE.NS", pd.Timestamp("2023-06-01"),
                          pd.Timestamp("2023-07-01")) == []


def test_nse_unparsable_timestamp_skipped(monkeypatch):
    _install_fake_nse(monkeypatch, [
        _nse_row("not-a-date"),
        _nse_row("29-Jun-2023 18:00:00"),
    ])
    provider = NseNewsProvider()
    arts = provider.fetch("RELIANCE.NS", pd.Timestamp("2023-06-01"),
                          pd.Timestamp("2023-07-01"))
    assert len(arts) == 1


def test_nse_supports_only_known_symbols():
    p = NseNewsProvider()
    assert p.supports("RELIANCE.NS")
    assert p.supports("TCS.NS")
    assert not p.supports("UNKNOWN.NS")
    assert not p.supports("AAPL")


def test_parse_ist():
    assert _parse_ist("30-Jun-2023 20:28:36") == pd.Timestamp("2023-06-30 14:58:36")
    assert _parse_ist("01-Jan-2024 05:30:00") == pd.Timestamp("2024-01-01 00:00:00")
    assert _parse_ist("garbage") is None

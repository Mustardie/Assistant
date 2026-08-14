"""News service tests: PIT filtering, caching/resume, duplicates, rate
limiting, failed requests, missing data -- all with mocked providers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pytest

import config
from news.base import Article
from news.service import (NewsDb, _backoff_seconds, fetch_and_cache,
                          news_features, retry_call)


def _article(ticker="AAPL", ts="2023-01-15 12:00:00", url=None,
             title="Title", source="sec.gov"):
    url = url or f"https://x.test/{abs(hash(ts))}"
    return Article(ticker=ticker, ts=pd.Timestamp(ts), title=title, url=url,
                   source=source, snippet=title, identifier=url)


class _FakeProvider:
    """Provider whose responses are scripted; counts its calls and sleeps."""
    name = "fake"

    def __init__(self, responses=None, fail=None, sleep_recorder=None):
        self.responses = list(responses or [])
        self.calls = 0
        self.fail = fail  # callable(ticker, start, end): raises or returns articles
        self.sleeps = [] if sleep_recorder is None else sleep_recorder

    def supports(self, ticker):
        return True

    def query_for(self, ticker):
        return "fake-query"

    def fetch(self, ticker, start, end):
        self.calls += 1
        if self.fail is not None:
            return self.fail(ticker, start, end)
        n = min(self.calls - 1, len(self.responses) - 1) if self.responses else 0
        return self.responses[n] if self.responses else []


class _SleepRecorder:
    def __init__(self):
        self.total = 0.0
        self.calls = []

    def __call__(self, seconds):
        self.total += seconds
        self.calls.append(seconds)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Every test uses its own throwaway news cache (never the real one)."""
    monkeypatch.setattr("news.service.NEWS_DB", tmp_path / "news_test.sqlite")


@pytest.fixture
def sleep_rec():
    return _SleepRecorder()


def _run(provider, ticker="AAPL", start="2023-01-01", end="2023-02-01",
         force=False, audit=None, sleep=None):
    return fetch_and_cache(ticker, start=start, end=end, force=force,
                           audit=audit, provider=provider, sleep=sleep or _noop_sleep)


def _noop_sleep(seconds):
    return None


# ---------------------------------------------------------------------------
# timestamp filtering (point-in-time)
# ---------------------------------------------------------------------------

def test_articles_outside_window_are_dropped():
    provider = _FakeProvider(responses=[[
        _article(ts="2022-12-31 23:59:59"),   # before window start
        _article(ts="2023-01-15 10:00:00"),   # inside
        _article(ts="2023-02-01 00:00:00"),   # at window end -> excluded
    ]])
    df, stats = _run(provider, start="2023-01-01", end="2023-02-01")
    assert len(df) == 1
    assert df["ts"].iloc[0] == pd.Timestamp("2023-01-15 10:00:00")
    assert stats["new"] == 1


def test_features_never_use_future_articles(tmp_path):
    db = NewsDb(path=tmp_path / "news.sqlite")
    db.insert_articles("AAPL", [
        _article(ts="2023-01-05"), _article(ts="2023-01-20"),
        _article(ts="2023-02-10"),
    ], provider="fake")
    db.close()
    feats = news_features("AAPL", db=NewsDb(path=tmp_path / "news.sqlite"))
    row = feats.loc[pd.Timestamp("2023-01-20")]
    assert row["f_news_7d"] == 1.0      # only the 20th is within 7d of the 20th
    assert row["f_news_30d"] == 2.0     # 05 and 20
    row2 = feats.loc[pd.Timestamp("2023-02-10")]
    assert row2["f_news_7d"] == 1.0
    assert row2["f_news_30d"] == 2.0    # 01-20 and 02-10 (01-05 is >30d back)
    assert row2["f_news_90d"] == 3.0
    assert feats.index.max() == pd.Timestamp("2023-02-10")
    assert not (feats.index < pd.Timestamp("2023-01-05")).any()


# ---------------------------------------------------------------------------
# caching / resume / duplicates
# ---------------------------------------------------------------------------

def test_resume_skips_already_fetched_windows(sleep_rec):
    provider = _FakeProvider(responses=[[_article()]])
    _run(provider, start="2023-01-01", end="2023-02-01")
    calls_first = provider.calls
    assert calls_first >= 1
    provider2 = _FakeProvider(responses=[[_article(url="https://x.test/b")]])
    df, stats = _run(provider2, start="2023-01-01", end="2023-02-01")
    assert provider2.calls == 0          # window already in the log -> no HTTP
    assert stats["skipped"] == 1
    assert len(df) >= 1


def test_force_refetches():
    provider = _FakeProvider(responses=[[_article()]])
    _run(provider, start="2023-01-01", end="2023-02-01")
    provider2 = _FakeProvider(responses=[[_article(url="https://x.test/b")]])
    _run(provider2, start="2023-01-01", end="2023-02-01", force=True)
    assert provider2.calls == 1


def test_duplicate_urls_inserted_once(tmp_path):
    db = NewsDb(path=tmp_path / "news.sqlite")
    db.insert_articles("AAPL", [_article(), _article()], provider="fake")
    db.insert_articles("AAPL", [_article()], provider="fake")
    assert len(db.fetch("AAPL")) == 1


# ---------------------------------------------------------------------------
# rate limiting
# ---------------------------------------------------------------------------

def test_min_delay_enforced_between_requests():
    rec = _SleepRecorder()
    provider = _FakeProvider(
        responses=[[_article()], [_article(ts="2023-02-05")], [_article(ts="2023-03-05")]],
        sleep_recorder=rec)
    _run(provider, start="2023-01-01", end="2023-04-01", sleep=rec)
    # every request is followed by at least the provider's min delay
    assert provider.calls == 3
    assert all(d >= 2.0 for d in rec.calls)


# ---------------------------------------------------------------------------
# failed requests / graceful failure
# ---------------------------------------------------------------------------

def test_failed_request_retries_then_marks_window(monkeypatch):
    class Boom(Exception):
        pass

    def always_fail(ticker, start, end):
        raise Boom("network down")

    rec = _SleepRecorder()
    provider = _FakeProvider(fail=always_fail, sleep_recorder=rec)
    monkeypatch.setattr(config, "NEWS_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "NEWS_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(config, "NEWS_BACKOFF_CAP", 0.05)
    df, stats = _run(provider, start="2023-01-01", end="2023-02-01", sleep=rec)
    assert provider.calls == 3                       # exactly max_retries
    assert len(stats["failed_windows"]) == 1         # honestly reported
    assert df.empty
    assert rec.total > 0                             # backoff sleeps happened


def test_consecutive_failures_abort_early(monkeypatch):
    class Boom(Exception):
        pass

    def always_fail(ticker, start, end):
        raise Boom("rate limited")

    provider = _FakeProvider(fail=always_fail)
    monkeypatch.setattr(config, "NEWS_MAX_RETRIES", 2)
    monkeypatch.setattr(config, "NEWS_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(config, "NEWS_BACKOFF_CAP", 0.02)
    monkeypatch.setattr(config, "NEWS_MAX_CONSECUTIVE_FAILURES", 2)
    _, stats = _run(provider, start="2023-01-01", end="2023-04-01")  # 3 windows
    assert stats["aborted"] is True
    assert len(stats["failed_windows"]) == 2          # stopped after 2


def test_success_after_retry(monkeypatch):
    class Flaky(Exception):
        pass

    def fail_once(ticker, start, end):
        if fail_once.n == 0:
            fail_once.n += 1
            raise Flaky("transient")
        return [_article()]

    fail_once.n = 0
    rec = _SleepRecorder()
    provider = _FakeProvider(fail=fail_once, sleep_recorder=rec)
    monkeypatch.setattr(config, "NEWS_MAX_RETRIES", 3)
    monkeypatch.setattr(config, "NEWS_BACKOFF_BASE", 0.01)
    monkeypatch.setattr(config, "NEWS_BACKOFF_CAP", 0.02)
    df, stats = _run(provider, start="2023-01-01", end="2023-02-01")
    assert len(df) == 1
    assert stats["failed_windows"] == []


# ---------------------------------------------------------------------------
# missing data
# ---------------------------------------------------------------------------

def test_empty_results_are_ok_not_failed():
    provider = _FakeProvider(responses=[[]])
    df, stats = _run(provider, start="2023-01-01", end="2023-02-01")
    assert df.empty
    assert stats["failed_windows"] == []
    assert stats["new"] == 0


def test_unsupported_ticker_is_reported():
    provider = _FakeProvider()
    provider.supports = lambda ticker: False  # noqa: E731
    df, stats = fetch_and_cache("NOPE", start="2023-01-01", end="2023-02-01",
                                provider=provider)
    assert stats["unsupported"] is True
    assert df.empty


# ---------------------------------------------------------------------------
# backoff unit
# ---------------------------------------------------------------------------

def test_backoff_exponential_and_capped():
    waits = [_backoff_seconds(i, 10.0, 30.0, retry_after=None) for i in range(4)]
    assert waits[1] > waits[0]
    assert all(w <= 30.0 for w in waits)          # capped, never grows unbounded


def test_retry_after_header_wins_and_capped():
    assert _backoff_seconds(0, 10.0, 30.0, retry_after=5.0) == 5.0
    assert _backoff_seconds(0, 10.0, 30.0, retry_after=999.0) <= 30.0


def test_retry_call_returns_none_on_exhaustion():
    def boom(timeout=None):
        raise OSError("down")

    out = retry_call(boom, max_retries=2, backoff_base=0.01, backoff_cap=0.02,
                     timeout=1, min_delay=0.0)
    assert out is None


def test_retry_call_success():
    def ok(timeout=None):
        return "data"

    assert retry_call(ok, max_retries=2, backoff_base=0.01, backoff_cap=0.02,
                      timeout=1, min_delay=0.0) == "data"

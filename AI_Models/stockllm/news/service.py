"""News service: windowed ingestion, caching, retries and PIT features.

Sits on top of the provider abstraction (news.base).  Responsibilities:

  * windowed monthly fetches with exponential backoff + jitter and a
    configurable minimum delay between HTTP requests
  * a per-window fetch log so already-fetched windows are never re-requested
    (resume); successful responses are persisted immediately, window by window
  * graceful failure: a window that exhausts retries is marked `failed` and
    reported in the returned stats (never silently treated as success); after
    NEWS_MAX_CONSECUTIVE_FAILURES consecutive failures the run aborts early
  * point-in-time contract: `news_features` counts articles only with
    publication ts <= t, and the storage layer records publication ts,
    retrieval ts (fetched_at), ticker, source/domain, URL/identifier, title
    and snippet for every article

Providers are selected per ticker (US -> EDGAR filings, IN -> NSE corporate
announcements).  GDELT is not used anywhere in this pipeline.
"""
from __future__ import annotations

import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import config
from news.base import Article, NewsProvider
from news.edgar_provider import EdgarNewsProvider, cik_for_ticker
from news.nse_provider import NseNewsProvider
from utils.logging import get_logger

log = get_logger(__name__)

NEWS_DB = config.DATA_DIR / "news.sqlite"


# ---------------------------------------------------------------------------
# provider selection
# ---------------------------------------------------------------------------

def provider_for_ticker(ticker: str) -> NewsProvider | None:
    """US tickers -> EDGAR filings, Indian (.NS) tickers -> NSE announcements."""
    if ticker.endswith(".NS"):
        nse = NseNewsProvider()
        return nse if nse.supports(ticker) else None
    cik = cik_for_ticker(ticker)
    if cik is None:
        return None
    return EdgarNewsProvider(cik=cik)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

class NewsDb:
    """SQLite cache of articles + per-window fetch log (resume support)."""

    def __init__(self, path: Path | None = None):
        self.path = str(path or NEWS_DB)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                ticker TEXT NOT NULL,
                ts TEXT NOT NULL,
                domain TEXT,
                title TEXT,
                url TEXT,
                language TEXT,
                sourcecountry TEXT,
                fetched_at TEXT,
                snippet TEXT,
                provider TEXT,
                PRIMARY KEY (ticker, url)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS window_log (
                ticker TEXT NOT NULL,
                win_start TEXT NOT NULL,
                win_end TEXT NOT NULL,
                status TEXT NOT NULL,
                n_articles INTEGER DEFAULT 0,
                error TEXT,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (ticker, win_start, win_end)
            )
            """
        )
        self._migrate()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_ts ON articles(ticker, ts)")

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(articles)").fetchall()}
        for name, ddl in (("fetched_at", "TEXT"), ("snippet", "TEXT"),
                          ("provider", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {ddl}")
        self.conn.commit()

    def window_fetched_ok(self, ticker: str, win_start: str, win_end: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM window_log WHERE ticker=? AND win_start=? AND win_end=? "
            "AND status='ok'", (ticker, win_start, win_end)).fetchone()
        return row is not None

    def record_window(self, ticker: str, win_start: str, win_end: str,
                      status: str, n_articles: int = 0, error: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO window_log "
            "(ticker, win_start, win_end, status, n_articles, error, fetched_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (ticker, win_start, win_end, status, int(n_articles), error,
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def insert_articles(self, ticker: str, articles: list[Article],
                        provider: str) -> int:
        """Upsert by (ticker, url) -- duplicates are ignored, not re-inserted."""
        n = 0
        now = datetime.utcnow().isoformat(timespec="seconds")
        for a in articles:
            a.fetched_at = now
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO articles "
                "(ticker, ts, domain, title, url, language, sourcecountry, "
                "fetched_at, snippet, provider) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (a.ticker, a.ts.strftime("%Y-%m-%d %H:%M:%S"),
                 a.source, a.title, a.url, "", "", now, a.snippet, provider))
            n += cur.rowcount
        self.conn.commit()
        return n

    def fetch(self, ticker: str) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT ts, domain, title, url, language, sourcecountry, fetched_at, "
            "snippet, provider FROM articles WHERE ticker = ? ORDER BY ts",
            self.conn, params=(ticker,))
        if not df.empty:
            df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
            df = df.dropna(subset=["ts"])
        return df

    def close(self) -> None:
        self.conn.close()


# ---------------------------------------------------------------------------
# retry / backoff
# ---------------------------------------------------------------------------

def _backoff_seconds(attempt: int, base: float, cap: float,
                     retry_after: float | None = None) -> float:
    if retry_after is not None and retry_after > 0:
        return min(float(retry_after), cap)
    return min(base * (2 ** attempt) + random.uniform(0, 0.5 * base), cap)


def retry_call(fn, *, max_retries: int, backoff_base: float, backoff_cap: float,
               timeout: int, min_delay: float,
               sleep: "callable" = time.sleep) -> object | None:
    """Call `fn` with retries on any exception; None when exhausted.

    `min_delay` is enforced after every attempt (rate limiting).  Never
    raises: callers record failures and keep going.
    """
    for attempt in range(max_retries):
        try:
            result = fn(timeout=timeout)
            if min_delay > 0:
                sleep(min_delay)
            return result
        except Exception as exc:  # noqa: BLE001 - transport errors are retried
            retry_after = None
            if hasattr(exc, "code") and exc.code == 429:  # type: ignore[attr-defined]
                retry_after = getattr(getattr(exc, "headers", None), "get", lambda _: None)("Retry-After")
            wait = _backoff_seconds(attempt, backoff_base, backoff_cap,
                                    float(retry_after) if retry_after else None)
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            if attempt < max_retries - 1:
                log.warning("news request attempt %d/%d failed (%s); retrying in %.1fs",
                            attempt + 1, max_retries, last_error, wait)
                sleep(wait)
            else:
                log.error("news request gave up after %d attempts (%s)",
                          max_retries, last_error)
    return None


# ---------------------------------------------------------------------------
# windowed download with resume
# ---------------------------------------------------------------------------

def _windows(start: pd.Timestamp, end: pd.Timestamp, chunk_days: int = 31) -> list[tuple]:
    out = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + pd.Timedelta(days=chunk_days), end)
        out.append((chunk_start.strftime("%Y%m%d%H%M%S"),
                    chunk_end.strftime("%Y%m%d%H%M%S")))
        chunk_start = chunk_end
    return out


def fetch_and_cache(ticker: str, start: str = config.NEWS_FETCH_START,
                    end: str | None = None, force: bool = False,
                    audit=None, provider: NewsProvider | None = None,
                    sleep: "callable" = time.sleep) -> tuple[pd.DataFrame, dict]:
    """Fetch news for a ticker in monthly windows; resume from the cache.

    Returns (articles_df, stats) where stats carries `new`, `skipped`,
    `failed_windows`, `aborted` and `unsupported`, so callers can report
    failures loudly (never silently treated as success).
    """
    end = end or datetime.now().strftime("%Y-%m-%d")
    if provider is None:
        provider = provider_for_ticker(ticker)
    stats = {"new": 0, "skipped": 0, "failed_windows": [], "aborted": False,
             "unsupported": False}
    if provider is None or not provider.supports(ticker):
        stats["unsupported"] = True
        log.warning("%s: no news provider available for this ticker -- skipping "
                    "(no fabricated news is ever added)", ticker)
        return pd.DataFrame(), stats

    cfg = config.NEWS_PROVIDER_SETTINGS.get(provider.name, {})
    min_delay = cfg.get("min_delay", config.NEWS_MIN_DELAY)
    max_retries = cfg.get("max_retries", config.NEWS_MAX_RETRIES)
    max_consecutive_failures = config.NEWS_MAX_CONSECUTIVE_FAILURES

    db = NewsDb()
    try:
        windows = _windows(pd.Timestamp(start), pd.Timestamp(end))
        consecutive_failures = 0
        for win_start, win_end in windows:
            if not force and db.window_fetched_ok(ticker, win_start, win_end):
                stats["skipped"] += 1
                continue
            win_start_ts = pd.Timestamp(win_start)
            win_end_ts = pd.Timestamp(win_end)
            payload = retry_call(
                lambda timeout=config.NEWS_REQUEST_TIMEOUT, s=win_start_ts, e=win_end_ts:
                    provider.fetch(ticker, s, e),
                max_retries=max_retries,
                backoff_base=cfg.get("backoff_base", config.NEWS_BACKOFF_BASE),
                backoff_cap=config.NEWS_BACKOFF_CAP,
                timeout=config.NEWS_REQUEST_TIMEOUT,
                min_delay=min_delay,
                sleep=sleep)
            if payload is None:
                db.record_window(ticker, win_start, win_end, "failed", error="max retries")
                stats["failed_windows"].append((win_start, win_end))
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    stats["aborted"] = True
                    log.error(
                        "news %s: %d consecutive window failures -- aborting "
                        "remaining windows (resume later with the same command)",
                        ticker, consecutive_failures)
                    break
                continue
            consecutive_failures = 0
            in_window = [a for a in payload if win_start_ts <= a.ts < win_end_ts]
            stats["new"] += db.insert_articles(ticker, in_window, provider.name)
            db.record_window(ticker, win_start, win_end, "ok", n_articles=len(in_window))
        all_rows = db.fetch(ticker)
        log.info("%s: news done -- %d cached, %d new, %d windows skipped, "
                 "%d failed%s", ticker, len(all_rows), stats["new"], stats["skipped"],
                 len(stats["failed_windows"]),
                 " (ABORTED early)" if stats["aborted"] else "")
        if audit is not None and not all_rows.empty:
            now = datetime.utcnow().isoformat(timespec="seconds")
            audit_rows = [
                (ticker, "news", ts, f"{domain}",
                 {"title": title, "url": url, "fetched_at": fetched_at,
                  "provider": provider.name}, now)
                for ts, domain, title, url, fetched_at in
                all_rows[["ts", "domain", "title", "url", "fetched_at"]].itertuples(index=False)
            ]
            audit.record_many(audit_rows)
        return all_rows, stats
    finally:
        db.close()


# ---------------------------------------------------------------------------
# point-in-time features
# ---------------------------------------------------------------------------

def news_features(ticker: str, db: NewsDb | None = None) -> pd.DataFrame:
    """Counts/freshness on the ticker's own article timeline (ts = publication).

    The feature builder as-of joins these to the trading calendar, so a row at
    day t only ever sees articles with ts <= t -- an article is never used for
    a prediction before its publication timestamp (unit-tested).

    Columns: f_news_7d, f_news_30d, f_news_90d, f_days_since_news,
             f_news_avail (1.0 once any article exists).
    """
    own = db is None
    db = db or NewsDb()
    try:
        df = db.fetch(ticker)
    finally:
        if own:
            db.close()
    if df.empty:
        return pd.DataFrame(columns=["f_news_avail"])
    df = df.copy()
    arr = df["ts"].sort_values().to_numpy(dtype="datetime64[ns]")
    uniq = np.unique(arr)
    idx = pd.DatetimeIndex(uniq)
    counts = pd.DataFrame(index=idx)
    for w in (7, 30, 90):
        right = np.searchsorted(arr, uniq, side="right")
        left = np.searchsorted(arr, uniq - np.timedelta64(w, "D"), side="left")
        counts[f"f_news_{w}d"] = (right - left).astype(float)
    counts["f_last_news_ts"] = idx
    counts["f_days_since_news"] = 0.0
    counts["f_news_avail"] = 1.0
    return counts


def count_articles(ticker: str) -> int:
    db = NewsDb()
    try:
        return len(db.fetch(ticker))
    finally:
        db.close()

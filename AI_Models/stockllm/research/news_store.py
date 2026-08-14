"""Research store: sourced web information from JARVIS.

JARVIS's web-research tools hand StockLLM (source, timestamp, extracted text,
relevance), which we persist in SQLite so reports can cite where a fact came
from and when.  Nothing here trusts the text itself: relevance and a verified
flag are recorded, and the prompts explicitly instruct the LLM to treat web
sources as unverified.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from config import NEWS_DB_PATH, NEWS_MAX_AGE_DAYS


class NewsStore:
    def __init__(self, path=NEWS_DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                url TEXT,
                title TEXT,
                content TEXT,
                source TEXT,
                relevance REAL DEFAULT 0.0,
                verified INTEGER DEFAULT 0,
                fetched_at TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ticker_time ON sources(ticker, fetched_at)"
        )
        self.conn.commit()

    def add(self, ticker: str, content: str, source: str = "", url: str = "",
            title: str = "", relevance: float = 0.0, verified: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO sources (ticker, url, title, content, source, relevance, "
            "verified, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            (ticker.upper(), url, title, content, source, float(relevance),
             int(bool(verified)), datetime.utcnow().isoformat(timespec="seconds")),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent(self, ticker: str | None = None, days: int = NEWS_MAX_AGE_DAYS,
               min_relevance: float = 0.0, limit: int = 20) -> list[dict]:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        query = (
            "SELECT ticker, title, content, source, url, relevance, verified, fetched_at "
            "FROM sources WHERE fetched_at >= ? AND relevance >= ?"
        )
        args: list = [since, float(min_relevance)]
        if ticker:
            query += " AND ticker = ?"
            args.append(ticker.upper())
        query += " ORDER BY fetched_at DESC LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(query, args).fetchall()
        cols = ["ticker", "title", "content", "source", "url", "relevance",
                "verified", "fetched_at"]
        return [dict(zip(cols, r)) for r in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])

    def close(self) -> None:
        self.conn.close()

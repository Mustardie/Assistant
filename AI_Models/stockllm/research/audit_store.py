"""Audit store: records every external information item used by the system.

Requirement (V3): no future information may reach the model.  Part of that
guarantee is *provable provenance*: for every item of external information
(fundamental statement, earnings event, news article, price/benchmark/sector
series) we record here when it was available, where it came from, and what it
was, keyed by a content hash so the same item is never double-counted.

Kinds of items recorded:
    fundamental  : one quarterly statement, available at its filing/report date
    earnings     : one earnings event (report date + surprise)
    news         : one news article, available at its publication timestamp
    series       : one OHLCV series download (price/benchmark/sector), with the
                   date range it covers (its "items" are the rows themselves)

The feature builder consumes the point-in-time tables (see fundamentals/,
news/, marketdata/sector.py); this store exists so coverage and provenance can
be audited independently (`python main.py audit`).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from config import DATA_DIR

AUDIT_DB_PATH = DATA_DIR / "audit.sqlite"


def content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditStore:
    def __init__(self, path: Path = AUDIT_DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                ticker TEXT NOT NULL,
                kind TEXT NOT NULL,
                item_ts TEXT NOT NULL,
                source TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                raw_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (ticker, kind, raw_hash)
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_ts ON items(kind, item_ts)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_ticker ON items(ticker, item_ts)")
        self.conn.commit()

    def record_many(self, rows: list[tuple]) -> int:
        """rows: (ticker, kind, item_ts, source, detail_dict, fetched_at)."""
        if not rows:
            return 0
        inserted = 0
        for ticker, kind, item_ts, source, detail, fetched_at in rows:
            raw_hash = content_hash({"kind": kind, "ts": item_ts, "source": source,
                                     "detail": detail})
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO items "
                "(ticker, kind, item_ts, source, detail, raw_hash, fetched_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (ticker, kind, str(item_ts), source,
                 json.dumps(detail, ensure_ascii=False, default=str),
                 raw_hash, fetched_at),
            )
            inserted += cur.rowcount
        self.conn.commit()
        return inserted

    def count_by_kind(self, kind: str | None = None) -> dict[str, int]:
        q = ("SELECT kind, COUNT(*) FROM items"
             + (" WHERE kind = ?" if kind else "")
             + " GROUP BY kind")
        args = (kind,) if kind else ()
        return dict(self.conn.execute(q, args).fetchall())

    def first_seen(self, kind: str) -> "pd.Series":
        """Earliest recorded item timestamp per ticker for one kind."""
        import pandas as pd
        df = pd.read_sql_query(
            "SELECT ticker, MIN(item_ts) AS first_ts FROM items "
            "WHERE kind = ? GROUP BY ticker", self.conn, params=(kind,))
        if df.empty:
            return pd.Series(dtype="object")
        return df.set_index("ticker")["first_ts"].sort_index()

    def coverage(self, kind: str, start: str | None = None,
                 end: str | None = None, tickers: list[str] | None = None) -> "pd.Series":
        """Rows per ticker in [start, end) for one kind."""
        import pandas as pd
        conds, args = [], []
        conds.append("kind = ?")
        args.append(kind)
        if start:
            conds.append("item_ts >= ?")
            args.append(start)
        if end:
            conds.append("item_ts < ?")
            args.append(end)
        if tickers:
            conds.append(f"ticker IN ({','.join('?' * len(tickers))})")
            args.extend(tickers)
        q = f"SELECT ticker, COUNT(*) AS n FROM items WHERE {' AND '.join(conds)} GROUP BY ticker"
        df = pd.read_sql_query(q, self.conn, params=args)
        if df.empty:
            return df
        return df.set_index("ticker")["n"].sort_index()

    def close(self) -> None:
        self.conn.close()

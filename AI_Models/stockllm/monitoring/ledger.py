"""Prediction ledger for tracked tickers (SQLite).

The ledger is the audit trail of the monitoring flow: every prediction
emitted for a tracked ticker is recorded once (deduplicated by
ticker + as_of_date + horizon), and later resolved against the actual
outcome once the horizon window has passed.  Outcomes are computed from
the daily close series via ``resolve_due``.

Schema:

    predictions(
        id INTEGER PRIMARY KEY,
        ticker TEXT NOT NULL,
        as_of_date TEXT NOT NULL,        -- trading day the forecast was made
        horizon_days INTEGER NOT NULL,
        price REAL NOT NULL,             -- close at as_of_date
        prob_up REAL, expected_return REAL,
        direction TEXT,                  -- UP / DOWN
        created_ts TEXT NOT NULL,
        resolved_ts TEXT,                -- when the outcome was attached
        outcome_ret REAL,                -- actual forward return
        outcome_direction_correct INTEGER -- 1 / 0 / NULL
    )

Only stdlib sqlite3 -- no extra dependencies, safe for concurrent reads.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from utils.logging import get_logger

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    price REAL NOT NULL,
    prob_up REAL,
    expected_return REAL,
    direction TEXT,
    created_ts TEXT NOT NULL,
    resolved_ts TEXT,
    outcome_ret REAL,
    outcome_direction_correct INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pred_ticker_date ON predictions(ticker, as_of_date);
CREATE INDEX IF NOT EXISTS idx_pred_horizon ON predictions(horizon_days);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pred_unique
    ON predictions(ticker, as_of_date, horizon_days);
"""


class PredictionLedger:
    """SQLite-backed store of predictions made by the monitoring flow."""

    def __init__(self, db_path=None):
        from config import TRACKING_DB
        self.path = Path(db_path or TRACKING_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- writes -------------------------------------------------------------

    def add(self, ticker: str, as_of_date: str, horizon_days: int, price: float,
            prob_up: float | None = None, expected_return: float | None = None,
            direction: str | None = None, created_ts: str | None = None) -> bool:
        """Insert one prediction; returns False when a duplicate exists
        (same ticker + as_of_date + horizon) and the row is kept."""
        import datetime
        if self.has(ticker, as_of_date, horizon_days):
            return False
        ts = created_ts or datetime.datetime.now().isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT INTO predictions (ticker, as_of_date, horizon_days, price, "
            "prob_up, expected_return, direction, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker.upper(), str(as_of_date), int(horizon_days), float(price),
             float(prob_up) if prob_up is not None else None,
             float(expected_return) if expected_return is not None else None,
             direction, ts),
        )
        self._conn.commit()
        return True

    def has(self, ticker: str, as_of_date: str, horizon_days: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM predictions WHERE ticker = ? AND as_of_date = ? "
            "AND horizon_days = ?",
            (ticker.upper(), str(as_of_date), int(horizon_days)))
        return cur.fetchone() is not None

    # -- reads --------------------------------------------------------------

    def all(self, ticker: str | None = None, unresolved_only: bool = False,
            limit: int | None = None) -> list[dict]:
        q = "SELECT * FROM predictions"
        conds, args = [], []
        if ticker:
            conds.append("ticker = ?")
            args.append(ticker.upper())
        if unresolved_only:
            conds.append("resolved_ts IS NULL")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY as_of_date DESC, ticker"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def unresolved(self, limit: int | None = None) -> list[dict]:
        return self.all(unresolved_only=True, limit=limit)

    def tickers(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT ticker FROM predictions ORDER BY ticker").fetchall()
        return [r["ticker"] for r in rows]

    def stats(self) -> dict:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN resolved_ts IS NULL THEN 1 ELSE 0 END) AS open, "
            "SUM(outcome_direction_correct) AS correct, "
            "AVG(outcome_ret) AS mean_outcome "
            "FROM predictions")
        row = cur.fetchone()
        total = int(row["total"] or 0)
        open_ = int(row["open"] or 0)
        resolved = total - open_
        return {
            "total": total,
            "open": open_,
            "resolved": resolved,
            "correct": int(row["correct"] or 0) if row["correct"] is not None else 0,
            "direction_acc": (int(row["correct"] or 0) / resolved
                              if resolved else None),
            "mean_outcome_ret": float(row["mean_outcome"]) if row["mean_outcome"] is not None else None,
        }

    # -- resolution ---------------------------------------------------------

    def resolve_due(self, prices: dict[str, pd.Series],
                    now=None) -> int:
        """Resolve predictions whose horizon window has passed.

        ``prices`` maps ticker -> daily close Series (index = date).
        A prediction made on as_of_date with horizon H resolves when H
        trading sessions after as_of_date exist in the price series.
        Returns the number of predictions resolved.
        """
        import datetime
        today = (now or datetime.date.today())
        resolved = 0
        for rec in self.unresolved():
            ticker = rec["ticker"]
            closes = prices.get(ticker)
            if closes is None or len(closes) < 2:
                continue
            closes = closes.dropna().sort_index()
            if closes.index[0].date() > today:
                continue
            locs = list(closes.index)
            try:
                pos = locs.index(pd.Timestamp(rec["as_of_date"]))
            except ValueError:
                pos = None
            if pos is None or pos + rec["horizon_days"] >= len(closes):
                continue
            price0 = float(closes.iloc[pos])
            price1 = float(closes.iloc[pos + rec["horizon_days"]])
            if price0 <= 0 or not all(map(pd.notna, (price0, price1))):
                continue
            outcome = price1 / price0 - 1.0
            direction = rec.get("direction") or ("UP" if (rec.get("prob_up") or 0.5) >= 0.5 else "DOWN")
            correct = int((outcome > 0) == (direction == "UP"))
            self._conn.execute(
                "UPDATE predictions SET resolved_ts = ?, outcome_ret = ?, "
                "outcome_direction_correct = ? WHERE id = ?",
                (datetime.datetime.now().isoformat(timespec="seconds"),
                 outcome, correct, rec["id"]))
            resolved += 1
        if resolved:
            self._conn.commit()
            log.info("ledger: resolved %d due predictions", resolved)
        return resolved

    def as_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.all())

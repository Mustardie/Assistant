"""Market-data acquisition and caching.

Primary source: Yahoo Finance via the `yfinance` library.  Frames are fetched
with auto_adjust=True, meaning dividends/splits are already reflected in the
prices (this avoids corporate-action jumps inside the feature stream) and are
cached as CSV in data/raw/.

You may also drop your own CSV files into data/raw/ with the standard schema:

    date,open,high,low,close,volume[,adj close]

in which case the loader uses them without touching the network.

Documented limitations of the primary source:
  * yfinance only exposes currently-listed tickers; delisted companies cannot
    be fetched, so survivorship bias is inherent to the raw data.
  * Intra-day timestamps are collapsed to calendar dates.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from config import LOOKBACK_DAYS, RAW_DATA_DIR, REQUIRED_COLUMNS
from utils.logging import get_logger

log = get_logger(__name__)

_STD_COLS = ["open", "high", "low", "close", "adj_close", "volume"]


class DataUnavailableError(RuntimeError):
    pass


def _normalize_frame(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Normalize any raw frame to the standard schema (sorted, de-duplicated, numeric)."""
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={"adj close": "adj_close", "adjusted close": "adj_close"})
    for col in _STD_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise DataUnavailableError(f"{ticker}: missing required columns {missing}")
    if "adj_close" not in df.columns:
        df["adj_close"] = df["close"]
    df = df.dropna(subset=["close", "volume"])
    return df


def cache_path(ticker: str, base_dir: Path = RAW_DATA_DIR) -> Path:
    safe = ticker.replace("^", "INDEX_").replace("=", "_")
    return base_dir / f"{safe}.csv"


def load_local_csv(ticker: str, base_dir: Path = RAW_DATA_DIR) -> pd.DataFrame | None:
    """Load a user-provided CSV from data/raw/ (or base_dir) if present."""
    path = cache_path(ticker, base_dir)
    if not path.exists():
        return None
    df = pd.read_csv(path)
    if "date" not in df.columns:
        df = df.reset_index().rename(columns={"index": "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.set_index("date")
    return _normalize_frame(df, ticker)


def download_and_cache(ticker: str, start: str, end: str | None = None,
                       force: bool = False, base_dir: Path = RAW_DATA_DIR) -> pd.DataFrame:
    """Download OHLCV from Yahoo Finance and cache it under data/raw/."""
    path = cache_path(ticker, base_dir)
    if path.exists() and not force:
        cached = load_local_csv(ticker, base_dir)
        if cached is not None and len(cached) > 0:
            return cached
    try:
        import yfinance as yf
    except ImportError as exc:
        raise DataUnavailableError(
            f"{ticker}: yfinance is not installed (`pip install yfinance`). "
            f"Alternatively place a CSV named {path.name} in {path.parent}."
        ) from exc

    start_dt = pd.to_datetime(start) - pd.Timedelta(days=LOOKBACK_DAYS)
    end_dt = end or dt.date.today().isoformat()
    raw = yf.Ticker(ticker).history(
        start=start_dt.strftime("%Y-%m-%d"), end=end_dt, auto_adjust=True
    )
    if raw is None or raw.empty:
        raise DataUnavailableError(
            f"{ticker}: no data returned (symbol invalid or delisted?)"
        )
    df = _normalize_frame(raw, ticker)
    df.to_csv(path, index_label="date")
    log.info("cached %s -> %s (%d rows)", ticker, path.name, len(df))
    return df


def load_ticker(ticker: str, start: str | None = None, end: str | None = None,
                base_dir: Path = RAW_DATA_DIR) -> pd.DataFrame:
    """Load a ticker from cache/local CSV; raise with guidance if unavailable."""
    df = load_local_csv(ticker, base_dir)
    if df is None:
        raise DataUnavailableError(
            f"{ticker}: no cached data. Run `python main.py download` first, "
            f"or place a CSV at data/raw/{cache_path(ticker, base_dir).name}."
        )
    if start:
        df = df.loc[df.index >= pd.to_datetime(start)]
    if end:
        df = df.loc[df.index <= pd.to_datetime(end)]
    return df


def download_all(tickers, start: str, end: str | None = None,
                 force: bool = False) -> dict[str, pd.DataFrame]:
    """Download many tickers, skipping failures with a warning."""
    out = {}
    for t in tickers:
        try:
            out[t] = download_and_cache(t, start, end, force)
        except DataUnavailableError as exc:
            log.warning("skipping %s: %s", t, exc)
    return out


def load_market_data(tickers, start: str | None = None, end: str | None = None,
                     benchmarks=()) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Load tickers and benchmarks from cache; unavailable ones are skipped."""
    frames, bench = {}, {}
    for t in tickers:
        try:
            frames[t] = load_ticker(t, start, end)
        except DataUnavailableError as exc:
            log.warning("%s", exc)
    for b in benchmarks:
        try:
            bench[b] = load_ticker(b, start, end)
        except DataUnavailableError as exc:
            log.warning("benchmark unavailable: %s", exc)
    return frames, bench

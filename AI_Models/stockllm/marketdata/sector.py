"""Sector context: each ticker's sector index, as point-in-time features.

Sector membership is a *snapshot* (a ticker's sector today applied to all of
history -- a documented limitation; index membership drift is slow).

Mappings (used only for these default-universe tickers; anything else gets no
sector context and coverage reports it as unavailable):
    US : XLK (tech), XLY (consumer discretionary)
    IN : ^NSEBANK (banking), ^CNXIT (IT), ^CNXFMCG (FMCG), ^CNXENERGY (energy)
"""
from __future__ import annotations

import pandas as pd

from config import SECTOR_MAP, RAW_DATA_DIR
from marketdata import loader
from utils.logging import get_logger

log = get_logger(__name__)

SECTOR_NAMES = {  # human-readable names for reporting
    "XLK": "US Tech", "XLY": "US Cons. Discr.",
    "^NSEBANK": "Nifty Bank", "^CNXIT": "Nifty IT",
    "^CNXFMCG": "Nifty FMCG", "^CNXENERGY": "Nifty Energy",
}


def sector_for_ticker(ticker: str) -> str | None:
    return SECTOR_MAP.get(ticker)


def ensure_sector_data(sectors: tuple[str, ...] = (), force: bool = False) -> dict[str, pd.DataFrame]:
    """Download/cache any missing sector index series into data/raw/."""
    wanted = tuple(dict.fromkeys(sectors or list(SECTOR_MAP.values())))
    out = {}
    for sym in wanted:
        path = loader.cache_path(sym)
        if path.exists() and not force:
            df = loader.load_local_csv(sym)
            if df is not None and len(df):
                out[sym] = df
                continue
        try:
            out[sym] = loader.download_and_cache(sym, "2016-01-01", force=force)
        except loader.DataUnavailableError as exc:
            log.warning("sector index unavailable %s: %s", sym, exc)
    return out


def load_sectors(sectors: tuple[str, ...] = ()) -> dict[str, pd.DataFrame]:
    return {sym: df for sym, df in ensure_sector_data(sectors).items()}


def sector_features(ticker: str, sector: pd.DataFrame | None) -> pd.DataFrame:
    """Sector-index context for a ticker, keyed by date (as-of joined by the
    feature builder).  Columns:
        s_ret_1, s_ret_5, s_ret_20, s_vol_20, s_dist_ma50,
        s_rs_5, s_rs_20      (ticker's own momentum vs sector momentum)
        s_rs_market_20       (sector momentum vs the home index)
        s_avail              (1.0 on/after the sector series exists)
    """
    if sector is None or sector.empty:
        return pd.DataFrame(columns=["s_avail"])
    b = sector["close"]
    b_ret1 = b.pct_change()
    sf = pd.DataFrame({
        "s_ret_1": b_ret1,
        "s_ret_5": b.pct_change(5),
        "s_ret_20": b.pct_change(20),
        "s_vol_20": b_ret1.rolling(20, min_periods=20).std(),
        "s_dist_ma50": b / b.rolling(50, min_periods=50).mean() - 1.0,
    })
    sf["s_avail"] = 1.0
    return sf


def sector_relative_features(ticker: str, stock_close: pd.Series,
                             sector: pd.DataFrame | None,
                             benchmark: pd.DataFrame | None) -> pd.DataFrame:
    """Relative-strength columns that need the ticker's own price series.

    Indexed by the ticker's dates; the feature builder merges with the rest.
    Empty frame (no columns) when the sector series is unavailable, so
    coverage correctly reports the features as missing rather than duplicating
    the ticker's own momentum.
    """
    if sector is None or sector.empty:
        return pd.DataFrame(index=stock_close.index)
    b = sector["close"]
    out = pd.DataFrame(index=stock_close.index)
    out["s_rs_5"] = stock_close.pct_change(5) - b.pct_change(5).reindex(out.index, method="nearest")
    out["s_rs_20"] = stock_close.pct_change(20) - b.pct_change(20).reindex(out.index, method="nearest")
    if benchmark is not None and not benchmark.empty:
        bm = benchmark["close"]
        out["s_rs_market_20"] = (
            b.pct_change(20).reindex(out.index, method="nearest")
            - bm.pct_change(20).reindex(out.index, method="nearest")
        )
    return out

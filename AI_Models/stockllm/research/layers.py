"""Assembles the V3 point-in-time layers for the feature builder.

Each layer is a per-ticker frame whose index is the *availability* timeline:

  * fundamentals : statement availability dates + earnings event dates, ffilled
                   so every later date sees the latest public statement/event
  * news         : article publication timestamps, ffilled counts
  * sectors      : sector index OHLCV frames (marketdata.sector)

The feature builder as-of (backward) joins these onto the trading calendar,
which is the causality guarantee: a row at day t only ever sees items whose
availability date is <= t.
"""
from __future__ import annotations

import pandas as pd

from fundamentals.fetcher import earnings_features, fundamental_features
from marketdata import sector as sector_mod
from news.service import news_features
from utils.logging import get_logger

log = get_logger(__name__)


def _union_ffill(*frames: pd.DataFrame) -> pd.DataFrame:
    """Union of indexes with forward fill; empty frame when nothing to merge."""
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0].copy()
    idx = pd.DatetimeIndex(sorted(set().union(*[set(f.index) for f in frames])))
    out = pd.DataFrame(index=idx)
    for f in frames:
        out = out.combine_first(f)
    return out.sort_index().ffill()


def _days_since(idx: pd.DatetimeIndex, ts_col: pd.Series) -> pd.Series:
    diff = idx - pd.DatetimeIndex(ts_col)
    return (diff.total_seconds() / 86400.0).astype(float)


def _fund_layer(ticker: str) -> pd.DataFrame:
    m = _union_ffill(fundamental_features(ticker), earnings_features(ticker))
    if m.empty:
        return m
    if "f_last_earnings_ts" in m.columns:
        m["f_days_since_earnings"] = _days_since(m.index, m["f_last_earnings_ts"])
        m = m.drop(columns=["f_last_earnings_ts"])
    return m


def _news_layer(ticker: str) -> pd.DataFrame:
    m = news_features(ticker)
    if m.empty or "f_last_news_ts" not in m.columns:
        return m
    m["f_days_since_news"] = _days_since(m.index, m["f_last_news_ts"])
    return m.drop(columns=["f_last_news_ts"])


def load_layers(tickers: list[str],
                with_fundamentals: bool = True,
                with_news: bool = True) -> tuple[dict[str, pd.DataFrame],
                                                 dict[str, pd.DataFrame],
                                                 dict[str, pd.DataFrame]]:
    """Build {ticker: frame} for fundamentals, news and sectors.

    Frames come from the local caches; tickers without data simply get an
    empty frame (features will be NaN and coverage reports it honestly).
    """
    fund: dict[str, pd.DataFrame] = {}
    news: dict[str, pd.DataFrame] = {}
    for t in tickers:
        fund[t] = _fund_layer(t) if with_fundamentals else pd.DataFrame()
        news[t] = _news_layer(t) if with_news else pd.DataFrame()
    sectors = sector_mod.load_sectors()
    return fund, news, sectors

"""Causal candlestick geometry features (V4 layer).

Every feature at row ``t`` is computed from the bar at ``t`` (the close of
which is known at the end of the session) and bars at ``t-1, t-2, ...`` --
never from future bars.  Patterns that need the previous bar use ``shift(1)``
only.

The layer is pure geometry (OHLC): volume-based anomaly features already
exist in the numeric layer (``vol_ratio_*``), so this module only guards
against a missing ``volume`` column instead of duplicating it.

Columns (all prefixed ``f_candle_``):

  * body / range geometry     body, body_pct, range, range_pct,
                              upper_wick, lower_wick (+ pct of range)
  * intra-bar position        close_pos (close within the bar's range),
                              body_to_range, upper_to_range, lower_to_range
  * session flags             bullish, bearish, doji
  * open gap                  open_close_gap (open vs prior close)
  * patterns (2-bar)          bull_engulf, bear_engulf, inside_bar,
                              outside_bar
  * patterns (single-bar)     hammer, shooting_star
  * streaks                   up_streak, down_streak (consecutive sessions)
  * availability              f_candle_avail = 1 once the bar is usable
                              (first valid row onward), for the selectivity
                              evidence layer
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_PREFIX = "f_candle_"


def _safe_range(rng: pd.Series) -> pd.Series:
    """Division guard for zero-range bars (doji / halts)."""
    return rng.replace(0.0, np.nan)


def candle_features_from_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """Compute the causal candlestick feature frame for one OHLCV frame.

    ``df`` must have a DatetimeIndex and at least open/high/low/close
    columns (volume optional).  Returns a frame on the same index.
    """
    df = df.copy()
    open_, high, low, close = df["open"], df["high"], df["low"], df["close"]

    f = pd.DataFrame(index=df.index)
    f[f"{_PREFIX}body"] = close - open_
    f[f"{_PREFIX}body_pct"] = f[f"{_PREFIX}body"] / open_.replace(0.0, np.nan)
    f[f"{_PREFIX}range"] = high - low
    rng = _safe_range(f[f"{_PREFIX}range"])
    f[f"{_PREFIX}range_pct"] = f[f"{_PREFIX}range"] / open_.replace(0.0, np.nan)
    f[f"{_PREFIX}upper_wick"] = high - np.maximum(open_, close)
    f[f"{_PREFIX}lower_wick"] = np.minimum(open_, close) - low
    f[f"{_PREFIX}upper_wick_pct"] = f[f"{_PREFIX}upper_wick"] / rng
    f[f"{_PREFIX}lower_wick_pct"] = f[f"{_PREFIX}lower_wick"] / rng
    f[f"{_PREFIX}close_pos"] = (close - low) / rng
    f[f"{_PREFIX}body_to_range"] = f[f"{_PREFIX}body"] / rng
    f[f"{_PREFIX}upper_to_range"] = f[f"{_PREFIX}upper_wick"] / rng
    f[f"{_PREFIX}lower_to_range"] = f[f"{_PREFIX}lower_wick"] / rng

    # bars whose OHLC is missing are "unknown": all geometry-derived flags
    # and any pattern spanning such a bar become NaN, never 0.
    valid = df[["open", "high", "low", "close"]].notna().all(axis=1)

    f[f"{_PREFIX}bullish"] = (close > open_).astype(float).where(valid)
    f[f"{_PREFIX}bearish"] = (close < open_).astype(float).where(valid)
    f[f"{_PREFIX}doji"] = ((f[f"{_PREFIX}body"].abs() / rng) < 0.1).astype(float).where(valid)
    f[f"{_PREFIX}open_close_gap"] = open_ / close.shift(1) - 1.0

    prev_valid = valid.shift(1).fillna(False)

    # --- two-bar patterns ---------------------------------------------------
    prev_bull, prev_bear = f[f"{_PREFIX}bullish"].shift(1), f[f"{_PREFIX}bearish"].shift(1)
    prev_body = f[f"{_PREFIX}body"].shift(1)
    two_bar_mask = valid & prev_valid
    bull_engulf = (
        (prev_bear == 1.0) & (f[f"{_PREFIX}bullish"] == 1.0)
        & (f[f"{_PREFIX}body"] > -prev_body)
        & (open_ <= close.shift(1)) & (close >= open_.shift(1))
    ).astype(float).where(two_bar_mask)
    bear_engulf = (
        (prev_bull == 1.0) & (f[f"{_PREFIX}bearish"] == 1.0)
        & (-f[f"{_PREFIX}body"] > prev_body)
        & (open_ >= close.shift(1)) & (close <= open_.shift(1))
    ).astype(float).where(two_bar_mask)
    f[f"{_PREFIX}bull_engulf"] = bull_engulf
    f[f"{_PREFIX}bear_engulf"] = bear_engulf
    f[f"{_PREFIX}inside_bar"] = (
        (high <= high.shift(1)) & (low >= low.shift(1))
    ).astype(float).where(two_bar_mask)
    f[f"{_PREFIX}outside_bar"] = (
        (high > high.shift(1)) & (low < low.shift(1))
    ).astype(float).where(two_bar_mask)

    # --- single-bar reversal hints -----------------------------------------
    body_abs = f[f"{_PREFIX}body"].abs()
    f[f"{_PREFIX}hammer"] = (
        (f[f"{_PREFIX}lower_wick"] >= 2.0 * body_abs)
        & (f[f"{_PREFIX}upper_wick"] <= body_abs)
        & (body_abs > 0.0)
    ).astype(float).where(valid)
    f[f"{_PREFIX}shooting_star"] = (
        (f[f"{_PREFIX}upper_wick"] >= 2.0 * body_abs)
        & (f[f"{_PREFIX}lower_wick"] <= body_abs)
        & (body_abs > 0.0)
    ).astype(float).where(valid)

    # --- streaks ------------------------------------------------------------
    f[f"{_PREFIX}up_streak"] = f[f"{_PREFIX}bullish"].groupby(
        (f[f"{_PREFIX}bullish"] == 0.0).cumsum()
    ).cumsum().where(valid)
    f[f"{_PREFIX}down_streak"] = f[f"{_PREFIX}bearish"].groupby(
        (f[f"{_PREFIX}bearish"] == 0.0).cumsum()
    ).cumsum().where(valid)

    f[f"{_PREFIX}avail"] = valid.astype(float).where(valid)
    return f


def candle_features(ticker: str, as_of: str | None = None) -> pd.DataFrame:
    """Candlestick features for a ticker's latest OHLCV frame (as-of aware).

    Convenience wrapper for reports: loads the ticker's daily frame and
    returns the candle feature frame; empty when no data is available.
    """
    from marketdata import loader
    try:
        frames, _ = loader.load_market_data([ticker])
    except Exception:
        return pd.DataFrame()
    df = frames.get(ticker)
    if df is None or df.empty:
        return pd.DataFrame()
    if as_of is not None:
        df = df[df.index <= pd.Timestamp(as_of)]
    return candle_features_from_ohlcv(df)

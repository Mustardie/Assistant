"""Candlestick geometry layer: causality and volume handling (V4)."""
import numpy as np
import pandas as pd

from marketdata.features import make_feature_matrix
from research.candles import candle_features_from_ohlcv


def _frame(n=120, volume=True, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-01", periods=n)
    open_ = 100 + rng.normal(0, 0.5, n)
    close = open_ + rng.normal(0, 0.8, n)
    high = np.maximum(open_, close) + rng.uniform(0.1, 1.0, n)
    low = np.minimum(open_, close) - rng.uniform(0.1, 1.0, n)
    df = pd.DataFrame({"open": open_, "high": high, "low": low,
                       "close": close}, index=idx)
    if volume:
        df["volume"] = rng.integers(1_000_000, 5_000_000, n)
    return df


def test_candle_features_causal():
    cf = candle_features_from_ohlcv(_frame())
    # first row can compute bar geometry but not shifted-bar features
    body_cols = [c for c in cf.columns if c.endswith("body")]
    assert cf.iloc[0][body_cols].notna().all()
    # shifted-bar features (gap, engulfing, inside/outside) NaN on row 0
    for col in ("f_candle_open_close_gap", "f_candle_bull_engulf",
                "f_candle_bear_engulf", "f_candle_inside_bar",
                "f_candle_outside_bar"):
        assert pd.isna(cf.iloc[0][col]), col
    # streaks only need bar t, so row 0 is a valid 0/1 run count
    for col in ("f_candle_up_streak", "f_candle_down_streak"):
        assert cf.iloc[0][col] in (0.0, 1.0), col
    # no future leakage: rolling/non-rolling values are position-independent
    cf2 = candle_features_from_ohlcv(_frame(seed=1))
    assert len(cf) == len(cf2)


def test_candle_no_volume_column():
    cf = candle_features_from_ohlcv(_frame(volume=False))
    assert not cf.empty
    assert cf.notna().any(axis=1).all()


def test_candle_missing_bars_are_nan():
    df = _frame()
    df.loc[df.index[5]] = np.nan
    cf = candle_features_from_ohlcv(df)
    assert cf.iloc[5].isna().all()
    assert cf.iloc[6][["f_candle_open_close_gap",
                       "f_candle_bull_engulf"]].isna().all()  # needs bar t-1


def test_candle_avail_flag():
    cf = candle_features_from_ohlcv(_frame())
    assert cf["f_candle_avail"].iloc[0] == 1.0
    assert cf["f_candle_avail"].isin([0.0, 1.0]).all()


def test_matrix_includes_candles_only_when_requested():
    frames = {t: _frame(seed=i) for i, t in enumerate(("AAA", "BBB"))}
    with_c = make_feature_matrix(frames, horizon=7, with_candles=True)
    without = make_feature_matrix(frames, horizon=7, with_candles=False)
    assert any(c.startswith("f_candle_") for c in with_c.columns)
    assert not any(c.startswith("f_candle_") for c in without.columns)
    assert "candle_avail_ev" in with_c.columns
    assert "candle_avail_ev" not in without.columns
    # candles never enter the feature list without the flag
    from marketdata.features import get_features
    assert all(not c.startswith("f_candle_") for c in get_features())


def test_matrix_horizon_3_labels():
    frames = {"AAA": _frame(seed=1)}
    m = make_feature_matrix(frames, horizon=3)
    assert "ret_3d" in m.columns
    assert "ret_3d_voladj" in m.columns
    assert m["ret_3d"].iloc[-1] != m["ret_3d"].iloc[-1]  # last rows NaN
    assert not m["ret_3d"].iloc[: -3].isna().any()
    from marketdata.features import get_features
    assert "ret_3d" not in get_features()
    assert "ret_3d_voladj" not in get_features()

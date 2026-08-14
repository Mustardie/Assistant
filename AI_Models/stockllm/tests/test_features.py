"""Causality and label tests: the future must never leak into features."""
import numpy as np
import pandas as pd
import pytest

from config import HORIZON, LABEL_COLUMN
from marketdata.features import (add_label, build_features, make_feature_matrix)


def _frame(n=400, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.01, n))),
        "low": close * (1 - np.abs(rng.normal(0, 0.01, n))),
        "close": close,
        "volume": rng.integers(100_000, 1_000_000, n),
    }, index=idx)


def test_features_are_causal_when_future_is_dropped():
    df = _frame()
    f1 = build_features(df)
    f2 = build_features(df.iloc[:-10])
    common = f1.index[:-10]
    pd.testing.assert_frame_equal(f1.loc[common], f2.loc[common])


def test_matrix_is_causal_when_future_is_dropped():
    """Matrix-level causality: market-wide features must also be causal.

    The outcome label (ret_7d) is excluded: truncation changes label
    availability near the end by construction (it is a future outcome, not
    an input).  Label correctness is covered by test_label_matches_future_return.
    """
    frames = {"A": _frame(seed=1), "B": _frame(seed=2)}
    m1 = make_feature_matrix(frames).sort_values(["ticker", "date"]).reset_index(drop=True)
    m2 = make_feature_matrix({k: v.iloc[:-10] for k, v in frames.items()}).sort_values(["ticker", "date"]).reset_index(drop=True)
    common_dates = m2["date"].unique()
    m1c = m1[m1["date"].isin(common_dates)].reset_index(drop=True)
    cols = [c for c in m2.columns if c not in (LABEL_COLUMN, "ret_7d_voladj")]
    pd.testing.assert_frame_equal(m1c[cols], m2[cols])


def test_label_matches_future_return():
    df = _frame()
    f = add_label(df)
    expected = df["close"].shift(-HORIZON) / df["close"] - 1.0
    assert np.allclose(f[LABEL_COLUMN], expected, equal_nan=True)


def test_label_nan_on_last_horizon_rows():
    f = add_label(_frame())
    assert int(f[LABEL_COLUMN].isna().sum()) == HORIZON


def test_voladj_target_matches_ret_over_vol():
    from marketdata.features import add_voladj_target
    df = _frame()
    f = add_label(df)
    ret1 = df["close"].pct_change()
    vol_ann = ret1.rolling(63, min_periods=63).std() * np.sqrt(252)
    f = add_voladj_target(f, vol_ann)
    expected = f[LABEL_COLUMN] / vol_ann
    assert np.allclose(f["ret_7d_voladj"], expected, equal_nan=True)


def test_rsi_bounds_and_atr_positive():
    f = build_features(_frame())
    assert f["rsi_14"].dropna().between(0, 100).all()
    assert (f["atr14_norm"].dropna() > 0).all()
    assert (f["bb_pctb"].dropna() >= -5).all() and (f["bb_pctb"].dropna() <= 5).all()


def test_make_feature_matrix_structure():
    frames = {"A": _frame(seed=1), "B": _frame(seed=2)}
    m = make_feature_matrix(frames)
    assert {"ticker", "row_rank", "date", "price", LABEL_COLUMN,
            "ret_7d_voladj", "vol_ann", "region"}.issubset(m.columns)
    assert set(m["ticker"].unique()) == {"A", "B"}
    per_ticker = m.groupby("ticker")["row_rank"]
    assert per_ticker.max().min() >= 130
    assert per_ticker.min().min() == 0


def test_region_and_benchmark_mapping():
    """Indian-listed tickers must get ^NSEI benchmark features, US get ^GSPC."""
    frames = {"A.NS": _frame(seed=1), "B": _frame(seed=2)}
    nsei = _frame(seed=3)
    gspc = _frame(seed=4)
    for f in (nsei, gspc):
        f["close"] = 1000 * np.exp(np.cumsum(np.linspace(0.0001, 0.0001, len(f))))
    benches = {"^NSEI": nsei, "^GSPC": gspc}
    m = make_feature_matrix(frames, benches)
    a = m[m["ticker"] == "A.NS"].sort_values("date").iloc[40]
    b = m[m["ticker"] == "B"].sort_values("date").iloc[40]
    assert a["region"] == 1.0 and b["region"] == 0.0
    assert abs(a["bench_ret_1"] - nsei["close"].pct_change().iloc[40]) < 1e-9
    assert abs(b["bench_ret_1"] - gspc["close"].pct_change().iloc[40]) < 1e-9


def test_no_label_column_inside_features():
    from marketdata.features import get_features
    m = make_feature_matrix({"A": _frame()})
    assert LABEL_COLUMN not in get_features()
    assert "ret_7d_voladj" not in get_features()
    assert "bench_fwd_7" not in get_features()
    assert len(get_features()) > 50

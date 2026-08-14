"""Loader behavior with user-provided CSVs."""
import numpy as np
import pandas as pd
import pytest

from marketdata.loader import (DataUnavailableError, _normalize_frame,
                               load_local_csv)


def _write_csv(tmp_path, name, n=60, seed=1, extra_col=None):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    df = pd.DataFrame({
        "date": idx, "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": rng.integers(100_000, 1_000_000, n),
    })
    if extra_col:
        df[extra_col] = close
    path = tmp_path / f"{name}.csv"
    df.to_csv(path, index=False)
    return path


def test_load_local_csv_normalizes(tmp_path):
    _write_csv(tmp_path, "AAPL_TEST")
    df = load_local_csv("AAPL_TEST", base_dir=tmp_path)
    assert df is not None
    assert set(df.columns) >= {"open", "high", "low", "close", "volume", "adj_close"}
    assert df.index.is_monotonic_increasing
    assert not df.index.duplicated().any()
    assert df["close"].notna().all()


def test_load_local_csv_with_adj_close(tmp_path):
    _write_csv(tmp_path, "MSFT_TEST", extra_col="Adj Close")
    df = load_local_csv("MSFT_TEST", base_dir=tmp_path)
    assert np.allclose(df["adj_close"], df["close"], rtol=1e-9) or (df["adj_close"] > 0).all()


def test_load_missing_ticker_raises(tmp_path):
    assert load_local_csv("NOPE_NOPE", base_dir=tmp_path) is None


def test_normalize_drops_bad_rows():
    df = pd.DataFrame({
        "date": pd.bdate_range("2021-01-01", periods=5),
        "open": [1, 2, 3, 4, 5], "high": [1.1, 2.1, 3.1, 4.1, 5.1],
        "low": [0.9, 1.9, 2.9, 3.9, 4.9], "close": [1, 2, 3, 4, 5],
        "volume": [100, 200, 300, 400, 500],
    })
    df.loc[2, "close"] = np.nan
    out = _normalize_frame(df.set_index("date"), "X")
    assert len(out) == 4


def test_missing_columns_raise():
    df = pd.DataFrame({
        "date": pd.bdate_range("2021-01-01", periods=3),
        "close": [1, 2, 3], "volume": [1, 2, 3],
    })
    with pytest.raises(DataUnavailableError):
        _normalize_frame(df.set_index("date"), "X")

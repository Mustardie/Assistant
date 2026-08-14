"""Chronological splits and the walk-forward leakage guard."""
import numpy as np
import pandas as pd
import pytest

from config import HORIZON
from marketdata.splits import (allowed_train_rows, chronological_split,
                               walk_forward_cutoffs)


def _matrix(n=200):
    rng = np.random.default_rng(0)
    dates = pd.bdate_range("2020-01-01", periods=n)
    rows = []
    for ticker in ("A", "B"):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
        rows.append(pd.DataFrame({
            "date": dates, "ticker": ticker, "row_rank": range(n),
            "price": close,
            "x1": rng.normal(size=n), "x2": rng.normal(size=n),
            "ret_7d": np.r_[rng.normal(0, 0.02, n - HORIZON), [np.nan] * HORIZON],
        }))
    return pd.concat(rows, ignore_index=True)


def test_chronological_split_disjoint_ordered():
    m = _matrix()
    train, val, test = chronological_split(m, "2020-06-30", "2020-09-30")
    train_dates = set(train["date"])
    test_dates = set(test["date"])
    assert not (train_dates & test_dates)
    assert train["date"].max() <= pd.Timestamp("2020-06-30")
    assert test["date"].min() > pd.Timestamp("2020-09-30")
    assert len(train) + len(val) + len(test) == len(m)


def test_walk_forward_cutoff_grid():
    dates = pd.bdate_range("2023-01-01", periods=100)
    cutoffs = walk_forward_cutoffs(dates, refit_every=10)
    assert cutoffs[0] == dates[0]
    assert all(c in dates for c in cutoffs)
    assert len(cutoffs) == 10


def test_allowed_train_rows_excludes_incomplete_labels():
    m = _matrix()
    cutoff = pd.Timestamp("2020-09-10")
    allowed = allowed_train_rows(m, cutoff)
    assert not allowed.empty
    for ticker, g in m.groupby("ticker"):
        cutoff_rank = g.loc[g["date"] <= cutoff, "row_rank"].max()
        allowed_t = allowed[allowed["ticker"] == ticker]
        assert (allowed_t["row_rank"] <= cutoff_rank - HORIZON).all()
        assert allowed_t["ret_7d"].notna().all()


def test_allowed_train_rows_empty_before_history():
    m = _matrix()
    allowed = allowed_train_rows(m, pd.Timestamp("2020-01-05"))
    assert allowed.empty

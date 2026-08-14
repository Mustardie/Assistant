"""Chronological splitting and walk-forward machinery.

Time-series rule: the future never enters the past.  Splits are cuts on the
date axis -- never random shuffles -- and the final test window is completely
untouched by training and model selection until evaluation.
"""
from __future__ import annotations

import pandas as pd

from config import HORIZON, LABEL_COLUMN
from marketdata.features import label_column


def chronological_split(matrix: pd.DataFrame, train_end: str, val_end: str):
    """Split a matrix into train / validation / test by date cuts."""
    train = matrix[matrix["date"] <= pd.to_datetime(train_end)]
    val = matrix[(matrix["date"] > pd.to_datetime(train_end))
                 & (matrix["date"] <= pd.to_datetime(val_end))]
    test = matrix[matrix["date"] > pd.to_datetime(val_end)]
    return train, val, test


def walk_forward_cutoffs(test_dates: pd.DatetimeIndex, refit_every: int) -> list[pd.Timestamp]:
    """Refit cutoffs on a ticker's own test-date grid."""
    cutoffs = []
    for i in range(0, len(test_dates), refit_every):
        cutoffs.append(test_dates[i])
    return cutoffs


def allowed_train_rows(matrix: pd.DataFrame, cutoff: pd.Timestamp,
                       horizon: int = HORIZON,
                       label_col: str | None = None) -> pd.DataFrame:
    """Rows whose *label* is fully known at `cutoff`.

    A row dated `t` is usable at cutoff only if its label window ends at
    or before cutoff (t + horizon <= cutoff).  This is the single most
    important leakage guard in the walk-forward loop: without it, rows near
    the cutoff would carry future information into the fit.
    """
    label = label_col or label_column(horizon)
    rows = []
    for _, g in matrix.groupby("ticker", sort=False):
        known = g.loc[g["date"] <= cutoff, "row_rank"]
        if known.empty:
            continue
        max_rank = known.max()
        usable = g[(g["row_rank"] <= max_rank - horizon) & g[label].notna()]
        rows.append(usable)
    if not rows:
        return pd.DataFrame(columns=matrix.columns)
    return pd.concat(rows)

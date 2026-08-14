"""Walk-forward backtesting engine.

Correctness guarantees, enforced in code:

1. Chronological walk: at refit cutoff C the model is fitted only on rows
   whose label window ends at or before C (`allowed_train_rows`), then used
   only for prediction dates strictly after C.
2. Causal features: see marketdata/features.py (verified by unit tests).
3. Windows: `dev` (model selection, config.BACKTEST_WINDOWS["dev"]) and
   `holdout` (the frozen final holdout).  The holdout is evaluated exactly
   once with the final selected configuration and never used for selection.

Scope: "pooled" (one model over all tickers), "id" (pooled + ticker_id
feature), "per_stock" (one model per ticker).  Compared on the dev window
only (`main.py diagnose`).

Each recorded forecast stores: date, ticker, price, prediction, confidence
signals, actual return, direction correctness, error, model version, features
used, the refit cutoff that produced it (so the no-leakage property is
auditable in the results file itself), plus baseline columns (prior
momentum, benchmark returns, forward benchmark return) so evaluation can
compute momentum/index baselines from the results file alone.
"""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd

from config import (BACKTEST_MIN_TESTS, BACKTEST_WINDOWS, DEFAULT_BACKTEST_WINDOW,
                    DEFAULT_MODEL_SCOPE, DEFAULT_TARGET, HORIZON,
                    MIN_TRAIN_ROWS, MODEL_SCOPES, REFIT_EVERY_DAYS, RESULTS_DEV_DIR,
                    RESULTS_HOLDOUT_DIR, TARGET_VARIANTS)
from forecasting.numerical import NumericalForecaster, PerStockForecaster
from marketdata.features import get_features, label_column
from marketdata.splits import allowed_train_rows, walk_forward_cutoffs
from utils.logging import get_logger

log = get_logger(__name__)


class BacktestEngine:
    """Walk-forward engine over a feature matrix (see make_feature_matrix).

    `horizon` selects the label column (``ret_7d`` legacy / ``ret_{h}d``):
    the matrix must have been built for the same horizon.
    """

    def __init__(self, matrix: pd.DataFrame, test_start: str | None = None,
                 test_end: str | None = None, window: str | None = None,
                 refit_every: int = REFIT_EVERY_DAYS, horizon: int = HORIZON,
                 min_train_rows: int = MIN_TRAIN_ROWS,
                 scope: str = DEFAULT_MODEL_SCOPE, target: str = DEFAULT_TARGET,
                 features_override: list[str] | None = None):
        if scope not in MODEL_SCOPES:
            raise ValueError(f"unknown scope {scope!r} (choose from {MODEL_SCOPES})")
        if target not in TARGET_VARIANTS:
            raise ValueError(f"unknown target {target!r} (choose from {TARGET_VARIANTS})")
        if window is not None:
            test_start = test_start or BACKTEST_WINDOWS[window]["start"]
            test_end = test_end if test_end is not None else BACKTEST_WINDOWS[window]["end"]
        self.matrix = matrix.sort_values(["ticker", "date"]).reset_index(drop=True)
        self.test_start = pd.Timestamp(test_start or BACKTEST_WINDOWS[DEFAULT_BACKTEST_WINDOW]["start"])
        self.test_end = pd.Timestamp(test_end) if test_end else None
        self.refit_every = refit_every
        self.horizon = horizon
        self.label_col = label_column(horizon)
        self.min_train_rows = min_train_rows
        self.scope = scope
        self.target = target
        self.features = list(features_override or
                             (get_features() + (["ticker_id"] if scope == "id" else [])))
        self._model_cache: dict[tuple, object] = {}

    # -- model fitting -----------------------------------------------------

    def _usable_feature_cols(self, train: pd.DataFrame) -> list[str]:
        """Features that exist and carry at least one non-NaN value in the
        training slice.

        An all-NaN column cannot contribute to a fit; leaving it in the
        dropna subset would silently zero out the whole training set.
        """
        return [c for c in self.features
                if c in train.columns and train[c].notna().any()]

    def _pooled_train(self, cutoff: pd.Timestamp) -> tuple[pd.DataFrame, list[str]]:
        train = allowed_train_rows(self.matrix, cutoff, self.horizon, self.label_col)
        cols = self._usable_feature_cols(train)
        return train.dropna(subset=cols + [self.label_col]), cols

    def _per_stock_train(self, cutoff: pd.Timestamp,
                         ticker: str) -> tuple[pd.DataFrame, list[str]]:
        train = allowed_train_rows(self.matrix, cutoff, self.horizon, self.label_col)
        train = train[train["ticker"] == ticker]
        cols = self._usable_feature_cols(train)
        return train.dropna(subset=cols + [self.label_col]), cols

    def _fit_at(self, cutoff: pd.Timestamp):
        key = (cutoff, None)
        if key in self._model_cache:
            return self._model_cache[key]
        if self.scope == "per_stock":
            matrices = {}
            common = None
            for t in sorted(self.matrix["ticker"].unique()):
                train, cols = self._per_stock_train(cutoff, t)
                min_rows = max(50, self.min_train_rows // 4)
                if len(train) >= min_rows:
                    matrices[t] = train
                    common = set(cols) if common is None else (common & set(cols))
            if not matrices or not common:
                log.warning("refit %s skipped: no ticker has enough rows "
                            "with usable features", cutoff.date())
                return None
            model = PerStockForecaster(sorted(common), min_rows=min_rows,
                                       target=self.target, horizon=self.horizon,
                                       label_col=self.label_col).fit(matrices)
        else:
            train, cols = self._pooled_train(cutoff)
            if len(train) < self.min_train_rows:
                log.warning("refit at %s skipped: only %d train rows "
                            "(%d usable features)",
                            cutoff.date(), len(train), len(cols))
                return None
            model = NumericalForecaster(cols, min_rows=self.min_train_rows,
                                        target=self.target,
                                        horizon=self.horizon,
                                        label_col=self.label_col).fit(train)
        self._model_cache[key] = model
        return model

    # -- the walk ----------------------------------------------------------

    def run(self) -> pd.DataFrame:
        m = self.matrix
        test_rows = m[m["date"] >= self.test_start]
        if self.test_end is not None:
            test_rows = test_rows[test_rows["date"] <= self.test_end]
        if test_rows.empty:
            raise ValueError("no rows in the test window "
                             "(check config.BACKTEST_WINDOWS dates vs data)")

        records = []
        for ticker, g in test_rows.groupby("ticker", sort=True):
            dates = pd.DatetimeIndex(sorted(g["date"].unique()))
            cutoffs = walk_forward_cutoffs(dates, self.refit_every)
            for i, cutoff in enumerate(cutoffs):
                next_cutoff = cutoffs[i + 1] if i + 1 < len(cutoffs) else None
                window = [d for d in dates if cutoff < d and (next_cutoff is None or d < next_cutoff)]
                if not window:
                    continue
                model = self._fit_at(cutoff)
                if model is None:
                    continue
                batch = g[g["date"].isin(window)]
                preds = model.predict(batch)
                if preds.empty:
                    continue
                for (_, row), (_, pr) in zip(batch.iterrows(), preds.iterrows()):
                    actual = row.get(self.label_col)
                    if not np.isfinite(pr["pred_ret"]) or not np.isfinite(actual):
                        continue
                    rec = self._record(ticker, row, pr, cutoff, model)
                    records.append(rec)
                log.info("ticker=%s cutoff=%s predicted %d rows",
                         ticker, cutoff.date(), len(window))

        results = pd.DataFrame(records)
        if results.empty:
            raise ValueError(
                "backtest produced 0 forecasts -- every walk-forward refit was "
                f"skipped (min_train_rows={self.min_train_rows}, "
                f"{len(test_rows)} test rows across "
                f"{test_rows['ticker'].nunique()} ticker(s)). Check that the "
                "requested feature set has non-NaN values in the training "
                "window (all-NaN columns are dropped from each fit) and that "
                "config.MIN_TRAIN_ROWS is below the available history."
            )
        if len(results) < BACKTEST_MIN_TESTS:
            log.warning(
                "only %d forecasts recorded (target %d) -- smaller windows are "
                "expected to stay below the target; add tickers for more.",
                len(results), BACKTEST_MIN_TESTS,
            )
        return results

    def _record(self, ticker: str, row: pd.Series, pr: pd.Series,
                cutoff: pd.Timestamp, model) -> dict:
        def num(name, default=np.nan):
            v = row.get(name, np.nan)
            return float(v) if np.isfinite(v) else default
        bench_fwd_name = ("bench_fwd_7" if self.horizon == HORIZON
                          else f"bench_fwd_{self.horizon}d")
        rec = {
            "ticker": ticker,
            "date": str(row["date"].date()),
            "refit_cutoff": str(pd.Timestamp(cutoff).date()),
            "horizon_days": self.horizon,
            "price": float(row["price"]),
            "pred_ret": float(pr["pred_ret"]),
            "prob_up": float(pr["prob_up"]),
            "q_lo": float(pr["q_lo"]),
            "q_hi": float(pr["q_hi"]),
            "actual_ret": float(row[self.label_col]),
            "direction_correct": bool((pr["prob_up"] >= 0.5) == (row[self.label_col] > 0)),
            "abs_error": float(abs(pr["pred_ret"] - row[self.label_col])),
            "model_version": model.version,
            "scope": self.scope,
            "target": self.target,
            "features_used": json.dumps(self.features),
            # baseline columns (evaluation-only)
            "prior_20d": num("cum_ret_20"),
            "prior_60d": num("cum_ret_60"),
            "bench_ret_1": num("bench_ret_1"),
            "bench_ret_5": num("bench_ret_5"),
            "bench_ret_20": num("bench_ret_20"),
            "bench_fwd_7": num(bench_fwd_name),
            "vol_ann": num("vol_ann"),
            # evidence availability at forecast time (selectivity layer)
            "fund_avail_ev": num("fund_avail_ev", 0.0),
            "news_avail_ev": num("news_avail_ev", 0.0),
            "sector_avail_ev": num("sector_avail_ev", 0.0),
            "candle_avail_ev": num("candle_avail_ev", 0.0),
        }
        return rec

    @staticmethod
    def save(results: pd.DataFrame, window: str) -> str:
        dir_path = RESULTS_HOLDOUT_DIR if window == "holdout" else RESULTS_DEV_DIR
        dir_path.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = dir_path / f"backtest_{window}_{stamp}.csv"
        results.to_csv(path, index=False)
        return str(path)

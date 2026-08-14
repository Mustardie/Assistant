"""Numerical forecasting models (LightGBM, CPU-friendly).

The numerical layer owns the raw numbers:
  * expected 7-day return           (mean regression)
  * probability of a positive return (binary classification)
  * low/high quantiles of return     (quantile regression -> price range)

The LLM layer never computes numbers; it reasons over the output of this
layer.

Model scope: `pooled` trains one model on all tickers; `id` adds the
ticker_id categorical feature to the pooled model; `per_stock` trains one
model per ticker (see PerStockForecaster below).  The choice is an
experiment measured on the dev window (see `main.py diagnose`), not an
assertion.

Target variants (for mean/quantile heads):
  * raw    : predict ret_7d directly (V1)
  * voladj : predict ret_7d / annualized_vol_63 (scale-free, so the pooled
             model is not dominated by high-vol names), converted back to
             raw-return space at predict time so results stay comparable.

Early stopping uses the chronologically last 15% of the fit data, so the
stopping decision never sees the future.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import warnings

import lightgbm as lgb
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message=".*eval_set.*deprecated.*")

from config import (EARLY_STOPPING_ROUNDS, HORIZON, LABEL_COLUMN, LGB_PARAMS,
                    MODEL_VERSION, QUANTILES, SEED)
from marketdata.features import label_column, voladj_label
from utils.logging import get_logger

log = get_logger(__name__)

VOLADJ_COLUMN = "ret_7d_voladj"  # legacy name, kept for old pickles


class NumericalForecaster:
    """LightGBM forecaster: expected return, P(up), return quantiles.

    One instance = one model fit on one matrix.  `scope` is informational
    (versions/importance reporting); pooling behavior is decided by which
    matrix the caller passes (see BacktestEngine).

    `horizon` / `label_col` pin the forecast target: a 3-day model predicts
    the ``ret_3d`` column, a 7-day model the legacy ``ret_7d``.  Both must
    match the matrix the model is fit on.
    """

    def __init__(self, features, params=None, quantiles=QUANTILES, min_rows=100,
                 target: str = "raw", horizon: int = HORIZON,
                 label_col: str | None = None):
        self.features = list(features)
        self.params = dict(params or LGB_PARAMS)
        self.quantiles = tuple(quantiles)
        self.min_rows = min_rows
        self.target = target
        self.horizon = int(horizon)
        self.label_col = label_col or label_column(self.horizon)
        self.models = {}
        self.version = MODEL_VERSION
        self.fit_stats = {}
        self._label = (self.label_col if target == "raw"
                       else voladj_label(self.label_col))

    def _prepare(self, matrix: pd.DataFrame) -> pd.DataFrame:
        df = matrix.copy()
        need = [c for c in self.features if c not in df.columns]
        if need:
            raise ValueError(f"missing feature columns: {need}")
        if self._label not in df.columns:
            raise ValueError(f"missing target column: {self._label} "
                             f"(target variant '{self.target}')")
        df = df.dropna(subset=self.features + [self._label])
        sort_by = [c for c in ("date", "ticker") if c in df.columns]
        df = df.sort_values(sort_by).reset_index(drop=True)
        return df

    def fit(self, matrix: pd.DataFrame) -> "NumericalForecaster":
        df = self._prepare(matrix)
        if len(df) < self.min_rows:
            raise ValueError(f"too few rows for training: {len(df)}")
        split = int(len(df) * 0.85)
        train, val = df.iloc[:split], df.iloc[split:]
        Xtr, ytr = train[self.features], train[self._label]
        Xva, yva = val[self.features], val[self._label]
        cb = [lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)]

        self.models["mean"] = lgb.LGBMRegressor(objective="regression", **self.params)
        self.models["mean"].fit(Xtr, ytr, eval_set=[(Xva, yva)], callbacks=cb)

        ybin = (ytr > 0).astype(int)
        ybin_v = (yva > 0).astype(int)
        self.models["prob"] = lgb.LGBMClassifier(objective="binary", **self.params)
        self.models["prob"].fit(Xtr, ybin, eval_set=[(Xva, ybin_v)], callbacks=cb)

        for q in self.quantiles:
            m = lgb.LGBMRegressor(objective="quantile", alpha=q, **self.params)
            m.fit(Xtr, ytr, eval_set=[(Xva, yva)], callbacks=cb)
            self.models[f"q{int(q * 100)}"] = m

        self.fit_stats = {
            "train_rows": int(len(train)),
            "val_rows": int(len(val)),
            "mean_label": float(ytr.mean()),
            "up_rate_train": float(ybin.mean()),
            "target": self.target,
            "horizon": self.horizon,
        }
        self.version = self._compute_version()
        log.info("fitted %s (target=%s, horizon=%dd): %s",
                 self.version, self.target, self.horizon, self.fit_stats)
        return self

    def _compute_version(self) -> str:
        digest = hashlib.sha256(
            json.dumps({"features": self.features, "params": self.params,
                        "target": self.target, "horizon": self.horizon},
                       sort_keys=True).encode()
        ).hexdigest()[:8]
        return f"{MODEL_VERSION}-{digest}"

    def _to_return_space(self, df: pd.DataFrame, values: np.ndarray) -> np.ndarray:
        """Convert voladj-space predictions back to raw-return space."""
        if self.target != "voladj":
            return values
        if "vol_ann" not in df.columns:
            raise ValueError("target='voladj' needs the vol_ann column at predict time")
        vol = df["vol_ann"].to_numpy()
        return np.where(np.isfinite(vol) & (vol > 0), values * vol, np.nan)

    def predict(self, matrix: pd.DataFrame) -> pd.DataFrame:
        df = matrix.copy()
        need = [c for c in self.features if c not in df.columns]
        if need:
            raise ValueError(f"missing feature columns: {need}")
        X = df[self.features]
        out = pd.DataFrame(index=df.index)
        out["pred_ret"] = self._to_return_space(df, self.models["mean"].predict(X))
        out["prob_up"] = self.models["prob"].predict_proba(X)[:, 1]
        q_lo_name = f"q{int(min(self.quantiles) * 100)}"
        q_hi_name = f"q{int(max(self.quantiles) * 100)}"
        out["q_lo"] = self._to_return_space(df, self.models[q_lo_name].predict(X))
        out["q_hi"] = self._to_return_space(df, self.models[q_hi_name].predict(X))
        return out

    def feature_importance(self, top: int = 10) -> list[tuple[str, float]]:
        """Gain importance for every head, returned as {head: [(feat, gain)]}."""
        out = {}
        for name, model in self.models.items():
            ranked = sorted(zip(self.features, model.feature_importances_),
                            key=lambda x: -x[1])
            out[name] = ranked[:top]
        return out

    def save(self, path) -> None:
        with open(path, "wb") as fh:
            pickle.dump(self, fh)

    @classmethod
    def load(cls, path) -> "NumericalForecaster":
        with open(path, "rb") as fh:
            return pickle.load(fh)


class PerStockForecaster:
    """Per-ticker LightGBM forecasters sharing one parameter set.

    Same predict() interface as NumericalForecaster so the engine treats
    both identically.  A ticker with too little history is skipped; predict
    returns NaN for it (the engine drops non-finite rows downstream).
    """

    def __init__(self, features, params=None, quantiles=QUANTILES, min_rows=60,
                 target: str = "raw", horizon: int = HORIZON,
                 label_col: str | None = None):
        self.features = list(features)
        self.params = dict(params or LGB_PARAMS)
        self.quantiles = tuple(quantiles)
        self.min_rows = min_rows
        self.target = target
        self.horizon = int(horizon)
        self.label_col = label_col or label_column(self.horizon)
        self._models: dict[str, NumericalForecaster] = {}
        self.version = MODEL_VERSION
        self.fit_stats = {}

    def fit(self, matrices: dict[str, pd.DataFrame]) -> "PerStockForecaster":
        for ticker, matrix in matrices.items():
            try:
                self._models[ticker] = NumericalForecaster(
                    self.features, self.params, self.quantiles, self.min_rows,
                    self.target, self.horizon, self.label_col).fit(matrix)
            except ValueError as exc:
                log.warning("per_stock: %s skipped (%s)", ticker, exc)
        if not self._models:
            raise ValueError("per_stock: no ticker could be fitted")
        self.fit_stats = {"n_tickers": len(self._models),
                          "target": self.target,
                          "horizon": self.horizon,
                          "versions": [m.version for m in self._models.values()][:3]}
        self.version = MODEL_VERSION + "-perstock"
        return self

    def predict(self, matrix: pd.DataFrame) -> pd.DataFrame:
        out = []
        for ticker, g in matrix.groupby("ticker", sort=False):
            model = self._models.get(ticker)
            if model is None:
                continue
            preds = model.predict(g)
            preds["ticker"] = ticker
            out.append(preds)
        if not out:
            return pd.DataFrame()
        return pd.concat(out)

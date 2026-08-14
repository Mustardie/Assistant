"""Model-selection diagnostics for the dev window (never the holdout).

  * permutation_importance: out-of-sample permutation importance computed
    over the walk-forward predictions themselves (each refit's model is
    applied to its own prediction window, feature j is shuffled within that
    window, and the MAE/accuracy degradation is aggregated over all windows).
  * variant_comparison: pooled vs pooled+ticker_id vs per_stock, and target
    variants, measured on the dev window only.
  * robustness: per-ticker / per-year / per-regime breakdowns (also produced
    by `evaluate` from the results CSV).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtesting.engine import BacktestEngine
from config import MODEL_SCOPES, TARGET_VARIANTS
from evaluation import metrics
from marketdata.features import get_features
from utils.logging import get_logger

log = get_logger(__name__)


def _batch_for_window(matrix: pd.DataFrame, window_rows: pd.DataFrame) -> pd.DataFrame:
    keys = window_rows[["ticker", "date"]]
    m = matrix.copy()
    m["date"] = pd.to_datetime(m["date"])
    keys["date"] = pd.to_datetime(keys["date"])
    batch = m.merge(keys, on=["ticker", "date"], how="inner")
    return batch


def permutation_importance(results: pd.DataFrame, matrix: pd.DataFrame,
                           engine: BacktestEngine, features: list[str],
                           n_perm: int = 2, seed: int = 42) -> pd.DataFrame:
    """Out-of-sample permutation importance over the walk-forward predictions.

    For each refit cutoff we use the *same* model that produced the recorded
    forecasts, so the importance estimate is out-of-sample by construction.
    """
    rng = np.random.default_rng(seed)
    results = results.copy()
    base_mae_sum, base_acc_sum, n_tot = 0.0, 0.0, 0
    perms = {f: {"mae_sum": 0.0, "acc_sum": 0.0} for f in features}
    cutoffs = results["refit_cutoff"].unique()
    for cutoff in cutoffs:
        sub = results[results["refit_cutoff"] == cutoff]
        batch = _batch_for_window(matrix, sub)
        if batch.empty:
            continue
        model = engine._model_cache.get((pd.Timestamp(cutoff), None))
        if model is None:
            continue
        preds = model.predict(batch)
        keep = np.isfinite(preds["pred_ret"].to_numpy()) & np.isfinite(batch["ret_7d"].to_numpy())
        if keep.sum() == 0:
            continue
        y = batch["ret_7d"].to_numpy()[keep]
        p = preds["pred_ret"].to_numpy()[keep]
        w = int(keep.sum())
        base_mae_sum += float(np.abs(p - y).sum())
        base_acc_sum += float(((preds["prob_up"].to_numpy()[keep] >= 0.5) == (y > 0)).sum())
        n_tot += w
        for feat in features:
            X = batch[features].copy().to_numpy()
            col = features.index(feat)
            d_mae_sum = 0.0
            d_acc_sum = 0.0
            for _ in range(n_perm):
                Xp = X.copy()
                Xp[:, col] = rng.permutation(Xp[:, col])
                perm_df = batch.copy()
                perm_df[features] = Xp
                pp = model.predict(perm_df)
                pk = pp["pred_ret"].to_numpy()[keep]
                d_mae_sum += float(np.abs(pk - y).sum())
                d_acc_sum += float(((pp["prob_up"].to_numpy()[keep] >= 0.5) == (y > 0)).sum())
            perms[feat]["mae_sum"] += d_mae_sum
            perms[feat]["acc_sum"] += d_acc_sum
    if n_tot == 0:
        return pd.DataFrame(columns=["feature", "dmae", "dacc", "n"])
    rows = []
    for feat, d in perms.items():
        rows.append({
            "feature": feat,
            "dmae_pp": (d["mae_sum"] / n_perm - base_mae_sum) / n_tot * 100,
            "dacc_pp": (d["acc_sum"] / n_perm - base_acc_sum) / n_tot * 100,
            "n": n_tot,
        })
    out = pd.DataFrame(rows).sort_values("dmae_pp", ascending=False)
    return out


def variant_comparison(matrix: pd.DataFrame, scopes=MODEL_SCOPES,
                       targets=TARGET_VARIANTS) -> pd.DataFrame:
    """Compare scope x target variants on the dev window (never the holdout)."""
    rows = []
    for scope in scopes:
        for target in targets:
            log.info("variant: scope=%s target=%s", scope, target)
            engine = BacktestEngine(matrix, window="dev", scope=scope, target=target)
            results = engine.run()
            if results.empty:
                rows.append({"scope": scope, "target": target, "n": 0})
                continue
            trades = metrics.simulated_trade_returns(results)
            traded = trades[trades != 0]
            rows.append({
                "scope": scope,
                "target": target,
                "n": len(results),
                "dir_acc": metrics.directional_accuracy(results),
                "base_rate": metrics.base_rate_up(results),
                "mae_pp": metrics.mae(results) * 100,
                "zero_pred_mae_pp": float(results["actual_ret"].abs().mean()) * 100,
                "cal_err": metrics.calibration_error(results),
                "brier": metrics.brier_score(results),
                "prec_0.55": metrics.precision_positive(results, 0.55),
                "trade_win_rate": (float((traded > 0).mean()) if len(traded) else np.nan),
                "mean_trade_ret_pp": (float(traded.mean() * 100) if len(traded) else np.nan),
                "n_trades": int(len(traded)),
            })
    return pd.DataFrame(rows)


def to_markdown_table(df: pd.DataFrame) -> str:
    lines = ["| " + " | ".join(str(c) for c in df.columns) + " |",
             "|" + "|".join("---" for _ in df.columns) + "|"]
    for _, r in df.iterrows():
        cells = []
        for c in df.columns:
            v = r[c]
            if isinstance(v, float) and np.isfinite(v) and c not in ("n", "n_trades"):
                cells.append(f"{v:.3f}" if c == "cal_err" or c == "brier" else f"{v*100:.2f}")
            elif isinstance(v, float) and np.isfinite(v):
                cells.append(f"{v:.0f}")
            elif isinstance(v, float):
                cells.append("n/a")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)

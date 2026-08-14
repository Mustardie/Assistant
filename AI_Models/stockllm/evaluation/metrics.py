"""Evaluation metrics for forecast results.

All functions take a results DataFrame with at least the columns
    actual_ret, pred_ret, prob_up, direction_correct
and return plain floats/dicts so they stay easy to unit-test.

Simulated-strategy numbers are explicitly labeled "indicative": they assume
each qualifying forecast is held for the horizon with transaction costs, and
are NOT a claim about real trading outcomes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_TF = 252 / 7  # annualization factor for 7-day returns


def directional_accuracy(df: pd.DataFrame) -> float:
    if len(df) == 0:
        return 0.0
    return float(df["direction_correct"].mean())


def base_rate_up(df: pd.DataFrame) -> float:
    if len(df) == 0:
        return 0.0
    return float((df["actual_ret"] > 0).mean())


def mae(df: pd.DataFrame) -> float:
    return float((df["pred_ret"] - df["actual_ret"]).abs().mean())


def rmse(df: pd.DataFrame) -> float:
    return float(np.sqrt(((df["pred_ret"] - df["actual_ret"]) ** 2).mean()))


def bias(df: pd.DataFrame) -> float:
    return float((df["pred_ret"] - df["actual_ret"]).mean())


def brier_score(df: pd.DataFrame) -> float:
    y = (df["actual_ret"] > 0).astype(float)
    return float(((df["prob_up"] - y) ** 2).mean())


def precision_positive(df: pd.DataFrame, threshold: float = 0.5) -> float:
    calls = df[df["prob_up"] >= threshold]
    if calls.empty:
        return 0.0
    return float((calls["actual_ret"] > 0).mean())


def recall_positive(df: pd.DataFrame, threshold: float = 0.5) -> float:
    ups = df[df["actual_ret"] > 0]
    if ups.empty:
        return 0.0
    return float((ups["prob_up"] >= threshold).mean())


def calibration_table(df: pd.DataFrame, bins: int = 10) -> pd.DataFrame:
    """Binned P(up) vs realized up-frequency: the honesty check."""
    y = (df["actual_ret"] > 0).astype(float)
    binned = pd.cut(df["prob_up"], bins=np.linspace(0, 1, bins + 1), include_lowest=True)
    g = pd.DataFrame({"prob_up": df["prob_up"], "y": y}).groupby(binned, observed=True)
    table = g.agg(pred=("prob_up", "mean"), actual=("y", "mean"), n=("y", "size"))
    table["err"] = (table["pred"] - table["actual"]).abs()
    return table


def calibration_error(df: pd.DataFrame, bins: int = 10) -> float:
    table = calibration_table(df, bins)
    if table["n"].sum() == 0:
        return 0.0
    return float((table["err"] * table["n"]).sum() / table["n"].sum())


def simulated_trade_returns(df: pd.DataFrame, buy_threshold: float = 0.55,
                            costs_per_trade: float = 0.002) -> pd.Series:
    """Per-forecast trade returns for an indicative long-only rule."""
    returns = np.where(df["prob_up"] >= buy_threshold,
                       df["actual_ret"] - costs_per_trade, 0.0)
    return pd.Series(returns, index=df.index)


def return_stats(returns: np.ndarray | pd.Series) -> dict:
    """mean/std/total/Sharpe/max-drawdown of a return series."""
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return {"n": 0}
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    drawdowns = equity / peak - 1.0
    std = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    return {
        "n": int(len(r)),
        "mean": float(r.mean()),
        "std": float(std),
        "total": float(equity[-1] - 1.0),
        "sharpe": float(r.mean() / std * np.sqrt(_TF)) if std > 0 else 0.0,
        "max_drawdown": float(drawdowns.min()),
    }


def benchmark_comparison(df: pd.DataFrame) -> dict:
    """How the model compares with trivial baselines on the same rows.

    Baselines:
      * always-up      : predict "up" for every row (base rate)
      * zero-prediction: predict 0 return (MAE = |actual|)
      * momentum       : predict the sign of the prior 20d return
      * index-follow   : predict the forward 7d return of the home index
    """
    out = {
        "directional_accuracy": directional_accuracy(df),
        "base_rate_up (always-up)": base_rate_up(df),
        "model_mae": mae(df),
        "zero_prediction_mae": float(df["actual_ret"].abs().mean()),
        "precision_pos_calls": precision_positive(df, 0.5),
    }
    if "prior_20d" in df.columns and df["prior_20d"].notna().any():
        out["momentum_accuracy"] = float(((df["prior_20d"] > 0) == (df["actual_ret"] > 0)).mean())
    if "bench_fwd_7" in df.columns and df["bench_fwd_7"].notna().any():
        out["index_follow_mae"] = float((df["bench_fwd_7"] - df["actual_ret"]).abs().mean())
        out["index_follow_accuracy"] = float(((df["bench_fwd_7"] > 0) == (df["actual_ret"] > 0)).mean())
    if "bench_ret_20" in df.columns and df["bench_ret_20"].notna().any():
        up_market = df["bench_ret_20"] > 0
        out["dir_acc_up_market"] = float(directional_accuracy(df[up_market]))
        out["dir_acc_down_market"] = float(directional_accuracy(df[~up_market]))
    return out


def breakdown(df: pd.DataFrame, by: str) -> pd.DataFrame:
    """Per-group accuracy/MAE/n of a results frame (robustness check).

    `by` is a column of the results frame; groups with n < 30 are omitted.
    """
    g = df.groupby(by, sort=True)
    rows = []
    for key, sub in g:
        if len(sub) < 30:
            continue
        rows.append({
            by: key,
            "n": len(sub),
            "dir_acc": directional_accuracy(sub),
            "base_rate": base_rate_up(sub),
            "mae": mae(sub),
            "zero_pred_mae": float(sub["actual_ret"].abs().mean()),
        })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(by)
    return out


def vol_regime_breakdown(df: pd.DataFrame, n_bins: int = 3) -> pd.DataFrame:
    """Break results down by the ticker's annualized volatility at forecast time."""
    if "vol_ann" not in df.columns or df["vol_ann"].notna().sum() < 30:
        return pd.DataFrame()
    d = df.copy()
    d["vol_bin"] = pd.qcut(d["vol_ann"], n_bins, labels=False, duplicates="drop")
    d["vol_bin"] = d["vol_bin"].map(lambda b: f"v{b}")
    return breakdown(d, "vol_bin")


def summarize(df: pd.DataFrame, title: str) -> str:
    """Markdown summary of a backtest results frame."""
    if df.empty:
        return f"# {title}\n\nNo forecasts recorded."
    lines = [
        f"# {title}",
        "",
        f"- **Forecasts recorded:** {len(df)}",
        f"- **Tickers:** {', '.join(sorted(df['ticker'].unique()))}",
        f"- **Period:** {df['date'].min()} .. {df['date'].max()}",
        f"- **Directional accuracy:** {directional_accuracy(df) * 100:.1f}% "
        f"(base rate: {base_rate_up(df) * 100:.1f}%)",
        f"- **MAE:** {mae(df) * 100:.2f}pp | **RMSE:** {rmse(df) * 100:.2f}pp | **Bias:** {bias(df) * 100:.2f}pp",
        f"- **Brier score:** {brier_score(df):.4f} | **Calibration error:** {calibration_error(df):.4f}",
        f"- **Avg predicted return:** {df['pred_ret'].mean() * 100:+.2f}% | "
        f"**Avg actual return:** {df['actual_ret'].mean() * 100:+.2f}%",
        "",
    ]
    trades = simulated_trade_returns(df)
    traded = trades[trades != 0]
    if len(traded):
        all_long = df["actual_ret"] - 0.002
        sim_stats = return_stats(traded.values)
        lines += [
            "",
            "## Simulated rule (indicative, per-forecast trades)",
            "",
            "Long when P(up) >= 0.55, held for the horizon, 0.2% round-trip costs. "
            "Per-forecast trade statistics -- NOT a portfolio simulation "
            "(for that, use `python main.py paper`).",
            "",
            f"- Trades triggered: {len(traded)} / {len(df)}",
            f"- Trade win rate: {(traded > 0).mean() * 100:.1f}% "
            f"(all-long win rate: {(all_long > 0).mean() * 100:.1f}%)",
            f"- Mean trade return: {traded.mean() * 100:+.2f}% "
            f"(all-long: {all_long.mean() * 100:+.2f}%)",
            f"- Median trade return: {np.median(traded) * 100:+.2f}%",
            f"- Max drawdown (simulated series): {sim_stats['max_drawdown'] * 100:.2f}%",
        ]
    lines += ["", "## Calibration (P(up) vs realized up-rate)", ""]
    table = calibration_table(df)
    lines.append("| bin | predicted | realized | n |")
    lines.append("|-----|-----------|----------|---|")
    for _, row in table.iterrows():
        lines.append(f"| {row.name} | {row['pred']:.2f} | {row['actual']:.2f} | {int(row['n'])} |")
    lines += ["", "## Benchmark comparison", ""]
    for k, v in benchmark_comparison(df).items():
        lines.append(f"- {k}: {v:.4f}")

    lines += ["", "## Robustness breakdowns", ""]
    bt = breakdown(df, "ticker")
    if not bt.empty:
        lines += ["### By ticker", ""]
        lines.append("| ticker | n | dir acc | base rate | MAE | zero-pred MAE |")
        lines.append("|--------|---|---------|-----------|-----|---------------|")
        for _, r in bt.iterrows():
            lines.append(
                f"| {r['ticker']} | {int(r['n'])} | {r['dir_acc'] * 100:.1f}% | "
                f"{r['base_rate'] * 100:.1f}% | {r['mae'] * 100:.2f}pp | "
                f"{r['zero_pred_mae'] * 100:.2f}pp |")
    if "date" in df.columns:
        yr = df.copy()
        yr["year"] = pd.to_datetime(yr["date"]).dt.year
        by_year = breakdown(yr, "year")
        if not by_year.empty:
            lines += ["", "### By year", ""]
            lines.append("| year | n | dir acc | base rate | MAE | zero-pred MAE |")
            lines.append("|------|---|---------|-----------|-----|---------------|")
            for _, r in by_year.iterrows():
                lines.append(
                    f"| {int(r['year'])} | {int(r['n'])} | {r['dir_acc'] * 100:.1f}% | "
                    f"{r['base_rate'] * 100:.1f}% | {r['mae'] * 100:.2f}pp | "
                    f"{r['zero_pred_mae'] * 100:.2f}pp |")
        ym = df.copy()
        ym["month"] = pd.to_datetime(ym["date"]).dt.strftime("%Y-%m")
        by_month = breakdown(ym, "month")
        if not by_month.empty:
            lines += ["", "### By month", ""]
            lines.append("| month | n | dir acc | base rate | MAE | zero-pred MAE |")
            lines.append("|-------|---|---------|-----------|-----|---------------|")
            for _, r in by_month.iterrows():
                lines.append(
                    f"| {r['month']} | {int(r['n'])} | {r['dir_acc'] * 100:.1f}% | "
                    f"{r['base_rate'] * 100:.1f}% | {r['mae'] * 100:.2f}pp | "
                    f"{r['zero_pred_mae'] * 100:.2f}pp |")
    vr = vol_regime_breakdown(df)
    if not vr.empty:
        lines += ["", "### By volatility regime (vol_ann terciles)", ""]
        lines.append("| vol bin | n | dir acc | base rate | MAE | zero-pred MAE |")
        lines.append("|---------|---|---------|-----------|-----|---------------|")
        for _, r in vr.iterrows():
            lines.append(
                f"| {r['vol_bin']} | {int(r['n'])} | {r['dir_acc'] * 100:.1f}% | "
                f"{r['base_rate'] * 100:.1f}% | {r['mae'] * 100:.2f}pp | "
                f"{r['zero_pred_mae'] * 100:.2f}pp |")
    if "bench_ret_20" in df.columns and df["bench_ret_20"].notna().any():
        up = df[df["bench_ret_20"] > 0]
        dn = df[df["bench_ret_20"] <= 0]
        lines += ["", "### By market regime (home-index 20d return)", ""]
        lines.append("| regime | n | dir acc | base rate |")
        lines.append("|--------|---|---------|-----------|")
        lines.append(f"| up market | {len(up)} | {directional_accuracy(up) * 100:.1f}% | {base_rate_up(up) * 100:.1f}% |")
        lines.append(f"| down market | {len(dn)} | {directional_accuracy(dn) * 100:.1f}% | {base_rate_up(dn) * 100:.1f}% |")

    lines += ["", "> Forecasts are probabilistic hypotheses. Past performance "
                   "does not guarantee future results."]
    return "\n".join(lines)

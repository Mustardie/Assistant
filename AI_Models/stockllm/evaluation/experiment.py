"""V3/V4 experiment: feature-set variants compared on the DEV window only.

Variants (config.EXPERIMENT_VARIANTS):
    A : v2 numeric features only
    B : + fundamentals / earnings / sector context (f_*, s_*)
    C : + news layer (f_news_*)
    D : A + candlestick geometry (f_candle_*)
    E : C + candlestick geometry (full stack)

Every variant runs the same walk-forward engine (scope/target from config)
on the dev window.  For each variant we report:

  * headline metrics (accuracy, MAE, calibration, simulated rule)
  * selectivity tiers: NO SIGNAL / LOW / MEDIUM / HIGH with coverage, accuracy
    vs base rate, and mean returns per tier
  * an indicative portfolio simulation on (a) all trades above the buy
    threshold and (b) HIGH+MEDIUM tier signals only
  * an incremental-effect section: D vs A (candles over numeric) and
    E vs C (candles over the full stack)

The frozen final holdout is never touched here.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

import config
from backtesting.engine import BacktestEngine
from evaluation import metrics, selectivity
from paper_trading.simulator import PaperPortfolio
from utils.logging import get_logger

log = get_logger(__name__)

_BASE_PREFIXES = ("f_", "s_")


def _feature_groups(features: list[str]) -> dict[str, list[str]]:
    candle = [c for c in features if c.startswith("f_candle_")]
    numeric = [c for c in features
               if not c.startswith(_BASE_PREFIXES) and not c.startswith("f_candle_")]
    fund = [c for c in features
            if c.startswith("f_") and not c.startswith("f_news_")
            and c != "f_days_since_news" and c != "f_candle_"]
    news = [c for c in features
            if c.startswith("f_news_") or c == "f_days_since_news"]
    sector = [c for c in features if c.startswith("s_")]
    return {"numeric": numeric, "fund": fund, "news": news,
            "sector": sector, "candle": candle}


def variant_features(features: list[str], variant: str) -> list[str]:
    g = _feature_groups(features)
    base = g["numeric"] + g["sector"]  # sector context ships with every variant
    if variant == "A":
        return base
    if variant == "B":
        return base + g["fund"]
    if variant == "C":
        return base + g["fund"] + g["news"]
    if variant == "D":
        return base + g["candle"]
    if variant == "E":
        return base + g["fund"] + g["news"] + g["candle"]
    raise ValueError(f"unknown variant {variant!r}")


def _extra_metrics(results: pd.DataFrame) -> dict:
    """Trade-focused metrics added in V4 (RMSE, signal stats)."""
    out = {
        "rmse": float(np.sqrt(np.mean(results["abs_error"] ** 2))),
        "n_signals": int((results["prob_up"] >= config.BUY_PROB_THRESHOLD).sum()),
        "n_high": int((results["tier"] == "HIGH").sum()),
    }
    traded = metrics.simulated_trade_returns(results)
    traded = traded[traded != 0]
    out["n_trades"] = int(len(traded))
    if len(traded):
        out["trade_win_rate"] = float((traded > 0).mean())
        out["trade_mean"] = float(traded.mean())
    else:
        out["trade_win_rate"] = np.nan
        out["trade_mean"] = np.nan
    return out


def _tier_table(results: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tier, g in results.groupby("tier", sort=False):
        rows.append({
            "tier": tier,
            "n": len(g),
            "coverage_share": float((g["coverage"] >= config.SELECTIVITY_CAP_COVERAGE).mean()),
            "dir_acc": metrics.directional_accuracy(g),
            "base_rate": metrics.base_rate_up(g),
            "mae": metrics.mae(g),
            "mean_ret": float(g["actual_ret"].mean()),
        })
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NO SIGNAL": 3}
    out = pd.DataFrame(rows).sort_values("tier", key=lambda s: s.map(order))
    return out


def _markdown_table(df: pd.DataFrame, fmt: dict[str, str]) -> str:
    lines = ["| " + " | ".join(df.columns) + " |",
             "|" + "|".join(["---"] * len(df.columns)) + "|"]
    for _, r in df.iterrows():
        cells = []
        for col in df.columns:
            v = r[col]
            if isinstance(v, float):
                v = fmt.get(col, "{:.4f}").format(v)
            cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _paper_summary(signals: pd.DataFrame, prices: dict[str, pd.Series]) -> dict:
    if signals.empty or not prices:
        return {"n": 0}
    portfolio = PaperPortfolio()
    stats = portfolio.run(signals, prices)
    if not stats:
        return {"n": 0}
    return {
        "n": int(stats.get("n_round_trips", 0)),
        "n_trades": int(stats.get("n_round_trips", 0)),
        "win_rate": stats.get("win_rate_pct", 0.0) / 100.0,
        "total_return": stats.get("total_return_pct", 0.0) / 100.0,
        "max_drawdown": stats.get("max_drawdown", 0.0),
    }


def run_variants(matrix: pd.DataFrame,
                 variants: dict | None = None,
                 scope: str = config.EXPERIMENT_SCOPE,
                 target: str = config.EXPERIMENT_TARGET,
                 refit_every: int = config.REFIT_EVERY_DAYS) -> str:
    variants = variants or config.EXPERIMENT_VARIANTS
    from marketdata.features import get_features
    features = get_features()
    from marketdata import loader
    lines = [
        f"# StockLLM v3/v4 experiment -- feature-set variants (dev window only)",
        "",
        f"> Generated {datetime.now():%Y-%m-%d %H:%M} | scope={scope}, target={target} | "
        f"window {config.DEV_START}..{config.DEV_END}",
        "",
        "> The frozen final holdout is NOT used for any decision in this report.",
        "",
        "## Evidence availability per ticker (whole matrix)",
        "",
    ]
    avail_cols = [c for c in ("fund_avail_ev", "news_avail_ev", "sector_avail_ev",
                              "candle_avail_ev") if c in matrix.columns]
    avail = matrix.groupby("ticker")[avail_cols].mean()
    avail_renamed = avail.reset_index().rename(columns={
        "fund_avail_ev": "fundamentals", "news_avail_ev": "news",
        "sector_avail_ev": "sector", "candle_avail_ev": "candles"})
    lines.append(_markdown_table(avail_renamed,
                                 {c: "{:.2f}" for c in avail_renamed.columns
                                  if c != "ticker"}))

    results_by_variant: dict[str, pd.DataFrame] = {}
    extras: dict[str, dict] = {}
    for variant, spec in variants.items():
        log.info("running variant %s (%s)", variant, spec["name"])
        vfeats = variant_features(features, variant)
        engine = BacktestEngine(matrix, window="dev", scope=scope, target=target,
                                refit_every=refit_every, features_override=vfeats)
        results = engine.run()
        results = selectivity.apply_tiers(results)
        results_by_variant[variant] = results
        extras[variant] = _extra_metrics(results)

        prices = {}
        frames, _ = loader.load_market_data(sorted(results["ticker"].unique()))
        prices = {t: f["close"] for t, f in frames.items()}
        all_trades = results[results["prob_up"] >= config.BUY_PROB_THRESHOLD]
        hi_sig = results[results["tier"].isin(["HIGH", "MEDIUM"])]
        paper_all = _paper_summary(all_trades, prices)
        paper_tier = _paper_summary(hi_sig, prices)

        lines += [
            "",
            f"## Variant {variant}: {spec['name']}",
            "",
            f"- Features: {len(vfeats)} -- `{'`, `'.join(vfeats[:8])}`"
            f"{', ...' if len(vfeats) > 8 else ''}",
            f"- Forecasts: {len(results)}",
            f"- Directional accuracy: {metrics.directional_accuracy(results) * 100:.1f}% "
            f"(base rate {metrics.base_rate_up(results) * 100:.1f}%)",
            f"- MAE: {metrics.mae(results) * 100:.2f}pp | RMSE: {extras[variant]['rmse'] * 100:.2f}pp "
            f"| Brier: {metrics.brier_score(results):.4f} "
            f"| Calibration error: {metrics.calibration_error(results):.4f}",
            f"- Mean predicted: {results['pred_ret'].mean() * 100:+.2f}% | "
            f"Mean actual: {results['actual_ret'].mean() * 100:+.2f}%",
            f"- Trade stats: {extras[variant]['n_signals']} signals at "
            f"P(up)>={config.BUY_PROB_THRESHOLD} | {extras[variant]['n_trades']} trades "
            f"| win rate {extras[variant]['trade_win_rate'] * 100:.0f}% "
            f"(mean {extras[variant]['trade_mean'] * 100:+.2f}%)"
            if np.isfinite(extras[variant]["trade_win_rate"]) else
            f"- Trade stats: {extras[variant]['n_signals']} signals at "
            f"P(up)>={config.BUY_PROB_THRESHOLD} | no trades",
            "",
            "### Selectivity tiers (evidence coverage + edge gating)",
            "",
        ]
        tier_table = _tier_table(results)
        lines.append(_markdown_table(tier_table, {
            "n": "{:.0f}", "coverage_share": "{:.2f}", "dir_acc": "{:.3f}",
            "base_rate": "{:.3f}", "mae": "{:.4f}", "mean_ret": "{:+.4f}"}))
        lines += [
            "",
            "### Indicative portfolio simulation (NOT a trading recommendation)",
            "",
            f"- All signals P(up) >= {config.BUY_PROB_THRESHOLD}: {_fmt_paper(paper_all)}",
            f"- HIGH+MEDIUM tier only: {_fmt_paper(paper_tier)}",
            "",
        ]

    lines += ["", "## Cross-variant summary", "",
              "| variant | dir acc | base rate | brier | cal err | rmse(pp) | trades(0.55) | win rate | tier-sim return |",
              "|---------|---------|-----------|-------|---------|----------|--------------|----------|-----------------|"]
    for variant, results in results_by_variant.items():
        traded = metrics.simulated_trade_returns(results)
        traded = traded[traded != 0]
        frames, _ = loader.load_market_data(sorted(results["ticker"].unique()))
        prices = {t: f["close"] for t, f in frames.items()}
        sim = _paper_summary(results[results["prob_up"] >= config.BUY_PROB_THRESHOLD], prices)
        lines.append(
            f"| {variant} | {metrics.directional_accuracy(results) * 100:.1f}% | "
            f"{metrics.base_rate_up(results) * 100:.1f}% | "
            f"{metrics.brier_score(results):.4f} | {metrics.calibration_error(results):.4f} | "
            f"{extras[variant]['rmse'] * 100:.2f} | {len(traded)} | "
            f"{(traded > 0).mean() * 100:.1f}% | "
            f"{sim['total_return'] * 100:+.1f}% |")

    if "D" in results_by_variant or "E" in results_by_variant:
        lines += ["", "## Candlestick layer: incremental effect", "",
                  "| pair | delta dir acc (pp) | delta MAE (pp) | delta Brier | delta trades | delta win rate (pp) |",
                  "|------|--------------------|----------------|-------------|--------------|---------------------|"]
        for base, plus in (("A", "D"), ("C", "E")):
            if base not in results_by_variant or plus not in results_by_variant:
                continue
            r_base, r_plus = results_by_variant[base], results_by_variant[plus]
            traded_b = metrics.simulated_trade_returns(r_base)
            traded_b = traded_b[traded_b != 0]
            traded_p = metrics.simulated_trade_returns(r_plus)
            traded_p = traded_p[traded_p != 0]
            lines.append(
                f"| {base} -> {plus} "
                f"| {(metrics.directional_accuracy(r_plus) - metrics.directional_accuracy(r_base)) * 100:+.2f} | "
                f"{(metrics.mae(r_plus) - metrics.mae(r_base)) * 100:+.2f} | "
                f"{metrics.brier_score(r_plus) - metrics.brier_score(r_base):+.4f} | "
                f"{extras[plus]['n_trades'] - extras[base]['n_trades']:+d} | "
                f"{(np.nanmean(traded_p > 0) - np.nanmean(traded_b > 0)) * 100:+.2f} |")
    lines += [
        "",
        "> Tiers gate by *evidence* (fundamentals/news/sector available at that date) "
        "and *edge* (|P(up)-0.5|). NO SIGNAL rows are not tradable signals by design.",
        "",
        "> Simulated strategies assume each qualifying forecast is held for the horizon "
        "with transaction costs -- indicative only, not a claim about real outcomes.",
    ]
    return "\n".join(lines)


def _fmt_paper(stats: dict) -> str:
    if stats.get("n") == 0:
        return "no trades"
    return (f"round trips={stats['n_trades']}, win rate {stats['win_rate'] * 100:.0f}%, "
            f"total {stats['total_return'] * 100:+.1f}%, "
            f"max DD {stats['max_drawdown'] * 100:.1f}%")

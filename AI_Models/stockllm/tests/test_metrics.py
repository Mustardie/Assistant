"""Metric correctness on known-value inputs."""
import numpy as np
import pandas as pd
import pytest

from evaluation.metrics import (base_rate_up, benchmark_comparison, bias,
                                brier_score, breakdown, calibration_error,
                                calibration_table, directional_accuracy, mae,
                                precision_positive, recall_positive,
                                return_stats, simulated_trade_returns,
                                vol_regime_breakdown)


def test_directional_accuracy():
    df = pd.DataFrame({"direction_correct": [True, False, True, True]})
    assert directional_accuracy(df) == pytest.approx(0.75)


def test_mae_rmse_bias():
    df = pd.DataFrame({"pred_ret": [0.10, -0.20, 0.05], "actual_ret": [0.20, -0.10, 0.05],
                       "prob_up": [0.5, 0.5, 0.5]})
    assert mae(df) == pytest.approx(0.10 / 3 * 2)
    assert bias(df) == pytest.approx(-0.20 / 3)
    assert brier_score(df) == pytest.approx((0.25 + 0.25 + 0.25) / 3)


def test_precision_recall():
    df = pd.DataFrame({
        "prob_up": [0.9, 0.8, 0.4, 0.3, 0.7],
        "actual_ret": [0.05, -0.02, 0.03, -0.01, 0.02],
    })
    assert precision_positive(df) == pytest.approx(2 / 3)
    assert recall_positive(df) == pytest.approx(2 / 3)


def test_calibration_perfect_is_low_error():
    rng = np.random.default_rng(0)
    probs = np.linspace(0.05, 0.95, 1000)
    y = (rng.random(1000) < probs).astype(int)
    df = pd.DataFrame({"prob_up": probs, "actual_ret": y * 0.05 - 0.01})
    assert calibration_error(df, bins=10) < 0.2
    table = calibration_table(df, bins=10)
    assert len(table) == 10


def test_return_stats_known_series():
    stats = return_stats([0.02] * 50)
    assert stats["total"] == pytest.approx(1.02 ** 50 - 1, rel=1e-6)
    assert stats["max_drawdown"] == pytest.approx(0.0)
    assert stats["sharpe"] == 0.0  # zero variance -> no meaningful sharpe
    stats2 = return_stats(np.array([0.05, -0.05] * 20))
    assert stats2["max_drawdown"] < 0
    assert stats2["mean"] == pytest.approx(0.0)


def test_base_rate_and_benchmark():
    df = pd.DataFrame({
        "direction_correct": [True, False, False, True],
        "pred_ret": [0.1, -0.1, 0.05, -0.05],
        "actual_ret": [0.1, -0.2, 0.1, 0.05],
        "prob_up": [0.8, 0.2, 0.7, 0.3],
    })
    assert base_rate_up(df) == pytest.approx(0.75)
    bench = benchmark_comparison(df)
    assert bench["directional_accuracy"] == pytest.approx(0.5)
    assert bench["model_mae"] == pytest.approx((0.0 + 0.1 + 0.05 + 0.10) / 4)


def test_simulated_trade_returns():
    df = pd.DataFrame({"prob_up": [0.9, 0.4], "actual_ret": [0.02, 0.03]})
    out = simulated_trade_returns(df, buy_threshold=0.55, costs_per_trade=0.002)
    assert out.iloc[0] == pytest.approx(0.018)
    assert out.iloc[1] == pytest.approx(0.0)


def test_momentum_and_index_baselines():
    df = pd.DataFrame({
        "direction_correct": [True, True, True, True],
        "pred_ret": [0.0, 0.0, 0.0, 0.0],
        "actual_ret": [0.05, 0.04, -0.02, -0.03],
        "prob_up": [0.6, 0.6, 0.4, 0.4],
        "prior_20d": [0.02, 0.01, -0.03, 0.02],
        "bench_fwd_7": [0.01, 0.00, -0.01, -0.01],
        "bench_ret_20": [0.01, 0.01, -0.01, -0.01],
    })
    bench = benchmark_comparison(df)
    # momentum baseline accuracy: rows 0,1,2 match sign, row 3 mismatches -> 0.75
    assert bench["momentum_accuracy"] == pytest.approx(0.75)
    # index-follow MAE
    expected = (abs(0.01 - 0.05) + abs(0.0 - 0.04) + abs(-0.01 + 0.02) + abs(-0.01 + 0.03)) / 4
    assert bench["index_follow_mae"] == pytest.approx(expected)
    assert "dir_acc_up_market" in bench and "dir_acc_down_market" in bench


def test_breakdowns_group_by():
    rng = np.random.default_rng(0)
    n = 300
    df = pd.DataFrame({
        "ticker": np.random.choice(["A", "B", "C"], n),
        "year": np.random.choice([2023, 2024], n),
        "date": pd.date_range("2023-01-01", periods=n, freq="B"),
        "actual_ret": rng.normal(0, 0.02, n),
        "pred_ret": rng.normal(0, 0.02, n),
        "prob_up": rng.random(n),
        "direction_correct": rng.random(n) > 0.5,
        "vol_ann": rng.uniform(0.1, 0.8, n),
        "bench_ret_20": rng.normal(0, 0.02, n),
    })
    bt = breakdown(df, "ticker")
    assert set(bt["ticker"]) == {"A", "B", "C"}
    assert "base_rate" in bt.columns and "mae" in bt.columns
    vr = vol_regime_breakdown(df, n_bins=3)
    assert len(vr) == 3
    yr = df.copy()
    yr["year"] = pd.to_datetime(yr["date"]).dt.year
    by_year = breakdown(yr, "year")
    assert set(by_year["year"]) == {2023, 2024}

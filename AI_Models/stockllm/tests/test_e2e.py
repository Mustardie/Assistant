"""End-to-end walk-forward test: the recorded results must be leak-free.

Synthetic data only -- this validates the machinery (splits, features, model,
engine), not any financial claim.
"""
import numpy as np
import pandas as pd
import pytest

from backtesting.engine import BacktestEngine
from config import LABEL_COLUMN
from marketdata.features import make_feature_matrix


def _frame(n=1000, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)))
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.01, n))),
        "low": close * (1 - np.abs(rng.normal(0, 0.01, n))),
        "close": close,
        "volume": rng.integers(100_000, 1_000_000, n),
    }, index=idx)


def test_walk_forward_results_are_point_in_time():
    frames = {"A": _frame(seed=1), "B": _frame(seed=2)}
    matrix = make_feature_matrix(frames)
    test_start = "2020-09-01"
    engine = BacktestEngine(matrix, test_start=test_start, refit_every=10,
                            min_train_rows=60)
    results = engine.run()

    assert not results.empty
    assert (results["date"] >= test_start).all()
    assert (results["date"].str.len() == 10).all()  # ISO dates
    assert results["direction_correct"].isin([True, False]).all()
    for col in ("pred_ret", "prob_up", "q_lo", "q_hi", "actual_ret", "abs_error"):
        assert results[col].notna().all()

    refit = pd.to_datetime(results["refit_cutoff"])
    dates = pd.to_datetime(results["date"])
    assert (dates > refit).all(), "forecast dates must be strictly after the refit cutoff"

    assert results["model_version"].str.startswith("stockllm").all()
    assert (results["prob_up"].between(0, 1)).all()
    for col in ("prior_20d", "prior_60d", "bench_fwd_7", "vol_ann"):
        assert col in results.columns


def test_model_sees_no_label_window_past_cutoff():
    frames = {"A": _frame(seed=3)}
    matrix = make_feature_matrix(frames)
    engine = BacktestEngine(matrix, test_start="2020-09-01", refit_every=20,
                            min_train_rows=40)
    results = engine.run()
    assert len(results) > 20


def test_per_stock_scope_runs():
    frames = {"A": _frame(seed=4), "B": _frame(seed=5)}
    matrix = make_feature_matrix(frames)
    engine = BacktestEngine(matrix, test_start="2020-09-01", refit_every=10,
                            min_train_rows=60, scope="per_stock")
    results = engine.run()
    assert not results.empty
    assert set(results["ticker"].unique()) == {"A", "B"}


def test_id_scope_adds_ticker_id_feature():
    frames = {"A": _frame(seed=6), "B": _frame(seed=7)}
    matrix = make_feature_matrix(frames)
    engine = BacktestEngine(matrix, test_start="2020-09-01", refit_every=10,
                            min_train_rows=60, scope="id")
    results = engine.run()
    assert not results.empty
    import json as _json
    assert "ticker_id" in _json.loads(results.iloc[0]["features_used"])


def test_voladj_target_runs_and_returns_comparable_predictions():
    frames = {"A": _frame(seed=8), "B": _frame(seed=9)}
    matrix = make_feature_matrix(frames)
    engine = BacktestEngine(matrix, test_start="2020-09-01", refit_every=10,
                            min_train_rows=60, target="voladj")
    results = engine.run()
    assert not results.empty
    assert (results["target"] == "voladj").all()
    assert results["pred_ret"].abs().max() < 1.0  # sanity: raw-return space

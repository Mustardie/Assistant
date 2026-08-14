"""Regression tests for the V3 experiment zero-forecasts failure.

The V3 experiment died with a `KeyError: 'prob_up'` because variants B/C
fit on 0 training rows: an all-NaN feature column (f_debt_to_equity, which
was undefined whenever an EDGAR ticker reported only one debt component)
zeroed out the whole training set through `dropna`.  These tests lock in
the three-layer fix:

  1. the engine drops degenerate (all-NaN) feature columns per training
     slice instead of letting them zero out the fit;
  2. the engine raises a ValueError with a diagnostic instead of returning
     an empty frame when every refit is skipped;
  3. the fetcher derives `debt` from whichever of debt_cur/debt_lt exists,
     and `f_days_since_news` is grouped as news (not fundamentals).
"""
import numpy as np
import pandas as pd
import pytest

from backtesting.engine import BacktestEngine
from evaluation.experiment import _feature_groups, variant_features
from fundamentals import fetcher
from marketdata.features import get_features, make_feature_matrix


def _price_frame(n=1400, seed=1, start="2018-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.01, n))),
        "low": close * (1 - np.abs(rng.normal(0, 0.01, n))),
        "close": close,
        "volume": rng.integers(100_000, 1_000_000, n),
    }, index=idx)


def _layer_frames(n=120, start="2018-01-15", all_nan=False):
    idx = pd.bdate_range(start, periods=n)
    frame = pd.DataFrame({
        "f_report_avail": 1.0,
        "f_net_margin": np.linspace(0.05, 0.2, n),
    }, index=idx)
    if all_nan:
        # degenerate column: present but NaN on every row (the historical bug)
        frame["f_debt_to_equity"] = np.nan
    return frame


def _matrix(price_n=1400, all_nan=False, start="2018-01-01"):
    frames = {"A": _price_frame(price_n, seed=1, start=start),
              "B": _price_frame(price_n, seed=2, start=start)}
    fund = {t: _layer_frames(all_nan=all_nan) for t in frames}
    news = {t: pd.DataFrame({"f_news_avail": 1.0},
                            index=pd.bdate_range("2018-01-15", periods=120))
            for t in frames}
    return make_feature_matrix(frames, fundamentals=fund, news=news)


def test_engine_ignores_all_nan_feature():
    matrix = _matrix(all_nan=True)
    groups = _feature_groups(get_features())
    vfeats = variant_features(get_features(), "B")  # numeric + sector + fund
    assert "f_debt_to_equity" in vfeats  # the degenerate column is requested

    engine = BacktestEngine(matrix, window="dev", scope="id", target="raw",
                            refit_every=60)
    results = engine.run()
    assert len(results) > 0  # must not collapse to 0 train rows -> 0 forecasts
    assert "prob_up" in results.columns


def test_engine_empty_results_raise_diagnostic():
    # too little history for even one refit -> explicit error, not a KeyError
    matrix = _matrix(price_n=150, start="2023-06-01")
    engine = BacktestEngine(matrix, window="dev", scope="id", target="raw",
                            min_train_rows=400)
    with pytest.raises(ValueError, match="0 forecasts"):
        engine.run()


def _edgar_wide(n=4, debt_col="debt_lt"):
    """facts_pivot-style wide frame with at least 4 statement rows."""
    ends = pd.bdate_range("2019-09-30", periods=n, freq="QE")
    avail = ends + pd.Timedelta(days=35)
    data = {
        "revenue": np.linspace(100.0, 150.0, n),
        "net_income": np.linspace(10.0, 15.0, n),
        "cash": np.full(n, 10.0),
        "equity": np.full(n, 200.0),
        "avail_date": avail,
    }
    data[debt_col] = np.linspace(50.0, 60.0, n)
    return pd.DataFrame(data, index=ends)


def test_statements_for_combines_single_debt_component(monkeypatch):
    # EDGAR tickers report current *or* non-current debt; either alone must
    # still produce the `debt` column (was: required both, got neither).
    monkeypatch.setattr("fundamentals.fetcher._cik_for_ticker", lambda t: 1)
    monkeypatch.setattr("fundamentals.edgar_facts.facts_pivot",
                        lambda cik: _edgar_wide(debt_col="debt_lt"))

    stmts = fetcher._statements_for("AAPL", None)
    assert "debt" in stmts.columns
    assert np.allclose(stmts["debt"].tolist(),
                       [50.0, 160.0 / 3, 170.0 / 3, 60.0])
    assert "debt_cur" not in stmts.columns
    assert "debt_lt" not in stmts.columns


def test_statements_for_missing_debt_components(monkeypatch):
    wide = _edgar_wide().drop(columns=["debt_lt"])
    monkeypatch.setattr("fundamentals.fetcher._cik_for_ticker", lambda t: 1)
    monkeypatch.setattr("fundamentals.edgar_facts.facts_pivot", lambda cik: wide)

    stmts = fetcher._statements_for("AAPL", None)
    assert "debt" not in stmts.columns  # no fabrication
    assert not stmts.empty


def _patch_statement_source(monkeypatch, blob):
    monkeypatch.setattr("fundamentals.fetcher.load_fundamentals",
                        lambda t: {"statements": blob})
    monkeypatch.setattr("fundamentals.fetcher.fetch_edgar_filing_dates",
                        lambda t: pd.DataFrame())


def test_pit_statements_combines_debt_read_side(monkeypatch):
    # cached statements (already written) with only debt_lt must still yield
    # the debt column at read time, so existing caches heal without a refetch
    blob = {"index": ["2020-03-31", "2020-06-30"],
            "columns": ["revenue", "debt_lt", "cash", "equity", "avail_date"],
            "values": [[100.0, 50.0, 10.0, 200.0, "2020-05-01"],
                       [110.0, 55.0, 10.0, 200.0, "2020-08-01"]]}
    _patch_statement_source(monkeypatch, blob)
    stmts = fetcher.pit_statements("AAPL")
    assert "debt" in stmts.columns
    assert stmts["debt"].tolist() == [50.0, 55.0]
    assert "debt_lt" not in stmts.columns


def test_pit_statements_combines_both_debt_components(monkeypatch):
    blob = {"index": ["2020-03-31"],
            "columns": ["debt_cur", "debt_lt", "avail_date"],
            "values": [[30.0, 50.0, "2020-05-01"]]}
    _patch_statement_source(monkeypatch, blob)
    stmts = fetcher.pit_statements("AAPL")
    assert stmts["debt"].tolist() == [80.0]


def test_variant_groups_news_days_since():
    features = ["x", "f_net_margin", "f_news_7d", "f_days_since_news", "s_ret"]
    g = _feature_groups(features)
    assert "f_days_since_news" in g["news"]
    assert "f_days_since_news" not in g["fund"]
    assert g["fund"] == ["f_net_margin"]
    # variant B must not smuggle news-derived information
    assert "f_days_since_news" not in variant_features(features, "B")
    assert "f_days_since_news" in variant_features(features, "C")

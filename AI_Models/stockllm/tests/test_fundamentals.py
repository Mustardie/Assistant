"""Regression tests for point-in-time fundamentals robustness.

These cover the V3 experiment crash: `fundamental_features` assumed a
`period_end` column while `pit_statements` returns it as the index, the
cached earnings event dates carry mixed timezone offsets (yfinance DST vs
standard time), and Indian statement caches contain duplicate column labels.
"""
import numpy as np
import pandas as pd
import pytest

from fundamentals import fetcher
from fundamentals.fetcher import (earnings_features, fundamental_features,
                                  pit_earnings)
from marketdata.features import _pit_merge, make_feature_matrix
from research.layers import load_layers


def _stmt_frame():
    """A pit_statements-style frame: period_end is the index, avail_date a col."""
    idx = pd.to_datetime(["2020-03-31", "2020-06-30", "2020-09-30",
                          "2020-12-31", "2021-03-31"])
    avail = pd.to_datetime(["2020-05-01", "2020-08-01", "2020-11-01",
                            "2021-02-01", "2021-05-01"])
    return pd.DataFrame({
        "revenue": [100.0, 110.0, 120.0, 130.0, 150.0],
        "gross_profit": [60.0, 66.0, 72.0, 78.0, 90.0],
        "net_income": [10.0, 11.0, 12.0, 13.0, 15.0],
        "fcf": [5.0, 6.0, 7.0, 8.0, 9.0],
        "debt": [200.0, 200.0, 200.0, 200.0, 200.0],
        "cash": [50.0, 50.0, 50.0, 50.0, 50.0],
        "equity": [400.0, 400.0, 400.0, 400.0, 400.0],
        "assets": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "avail_date": avail,
    }, index=idx)


def _price_frame(n=300, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.01, n))),
        "low": close * (1 - np.abs(rng.normal(0, 0.01, n))),
        "close": close,
        "volume": rng.integers(100_000, 1_000_000, n),
    }, index=idx)


def test_fundamental_features_empty_fundamentals(monkeypatch):
    monkeypatch.setattr("fundamentals.fetcher.pit_statements",
                        lambda t: pd.DataFrame())
    out = fundamental_features("A")
    assert out.empty
    assert list(out.columns) == ["f_report_avail"]


def test_fundamental_features_missing_period_end(monkeypatch):
    # malformed frame: RangeIndex and no period_end column anywhere
    bad = pd.DataFrame({"revenue": [1.0, 2.0]})
    monkeypatch.setattr("fundamentals.fetcher.pit_statements", lambda t: bad)
    out = fundamental_features("A")
    assert out.empty
    assert list(out.columns) == ["f_report_avail"]


def test_fundamental_features_avail_date_missing(monkeypatch):
    frame = _stmt_frame().drop(columns=["avail_date"])
    monkeypatch.setattr("fundamentals.fetcher.pit_statements", lambda t: frame)
    out = fundamental_features("A")
    assert out.empty
    assert list(out.columns) == ["f_report_avail"]


def test_fundamental_features_valid(monkeypatch):
    monkeypatch.setattr("fundamentals.fetcher.pit_statements",
                        lambda t: _stmt_frame())
    out = fundamental_features("A")
    assert not out.empty
    # index re-keyed onto availability dates (period_end -> avail_date 1:1)
    assert list(out.index) == pd.to_datetime(
        ["2020-05-01", "2020-08-01", "2020-11-01", "2021-02-01", "2021-05-01"]).tolist()
    assert (out["f_report_avail"] == 1.0).all()
    # ratios must be real values, not label-misaligned NaN
    assert np.allclose(out["f_net_margin"].dropna(), [0.1, 0.1, 0.1, 0.1, 0.1])
    assert np.allclose(out["f_gross_margin"].dropna(), [0.6, 0.6, 0.6, 0.6, 0.6])
    assert np.allclose(out["f_debt_to_equity"].dropna(), [0.375] * 5)
    assert out["f_rev_growth_yoy"].notna().any()  # last row has 4 prior periods


def test_fundamental_features_duplicate_columns_tolerated(monkeypatch):
    frame = _stmt_frame()
    # Indian caches carry duplicated metric columns (yearly + quarterly concat)
    dup = frame.copy()
    dup["revenue"] = dup["revenue"]  # now 'revenue' appears twice
    monkeypatch.setattr("fundamentals.fetcher.pit_statements", lambda t: dup)
    out = fundamental_features("A")
    assert not out.empty
    assert out["f_net_margin"].notna().any()


def test_pit_earnings_mixed_timezones(monkeypatch):
    blob = {
        "index": ["2023-01-24 00:00:00-05:00", "2023-06-29 00:00:00-04:00",
                  "2023-10-25 00:00:00-04:00"],
        "columns": ["est_eps", "reported_eps", "surprise_pct"],
        "values": [[1.0, 2.0, 5.0], [1.0, 2.1, 10.0], [1.0, 1.9, -5.0]],
    }
    monkeypatch.setattr("fundamentals.fetcher.load_fundamentals",
                        lambda t: {"earnings": blob})
    ev = pit_earnings("A")
    assert len(ev) == 3
    assert getattr(ev.index, "tz", None) is None  # naive, no mixed-tz crash
    assert ev.index.min().normalize() == pd.Timestamp("2023-01-24")
    out = earnings_features("A")
    assert len(out) == 3
    assert (out["f_earnings_avail"] == 1.0).all()
    assert np.isclose(out["f_surprise_mean_4"].iloc[1], 7.5)


def test_pit_merge_tolerates_datetime_unit_mismatch():
    feats = pd.DataFrame({"close": [1.0, 2.0]},
                         index=pd.to_datetime(["2020-05-02", "2020-06-01"]))
    # pit frame indexed with a different resolution (e.g. M8[us]) must still merge
    pit = pd.DataFrame({"f_report_avail": [1.0]},
                       index=pd.to_datetime(["2020-05-01"]).as_unit("us"))
    out = _pit_merge(feats, pit)
    assert out is not None
    assert out["f_report_avail"].tolist() == [1.0, 1.0]


def test_experiment_with_incomplete_fundamentals(monkeypatch):
    avail = pd.to_datetime(["2018-01-15", "2018-04-15", "2018-07-15",
                            "2018-10-15", "2019-01-15"])
    fund_a = pd.DataFrame({"f_report_avail": [1.0] * len(avail)}, index=avail)
    news_a = pd.DataFrame({"f_news_avail": [1.0, 1.0]},
                          index=pd.to_datetime(["2018-01-15", "2018-04-15"]))
    earn_a = pd.DataFrame({"f_earnings_avail": [1.0]},
                          index=pd.to_datetime(["2018-01-15"]))

    monkeypatch.setattr("research.layers.fundamental_features",
                        lambda t: fund_a if t == "A" else pd.DataFrame())
    monkeypatch.setattr("research.layers.earnings_features",
                        lambda t: earn_a if t == "A" else pd.DataFrame())
    monkeypatch.setattr("research.layers.news_features",
                        lambda t: news_a if t == "A" else pd.DataFrame())
    monkeypatch.setattr("research.layers.sector_mod.load_sectors",
                        lambda: {})

    fund, news, sectors = load_layers(["A", "B"])
    assert not fund["A"].empty
    assert fund["B"].empty
    assert news["B"].empty

    frames = {"A": _price_frame(seed=1), "B": _price_frame(seed=2)}
    matrix = make_feature_matrix(frames, fundamentals=fund, news=news,
                                 sectors=sectors)
    a = matrix[matrix["ticker"] == "A"]
    b = matrix[matrix["ticker"] == "B"]
    assert (a["fund_avail_ev"] > 0).any()
    assert (b["fund_avail_ev"] == 0).all()  # no fundamentals -> zero evidence
    assert "f_report_avail" in a.columns

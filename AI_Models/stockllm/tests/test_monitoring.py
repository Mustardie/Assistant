"""Monitoring: ledger dedupe/resolution, watchlists, intent parsing (V4)."""
import json

import numpy as np
import pandas as pd
import pytest

from jarvis.intents import (IntentError, describe, parse_request, resolve_ticker)
from monitoring.ledger import PredictionLedger
from monitoring.service import (add_to_watchlist, load_monitor_watchlist,
                                remove_from_watchlist, save_monitor_watchlist)


@pytest.fixture
def ledger(tmp_path):
    db = tmp_path / "ledger.sqlite"
    led = PredictionLedger(db)
    yield led
    led.close()


def _prices(ticker="AAA", n=60, base=100.0, drift=0.0005):
    idx = pd.bdate_range("2024-01-01", periods=n)
    rng = np.random.default_rng(1)
    close = base * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    return pd.Series(close, index=idx, name="close")


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def test_ledger_add_and_dedupe(ledger):
    assert ledger.add("AAA", "2024-01-10", 7, 100.0, prob_up=0.6,
                      expected_return=0.02, direction="UP")
    # identical (ticker, as_of_date, horizon) is a duplicate -> rejected
    assert not ledger.add("AAA", "2024-01-10", 7, 100.0, prob_up=0.6,
                          expected_return=0.02, direction="UP")
    # same ticker/date with a different horizon is a NEW prediction
    assert ledger.add("AAA", "2024-01-10", 3, 100.0, prob_up=0.6,
                      expected_return=0.01, direction="UP")
    assert ledger.stats()["total"] == 2
    assert ledger.stats()["open"] == 2


def test_ledger_resolve_due(ledger):
    prices = _prices()
    as_of = "2024-01-10"  # index position 7
    assert ledger.add("AAA", as_of, 7, float(prices.loc[as_of]),
                      prob_up=0.6, expected_return=0.0, direction="UP")
    # not resolvable yet: horizon window not complete
    assert ledger.resolve_due({"AAA": prices.iloc[: 7 + 7]}) == 0
    # resolvable once 7 sessions after as_of exist
    assert ledger.resolve_due({"AAA": prices.iloc[: 7 + 7 + 1]}) == 1
    rec = ledger.all()[0]
    expected = prices.iloc[7 + 7] / prices.iloc[7] - 1.0
    assert rec["outcome_ret"] == pytest.approx(expected)
    assert rec["outcome_direction_correct"] in (0, 1)
    assert rec["outcome_direction_correct"] == int(expected > 0)
    assert ledger.stats()["resolved"] == 1


def test_ledger_does_not_resolve_future_or_unknown(ledger):
    assert ledger.add("AAA", "2024-01-10", 7, 100.0, prob_up=0.6,
                      expected_return=0.0, direction="UP")
    assert ledger.add("UNKNOWN", "2024-01-10", 7, 100.0, prob_up=0.6,
                      expected_return=0.0, direction="UP")
    assert ledger.resolve_due({}) == 0
    assert ledger.resolve_due({"AAA": _prices()}) == 1  # UNKNOWN has no prices


# ---------------------------------------------------------------------------
# monitoring watchlist
# ---------------------------------------------------------------------------

def test_watchlist_roundtrip(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "MONITOR_WATCHLIST_FILE",
                        tmp_path / "monitor.json")
    entries = load_monitor_watchlist()
    assert entries == []
    add_to_watchlist(entries, "nvda", interval_min=5, horizon="3d")
    add_to_watchlist(entries, "tcs", interval_min=30)
    # bare "tcs" resolves to the canonical symbol TCS.NS via the alias table
    assert [e["ticker"] for e in entries] == ["NVDA", "TCS.NS"]
    # re-adding updates in place (no duplicates)
    add_to_watchlist(entries, "nvda", interval_min=10, horizon="14d")
    nvda = [e for e in entries if e["ticker"] == "NVDA"][0]
    assert nvda["interval_min"] == 10 and nvda["horizon"] == "14d"
    assert len(entries) == 2
    assert remove_from_watchlist(entries, "nvda")
    assert not remove_from_watchlist(entries, "nvda")
    assert [e["ticker"] for e in entries] == ["TCS.NS"]
    # bare common names resolve too ("reliance" -> RELIANCE.NS)
    entry = add_to_watchlist(load_monitor_watchlist(), "reliance",
                             interval_min=0, horizon="1 month")
    assert entry["ticker"] == "RELIANCE.NS"
    assert entry["interval_min"] == 1
    assert entry["horizon"] == "21d"


# ---------------------------------------------------------------------------
# intent parsing
# ---------------------------------------------------------------------------

def test_intent_predict():
    intent = parse_request("what do you think about NVIDIA")
    assert intent.action == "predict"
    assert "NVDA" in intent.tickers
    assert intent.horizon is None

    intent = parse_request("forecast TCS for 2 weeks")
    assert intent.action == "predict"
    assert intent.tickers == ["TCS.NS"]
    assert intent.horizon == "10d"  # 2 trading weeks = 10 sessions
    assert intent.horizon_days == 10

    intent = parse_request("predict RELIANCE for next month")
    assert intent.tickers == ["RELIANCE.NS"]
    assert intent.horizon == "21d"

    intent = parse_request("will AAPL go up")
    assert intent.action == "predict"
    assert intent.tickers == ["AAPL"]


def test_intent_track_and_untrack():
    intent = parse_request("track RELIANCE every 15 minutes")
    assert intent.action == "track"
    assert intent.tickers == ["RELIANCE.NS"]
    assert intent.interval_min == 15

    intent = parse_request("monitor NVIDIA for 3 days")
    assert intent.action == "track"
    assert intent.tickers == ["NVDA"]
    assert intent.horizon == "3d"
    assert intent.interval_min is not None  # default interval applied

    intent = parse_request("stop tracking AAPL")
    assert intent.action == "untrack"
    assert intent.tickers == ["AAPL"]


def test_intent_watchlist_and_tracking():
    assert parse_request("what am I tracking").action == "watchlist"
    assert parse_request("show my watchlist").action == "watchlist"
    assert parse_request("how are my predictions doing").action == "tracking"


def test_intent_ticker_aliases():
    assert resolve_ticker("infosys") == "INFY.NS"
    assert resolve_ticker("HDFC Bank") == "HDFCBANK.NS"
    assert resolve_ticker("reliance") == "RELIANCE.NS"
    assert resolve_ticker("NVDA") == "NVDA"
    assert resolve_ticker("FOO.BAR") == "FOO.BAR"
    assert resolve_ticker("banana") is None


def test_intent_errors():
    with pytest.raises(IntentError):
        parse_request("")
    with pytest.raises(IntentError):
        parse_request("stop tracking")  # no ticker to untrack


def test_describe():
    intent = parse_request("track nvidia every 10 min")
    assert "track NVDA every 10 min" in describe(intent)
    intent = parse_request("forecast TCS")
    assert "TCS.NS" in describe(intent)

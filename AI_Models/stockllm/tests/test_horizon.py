"""Horizon parsing and canonicalization (V4)."""
import pytest

from marketdata.horizon import (DEFAULT_HORIZON, Horizon,
                                canonical_trading_days, parse_interval_minutes)


def test_parse_number_phrases():
    assert Horizon.parse("3 days").trading_days == 3
    assert Horizon.parse("3d").trading_days == 3
    assert Horizon.parse("14 days").trading_days == 14
    assert Horizon.parse("2 weeks").trading_days == 10
    assert Horizon.parse("1 month").trading_days == 21
    assert Horizon.parse("1 quarter").trading_days == 63
    assert Horizon.parse("1w").trading_days == 5
    assert Horizon.parse("3mo").trading_days == 63


def test_parse_phrases():
    assert Horizon.parse("next week").trading_days == 5
    assert Horizon.parse("this month").trading_days == 21
    assert Horizon.parse("next month").trading_days == 21
    assert Horizon.parse("next quarter").trading_days == 63
    assert Horizon.parse("tomorrow").trading_days == 1
    assert Horizon.parse("today").trading_days == 1


def test_parse_snaps_to_canonical_grid():
    # 8 trading days snaps to the closest canonical horizon (7)
    assert Horizon.parse("8 days").trading_days == 7
    # 2 days sits between 1 and 3; ties prefer the longer horizon
    assert Horizon.parse("2 days").trading_days == 3
    # 30 days snaps to 21 (not 42)
    assert Horizon.parse("30 days").trading_days == 21


def test_parse_bare_integer():
    assert Horizon.parse("7").trading_days == 7
    assert Horizon.parse("21").trading_days == 21


def test_parse_errors():
    with pytest.raises(ValueError):
        Horizon.parse("banana")
    with pytest.raises(ValueError):
        Horizon.parse("")
    with pytest.raises(ValueError):
        canonical_trading_days(0)


def test_parse_or_default():
    assert Horizon.parse_or_default(None).trading_days == DEFAULT_HORIZON.trading_days
    assert Horizon.parse_or_default("garbage").trading_days == 7
    assert Horizon.parse_or_default("3d").trading_days == 3


def test_horizon_label_and_equality():
    assert Horizon(3).label == "3d"
    assert Horizon(3).label_column == "ret_3d"
    assert Horizon(7).label_column == "ret_7d"
    assert Horizon(7) == 7
    assert Horizon(7) == DEFAULT_HORIZON
    assert Horizon(3) != Horizon(7)
    assert len({Horizon(7), Horizon(7), Horizon(3)}) == 2


def test_interval_minutes():
    assert parse_interval_minutes("10m") == 10
    assert parse_interval_minutes("10 min") == 10
    assert parse_interval_minutes("30 minutes") == 30
    assert parse_interval_minutes("1h") == 60
    assert parse_interval_minutes("2 hours") == 120
    assert parse_interval_minutes("banana") is None
    assert parse_interval_minutes("0m") == 1  # floor at 1

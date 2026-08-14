"""Forecast object construction and confidence mapping."""
import numpy as np
import pandas as pd
import pytest

from forecasting.signals import (Forecast, build_forecast,
                                 confidence_from_prob)


def test_confidence_mapping():
    assert confidence_from_prob(0.5) == (0.0, "Low")
    assert confidence_from_prob(0.55) == (0.10, "Low")
    assert confidence_from_prob(0.7) == (0.40, "Medium")
    assert confidence_from_prob(0.65) == (0.30, "Medium")
    assert confidence_from_prob(0.95) == (0.90, "High")


def test_build_forecast_range_math():
    pred = pd.Series({"pred_ret": 0.038, "prob_up": 0.67, "q_lo": -0.04, "q_hi": 0.08})
    fc = build_forecast("XYZ", "2026-08-10", 100.0, pred, "v1-test", ["a", "b"])
    assert isinstance(fc, Forecast)
    assert fc.expected_return == pytest.approx(0.038)
    assert fc.expected_range_lo == pytest.approx(96.0)
    assert fc.expected_range_hi == pytest.approx(108.0)
    assert fc.direction == "UP"
    assert fc.confidence_level == "Medium"
    assert fc.as_of_date == "2026-08-10"
    d = fc.to_dict()
    assert d["direction"] == "UP"
    assert d["model_version"] == "v1-test"

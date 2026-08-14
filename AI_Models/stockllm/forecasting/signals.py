"""Forecast objects: the typed output of the forecasting pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd


def confidence_from_prob(prob_up: float) -> tuple[float, str]:
    """Map P(up) into a 0..1 edge value plus a Low/Medium/High label.

    Confidence is defined as the distance of P(up) from 0.5 -- a model close
    to coin-flip gets Low confidence, which is exactly the honest reading.
    """
    value = round(abs(float(prob_up) - 0.5) * 2.0, 3)
    if value < 0.15:
        level = "Low"
    elif value <= 0.40:
        level = "Medium"
    else:
        level = "High"
    return value, level


@dataclass
class Forecast:
    ticker: str
    as_of_date: str
    price: float
    expected_return: float  # fractional expected return over the horizon
    prob_up: float
    q_lo: float             # low quantile of horizon return
    q_hi: float             # high quantile of horizon return
    expected_range_lo: float
    expected_range_hi: float
    confidence_value: float
    confidence_level: str
    model_version: str
    features_used: list
    horizon: str = "7d"     # canonical horizon label ("7d", "3d", "21d", ...)
    horizon_days: int = 7   # trading days the forecast looks forward

    @property
    def direction(self) -> str:
        return "UP" if self.prob_up >= 0.5 else "DOWN"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["direction"] = self.direction
        return data


def build_forecast(ticker: str, date, price: float, pred: pd.Series,
                   model_version: str, features_used: list,
                   horizon: str = "7d", horizon_days: int = 7) -> Forecast:
    """Assemble a Forecast from a numeric prediction row."""
    prob_up = float(pred["prob_up"])
    q_lo, q_hi = float(pred["q_lo"]), float(pred["q_hi"])
    conf_value, conf_level = confidence_from_prob(prob_up)
    return Forecast(
        ticker=ticker,
        as_of_date=str(pd.Timestamp(date).date()),
        price=float(price),
        expected_return=float(pred["pred_ret"]),
        prob_up=prob_up,
        q_lo=q_lo,
        q_hi=q_hi,
        expected_range_lo=float(price * (1.0 + q_lo)),
        expected_range_hi=float(price * (1.0 + q_hi)),
        confidence_value=conf_value,
        confidence_level=conf_level,
        model_version=model_version,
        features_used=list(features_used),
        horizon=horizon,
        horizon_days=horizon_days,
    )

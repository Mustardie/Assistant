"""Forecast horizons, anchored to trading days.

A :class:`Horizon` maps a user-facing request ("3 days", "next week",
"1 month") to a canonical trading-day count. The trading-day count is
the single source of truth used for the label (``ret_3d``), the dataset
file (``feature_matrix_3d.csv.gz``) and the model registry
(``forecaster_3d.pkl``), so "3 days", "3 trading days" and "3d" all
address the same 3-day model.

The legacy default horizon stays 7 trading days so existing matrices,
models and commands keep working unchanged.
"""
from __future__ import annotations

import re

# User-facing horizons snap to this grid (closest wins; ties prefer the
# longer horizon). 1 covers overnight calls, 7 stays the legacy default,
# 10 = two trading weeks, 3/5/14/21/63 are the natural short/medium-term
# choices.
CANONICAL_TRADING_DAYS = (1, 3, 5, 7, 10, 14, 21, 42, 63, 126)

# Free-form phrases -> trading days (checked before the number regexes).
_TOKEN_DAYS = {
    "today": 1,
    "tomorrow": 1,
    "overnight": 1,
    "this week": 5,
    "next week": 5,
    "a week": 5,
    "one week": 5,
    "this month": 21,
    "next month": 21,
    "a month": 21,
    "one month": 21,
    "this quarter": 63,
    "next quarter": 63,
}

# Number phrases -> trading days per unit.
_REGEX_MULTIPLIERS = (
    (r"(\d+)\s*(?:trading\s+)?days?\b", 1),
    (r"(\d+)\s*d\b", 1),
    (r"(\d+)\s*(?:trading\s+)?weeks?\b", 5),
    (r"(\d+)\s*w\b", 5),
    (r"(\d+)\s*(?:calendar\s+)?months?\b", 21),
    (r"(\d+)\s*mo\b", 21),
    (r"(\d+)\s*(?:trading\s+)?quarters?\b", 63),
)


def canonical_trading_days(days: int) -> int:
    """Snap a raw day count to the nearest canonical horizon (ties -> longer)."""
    if days < 1:
        raise ValueError(
            f"horizon must be a positive number of trading days, got {days}"
        )
    return min(CANONICAL_TRADING_DAYS, key=lambda c: (abs(c - days), -c))


class Horizon:
    """Forecast horizon anchored to trading days.

    Attributes:
        trading_days: canonical number of trading days the forecast
            looks forward (the label is the forward return over this
            window).
        label: canonical label such as ``"7d"``; used in dataset and
            model file names.
    """

    __slots__ = ("trading_days",)

    def __init__(self, trading_days: int):
        self.trading_days = canonical_trading_days(int(trading_days))

    @property
    def label(self) -> str:
        return f"{self.trading_days}d"

    @property
    def label_column(self) -> str:
        return f"ret_{self.trading_days}d"

    def __eq__(self, other) -> bool:
        if isinstance(other, Horizon):
            return self.trading_days == other.trading_days
        if isinstance(other, int):
            return self.trading_days == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.trading_days)

    def __repr__(self) -> str:
        return f"Horizon({self.trading_days})"

    @classmethod
    def parse(cls, text: str) -> "Horizon":
        """Parse user text ("3 days", "next week", "14d") into a Horizon.

        Raises:
            ValueError: nothing in the text parses as a horizon.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("empty horizon (try '3d', '2 weeks', '1 month')")
        lowered = text.lower()
        for token, days in _TOKEN_DAYS.items():
            if token in lowered:
                return cls(days)
        for pattern, multiplier in _REGEX_MULTIPLIERS:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return cls(int(match.group(1)) * multiplier)
        if re.fullmatch(r"\d+", text):
            return cls(int(text))
        raise ValueError(
            f"cannot parse horizon from {text!r} "
            "(try '3d', '2 weeks', '1 month', 'next week')"
        )

    @classmethod
    def parse_or_default(cls, text: str | None,
                         default: "Horizon | None" = None) -> "Horizon":
        """Like :meth:`parse` but returns ``default`` (or 7d) on failure."""
        if not text:
            return default or DEFAULT_HORIZON
        try:
            return cls.parse(text)
        except ValueError:
            return default or DEFAULT_HORIZON


DEFAULT_HORIZON = Horizon(7)

# Re-export for callers that only want the label helper.
HORIZON_LABELS = [Horizon(d) for d in CANONICAL_TRADING_DAYS]


def parse_interval_minutes(text: str) -> int | None:
    """Parse a monitoring interval ("10m", "1h", "30 minutes") to minutes.

    Returns None if the text does not parse.
    """
    text = (text or "").strip().lower()
    match = re.fullmatch(
        r"(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h)", text
    )
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    if unit.startswith("h"):
        return max(1, value * 60)
    return max(1, value)

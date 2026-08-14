"""Selectivity: gate forecasts by evidence coverage and signal edge.

A probabilistic forecast is only as useful as the evidence behind it.  Each
row carries three availability indicators (set by make_feature_matrix):

    fund_avail_ev   : any fundamental statement / earnings event public
    news_avail_ev   : any news coverage
    sector_avail_ev : sector index data

Evidence coverage = fraction of the three groups with data at that date.
A forecast becomes a *signal* only when coverage and edge meet the
config thresholds; otherwise it is NO SIGNAL (do not act).

Tiers (config.SELECTIVITY_*):
    coverage < 0.6                     -> NO SIGNAL
    |prob - 0.5| < 0.04                -> NO SIGNAL
    edge >= 0.12  and coverage >= 0.8  -> HIGH      (else capped to MEDIUM)
    edge >= 0.075 and coverage >= 0.8  -> MEDIUM    (else LOW)
    otherwise                          -> LOW
"""
from __future__ import annotations

import pandas as pd

import config

EVIDENCE_COLS = ["fund_avail_ev", "news_avail_ev", "sector_avail_ev"]

TIER_ORDER = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "NO SIGNAL": 0}


def evidence_coverage(row: pd.Series) -> float:
    """Fraction of the evidence groups present at this row."""
    values = [row.get(c) for c in EVIDENCE_COLS]
    present = sum(1 for v in values if v is not None and v == v and float(v) > 0)
    return present / len(EVIDENCE_COLS)


def signal_tier(prob_up: float, coverage: float) -> str:
    if coverage < config.SELECTIVITY_NO_SIGNAL_COVERAGE:
        return "NO SIGNAL"
    edge = abs(prob_up - 0.5)
    if edge < config.SELECTIVITY_MIN_EDGE:
        return "NO SIGNAL"
    if edge >= config.SELECTIVITY_HIGH_EDGE:
        return "HIGH" if coverage >= config.SELECTIVITY_CAP_COVERAGE else "MEDIUM"
    if edge >= config.SELECTIVITY_MEDIUM_EDGE:
        return "MEDIUM" if coverage >= config.SELECTIVITY_CAP_COVERAGE else "LOW"
    return "LOW"


def apply_tiers(df: pd.DataFrame, prob_col: str = "prob_up") -> pd.DataFrame:
    """Attach `coverage` and `tier` columns to a results frame."""
    df = df.copy()
    df["coverage"] = df.apply(evidence_coverage, axis=1)
    df["tier"] = [signal_tier(p, c) for p, c in zip(df[prob_col], df["coverage"])]
    df["tier_rank"] = df["tier"].map(TIER_ORDER)
    return df

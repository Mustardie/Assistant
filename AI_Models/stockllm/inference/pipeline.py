"""End-to-end inference and reports.

`forecast_ticker` is the main entry point: it builds causal features from the
cached market data (optionally ending at `as_of` so historical re-forecasts
see only the past), applies a SAVED numerical model for the requested horizon
(the 7-day default fits a fresh pooled model only when no saved model exists
-- the legacy behavior), and returns a typed `Forecast`.

Model registry (V4): models are saved per horizon as ``forecaster_3d.pkl``
etc. (7d keeps the legacy ``forecaster.pkl`` path as well).  `predict` reuses
the saved model; a horizon without a saved model fails with a retrain hint
instead of silently producing an off-horizon prediction.

`generate_analysis_report` adds the Qwen reasoning layer when Ollama is up,
and falls back to a deterministic template otherwise -- the report structure
stays identical so JARVIS can parse either.

Watchlist helpers back the "Save NVIDIA" -> daily reports flow for JARVIS.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from config import (BENCHMARK_TICKERS, DEFAULT_TARGET,
                    DEFAULT_TICKERS, HORIZON, LLM_REQUIRED,
                    MODELS_DIR, PRICE_COLUMN, WATCHLIST_FILE)
from forecasting.numerical import NumericalForecaster
from forecasting.signals import Forecast, build_forecast
from llm import ollama_client, prompts
from marketdata.features import (get_features, label_column, make_feature_matrix,
                                 restore_features)
from marketdata.horizon import DEFAULT_HORIZON, Horizon
from marketdata.loader import DataUnavailableError, load_market_data
from research.news_store import NewsStore
from utils.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# model registry (per-horizon saved models)
# ---------------------------------------------------------------------------

def model_path(horizon: int | Horizon | None = None) -> Path:
    """Filesystem path of the saved model for a horizon.

    7d keeps the legacy ``forecaster.pkl`` name; other horizons use
    ``forecaster_{h}d.pkl``.
    """
    h = int(horizon) if horizon is not None else HORIZON
    if h == HORIZON:
        return MODELS_DIR / "forecaster.pkl"
    return MODELS_DIR / f"forecaster_{h}d.pkl"


def registered_horizons() -> dict[int, Path]:
    """Map of trained horizons (trading days) to their saved model paths."""
    out: dict[int, Path] = {}
    legacy = MODELS_DIR / "forecaster.pkl"
    if legacy.exists():
        out[HORIZON] = legacy
    for path in sorted(MODELS_DIR.glob("forecaster_*d.pkl")):
        name = path.stem[len("forecaster_"):]
        if name.endswith("d") and name[:-1].isdigit():
            out[int(name[:-1])] = path
    return out


def load_model(horizon: int | Horizon | None = None) -> NumericalForecaster | None:
    """Load the saved model for a horizon, or None when absent."""
    path = model_path(horizon)
    if not path.exists():
        return None
    try:
        model = NumericalForecaster.load(path)
    except Exception as exc:
        log.warning("cannot load %s (%s); ignoring saved model", path, exc)
        return None
    if getattr(model, "horizon", None) != int(horizon if horizon is not None else HORIZON):
        log.warning("%s has horizon %s but was requested for %s",
                    path, getattr(model, "horizon", None),
                    int(horizon if horizon is not None else HORIZON))
    return model


def save_model(model: NumericalForecaster,
               horizon: int | Horizon | None = None) -> Path:
    """Persist a model for its horizon; 7d also keeps the legacy alias."""
    h = int(horizon) if horizon is not None else int(getattr(model, "horizon", HORIZON))
    out = model_path(h)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.save(out)
    if h == HORIZON and out != MODELS_DIR / "forecaster.pkl":
        model.save(MODELS_DIR / "forecaster.pkl")
    return out


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%" if np.isfinite(x) else "n/a"


def digest_row(row: pd.Series) -> str:
    """One-line textual digest of a feature row for humans and the LLM."""
    def get(name):
        v = row.get(name, np.nan)
        return v if np.isfinite(v) else np.nan

    rsi = get("rsi_14")
    trend = get("trend_sma")
    return (
        f"- 1-day return: {_fmt_pct(get('ret_1'))} | 20-day return: {_fmt_pct(get('cum_ret_20'))}\n"
        f"- RSI(14): {rsi:.1f} | vs 20-day MA: {_fmt_pct(get('dist_ma20'))}\n"
        f"- MACD histogram: {get('macd_hist'):+.3f} | ATR(14)/price: {get('atr14_norm'):.3f}\n"
        f"- Volume ratio (20d): {get('vol_ratio_20'):.2f} | Trend MA10>MA20: {bool(trend)}"
    )


def _load_universe(ticker: str, as_of: str | None,
                   benchmark: str | None = None):
    """Load the target ticker plus the default universe (cached) and benchmarks.

    The wider universe is needed to compute the causal market-wide features
    (breadth, cross-sectional momentum/vol) exactly as in training.
    """
    universe = list(dict.fromkeys([ticker] + DEFAULT_TICKERS))
    frames, benches = load_market_data(universe, end=as_of,
                                       benchmarks=list(BENCHMARK_TICKERS))
    if benchmark:  # explicit home-index override
        from marketdata import loader
        try:
            benches[benchmark] = loader.load_ticker(benchmark, end=as_of)
        except DataUnavailableError as exc:
            log.warning("benchmark override unavailable: %s", exc)
    return frames, benches


def _resolve_ticker(ticker: str) -> str:
    """Normalize a bare/common ticker name (e.g. "reliance", "HDFC Bank")
    to its canonical symbol via the JARVIS alias table when possible.
    Explicit symbols pass through untouched; unknown names are returned
    as-is so the normal DataUnavailableError path reports them."""
    try:
        from jarvis.intents import resolve_ticker
    except Exception:
        return ticker
    return resolve_ticker(ticker) or ticker


def forecast_ticker(ticker: str, as_of: str | None = None,
                    benchmark: str | None = None,
                    horizon: int | str | Horizon | None = None) -> Forecast:
    """Produce a Forecast using only information available at `as_of`.

    `horizon` is a Horizon (or anything Horizon accepts: "3d", "next week",
    ​21).  It defaults to the 7-day legacy horizon.  A SAVED model for the
    horizon is used when present; the 7-day default falls back to fitting a
    fresh pooled model (legacy behavior) when no model has been saved yet.
    Other horizons require a saved model (see `main.py train --horizon`).
    """
    ticker = _resolve_ticker(ticker)
    restore_features()
    horizon_obj = Horizon.parse_or_default(horizon) if horizon is not None else DEFAULT_HORIZON
    h = horizon_obj.trading_days
    label = label_column(h)
    model = load_model(h)
    if model is None:
        if h != HORIZON:
            raise DataUnavailableError(
                f"no saved model for horizon {horizon_obj.label} -- run "
                f"`python main.py train --horizon {h}d` first (trained: "
                f"{sorted(registered_horizons())})"
            )
        log.info("no saved 7d model; falling back to a fresh pooled fit (legacy)")
    frames, benches = _load_universe(ticker, as_of, benchmark)
    if ticker not in frames:
        raise DataUnavailableError(
            f"{ticker}: no market data available (run `python main.py download`)."
        )
    from research.layers import load_layers
    fund, news, sectors = load_layers(DEFAULT_TICKERS + [ticker])
    with_candles = bool(model and any(f.startswith("f_candle_") for f in model.features))
    matrix = make_feature_matrix(frames, benches, horizon=h, fundamentals=fund,
                                 news=news, sectors=sectors, with_candles=with_candles)
    if model is not None:
        features = [f for f in model.features if f in matrix.columns]
        if len(features) != len(model.features):
            missing = sorted(set(model.features) - set(features))
            log.warning("model features unavailable in matrix, dropping: %s", missing)
    else:
        features = [f for f in get_features() if f in matrix.columns]
        features += [f for f in ("ticker_id",) if f in matrix.columns]
    t = matrix[matrix["ticker"] == ticker]
    train = t.dropna(subset=features + [label])
    if len(train) < 100:
        raise DataUnavailableError(f"{ticker}: too little history for a model ({len(train)} rows)")
    if model is None:
        model = NumericalForecaster(features, target=DEFAULT_TARGET,
                                    horizon=h, label_col=label).fit(train)
    row = t.iloc[[-1]]
    pred = model.predict(row).iloc[0]
    price = float(row.iloc[0]["price"])
    last_date = t.iloc[-1]["date"]
    return build_forecast(ticker, last_date, price, pred, model.version, features,
                          horizon=horizon_obj.label, horizon_days=h)


def _news_items(ticker: str, limit: int = 5) -> list[dict]:
    store = NewsStore()
    try:
        return store.recent(ticker=ticker, limit=limit)
    finally:
        store.close()


def deterministic_report(ticker: str, forecast: Forecast, digest: str,
                         news: list[dict] | None = None,
                         status: str | None = None) -> str:
    f = forecast
    lines = [
        f"Stock: {ticker}",
        f"As of: {f.as_of_date}",
    ]
    if status:
        lines.append(f"Recommendation status: {status}")
    lines += [
        f"Current price: {f.price:,.2f}",
        f"{f.horizon_days}-day expected return: {_fmt_pct(f.expected_return)}",
        f"Probability of positive return: {f.prob_up * 100:.0f}%",
        f"Expected range ({f.horizon_days} days): {f.expected_range_lo:,.2f} - {f.expected_range_hi:,.2f}",
        f"Confidence: {f.confidence_level} (edge {f.confidence_value:.2f})",
        f"Model version: {f.model_version}",
        "",
        "Main factors (current indicators):",
        digest,
        "",
        "Risks / what could invalidate the forecast:",
        f"- Downside quantile: {_fmt_pct(f.q_lo)} (a 10% tail scenario within {f.horizon_days} days)",
        "- Unpredictable events: earnings surprises, news shocks, macro shifts.",
    ]
    if news:
        lines += ["", "News / research (UNVERIFIED, from JARVIS web research):"]
        for n in news:
            lines.append(
                f"- {n.get('title') or '(no title)'} | {n.get('source')} | "
                f"{n.get('fetched_at')} | relevance {n.get('relevance')}"
            )
    lines += [
        "",
        "Disclaimer: this is a probabilistic hypothesis from historical patterns, "
        "not a guarantee of future returns and not a recommendation to buy or sell.",
    ]
    return "\n".join(lines)


def generate_analysis_report(ticker: str, forecast: Forecast, digest: str,
                             news: list[dict] | None = None,
                             use_llm: bool = True, status: str | None = None) -> str:
    """LLM analysis with deterministic fallback (identical report skeleton)."""
    if use_llm:
        prompt = prompts.build_analysis_prompt(ticker, forecast, digest, news)
        text = ollama_client.generate(prompts.SYSTEM_CORE, prompt)
        if text:
            if status:
                text = f"Recommendation status: {status}\n\n{text}"
            return text
        if LLM_REQUIRED:
            raise RuntimeError("LLM is required (config.LLM_REQUIRED=True) but unreachable")
    return deterministic_report(ticker, forecast, digest, news, status)


# ---------------------------------------------------------------------------
# Watchlist ("Save NVIDIA") and daily reports for JARVIS
# ---------------------------------------------------------------------------

NEUTRAL_STATUSES = [
    "HOLD / CONTINUE WATCHING",
    "REVIEW",
    "HIGH RISK",
    "FORECAST IMPROVING",
    "FORECAST DETERIORATING",
]


def load_watchlist() -> list[dict]:
    if not WATCHLIST_FILE.exists():
        return []
    with open(WATCHLIST_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("tickers", [])


def save_watchlist(entries: list[dict]) -> None:
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as fh:
        json.dump({"tickers": entries}, fh, indent=2)


def save_stock(ticker: str, forecast: Forecast) -> dict:
    entries = load_watchlist()
    entries = [e for e in entries if e["ticker"] != ticker.upper()]
    entry = {
        "ticker": ticker.upper(),
        "added_on": datetime.now().date().isoformat(),
        "added_price": forecast.price,
        "forecast": forecast.to_dict(),
    }
    entries.append(entry)
    save_watchlist(entries)
    return entry


def _status(entry: dict, forecast: Forecast) -> str:
    saved = entry.get("forecast") or {}
    saved_price = entry.get("added_price")
    saved_prob = saved.get("prob_up", 0.5)
    downside = forecast.expected_range_lo / forecast.price - 1.0
    if downside <= -0.05 and forecast.prob_up < 0.45:
        return "HIGH RISK"
    if forecast.prob_up - saved_prob >= 0.10:
        return "FORECAST IMPROVING"
    if forecast.prob_up - saved_prob <= -0.10:
        return "FORECAST DETERIORATING"
    if saved_price and forecast.price / saved_price - 1.0 <= -0.06:
        return "REVIEW"
    return "HOLD / CONTINUE WATCHING"


def daily_report(entry: dict, use_llm: bool = True,
                 horizon: int | str | Horizon | None = None) -> dict:
    """Full daily report for one watchlist entry (JARVIS entry point)."""
    ticker = entry["ticker"]
    try:
        forecast = forecast_ticker(ticker, horizon=horizon)
    except DataUnavailableError as exc:
        return {"ticker": ticker, "error": str(exc)}
    status = _status(entry, forecast)
    saved = entry.get("forecast") or {}
    digest = digest_row(feature_row_latest(ticker))
    news = _news_items(ticker)
    text = generate_analysis_report(ticker, forecast, digest, news, use_llm, status)
    saved_price = entry.get("added_price")
    report = {
        "ticker": ticker,
        "date": forecast.as_of_date,
        "horizon": forecast.horizon,
        "horizon_days": forecast.horizon_days,
        "current_price": forecast.price,
        "change_since_saved": (forecast.price / saved_price - 1.0) if saved_price else None,
        "saved_on": entry.get("added_on"),
        "original_forecast": saved.get("expected_return"),
        "original_prob_up": saved.get("prob_up"),
        "current_forecast": forecast.expected_return,
        "prob_up": forecast.prob_up,
        "expected_return_7d": forecast.expected_return,
        "expected_range": [forecast.expected_range_lo, forecast.expected_range_hi],
        "confidence": forecast.confidence_level,
        "status": status,
        "report_text": text,
        "news": news,
        "model_version": forecast.model_version,
    }
    return report


def feature_row_latest(ticker: str) -> pd.Series:
    """Latest feature row for a ticker (used for the report digest)."""
    ticker = _resolve_ticker(ticker)
    frames, benches = _load_universe(ticker, None)
    if ticker not in frames:
        return pd.Series(dtype=float)
    from research.layers import load_layers
    fund, news, sectors = load_layers(DEFAULT_TICKERS + [ticker])
    matrix = make_feature_matrix(frames, benches, fundamentals=fund, news=news,
                                 sectors=sectors)
    t = matrix[matrix["ticker"] == ticker]
    if t.empty:
        return pd.Series(dtype=float)
    return t.iloc[-1]

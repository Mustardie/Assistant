"""Monitoring: watchlists, quote providers, and the prediction loop.

Two watchlists coexist:
  * daily-report watchlist (data/watchlist.json, `save`/`report`): one
    forecast per ticker, refreshed on demand
  * monitoring watchlist (data/monitor_watchlist.json, `track`/`untrack`):
    tickers polled on an interval, each prediction recorded once per
    trading day in the tracking ledger and resolved automatically when the
    horizon window closes

The monitoring loop (`run_monitor`) is deliberately decoupled from market
data sources: a :class:`QuoteProvider` supplies the latest close per
ticker.  The default provider reads the cached daily frames (same data the
forecasts are built from); a real-time provider can be dropped in without
touching the loop.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from config import (BENCHMARK_TICKERS, DEFAULT_TRACK_INTERVAL_MIN,
                    DEFAULT_TICKERS, MONITOR_UNIVERSE,
                    MAX_TRACK_INTERVAL_MIN, MIN_TRACK_INTERVAL_MIN)
from inference.pipeline import (digest_row, forecast_ticker, load_model,
                                registered_horizons)
from marketdata.horizon import Horizon
from monitoring.ledger import PredictionLedger
from utils.logging import get_logger

log = get_logger(__name__)

MAX_HORIZON_TRADING_DAYS = 126


# ---------------------------------------------------------------------------
# monitoring watchlist persistence
# ---------------------------------------------------------------------------

def _watchlist_file() -> Path:
    return config.MONITOR_WATCHLIST_FILE


def load_monitor_watchlist() -> list[dict]:
    path = _watchlist_file()
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("tickers", [])


def save_monitor_watchlist(entries: list[dict]) -> None:
    path = _watchlist_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"tickers": entries}, fh, indent=2)


def _resolve_ticker(ticker: str) -> str:
    try:
        from jarvis.intents import resolve_ticker
    except Exception:
        return ticker
    return resolve_ticker(ticker) or ticker


def add_to_watchlist(entries: list[dict], ticker: str,
                     interval_min: int = DEFAULT_TRACK_INTERVAL_MIN,
                     horizon: str | int | None = None) -> dict:
    """Add or update a tracked ticker (mutates ``entries`` in place); returns its entry."""
    horizon_obj = Horizon.parse_or_default(horizon)
    interval = max(MIN_TRACK_INTERVAL_MIN,
                   min(int(interval_min), MAX_TRACK_INTERVAL_MIN))
    ticker = _resolve_ticker(ticker).upper()
    entries[:] = [e for e in entries if e["ticker"] != ticker]
    entry = {
        "ticker": ticker,
        "interval_min": interval,
        "horizon": horizon_obj.label,
        "horizon_days": horizon_obj.trading_days,
        "added_on": datetime.now().date().isoformat(),
    }
    entries.append(entry)
    save_monitor_watchlist(entries)
    return entry


def remove_from_watchlist(entries: list[dict], ticker: str) -> bool:
    """Remove a ticker (mutates ``entries`` in place); True if one was removed."""
    ticker = _resolve_ticker(ticker).upper()
    before = len(entries)
    entries[:] = [e for e in entries if e["ticker"] != ticker]
    if len(entries) == before:
        return False
    save_monitor_watchlist(entries)
    return True


# ---------------------------------------------------------------------------
# quote providers
# ---------------------------------------------------------------------------

class QuoteProvider:
    """Protocol: latest close per ticker. Implementations may raise."""

    def latest_closes(self, tickers: list[str]) -> dict[str, float]:
        raise NotImplementedError


class CachedDailyProvider(QuoteProvider):
    """Latest close from the cached daily frames (offline-safe).

    The same cached data the feature matrix is built from, so predictions
    and prices are always consistent.
    """

    def latest_closes(self, tickers: list[str]) -> dict[str, float]:
        from marketdata.loader import load_market_data
        frames, _ = load_market_data(tickers, end=None)
        out = {}
        for t, df in frames.items():
            closes = df["close"].dropna()
            if not closes.empty:
                out[t] = float(closes.iloc[-1])
        return out


# ---------------------------------------------------------------------------
# the monitoring loop
# ---------------------------------------------------------------------------

def ensure_model_for(horizon: Horizon):
    """Load the saved model for a horizon or raise with a train hint."""
    model = load_model(horizon.trading_days)
    if model is None:
        raise RuntimeError(
            f"no saved model for horizon {horizon.label} -- run "
            f"`python main.py train --horizon {horizon.trading_days}d` first "
            f"(trained: {sorted(registered_horizons())})"
        )
    return model


def monitor_once(watchlist: list[dict], ledger: PredictionLedger | None = None,
                 provider: QuoteProvider | None = None,
                 as_of: str | None = None) -> dict:
    """Run one monitoring cycle over the watchlist.

    Predicts the latest daily close for each tracked ticker with its
    configured horizon, records the prediction in the ledger (deduplicated
    per ticker + as_of_date + horizon), and resolves any predictions whose
    horizon window has closed.  Returns a per-ticker summary dict.
    """
    ledger = ledger or PredictionLedger()
    provider = provider or CachedDailyProvider()
    tickers = [e["ticker"] for e in watchlist]
    if not tickers:
        return {"skipped": "empty watchlist"}
    # one model per distinct horizon used by the watchlist
    horizons = {e["horizon_days"]: Horizon(e["horizon_days"])
                for e in watchlist}
    for h in horizons.values():
        ensure_model_for(h)
    # resolve due predictions first (uses the full price history)
    frames, _ = _load_all_prices(tickers, as_of)
    closes = {t: f["close"] for t, f in frames.items()}
    ledger.resolve_due(closes)
    summary = {}
    for entry in watchlist:
        ticker = entry["ticker"]
        horizon_obj = horizons[entry["horizon_days"]]
        summary[ticker] = _predict_and_record(
            ticker, horizon_obj, ledger, provider, closes, as_of)
    return summary


def _load_all_prices(tickers: list[str], as_of: str | None):
    from marketdata.loader import load_market_data
    universe = list(dict.fromkeys(tickers + list(MONITOR_UNIVERSE)))
    frames, benches = load_market_data(universe, end=as_of,
                                       benchmarks=list(BENCHMARK_TICKERS))
    return frames, benches


def _predict_and_record(ticker: str, horizon_obj: Horizon,
                        ledger: PredictionLedger, provider: QuoteProvider,
                        closes: dict[str, pd.Series],
                        as_of: str | None) -> dict:
    try:
        forecast = forecast_ticker(ticker, as_of=as_of, horizon=horizon_obj)
    except Exception as exc:
        log.warning("monitor: %s forecast failed: %s", ticker, exc)
        return {"ticker": ticker, "error": str(exc)}
    price = float(provider.latest_closes([ticker]).get(ticker, forecast.price))
    recorded = ledger.add(
        ticker=ticker,
        as_of_date=forecast.as_of_date,
        horizon_days=forecast.horizon_days,
        price=price,
        prob_up=forecast.prob_up,
        expected_return=forecast.expected_return,
        direction=forecast.direction,
    )
    return {
        "ticker": ticker,
        "as_of": forecast.as_of_date,
        "horizon": forecast.horizon,
        "price": price,
        "prob_up": forecast.prob_up,
        "expected_return": forecast.expected_return,
        "direction": forecast.direction,
        "recorded": recorded,
        "status": _status_line(forecast),
    }


def _status_line(forecast) -> str:
    p = forecast.prob_up
    if p >= 0.65:
        return "STRONG UP"
    if p >= 0.55:
        return "UP"
    if p <= 0.35:
        return "STRONG DOWN"
    if p <= 0.45:
        return "DOWN"
    return "NEUTRAL"


def run_monitor(watchlist: list[dict], interval_min: int | None = None,
                once: bool = False, max_cycles: int | None = None,
                provider: QuoteProvider | None = None,
                ledger_path=None) -> int:
    """Run the monitoring loop: predict -> record -> sleep.

    `once=True` runs a single cycle and returns.  Otherwise the loop runs
    until Ctrl-C or `max_cycles` cycles complete.
    """
    interval = max(MIN_TRACK_INTERVAL_MIN,
                   min(int(interval_min or DEFAULT_TRACK_INTERVAL_MIN),
                       MAX_TRACK_INTERVAL_MIN))
    if not watchlist:
        raise SystemExit("no stocks tracked -- use `python main.py track --tickers ...` first")
    ledger = PredictionLedger(ledger_path)
    try:
        cycle = 0
        while True:
            cycle += 1
            started = datetime.now()
            log.info("monitor cycle %d (%d tickers)", cycle, len(watchlist))
            summary = monitor_once(watchlist, ledger=ledger, provider=provider)
            for t, s in summary.items():
                if "error" in s:
                    log.warning("  %s: %s", t, s["error"])
                else:
                    log.info("  %s: %s P(up)=%.2f (recorded=%s)",
                             t, s["direction"], s["prob_up"], s["recorded"])
            if once or (max_cycles and cycle >= max_cycles):
                break
            elapsed = (datetime.now() - started).total_seconds()
            sleep_s = max(1.0, interval * 60 - elapsed)
            log.info("sleeping %.0fs until next cycle", sleep_s)
            try:
                time.sleep(sleep_s)
            except KeyboardInterrupt:
                log.info("interrupted -- ledger closed cleanly")
                break
    finally:
        ledger.close()
    return 0


def tracking_report_markdown(ledger: PredictionLedger | None = None,
                             db_path=None) -> str:
    """Human-readable report of ledger state: open, resolved, outcomes."""
    ledger = ledger or PredictionLedger(db_path)
    try:
        stats = ledger.stats()
        rows = ledger.all(limit=500)
        acc_line = ("- Direction accuracy (resolved): "
                    f"{stats['direction_acc'] * 100:.1f}%"
                    if stats["direction_acc"] is not None
                    else "- Direction accuracy (resolved): n/a")
        mean_line = ("- Mean outcome return: "
                     f"{stats['mean_outcome_ret'] * 100:+.2f}%"
                     if stats["mean_outcome_ret"] is not None
                     else "- Mean outcome return: n/a")
        lines = [
            "# Prediction tracking report",
            "",
            f"- Total predictions: {stats['total']}",
            f"- Open (horizon not yet elapsed): {stats['open']}",
            f"- Resolved: {stats['resolved']}",
            acc_line,
            mean_line,
            "",
            "| created | ticker | as_of | horizon | price | P(up) | direction | outcome | correct |",
            "|---------|--------|-------|---------|-------|-------|-----------|---------|---------|",
        ]
        for r in rows:
            correct = ("yes" if r["outcome_direction_correct"] == 1
                       else ("no" if r["outcome_direction_correct"] == 0 else ""))
            outcome = (f"{(r['outcome_ret'] * 100):+.1f}%" if r["outcome_ret"] is not None
                       else "--")
            lines.append(
                f"| {r['created_ts'][:16]} | {r['ticker']} | {r['as_of_date']} | "
                f"{r['horizon_days']}d | {r['price']:.2f} | "
                f"{r['prob_up']:.2f} | {r['direction']} | {outcome} | {correct} |")
        return "\n".join(lines)
    finally:
        ledger.close()


def digest_for_ticker(ticker: str, as_of: str | None = None) -> str:
    """One-line indicator digest for a ticker (beacon + JARVIS summaries)."""
    from inference.pipeline import feature_row_latest
    row = feature_row_latest(ticker)
    if row.empty:
        return f"{ticker}: no feature row available"
    return digest_row(row)

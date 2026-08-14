"""Paper trading simulator -- virtual money only, no orders are ever placed.

Consumes point-in-time forecast results (from the backtest engine, which are
leakage-free by construction) plus real price history, then applies simple
rules with transaction costs and slippage:

  * enter when P(up) >= BUY_PROB_THRESHOLD, sized by edge and a max position cap
  * exit on P(up) < SELL_PROB_THRESHOLD, stop-loss, or holding longer than the horizon
  * costs: TRANSACTION_COST + SLIPPAGE per side

Everything here simulates; the summary must be read as an estimate of rule
behavior, not as trading advice.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import (BUY_PROB_THRESHOLD, INITIAL_CAPITAL, MAX_HOLD_DAYS,
                    MAX_POSITION_PCT, SELL_PROB_THRESHOLD, SLIPPAGE,
                    STOP_LOSS_PCT, TRANSACTION_COST)
from evaluation.metrics import return_stats
from utils.logging import get_logger

log = get_logger(__name__)


class PaperPortfolio:
    def __init__(self, capital: float = INITIAL_CAPITAL):
        self.initial = float(capital)
        self.cash = float(capital)
        self.positions: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.equity_history: list[tuple[pd.Timestamp, float]] = []

    # ------------------------------------------------------------------
    def _enter(self, ticker, day, price, prob_up, rank):
        edge = max(prob_up - 0.5, 0.0)
        size_pct = min(MAX_POSITION_PCT, 4.0 * edge)
        if size_pct <= 0:
            return
        buy_price = price * (1.0 + SLIPPAGE)
        budget = self.cash * size_pct
        shares = budget / (buy_price * (1.0 + TRANSACTION_COST)) if buy_price > 0 else 0.0
        cost = shares * buy_price * (1.0 + TRANSACTION_COST)
        if cost > self.cash:
            shares = self.cash / (buy_price * (1.0 + TRANSACTION_COST))
            cost = shares * buy_price * (1.0 + TRANSACTION_COST)
        if shares <= 1e-8:
            return
        self.cash -= cost
        self.positions[ticker] = {
            "shares": shares,
            "avg_price": cost / shares,
            "entry_day": day,
            "entry_rank": rank,
            "entry_prob": prob_up,
        }
        self.trades.append({
            "ticker": ticker, "date": str(day.date()), "side": "BUY",
            "shares": round(shares, 4), "price": round(buy_price, 4),
            "notional": round(cost, 2), "reason": "signal",
        })

    def _exit(self, ticker, day, price, reason):
        pos = self.positions.pop(ticker)
        sell_price = price * (1.0 - SLIPPAGE)
        proceeds = pos["shares"] * sell_price * (1.0 - TRANSACTION_COST)
        self.cash += proceeds
        pnl = proceeds - pos["shares"] * pos["avg_price"]
        self.trades.append({
            "ticker": ticker, "date": str(day.date()), "side": "SELL",
            "shares": round(pos["shares"], 4), "price": round(sell_price, 4),
            "notional": round(proceeds, 2), "reason": reason,
            "pnl": round(pnl, 2),
        })

    # ------------------------------------------------------------------
    def run(self, signals: pd.DataFrame, prices: dict[str, pd.Series]) -> dict:
        """signals: backtest results frame (ticker, date, prob_up, ...).
        prices: {ticker: pd.Series of close prices indexed by date}."""
        if signals.empty:
            log.warning("no signals -> nothing to simulate")
            return {}
        signals = signals.copy()
        signals["date"] = pd.to_datetime(signals["date"])
        by_date = {d: g for d, g in signals.groupby("date")}
        dates = sorted(by_date.keys())
        day_rank = {d: i for i, d in enumerate(dates)}

        for day in dates:
            for _, s in by_date[day].iterrows():
                ticker = s["ticker"]
                px = prices.get(ticker)
                if px is None:
                    continue
                price = px.get(day)
                if price is None or not np.isfinite(price):
                    continue
                pos = self.positions.get(ticker)
                if pos is not None:
                    reason = None
                    if s["prob_up"] < SELL_PROB_THRESHOLD:
                        reason = "signal-exit"
                    elif day_rank[day] - pos["entry_rank"] >= MAX_HOLD_DAYS:
                        reason = "horizon"
                    elif price <= pos["avg_price"] * (1.0 - STOP_LOSS_PCT):
                        reason = "stop-loss"
                    if reason:
                        self._exit(ticker, day, price, reason)
                elif s["prob_up"] >= BUY_PROB_THRESHOLD:
                    self._enter(ticker, day, price, s["prob_up"], day_rank[day])

            mark = self.cash + sum(
                p["shares"] * px.get(day, p["avg_price"])
                for p, px in ((pos, prices.get(t)) for t, pos in self.positions.items())
            )
            self.equity_history.append((day, mark))

        if not self.equity_history:
            return {"n_trades": 0}
        equity = pd.Series({d: v for d, v in self.equity_history}).sort_index()
        daily_returns = equity.pct_change().dropna()
        stats = return_stats(daily_returns.values)
        stats["n_trades"] = len(self.trades)
        stats["n_round_trips"] = len(self.trades) // 2
        stats["final_equity"] = float(equity.iloc[-1])
        stats["total_return_pct"] = (equity.iloc[-1] / self.initial - 1.0) * 100.0
        buys = [t for t in self.trades if t["side"] == "BUY"]
        sells = [t for t in self.trades if t["side"] == "SELL"]
        wins = [t for t in sells if t.get("pnl", 0) > 0]
        stats["win_rate_pct"] = (len(wins) / len(sells) * 100.0) if sells else 0.0
        stats["equity"] = equity
        stats["trades"] = pd.DataFrame(self.trades)
        stats["buy_signals"] = len(buys)
        stats["buy_and_hold"] = self._buy_and_hold(prices, dates)
        return stats

    def _buy_and_hold(self, prices: dict[str, pd.Series],
                      dates: list) -> dict:
        """Equal-weight buy-and-hold of the traded universe, one cost round trip.

        Benchmark for the rule: capital split equally across all tickers with
        prices at the start of the simulation, held to the end (0.2% cost per
        side: TRANSACTION_COST + SLIPPAGE).
        """
        start, end = dates[0], dates[-1]
        tickers = [t for t, px in prices.items()
                   if px is not None and start in px.index and end in px.index
                   and np.isfinite(px.loc[start]) and np.isfinite(px.loc[end])]
        if not tickers:
            return {"n": 0}
        w = 1.0 / len(tickers)
        entry = np.array([prices[t].loc[start] for t in tickers])
        exit_ = np.array([prices[t].loc[end] for t in tickers])
        one_way = TRANSACTION_COST + SLIPPAGE
        gross = float(np.sum(w * exit_ / entry))
        net = gross * (1.0 - one_way) * (1.0 - one_way) - 1.0
        return {"n": len(tickers), "gross_pct": gross * 100.0 - 100.0,
                "net_pct": net * 100.0}

    def summary_markdown(self, stats: dict) -> str:
        if not stats:
            return "# Paper trading\n\nNo signals were produced for the period."
        lines = [
            "# Paper trading summary (SIMULATION ONLY)",
            "",
            f"- Initial virtual capital: {self.initial:,.0f}",
            f"- Final virtual equity: {stats['final_equity']:,.0f}",
            f"- Total return: {stats['total_return_pct']:+.2f}%",
            f"- Daily Sharpe (annualized): {stats.get('sharpe', 0.0):.2f}",
            f"- Max drawdown: {stats.get('max_drawdown', 0.0) * 100:.1f}%",
            f"- Buy signals: {stats['buy_signals']} | round trips: {stats['n_round_trips']}",
            f"- Closed-trade win rate: {stats['win_rate_pct']:.1f}%",
            "",
            "Benchmarks (same universe and period):",
            f"- Equal-weight buy-and-hold (net of one 0.2% cost round trip): "
            f"{stats['buy_and_hold']['net_pct']:+.2f}% "
            f"(gross {stats['buy_and_hold']['gross_pct']:+.2f}%)",
            "",
            "Rules: buy when P(up) >= 0.55 (edge-sized), sell when P(up) < 0.50, "
            "stop-loss 10%, horizon cap, 0.1% commission + 0.1% slippage per side.",
            "",
            "> Simulated on historical data with point-in-time forecasts. "
            "No real orders were placed. Past simulation results do not "
            "guarantee future performance.",
        ]
        return "\n".join(lines)

"""Paper-trading simulator sanity tests (simulation only, no money)."""
import numpy as np
import pandas as pd
import pytest

from paper_trading.simulator import PaperPortfolio


def _signals(n_days=30, n_tickers=3):
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    rows = []
    for t in range(n_tickers):
        for d in dates:
            rows.append({"ticker": f"T{t}", "date": d,
                         "prob_up": 0.6 if (d.day % 2 == 0) else 0.4,
                         "actual_ret": 0.01, "pred_ret": 0.01,
                         "q_lo": -0.02, "q_hi": 0.03})
    return pd.DataFrame(rows)


def test_buy_and_hold_benchmark():
    prices = {}
    for t in range(3):
        idx = pd.bdate_range("2024-01-01", periods=30)
        close = pd.Series(100 * (1 + np.linspace(0, 0.10, 30)), index=idx)
        prices[f"T{t}"] = close
    pf = PaperPortfolio()
    stats = pf.run(_signals(), prices)
    assert stats["n_round_trips"] >= 0
    bh = stats["buy_and_hold"]
    assert bh["n"] == 3
    assert bh["gross_pct"] == pytest.approx(10.0, abs=0.5)
    assert bh["net_pct"] < bh["gross_pct"]


def test_run_produces_equity_history():
    prices = {f"T{t}": pd.Series(100.0, index=pd.bdate_range("2024-01-01", periods=30))
              for t in range(3)}
    pf = PaperPortfolio()
    stats = pf.run(_signals(), prices)
    assert stats["n_trades"] > 0
    assert stats["final_equity"] > 0
    assert not stats["equity"].empty

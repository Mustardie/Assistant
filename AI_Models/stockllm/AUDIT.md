# StockLLM V1 Audit (2026-08-10)

Audit of the existing numerical forecasting system before the V2 upgrade.
The audit covers architecture, features, target, weaknesses, leakage review,
and recommended improvements. The V1 backtest (9,600 forecasts, 2023-2026)
reported 54.6% directional accuracy vs 55.1% base rate, MAE 3.75pp vs 3.76pp
zero-prediction: **no demonstrated edge**. From now on that 2023-2026 window
is treated as contaminated for model selection.

---

## 1. Current model architecture

- **Pooled LightGBM** (`forecasting/numerical.py`): one model trained on ALL
  tickers pooled together, with 4 heads:
  - `mean`   : regression on the 7-day forward return (expected return)
  - `prob`   : binary classifier on sign(7-day return) (P(up))
  - `q10/q90`: quantile regression for the price range
- All 4 heads share the same feature set and the same label, fit with
  `n_estimators=600`, `lr=0.05`, `num_leaves=31`, early stopping (40 rounds)
  on the chronologically last 15% of the fit rows.
- **Walk-forward** (`backtesting/engine.py`): refit every 10 trading days per
  ticker; a model fit at cutoff C is used only for prediction dates in
  (C, next_C]. Fits are cached per cutoff date and shared across tickers.
- **Leakage guard**: `allowed_train_rows` keeps only rows whose 7-day label
  window ends at or before the cutoff (verified by unit tests + e2e test).
- LLM layer (Qwen via Ollama) reasons over this layer's output; it never
  computes numbers. Not part of this audit's scope beyond the pipeline seam.

## 2. Current features (38, all causal)

| Group | Columns |
|---|---|
| Returns | ret_1, ret_2, ret_3, ret_5, ret_10, ret_20, log_ret_1 |
| Moving averages | ma_5, ma_10, ma_20, ma_50, dist_ma20, dist_ma50 |
| Volatility | vol_5, vol_10, vol_20 (std of ret_1) |
| Oscillators | rsi_7, rsi_14, macd, macd_signal, macd_hist, bb_pctb |
| ATR/range | atr14_norm, atr10_norm, hl_range_1/5/20, gap_1 |
| Volume | vol_ratio_5, vol_ratio_20 |
| Momentum | cum_ret_20, cum_ret_60 |
| Trend | trend_sma (ma_10 > ma_20) |
| Time | dayofweek, month (raw ints) |
| Market | bench_ret_1, bench_ret_5, bench_ret_20 (^GSPC only) |

## 3. Current target

- **`ret_7d`** = 7-trading-day forward return of the adjusted close
  (`price.shift(-7) / price - 1`), used by all four heads.
- The classifier and quantile heads are just alternative uses of the same
  raw-return label; there is no volatility adjustment, no bucket/ordinal
  target, no rank target.

## 4. Weaknesses found

1. **Benchmark feature mismatch (correctness bug)** — `features.py:162` uses
   `bench_list[0]` (^GSPC) for **every** ticker. Indian `.NS` stocks get
   S&P 500 features; ^NSEI is downloaded but never used. The same bug is in
   `pipeline.py` (live forecast + digest both default to ^GSPC).
2. **No stock/sector/region identity** — the pooled model cannot distinguish
   NVDA from TCS; cross-sectional level differences (typical volatility,
   momentum persistence, intraday behavior) are invisible to the model.
3. **No market-wide context** — no breadth, no cross-sectional median
   momentum, no market volatility level.
4. **No relative strength** — no RS vs the (correct) market index, so the
   model cannot distinguish stock alpha from market beta.
5. **Momentum is raw and short** — only 20/60-day cumulative returns; no
   multi-horizon normalized momentum (z-scores vs own history), no
   120/126-day term.
6. **No volatility regime features** — vol_5/10/20 are levels; no
   vol-of-vol, no vol compression/expansion ratio, no vol percentile, no
   annualized vol context.
7. **Volume features are weak** — only 5/20-day ratios vs own mean; no
   1-day anomaly, no volume trend, no high-volume regime.
8. **Trend features are minimal** — one binary (ma_10 > ma_20); no MA
   slopes, no price position in its range (Donchian), no new highs/lows,
   no distance-from-high.
9. **Time features are raw ints** — dayofweek/month as continuous integers;
   no cyclical encoding.
10. **Target design is naive for a pooled model** — raw return regression
    over volatility-heterogeneous tickers means the MAE is dominated by the
    high-vol names (NVDA/TSLA); the model can exploit cross-sectional vol
    level instead of signal. No volatility-adjusted target to compare.
11. **Baselines are thin** — evaluate reports only always-up base rate and
    zero-prediction MAE. Missing: momentum baseline, index-follow baseline,
    buy-and-hold comparison.
12. **No feature-importance diagnostics** — gain importance only for the
    `mean` head, top-8, never persisted, never analyzed; no permutation
    importance.
13. **No robustness breakdowns** — no per-ticker, per-year, per-volatility-
    regime, or per-market-regime decomposition of results.
14. **Pooling vs per-stock never measured** — the pooling choice is
    asserted ("increases sample size") but never tested.
15. **No model-selection discipline** — there is a single test window
    (2023+) and no frozen final holdout; results from that window were used
    to draw conclusions. This is the contamination the user flagged.
16. **Results CSV is not self-contained for baselines** — records
    `bench_ret_1` and `ret_1` only; prior momentum, longer benchmark
    returns, and forward benchmark return are missing, so momentum/index
    baselines can't be computed from the results file alone.
17. **`MODEL_VERSION`** is derived from features+params (good), but the
    constant `stockllm-v1-pooled-lgb` prefix will not distinguish future
    variants (pooled vs per-stock vs +id).

## 5. Leakage review (what I checked)

**No leakage found** in the existing machinery:

- Features are causal by construction; the drop-future causality test
  (`test_features_are_causal_when_future_is_dropped`) covers every feature.
- Label is `shift(-7)` (future), and the causality test + label tests pass.
- `allowed_train_rows` restricts fit rows so the label window ends at or
  before the refit cutoff — e2e test verifies forecast dates are strictly
  after cutoff.
- Benchmark joins use `merge_asof(direction="backward")` — only past index
  values.
- Early stopping uses the chronological tail of the fit data (no future).
- Chronological splits only; never a random shuffle.
- Walk-forward predictions are made strictly after the refit cutoff.

**Non-leakage risks worth noting:**

- **Model-selection contamination (the real "leakage" here)**: 2023-2026
  numbers were read and used to conclude the model has no edge. Any further
  use of that window for decisions is contaminated. Fix: dev window for
  selection + a frozen, never-looked-at final holdout.
- **Survivorship bias** (documented): yfinance only serves currently-listed
  tickers.
- `vol_ratio_5/20` denominators include today's bar — causal but slightly
  noisy; not leakage.
- `gap_1` uses today's open, which is known at close of day t (forecasts are
  made at close t) — OK.
- The reported 54.6% etc. are honest measurements; the model simply has no
  edge. Nothing fabricated.

## 6. Recommended improvements (priority order)

1. **Fix the benchmark mapping** — map each ticker to its home index
   (`.NS`/`.BO` → ^NSEI, else ^GSPC) for bench features and live inference.
2. **Establish the holdout discipline** — dev window 2023-01-01..2025-06-30
   for all selection; frozen final holdout 2025-07-01..end, evaluated once.
3. **Feature set v2** (all strictly causal):
   - multi-horizon momentum + z-scores (ret_63, cum_ret_126, mom_z_20/63)
   - relative strength vs home index (rs_5/21/63)
   - volatility regime (vol_63, vol_ratio_20_63, vol_regime, atr_regime)
   - volume anomalies (vol_ratio_1, vol_ratio_63, vol trend)
   - trend (MA slopes, Donchian position 20/60, new highs/lows,
     dist_high_20)
   - market-wide (breadth above 20d MA, cross-sectional median momentum and
     vol, per-index dist_ma200 / cum_20 / vol_20)
   - cyclical time encodings (sin/cos of dayofweek/month)
   - identity: region (IN/US), optional ticker_id for the +id variant
4. **Target design experiment** — keep raw `ret_7d`; add a
   volatility-adjusted target (`ret_7d / vol_ann`) and measure both on the
   dev window. Do not pre-judge.
5. **Model variants experiment** — pooled vs pooled+ticker_id vs per-stock,
   measured on the dev window only; per-stock predictions also give
   per-ticker importance.
6. **Stronger baselines in evaluation** — momentum (sign of prior 20d),
   index-follow (forecast = forward index return), zero-prediction,
   always-up; buy-and-hold comparison for the paper simulator.
7. **Diagnostics** — gain importance for all heads + out-of-sample
   permutation importance from the walk-forward predictions; robustness
   breakdowns by ticker / year / volatility regime / market regime.
8. **No change to the evaluation rules or the walk-forward methodology** —
   they are sound and must stay comparable to V1.

## 7. Non-goals (confirmed)

- No Qwen fine-tuning. No claim that Qwen improves numerical prediction.
- No real money; paper trading stays a simulation.
- Keep the existing walk-forward methodology; do not tune rules to make
  numbers look better.

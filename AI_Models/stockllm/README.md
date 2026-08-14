# StockLLM

A financial-analysis and stock-forecasting AI module for JARVIS. It understands
companies and markets, produces **probabilistic 7-trading-day forecasts**,
explains them, backtests itself honestly, and never executes a trade.

Built as a **hybrid architecture** — no LLM is asked to predict raw prices:

```
Market / time-series data (OHLCV, indices, fundamentals-ready)
        │
        ▼
Causal feature engineering (strictly point-in-time)
        │
        ▼
Numerical forecasting layer (LightGBM, pooled)
        │  expected return · P(up) · low/high quantiles · confidence
        ▼
Qwen reasoning layer (local Ollama)  ──  JARVIS web research (unverified sources)
        │  interpretation · risks · what could invalidate the forecast
        ▼
Readable analysis / daily reports (neutral statuses, no buy/sell advice)
```

Division of labor: the **numerical layer owns the numbers**, the **LLM layer
owns language** (financial interpretation, news, explanation) and is explicitly
instructed never to invent data and never to present forecasts as guarantees.

---

## What StockLLM does (and does not)

| Works | Does not |
|---|---|
| Loads real market data (Yahoo Finance, auto-adjusted OHLCV) | Execute any trade |
| 68 causal technical/market/identity features | Claim profits are assured |
| 7-day return / P(up) / price-range forecasts | Fabricate financial data |
| Walk-forward backtesting (dev + frozen final holdout) | Use future information in training or prediction |
| Metrics: direction accuracy, MAE, calibration, 5+ baselines, robustness breakdowns | Shuffle time-series data for splitting |
| Feature diagnostics: gain + out-of-sample permutation importance | Tune on the final holdout |
| Model-scope experiments: pooled / +ticker-id / per-stock | |
| Paper trading with virtual capital + buy-and-hold benchmark | |
| Daily reports + watchlist ("Save NVIDIA") for JARVIS | |
| Qwen (Ollama) reasoning layer with deterministic fallback | |
| Foundation scripts for future Qwen fine-tuning (LoRA/QLoRA) | |

---

## Directory layout

```
stockllm/
├── main.py                  CLI: download prep train backtest evaluate diagnose predict save report paper news build-dataset finetune info
├── config.py                every tunable (horizon, windows, scope/target, model, costs, LLM)
├── AUDIT.md                 V1 audit (2026-08-10): architecture, weaknesses, leakage review
├── requirements.txt
├── README.md
├── marketdata/
│   ├── loader.py            yfinance download + cache to data/raw/ + local-CSV support
│   ├── features.py          causal feature engineering v2 + label + market-wide context
│   └── splits.py            chronological splits + walk-forward leakage guard
├── forecasting/
│   ├── numerical.py         LightGBM: mean, binary (P up), quantiles; target variants; per-stock wrapper
│   └── signals.py           Forecast object + confidence mapping
├── inference/
│   └── pipeline.py          end-to-end forecast, reports, watchlist, daily reports
├── llm/
│   ├── ollama_client.py     local Qwen via Ollama (pkg or REST; graceful offline)
│   └── prompts.py           disciplined prompts (no-guarantee, unverified sources)
├── backtesting/
│   └── engine.py            walk-forward engine, dev/holdout windows, scope variants
├── evaluation/
│   ├── metrics.py           accuracy, MAE/RMSE/bias, Brier, calibration, baselines, breakdowns
│   └── diagnostics.py       variant comparison + out-of-sample permutation importance
├── paper_trading/
│   └── simulator.py         virtual portfolio with costs + slippage + buy-and-hold benchmark
├── research/
│   └── news_store.py        SQLite store for JARVIS web-research items (source+time)
├── training/
│   ├── build_reasoning_dataset.py   synthetic reasoning examples (grounded in realized outcomes)
│   └── qwen_finetune.py            QLoRA foundation script (extra deps, not installed)
├── data/                    raw downloads + watchlist.json (gitignored)
├── datasets/                feature matrix + reasoning dataset
├── models/                  trained artifacts (gitignored)
├── logs/                    evaluation/diagnose/paper reports (gitignored)
├── backtesting/results/     dev/ and holdout/ per-forecast records (gitignored)
└── tests/                   leakage / splits / metrics / e2e / paper (36 tests)
```

---

## Install

```powershell
cd E:\Assistant\AI_Models\stockllm
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

(Optional, for the fine-tuning scripts only — not needed for V1 usage:)
`pip install transformers peft bitsandbytes accelerate trl datasets`

## 1. Get data

```powershell
python main.py download              # default universe (12 tickers) + indices ^GSPC ^NSEI
python main.py download --tickers RELIANCE.NS,TCS.NS --benchmarks ^NSEI
```

- Data is cached as CSV in `data/raw/` and never re-downloaded unless `--force`.
- yfinance uses **auto-adjusted** prices, so splits/dividends (corporate
  actions) are already reflected — no price jumps inside the feature stream.
- **Survivorship bias:** yfinance only serves currently-listed tickers; delisted
  companies are a documented data-source limitation, not something V1 hides.
- You can drop your own CSVs into `data/raw/` with the schema
  `date,open,high,low,close,volume[,adj close]` — the loader uses them with no
  network access.

## 2. Build the feature matrix

```powershell
python main.py prep
```

Creates `datasets/feature_matrix.csv.gz` + `datasets/meta.json` (~30k rows,
68 features for the default universe). Every feature is **strictly causal** —
rolling windows end at the row's date, returns use past closes, benchmark
values are as-of joined, and the label is the *forward* 7-day return
(`ret_7d`). The last 7 rows per ticker have no label by construction.

Feature groups (V2): returns & multi-horizon momentum (incl. z-scores),
moving averages & MA slopes, price position (Donchian) & new extremes,
volatility + regime (vol compression, 1y-relative vol), volume anomalies,
RS vs the **home index** (^NSEI for `.NS` tickers, ^GSPC otherwise —
fixed in V2; V1 used ^GSPC for everything), market-wide breadth and
cross-sectional momentum/vol, cyclical time encodings, and a region flag.
A deterministic `ticker_id` enables the "+id" model variant.

## 3. Train the numerical model

```powershell
python main.py train
```

Fits the LightGBM forecaster (expected return, P(up), 10%/90% quantiles)
on the chronological train split (≤ 2021-12-31), early-stops on the tail of
the fit data (never the future), reports validation metrics, saves
`models/forecaster.pkl`. **Splits are date cuts, never shuffles.**

## 4. Backtest (the honest part)

```powershell
python main.py backtest                    # dev window (model selection): 2023-01-01..2025-06-30
python main.py backtest --window holdout   # frozen final holdout: 2025-07-01..end (run ONCE)
python main.py backtest --scope pooled|id|per_stock --target raw|voladj
```

**Model-selection discipline (established after the V1 backtest):** the
2023-2026 window is treated as contaminated for model selection. All
development, feature/variant comparison and tuning happens on the **dev
window** only; the **final holdout** is frozen and evaluated exactly once
with the final configuration. Results are stored in
`backtesting/results/dev/` and `backtesting/results/holdout/`.

The engine refits the model every `REFIT_EVERY_DAYS` trading days on history
up to the refit cutoff, then predicts only strictly later dates. Two
guarantees are enforced in code and verified by tests:

1. **No label leakage:** training rows at a cutoff are restricted to rows whose
   7-day label window *ends* at or before the cutoff.
2. **No feature leakage:** features are causal by construction.

Every recorded forecast stores: date, ticker, price, prediction, quantiles,
confidence, actual return, direction correctness, error, model version, the
feature list, **the refit cutoff that produced it**, plus baseline columns
(prior 20/60d momentum, benchmark 1/5/20d returns, forward index return,
volatility at forecast time) — so baselines and robustness breakdowns are
computable from the results CSV alone.

## 5. Evaluate

```powershell
python main.py evaluate --window dev|holdout
```

Writes a markdown report to `logs/evaluation_<window>_*.md` with:

- Directional accuracy **vs the always-up base rate** (the key honesty check)
- MAE / RMSE / bias, average predicted vs actual return
- Brier score and a **calibration table** (predicted P(up) vs realized up-rate)
- Baselines: always-up, zero-prediction, **momentum** (sign of prior 20d),
  **index-follow** (forward home-index return: MAE and direction accuracy)
- Robustness breakdowns: **by ticker, by year, by volatility regime, by
  market regime**
- An indicative per-forecast trade rule with costs (explicitly labeled *not* a
  portfolio simulation)

V2 results on the dev window (id scope): 57.1% directional accuracy vs 56.9%
base rate, MAE 3.77pp vs 3.82pp zero-prediction, calibration error 0.018;
momentum baseline 50.9%, index-follow direction 72.0%. On the frozen final
holdout (2025-07-01..2026-08-07): 52.1% vs 51.9% base rate, MAE 3.65pp vs
3.59pp zero-prediction. **The model still has no demonstrated directional
edge; the honest findings are the baselines — the market's own 7-day
direction (index-follow: ~70% on both windows) dominates any stock-specific
signal this feature set extracts.**

## 5b. Diagnostics (dev window only)

```powershell
python main.py diagnose                       # scopes x targets + permutation importance
python main.py diagnose --scopes pooled,id --targets raw,voladj --n-perm 3
```

Compares model variants (pooled vs pooled+ticker_id vs per_stock, targets
raw vs volatility-adjusted) on the dev window and computes **out-of-sample
permutation importance** from the walk-forward predictions themselves
(which features, when shuffled, degrade MAE/accuracy the most). Selected on
dev: `scope=id, target=raw` (per_stock was worse; voladj was
indistinguishable). The final holdout is never used here.

## 6. Live forecasts + Qwen/Ollama

```powershell
python main.py predict --ticker NVDA            # with local Qwen reasoning
python main.py predict --ticker NVDA --no-llm   # deterministic template (no LLM)
python main.py predict --ticker MSFT --as-of 2023-06-15 --json   # point-in-time re-forecast
```

- The pipeline checks `config.OLLAMA_MODEL` (default `qwen3:8b` — set it to
  whatever Qwen tag you have in Ollama) at `http://localhost:11434`.
- If Ollama is offline the report falls back to a deterministic template with
  the identical structure (set `config.LLM_REQUIRED = True` to refuse instead).
- Console mojibake for non-ASCII characters? Set `$env:PYTHONIOENCODING="utf-8"`
  before running.

## 7. Watchlist + daily reports (JARVIS flow)

```powershell
python main.py save --ticker NVDA        # JARVIS: "Save NVIDIA"
python main.py report                    # all saved tickers
python main.py report --json             # machine-readable for JARVIS
```

`report` produces per-stock daily reports: current price, change since saved,
original vs current forecast, 7-day expected return, confidence, technical
situation, news (with source + timestamp + unverified marker), risks, why the
prediction changed, and a neutral status — `HOLD / CONTINUE WATCHING`,
`REVIEW`, `HIGH RISK`, `FORECAST IMPROVING`, `FORECAST DETERIORATING`.
No buy/sell is ever recommended. Reports are saved to `logs/daily_report_*.json`.

## 7b. Monitoring + prediction tracking (V4)

```powershell
python main.py track --tickers NVDA TCS.NS --interval 10 --horizon 7d
python main.py track --once                       # one monitor cycle, then exit
python main.py untrack --tickers TCS.NS
python main.py watchlist                          # both watchlists
python main.py tracking-report                    # ledger: open/resolved outcomes
```

`track` adds tickers to `data/monitor_watchlist.json` (interval clamped
1-1440 min) and runs the monitor loop: each cycle forecasts every tracked
ticker, records the prediction in `data/tracking_ledger.sqlite` (deduped by
ticker + as-of date + horizon), and re-forecasts only when the ticker's own
interval has elapsed. `tracking-report` resolves predictions whose horizon
window has completed (positional index) and shows win rate per ticker —
the ledger is the audit trail of every forecast, never a recommendation.

## 7c. Multi-horizon forecasting + candlestick layer (V4)

```powershell
python main.py prep --horizon 3 --candles         # feature_matrix_3d.csv.gz (+25 candle cols)
python main.py train --horizon 3 --candles        # fits + saves forecaster_3d.pkl
python main.py predict --ticker NVDA --horizon "2 weeks"
python main.py backtest --horizon 3
python main.py experiment                         # variants D = A + candles, E = C + candles
python main.py info                               # registered model horizons
```

- Horizons snap to a canonical grid `(1,3,5,7,10,14,21,42,63,126)` trading
  days; `7d` stays the legacy default, so existing matrices/models/commands
  are untouched. Non-7d artifacts are horizon-suffixed
  (`feature_matrix_3d.csv.gz`, `forecaster_3d.pkl`); `predict` loads the
  saved per-horizon model from the registry (`inference/pipeline.py`) and
  falls back to fresh fitting only for the legacy 7d model.
- `research/candles.py` adds 25 causal candlestick features (body/range/
  wick geometry, engulfing/inside/outside bars, hammer/shooting-star,
  streaks, availability). Every value at row `t` uses only bars `t, t-1,
  ...`; patterns spanning a missing bar are NaN, never 0.
- `python main.py jarvis --request "..."` turns natural language into a
  structured intent (ticker aliases, horizons, intervals) and executes it:
  `"what do you think about NVIDIA"`, `"track RELIANCE every 15 minutes"`,
  `"stop tracking AAPL"`, `"how are my predictions doing"`.

## 8. Paper trading (simulation only)

```powershell
python main.py paper
```

Runs the walk-forward signals through a virtual portfolio
(`INITIAL_CAPITAL = ₹1,000,000`): edge-sized entries when P(up) ≥ 0.55,
exits on P(up) < 0.50 / stop-loss / horizon cap, 0.1% commission + 0.1%
slippage per side. Outputs equity curve, Sharpe, drawdown, win rate, and an
**equal-weight buy-and-hold benchmark over the same universe and period**
(dev-window example: rule +19.1% vs buy-and-hold +148.9% — the rule sits in
cash during strong trends). **No orders are ever placed.**

## 9. Research store (JARVIS web research)

```powershell
python main.py news add --ticker NVDA --title "Q2 earnings beat" --source browser --relevance 0.8 --text "..."
python main.py news list --ticker NVDA
```

Persistence is SQLite (`research/news_store.sqlite`). Every item keeps source,
timestamp, extracted text, relevance and a verified flag. Reports surface these
with an explicit "UNVERIFIED" marker and the LLM prompt instructs the model to
treat web pages as unverified — StockLLM never blindly trusts the browser.

## 10. Fine-tuning foundation (later step, not part of V1)

```powershell
python main.py build-dataset --limit 2000                    # synthetic reasoning examples (JSONL)
python main.py build-dataset --limit 500 --use-teacher-llm   # distillation: local Qwen writes rationales
python main.py finetune --dry-run                            # validate dataset without training
python main.py finetune --epochs 2                           # QLoRA SFT (needs extra pip installs)
```

The dataset builder creates historical examples whose *input* contains only
information available on that date and whose *response* is grounded in the
**realized** outcome (known because the date is historical). The QLoRA script
targets `Qwen/Qwen2.5-7B-Instruct` with 4-bit NF4 quantization — comfortable on
the RTX 5070 (12 GB) / 32 GB RAM setup. The script is the scaffold: it is not
run during V1 and never feeds future outcomes into live prompts.

---

## JARVIS integration

From JARVIS, everything is one subprocess call (`tools/stockllm_tool.py`
wraps these in the tool registry):

- `python main.py save --ticker NVDA`  ← "Save NVIDIA"
- `python main.py report --json`       ← daily stock report (parse the JSON)
- `python main.py predict --ticker X --horizon 3d --json --no-llm`
- `python main.py track --tickers X --once`        ← stockllm_track
- `python main.py untrack --tickers X`             ← stockllm_untrack
- `python main.py watchlist`                       ← stockllm_watchlist
- `python main.py tracking-report`                 ← stockllm_tracking_report
- `python main.py info`                            ← stockllm_status
- `python main.py jarvis --request "track TCS for 2 weeks"`  ← parse NL directly
- `python main.py news add ...`        ← feed web-research findings in

The report JSON contains `status`, `prob_up`, `expected_return_7d`,
`expected_range`, `confidence`, `news`, and `report_text` for narration.

## Leakage controls (summary)

1. Causal features only — verified by `tests/test_features.py` (incl. the new
   market-wide features at matrix level).
2. Label = forward 7-day return, NaN on the last 7 rows.
3. Chronological train/val/dev/holdout cuts; the dev window is the only place
   model selection happens; the **final holdout is evaluated once**.
4. Walk-forward refits restricted to rows whose label window ends before the
   cutoff (`marketdata/splits.allowed_train_rows`).
5. Benchmark joins are as-of (backward) — only past index values; each ticker
   uses its **home index**.
6. Per-forecast records store their refit cutoff for auditability.
7. Permutation importance is computed out-of-sample, on the walk-forward
   predictions themselves.
8. Survivorship and corporate-action caveats are documented, not hidden.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

100 tests cover feature causality (incl. matrix-level and the V4
candlestick layer), label + vol-adjusted target correctness, horizon
parsing/snapping, home-index mapping, walk-forward label guard, scope and
target variants, metric correctness on known values, momentum/index
baselines, robustness breakdowns, the paper-trading buy-and-hold benchmark,
confidence mapping, loader normalization, the prediction ledger and
watchlist/intent flows, and an end-to-end leak-free backtest on synthetic
data.

## Hardware

Everything in V1 runs on the CPU in seconds-to-minutes (LightGBM). The LLM
layer uses your existing local Qwen via Ollama (qwen3:8b is a good default on
the RTX 5070). Fine-tuning uses QLoRA and fits comfortably in 12 GB VRAM.

## Honest limitations

- The V2 model is **still not profitable, skilled, or tuned** — the evaluation
  framework exists precisely to show you that before you trust it. On the
  frozen final holdout: 52.1% vs 51.9% base rate (no directional edge) and
  MAE 3.65pp vs 3.59pp zero-prediction (no magnitude edge). The dominant
  7-day signal measured so far is the **market's own direction** (index-follow
  ~70%), which this feature set does not yet exploit.
- Hyperparameters were not re-tuned (V1 defaults carried over); that is a
  documented next step — on the dev window only.
- Technical indicators only; fundamentals/earnings feeds are designed-in hooks,
  not yet wired to a data source.
- News is stored and surfaced but only as unverified context.
- Forecasts are probabilistic hypotheses. Nothing here guarantees returns, and
  nothing here ever trades for you.

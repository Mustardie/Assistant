"""StockLLM central configuration.

All experiment-relevant tunables live here so runs stay reproducible:
edit this file (or pass the CLI flags described in README) and re-run.
"""
from __future__ import annotations

from pathlib import Path

from marketdata.horizon import DEFAULT_HORIZON

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = ROOT / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
MONITOR_WATCHLIST_FILE = DATA_DIR / "monitor_watchlist.json"
TRACKING_DB = DATA_DIR / "tracking_ledger.sqlite"
DATASETS_DIR = ROOT / "datasets"
MODELS_DIR = ROOT / "models"
LOGS_DIR = ROOT / "logs"
RESULTS_DIR = ROOT / "backtesting" / "results"
NEWS_DB_PATH = ROOT / "research" / "news_store.sqlite"

for _d in (DATA_DIR, RAW_DATA_DIR, DATASETS_DIR, MODELS_DIR, LOGS_DIR, RESULTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
DEFAULT_TICKERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "SBIN.NS", "ITC.NS",
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "TSLA",
]
BENCHMARK_TICKERS = ["^GSPC", "^NSEI"]
DOWNLOAD_START = "2017-01-01"
DOWNLOAD_END = None  # None means "today"
LOOKBACK_DAYS = 120  # extra calendar days fetched before the start date for indicator warm-up
REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]

# ---------------------------------------------------------------------------
# Prediction setup
# ---------------------------------------------------------------------------
HORIZON = 7              # forecast horizon in trading days (legacy default)
DEFAULT_HORIZON = DEFAULT_HORIZON  # canonical Horizon(7) for the pipeline
MAX_HORIZON_TRADING_DAYS = 126     # longest supported horizon
PRICE_COLUMN = "close"   # adjusted close (yfinance auto_adjust=True)
LABEL_COLUMN = "ret_7d"
QUANTILES = (0.10, 0.90)

# ---------------------------------------------------------------------------
# Monitoring / prediction tracking (V4)
# ---------------------------------------------------------------------------
DEFAULT_TRACK_INTERVAL_MIN = 10          # default minutes between monitor cycles
MIN_TRACK_INTERVAL_MIN = 1               # never poll faster than this
MAX_TRACK_INTERVAL_MIN = 60 * 24         # sanity cap for the CLI
DEFAULT_TRACK_HORIZON = "7d"             # horizon for tracked predictions
MONITOR_UNIVERSE = DEFAULT_TICKERS  # context tickers loaded each monitor cycle

# ---------------------------------------------------------------------------
# Chronological data split (never a random shuffle)
#
# Model-selection discipline (established 2026-08-10, after the V1 backtest):
# the 2023-2026 window is contaminated for model selection. All development,
# feature/variant comparison and tuning happens on the DEV window only.
# The FINAL HOLDOUT (HOLDOUT_START onwards) is frozen: it is evaluated
# exactly once, with the final selected configuration, and never used for
# any selection decision.
# ---------------------------------------------------------------------------
TRAIN_END = "2021-12-31"
VAL_END = "2022-12-31"
TEST_START = "2023-01-01"   # legacy alias of DEV_START (kept for V1 scripts)
DEV_START = "2023-01-01"    # model selection window start
DEV_END = "2025-06-30"      # model selection window end (contaminated V1 results stop 2023+)
HOLDOUT_START = "2025-07-01"  # frozen final holdout start
MIN_TRAIN_ROWS = 400  # pooled rows below which a walk-forward refit is skipped

# Backtest windows: "dev" selects on the dev window, "holdout" on the frozen
# final holdout. Results are stored in separate subdirectories.
BACKTEST_WINDOWS = {
    "dev": {"start": DEV_START, "end": DEV_END},
    "holdout": {"start": HOLDOUT_START, "end": None},
}
DEFAULT_BACKTEST_WINDOW = "dev"

# Model scope variants (compared on the dev window, see `diagnose`):
#   pooled    : one model over all tickers (V1 architecture)
#   id        : pooled model + ticker_id categorical feature
#   per_stock : one model per ticker
MODEL_SCOPES = ("pooled", "id", "per_stock")
DEFAULT_MODEL_SCOPE = "id"  # selected on dev window 2026-08-10 (see logs/diagnose_*)

# Target variants for the mean/quantile heads:
#   raw    : predict ret_7d directly (V1)
#   voladj : predict ret_7d / annualized_vol_63, converted back to returns
TARGET_VARIANTS = ("raw", "voladj")
DEFAULT_TARGET = "raw"  # voladj was indistinguishable on dev; raw keeps labels simple

# ---------------------------------------------------------------------------
# Model (LightGBM, CPU-friendly)
# ---------------------------------------------------------------------------
SEED = 42
LGB_PARAMS = dict(
    n_estimators=600,
    learning_rate=0.05,
    num_leaves=31,
    min_child_samples=40,
    subsample=0.9,
    colsample_bytree=0.9,
    reg_alpha=0.1,
    reg_lambda=1.0,
    random_state=SEED,
    verbose=-1,
)
EARLY_STOPPING_ROUNDS = 40
MODEL_VERSION = "stockllm-v2-lgb"
RESULTS_DEV_DIR = RESULTS_DIR / "dev"
RESULTS_HOLDOUT_DIR = RESULTS_DIR / "holdout"

# ---------------------------------------------------------------------------
# Backtesting (walk-forward)
# ---------------------------------------------------------------------------
REFIT_EVERY_DAYS = 10    # trading days between refits
BACKTEST_MIN_TESTS = 10000

# ---------------------------------------------------------------------------
# Paper trading (SIMULATION ONLY -- never places orders)
# ---------------------------------------------------------------------------
INITIAL_CAPITAL = 1_000_000.0
BUY_PROB_THRESHOLD = 0.55
SELL_PROB_THRESHOLD = 0.50
MAX_POSITION_PCT = 0.20
STOP_LOSS_PCT = 0.10
MAX_HOLD_DAYS = HORIZON + 3
TRANSACTION_COST = 0.001  # per side, fraction of notional
SLIPPAGE = 0.001          # per side, fraction of price

# ---------------------------------------------------------------------------
# LLM layer (local Qwen via Ollama)
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"  # use whatever Qwen tag you have locally (e.g. qwen3:14b)
LLM_TIMEOUT_SECONDS = 180
LLM_TEMPERATURE = 0.3
LLM_MAX_TOKENS = 1200
LLM_REQUIRED = False  # if True, reports refuse to run when Ollama is offline

# ---------------------------------------------------------------------------
# Research / news store
# ---------------------------------------------------------------------------
NEWS_MAX_AGE_DAYS = 14
NEWS_MIN_RELEVANCE = 0.0

# ---------------------------------------------------------------------------
# V3 data layers (fundamentals / news / sector context)
# ---------------------------------------------------------------------------
# Availability date for a statement when no exact filing date is known
# (EDGAR gives exact dates for US tickers; this lag is the fallback).
FUNDAMENTAL_PUB_LAG_DAYS = {"US": 30, "IN": 45}
NEWS_FETCH_START = "2020-01-01"

# News ingestion policy (see news/service.py).  Providers are company
# disclosures with exact publication timestamps (no general media, no article
# body -- documented limitation):
#   US : SEC EDGAR filings (8-K / 10-K / 10-Q, filing date = availability)
#   IN : NSE corporate announcements (IST timestamps, converted to UTC)
NEWS_MIN_DELAY = 2.0              # min seconds between HTTP requests
NEWS_MAX_RETRIES = 5              # retries per (ticker, window)
NEWS_BACKOFF_BASE = 5.0           # exponential backoff base (seconds)
NEWS_BACKOFF_CAP = 120.0          # backoff never exceeds this (no stalling)
NEWS_REQUEST_TIMEOUT = 60         # per-request timeout (seconds)
NEWS_MAX_CONSECUTIVE_FAILURES = 8  # abort after this many consecutive failures

EDGAR_NEWS_FORMS = ("8-K", "10-K", "10-Q")

# NSE announcements are served for these symbols only (our Indian universe).
NSE_SYMBOLS = ("RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN", "ITC")

NEWS_PROVIDER_SETTINGS = {
    "edgar": {"min_delay": 1.0, "max_retries": 5, "backoff_base": 3.0},
    "nse": {"min_delay": 2.5, "max_retries": 6, "backoff_base": 8.0},
}

# Sector index per ticker (snapshot mapping; index-membership drift over the
# history is a documented limitation of this layer).
SECTOR_MAP = {
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "GOOGL": "XLK",
    "AMZN": "XLY", "TSLA": "XLY",
    "RELIANCE.NS": "^CNXENERGY", "TCS.NS": "^CNXIT", "INFY.NS": "^CNXIT",
    "HDFCBANK.NS": "^NSEBANK", "SBIN.NS": "^NSEBANK", "ITC.NS": "^CNXFMCG",
}

# ---------------------------------------------------------------------------
# V3 selectivity: evidence-based signal gating
# ---------------------------------------------------------------------------
# A forecast is only usable as a HIGH/MEDIUM/LOW signal when it has evidence:
#   * evidence coverage = fraction of the evidence feature groups (fundamentals,
#     news, sector context) with data at that date
#   * signal edge       = |prob_up - 0.5|
# NO_SIGNAL below the coverage floor or the minimum edge.
SELECTIVITY_NO_SIGNAL_COVERAGE = 0.6   # below this -> NO SIGNAL
SELECTIVITY_CAP_COVERAGE = 0.8         # HIGH tier requires coverage >= this
SELECTIVITY_MIN_EDGE = 0.04            # |prob-0.5| below this -> NO SIGNAL
SELECTIVITY_MEDIUM_EDGE = 0.075        # edge >= this -> MEDIUM (LOW below)
SELECTIVITY_HIGH_EDGE = 0.12           # edge >= this -> HIGH

# V3 experiment: feature-set variants compared on the DEV window.
#   A = v2 numeric only | B = + fundamentals/earnings | C = + news
# V4 added the candlestick geometry layer (research/candles.py):
#   D = A + candles | E = C + candles (full stack)
EXPERIMENT_VARIANTS = {
    "A": {"name": "A: numeric (v2)", "with_fundamentals": False, "with_news": False},
    "B": {"name": "B: + fundamentals/earnings", "with_fundamentals": True, "with_news": False},
    "C": {"name": "C: + news", "with_fundamentals": True, "with_news": True},
    "D": {"name": "D: + candles", "with_fundamentals": False, "with_news": False},
    "E": {"name": "E: full stack (news + candles)", "with_fundamentals": True, "with_news": True},
}
EXPERIMENT_SCOPE = "id"
EXPERIMENT_TARGET = "raw"

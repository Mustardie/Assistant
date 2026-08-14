"""Feature engineering v2 -- every feature is strictly causal.

A feature stored at row `t` may only depend on information observed at or
before `t`: rolling windows end at `t`, returns use past closes, and the
label (forward return) is computed with a negative shift so it can never
appear in any feature.  This is enforced by construction, and the unit tests
verify it (test_features.py) -- including the new market-wide features.

Feature groups (V2 additions over V1 are marked +):

  * returns           ret_1/2/3/5/10/20/63, log_ret_1
  * momentum          cum_ret_20/60/126, mom_z_20/63 (z-score vs own history) +
  * moving averages   ma_5/10/20/50, dist_ma20/50, ma slopes +, trend_sma
  * price position    Donchian position 20/60 +, dist_high_20 +,
                      new 20/60d highs and lows +
  * volatility        vol_5/10/20/63, vol_ratio_20_63 (compression) +,
                      vol_regime (vs 1y history) +, atr14/10_norm, atr_regime +
  * oscillators       rsi_7/14, macd, macd_signal, macd_hist, bb_pctb
  * range/gap         hl_range_1/5/20, gap_1
  * volume            vol_ratio_1/5/20/63 (1d anomaly +, 63d trend +)
  * time              dayofweek/month (raw) + cyclical sin/cos encodings +
  * market-wide       market_breadth (fraction above 20d MA) +,
                      cross-sectional median 21d momentum + and vol_20 +
                      (computed causally across the loaded universe)
  * benchmark (home   bench_ret_1/5/20, bench_cum_20 +, bench_vol_20 +,
    index per ticker) bench_dist_ma200 +   (^NSEI for .NS/.BO, ^GSPC else)
  * identity          region (1 = India, 0 = US) +; ticker_id is added by the
                      matrix builder for the "id" model variant only

Metadata columns (never features): price, ticker, date, row_rank, vol_ann
(annualized 63d realized vol), ret_7d_voladj (volatility-adjusted target),
bench_fwd_7 (forward index return, an outcome used only for baselines).

Causality of market-wide features: the context panel is built on a date
grid that contains only dates <= t for each value (ffill never looks
forward); per-ticker market columns are joined with as-of (backward)
alignment.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from config import DEFAULT_TICKERS, HORIZON, LABEL_COLUMN, PRICE_COLUMN

_FEATURES: list[str] = []


def get_features() -> list[str]:
    """Current feature column list (set by make_feature_matrix)."""
    return list(_FEATURES)


def restore_features(meta_path=None) -> list[str]:
    """Restore the feature list persisted by `prep` (needed in fresh processes)."""
    global _FEATURES
    if _FEATURES:
        return list(_FEATURES)
    import json
    meta_path = meta_path or Path(__file__).resolve().parents[1] / "datasets" / "meta.json"
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as fh:
            _FEATURES = list(json.load(fh).get("features", []))
    return list(_FEATURES)


def load_matrix(path=None) -> pd.DataFrame:
    """Load the persisted feature matrix and restore its feature list."""
    import json
    path = path or Path(__file__).resolve().parents[1] / "datasets" / "feature_matrix.csv.gz"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing -- run `python main.py prep` first.")
    restore_features()
    return pd.read_csv(path, parse_dates=["date"])


# ---------------------------------------------------------------------------
# identity / benchmark mapping
# ---------------------------------------------------------------------------

def region_for_ticker(ticker: str) -> float:
    """1.0 for Indian-listed symbols, 0.0 otherwise (used as a feature)."""
    return 1.0 if (ticker.endswith(".NS") or ticker.endswith(".BO")) else 0.0


def index_for_ticker(ticker: str, benchmarks: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    """Home market index for a ticker (^NSEI for Indian listings, ^GSPC else)."""
    want = "^NSEI" if region_for_ticker(ticker) == 1.0 else "^GSPC"
    if want in benchmarks:
        return benchmarks[want]
    return next(iter(benchmarks.values()), None)


def ticker_id_for(ticker: str) -> float:
    """Deterministic ticker code used by the "id" model variant.

    Stable across processes and across loaded ticker sets: default-universe
    tickers get their position in DEFAULT_TICKERS, anything else gets a
    stable hash.  An unseen ticker therefore never shifts the codes of the
    tickers the model was trained on.
    """
    if ticker in DEFAULT_TICKERS:
        return float(DEFAULT_TICKERS.index(ticker))
    return float(int(hashlib.sha256(ticker.encode("utf-8")).hexdigest(), 16) % 4096)


# ---------------------------------------------------------------------------
# target
# ---------------------------------------------------------------------------

def label_column(horizon: int = HORIZON) -> str:
    """Name of the forward-return label column for a horizon."""
    return LABEL_COLUMN if int(horizon) == HORIZON else f"ret_{int(horizon)}d"


def add_label(df: pd.DataFrame, price: pd.Series | None = None,
              horizon: int = HORIZON, label: str | None = None) -> pd.DataFrame:
    """Attach the forward `horizon`-day return label.  Last `horizon` rows get NaN.

    `price` must be the same-length price series when `df` holds only features
    (the raw OHLCV frames contain the price column themselves).
    """
    df = df.copy()
    if price is None:
        price = df[PRICE_COLUMN]
    label = label or label_column(horizon)
    df[label] = price.shift(-horizon) / price - 1.0
    return df


def voladj_label(label: str | None = None) -> str:
    """Name of the volatility-adjusted target for a label column."""
    return (label or LABEL_COLUMN) + "_voladj"


def add_voladj_target(df: pd.DataFrame, vol_ann: pd.Series,
                      label: str | None = None) -> pd.DataFrame:
    """Volatility-adjusted target: label return / annualized realized vol.

    Scale-free, so the pooled model is not dominated by high-vol names.
    NaN vol (insufficient history) propagates to NaN target.
    """
    df = df.copy()
    df[voladj_label(label)] = df[label or LABEL_COLUMN] / vol_ann.replace(0.0, np.nan)
    return df


# ---------------------------------------------------------------------------
# indicators
# ---------------------------------------------------------------------------

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _zscore(series: pd.Series, window: int = 250) -> pd.Series:
    mean = series.rolling(window, min_periods=window).mean()
    std = series.rolling(window, min_periods=window).std()
    return (series - mean) / std.replace(0.0, np.nan)


# ---------------------------------------------------------------------------
# market-wide context (causal, cross-sectional within the loaded universe)
# ---------------------------------------------------------------------------

def _market_context(frames: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    """Causally-computed cross-sectional features on a union date grid.

    Each ticker contributes its own (past-only) series; ffilling on the union
    grid never looks forward, and per-ticker rows are later joined as-of
    (backward), so only information available at or before `t` is used.
    """
    if not frames:
        return None
    date_idx = sorted(set().union(*[set(df.index) for df in frames.values()]))
    date_idx = pd.DatetimeIndex(date_idx)
    cols = {}
    for ticker, df in frames.items():
        ret1 = df[PRICE_COLUMN].pct_change()
        above = (df[PRICE_COLUMN]
                 > df[PRICE_COLUMN].rolling(20, min_periods=20).mean()).astype(float)
        cols[(ticker, "above_ma20")] = above
        cols[(ticker, "ret_21")] = df[PRICE_COLUMN].pct_change(21)
        cols[(ticker, "vol_20")] = ret1.rolling(20, min_periods=20).std()
    panel = pd.DataFrame(cols, index=date_idx)
    panel = panel.sort_index().ffill()
    above = panel[[c for c in panel.columns if c[1] == "above_ma20"]]
    ret21 = panel[[c for c in panel.columns if c[1] == "ret_21"]]
    vol20 = panel[[c for c in panel.columns if c[1] == "vol_20"]]
    return pd.DataFrame({
        "market_breadth": above.mean(axis=1),
        "market_mom_median": ret21.median(axis=1),
        "market_vol_median": vol20.median(axis=1),
    })


# ---------------------------------------------------------------------------
# per-ticker features
# ---------------------------------------------------------------------------

def build_features(df: pd.DataFrame, benchmark: pd.DataFrame | None = None,
                   context: pd.DataFrame | None = None,
                   horizon: int = HORIZON) -> pd.DataFrame:
    """Compute causal feature columns from an OHLCV frame (index = date).

    `benchmark` is the ticker's HOME index frame (see index_for_ticker);
    `context` holds the market-wide columns (see _market_context).  Both are
    joined with as-of (backward) alignment so only past values are used.
    """
    df = df.copy()
    close, vol = df["close"], df["volume"]
    high, low, open_ = df["high"], df["low"], df["open"]

    f = pd.DataFrame(index=df.index)

    # --- returns ----------------------------------------------------------
    for lag in (1, 2, 3, 5, 10, 20, 63):
        f[f"ret_{lag}"] = close.pct_change(lag)
    f["log_ret_1"] = np.log(close / close.shift(1))

    # --- momentum ---------------------------------------------------------
    f["cum_ret_20"] = close / close.shift(20) - 1.0
    f["cum_ret_60"] = close / close.shift(60) - 1.0
    f["cum_ret_126"] = close / close.shift(126) - 1.0
    f["mom_z_20"] = _zscore(f["cum_ret_20"])
    f["mom_z_63"] = _zscore(f["cum_ret_60"])

    # --- moving averages / trend ------------------------------------------
    for w in (5, 10, 20, 50):
        f[f"ma_{w}"] = close.rolling(w, min_periods=w).mean()
    f["dist_ma20"] = close / f["ma_20"] - 1.0
    f["dist_ma50"] = close / f["ma_50"] - 1.0
    f["ma_slope_10_20"] = (f["ma_10"] - f["ma_20"]) / close
    f["ma_slope_20_50"] = (f["ma_20"] - f["ma_50"]) / close
    f["trend_sma"] = (f["ma_10"] > f["ma_20"]).astype(float)

    # --- price position / new extremes ------------------------------------
    hi20, lo20 = close.rolling(20, min_periods=20).max(), close.rolling(20, min_periods=20).min()
    hi60, lo60 = close.rolling(60, min_periods=60).max(), close.rolling(60, min_periods=60).min()
    f["price_pos_20"] = (close - lo20) / (hi20 - lo20).replace(0.0, np.nan)
    f["price_pos_60"] = (close - lo60) / (hi60 - lo60).replace(0.0, np.nan)
    f["dist_high_20"] = close / hi20 - 1.0
    f["new_high_20"] = (close >= hi20).astype(float)
    f["new_high_60"] = (close >= hi60).astype(float)
    f["new_low_20"] = (close <= lo20).astype(float)
    f["new_low_60"] = (close <= lo60).astype(float)

    # --- volatility / regime ----------------------------------------------
    for w in (5, 10, 20):
        f[f"vol_{w}"] = f["ret_1"].rolling(w, min_periods=w).std()
    f["vol_63"] = f["ret_1"].rolling(63, min_periods=63).std()
    f["vol_ratio_20_63"] = f["vol_20"] / f["vol_63"].replace(0.0, np.nan)
    f["vol_regime"] = f["vol_20"] / f["vol_20"].rolling(250, min_periods=250).mean() - 1.0
    f["atr14_norm"] = _atr(high, low, close, 14) / close
    f["atr10_norm"] = _atr(high, low, close, 10) / close
    f["atr_regime"] = f["atr14_norm"] / f["atr14_norm"].rolling(250, min_periods=250).mean() - 1.0

    # --- oscillators -------------------------------------------------------
    f["rsi_7"] = _rsi(close, 7)
    f["rsi_14"] = _rsi(close, 14)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    f["macd"] = ema12 - ema26
    f["macd_signal"] = f["macd"].ewm(span=9, adjust=False).mean()
    f["macd_hist"] = f["macd"] - f["macd_signal"]
    ma20 = close.rolling(20, min_periods=20).mean()
    sd20 = close.rolling(20, min_periods=20).std()
    f["bb_pctb"] = (close - (ma20 - 2 * sd20)) / (4 * sd20).replace(0.0, np.nan)

    # --- range / gap -------------------------------------------------------
    hl_range = (high - low) / close
    f["hl_range_1"] = hl_range
    for w in (5, 20):
        f[f"hl_range_{w}"] = hl_range.rolling(w, min_periods=w).mean()
    f["gap_1"] = open_ / close.shift(1) - 1.0

    # --- volume anomalies --------------------------------------------------
    for w in (1, 5, 20, 63):
        f[f"vol_ratio_{w}"] = vol / vol.rolling(w, min_periods=w).mean()

    # --- time (raw + cyclical) ---------------------------------------------
    f["dayofweek"] = df.index.dayofweek.astype(float)
    f["month"] = df.index.month.astype(float)
    dow = 2 * np.pi * df.index.dayofweek / 7.0
    mon = 2 * np.pi * (df.index.month - 1) / 12.0
    f["dow_sin"], f["dow_cos"] = np.sin(dow), np.cos(dow)
    f["month_sin"], f["month_cos"] = np.sin(mon), np.cos(mon)

    # --- market-wide -------------------------------------------------------
    if context is not None:
        f = pd.merge_asof(f, context, left_index=True, right_index=True,
                          direction="backward")

    # --- home-index benchmark ----------------------------------------------
    if benchmark is not None:
        b_close = benchmark[PRICE_COLUMN]
        b_ret1 = b_close.pct_change()
        bf = pd.DataFrame({
            "bench_ret_1": b_ret1,
            "bench_ret_5": b_close.pct_change(5),
            "bench_ret_20": b_close.pct_change(20),
            "bench_cum_20": b_close / b_close.shift(20) - 1.0,
            "bench_vol_20": b_ret1.rolling(20, min_periods=20).std(),
            "bench_dist_ma200": b_close / b_close.rolling(200, min_periods=200).mean() - 1.0,
            # forward index return, an outcome used only for baselines
            f"bench_fwd_{'7' if int(horizon) == HORIZON else f'{int(horizon)}d'}": (
                b_close.shift(-horizon) / b_close - 1.0
            ),
        }).dropna(how="all")
        f = pd.merge_asof(f, bf, left_index=True, right_index=True, direction="backward")

    return f


# ---------------------------------------------------------------------------
# matrix assembly
# ---------------------------------------------------------------------------

def _pit_merge(feats: pd.DataFrame, pit: pd.DataFrame | None) -> pd.DataFrame | None:
    """As-of (backward) join of a point-in-time frame onto the feature frame.

    Only values available at or before the row's date are used, which is the
    causality contract for the fundamentals / news layers.
    """
    if pit is None or pit.empty:
        return None
    pit = pit.sort_index()
    if not isinstance(pit.index, pd.DatetimeIndex):
        pit.index = pd.to_datetime(pit.index)
    if pit.index.dtype != feats.index.dtype:
        # merge_asof rejects unit mismatches (M8[us] vs M8[ns])
        pit.index = pit.index.astype(feats.index.dtype)
    return pd.merge_asof(feats, pit, left_index=True, right_index=True,
                         direction="backward")


def _days_since(frame: pd.DataFrame, ts_col: str) -> pd.Series:
    """Days between the row date and a (possibly NaT) point-in-time column."""
    return ((frame.index - frame[ts_col]).total_seconds() / 86400.0).astype(float)


def make_feature_matrix(frames: dict[str, pd.DataFrame],
                        benchmarks: dict[str, pd.DataFrame] | None = None,
                        horizon: int = HORIZON,
                        fundamentals: dict[str, pd.DataFrame] | None = None,
                        news: dict[str, pd.DataFrame] | None = None,
                        sectors: dict[str, pd.DataFrame] | None = None,
                        with_candles: bool = False) -> pd.DataFrame:
    """Combine per-ticker frames into one training matrix.

    Columns: causal features + label (+ vol-adjusted target) + ticker/price/
    region/vol_ann/row_rank metadata.  row_rank is the per-ticker position in
    the timeline (0 = oldest); the walk-forward engine uses it to guarantee
    the label window is complete.

    V3 point-in-time layers (all merged as-of, so only past information):
      * `fundamentals` : dict ticker -> fundamental/earnings feature frames
        (see fundamentals.fetcher)  -> f_* columns
      * `news`         : dict ticker -> news feature frames (see news.service)
        -> f_news_* columns
      * `sectors`      : dict sector-symbol -> OHLCV frame (see marketdata.sector)
        -> s_* columns
    Availability is recorded per row in the metadata columns
    fund_avail_ev / news_avail_ev / sector_avail_ev (never features), which
    the selectivity layer uses to decide whether a forecast has evidence.

    V4 candlestick layer: when `with_candles` is True, causal OHLC geometry
    features (research/candles.py) are attached as f_candle_* columns and
    availability is recorded in candle_avail_ev.
    """
    global _FEATURES
    benchmarks = benchmarks or {}
    fundamentals = fundamentals or {}
    news = news or {}
    sectors = sectors or {}
    context = _market_context(frames)
    label = label_column(horizon)
    parts = []
    for ticker, df in frames.items():
        bench = index_for_ticker(ticker, benchmarks)
        feats = build_features(df, bench, context, horizon)
        feats = add_label(feats, df[PRICE_COLUMN], horizon, label)
        ret1 = df[PRICE_COLUMN].pct_change()
        vol_ann = ret1.rolling(63, min_periods=63).std() * np.sqrt(252)
        feats["vol_ann"] = vol_ann
        feats = add_voladj_target(feats, vol_ann, label)

        # --- V4 candlestick geometry ---------------------------------------
        candle_avail_ev = None
        if with_candles:
            from research.candles import candle_features_from_ohlcv
            cf = candle_features_from_ohlcv(df)
            if "f_candle_avail" in cf.columns:
                candle_avail_ev = cf["f_candle_avail"].fillna(0.0)
            feats = feats.join(cf)

        # --- V3 point-in-time layers --------------------------------------
        fund_avail_ev = news_avail_ev = sector_avail_ev = pd.Series(0.0, index=feats.index)
        fund = _pit_merge(feats, fundamentals.get(ticker))
        if fund is not None:
            if "f_last_earnings_ts" in fund.columns:
                fund["f_days_since_earnings"] = _days_since(fund, "f_last_earnings_ts")
            fund = fund.drop(columns=[c for c in ("f_last_earnings_ts",
                                                  "f_last_news_ts") if c in fund.columns])
            feats = fund
            avail_cols = [c for c in ("f_report_avail", "f_earnings_avail")
                          if c in fund.columns]
            fund_avail_ev = fund[avail_cols].max(axis=1).fillna(0.0) if avail_cols else fund_avail_ev
        nw = _pit_merge(feats, news.get(ticker))
        if nw is not None:
            if "f_last_news_ts" in nw.columns:
                nw["f_days_since_news"] = _days_since(nw, "f_last_news_ts")
            nw = nw.drop(columns=[c for c in ("f_last_earnings_ts",
                                              "f_last_news_ts") if c in nw.columns])
            feats = nw
            news_avail_ev = nw["f_news_avail"].fillna(0.0)
        from marketdata import sector as sector_mod
        sym = sector_mod.sector_for_ticker(ticker)
        sdf = sectors.get(sym) if sym else None
        srel = sector_mod.sector_relative_features(ticker, df[PRICE_COLUMN], sdf, bench)
        if not srel.empty:
            feats = feats.join(srel)
        sm = _pit_merge(feats, sector_mod.sector_features(ticker, sdf))
        if sm is not None:
            feats = sm
            sector_avail_ev = sm["s_avail"].fillna(0.0)
        feats["fund_avail_ev"] = fund_avail_ev
        feats["news_avail_ev"] = news_avail_ev
        feats["sector_avail_ev"] = sector_avail_ev
        if candle_avail_ev is not None:
            feats["candle_avail_ev"] = candle_avail_ev

        feats["ticker"] = ticker
        feats["ticker_id"] = ticker_id_for(ticker)
        feats["region"] = region_for_ticker(ticker)
        feats["price"] = df[PRICE_COLUMN]
        parts.append(feats)
    if not parts:
        raise ValueError("no data to build features from")
    matrix = pd.concat(parts)
    _EXCLUDE = {label, voladj_label(label), "vol_ann", "ticker", "ticker_id",
                "price", f"bench_fwd_{'7' if int(horizon) == HORIZON else f'{int(horizon)}d'}",
                "fund_avail_ev", "news_avail_ev", "sector_avail_ev",
                "candle_avail_ev",
                "f_last_earnings_ts", "f_last_news_ts"}
    _FEATURES = [c for c in parts[0].columns if c not in _EXCLUDE]
    matrix = matrix.rename_axis("date").reset_index()
    matrix["row_rank"] = matrix.groupby("ticker").cumcount()
    return matrix


def load_matrix(path=None, horizon: int | None = None) -> pd.DataFrame:
    """Load a persisted feature matrix and restore its feature list.

    `horizon` selects the horizon-suffixed file (``feature_matrix_3d.csv.gz``)
    when given; the default keeps the legacy 7d name for backward
    compatibility.
    """
    import json
    if horizon is not None and int(horizon) != HORIZON:
        path = path or (
            Path(__file__).resolve().parents[1] / "datasets"
            / f"feature_matrix_{int(horizon)}d.csv.gz"
        )
    else:
        path = path or Path(__file__).resolve().parents[1] / "datasets" / "feature_matrix.csv.gz"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing -- run `python main.py prep` first.")
    restore_features()
    return pd.read_csv(path, parse_dates=["date"])

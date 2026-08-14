"""Fundamentals: quarterly statements + earnings events, strictly point-in-time.

Sources
-------
* Statements (income, balance sheet, cash flow) and earnings events come from
  Yahoo Finance (`yf.Ticker(...)`).  This is the *restated* view: yfinance only
  exposes today's numbers, so reported EPS etc. are what they are as of now.
  To stay causal we therefore never use the reported value as-of a date
  earlier than its *availability date*.
* Availability date: for US tickers we take the exact SEC filing date from the
  EDGAR submissions API (10-Q/10-K), matched to the statement's period end.
  For Indian tickers (no EDGAR) and unmatched periods we apply a conservative
  publication lag (period end + FUNDAMENTAL_PUB_LAG_DAYS).  The choice per
  statement is recorded in the `source` column ('edgar' vs 'lag-<n>d').

Statement data is cached as JSON under data/fundamentals/.  Nothing here ever
uses information that was not public at the availability date.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from config import DATA_DIR, FUNDAMENTAL_PUB_LAG_DAYS, RAW_DATA_DIR
from utils.logging import get_logger

log = get_logger(__name__)

FUND_DIR = DATA_DIR / "fundamentals"
FUND_DIR.mkdir(parents=True, exist_ok=True)

_INCOME_ROWS = {
    "revenue": "TotalRevenue",
    "gross_profit": "GrossProfit",
    "net_income": "NetIncome",
    "diluted_eps": "DilutedEPS",
    "ebit": "EBIT",
}
_BALANCE_ROWS = {
    "assets": "TotalAssets",
    "liabilities": "TotalLiabilitiesNetMinorityInterest",
    "cash": "CashAndCashEquivalents",
    "debt": "TotalDebt",
    "equity": "StockholdersEquity",
}
_CASHFLOW_ROWS = {
    "ocf": "OperatingCashFlow",
    "capex": "CapitalExpenditure",
    "fcf": "FreeCashFlow",
}

_EDGAR_USER_AGENT = "StockLLM Research mailto:stockllm-research@example.com"


def _to_num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype(float)


def _cache_path(ticker: str) -> Path:
    return FUND_DIR / f"{ticker.replace('.', '_').replace('^', 'INDEX_')}.json"


# ---------------------------------------------------------------------------
# EDGAR filing dates (exact point-in-time availability for US tickers)
# ---------------------------------------------------------------------------

def fetch_edgar_filing_dates(ticker: str, start: str = "2010-01-01") -> pd.DataFrame:
    """Period end -> filing date for 10-K/10-Q from the EDGAR submissions API.

    Returns columns: period_end (datetime), form, filing_date (datetime).
    Empty frame when the ticker is not a US filer (or EDGAR is unreachable).
    """
    cik = _cik_for_ticker(ticker)
    if cik is None:
        return pd.DataFrame(columns=["period_end", "form", "filing_date"])
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    req = urllib.request.Request(url, headers={"User-Agent": _EDGAR_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except Exception as exc:  # network/parse failure must not block the build
        log.warning("%s: EDGAR unavailable (%s) -- using publication lag", ticker, exc)
        return pd.DataFrame(columns=["period_end", "form", "filing_date"])
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    periods = recent.get("reportDate", [])
    rows = []
    for form, filing, period in zip(forms, dates, periods):
        if form in ("10-K", "10-Q"):
            rows.append({"period_end": pd.Timestamp(period), "form": form,
                         "filing_date": pd.Timestamp(filing)})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates("period_end").sort_values("period_end")
    return df


def _cik_for_ticker(ticker: str) -> int | None:
    if "." in ticker:  # not a US listing
        return None
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
           f"&CIK={ticker}&type=10-K&dateb=&owner=include&count=1&output=atom")
    req = urllib.request.Request(url, headers={"User-Agent": _EDGAR_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(200_000).decode("utf-8", errors="ignore")
        marker = "<cik>"
        idx = body.find(marker)
        if idx < 0:
            return None
        end = body.find("</cik>", idx)
        return int(body[idx + len(marker):end].strip())
    except Exception as exc:
        log.warning("%s: CIK lookup failed (%s)", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# fetch + normalize
# ---------------------------------------------------------------------------

def fetch_and_cache_fundamentals(ticker: str, force: bool = False,
                                 audit: "AuditStore | None" = None) -> dict:
    """Fetch statements + earnings events, normalize, cache, audit.

    Statements: EDGAR companyfacts (US, exact filed dates) with a yfinance
    fallback; yfinance annual+quarterly for Indian tickers (covers ~5
    periods -- coverage is reported honestly by the audit).  Earnings events
    always come from yfinance (history back to ~2020).
    """
    path = _cache_path(ticker)
    if path.exists() and not force:
        return json.loads(path.read_text(encoding="utf-8"))

    import yfinance as yf
    tk = yf.Ticker(ticker)
    data: dict = {"ticker": ticker}
    stmts = _statements_for(ticker, tk)
    data["statements"] = _as_json(stmts)

    ed = tk.get_earnings_dates(limit=100)
    if ed is not None and not ed.empty:
        ev = pd.DataFrame({
            "event_date": _naive_index(ed.index),
            "est_eps": _to_num(ed["EPS Estimate"]),
            "reported_eps": _to_num(ed["Reported EPS"]),
            "surprise_pct": _to_num(ed["Surprise(%)"]),
        }).drop_duplicates("event_date").sort_values("event_date")
        data["earnings"] = _as_json(ev.set_index("event_date"))

    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    log.info("cached fundamentals %s (%d statements, %d earnings)",
             ticker, len(stmts), len(data.get("earnings", [])))

    if audit is not None:
        _audit_fundamentals(audit, ticker, stmts, data.get("earnings"), None)
    return data


def _combine_debt(stmts: pd.DataFrame) -> None:
    """Fold whatever debt components exist into one 'debt' column (in place).

    EDGAR-reported tickers may carry only current or only non-current debt;
    requiring both used to leave 'debt' undefined and zeroed out every
    f_debt_to_equity ratio.  Component columns are consumed either way so
    the written schema is stable across providers.
    """
    parts = [c for c in ("debt_cur", "debt_lt") if c in stmts.columns]
    if not parts:
        return
    if "debt" not in stmts.columns:
        stmts["debt"] = stmts[parts].sum(axis=1, min_count=1)
    stmts.drop(columns=parts, inplace=True)


def _statements_for(ticker: str, tk) -> pd.DataFrame:
    """Tidy quarterly statements; EDGAR facts first for US, yfinance else."""
    stmts = pd.DataFrame()
    if not (ticker.endswith(".NS") or ticker.endswith(".BO")):
        try:
            from fundamentals.edgar_facts import facts_pivot
            cik = _cik_for_ticker(ticker)
            if cik is not None:
                wide = facts_pivot(cik)
                if len(wide) >= 4:
                    stmts = wide.drop(columns=["avail_date"]).copy()
                    _combine_debt(stmts)
                    if "ocf" in stmts.columns and "capex" in stmts.columns:
                        stmts["fcf"] = stmts["ocf"] - stmts["capex"].abs()
        except Exception as exc:
            log.warning("%s: EDGAR facts failed (%s) -- yfinance fallback",
                        ticker, str(exc)[:120])
    if stmts.empty:
        stmts = _yfinance_statements(tk)
    return stmts


def _yfinance_statements(tk) -> pd.DataFrame:
    try:
        inc_a = tk.get_income_stmt(freq="yearly")
        bal_a = tk.get_balance_sheet(freq="yearly")
        csh_a = tk.get_cashflow(freq="yearly")
    except Exception as exc:
        raise RuntimeError(f"yfinance statements failed: {exc}") from exc
    inc_q = csh_q = bal_q = None
    try:
        inc_q = tk.get_income_stmt(freq="quarterly")
        bal_q = tk.get_balance_sheet(freq="quarterly")
        csh_q = tk.get_cashflow(freq="quarterly")
    except Exception as exc:
        log.warning("yfinance quarterly statements failed: %s", str(exc)[:100])
    if inc_a is None and inc_q is None:
        raise RuntimeError("yfinance returned no statement data")

    def to_tidy(df, row_map) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        df.columns = pd.to_datetime(df.columns)
        out = pd.DataFrame(index=pd.DatetimeIndex(sorted(df.columns)))
        for out_name, row in row_map.items():
            if row in df.index:
                out[out_name] = _to_num(df.loc[row]).reindex(out.index)
        return out

    frames = [to_tidy(inc_a, _INCOME_ROWS), to_tidy(bal_a, _BALANCE_ROWS),
              to_tidy(csh_a, _CASHFLOW_ROWS)]
    if inc_q is not None:
        frames += [to_tidy(inc_q, _INCOME_ROWS), to_tidy(bal_q, _BALANCE_ROWS),
                   to_tidy(csh_q, _CASHFLOW_ROWS)]
    stmts = pd.concat(frames, axis=1)
    stmts = stmts[~stmts.index.duplicated(keep="last")].sort_index()
    return stmts


def _as_json(df: pd.DataFrame) -> dict:
    return {"index": [str(i) for i in df.index],
            "columns": list(df.columns),
            "values": df.values.tolist()}


def load_fundamentals(ticker: str) -> dict | None:
    """Load cached fundamentals; None when not downloaded yet."""
    path = _cache_path(ticker)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# point-in-time availability
# ---------------------------------------------------------------------------

def pit_statements(ticker: str) -> pd.DataFrame:
    """Statements with an availability date; NaN rows dropped.

    Columns: period_end, avail_date, source + the tidy statement columns.
    """
    data = load_fundamentals(ticker)
    if data is None:
        return pd.DataFrame()
    stmts = _from_json(data["statements"])
    if stmts.empty:
        return pd.DataFrame()
    _combine_debt(stmts)
    stmts.index = _naive_index(stmts.index)
    stmts = stmts[~stmts.index.isna()]
    if stmts.empty:
        return pd.DataFrame()
    region = "IN" if ticker.endswith(".NS") else "US"
    filing = fetch_edgar_filing_dates(ticker) if region == "US" else pd.DataFrame()
    by_period = (filing.set_index("period_end")["filing_date"]
                 if not filing.empty else pd.Series(dtype="datetime64[ns]"))
    lag = FUNDAMENTAL_PUB_LAG_DAYS.get(region, 45)
    source = []
    avail = []
    for period_end in stmts.index:
        if period_end in by_period.index:
            avail.append(by_period.loc[period_end])
            source.append("edgar")
        else:
            avail.append(period_end + pd.Timedelta(days=lag))
            source.append(f"lag-{lag}d")
    stmts["avail_date"] = pd.to_datetime(avail)
    stmts["source"] = source
    return stmts.dropna(subset=["avail_date"])


def pit_earnings(ticker: str) -> pd.DataFrame:
    """Earnings events as point-in-time rows (available at event_date)."""
    data = load_fundamentals(ticker)
    if data is None:
        return pd.DataFrame()
    ev = _from_json(data.get("earnings", {}))
    if ev.empty:
        return pd.DataFrame()
    ev.index = _naive_index(ev.index)
    ev = ev[~ev.index.isna()]
    return ev.sort_index()


def _from_json(blob: dict) -> pd.DataFrame:
    if not blob or not blob.get("columns"):
        return pd.DataFrame()
    df = pd.DataFrame(blob["values"], columns=blob["columns"], index=blob["index"])
    df = df.loc[:, ~df.columns.duplicated(keep="last")]
    return df.apply(pd.to_numeric, errors="coerce")


def _naive_index(idx) -> pd.DatetimeIndex:
    """Normalize any index/iterable of timestamps to a naive DatetimeIndex.

    Tolerates mixed timezone offsets (yfinance caches DST vs standard time)
    by parsing to a common UTC wall-clock then stripping the tz, so event
    dates are preserved and deterministic without ever shifting a date.
    Unparsable entries become NaT (dropped by the caller).  The result is
    forced to nanosecond resolution so it joins cleanly with the trading
    calendar (merge_asof rejects unit mismatches like M8[us] vs M8[ns]).
    """
    ts = pd.to_datetime(idx, errors="coerce", utc=True)
    if ts.tz is not None:
        ts = ts.tz_localize(None)
    return ts.as_unit("ns")


# ---------------------------------------------------------------------------
# point-in-time features (one row per statement)
# ---------------------------------------------------------------------------

def fundamental_features(ticker: str) -> pd.DataFrame:
    """Derived fundamental ratios, keyed by avail_date (a statement is usable
    from its availability date onwards).  NaN means the item was not public yet.

    Returns a frame indexed by avail_date with columns:
        f_rev_growth_yoy, f_eps_growth_yoy, f_net_margin, f_gross_margin,
        f_fcf_margin, f_debt_to_equity, f_roe, f_roa
    plus `f_report_avail` (1.0 on/after the first statement).

    Robust by contract: `pit_statements` returns a frame whose *index* is the
    statement's period_end (with an `avail_date` column).  Any frame that is
    empty, lacks a usable datetime index, or carries no recognised metric
    columns yields an empty schema-safe frame (no fabricated data), which the
    caller's low-evidence logic treats as "no fundamentals yet".
    """
    st = pit_statements(ticker)
    if st.empty:
        return pd.DataFrame(columns=["f_report_avail"])
    if "period_end" in st.columns and not isinstance(st.index, pd.DatetimeIndex):
        st = st.set_index("period_end")
    if not isinstance(st.index, pd.DatetimeIndex) or "avail_date" not in st.columns:
        return pd.DataFrame(columns=["f_report_avail"])
    st = st.loc[:, ~st.columns.duplicated(keep="last")]
    st = st[~st.index.duplicated(keep="last")].sort_index()
    avail = pd.to_datetime(st["avail_date"], errors="coerce")
    if avail.isna().all():
        return pd.DataFrame(columns=["f_report_avail"])
    avail = avail.fillna(pd.Series(st.index, index=avail.index)).values  # 1:1 with st rows

    growth = pd.DataFrame(index=st.index)
    for base_col, out in (("revenue", "rev_growth_yoy"),
                          ("net_income", "ni_growth_yoy")):
        if base_col in st.columns:
            base = st[base_col].shift(4)
            growth[out] = (st[base_col] / base - 1.0).where(base.notna())

    cols: dict[str, pd.Series] = {}
    if {"net_income", "revenue"} <= set(st.columns):
        cols["f_net_margin"] = st["net_income"] / st["revenue"]
    if {"gross_profit", "revenue"} <= set(st.columns):
        cols["f_gross_margin"] = st["gross_profit"] / st["revenue"]
    if {"fcf", "revenue"} <= set(st.columns):
        cols["f_fcf_margin"] = st["fcf"] / st["revenue"]
    if {"debt", "cash", "equity"} <= set(st.columns):
        cols["f_debt_to_equity"] = (st["debt"] - st["cash"]) / st["equity"]
    if {"net_income", "equity"} <= set(st.columns):
        cols["f_roe"] = st["net_income"] / st["equity"]
    if {"net_income", "assets"} <= set(st.columns):
        cols["f_roa"] = st["net_income"] / st["assets"]
    if "rev_growth_yoy" in growth.columns:
        cols["f_rev_growth_yoy"] = growth["rev_growth_yoy"]
    if "ni_growth_yoy" in growth.columns:
        cols["f_eps_growth_yoy"] = growth["ni_growth_yoy"]
    if not cols:
        return pd.DataFrame(columns=["f_report_avail"])

    f = pd.DataFrame(cols)
    f.index = avail  # re-key statements (period_end rows) onto their avail dates
    f = f.dropna(how="all")
    f = f[~f.index.duplicated(keep="last")].sort_index()
    f["f_report_avail"] = 1.0
    return f


def earnings_features(ticker: str) -> pd.DataFrame:
    """Point-in-time earnings-event features, indexed by event date:
        f_days_since_earnings  (0 at the event), f_last_surprise_pct,
        f_surprise_mean_4, f_earnings_avail.
    """
    ev = pit_earnings(ticker)
    if ev.empty:
        return pd.DataFrame(columns=["f_earnings_avail"])
    ev = ev.sort_index()
    out = pd.DataFrame(index=ev.index)
    out["f_last_earnings_ts"] = ev.index
    out["f_days_since_earnings"] = 0.0
    if "surprise_pct" in ev.columns:
        out["f_last_surprise_pct"] = ev["surprise_pct"]
        out["f_surprise_mean_4"] = ev["surprise_pct"].rolling(4, min_periods=1).mean()
    out["f_earnings_avail"] = 1.0
    return out


# ---------------------------------------------------------------------------
# audit wiring
# ---------------------------------------------------------------------------

def _audit_fundamentals(audit, ticker: str, stmts: pd.DataFrame,
                        earnings_blob: dict | None, _filing) -> None:
    rows = []
    now = datetime.utcnow().isoformat(timespec="seconds")
    for period_end, row in stmts.iterrows():
        rows.append((ticker, "fundamental", period_end, "yfinance",
                     {"period_end": str(period_end),
                      "revenue": _safe(row.get("revenue"))}, now))
    if earnings_blob:
        ev = _from_json(earnings_blob)
        for event_date, row in ev.iterrows():
            rows.append((ticker, "earnings", event_date, "yfinance",
                         {"reported_eps": _safe(row.get("reported_eps")),
                          "surprise_pct": _safe(row.get("surprise_pct"))}, now))
    audit.record_many(rows)


def _safe(x) -> str:
    return "" if x is None or (isinstance(x, float) and np.isnan(x)) else str(x)

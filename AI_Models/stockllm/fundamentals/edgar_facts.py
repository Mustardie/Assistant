"""EDGAR companyfacts: exact quarterly facts with their original filing dates.

For US tickers this is the authoritative point-in-time fundamental source:
every XBRL fact is recorded here with the filing that first reported it
(`filed`).  We keep, per period end, the *first filed* value (as originally
reported -- no restatement look-ahead), and treat `filed` as the availability
date.

The per-ticker payload is cached gzip-compressed under data/edgar/.
"""
from __future__ import annotations

import gzip
import json
import urllib.request
from pathlib import Path

import pandas as pd

from config import DATA_DIR
from utils.logging import get_logger

log = get_logger(__name__)

EDGAR_DIR = DATA_DIR / "edgar"
EDGAR_DIR.mkdir(parents=True, exist_ok=True)
_UA = {"User-Agent": "StockLLM Research mailto:stockllm-research@example.com"}

FACTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet", "TotalRevenues"],
    "net_income": ["NetIncomeLoss"],
    "diluted_eps": ["EarningsPerShareDiluted"],
    "gross_profit": ["GrossProfit"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"],
    "debt_cur": ["DebtInstrumentCurrent"],
    "debt_lt": ["LongTermDebtNoncurrent"],
    "equity": ["StockholdersEquity"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
}
DURATION_FACTS = {"revenue", "net_income", "diluted_eps", "gross_profit", "ocf", "capex"}


def _cache_path(cik: int) -> Path:
    return EDGAR_DIR / f"cik{cik:010d}.json.gz"


def fetch_companyfacts(cik: int, force: bool = False) -> dict | None:
    """Download (once, gzip-cached) the companyfacts payload for a CIK."""
    path = _cache_path(cik)
    if path.exists() and not force:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
    except Exception as exc:
        log.warning("EDGAR companyfacts CIK %s unavailable: %s", cik, str(exc)[:100])
        return None
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    log.info("cached EDGAR companyfacts CIK %s", cik)
    return payload


def _usd_entries(fact_units: dict, is_duration: bool) -> list[dict]:
    entries = fact_units.get("USD", [])
    out = []
    for e in entries:
        start, end = e.get("start"), e.get("end")
        if is_duration and start is None:
            continue
        if not is_duration and start is not None and start != end:
            continue  # point-in-time facts must have zero duration
        if e.get("fp") not in ("FY", "Q1", "Q2", "Q3", "Q4"):
            continue
        if e.get("filed") is None or e.get("val") is None:
            continue
        out.append(e)
    return out


def facts_table(cik: int, force: bool = False) -> pd.DataFrame:
    """Quarterly facts as a tidy frame (period_end, filed, fact, value).

    Per period end, the first-filed value wins (as originally reported).
    """
    payload = fetch_companyfacts(cik, force=force)
    if payload is None:
        return pd.DataFrame()
    us_gaap = payload.get("facts", {}).get("us-gaap", {})
    rows = []
    for fact_name, keys in FACTS.items():
        is_dur = fact_name in DURATION_FACTS
        chosen = next((k for k in keys if k in us_gaap), None)
        if chosen is None:
            continue
        entries = _usd_entries(us_gaap[chosen].get("units", {}), is_dur)
        seen: dict[str, tuple] = {}
        for e in sorted(entries, key=lambda x: x["filed"]):
            key = e["end"]
            if key not in seen:  # first filed = as originally reported
                seen[key] = (e["filed"], e["val"])
        for end, (filed, val) in seen.items():
            rows.append({"period_end": end, "filed": filed,
                         "fact": fact_name, "value": val})
    return pd.DataFrame(rows)


def facts_pivot(cik: int, force: bool = False) -> pd.DataFrame:
    """Wide quarterly table: period_end x facts, plus first-filed date per row.

    Only quarter-end periods reported in a quarterly/annual filing are kept
    (fp Q1..Q4).  `avail_date` is the filing date of the first report for the
    most recent fact in that period (approx: statement-level availability).
    """
    df = facts_table(cik, force=force)
    if df.empty:
        return df
    wide = df.pivot_table(index="period_end", columns="fact", values="value",
                          aggfunc="first")
    avail = df.groupby("period_end")["filed"].min()
    wide["avail_date"] = avail
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()
    return wide

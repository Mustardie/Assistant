"""Natural-language -> structured intent parsing (JARVIS-facing).

The stockllm CLI exposes `python main.py jarvis --request "..."`; this
module turns free-form requests into a structured Intent that the CLI
then executes.  The parser is deterministic (no LLM): ticker aliases,
horizon phrases and interval phrases are matched by table lookup and
regexes.

Supported actions:

  * predict     "what do you think about NVIDIA" / "forecast TCS for 2 weeks"
  * track       "track RELIANCE every 15 minutes" / "monitor NVIDIA"
  * untrack     "stop tracking AAPL"
  * watchlist   "what am I tracking" / "show my watchlist"
  * tracking    "how are my predictions doing" / "tracking report"
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import config
from marketdata.horizon import Horizon, parse_interval_minutes

# Common names -> exchange symbols.  Explicit symbols (NVDA, RELIANCE.NS)
# pass through as-is; the default universe needs no suffix.
TICKER_ALIASES = {
    "RELIANCE": "RELIANCE.NS", "RIL": "RELIANCE.NS", "TATA CONSULTANCY": "TCS.NS",
    "TCS": "TCS.NS", "HDFC": "HDFCBANK.NS", "HDFCBANK": "HDFCBANK.NS",
    "HDFC BANK": "HDFCBANK.NS", "INFY": "INFY.NS", "INFOSYS": "INFY.NS",
    "SBIN": "SBIN.NS", "SBI": "SBIN.NS", "STATE BANK": "SBIN.NS",
    "ITC": "ITC.NS",
    "APPLE": "AAPL", "MICROSOFT": "MSFT", "NVIDIA": "NVDA",
    "GOOGLE": "GOOGL", "ALPHABET": "GOOGL", "AMAZON": "AMZN",
    "TESLA": "TSLA",
}

# Multi-word aliases must be matched before single tokens.
_MULTIWORD_ALIASES = {k: v for k, v in TICKER_ALIASES.items() if " " in k}
_SINGLE_ALIASES = {k: v for k, v in TICKER_ALIASES.items() if " " not in k}

_ACTION_KEYWORDS = {
    "predict": ("predict", "forecast", "outlook", "what do you think",
                "what do u think", "will ", "do you think", "estimate",
                "where is", "where's"),
    "track": ("track", "monitor", "watch it", "keep an eye", "poll"),
    "untrack": ("untrack", "stop tracking", "remove from watchlist",
                "stop monitoring", "cancel tracking"),
    "watchlist": ("watchlist", "what am i tracking", "what am I tracking",
                  "show my stocks", "list my stocks", "what do i track"),
    "tracking": ("tracking report", "how are my predictions", "prediction report",
                 "how are my calls", "ledger", "outcomes"),
}

_HORIZON_HINTS = ("horizon", "for the next", "over the next", "ahead",
                  "looking", "window", "next week", "next month",
                  "week", "month", "day")


@dataclass
class Intent:
    """Structured request produced by :func:`parse_request`."""
    action: str                    # predict | track | untrack | watchlist | tracking
    tickers: list[str] = field(default_factory=list)
    horizon: str | None = None     # canonical label like "7d"
    horizon_days: int | None = None
    interval_min: int | None = None
    error: str | None = None       # parse issue (action defaults to predict)
    raw: str = ""


class IntentError(Exception):
    """The request cannot be interpreted (raised with a user-facing message)."""


def resolve_ticker(token: str) -> str | None:
    """Map a spoken/typed ticker token to a symbol, or None.

    Bare tokens only resolve when they are a known alias or part of the
    default universe; explicit exchange-qualified symbols (NVDA.NS,
    FOO.BAR) pass through.  Arbitrary words never become symbols.
    """
    token = (token or "").strip().upper()
    if not token:
        return None
    if token in _SINGLE_ALIASES:
        return _SINGLE_ALIASES[token]
    if token in _MULTIWORD_ALIASES:
        return _MULTIWORD_ALIASES[token]
    if token in config.DEFAULT_TICKERS:
        return token
    if "." in token and re.fullmatch(r"[A-Z0-9.]+", token) and len(token) <= 10:
        return token
    return None


_STOP_TOKENS = {
    "A", "AN", "THE", "I", "FOR", "OF", "TO", "EVERY", "MIN", "MINUTES",
    "HOUR", "HOURS", "DAY", "DAYS", "WEEK", "WEEKS", "MONTH", "MONTHS",
    "NEXT", "PREDICT", "FORECAST", "TRACK", "MONITOR", "STOP", "REPORT",
    "SHOW", "MY", "WHAT", "HOW", "DO", "ARE", "IS", "WILL", "IT", "UP",
    "DOWN", "GO", "DOING", "WITH", "ABOUT", "PRICE", "STOCK", "STOCKS",
    "SHARE", "SHARES", "THINK", "OVER", "FROM", "ON", "AT", "IN", "BE",
    "AFTER", "BEFORE", "MORE", "LESS", "OR", "AND", "YOU", "ME", "US",
    "TELL", "LOOKING", "LOOK", "BUY", "SELL", "POSITION",
    "PREDICTIONS", "FORECASTS", "OUTLOOK", "ESTIMATE", "OPINION",
    "THOUGHTS", "WATCHLIST", "TRACKING", "LEDGER", "OUTCOMES",
    "PERFORMANCE", "STATUS", "SIGNAL", "SIGNALS", "MOMENTUM", "TREND",
}


def _extract_tickers(text: str) -> list[str]:
    """Pull ticker mentions out of free text (in order of appearance)."""
    found: list[str] = []
    upper = " " + text.upper() + " "
    # multi-word aliases first (longest first); their spans are consumed
    consumed: list[tuple[int, int]] = []
    for alias in sorted(_MULTIWORD_ALIASES, key=len, reverse=True):
        start = upper.find(" " + alias + " ")
        if start >= 0:
            found.append(_MULTIWORD_ALIASES[alias])
            consumed.append((start + 1, start + 1 + len(alias)))

    def in_span(pos: int) -> bool:
        return any(a <= pos < b for a, b in consumed)

    # token scan for single aliases and bare symbols
    for match in re.finditer(r"[A-Z0-9]+(?:\.[A-Z]+)?", upper):
        token = match.group(0)
        if in_span(match.start()):
            continue
        if token.isdigit() or (token[:1].isdigit()
                               and not token.endswith((".NS", ".BO", ".NSE"))):
            continue
        if token in _STOP_TOKENS:
            continue
        symbol = resolve_ticker(token)
        if symbol and symbol not in found:
            found.append(symbol)
    return found


def _extract_horizon(text: str) -> tuple[str | None, int | None]:
    for token in _HORIZON_HINTS:
        if token in text.lower():
            break
    else:
        return None, None
    try:
        h = Horizon.parse(text)
    except ValueError:
        return None, None
    return h.label, h.trading_days


def _extract_interval(text: str) -> int | None:
    match = re.search(
        r"every\s+(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h)\b", text, re.IGNORECASE)
    if not match:
        match = re.search(r"(\d+)\s*min(?:ute)?\b", text, re.IGNORECASE)
    if not match:
        return None
    return parse_interval_minutes(f"{match.group(1)}{match.group(2)}")


def parse_request(text: str) -> Intent:
    """Parse free-form text into an Intent.

    Raises IntentError only when the action cannot be determined at all;
    a predict intent with no tickers falls back to the default universe.
    """
    text = (text or "").strip()
    if not text:
        raise IntentError("empty request -- tell me what you want to know "
                          "(e.g. 'what do you think about NVIDIA')")
    lowered = text.lower()

    action = "predict"
    for candidate, keywords in _ACTION_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            action = candidate  # later candidates win (untrack > track > predict)

    tickers = _extract_tickers(text)
    if action == "predict" and not tickers:
        tickers = list(config.DEFAULT_TICKERS[:3])
    horizon_label, horizon_days = _extract_horizon(text)
    interval = _extract_interval(text)
    if interval is None and action == "track":
        interval = config.DEFAULT_TRACK_INTERVAL_MIN

    if action == "untrack" and not tickers:
        raise IntentError("which stock should I stop tracking? "
                          "(e.g. 'stop tracking AAPL')")
    if action == "track" and not tickers:
        raise IntentError("which stock should I track? (e.g. 'track NVDA')")
    if action in ("track", "untrack") and len(tickers) > 5:
        raise IntentError("too many tickers in one request -- name up to 5")

    return Intent(action=action, tickers=tickers,
                  horizon=horizon_label, horizon_days=horizon_days,
                  interval_min=interval, raw=text)


def describe(intent: Intent) -> str:
    """Human-readable description of an intent (for confirmation/echo)."""
    tickers = ", ".join(intent.tickers) or "the default universe"
    if intent.action == "predict":
        horizon = f" over {intent.horizon}" if intent.horizon else " (7d default)"
        return f"forecast for {tickers}{horizon}"
    if intent.action == "track":
        every = (f" every {intent.interval_min} min" if intent.interval_min else "")
        horizon = f" horizon {intent.horizon}" if intent.horizon else ""
        return f"track {tickers}{every}{horizon}"
    if intent.action == "untrack":
        return f"stop tracking {tickers}"
    if intent.action == "watchlist":
        return "show the monitoring watchlist"
    if intent.action == "tracking":
        return "prediction tracking report"
    return f"unknown action {intent.action!r}"

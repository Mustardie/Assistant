"""StockLLM specialist for Jarvis: the forecasting CLI as library tools.

AI_Models/stockllm is a self-contained forecasting project (its own
`config` module, its own venv-independent deps already installed in
Jarvis's Python).  Importing it in-process would collide with Jarvis's own
`config` package, so this module shells out to its CLI:

    python main.py predict --ticker NVDA --horizon 3d --json --no-llm
    python main.py track --tickers NVDA --interval 10 --horizon 7d --once
    python main.py untrack --tickers NVDA
    python main.py watchlist
    python main.py tracking-report
    python main.py info

Every tool returns a dict the planner can present directly: the predict
tool returns the parsed forecast JSON (numeric forecast, probabilities,
drivers, report text) so Jarvis can summarize it in its own voice.

The CLI is read-only except for the explicit watchlist-management tools
(stockllm_track / stockllm_untrack), which only add/remove watchlist
entries and run one monitor cycle -- never a blocking loop.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_STOCKLLM_DIR = Path(__file__).resolve().parents[1] / "AI_Models" / "stockllm"

_TOOL_TIMEOUT_S = 120  # predict can rebuild matrices; watchlist ops are fast


class StockLLMError(Exception):
    """The stockllm CLI cannot be run (missing project, python, or crash)."""


def _pick_python() -> str:
    """A python that can import stockllm's deps (pandas etc.).

    Preference: the project's own venv (self-contained), then the
    interpreter that runs this tool (Jarvis's venv may lack pandas), then
    any 'python' on PATH.  Probed once and cached.
    """
    if _pick_python.cached:
        return _pick_python.cached
    candidates = []
    venv_py = _STOCKLLM_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_py.is_file():
        candidates.append(str(venv_py))
    candidates.append(sys.executable)
    candidates.append("python")
    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import pandas"], capture_output=True,
                text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            _pick_python.cached = candidate
            return candidate
    raise StockLLMError(
        "no Python with pandas found for the stockllm CLI "
        f"(tried: {', '.join(candidates)})")


_pick_python.cached = None  # type: ignore[attr-defined]


def _run_cli(args: list[str], timeout: int = _TOOL_TIMEOUT_S) -> str:
    if not _STOCKLLM_DIR.is_dir():
        raise StockLLMError(f"StockLLM project not found at {_STOCKLLM_DIR}")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [_pick_python(), "main.py", *args],
            cwd=str(_STOCKLLM_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise StockLLMError(f"stockllm {' '.join(args)} timed out after {timeout}s")
    except OSError as exc:
        raise StockLLMError(f"cannot run stockllm CLI: {exc}")
    logger.info("stockllm %s finished in %.1fs (rc=%s)",
                " ".join(args[:2]), time.monotonic() - started, proc.returncode)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise StockLLMError(
            f"stockllm {' '.join(args[:2])} failed (rc={proc.returncode}): "
            f"{message[-1] if message else 'no output'}")
    return proc.stdout.strip()


def _guard(ticker: str, horizon: str | None) -> None:
    if not ticker or not ticker.strip():
        raise StockLLMError("Missing argument 'ticker' -- provide a symbol "
                            "(e.g. 'NVDA', 'RELIANCE.NS').")
    if horizon and not str(horizon).strip():
        raise StockLLMError("Empty horizon -- pass e.g. '7d', '3d', '2 weeks'.")


def stockllm_predict(ticker: str = None, horizon: str = None) -> dict:
    """Forecast one ticker: direction, probabilities, expected return,
    drivers, and the model's own report text. horizon is optional
    ('7d' default; also accepts '3d', '2 weeks', '1 month', ...)."""
    _guard(ticker, horizon)
    args = ["predict", "--ticker", ticker.strip().upper(), "--json", "--no-llm"]
    if horizon:
        args += ["--horizon", str(horizon).strip()]
    try:
        raw = _run_cli(args)
    except StockLLMError as exc:
        return {"success": False, "error": str(exc)}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"success": False, "error": f"unparseable CLI output: {raw[:200]}"}
    forecast = payload.get("forecast", {})
    return {
        "success": True,
        "ticker": ticker.strip().upper(),
        "horizon": forecast.get("horizon"),
        "direction": forecast.get("direction"),
        "prob_up": forecast.get("prob_up"),
        "expected_return": forecast.get("expected_return"),
        "drivers": forecast.get("drivers"),
        "report": payload.get("report"),
        "raw": payload,
    }


def stockllm_track(ticker: str = None, interval_min: int = None,
                   horizon: str = None) -> dict:
    """Add a ticker to the monitoring watchlist and run ONE prediction
    cycle for it. interval_min: minutes between cycles (default 10,
    clamped 1-1440). horizon: prediction horizon (default '7d')."""
    _guard(ticker, horizon)
    args = ["track", "--tickers", ticker.strip().upper(), "--once"]
    if interval_min is not None:
        try:
            args += ["--interval", str(int(interval_min))]
        except (TypeError, ValueError):
            return {"success": False,
                    "error": "interval_min must be a whole number of minutes."}
    if horizon:
        args += ["--horizon", str(horizon).strip()]
    try:
        out = _run_cli(args)
    except StockLLMError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "ticker": ticker.strip().upper(),
            "action": "track", "output": out}


def stockllm_untrack(ticker: str = None) -> dict:
    """Remove a ticker from the monitoring watchlist."""
    _guard(ticker, None)
    try:
        out = _run_cli(["untrack", "--tickers", ticker.strip().upper()])
    except StockLLMError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "ticker": ticker.strip().upper(),
            "action": "untrack", "output": out}


def stockllm_watchlist() -> dict:
    """Show the current monitoring watchlist (tracked tickers, intervals,
    horizons) and the saved-report watchlist."""
    try:
        out = _run_cli(["watchlist"])
    except StockLLMError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "output": out}


def stockllm_tracking_report() -> dict:
    """Prediction ledger: every recorded forecast with its outcome, win
    rate, and open (unresolved) predictions."""
    try:
        out = _run_cli(["tracking-report"])
    except StockLLMError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "output": out}


def stockllm_status() -> dict:
    """StockLLM system status: registered model horizons, data coverage,
    and config overview."""
    try:
        out = _run_cli(["info"])
    except StockLLMError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "output": out}

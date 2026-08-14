"""Prompt construction for the Qwen reasoning layer.

Every prompt enforces the same discipline:
  * forecasts are probabilistic hypotheses, never guarantees;
  * the LLM reasons over provided numbers, it does not invent data;
  * web-sourced information is flagged as unverified;
  * no buy/sell recommendations, ever.
"""
from __future__ import annotations

SYSTEM_CORE = (
    "You are StockLLM, a conservative financial-analysis assistant embedded in "
    "JARVIS. You NEVER give financial advice with certainty. All forecasts are "
    "probabilistic hypotheses derived from historical patterns; markets are not "
    "predictable with guarantees. You never claim profits are assured, never "
    "recommend buying or selling, and always state what could invalidate a "
    "forecast. You treat web sources as unverified and say so explicitly. "
    "You reason over the numbers you are given and never invent data."
)


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def build_analysis_prompt(ticker: str, forecast, digest: str,
                          news: list[dict] | None = None,
                          outcome_note: str = "") -> str:
    f = forecast
    lines = [
        f"Stock: {ticker}",
        f"As of: {f.as_of_date}",
        f"Current price: {f.price:,.2f}",
        f"7-day expected return: {_pct(f.expected_return)}",
        f"Probability of positive 7-day return: {f.prob_up * 100:.0f}%",
        f"Expected 7-day price range: {f.expected_range_lo:,.2f} - {f.expected_range_hi:,.2f}",
        f"Confidence: {f.confidence_level} (edge {f.confidence_value:.2f})",
        "",
        "Observable context (latest technical indicators):",
        digest,
    ]
    if news:
        lines += ["", "Web research items (UNVERIFIED, gathered by JARVIS browsing):"]
        for n in news:
            lines.append(
                f"- {n.get('title') or '(no title)'} | source: {n.get('source')} "
                f"| fetched: {n.get('fetched_at')} | relevance: {n.get('relevance')}"
            )
            body = (n.get("content") or "")[:400]
            lines.append(f"  {body}")
    if outcome_note:
        lines += ["", f"Realized outcome (historical review only): {outcome_note}"]
    lines += [
        "",
        "Write a concise analysis report with EXACTLY these sections:",
        "1. Interpretation: what the numbers suggest and why (use the indicators above).",
        "2. Main factors currently driving the view.",
        "3. Risks and what could invalidate the forecast.",
        "4. A clear disclaimer sentence.",
    ]
    return "\n".join(lines)


def build_daily_report_prompt(ticker: str, entry: dict, forecast, digest: str,
                              news: list[dict] | None = None) -> str:
    f = forecast
    saved = entry.get("forecast") or {}
    lines = [
        f"Daily report for {ticker}.",
        f"Saved to watchlist on {entry.get('added_on')} at price {entry.get('added_price'):,.2f} "
        f"(saved 7-day expectation: {_pct(saved.get('expected_return', 0.0))}, "
        f"P(up) {saved.get('prob_up', 0.0) * 100:.0f}%).",
        f"Now: price {f.price:,.2f} | expected return {_pct(f.expected_return)} | "
        f"P(up) {f.prob_up * 100:.0f}% | range {f.expected_range_lo:,.2f}-{f.expected_range_hi:,.2f} | "
        f"confidence {f.confidence_level}.",
        "Observable context:",
        digest,
    ]
    if news:
        lines += ["", "Web research items (UNVERIFIED):"]
        for n in news:
            lines.append(f"- {n.get('title') or ''} | {n.get('source')} | {n.get('fetched_at')} | relevance {n.get('relevance')}")
    lines += [
        "",
        "Produce a short daily report with EXACTLY these sections:",
        "1. Current situation (technical/fundamental reading of the indicators).",
        "2. News summary (mark clearly that sources are unverified).",
        "3. Forecast: 7-day expected return, probability, range, confidence.",
        "4. Why the forecast changed vs the saved one (if it did).",
        "5. Risk and what could invalidate it.",
        "6. One-line disclaimer.",
        "Use the neutral status language: HOLD / CONTINUE WATCHING, REVIEW, "
        "HIGH RISK, FORECAST IMPROVING, FORECAST DETERIORATING.",
    ]
    return "\n".join(lines)


def build_teacher_prompt(ticker: str, date: str, digest: str, outcome: float) -> str:
    return (
        f"Historical example: {ticker} on {date}.\n"
        f"Available indicators at that date:\n{digest}\n\n"
        f"The realized 7-trading-day return turned out to be {_pct(outcome)}.\n\n"
        "Write 2-4 sentences of financial reasoning a forecasting assistant "
        "could learn from: interpret the indicators, state what they suggested, "
        "and what risks were present. Use probabilistic language, no certainty, "
        "no buy/sell advice."
    )

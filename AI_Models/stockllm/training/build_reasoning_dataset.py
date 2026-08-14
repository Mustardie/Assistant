"""Synthetic reasoning-dataset builder for later Qwen fine-tuning.

Purpose (foundation, NOT executed in V1): produce JSONL examples that teach a
reasoning model to EXPLAIN forecasts.  Each example is built from a historical
(date, ticker) row:

  input  = the information available on that date (causal features + digest)
  output = an explanation whose conclusion is grounded in the *realized*
           outcome (known because the date is historical)

This grounds the LLM in fact rather than letting it invent outcomes.  When
Ollama is up, `--use-teacher-llm` asks a local Qwen to write the rationale
(distillation foundation); otherwise a deterministic template is used.

CRITICAL note for future use: this dataset is only ever applied to the
*historical* window.  A model trained on it must never be asked "what
happened" for a future date -- live usage keeps outcomes out of prompts.

Usage:  python main.py build-dataset --limit 2000 [--use-teacher-llm]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from config import DATASETS_DIR, LABEL_COLUMN, LLM_MAX_TOKENS
from inference.pipeline import digest_row
from llm import ollama_client, prompts
from marketdata.features import get_features
from utils.logging import get_logger

log = get_logger(__name__)


def _fill_response(row, digest: str, rationale: str) -> str:
    outcome = row[LABEL_COLUMN]
    direction = "up" if outcome > 0 else "down"
    return (
        f"The indicators available on {row['date'].date()} suggested the "
        f"following reasoning:\n\n{rationale}\n\n"
        f"Realized 7-trading-day outcome for {row['ticker']}: "
        f"{outcome * 100:+.2f}% ({direction}). "
        f"Treat every forecast as a probabilistic hypothesis; markets are not "
        f"guaranteed. Consider earnings, news and macro shocks as invalidation "
        f"risks not captured by these indicators."
    )


def build_examples(matrix: pd.DataFrame, limit: int = 1000, seed: int = 42,
                   use_teacher_llm: bool = False) -> list[dict]:
    df = matrix.dropna(subset=get_features() + [LABEL_COLUMN])
    sample = df.sample(n=min(limit, len(df)), random_state=seed)
    examples = []
    for _, row in sample.iterrows():
        digest = digest_row(row)
        if use_teacher_llm:
            prompt = prompts.build_teacher_prompt(
                row["ticker"], str(row["date"].date()), digest, row[LABEL_COLUMN]
            )
            rationale = ollama_client.generate(
                prompts.SYSTEM_CORE, prompt, max_tokens=300
            ) or "Indicators pointed to a directional bias with wide uncertainty."
        else:
            direction = "upward" if row[LABEL_COLUMN] > 0 else "downward"
            rationale = (
                f"The digest shows a {direction} technical bias with "
                f"uncertainty; the outcome below confirms or contradicts it, "
                f"which is exactly the signal an assistant should learn to "
                f"weigh carefully."
            )
        examples.append({
            "synthetic": True,
            "ticker": row["ticker"],
            "date": str(row["date"].date()),
            "outcome": float(row[LABEL_COLUMN]),
            "instruction": (
                f"Analyze {row['ticker']} as of {row['date'].date()} using only "
                f"the information below and produce a 7-trading-day forecast "
                f"with reasoning.\n\n{digest}"
            ),
            "response": _fill_response(row, digest, rationale),
        })
    return examples


def main(limit: int = 1000, seed: int = 42, use_teacher_llm: bool = False,
         matrix_path: str | None = None, out: str | None = None) -> str:
    from marketdata.features import load_matrix
    matrix = load_matrix(matrix_path)
    examples = build_examples(matrix, limit=limit, seed=seed,
                              use_teacher_llm=use_teacher_llm)
    out = out or str(DATASETS_DIR / "reasoning_train.jsonl")
    with open(out, "w", encoding="utf-8") as fh:
        for ex in examples:
            fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
    ups = sum(1 for e in examples if e["outcome"] > 0)
    log.info("wrote %d examples to %s (up %d / down %d)", len(examples), out,
             ups, len(examples) - ups)
    return out

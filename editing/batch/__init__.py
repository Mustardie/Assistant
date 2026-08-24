"""Batch mode: one configuration, many folders, one summary.

    schema.py    BatchConfig, BatchCandidate, BatchEntry, BatchSummary
    discover.py  which folders under a root hold footage
    run.py       the loop, and the four decisions it makes per folder
    store.py     where a batch's summary lives
    report.py    the readable summary

```
python -m editing.cli auto batch --root E:\\Clips --style cinematic_minecraft \\
    --director --retention-cut --render-proxy --no-premiere --limit 3
```

Sequential and deliberately unclever. No concurrency, no retries, no
dependency graph -- twenty folders in order, each producing an ordinary
hermetic run that every other command in this system already understands.

**One failure does not stop the batch.** A folder that raises is recorded with
its reason and the next one starts. The most useful property of an overnight
run is that it is still going in the morning.

**Nothing is ever overwritten.** A folder with a completed run is skipped;
``--force`` gives it a *new* run folder beside the old one. There is no path
through this package that writes over finished work.

**A dry run creates nothing** and says exactly which folders it would process,
which it would skip, and why -- which is the thing to type first.
"""
from editing.batch.discover import find_candidates, runs_for
from editing.batch.report import render, render_short
from editing.batch.run import DECISIONS, decide, run_batch, run_config_for
from editing.batch.schema import (
    ENTRY_STATUSES, SKIP_REASONS, BatchCandidate, BatchConfig, BatchEntry,
    BatchSummary, batch_id_for,
)
from editing.batch.store import (
    latest_batch_id, list_batches, load, load_or_none, save,
)

__all__ = [
    "BatchConfig", "BatchCandidate", "BatchEntry", "BatchSummary",
    "ENTRY_STATUSES", "SKIP_REASONS", "batch_id_for",
    "find_candidates", "runs_for",
    "run_batch", "decide", "run_config_for", "DECISIONS",
    "save", "load", "load_or_none", "list_batches", "latest_batch_id",
    "render", "render_short",
]

"""Reliability gates: fifteen checks on whether a run produced a usable thing.

    schema.py   GateInputs, GateResult, GateReport, the statuses and the names
    checks.py   the fifteen checks, one pure function each
    run.py      gathering what they read, and running them
    report.py   the readable report

Not to be confused with ``editing.auto.gates``, which is about *permission* --
what may be executed against Premiere. These are about *validity*: is what this
run produced something a person can use, and if not, which part is wrong.

**A warning never stops a run.** A gate fails only when the output is clearly
invalid -- no footage, a render that claims a video it does not have, a cut
with no runtime. Everything else warns with a fix attached, because a pipeline
that refuses to finish over a caption density is one nobody runs twice.

**Every gate carries evidence and a fix.** A gate that says "confidence is
low" without saying what it was, over how many words, is an opinion; and one
with no suggested fix is a complaint. Both fields are filled by every check.

**A gate about a pass that did not run reports ``skipped``.** "Captions are not
too dense" is not true of a run with no captions -- it is a question that does
not apply, and fifteen green ticks that mean nothing is worse than five.
"""
from editing.reliability.checks import CHECKS
from editing.reliability.report import render, render_short, summary_lines
from editing.reliability.run import check_run, collect, evaluate
from editing.reliability.schema import (
    GATE_NAMES, GATE_TITLES, NOT_A_QUALITY_JUDGEMENT, STATUSES, GateInputs,
    GateReport, GateResult, gate, skipped,
)

__all__ = [
    "GateInputs", "GateReport", "GateResult", "GATE_NAMES", "GATE_TITLES",
    "STATUSES", "NOT_A_QUALITY_JUDGEMENT", "gate", "skipped",
    "CHECKS", "evaluate", "collect", "check_run",
    "render", "render_short", "summary_lines",
]

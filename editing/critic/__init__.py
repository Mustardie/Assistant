"""The critic pass: look at the rough cut, then improve it once.

    rough cut plan
        -> review frames chosen by coverage rule, with their context
        -> Qwen3-VL critic, one frame at a time
        -> critic findings
        -> revision recommendations (safe ones carry draft operations)
        -> revision plan
        -> offline dry run
        -> (only on an explicit --yes) execution on the same scratch sequence

    schema.py   CriticFinding, RevisionRecommendation, RevisionPlan
    frames.py   which moments to look at, and the context to look at them with
    prompt.py   what the critic is asked
    critic.py   the model call, the coercion, and the mock
    revise.py   findings -> revisions: the safety rules live here
    plan.py     accepted revisions -> one ordered operation plan
    execute.py  the three execution modes and the guards around them
    report.py   the human-readable output

This pass is deliberately **one iteration**. It is meant to catch the obvious
mistakes an automatic assembly makes -- a zoom that crops the HUD, a caption
over the action, a beat cut a moment too early -- not to converge on a finished
edit. Running it twice is allowed; it is not designed to be run in a loop.

Two rules shape everything here:

* **A finding is not a fix.** What the critic saw and what the system proposes
  doing are different records, and the conversion between them is a set of
  explicit rules in ``revise.py``. Anything without a safe automatic form stays
  a recommendation with the reason attached.
* **The rough cut is never overwritten.** The critic report, the revisions and
  the revision plan are separate files. The baseline they are judging survives
  the judgement.
"""
from editing.critic.critic import (
    MockCritic, VisualCritic, build_critic, health, parse_response,
)
from editing.critic.execute import (
    ALLOWED_OPS, MODES, check_allowed, run, summarise,
    targets_scratch_sequence,
)
from editing.critic.execute import dry_run as dry_run_revisions
from editing.critic.frames import (
    CoverageOptions, enrich, plan_coverage_frames,
)
from editing.critic.plan import build_revision_plan
from editing.critic.report import render, render_issues
from editing.critic.revise import RevisionOptions, build_revisions
from editing.critic.schema import (
    FIXES, ISSUE_TYPES, REVISION_RISKS, REVISION_STATUSES, SAFE_FIXES,
    SEVERITIES, CriticFinding, CriticReport, NotApplied, RevisionPlan,
    RevisionRecommendation, RevisionSet, coerce_fix, coerce_issue,
    coerce_severity,
)

__all__ = [
    # schema
    "CriticFinding", "CriticReport", "RevisionRecommendation", "RevisionSet",
    "RevisionPlan", "NotApplied",
    "ISSUE_TYPES", "SEVERITIES", "FIXES", "SAFE_FIXES", "REVISION_RISKS",
    "REVISION_STATUSES",
    "coerce_issue", "coerce_severity", "coerce_fix",
    # frames
    "CoverageOptions", "plan_coverage_frames", "enrich",
    # critic
    "VisualCritic", "MockCritic", "build_critic", "parse_response", "health",
    # revise / plan
    "RevisionOptions", "build_revisions", "build_revision_plan",
    # execute
    "dry_run_revisions", "run", "targets_scratch_sequence", "check_allowed",
    "summarise", "MODES", "ALLOWED_OPS",
    # report
    "render", "render_issues",
]

"""Rough cut builder.

    StructureTimeline + recommendations
        -> selected source ranges
        -> computed sequence layout
        -> Premiere operations
        -> dry run
        -> (only on an explicit command) execution on a scratch sequence
        -> review frames

    schema.py   ClipPlacement, RoughCutPlan, ExecutionReport
    select.py   which ranges to keep, and where they land (pure)
    convert.py  layout + recommendations -> catalog operations
    execute.py  the four execution modes and the guards around them
    review.py   representative frames, traced back to recommendations

The chain **source file -> source range -> recommendation -> sequence position
-> operation** is preserved end to end, so every frame in the cut can be traced
back to the evidence that justified keeping it.

Nothing runs without an explicit execution mode, a dry run that passes in the
same call, and a plan that provably builds its own scratch sequence.
"""
from editing.roughcut.build import RoughCutOptions, build_rough_cut
from editing.roughcut.convert import build_ops
from editing.roughcut.execute import (
    MODES, ExecutionReport, dry_run, run, summarise, targets_scratch_sequence,
)
from editing.roughcut.review import (
    ReviewFrame, ReviewSet, export_frames, load_review, plan_frames, write_review,
)
from editing.roughcut.schema import (
    ClipPlacement, RoughCutPlan, SequenceMarker, Unconverted,
)
from editing.roughcut.select import (
    SelectedRange, assemble, coverage, map_to_sequence, select_ranges,
)

__all__ = [
    # schema
    "ClipPlacement", "RoughCutPlan", "SequenceMarker", "Unconverted",
    "ExecutionReport",
    # selection and layout
    "SelectedRange", "select_ranges", "assemble", "map_to_sequence", "coverage",
    # conversion
    "build_ops", "build_rough_cut", "RoughCutOptions",
    # execution
    "dry_run", "run", "targets_scratch_sequence", "summarise", "MODES",
    # review
    "ReviewFrame", "ReviewSet", "plan_frames", "export_frames", "write_review",
    "load_review",
]

"""Structured human review of an edit, collected for later learning.

Sessions 1-8 produce opinions about footage. This one records the editor's
opinion about those opinions, in a shape Session 10 can turn into a dataset.

```
artifacts (cut, layers, assets, episode, retention)
        |
        v
   review queue      what is actually worth asking about, ranked and grouped
        |
        v
   feedback.jsonl    append-only: rating + reason + correction + target
        |
        +--> preference signals   tendencies, with evidence and disagreement
        +--> training signals     one decision each, usable or explicitly not
        +--> exports              jsonl / json / csv, with a manifest
```

**It trains nothing.** No weights, no tuning, no pass changes behaviour because
of anything collected here. ``PreferenceSignal.safe_to_apply_automatically``
describes the evidence; it does not grant permission, and nothing reads it.

**It executes nothing.** No Premiere, no FFmpeg, no model, no footage. Every
input is JSON another pass already wrote.

**It never overwrites feedback.** The log is opened in append mode and nowhere
else; changing your mind appends a superseding item and both survive. See
``editing.feedback.store``.
"""
from editing.feedback.schema import (  # noqa: F401
    CORRECTION_ACTIONS, EXPORT_FORMATS, FeedbackCorrection, FeedbackExport,
    FeedbackItem, FeedbackRating, FeedbackReason, FeedbackSession,
    FeedbackTarget, NOT_MEASURED, POLARITY_FOR_RATING, PREFERENCE_DIMENSIONS,
    PreferenceSignal, PROMPT_FLAGS, RATINGS, RATING_GROUPS, REASON_CATEGORIES,
    ReviewPrompt, ReviewQueue, TARGET_TYPES, TASK_TYPES, TrainingSignal,
)
from editing.feedback.targets import Artifacts  # noqa: F401

__all__ = [
    "Artifacts", "CORRECTION_ACTIONS", "EXPORT_FORMATS", "FeedbackCorrection",
    "FeedbackExport", "FeedbackItem", "FeedbackRating", "FeedbackReason",
    "FeedbackSession", "FeedbackTarget", "NOT_MEASURED",
    "POLARITY_FOR_RATING", "PREFERENCE_DIMENSIONS", "PreferenceSignal",
    "PROMPT_FLAGS", "RATINGS", "RATING_GROUPS", "REASON_CATEGORIES",
    "ReviewPrompt", "ReviewQueue", "TARGET_TYPES", "TASK_TYPES",
    "TrainingSignal",
]

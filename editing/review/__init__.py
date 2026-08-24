"""The review package: one folder per run, with an index that reads top down.

    schema.py   ReviewItem, ReviewPackage
    build.py    gathering a run into a package, and writing it
    index.py    review_index.md, and the short terminal form
    store.py    where a package lives, inside the run it is about

A run leaves six sub-directories and forty files behind. Knowing that the
retention comparison lives in ``artifacts/retention/<name>.compare.json`` is a
thing a person has to learn, and this package exists so they do not have to:
the small readable things are copied in beside an index, the video is pointed
at, and the index answers the five questions in the order they get asked --
what was produced, what changed, what to watch for, where it is weak, and what
needs a human decision.

**A package is a view, not a record.** It is rebuilt from the run whenever it
is asked for, so ``review package --run <id>`` after a re-render says what is
true now. It lives inside the run folder because it is about that run, and
deleting the run deletes it -- which is the right coupling for a view over
artifacts that would no longer exist.

**Nothing in it is a verdict.** It says what was done, what was refused and
what is worth checking. Whether the edit works is a question only somebody
watching it can answer.
"""
from editing.review.build import build_package, write_package
from editing.review.index import render_index, render_summary
from editing.review.schema import NOT_A_VERDICT, ReviewItem, ReviewPackage
from editing.review.store import (
    index_path, latest_with_package, load_package, package_dir,
    package_or_none, package_path, save_package,
)

__all__ = [
    "ReviewPackage", "ReviewItem", "NOT_A_VERDICT",
    "build_package", "write_package",
    "render_index", "render_summary",
    "package_dir", "package_path", "index_path", "save_package",
    "load_package", "package_or_none", "latest_with_package",
]

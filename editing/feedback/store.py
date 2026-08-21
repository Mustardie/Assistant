"""Where feedback lives on disk, and why it is only ever added to.

One folder per session, under ``data/editing/feedback/sessions/<session_id>/``:

```
session.json     what the session is about, and what existed when it started
queue.json       the review queue, as generated
feedback.jsonl   every rating ever given, one JSON object per line, appended
summary.json     derived: counts, preference signals, training signals
report.md        derived: the human-readable version of summary.json
exports/         whatever was exported, plus a manifest per export
```

**Only ``feedback.jsonl`` is the record.** Everything else in that folder is
either metadata (``session.json``) or derived and regenerated on demand
(``summary.json``, ``report.md``). If the derived files were lost, running
``feedback report`` would rebuild them exactly; if the log were lost, the
review would have to happen again. That asymmetry is the reason for the split.

## Append-only, structurally

``append`` is the only function in this module that writes to the log, and it
opens the file in ``"a"`` mode. There is no update, no delete, and no rewrite:

* changing your mind appends a new item whose ``supersedes`` names the old one;
* ``read_all`` returns the whole history in the order it was written;
* ``read_current`` walks the supersede chains and returns what stands now.

So the current state is a *view*, computed on read, and the history is the
file. A corrupt or half-written line is skipped and reported in
``read_problems`` rather than aborting the read -- losing one line of a review
should not cost the other forty.

**A session is never silently reused.** ``create`` refuses an ID that already
exists, because the alternative is a second afternoon's review quietly landing
in the first afternoon's file with no way to tell them apart.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from editing.config import EditingConfig
from editing.errors import EditingError
from editing.feedback.schema import (
    FeedbackExport, FeedbackItem, FeedbackSession, ReviewQueue, new_id,
)
from editing.schema import short_hash

SESSION_FILE = "session.json"
QUEUE_FILE = "queue.json"
LOG_FILE = "feedback.jsonl"
SUMMARY_FILE = "summary.json"
REPORT_FILE = "report.md"
EXPORTS_DIR = "exports"


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def sessions_root(config: EditingConfig) -> Path:
    return config.feedback_dir / "sessions"


def session_dir(config: EditingConfig, session_id: str) -> Path:
    return sessions_root(config) / session_id


def log_path(config: EditingConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / LOG_FILE


def queue_path(config: EditingConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / QUEUE_FILE


def summary_path(config: EditingConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / SUMMARY_FILE


def report_path(config: EditingConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / REPORT_FILE


def exports_dir(config: EditingConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / EXPORTS_DIR


def session_id_for(
    *, run_id: str = "", name: str = "structure",
    when: Optional[float] = None,
) -> str:
    """A session ID that is readable, sortable and effectively unique.

    ``<timestamp>-fb-<hash of run and timeline>``. The timestamp sorts, which
    is how "the latest session" is answered without a pointer file that could
    go stale; the hash groups the sessions that reviewed the same thing.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(when or time.time()))
    return f"{stamp}-fb-{short_hash(run_id or 'norun', name, length=6)}"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create(
    config: EditingConfig, session: FeedbackSession, *, force: bool = False
) -> Path:
    """Make the session folder and write ``session.json``. Refuses to clobber.

    ``force`` will reuse an existing *empty* folder -- one with no feedback in
    it yet -- and still refuses one that has any, because the whole point of
    this layer is that a review is not repeatable and must not be overwritten.
    """
    if not session.session_id:
        raise EditingError(
            "A feedback session needs an ID",
            hint="Use `store.session_id_for()` to build one.",
        )
    directory = session_dir(config, session.session_id)
    if directory.exists():
        existing = _count_lines(directory / LOG_FILE)
        if existing or not force:
            raise EditingError(
                f"Feedback session '{session.session_id}' already exists"
                + (f" and holds {existing} item(s)" if existing else ""),
                hint="Feedback is never overwritten. Add to it with "
                     f"`feedback rate --session {session.session_id} ...`, or "
                     "start a new session with `feedback start`.",
                detail={"session_id": session.session_id,
                        "items": existing, "path": str(directory)},
            )
    directory.mkdir(parents=True, exist_ok=True)
    exports_dir(config, session.session_id).mkdir(parents=True, exist_ok=True)
    # Create the log now, so "the session exists" and "the log exists" cannot
    # disagree and every later read has a file to open.
    log = directory / LOG_FILE
    if not log.exists():
        log.touch()
    save_session(config, session)
    return directory


def save_session(config: EditingConfig, session: FeedbackSession) -> Path:
    """Rewrite ``session.json``. Metadata only -- never feedback."""
    directory = session_dir(config, session.session_id)
    directory.mkdir(parents=True, exist_ok=True)
    session.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    target = directory / SESSION_FILE
    _write_json(target, session.to_dict())
    return target


def load_session(config: EditingConfig, session_id: str) -> FeedbackSession:
    target = session_dir(config, session_id) / SESSION_FILE
    if not target.exists():
        raise EditingError(
            f"No feedback session named '{session_id}'",
            hint="List them with `python -m editing.cli feedback list "
                 "--sessions`, or start one with `feedback start`.",
            detail={"path": str(target)},
        )
    return FeedbackSession.from_dict(_read_json(target))


def exists(config: EditingConfig, session_id: str) -> bool:
    return (session_dir(config, session_id) / SESSION_FILE).exists()


def list_sessions(config: EditingConfig, *, limit: int = 50) -> list[FeedbackSession]:
    """Every session, newest first. Unreadable ones are skipped, not raised."""
    root = sessions_root(config)
    if not root.exists():
        return []
    out: list[FeedbackSession] = []
    for directory in sorted(root.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        target = directory / SESSION_FILE
        if not target.exists():
            continue
        try:
            out.append(FeedbackSession.from_dict(_read_json(target)))
        except (ValueError, OSError):
            continue
        if len(out) >= limit:
            break
    return out


def latest_session(
    config: EditingConfig, *, run_id: str = "", open_only: bool = False
) -> Optional[FeedbackSession]:
    """The most recent session, optionally for one run.

    Derived from the sorted session IDs rather than from a pointer file: a
    pointer is one more thing that can be stale, and the IDs already sort.
    """
    for session in list_sessions(config):
        if run_id and session.run_id != run_id:
            continue
        if open_only and not session.is_open:
            continue
        return session
    return None


def resolve_session(
    config: EditingConfig, session_id: str = "", *, run_id: str = ""
) -> FeedbackSession:
    """The session a command should act on, or a clear error saying why not."""
    if session_id:
        return load_session(config, session_id)
    found = latest_session(config, run_id=run_id)
    if found is None:
        raise EditingError(
            "No feedback session to work with"
            + (f" for run '{run_id}'" if run_id else ""),
            hint="Start one with `python -m editing.cli feedback start"
                 + (f" --run {run_id}" if run_id else "") + "`.",
        )
    return found


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------

def append(
    config: EditingConfig, session_id: str, item: FeedbackItem
) -> FeedbackItem:
    """Add one item to the log. The only function here that writes feedback.

    Opens in ``"a"`` mode and flushes, so a crash mid-review loses at most the
    item being written. Returns the settled item, which is what the caller
    should print -- ``settle`` may have set ``needs_follow_up`` or cleared
    ``usable_for_training``, and the user needs to see that it did.
    """
    directory = session_dir(config, session_id)
    if not (directory / SESSION_FILE).exists():
        raise EditingError(
            f"No feedback session named '{session_id}' to append to",
            hint="Start one with `python -m editing.cli feedback start`.",
        )
    item.session_id = session_id
    item.settle()

    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True)
    with open(directory / LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return item


def append_many(
    config: EditingConfig, session_id: str, items: Iterable[FeedbackItem]
) -> list[FeedbackItem]:
    return [append(config, session_id, item) for item in items]


def read_all(config: EditingConfig, session_id: str) -> list[FeedbackItem]:
    """Every item ever written to this session, in the order it was written.

    Superseded items are included. Use ``read_current`` for what stands now.
    """
    items, _problems = _read_log(log_path(config, session_id))
    return items


def read_problems(config: EditingConfig, session_id: str) -> list[str]:
    """Lines that could not be parsed, with their line numbers."""
    _items, problems = _read_log(log_path(config, session_id))
    return problems


def read_current(config: EditingConfig, session_id: str) -> list[FeedbackItem]:
    """What stands now: the latest item in each supersede chain.

    Order follows first appearance, not last write, so re-rating an item does
    not make it jump to the bottom of a report the reviewer is reading top to
    bottom.
    """
    return current_of(read_all(config, session_id))


def current_of(items: list[FeedbackItem]) -> list[FeedbackItem]:
    """The ``read_current`` view over an already-loaded history.

    Two things supersede an earlier item: an explicit ``supersedes``, and a
    later rating of the same target within the same session. The second is
    what makes re-rating from the CLI work without the user having to quote an
    ID they never saw.
    """
    explicitly_replaced = {
        item.supersedes for item in items if item.supersedes
    }

    # Position is claimed by the *first* rating of a target, including one that
    # was later superseded; the surviving rating then takes that position. So
    # re-rating item one leaves it at item one, which is what someone working
    # down a list expects. Ordering by the surviving items alone would send it
    # to the bottom, and ordering by first *survivor* would do the same,
    # because the original is exactly the item that got replaced.
    order: list[str] = []
    latest: dict[str, FeedbackItem] = {}
    for item in items:
        key = item.target.key()
        if key not in order:
            order.append(key)
        if item.feedback_id not in explicitly_replaced:
            latest[key] = item      # later writes win, position does not move

    return [latest[key] for key in order if key in latest]


def history_of(
    items: list[FeedbackItem], target_key: str
) -> list[FeedbackItem]:
    """Every rating ever given about one target, oldest first."""
    return [item for item in items if item.target.key() == target_key]


def find_item(
    items: list[FeedbackItem], feedback_id: str
) -> Optional[FeedbackItem]:
    for item in items:
        if item.feedback_id == feedback_id:
            return item
    return None


def _read_log(path: Path) -> tuple[list[FeedbackItem], list[str]]:
    if not path.exists():
        return [], []
    items: list[FeedbackItem] = []
    problems: list[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as error:
                problems.append(f"line {number}: not valid JSON ({error.msg})")
                continue
            if not isinstance(data, dict):
                problems.append(f"line {number}: not a JSON object")
                continue
            try:
                items.append(FeedbackItem.from_dict(data))
            except (TypeError, ValueError) as error:  # noqa: PERF203
                problems.append(f"line {number}: unreadable item ({error})")
    return items, problems


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def count(config: EditingConfig, session_id: str) -> int:
    return _count_lines(log_path(config, session_id))


# ---------------------------------------------------------------------------
# The queue and the derived files
# ---------------------------------------------------------------------------

def write_queue(
    config: EditingConfig, session_id: str, queue: ReviewQueue
) -> Path:
    """Save the queue. Regenerating one is allowed; it holds no feedback.

    The previous queue is kept beside it as ``queue.<n>.json`` rather than
    discarded, because a rating references a ``prompt_id`` and a reader six
    months from now should be able to find the question that ID answered.
    """
    directory = session_dir(config, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / QUEUE_FILE
    if target.exists():
        index = 1
        while (directory / f"queue.{index}.json").exists():
            index += 1
        target.replace(directory / f"queue.{index}.json")
    _write_json(target, queue.to_dict())
    return target


def load_queue(config: EditingConfig, session_id: str) -> ReviewQueue:
    target = queue_path(config, session_id)
    if not target.exists():
        raise EditingError(
            f"Session '{session_id}' has no review queue yet",
            hint="Build one with `python -m editing.cli feedback queue "
                 f"--session {session_id}`.",
            detail={"path": str(target)},
        )
    return ReviewQueue.from_dict(_read_json(target))


def queue_or_none(
    config: EditingConfig, session_id: str
) -> Optional[ReviewQueue]:
    try:
        return load_queue(config, session_id)
    except EditingError:
        return None


def find_prompt(config: EditingConfig, session_id: str, prompt_id: str):
    """One prompt from the current queue, or from a superseded one.

    Older queues are searched too: a prompt ID that was answered yesterday
    still has to resolve after the queue is regenerated, or the log ends up
    full of references to questions nobody can read back.
    """
    directory = session_dir(config, session_id)
    if not directory.exists():
        return None
    candidates = [directory / QUEUE_FILE]
    candidates += sorted(directory.glob("queue.*.json"), reverse=True)
    for path in candidates:
        if not path.exists():
            continue
        try:
            queue = ReviewQueue.from_dict(_read_json(path))
        except (ValueError, OSError):
            continue
        found = queue.prompt(prompt_id)
        if found is not None:
            return found
    return None


def write_summary(config: EditingConfig, session_id: str, summary: dict) -> Path:
    target = summary_path(config, session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target, summary)
    return target


def load_summary(config: EditingConfig, session_id: str) -> dict:
    target = summary_path(config, session_id)
    return _read_json(target) if target.exists() else {}


def write_report(config: EditingConfig, session_id: str, text: str) -> Path:
    target = report_path(config, session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def write_export(
    config: EditingConfig,
    session_id: str,
    *,
    body: str,
    record: FeedbackExport,
    filename: str = "",
) -> tuple[Path, FeedbackExport]:
    """Write an export and its manifest, and fill in the manifest's fields.

    Exports are never overwritten either -- a numbered suffix is added instead.
    Two exports of the same session with different filters are two different
    datasets, and silently replacing one with the other is how a dataset builder
    ends up training on something nobody meant.
    """
    directory = exports_dir(config, session_id)
    directory.mkdir(parents=True, exist_ok=True)

    stem = filename or f"feedback.{record.format}"
    target = directory / stem
    if target.exists():
        index = 1
        while (directory / f"{target.stem}.{index}{target.suffix}").exists():
            index += 1
        target = directory / f"{target.stem}.{index}{target.suffix}"

    data = body.encode("utf-8")
    target.write_bytes(data)

    record.path = str(target)
    record.bytes_written = len(data)
    record.checksum = hashlib.sha256(data).hexdigest()[:32]
    if not record.export_id:
        record.export_id = new_id("ex", session_id, record.checksum)
    if not record.created_at:
        record.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    _write_json(
        directory / f"{target.stem}.manifest.json", record.to_dict()
    )
    return target, record


def list_exports(config: EditingConfig, session_id: str) -> list[FeedbackExport]:
    directory = exports_dir(config, session_id)
    if not directory.exists():
        return []
    out: list[FeedbackExport] = []
    for path in sorted(directory.glob("*.manifest.json")):
        try:
            out.append(FeedbackExport.from_dict(_read_json(path)))
        except (ValueError, OSError):
            continue
    return out


# ---------------------------------------------------------------------------
# JSON, in one place
# ---------------------------------------------------------------------------

def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    # Written via a temp file in the same directory so a crash cannot leave a
    # half-written session.json where a valid one used to be.
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}

"""Skill planner: matching, request tracking and automatic suggestions.

This module answers three questions:

1. "Which skill does this request mean?"  -- find_skill()
2. "Should this request auto-run a skill?" -- auto_play_skill() (used by
   the agent loop before planning: 'generate my monthly report' should run
   the recorded 'Monthly Report' skill instead of rebuilding the workflow)
3. "Should Nova suggest learning a skill?" -- observe_request() (Phase 5:
   the user repeats the same task multiple times -> offer to record it)

Request-frequency stats live under the skills root's hidden .nova folder
(never in the project directory).
"""

import logging
import re
from datetime import datetime, timedelta

from config.settings import settings
from skills import storage

logger = logging.getLogger(__name__)

_MIN_REQUEST_LENGTH = 6
_RESUGGEST_AFTER_DAYS = 7


def _fuzz_ratio(a: str, b: str) -> float:
    try:
        from rapidfuzz import fuzz

        return float(fuzz.token_set_ratio(a, b)) / 100.0
    except Exception:
        import difflib

        return difflib.SequenceMatcher(None, a, b).ratio()


def _normalize_request(request: str) -> str:
    text = re.sub(r"[^\w\s]", " ", (request or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text[:80]


def _skill_text(record: dict) -> str:
    parts = [
        record.get("name", ""),
        record.get("description", ""),
        *record.get("tags", []),
    ]
    return " ".join(str(p) for p in parts if p)


def _best_match(request: str, skills: list[dict]) -> tuple[dict | None, float]:
    normalized = _normalize_request(request)
    best, best_score = None, 0.0
    for record in skills:
        score = _fuzz_ratio(normalized, _normalize_request(_skill_text(record)))
        if score > best_score:
            best, best_score = record, score
    return best, best_score


def find_skill(request: str, threshold: float = 0.55) -> dict | None:
    """Best-matching skill for a request, or None."""
    best, best_score = _best_match(request, storage.list_skills())
    if best is None or best_score < threshold:
        return None
    best["match_score"] = round(best_score, 3)
    return best


def auto_play_skill(request: str) -> dict | None:
    """High-confidence skill match that should run instead of the planner
    rebuilding the workflow from scratch."""
    return find_skill(request, threshold=settings.skills_auto_play_threshold)


def rank_skills(request: str) -> list[dict]:
    """All skills sorted by match confidence (for 'show my skills'-style
    clarification and for offering close matches)."""
    normalized = _normalize_request(request)
    ranked = []
    for record in storage.list_skills():
        score = _fuzz_ratio(normalized, _normalize_request(_skill_text(record)))
        ranked.append((score, record))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [dict(record, match_score=round(score, 3)) for score, record in ranked]


def observe_request(request: str) -> str | None:
    """Record that the user asked for this, and return a learning
    suggestion when the same task has been repeated enough times
    (Phase 5). Returns None when nothing worth suggesting."""
    normalized = _normalize_request(request)
    if len(normalized) < _MIN_REQUEST_LENGTH:
        return None

    stats = storage.request_stats()
    requests = stats.setdefault("requests", {})
    entry = requests.setdefault(normalized, {"count": 0, "first": None, "last": None})
    entry["count"] = entry.get("count", 0) + 1
    now = datetime.now().isoformat(timespec="seconds")
    prev_last = entry.get("last")
    entry["first"] = entry.get("first") or now
    entry["last"] = now
    storage.save_request_stats(stats)

    if find_skill(request):
        return None  # already have a skill -- nothing to suggest

    if entry.get("count", 0) < settings.skills_min_suggest_frequency:
        return None

    # Staleness is judged by the previous occurrence, not the one we just
    # recorded -- a task last seen weeks ago shouldn't be suggested just
    # because it was asked once today.
    try:
        if prev_last and datetime.now() - datetime.fromisoformat(prev_last) \
                > timedelta(days=settings.skills_suggest_window_days):
            return None
    except (KeyError, TypeError, ValueError):
        return None

    # Don't re-offer constantly -- only once per week per task.
    if entry.get("last_suggested"):
        try:
            if datetime.now() - datetime.fromisoformat(entry["last_suggested"]) < timedelta(days=_RESUGGEST_AFTER_DAYS):
                return None
        except (TypeError, ValueError):
            pass

    entry["last_suggested"] = datetime.now().isoformat(timespec="seconds")
    storage.save_request_stats(stats)

    count = entry["count"]
    task = normalized if len(normalized) <= 40 else normalized[:37] + "..."
    return (
        f"I noticed you've asked me to '{task}' {count} times. "
        "Would you like me to learn it as a reusable skill? "
        "Just say 'watch me' and do it once -- I'll handle it next time."
    )


def suggest_skills_for(request: str, limit: int = 3) -> list[dict]:
    """Skills worth mentioning for a request that matched nothing well."""
    return [r for r in rank_skills(request) if r["match_score"] >= 0.35][:limit]

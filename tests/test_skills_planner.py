"""Planner tests: skill matching, auto-play threshold, request-frequency
learning suggestions."""

from datetime import datetime, timedelta

from skills import planner, storage


def _make_skill(name, description="", tags=None):
    folder = storage.ensure_layout(name)
    record = {
        "id": storage.new_skill_id(),
        "name": name,
        "description": description,
        "created": storage.now_iso(),
        "tags": tags or [],
        "version": 1,
    }
    storage.save_skill(record)
    return record


def test_find_skill_by_name():
    _make_skill("Monthly Report", "generate the monthly expense report",
                ["report", "finance"])
    match = planner.find_skill("generate my monthly report")
    assert match is not None
    assert match["name"] == "Monthly Report"


def test_find_skill_returns_none_on_weak_match():
    _make_skill("Create PowerPoint", "build a presentation deck", ["slides"])
    assert planner.find_skill("what is the weather like") is None
    assert planner.find_skill("open my downloads folder") is None


def test_auto_play_needs_high_confidence():
    _make_skill("Monthly Report", "generate the monthly expense report")
    assert planner.auto_play_skill("generate my monthly report") is not None
    assert planner.auto_play_skill("how do I write a report about birds") is None


def test_rank_skills_orders_by_match():
    _make_skill("PowerPoint", "create slides")
    _make_skill("Word Document", "create documents")
    ranked = planner.rank_skills("create a powerpoint presentation")
    assert ranked[0]["name"] == "PowerPoint"


def test_observe_request_suggests_after_repeat():
    _make_skill("Create Word", "make a word document")  # unrelated skill
    stats = {"requests": {}}
    now = datetime.now()
    for _ in range(3):
        entry = stats["requests"].setdefault("can you open spotify daily", {
            "count": 0, "first": now.isoformat(), "last": now.isoformat()})
        entry["count"] += 1
    storage.save_request_stats(stats)

    suggestion = planner.observe_request("can you open spotify daily")
    assert suggestion is not None
    assert "spotify" in suggestion
    assert "skill" in suggestion.lower()


def test_observe_request_no_suggestion_when_rare():
    suggestion = planner.observe_request("open spotify daily")
    assert suggestion is None


def test_observe_request_no_suggestion_when_skill_exists():
    _make_skill("Spotify Daily", "open spotify and play the daily mix", ["music"])
    stats = {"requests": {}}
    now = datetime.now()
    for _ in range(5):
        entry = stats["requests"].setdefault("open spotify daily", {
            "count": 0, "first": now.isoformat(), "last": now.isoformat()})
        entry["count"] += 1
    storage.save_request_stats(stats)
    assert planner.observe_request("open spotify daily") is None


def test_observe_request_stale_request_not_suggested():
    stats = {"requests": {"archive old emails": {
        "count": 4,
        "first": (datetime.now() - timedelta(days=60)).isoformat(),
        "last": (datetime.now() - timedelta(days=30)).isoformat(),
    }}}
    storage.save_request_stats(stats)
    assert planner.observe_request("archive old emails") is None


def test_suggest_skills_for_returns_close_matches():
    _make_skill("Create Word", "make a word document")
    close = planner.suggest_skills_for("create a powerpoint document")
    assert any(s["name"] == "Create Word" for s in close)

"""Tests for the confirmation/safety policy engine (brain.policy)."""

from brain.policy import (
    RISK_LEVELS,
    disable_action,
    enable_action,
    is_global_auto_mode,
    list_auto_actions,
    requires_confirmation,
    risk_level,
    set_global_auto_mode,
)


def test_risk_level_classification():
    assert risk_level("send_message") == "high"
    assert risk_level("delete_file") == "high"
    assert risk_level("create_event") == "medium"
    assert risk_level("create_task") == "medium"
    assert risk_level("download_attachment") == "medium"
    assert risk_level("read_message") == "low"
    assert risk_level("search") == "low"
    assert risk_level("summarize") == "low"
    assert risk_level("unknown_action") == "low"  # default


def test_high_risk_requires_confirmation():
    assert requires_confirmation("send_message") is True
    assert requires_confirmation("delete_file") is True


def test_medium_risk_requires_confirmation():
    assert requires_confirmation("create_event") is True
    assert requires_confirmation("create_reminder") is True


def test_low_risk_no_confirmation():
    assert requires_confirmation("search") is False
    assert requires_confirmation("read_message") is False
    assert requires_confirmation("summarize") is False


def test_explicit_confirm_overrides():
    # confirm=True supplied by the caller/policy layer -> no confirmation.
    assert requires_confirmation("send_message", confirm=True) is False
    assert requires_confirmation("send_message", confirm=False) is True


def test_action_whitelist():
    enable_action("send_message")
    try:
        assert requires_confirmation("send_message") is False
        assert "send_message" in list_auto_actions()
    finally:
        disable_action("send_message")
    assert requires_confirmation("send_message") is True


def test_global_auto_mode():
    assert is_global_auto_mode() is False
    set_global_auto_mode(True)
    try:
        assert is_global_auto_mode() is True
        assert requires_confirmation("send_message") is False
        assert requires_confirmation("delete_file") is False
    finally:
        set_global_auto_mode(False)
    assert requires_confirmation("send_message") is True


def test_describe_policy():
    from brain.policy import describe_policy

    policy = describe_policy()
    assert policy["global_auto_mode"] is False
    assert "send_message" in policy["risk_levels"]
    assert policy["risk_levels"]["send_message"] == "high"
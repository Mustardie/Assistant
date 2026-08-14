"""Tests for the reusable workflow/skills system."""

import pytest

from events import NEW_EMAIL, NEW_FILE, NEW_MESSAGE, make_event
from workflows import (
    DailyBriefingWorkflow,
    HandleAssignmentWorkflow,
    HandleNewEmailWorkflow,
    ProcessDocumentWorkflow,
    ScheduleEventWorkflow,
    WorkflowAborted,
    WorkflowResult,
    register_all,
    workflow_registry,
)
from workflows.assignment import extract_deadline, looks_like_assignment
from workflows.meeting import extract_datetime


def test_deadline_extraction():
    assert extract_deadline("Complete questions 1-10 by Friday") == "friday"
    assert extract_deadline("Essay due 2025-03-15") == "2025-03-15"
    assert extract_deadline("HW due March 20") is not None
    assert extract_deadline("No deadline here") is None
    assert extract_deadline("") is None


def test_assignment_detection():
    assert looks_like_assignment("Complete questions 1-10 and submit")
    assert looks_like_assignment("read chapter 4 for the quiz")
    assert not looks_like_assignment("hello how are you doing today")


def test_datetime_extraction():
    assert extract_datetime("Meeting at 2025-06-01T15:00") == "2025-06-01T15:00"
    assert extract_datetime("Call on 2025-06-01") == "2025-06-01"
    assert extract_datetime("sometime later") is None


def test_registry_lists_and_routes():
    register_all()
    names = {w["name"] for w in workflow_registry.list()}
    assert {"handle_assignment", "handle_new_email", "process_document",
            "schedule_event", "daily_briefing"} <= names

    event = make_event("discord", NEW_MESSAGE, sender="Teacher",
                       content="Complete questions 1-10 by Friday")
    workflow = workflow_registry.find_for_event(event)
    assert workflow is not None
    assert workflow.name == "handle_assignment"


def test_assignment_workflow_runs_without_services(monkeypatch):
    """The flagship workflow must complete even with NO capabilities
    connected -- it degrades to warnings, never crashes."""
    from tools import tool_registry
    from workflows import assignment as assignment_mod

    def fake_run_tool(tool, args=None):
        args = args or {}
        if tool in ("inspect_attachment", "download_attachment", "read_document"):
            return False, {"error": "no service"}
        if tool == "create_task":
            return True, {"success": False, "error": "no connected service supports create_task"}
        if tool == "list_tasks":
            return True, {"success": False, "error": "no connected service"}
        return True, {"success": False, "error": f"no tool {tool}"}

    monkeypatch.setattr(tool_registry, "run_tool", fake_run_tool)
    monkeypatch.setattr(assignment_mod, "_llm_summarize_assignment",
                        lambda c, e: "")

    workflow = HandleAssignmentWorkflow()
    event = make_event("discord", NEW_MESSAGE, sender="Teacher",
                       content="Complete questions 1-10 by Friday for the quiz")
    result = workflow.run(event)
    assert result.workflow == "handle_assignment"
    assert result.intent == "assignment_text"
    assert result.plan  # has a plan
    assert result.final_result  # reported something
    assert "Deadline: friday" in result.final_result


def test_assignment_workflow_with_attachment(monkeypatch, tmp_path):
    from tools import tool_registry
    from workflows import assignment as assignment_mod

    doc = tmp_path / "hw.txt"
    doc.write_text("Answer all 10 questions", encoding="utf-8")

    def fake_run_tool(tool, args=None):
        args = args or {}
        if tool == "inspect_attachment":
            return True, {"success": True, "name": "hw.txt", "size": 42}
        if tool == "download_attachment":
            return True, {"success": True, "path": str(doc)}
        if tool == "read_document":
            return True, {"success": True, "text": "Answer all 10 questions"}
        if tool == "create_task":
            return True, {"success": True, "id": "task-123", "title": "Assignment: ..."}
        if tool == "list_tasks":
            return True, {"success": True, "tasks": [{"id": "task-123", "title": "x"}]}
        return True, {"success": False, "error": f"no tool {tool}"}

    monkeypatch.setattr(tool_registry, "run_tool", fake_run_tool)
    monkeypatch.setattr(assignment_mod, "_llm_summarize_assignment",
                        lambda c, e: "")

    workflow = HandleAssignmentWorkflow()
    event = make_event("whatsapp", NEW_MESSAGE, sender="Teacher",
                       content="HW due tomorrow",
                       attachment={"name": "hw.txt"})
    result = workflow.run(event)
    assert result.intent == "assignment_with_attachment"
    actions = {a["action"] for a in result.actions_completed}
    assert {"inspect_attachment", "read_attachment", "create_task"} <= actions
    assert any(v["description"] == "task_created_and_verified" and v["verified"]
               for v in result.verification)


def test_assignment_workflow_aborts_when_confirmation_needed(monkeypatch):
    from tools import tool_registry
    from workflows import assignment as assignment_mod

    def fake_run_tool(tool, args=None):
        args = args or {}
        if tool == "inspect_attachment":
            return True, {"success": False, "error": "no service"}
        if tool == "download_attachment":
            return True, {"success": False, "error": "no service"}
        if tool == "create_task":
            return True, {"success": False, "requires_confirmation": True,
                          "message": "Task creation needs your confirmation"}
        return True, {"success": False, "error": f"no tool {tool}"}

    monkeypatch.setattr(tool_registry, "run_tool", fake_run_tool)
    monkeypatch.setattr(assignment_mod, "_llm_summarize_assignment",
                        lambda c, e: "")

    workflow = HandleAssignmentWorkflow()
    event = make_event("discord", NEW_MESSAGE, sender="Teacher",
                       content="Complete the essay by Friday")
    result = workflow.run(event)
    assert result.success is False
    assert "confirmation" in result.final_result.lower()


def test_email_workflow_informational():
    workflow = HandleNewEmailWorkflow()
    event = make_event("gmail", NEW_EMAIL, sender="Newsletter",
                       content="Here is this week's digest.")
    result = workflow.run(event)
    assert result.success is True
    assert result.intent == "informational_email"


def test_email_workflow_actionable(monkeypatch):
    from workflows import email as email_mod

    def fake_run_universal(tool, **kwargs):
        if tool == "create_task":
            return {"success": True, "title": kwargs.get("title")}
        return {"success": False, "error": "no"}

    workflow = HandleNewEmailWorkflow()
    monkeypatch.setattr(workflow, "_run_universal", fake_run_universal)
    event = make_event("gmail", NEW_EMAIL, sender="Boss",
                       content="Please send the report by Friday.",
                       metadata={"subject": "Report needed"})
    result = workflow.run(event)
    assert result.success is True
    assert result.intent == "actionable_email"
    assert any(a["action"] == "create_task" for a in result.actions_completed)


def test_document_workflow(monkeypatch, tmp_path):
    from workflows import document as document_mod

    doc = tmp_path / "report.txt"
    doc.write_text("Quarterly results show strong growth across all regions.",
                   encoding="utf-8")

    def fake_run_universal(tool, **kwargs):
        if tool == "read_document":
            return {"success": True, "text": doc.read_text(encoding="utf-8")}
        return {"success": False, "error": "no"}

    workflow = ProcessDocumentWorkflow()
    monkeypatch.setattr(workflow, "_run_universal", fake_run_universal)
    event = make_event("filesystem", NEW_FILE, content=str(doc))
    result = workflow.run(event)
    assert result.success is True
    assert "report.txt" in result.final_result
    assert any(a["action"] == "read_document" for a in result.actions_completed)


def test_meeting_workflow(monkeypatch):
    from workflows import meeting as meeting_mod

    def fake_run_universal(tool, **kwargs):
        if tool == "create_event":
            return {"success": True, "id": "evt-1", "title": kwargs.get("title")}
        if tool == "create_reminder":
            return {"success": True}
        if tool == "list_events":
            return {"success": True, "events": [{"id": "evt-1", "title": kwargs.get("title")}]}
        return {"success": False, "error": "no"}

    workflow = ScheduleEventWorkflow()
    monkeypatch.setattr(workflow, "_run_universal", fake_run_universal)
    event = make_event("telegram", NEW_MESSAGE,
                       content="schedule a meeting on 2025-06-01T15:00 to discuss budget")
    result = workflow.run(event)
    assert result.success is True
    assert "evt-1" in result.final_result
    assert any(v["description"] == "event_created_and_verified" and v["verified"]
               for v in result.verification)


def test_briefing_workflow(monkeypatch):
    from workflows import briefing as briefing_mod

    def fake_run_universal(tool, **kwargs):
        if tool == "list_events":
            return {"success": True, "events": [{"title": "Standup", "start": "2025-06-01T09:00"}]}
        if tool == "list_tasks":
            return {"success": True, "tasks": [{"title": "Finish report", "completed": False}]}
        return {"success": False, "error": "no"}

    workflow = DailyBriefingWorkflow()
    monkeypatch.setattr(workflow, "_run_universal", fake_run_universal)
    result = workflow.run(make_event("system", "SYSTEM"))
    assert result.success is True
    assert "Standup" in result.final_result
    assert "Finish report" in result.final_result


def test_workflow_result_note_action_and_verification():
    result = WorkflowResult(workflow="test")
    result.note_action("do_something", {"ok": True, "text": "secret"}, verified=True)
    assert result.actions_completed[0]["action"] == "do_something"
    assert result.actions_completed[0]["verified"] is True
    assert "text" not in result.actions_completed[0]["result"]  # redacted
    d = result.to_dict()
    assert d["workflow"] == "test"


def test_workflow_registry_clear_and_reroute():
    register_all()
    workflow_registry.clear()
    assert workflow_registry.list() == []
    workflow_registry.register(HandleAssignmentWorkflow())
    event = make_event("x", NEW_MESSAGE, content="do the hw by friday")
    assert workflow_registry.find_for_event(event) is not None
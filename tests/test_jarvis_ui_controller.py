from ui.jarvis.controller import JarvisUIController
from ui.jarvis.events import JarvisEventBus, JarvisEventType
from ui.jarvis.manager import WidgetManager
from ui.jarvis.models import JarvisState
from ui.jarvis.registry import build_default_registry


def _controller(tmp_path):
    events = JarvisEventBus()
    manager = WidgetManager(build_default_registry(), event_bus=events, layout_path=tmp_path / "layout.json")
    return JarvisUIController(manager, events), manager, events


def test_normal_answer_does_not_open_a_tool_widget(tmp_path):
    controller, manager, _ = _controller(tmp_path)
    assert controller.route_request("What is the capital of France?") == []
    assert manager.all() == []


def test_intent_opens_only_useful_widgets_with_honest_fallbacks(tmp_path):
    controller, manager, _ = _controller(tmp_path)
    controller.route_request("What is the weather tomorrow?")
    weather = manager.find_type("weather")
    assert weather is not None
    assert weather.data["connected"] is False

    controller.route_request(r"Play this video C:\Media\review.mp4")
    video = manager.find_type("video_player")
    assert video.data["path"] == r"C:\Media\review.mp4"

    controller.route_request("Find my assignment file")
    assert manager.find_type("file_search") is not None


def test_tool_events_drive_progress_verification_and_failure_state(tmp_path):
    controller, manager, _ = _controller(tmp_path)
    controller.current_goal = "Find my report"
    controller.handle_agent_event("tool_started", {"tool": "file_search", "arguments": {"query": "report"}})
    progress = manager.find_type("task_progress")
    assert controller.state is JarvisState.EXECUTING_TOOL
    assert progress.data["tool_calls"][-1]["status"] == "running"

    controller.handle_agent_event("tool_failed", {"tool": "file_search", "error": "Index unavailable", "verified": True})
    assert controller.state is JarvisState.ERROR
    assert manager.find_type("error_debug") is not None
    assert progress.data["tool_calls"][-1]["status"] == "failed"


def test_confirmation_flow_records_exact_action_risk_and_target(tmp_path):
    controller, manager, events = _controller(tmp_path)
    widget_id = controller.confirmation_required("Delete file", "Cannot be undone", r"C:\Notes\draft.txt", confirmation_id="confirm-7")
    state = manager.get(widget_id)
    assert controller.state is JarvisState.WAITING_FOR_CONFIRMATION
    assert state.data == {
        "confirmation_id": "confirm-7",
        "action": "Delete file",
        "risk": "Cannot be undone",
        "target": r"C:\Notes\draft.txt",
        "resolved": False,
    }
    assert events.history(JarvisEventType.JARVIS_STATE_CHANGED)[-1].payload["state"] == "waiting_for_confirmation"


def test_chat_and_transcript_events_are_structured(tmp_path):
    controller, _, events = _controller(tmp_path)
    controller.user_message("remember that I prefer concise answers", voice=True)
    message = events.history(JarvisEventType.CHAT_MESSAGE)[-1]
    transcript = events.history(JarvisEventType.TRANSCRIPT_UPDATED)[-1]
    assert message.payload["role"] == "user"
    assert message.payload["voice"] is True
    assert transcript.payload["final"] is True


def test_file_results_and_targeted_memory_update_existing_widgets(tmp_path):
    controller, manager, _ = _controller(tmp_path)
    file_widget = manager.create("file_search", data={"query": "report", "results": []}, loading=True)
    controller.handle_agent_event(
        "tool_finished",
        {"tool": "file_search", "result": {"status": "clarify", "candidates": [{"path": r"C:\Docs\report.pdf"}]}, "verified": True},
    )
    assert manager.get(file_widget.widget_id).loading is False
    assert manager.get(file_widget.widget_id).data["results"][0]["path"].endswith("report.pdf")

    controller.handle_agent_event(
        "memory_retrieved",
        {"query": "my report", "used": True, "memories": [{"text": "Prefers concise reports", "matched_terms": ["report"]}]},
    )
    memory = manager.find_type("memory_recall")
    assert memory is not None
    assert memory.data["used"] is True


def test_file_intent_results_update_file_intelligence_widget(tmp_path):
    controller, manager, _ = _controller(tmp_path)
    controller.route_request("Find the Minecraft crash log")
    widget = manager.find_type("file_search")
    assert widget is not None
    controller.handle_agent_event(
        "tool_finished",
        {
            "tool": "file_intent_search",
            "result": {
                "success": True,
                "results": [{"path": r"C:\Games\.minecraft\logs\latest.log", "summary": "Crash log from Minecraft.", "risk": "low"}],
            },
        },
    )
    state = manager.get(widget.widget_id)
    assert state.loading is False
    assert state.data["results"][0]["summary"] == "Crash log from Minecraft."


def test_inbox_assignment_request_and_events_open_review_widgets(tmp_path):
    controller, manager, _ = _controller(tmp_path)
    controller.route_request("Teacher sent an assignment on WhatsApp, finish it")
    assert manager.find_type("inbox_item") is not None
    assert manager.find_type("assignment_analysis") is not None
    assert manager.find_type("source_files") is not None

    controller.handle_agent_event(
        "tool_finished",
        {
            "tool": "inbox_ingest_file",
            "result": {
                "success": True,
                "assignment_id": "assignment-1",
                "item": {"attachments": [{"filename": "worksheet.pdf", "local_path": r"C:\Downloads\worksheet.pdf"}]},
                "analyses": [{"short_summary": "School worksheet", "instructions": ["Answer all questions"]}],
            },
        },
    )
    analysis = manager.find_type("assignment_analysis")
    sources = manager.find_type("source_files")
    assert analysis.data["assignment_id"] == "assignment-1"
    assert sources.data["files"][0]["filename"] == "worksheet.pdf"

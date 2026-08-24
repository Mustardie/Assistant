import zipfile
from datetime import datetime
from pathlib import Path

from brain.intent_router import Intent, IntentRouter
from connectors.browser_downloads import BrowserDownloadsConnector
from tools.inbox_intelligence import InboxIntelligenceService
from tools.inbox_models import InboxItem, InboxMessageContext, InboxSource
from tools.tool_registry import TOOLS


def _docx(path: Path, text: str):
    xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def test_inbox_item_schema_tracks_source_context_and_safety():
    item = InboxItem(
        "inbox-1",
        InboxSource.WHATSAPP,
        InboxMessageContext("Complete tomorrow", "Teacher", "Class 10", "2026-08-23", True),
        confidence=0.9,
        safety_notes=("No auto-submit",),
    )
    value = item.to_dict()
    assert value["source"] == "whatsapp"
    assert value["message_context"]["sender"] == "Teacher"
    assert value["safety_notes"] == ("No auto-submit",)


def test_manual_import_extracts_task_deadline_plans_and_reviewable_output(tmp_path):
    source = tmp_path / "assignment.txt"
    source.write_text("SCIENCE ASSIGNMENT\n1. Explain photosynthesis?\nWrite a short report in a Word document.", encoding="utf-8")
    service = InboxIntelligenceService(tmp_path / "outputs", downloads_connector=BrowserDownloadsConnector([tmp_path]))
    result = service.ingest_file(
        source,
        source=InboxSource.WHATSAPP,
        message="Teacher said: Complete this assignment and submit by tomorrow.",
        sender="Science teacher",
        channel="Class 10",
    )
    assert result.success
    assert result.item.source == InboxSource.WHATSAPP
    assert "inferred from 'tomorrow'" in result.item.deadline
    assert result.item.message_context.sender == "Science teacher"
    tasks = service.extract_tasks(result.assignment_id)
    plan = service.create_plan(result.assignment_id)
    output = service.generate_draft(result.assignment_id)
    assert tasks["tasks"]
    assert plan.output_type == ".docx"
    assert output.submitted is False and output.review_required is True
    assert Path(output.analysis_path).is_file()
    assert Path(output.plan_path).is_file()
    assert Path(output.draft_path).is_file()
    assert Path(output.sources_path).is_file()
    assert Path(output.report_path).is_file()
    assert "has not been submitted" in Path(output.draft_path).read_text(encoding="utf-8")


def test_docx_assignment_text_is_extracted_without_external_account(tmp_path):
    source = tmp_path / "English_Assignment.docx"
    _docx(source, "Write an essay of 500 words about climate change. Submit tomorrow.")
    service = InboxIntelligenceService(tmp_path / "outputs")
    analysis = service.analyze_attachment(source)
    assert analysis.text_extraction == "docx_xml_text"
    assert "Write an essay" in analysis.extracted_text
    assert analysis.instructions
    assert analysis.required_deliverable == ".docx"


def test_image_assignment_reports_missing_ocr_honestly(tmp_path):
    image = tmp_path / "assignment_screenshot.png"
    image.write_bytes(b"not a real image")
    analysis = InboxIntelligenceService(tmp_path / "outputs", ocr=None).analyze_attachment(image)
    assert analysis.text_extraction == "ocr_unavailable"
    assert any("OCR" in item for item in analysis.missing_information)
    assert analysis.extracted_text == ""


def test_pdf_and_archive_are_bounded_and_honest(tmp_path):
    pdf = tmp_path / "worksheet.pdf"
    archive = tmp_path / "assignment.zip"
    pdf.write_bytes(b"invalid pdf")
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr("questions.txt", "Answer all questions")
    service = InboxIntelligenceService(tmp_path / "outputs")
    pdf_analysis = service.analyze_attachment(pdf)
    zip_analysis = service.analyze_attachment(archive)
    assert pdf_analysis.document_type == "document"
    assert pdf_analysis.text_extraction == "pdf_text_unavailable"
    assert zip_analysis.text_extraction == "archive_manifest_only"
    assert "questions.txt" in zip_analysis.extracted_text
    assert any("not extracted" in item for item in zip_analysis.missing_information)


def test_download_scan_and_limited_whatsapp_fallback(tmp_path):
    (tmp_path / "Hindi_Assignment.pdf").write_bytes(b"pdf")
    (tmp_path / "notes.txt").write_text("shopping", encoding="utf-8")
    connector = BrowserDownloadsConnector([tmp_path])
    service = InboxIntelligenceService(tmp_path / "outputs", downloads_connector=connector)
    scan = service.scan_downloads(query="Hindi assignment", days=2, limit=5)
    limited = service.scan_limited_source(InboxSource.WHATSAPP, query="assignment", days=2, limit=5)
    assert scan["success"] and scan["candidates"]
    assert scan["candidates"][0]["path"].endswith("Hindi_Assignment.pdf")
    assert limited["direct_chat_access"] is False
    assert "does not read private Whatsapp chats" in limited["limitation"]


def test_folder_ingestion_imports_without_moving_source_files(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "worksheet.txt"
    source.write_text("Answer all questions", encoding="utf-8")
    service = InboxIntelligenceService(tmp_path / "outputs")
    result = service.ingest_folder(inbox, message="Teacher shared this")
    assert result["success"] and result["count"] == 1
    assert result["moved_files"] is False
    assert source.is_file()


def test_multiple_download_candidates_are_marked_ambiguous(tmp_path):
    (tmp_path / "Assignment_A.pdf").write_bytes(b"a")
    (tmp_path / "Assignment_B.pdf").write_bytes(b"b")
    service = InboxIntelligenceService(tmp_path / "outputs", downloads_connector=BrowserDownloadsConnector([tmp_path]))
    result = service.scan_downloads(query="assignment", days=2, limit=5)
    assert len(result["candidates"]) >= 2
    assert result["ambiguous"] is True
    assert result["requires_user_choice"] is True


def test_export_and_submission_draft_never_submit(tmp_path):
    source = tmp_path / "worksheet.txt"
    source.write_text("Answer: What is gravity?", encoding="utf-8")
    service = InboxIntelligenceService(tmp_path / "outputs")
    ingested = service.ingest_file(source, message="Complete this worksheet")
    service.generate_draft(ingested.assignment_id, response_text="Gravity attracts masses toward each other.")
    exported = service.export(ingested.assignment_id, output_format="txt")
    submission = service.submission_draft(ingested.assignment_id, connector="gmail", recipient="teacher@example.com", message="Completed")
    assert exported["success"] and Path(exported["path"]).is_file()
    assert exported["submitted"] is False
    assert submission.requires_confirmation is True
    assert submission.sent is False


def test_assignment_requests_route_to_inbox_tools_and_tools_are_registered():
    route = IntentRouter().route("Teacher sent an assignment on WhatsApp, finish it")
    assert route.intent == Intent.INBOX_ASSIGNMENT
    assert route.likely_required_tools == ["inbox_scan_downloads"]
    required = {"inbox_scan_downloads", "inbox_ingest_file", "assignment_extract", "assignment_plan", "assignment_draft", "assignment_export", "assignment_report", "assignment_submission_draft"}
    assert required <= set(TOOLS)

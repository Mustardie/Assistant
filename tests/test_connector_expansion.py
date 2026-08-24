from pathlib import Path

from connectors.apps import AppLauncherConnector
from connectors.base import ConnectorRequest, ConnectorStatus
from connectors.browser_downloads import BrowserDownloadsConnector
from connectors.calendar import GoogleCalendarConnector
from connectors.defaults import default_registry
from connectors.drive import GoogleDriveConnector
from connectors.messaging import DiscordConnector, WhatsAppConnector
from connectors.gmail import GmailConnector
from connectors.planner import ConnectorActionPlanner
from connectors.registry import ConnectorRegistry


def test_default_registry_lists_expanded_connectors():
    names = set(default_registry().names())
    assert {"gmail", "google_drive", "google_calendar", "discord", "whatsapp", "browser_downloads", "app_launcher"} <= names


def test_download_and_drive_statuses_are_honest(tmp_path):
    assert BrowserDownloadsConnector([tmp_path]).status() == ConnectorStatus.READY
    assert BrowserDownloadsConnector([]).status() == ConnectorStatus.UNAVAILABLE
    assert GoogleDriveConnector(local_roots=[tmp_path]).status() == ConnectorStatus.READY
    assert GoogleDriveConnector(local_roots=[]).status() == ConnectorStatus.UNAVAILABLE
    assert GoogleDriveConnector(object(), auth_check=lambda: False, local_roots=[]).status() == ConnectorStatus.AUTH_REQUIRED


def test_capabilities_expose_id_risk_auth_confirmation_and_availability():
    calendar = GoogleCalendarConnector()
    create = next(item for item in calendar.capabilities() if item.name == "create_event").to_dict()
    assert create["id"] == "create_event"
    assert create["risk_level"] == "high"
    assert create["requires_auth"] is True
    assert create["requires_confirmation"] is True
    assert create["available"] is False
    assert "not configured" in create["unavailable_reason"]


class CalendarBackend:
    def __init__(self):
        self.created = []

    def list_events(self, **arguments):
        return [{"summary": "Exam", "start": {"dateTime": "2026-08-25T09:00:00+05:30", "timeZone": arguments["time_zone"]}}]

    def create_event(self, **arguments):
        self.created.append(arguments)
        return {"id": "event-1", "summary": arguments["title"], "start": arguments["start"], "time_zone": arguments["time_zone"]}


def test_calendar_create_requires_confirmation_and_normalizes_timezone():
    backend = CalendarBackend()
    registry = ConnectorRegistry()
    registry.register(GoogleCalendarConnector(backend, auth_check=lambda: True, time_zone="Asia/Kolkata"))
    blocked = registry.execute("google_calendar", "create_event", {"title": "Exam", "start": "2026-08-25T09:00:00+05:30"})
    created = registry.execute("google_calendar", "create_event", {"title": "Exam", "start": "2026-08-25T09:00:00+05:30"}, confirmed=True)
    listed = registry.execute("google_calendar", "list_events", {})
    assert not blocked.success
    assert blocked.error_detail.code == "confirmation_required"
    assert created.success
    assert backend.created[0]["time_zone"] == "Asia/Kolkata"
    assert listed.data["events"][0]["time_zone"] == "Asia/Kolkata"


def test_whatsapp_without_cloud_credentials_opens_prefills_and_reports_send_unavailable():
    opened = []
    connector = WhatsAppConnector(launcher=lambda _query: False, url_opener=lambda url: opened.append(url) or True)
    registry = ConnectorRegistry()
    registry.register(connector)
    prepared = registry.execute("whatsapp", "prepare_message", {"phone": "+91 99999 99999", "text": "Assignment received"})
    send_plan = registry.plan(ConnectorRequest("whatsapp", "send_message", {"phone": "919999999999", "text": "hello"}))
    sent = registry.execute("whatsapp", "send_message", {"phone": "919999999999", "text": "hello"}, confirmed=True)
    assert prepared.success
    assert prepared.data["message_prefilled"] is True
    assert prepared.data["message_sent"] is False
    assert opened and opened[0].startswith("https://wa.me/")
    assert not send_plan.supported
    assert "unsupported" in send_plan.reason.lower() or "api" in send_plan.reason.lower()
    assert not sent.success


def test_whatsapp_cloud_api_reads_local_export_and_sends_without_second_confirmation(tmp_path):
    export = tmp_path / "Teacher chat.txt"
    export.write_text("23/08/2026, 10:15 - Teacher: Complete worksheet 4\n23/08/2026, 10:16 - Me: Okay", encoding="utf-8")
    calls = []

    def transport(method, url, headers, payload):
        calls.append((method, url, headers, payload))
        if method == "POST":
            return 200, {"messages": [{"id": "wamid.1"}]}
        return 200, {"display_phone_number": "+91 99999 99999", "verified_name": "JARVIS"}

    connector = WhatsAppConnector(
        access_token="wa-token",
        phone_number_id="phone-id",
        webhook_store=tmp_path / "messages.jsonl",
        transport=transport,
        url_opener=lambda _url: True,
    )
    registry = ConnectorRegistry()
    registry.register(connector)
    imported = registry.execute("whatsapp", "import_chat", {"path": str(export)})
    read = registry.execute("whatsapp", "read_messages", {})
    plan = registry.plan(ConnectorRequest("whatsapp", "send_message", {"phone": "919999999999", "text": "Done"}))
    sent = registry.execute("whatsapp", "send_message", {"phone": "919999999999", "text": "Done"})
    assert imported.success and imported.data["imported"] == 2
    assert read.success and read.data["messages"][0]["sender"] == "Teacher"
    assert plan.supported and plan.requires_confirmation is False
    assert sent.success and sent.data["message_sent"] is True
    assert calls[-1][3]["text"]["body"] == "Done"


def test_discord_read_and_send_are_unavailable_without_token_scraping():
    connector = DiscordConnector(launcher=lambda _query: True, url_opener=lambda _url: True)
    registry = ConnectorRegistry()
    registry.register(connector)
    opened = registry.execute("discord", "open_app", {})
    read = registry.execute("discord", "read_messages", {})
    send = registry.execute("discord", "send_message", {"channel": "school", "text": "done"}, confirmed=True)
    assert opened.success
    assert not read.success
    assert not send.success
    capabilities = {item["name"]: item for item in registry.capabilities("discord")}
    assert capabilities["read_messages"]["available"] is False
    assert "token" in capabilities["read_messages"]["unavailable_reason"].lower()


def test_discord_bot_reads_and_sends_visible_channel_without_second_confirmation():
    calls = []

    def transport(method, url, headers, payload):
        calls.append((method, url, headers, payload))
        if method == "GET" and "/messages" in url:
            return 200, [{"id": "m1", "content": "Assignment attached"}]
        if method == "POST":
            return 200, {"id": "m2", "content": payload["content"]}
        return 200, {"id": "bot1", "username": "JARVIS"}

    connector = DiscordConnector(bot_token="bot-token", transport=transport, url_opener=lambda _url: True)
    registry = ConnectorRegistry()
    registry.register(connector)
    channel = "123456789012345678"
    read = registry.execute("discord", "read_messages", {"channel": channel, "limit": 10})
    plan = registry.plan(ConnectorRequest("discord", "send_message", {"channel": channel, "text": "Finished"}))
    sent = registry.execute("discord", "send_message", {"channel": channel, "text": "Finished"})
    assert read.success and read.data["messages"][0]["content"] == "Assignment attached"
    assert plan.supported and plan.requires_confirmation is False
    assert sent.success and sent.data["message_id"] == "m2"
    assert calls[-1][3] == {"content": "Finished"}


def test_browser_downloads_searches_by_intent_and_profiles_files(tmp_path):
    worksheet = tmp_path / "Class_10_Hindi_Assignment.pdf"
    installer = tmp_path / "setup.exe"
    worksheet.write_bytes(b"pdf metadata only")
    installer.write_bytes(b"binary")
    connector = BrowserDownloadsConnector([tmp_path], opener=lambda _path: True)
    registry = ConnectorRegistry()
    registry.register(connector)
    result = registry.execute("browser_downloads", "search_intent", {"query": "Hindi worksheet", "days": 2, "limit": 5})
    installer_plan = registry.plan(ConnectorRequest("browser_downloads", "open", {"path": str(installer)}))
    assert result.success
    assert result.data["results"][0]["path"] == str(worksheet.resolve())
    assert result.file_profiles
    assert all(profile["source"] == "browser_download" for profile in result.file_profiles)
    assert installer_plan.requires_confirmation
    assert installer_plan.risk_level == "high"


def test_local_drive_search_and_profile(tmp_path):
    worksheet = tmp_path / "Hindi_Worksheet.docx"
    worksheet.write_bytes(b"docx")
    connector = GoogleDriveConnector(local_roots=[tmp_path], opener=lambda _path: True)
    registry = ConnectorRegistry()
    registry.register(connector)
    result = registry.execute("google_drive", "search_files", {"query": "Hindi worksheet", "limit": 5})
    metadata = registry.execute("google_drive", "read_metadata", {"path_or_id": str(worksheet)})
    assert result.success and result.data["local_sync"] is True
    assert result.data["results"][0]["path"] == str(worksheet.resolve())
    assert metadata.success
    assert metadata.data["source"] == "google_drive"


def test_app_launcher_reports_success_and_failure():
    calls = []
    apps = {"discord": {"name": "Discord", "target": "Discord.exe", "source": "test"}}
    success = AppLauncherConnector(apps, launcher=lambda query: calls.append(query) or (True, "Discord"))
    failure = AppLauncherConnector(apps, launcher=lambda _query: (False, None))
    assert success.execute("open_app", {"query": "Discord"}).success
    assert calls == ["discord"]
    assert not failure.execute("open_app", {"query": "Discord"}).success


def test_connector_planner_chooses_connector_and_reports_missing_inputs():
    registry = ConnectorRegistry()
    registry.register(WhatsAppConnector(launcher=lambda _q: True, url_opener=lambda _u: True))
    registry.register(BrowserDownloadsConnector([]))
    planner = ConnectorActionPlanner(registry)
    whatsapp = planner.choose("message Tarun on WhatsApp")
    downloads = planner.choose("find the PDF I downloaded yesterday")
    assert whatsapp.connector == "whatsapp"
    assert whatsapp.capability == "prepare_message"
    assert {"phone", "text"} <= set(whatsapp.missing_inputs)
    assert downloads.connector == "browser_downloads"
    assert downloads.capability == "search_intent"
    assert downloads.connector_status == "unavailable"


def test_gmail_attachment_metadata_becomes_email_file_profile():
    class Service:
        def get_message(self, message_id):
            return {
                "id": message_id,
                "payload": {
                    "parts": [{"partId": "1", "filename": "Hindi_Worksheet.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "att-1", "size": 4096}}]
                },
            }

    class Backend:
        service = Service()

    registry = ConnectorRegistry()
    registry.register(GmailConnector(Backend(), auth_check=lambda: True))
    result = registry.execute("gmail", "list_attachments", {"message_id": "msg-1"})
    assert result.success
    assert result.data["attachments"][0]["downloaded"] is False
    assert result.file_profiles[0]["source"] == "email_attachment"
    assert result.file_profiles[0]["filename"] == "Hindi_Worksheet.pdf"


def test_connector_planner_calendar_create_requires_time_and_confirmation():
    backend = CalendarBackend()
    registry = ConnectorRegistry()
    registry.register(GoogleCalendarConnector(backend, auth_check=lambda: True))
    plan = ConnectorActionPlanner(registry).choose("make a calendar event for my exam")
    assert plan.connector == "google_calendar"
    assert plan.capability == "create_event"
    assert "start" in plan.missing_inputs
    assert plan.confirmation_required is True

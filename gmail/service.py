import logging
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from youtube_auth import ensure_youtube_auth

logger = logging.getLogger(__name__)


class GmailService:
    def __init__(self):
        self._service = None
        self._own_email: str | None = None

    def get_service(self):
        if self._service is None:
            try:
                credentials = ensure_youtube_auth()
                self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
                logger.info("Gmail service initialized")
            except Exception as exc:
                logger.warning("Unable to initialize Gmail service: %s", exc)
                raise RuntimeError(f"Unable to initialize Gmail service: {exc}") from exc
        return self._service

    def get_own_email_address(self) -> str:
        if self._own_email is None:
            try:
                users = self._get_users_resource()
                profile = self._request(lambda: users.getProfile(userId="me").execute())
                self._own_email = (profile.get("emailAddress") or "").lower()
            except Exception as exc:
                logger.warning("Unable to fetch own Gmail address: %s", exc)
                self._own_email = ""
        return self._own_email

    def _get_users_resource(self):
        service = self.get_service()
        if hasattr(service, "users"):
            return service.users()
        raise RuntimeError("Gmail service does not expose users().")

    def _get_messages_resource(self):
        users = self._get_users_resource()
        if hasattr(users, "messages"):
            return users.messages()
        raise RuntimeError("Gmail service does not expose users().messages().")

    def _get_drafts_resource(self):
        users = self._get_users_resource()
        if hasattr(users, "drafts"):
            return users.drafts()
        raise RuntimeError("Gmail service does not expose users().drafts().")

    def _request(self, operation):
        try:
            return operation()
        except HttpError as exc:
            logger.warning("Gmail API request failed: %s", exc)
            raise RuntimeError(f"Gmail API request failed: {exc}") from exc

    def list_messages(self, query: str = "", label_ids: list[str] | None = None, max_results: int = 10) -> dict[str, Any]:
        messages = self._get_messages_resource()
        request = messages.list(
            userId="me",
            q=query,
            labelIds=label_ids,
            maxResults=max_results,
        )
        return self._request(request.execute)

    def get_message(self, message_id: str) -> dict[str, Any]:
        messages = self._get_messages_resource()
        request = messages.get(userId="me", id=message_id, format="full")
        return self._request(request.execute)

    def modify_message(self, message_id: str, remove_labels: list[str] | None = None, add_labels: list[str] | None = None) -> dict[str, Any]:
        messages = self._get_messages_resource()
        request = messages.modify(
            userId="me",
            id=message_id,
            body={
                "removeLabelIds": remove_labels or [],
                "addLabelIds": add_labels or [],
            },
        )
        return self._request(request.execute)

    def send_message(self, message_body: dict[str, Any]) -> dict[str, Any]:
        messages = self._get_messages_resource()
        request = messages.send(userId="me", body=message_body)
        return self._request(request.execute)

    def create_draft(self, message_body: dict[str, Any]) -> dict[str, Any]:
        drafts = self._get_drafts_resource()
        request = drafts.create(userId="me", body=message_body)
        return self._request(request.execute)

    def trash_message(self, message_id: str) -> dict[str, Any]:
        messages = self._get_messages_resource()
        request = messages.trash(userId="me", id=message_id)
        return self._request(request.execute)

    def delete_message(self, message_id: str) -> dict[str, Any]:
        messages = self._get_messages_resource()
        request = messages.delete(userId="me", id=message_id)
        return self._request(request.execute)

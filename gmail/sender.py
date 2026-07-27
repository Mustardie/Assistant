import base64
from email.message import EmailMessage
from typing import Any


class GmailSender:
    def __init__(self, service):
        self.service = service

    def _build_message(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            # References should chain: prior references + the message being replied to.
            message["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
        message.set_content(body)
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        payload: dict[str, Any] = {"raw": encoded_message}
        if thread_id:
            # Tells Gmail this belongs to an existing thread instead of starting a new one.
            payload["threadId"] = thread_id
        return payload

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        message_body = self._build_message(to, subject, body, thread_id, in_reply_to, references)
        return self.service.send_message(message_body)

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        message_body = self._build_message(to, subject, body, thread_id, in_reply_to, references)
        return self.service.create_draft({"message": message_body})
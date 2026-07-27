import logging
import re
from typing import Any

from gmail.cache import GmailCache
from gmail.reader import GmailReader
from gmail.replier import GmailReplier
from gmail.service import GmailService
from recommendation.llm_client import LLMClient

logger = logging.getLogger(__name__)


class GmailSkill:
    def __init__(self):
        self.service = GmailService()
        self.reader = GmailReader(self.service)
        self.replier = GmailReplier(self.service)
        self.cache = GmailCache()

    def read(self, limit: int = 10) -> list[dict[str, Any]]:
        emails = self.reader.get_recent_emails(limit=limit)
        self.cache.add_emails(emails)
        return emails

    def get_recent_emails(self, limit: int = 20) -> list[dict[str, Any]]:
        emails = self.reader.get_recent_emails(limit=limit)
        self.cache.add_emails(emails)
        return emails

    def get_unread_emails(self, limit: int = 10) -> list[dict[str, Any]]:
        emails = self.reader.get_unread_emails(limit=limit)
        self.cache.add_emails(emails)
        return emails

    def get_email_by_id(self, message_id: str) -> dict[str, Any]:
        return self.reader.get_email_by_id(message_id)

    def unread(self, limit: int = 10) -> list[dict[str, Any]]:
        logger.info("Gmail unread request: limit=%s", limit)
        return self.reader.get_unread_emails(limit=limit)

    def summarize(self, emails: list[dict[str, Any]]) -> str:
        if not emails:
            return "You have no recent emails."

        lines = []
        for email in emails[:5]:
            subject = email.get("subject", "(no subject)")
            sender = email.get("sender", "unknown")
            lines.append(f"- {subject} — {sender}")
        return "Here is a concise summary of your emails:\n" + "\n".join(lines)

    def summarize_unread(self, limit: int = 10) -> str:
        emails = self.get_unread_emails(limit=limit)
        return self.summarize(emails)

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        emails = self.reader.search_emails(query, limit=limit)
        self.cache.add_emails(emails)
        return emails

    def reply(self, command: str, draft_mode: bool = False, confirm: bool = False) -> str:

        lower_command = command.lower().strip()
        if lower_command in {"send", "yes", "y", "confirm", "ok"}:
            if not getattr(self, "_pending_reply", None):
                return "There is no pending reply to send."
            pending = self._pending_reply
            self._pending_reply = None
            reply_payload = pending.get("reply_payload", {})
            if not isinstance(reply_payload, dict):
                reply_payload = {"subject": pending["email"].get("subject", ""), "body": str(reply_payload)}
            # Use the draft_mode the caller originally asked for, not a hardcoded value.
            pending_draft_mode = pending.get("draft_mode", False)
            result = self.replier.reply_to_email(pending["email"], reply_payload, draft_mode=pending_draft_mode)
            if pending["email"].get("message_id"):
                self.service.modify_message(pending["email"].get("message_id"), remove_labels=["UNREAD"])
            return "Draft saved successfully." if pending_draft_mode else "Reply sent successfully."

        if lower_command in {"cancel", "no", "n", "discard"}:
            self._pending_reply = None
            return "Reply cancelled."

        if lower_command in {"edit", "regenerate"}:
            if not getattr(self, "_pending_reply", None):
                return "There is no pending reply to edit."
            pending = self._pending_reply
            reply_payload = self.replier.generate_reply(pending["email"], instruction=pending.get("instruction"))
            if not reply_payload.get("body"):
                reply_payload = {"subject": pending["email"].get("subject", ""), "body": pending["email"].get("body", "") or "I will follow up shortly."}
            pending["reply_payload"] = reply_payload
            return self._format_preview(pending["email"], reply_payload, draft_mode=pending.get("draft_mode", False))

        command_for_target, instruction = self._split_command_and_instruction(command)
        if instruction:
            logger.info("Extracted explicit reply instruction: %s", instruction)

        email, error = self._resolve_email(command_for_target)
        if error:
            return error

        reply_payload = self.replier.generate_reply(email, instruction=instruction)
        if isinstance(reply_payload, str):
            reply_payload = {"subject": email.get("subject", ""), "body": reply_payload}
        elif not isinstance(reply_payload, dict):
            reply_payload = {"subject": email.get("subject", ""), "body": str(reply_payload)}

        if not reply_payload.get("body"):
            reply_payload = {"subject": email.get("subject", ""), "body": email.get("body", "") or "I will follow up shortly."}

        if confirm:
            # Caller has already confirmed (e.g. a UI that got explicit user approval
            # before calling in) — act immediately instead of requiring a second
            # "send" round-trip, and honor draft_mode this time.
            result = self.replier.reply_to_email(email, reply_payload, draft_mode=draft_mode)
            if email.get("message_id"):
                self.service.modify_message(email.get("message_id"), remove_labels=["UNREAD"])
            return "Draft saved successfully." if draft_mode else "Reply sent successfully."

        self._pending_reply = {
            "email": email,
            "reply_payload": reply_payload,
            "draft_mode": draft_mode,
            "instruction": instruction,
        }
        return self._format_preview(email, reply_payload, draft_mode=draft_mode)

    # Markers that separate "which email" from "what to say in the reply", e.g.
    # "reply to gheekster saying hello" or "reply to the 2+2 email with 5".
    _INSTRUCTION_MARKERS = [
        r"\bsaying\b",
        r"\btelling them\b",
        r"\bthat says\b",
        r"\bwith the (?:reply|answer|response|message)\b",
        r"\breply(?:ing)? with\b",
        r"\brespond(?:ing)? with\b",
        r"\bto say\b",
        r"\bwith\b",
    ]

    def _split_command_and_instruction(self, command: str) -> tuple[str, str | None]:
        lower = command.lower()
        best_match = None
        for pattern in self._INSTRUCTION_MARKERS:
            match = re.search(pattern, lower)
            if match and (best_match is None or match.start() < best_match.start()):
                best_match = match

        if not best_match:
            return command, None

        target_part = command[: best_match.start()].strip()
        instruction_part = command[best_match.end():].strip()
        if not instruction_part or not target_part:
            return command, None
        return target_part, instruction_part

    def _resolve_email(self, command: str) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve which email a command is referring to. Returns (email, error) —
        exactly one of the two will be non-None."""
        lower_command = command.lower().strip()
        latest_request = "latest" in lower_command or "newest" in lower_command
        target_phrase = ""
        if "reply to" in lower_command:
            start_index = lower_command.find("reply to") + len("reply to")
            target_phrase = command[start_index:].strip()
        elif lower_command.startswith("reply"):
            target_phrase = command[len("reply"):].strip()
        else:
            target_phrase = command.strip()

        generic_targets = {
            "my email",
            "my latest email",
            "the latest email",
            "the newest email",
            "latest email",
            "newest email",
            "this email",
            "that email",
            "my inbox",
            "my recent email",
        }
        explicit_reference = bool(target_phrase) and not latest_request and target_phrase.lower() not in generic_targets
        explicit_reference = explicit_reference or (
            not latest_request and any(
                phrase in lower_command
                for phrase in ["sender", "from", "subject", "title", "about", "regarding", "re:"]
            )
        )

        if explicit_reference:
            search_query = target_phrase or command
            if "reply to" in search_query.lower():
                search_query = search_query.lower().replace("reply to", "", 1).strip()
            if "re:" in search_query.lower():
                search_query = search_query.lower().replace("re:", "", 1).strip()
            logger.info("Using search for explicit reference: %s", search_query)
            search_results = self.search(search_query, limit=5)
            if isinstance(search_results, list) and search_results and isinstance(search_results[0], dict):
                email = search_results[0]
            else:
                logger.info("Search did not return a usable email list for %s; falling back to recent emails", search_query)
                emails = self.get_recent_emails(limit=5)
                if not emails:
                    return None, "I couldn't find a matching email for that request."
                email = emails[0]
        elif latest_request and self.cache.get_all():
            email = self.cache.get_latest()
        elif self.cache.get_all():
            target, confidence, _ = self.cache.resolve(command)
            if target is None and confidence < 70.0:
                logger.info("Unable to resolve email match for %r (confidence=%s)", command, confidence)
                return None, "I couldn't confidently identify the email you want to reply to."
            email = target or self.cache.get_latest()
        else:
            emails = self.get_recent_emails(limit=5)
            if not emails:
                return None, "No emails were found to reply to."
            email = emails[0]

        if not isinstance(email, dict):
            return None, "I couldn't find a matching email for that request."

        own_address = self.service.get_own_email_address()
        if own_address and (email.get("sender_email") or "").lower() == own_address:
            logger.warning(
                "Refusing to act on a self-sent message (message_id=%s, subject=%s) — "
                "this is likely a leftover self-addressed test message.",
                email.get("message_id"), email.get("subject"),
            )
            return None, (
                "The most recent matching email was sent from your own address, so I'm not "
                "acting on it (this is usually a leftover test message). If you meant a "
                "different email, try referencing the sender or subject directly."
            )

        if email.get("message_id"):
            logger.info("Reading message_id: %s", email.get("message_id"))
            fresh_email = self.reader.get_email_by_id(email.get("message_id"))
            if isinstance(fresh_email, dict):
                email = fresh_email
                self.cache.add_emails([email])

        return email, None

    def whats_it_about(self, command: str) -> str:
        """Answer 'what is this email about' / 'summarize this email' style requests
        for a single, specific email (as opposed to summarize_unread's list view)."""
        logger.info("Gmail whats_it_about request: command=%s", command)
        email, error = self._resolve_email(command)
        if error:
            return error
        summary = self.replier.summarize_email(email)
        sender = email.get("sender_email") or email.get("sender") or "Unknown"
        subject = email.get("subject") or "(no subject)"
        return f"About the email \"{subject}\" from {sender}:\n\n{summary}"

    def _format_preview(self, email: dict[str, Any], reply_payload: dict[str, Any], draft_mode: bool = False) -> str:
        sender = email.get("sender_email") or email.get("sender") or "Unknown"
        subject = email.get("subject") or "(no subject)"
        body = reply_payload.get("body") or ""
        action_verb = "Save this as a draft?" if draft_mode else "Send this email?"
        send_hint = "Type: send to save the draft." if draft_mode else "Type: send to send it."
        preview_lines = [
            "Reply preview",
            "",
            "To:",
            str(sender),
            "",
            "Subject:",
            f"Re: {subject}",
            "",
            "Reply:",
            "",
            str(body).strip(),
            "",
            action_verb,
            "",
            send_hint,
            "Type: cancel to discard it.",
            "Type: edit to regenerate.",
        ]
        return "\n".join(str(item) for item in preview_lines)

    def archive(self, message_id: str) -> str:
        self.service.modify_message(message_id, remove_labels=["INBOX"])
        return "Message archived."

    def mark_read(self, message_id: str) -> str:
        self.service.modify_message(message_id, remove_labels=["UNREAD"])
        return "Message marked as read."

    def mark_as_read(self, message_id: str) -> str:
        return self.mark_read(message_id)

    def star(self, message_id: str) -> str:
        self.service.modify_message(message_id, add_labels=["STARRED"])
        return "Message starred."

    def delete(self, message_id: str) -> str:
        self.service.delete_message(message_id)
        return "Message deleted."

    def send(self, to: str, subject: str, body: str, confirm: bool = False) -> str:
        if not confirm:
            self.replier.sender.create_draft(to, subject, body)
            return "Draft saved successfully."

        self.replier.sender.send_message(to, subject, body)
        return "Reply sent successfully."


gmail = GmailSkill()
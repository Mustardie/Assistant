import json
import logging
import re
from typing import Any

from recommendation.llm_client import LLMClient

from .sender import GmailSender

logger = logging.getLogger(__name__)


class GmailReplier:
    def __init__(self, service):
        self.service = service
        self.sender = GmailSender(service)

    def _build_reply_prompt(
        self,
        email: dict[str, Any],
        conversation_history: str | None = None,
        instruction: str | None = None,
    ) -> str:
        sender = email.get("sender_email") or email.get("sender", "")
        subject = email.get("subject", "")
        body = email.get("body", "")
        quoted_history = email.get("quoted_history") or ""

        prompt = [
            "Sender:",
            str(sender),
            "",
            "Subject:",
            str(subject),
            "",
            "Body:",
            str(body),
        ]

        if quoted_history:
            prompt.extend(["", "Quoted history:", quoted_history])

        if conversation_history:
            prompt.extend(["", "Conversation history:", conversation_history])

        if instruction:
            prompt.extend([
                "",
                "The user has explicitly told you what this reply should say:",
                str(instruction),
                "",
                "Your reply MUST faithfully include/answer with that exact content — do not soften, "
                "correct, second-guess, or omit it, even if it seems factually wrong or unusual. Just "
                "wrap it in a normal, natural-sounding email reply.",
            ])

        prompt.extend([
            "",
            "Write only the plain email reply body. Do not wrap it in JSON, markdown, or code fences. "
            "Do not include a subject line. Just the reply text.",
            "Always write a genuine, specific attempt at a reply based on the sender, subject, and body "
            "above — even if some details are missing, make reasonable assumptions rather than refusing "
            "or asking the user for more information. Never say you cannot help; a generic placeholder "
            "reply is not acceptable.",
        ])
        return "\n".join(prompt)

    def _parse_reply_payload(self, raw_reply: str) -> dict[str, str]:
        cleaned = (raw_reply or "").strip()
        if not cleaned:
            return {"subject": "", "body": ""}

        # Local models (e.g. Gemma via Ollama) often ignore "no markdown" instructions
        # and wrap the reply in a ```json ... ``` fence. Strip that BEFORE checking
        # whether the string looks like JSON, otherwise the JSON branch below never
        # triggers and the raw braces/quotes end up in the sent email.
        fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                payload = json.loads(cleaned)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                subject = payload.get("subject") or ""
                body = payload.get("body") or ""
                if isinstance(body, str) and body.strip():
                    return {
                        "subject": subject.strip() if isinstance(subject, str) else "",
                        "body": body.strip(),
                    }

        # Not JSON (or JSON parsing failed) — treat as plain text. Do NOT strip
        # braces/brackets here: a real reply body may legitimately contain them,
        # and stripping is what let garbled JSON key/value text through before.
        refusal_markers = ["i cannot", "i can't", "i will not", "i won't", "cannot fulfill", "unable to assist", "i refuse"]
        # Only treat this as a genuine refusal if one of these markers shows up in
        # the first ~60 characters of a short response — i.e. the response IS the
        # refusal. Checking the whole string (old behavior) meant a real, useful
        # reply that merely contained a phrase like "I can't guarantee..." later on
        # would get silently discarded and replaced with a placeholder.
        looks_like_refusal = len(cleaned) < 300 and any(
            marker in cleaned[:60].lower() for marker in refusal_markers
        )
        if looks_like_refusal:
            logger.warning("LLM appears to have refused to draft a reply; raw output: %s", cleaned)
            cleaned = "Hi,\n\nThanks for your email. I’ll get back to you shortly.\n\nRegards,\nVishal"

        return {"subject": "", "body": cleaned}

    def generate_reply(
        self,
        email: dict[str, Any],
        conversation_history: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, str]:
        prompt = self._build_reply_prompt(email, conversation_history=conversation_history, instruction=instruction)
        client = LLMClient()
        raw_reply = client.complete("You are a helpful email assistant.", prompt).strip()
        return self._parse_reply_payload(raw_reply)

    def summarize_email(self, email: dict[str, Any]) -> str:
        """Return a short, plain-language summary of what an email is about."""
        sender = email.get("sender_email") or email.get("sender", "")
        subject = email.get("subject", "")
        body = email.get("body", "") or email.get("snippet", "")

        prompt = "\n".join([
            "Sender:", str(sender),
            "", "Subject:", str(subject),
            "", "Body:", str(body),
            "",
            "In 2-3 short sentences, plainly explain what this email is about and what, if anything, "
            "it's asking the recipient to do. Do not draft a reply. Do not use JSON, markdown, or bullet "
            "points — just plain sentences.",
        ])
        client = LLMClient()
        raw = client.complete("You summarize emails clearly and concisely.", prompt).strip()

        # Reuse the same fence-stripping as replies, in case the local model wraps
        # this in markdown too.
        fence_match = re.match(r"^```(?:\w+)?\s*(.*?)\s*```$", raw, re.DOTALL)
        if fence_match:
            raw = fence_match.group(1).strip()
        return raw or "I couldn't determine what this email is about."

    def build_reply(self, email: dict[str, Any], reply_payload: dict[str, str]) -> dict[str, Any]:
        sender = email.get("sender_email") or email.get("sender", "")
        sender = sender or email.get("sender_name") or ""
        subject = reply_payload.get("subject") or ""
        thread_id = email.get("thread_id")

        if not subject:
            subject = email.get("subject", "")

        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"

        return {
            "to": sender,
            "subject": subject,
            "body": reply_payload.get("body") or "",
            "thread_id": thread_id,
            "in_reply_to": email.get("rfc_message_id") or "",
            "references": email.get("rfc_references") or "",
        }

    def reply_to_email(self, email: dict[str, Any], reply_payload: dict[str, str], draft_mode: bool = False) -> dict[str, Any]:
        message = self.build_reply(email, reply_payload)
        if draft_mode:
            result = self.sender.create_draft(
                message["to"],
                message["subject"],
                message["body"],
                thread_id=message["thread_id"],
                in_reply_to=message["in_reply_to"],
                references=message["references"],
            )
            return {
                "draft": True,
                "body": message["body"],
                "thread_id": message["thread_id"],
                "message_id": result.get("id"),
                "preview": "Draft reply prepared. Send this? (yes/no)",
            }

        result = self.sender.send_message(
            message["to"],
            message["subject"],
            message["body"],
            thread_id=message["thread_id"],
            in_reply_to=message["in_reply_to"],
            references=message["references"],
        )
        return {
            "draft": False,
            "body": message["body"],
            "thread_id": message["thread_id"],
            "message_id": result.get("id"),
            "preview": "Reply sent.",
        }
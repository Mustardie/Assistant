import logging
import re
from typing import Any

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


class GmailCache:
    def __init__(self):
        self._emails: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}

    def clear(self):
        self._emails = []
        self._by_id = {}

    def add_emails(self, emails: list[dict[str, Any]]) -> None:
        for email in emails:
            if not email:
                continue
            message_id = email.get("message_id")
            if message_id:
                self._by_id[message_id] = email
            if email not in self._emails:
                self._emails.append(email)

    def get_all(self) -> list[dict[str, Any]]:
        return list(self._emails)

    def get_latest(self) -> dict[str, Any] | None:
        return self._emails[0] if self._emails else None

    def get_by_id(self, message_id: str) -> dict[str, Any] | None:
        return self._by_id.get(message_id)

    def _normalize_query(self, query: str) -> str:
        normalized = (query or "").strip().lower()
        normalized = re.sub(r"\b(reply|re)\b", "", normalized)
        normalized = re.sub(r"\b(to|the|that|this|email|mail)\b", "", normalized)
        normalized = normalized.strip()
        return normalized

    def resolve(self, query: str, limit: int = 5) -> tuple[dict[str, Any] | None, float, list[tuple[dict[str, Any], float]]]:
        if not self._emails:
            return None, 0.0, []

        normalized = self._normalize_query(query)
        if not normalized:
            return self.get_latest(), 100.0, []

        if "latest" in query.lower() or "newest" in query.lower():
            return self.get_latest(), 100.0, []

        if "unread" in query.lower():
            unread_emails = [email for email in self._emails if email.get("unread")]
            if unread_emails:
                return unread_emails[0], 100.0, []

        scored = []
        for email in self._emails:
            sender_name = (email.get("sender_name") or "").lower()
            sender_email = (email.get("sender_email") or "").lower()
            subject = (email.get("subject") or "").lower()
            snippet = (email.get("snippet") or "").lower()
            body = (email.get("body") or "").lower()

            score = 0.0
            score += fuzz.ratio(normalized, sender_name) * 0.35
            score += fuzz.ratio(normalized, sender_email) * 0.35
            score += fuzz.ratio(normalized, subject) * 0.45
            score += fuzz.partial_ratio(normalized, snippet) * 0.2
            score += fuzz.partial_ratio(normalized, body) * 0.15
            score = min(score, 100.0)
            scored.append((email, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        top_email, top_score = scored[0]
        top_candidates = [(email, round(score, 1)) for email, score in scored[:limit] if score >= 0.0]

        if top_score < 70.0:
            return None, top_score, top_candidates

        return top_email, top_score, top_candidates

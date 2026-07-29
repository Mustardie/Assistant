import json
import logging
import re
import time

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

OLLAMA_CHAT_URL = None  # resolved from settings.ollama_url

_RETRYABLE_STATUS_CODES = {429, 503}
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.5
_REQUEST_TIMEOUT = 120


class OllamaConfigurationError(Exception):
    """Raised when Ollama is not reachable."""


class OllamaRequestError(Exception):
    """Raised when the Ollama API rejected the request."""


class OllamaClient:
    """Thin REST wrapper around Ollama's /api/chat endpoint.

    Implements the same chat_json / chat_text / chat_with_search interface
    as OpenRouterClient and GeminiClient so the brain can swap providers
    with a single config change.
    """

    def __init__(self, model: str | None = None, base_url: str | None = None):
        self.model = model or settings.ollama_model
        raw_url = (base_url or settings.ollama_url).rstrip("/")
        # Accept either the full /api/chat path or just the server root
        if raw_url.endswith("/api/chat"):
            self._api_url = raw_url
        else:
            self._api_url = f"{raw_url}/api/chat"
        self._session = None

    def _get_url(self) -> str:
        return self._api_url

    def _get_session(self):
        if self._session is None:
            self._session = requests.Session()
        return self._session

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned

    def _post(self, body: dict, timeout: int) -> dict:
        session = self._get_session()
        url = self._get_url()
        last_error_text = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = session.post(url, json=body, timeout=timeout)
            except requests.RequestException as exc:
                last_error_text = str(exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise OllamaRequestError(f"Could not reach Ollama at {url}: {exc}") from exc

            if response.status_code == 200:
                try:
                    return response.json()
                except json.JSONDecodeError:
                    last_error_text = "Ollama returned HTTP 200 with empty body"
                    if attempt < _MAX_RETRIES:
                        time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    raise OllamaRequestError(last_error_text)

            reason = self._extract_error_message(response)
            last_error_text = f"HTTP {response.status_code}: {reason}"

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                logger.warning(
                    "Ollama request failed (%s), retrying (%s/%s): %s",
                    response.status_code, attempt + 1, _MAX_RETRIES, reason,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            hint = ""
            if response.status_code == 404:
                hint = f" -- model '{self.model}' may not exist; run 'ollama pull {self.model}'"
            raise OllamaRequestError(f"{last_error_text}{hint}")

        raise OllamaRequestError(last_error_text or "Ollama request failed for an unknown reason")

    @staticmethod
    def _extract_error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
            return str(payload.get("error", response.text))
        except Exception:
            return response.text[:300]

    def chat_json(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT) -> dict:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.0},
        }

        data = self._post(body, timeout)
        text = (data.get("message") or {}).get("content") or ""

        if not text.strip():
            raise OllamaRequestError("Ollama returned empty response")
        try:
            cleaned = self._strip_code_fences(text)
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise OllamaRequestError(
                f"Ollama returned non-JSON response (model={self.model}): {text[:200]}"
            ) from exc

    def chat_text(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.0},
        }

        data = self._post(body, timeout)
        return (data.get("message") or {}).get("content") or ""

    def chat_with_search(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT):
        """Ollama has no built-in web search grounding. Falls back to plain
        text generation and returns empty sources."""
        answer = self.chat_text(
            system_prompt + "\n\nNote: You do NOT have live web search. Only answer from your training data.",
            user_prompt,
            timeout=timeout,
        )
        return answer, []

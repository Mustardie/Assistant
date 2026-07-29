import json
import logging
import re
import time

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.5
_REQUEST_TIMEOUT = 45
_TOTAL_DEADLINE = 90


class OpenRouterConfigurationError(Exception):
    """Raised when OPENROUTER_API_KEY is missing."""


class OpenRouterRequestError(Exception):
    """Raised when OpenRouter's API itself rejected the request. Carries
    the actual reason OpenRouter gave (bad key, model not found, out of
    credits, etc.) instead of a bare HTTP status code."""


class OpenRouterClient:
    """Thin REST wrapper around OpenRouter's OpenAI-compatible chat
    completions endpoint. Kept dependency-free (just `requests`, already
    used elsewhere in this project) rather than pulling in an SDK.
    """

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or settings.openrouter_model
        self.api_key = api_key or settings.openrouter_api_key
        self._session = None

    def _require_key(self):
        if not self.api_key:
            raise OpenRouterConfigurationError(
                "OpenRouter requires OPENROUTER_API_KEY. Set it in your .env file."
            )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Optional, but OpenRouter recommends these for attribution/limits.
            "HTTP-Referer": "https://nova.local",
            "X-Title": "Nova",
        }

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned

    def _get_session(self):
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _post(self, body: dict, timeout: int) -> dict:
        """POST to chat/completions, retrying transient errors, and raising
        OpenRouterRequestError with the actual error message on failure."""
        last_error_text = None
        session = self._get_session()

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = session.post(
                    OPENROUTER_API_URL, json=body, headers=self._headers(), timeout=timeout
                )
            except requests.RequestException as exc:
                last_error_text = str(exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise OpenRouterRequestError(f"Could not reach OpenRouter: {exc}") from exc

            if response.status_code == 200:
                try:
                    return response.json()
                except json.JSONDecodeError:
                    last_error_text = "OpenRouter returned HTTP 200 with empty body (free tier timeout)"
                    if attempt < _MAX_RETRIES:
                        time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    raise OpenRouterRequestError(last_error_text)

            reason = self._extract_error_message(response)
            last_error_text = f"HTTP {response.status_code}: {reason}"

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                logger.warning(
                    "OpenRouter request failed (%s), retrying (%s/%s): %s",
                    response.status_code, attempt + 1, _MAX_RETRIES, reason,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            hint = ""
            if response.status_code in (401, 403):
                hint = " -- check OPENROUTER_API_KEY is correct and active at https://openrouter.ai/keys"
            elif response.status_code == 404:
                hint = f" -- model '{self.model}' may not exist; check OPENROUTER_MODEL at https://openrouter.ai/models"
            elif response.status_code == 402:
                hint = " -- your OpenRouter account/key may be out of credits"
            raise OpenRouterRequestError(f"{last_error_text}{hint}")

        raise OpenRouterRequestError(last_error_text or "OpenRouter request failed for an unknown reason")

    @staticmethod
    def _extract_error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message", response.text))
            return str(error or response.text)
        except Exception:
            return response.text[:300]

    @staticmethod
    def _extract_message(data: dict) -> dict:
        choices = data.get("choices") or []
        if not choices:
            raise OpenRouterRequestError(f"OpenRouter returned no choices: {data}")
        return choices[0].get("message") or {}

    def chat_json(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT) -> dict:
        """Send one system+user turn, request JSON output, and parse it.
        Retries on empty/invalid response but gives up after TOTAL_DEADLINE
        seconds total so the agent loop never hangs for minutes."""
        self._require_key()

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "stream": False,
        }

        started = time.time()
        for attempt in range(3):
            if time.time() - started > _TOTAL_DEADLINE:
                raise OpenRouterRequestError(f"LLM call exceeded {_TOTAL_DEADLINE}s deadline")
            remaining = max(10, int(_TOTAL_DEADLINE - (time.time() - started)))
            data = self._post(body, min(timeout, remaining))
            text = self._extract_message(data).get("content") or ""
            cleaned = self._strip_code_fences(text)
            if cleaned.strip():
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    pass
            if attempt < 2:
                logger.warning("Empty/invalid LLM response, retrying (%s/2)", attempt + 1)
                time.sleep(1)
        raise OpenRouterRequestError(
            "LLM keeps returning empty responses. Your model is "
            f"'{self.model}' which is a free-tier model — free OpenRouter "
            "models are often overloaded. Try a paid model like "
            "'openai/gpt-4o-mini' or 'anthropic/claude-3-haiku' in your .env file."
        )

    def chat_text(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT) -> str:
        """Plain-text variant (no JSON forcing)."""
        self._require_key()

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }

        data = self._post(body, timeout)
        return self._extract_message(data).get("content") or ""

    def chat_with_search(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT):
        """Enables OpenRouter's built-in web-search plugin so the model can
        ground its answer in live results. Returns (answer_text, sources),
        where sources is a list of {"title": ..., "uri": ...} pulled from
        the response's citation annotations."""
        self._require_key()

        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "plugins": [{"id": "web"}],
            "stream": False,
        }

        data = self._post(body, timeout)
        message = self._extract_message(data)
        answer = message.get("content") or ""

        sources = []
        for annotation in message.get("annotations") or []:
            if not isinstance(annotation, dict):
                continue
            citation = annotation.get("url_citation")
            if isinstance(citation, dict) and citation.get("url"):
                sources.append({"title": citation.get("title", ""), "uri": citation["url"]})

        return answer, sources
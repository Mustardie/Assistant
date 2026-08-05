"""
Shared REST client for any provider that implements the OpenAI-style
``/chat/completions`` API shape (OpenAI itself, Groq, DeepSeek, and — kept
separate for historical reasons — OpenRouter).

Concrete providers subclass ``OpenAICompatibleClient`` and only need to set
``API_URL``, ``PROVIDER_LABEL`` and ``DOCS_URL``; everything else (retries,
error extraction, JSON-mode chat, plain chat) is shared here so provider
support is a ~20 line file, not a ~200 line copy/paste.
"""
import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.5
_REQUEST_TIMEOUT = 45
_TOTAL_DEADLINE = 90


class OpenAICompatibleConfigurationError(Exception):
    """Raised when a required API key is missing."""


class OpenAICompatibleRequestError(Exception):
    """Raised when the provider's API itself rejected the request."""


class OpenAICompatibleClient:
    API_URL: str = ""
    PROVIDER_LABEL: str = "provider"
    ENV_KEY_NAME: str = "API_KEY"
    DOCS_URL: str = ""

    def __init__(self, model: str, api_key: str, extra_headers: dict | None = None):
        self.model = model
        self.api_key = api_key or ""
        self._extra_headers = extra_headers or {}
        self._session = None

    def _require_key(self):
        if not self.api_key:
            raise OpenAICompatibleConfigurationError(
                f"{self.PROVIDER_LABEL} requires {self.ENV_KEY_NAME}. "
                "Set it from Settings -> API, or in your .env file."
            )

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self._extra_headers)
        return headers

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
        last_error_text = None
        session = self._get_session()

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = session.post(
                    self.API_URL, json=body, headers=self._headers(), timeout=timeout
                )
            except requests.RequestException as exc:
                last_error_text = str(exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise OpenAICompatibleRequestError(
                    f"Could not reach {self.PROVIDER_LABEL}: {exc}"
                ) from exc

            if response.status_code == 200:
                try:
                    return response.json()
                except json.JSONDecodeError:
                    last_error_text = f"{self.PROVIDER_LABEL} returned HTTP 200 with an empty body"
                    if attempt < _MAX_RETRIES:
                        time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    raise OpenAICompatibleRequestError(last_error_text)

            reason = self._extract_error_message(response)
            last_error_text = f"HTTP {response.status_code}: {reason}"

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                logger.warning(
                    "%s request failed (%s), retrying (%s/%s): %s",
                    self.PROVIDER_LABEL, response.status_code, attempt + 1, _MAX_RETRIES, reason,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            hint = ""
            if response.status_code in (401, 403):
                hint = f" -- check your {self.PROVIDER_LABEL} API key is correct and active"
                if self.DOCS_URL:
                    hint += f" at {self.DOCS_URL}"
            elif response.status_code == 404:
                hint = f" -- model '{self.model}' may not exist for {self.PROVIDER_LABEL}"
            elif response.status_code == 402:
                hint = f" -- your {self.PROVIDER_LABEL} account/key may be out of credits"
            raise OpenAICompatibleRequestError(f"{last_error_text}{hint}")

        raise OpenAICompatibleRequestError(
            last_error_text or f"{self.PROVIDER_LABEL} request failed for an unknown reason"
        )

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
            raise OpenAICompatibleRequestError(f"Provider returned no choices: {data}")
        return choices[0].get("message") or {}

    def chat_json(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT) -> dict:
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
                raise OpenAICompatibleRequestError(f"LLM call exceeded {_TOTAL_DEADLINE}s deadline")
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
        raise OpenAICompatibleRequestError(
            f"{self.PROVIDER_LABEL} model '{self.model}' kept returning empty/invalid responses."
        )

    def chat_text(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT) -> str:
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
        """Not natively supported by every OpenAI-compatible provider.
        Falls back to a plain chat call with no citations so callers that
        expect the (answer, sources) tuple still work."""
        answer = self.chat_text(system_prompt, user_prompt, timeout)
        return answer, []

import json
import logging
import re
import time

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.5
_REQUEST_TIMEOUT = 45
_TOTAL_DEADLINE = 90
_DEFAULT_MAX_TOKENS = 4096


class AnthropicConfigurationError(Exception):
    """Raised when ANTHROPIC_API_KEY is missing."""


class AnthropicRequestError(Exception):
    """Raised when Anthropic's API itself rejected the request."""


class AnthropicClient:
    """Thin REST wrapper around Anthropic's Messages API. Kept
    dependency-free (just `requests`) rather than pulling in the anthropic
    SDK, matching the style of the other provider clients in this package.
    """

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or settings.anthropic_model
        self.api_key = api_key or settings.anthropic_api_key
        self._session = None

    def _require_key(self):
        if not self.api_key:
            raise AnthropicConfigurationError(
                "Anthropic requires ANTHROPIC_API_KEY. Set it from "
                "Settings -> API, or in your .env file."
            )

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
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
        last_error_text = None
        session = self._get_session()

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = session.post(
                    ANTHROPIC_API_URL, json=body, headers=self._headers(), timeout=timeout
                )
            except requests.RequestException as exc:
                last_error_text = str(exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise AnthropicRequestError(f"Could not reach Anthropic: {exc}") from exc

            if response.status_code == 200:
                try:
                    return response.json()
                except json.JSONDecodeError:
                    last_error_text = "Anthropic returned HTTP 200 with an empty body"
                    if attempt < _MAX_RETRIES:
                        time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    raise AnthropicRequestError(last_error_text)

            reason = self._extract_error_message(response)
            last_error_text = f"HTTP {response.status_code}: {reason}"

            if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                logger.warning(
                    "Anthropic request failed (%s), retrying (%s/%s): %s",
                    response.status_code, attempt + 1, _MAX_RETRIES, reason,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue

            hint = ""
            if response.status_code in (401, 403):
                hint = " -- check ANTHROPIC_API_KEY is correct and active at https://console.anthropic.com/settings/keys"
            elif response.status_code == 404:
                hint = f" -- model '{self.model}' may not exist"
            raise AnthropicRequestError(f"{last_error_text}{hint}")

        raise AnthropicRequestError(last_error_text or "Anthropic request failed for an unknown reason")

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
    def _extract_text(data: dict) -> str:
        parts = data.get("content") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
        return "".join(texts)

    def chat_json(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT) -> dict:
        """Anthropic has no `response_format: json_object` toggle, so the
        JSON contract is enforced via the system prompt and parsed with
        retries, same recovery strategy as the other clients."""
        self._require_key()

        json_system_prompt = (
            f"{system_prompt}\n\nRespond with ONLY valid JSON. "
            "No markdown, no code fences, no commentary."
        )
        body = {
            "model": self.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "system": json_system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        started = time.time()
        for attempt in range(3):
            if time.time() - started > _TOTAL_DEADLINE:
                raise AnthropicRequestError(f"LLM call exceeded {_TOTAL_DEADLINE}s deadline")
            remaining = max(10, int(_TOTAL_DEADLINE - (time.time() - started)))
            data = self._post(body, min(timeout, remaining))
            text = self._extract_text(data)
            cleaned = self._strip_code_fences(text)
            if cleaned.strip():
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    pass
            if attempt < 2:
                logger.warning("Empty/invalid LLM response, retrying (%s/2)", attempt + 1)
                time.sleep(1)
        raise AnthropicRequestError(
            f"Anthropic model '{self.model}' kept returning empty/invalid JSON responses."
        )

    def chat_text(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT) -> str:
        self._require_key()

        body = {
            "model": self.model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        data = self._post(body, timeout)
        return self._extract_text(data)

    def chat_with_search(self, system_prompt: str, user_prompt: str, timeout: int = _REQUEST_TIMEOUT):
        """Anthropic's native web-search tool isn't wired here; falls back
        to a plain answer with no citations so callers expecting the
        (answer, sources) tuple still work."""
        answer = self.chat_text(system_prompt, user_prompt, timeout)
        return answer, []

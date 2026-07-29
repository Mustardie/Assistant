import json
import logging
import re
import time

import requests

from config.settings import settings

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Gemini's API genuinely does return transient 503s/429s under load even
# with a fully valid key -- retry those a couple of times before giving up.
_RETRYABLE_STATUS_CODES = {429, 500, 503}
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.5


class GeminiConfigurationError(Exception):
    """Raised when GEMINI_API_KEY is missing."""


class GeminiRequestError(Exception):
    """Raised when Gemini's API itself rejected the request. Carries the
    actual reason Google gave (bad key, model not found, quota, etc.)
    instead of a bare HTTP status code."""


class GeminiClient:
    """Thin REST wrapper around the Gemini generateContent endpoint.

    Kept deliberately small and dependency-free (just `requests`, already
    used elsewhere in this project) rather than pulling in the
    google-generativeai SDK.

    Google has been mid-migration through 2026 from "Standard" API keys
    (the classic "AIzaSy..." format, sent as a `?key=` query parameter) to
    "Auth" keys (the newer "AQ...." format, sent as an
    `Authorization: Bearer` header instead). Both are supported here,
    detected by prefix, since which one a given account/key issues has
    been changing over time.
    """

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or settings.gemini_model
        self.api_key = api_key or settings.gemini_api_key
        self._session = None

    def _require_key(self):
        if not self.api_key:
            raise GeminiConfigurationError(
                "Gemini requires GEMINI_API_KEY. Set it in your .env file."
            )

    def _is_auth_token(self) -> bool:
        """Newer-style "AQ." auth tokens go in an Authorization header;
        older-style "AIza" standard keys go in the ?key= query param."""
        return self.api_key.startswith("AQ.")

    def _auth_variants(self) -> list:
        """Returns a list of (query_suffix, headers) to try in order. Google's
        rollout of "AQ." auth-token keys has been inconsistent across
        accounts as of mid-2026 -- some projects accept them via one
        transport and reject the exact same key via another. Rather than
        commit to one and fail hard, try the known variants in order."""
        if self._is_auth_token():
            return [
                ("", {"Authorization": f"Bearer {self.api_key}"}),
                ("", {"x-goog-api-key": self.api_key}),
                (f"?key={self.api_key}", {}),
            ]
        return [(f"?key={self.api_key}", {})]

    def _get_session(self):
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _post(self, body: dict, timeout: int) -> dict:
        """POST to generateContent, trying each auth variant, retrying
        transient errors within a variant, and raising GeminiRequestError
        with Google's actual error message if every variant fails."""
        session = self._get_session()
        base_url = f"{GEMINI_API_BASE}/{self.model}:generateContent"
        variants = self._auth_variants()
        last_error_text = None

        for variant_index, (query_suffix, headers) in enumerate(variants):
            url = base_url + query_suffix

            for attempt in range(_MAX_RETRIES + 1):
                try:
                    response = session.post(url, json=body, headers=headers, timeout=timeout)
                except requests.RequestException as exc:
                    last_error_text = str(exc)
                    if attempt < _MAX_RETRIES:
                        time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    break

                if response.status_code == 200:
                    return response.json()

                reason = self._extract_error_message(response)
                last_error_text = f"HTTP {response.status_code}: {reason}"

                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    logger.warning(
                        "Gemini request failed (%s), retrying (%s/%s): %s",
                        response.status_code, attempt + 1, _MAX_RETRIES, reason,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue

                if response.status_code == 401 and variant_index < len(variants) - 1:
                    logger.warning(
                        "Auth variant %s rejected (401); trying the next one: %s",
                        variant_index + 1, reason,
                    )
                    break  # move to next auth variant

                hint = ""
                if response.status_code in (400, 401, 403):
                    hint = (
                        " -- all known auth methods for this key were rejected. "
                        "This matches a currently unresolved Google-side issue with "
                        "newer 'AQ.' keys (see https://discuss.ai.google.dev, search "
                        "'ACCESS_TOKEN_TYPE_UNSUPPORTED'). Try an older AIza-format "
                        "key if you have one, or fall back to Ollama for now."
                    )
                elif response.status_code == 404:
                    hint = f" -- model '{self.model}' may not exist or isn't available to your key; check GEMINI_MODEL"
                raise GeminiRequestError(f"{last_error_text}{hint}")

        raise GeminiRequestError(
            f"{last_error_text} -- all auth methods failed. This matches a currently "
            "unresolved Google-side issue with newer 'AQ.' keys; consider falling back "
            "to Ollama until it's resolved on Google's end."
        )

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        import re
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned

    @staticmethod
    def _extract_error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
            return str(payload.get("error", {}).get("message", response.text))
        except Exception:
            return response.text[:300]

    @staticmethod
    def _extract_candidates(data: dict) -> list:
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise GeminiRequestError(f"Gemini returned no candidates (blockReason={block_reason})")
        return candidates

    def chat_json(self, system_prompt: str, user_prompt: str, timeout: int = 120) -> dict:
        """Send one system+user turn, force JSON output via Gemini's
        responseMimeType, and parse the result. Raises on any failure --
        callers are expected to catch and degrade gracefully, same as the
        rest of this codebase does around external calls."""
        self._require_key()

        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }

        data = self._post(body, timeout)
        candidates = self._extract_candidates(data)

        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if "text" in part)

        cleaned = self._strip_code_fences(text)
        return json.loads(cleaned)

    def chat_with_search(self, system_prompt: str, user_prompt: str, timeout: int = 120):
        """Send one turn with Gemini's built-in Google Search grounding tool
        enabled. Returns (answer_text, sources) where sources is a list of
        {"title": ..., "uri": ...} dicts pulled from groundingMetadata.
        This is real live web search, unlike the plain chat_text/chat_json
        calls which only draw on the model's own training data."""
        self._require_key()

        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "tools": [{"google_search": {}}],
        }

        data = self._post(body, timeout)
        candidates = self._extract_candidates(data)
        candidate = candidates[0]

        parts = (candidate.get("content") or {}).get("parts") or []
        answer = "".join(part.get("text", "") for part in parts if "text" in part)

        sources = []
        grounding = candidate.get("groundingMetadata") or {}
        for chunk in grounding.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            if web.get("uri"):
                sources.append({"title": web.get("title", ""), "uri": web["uri"]})

        return answer, sources

    def chat_text(self, system_prompt: str, user_prompt: str, timeout: int = 120) -> str:
        """Plain-text variant (no JSON forcing) for cases like memory
        classification prompts that just need free text back, if ever
        needed."""
        self._require_key()

        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        }

        data = self._post(body, timeout)
        candidates = self._extract_candidates(data)

        parts = (candidates[0].get("content") or {}).get("parts") or []
        return "".join(part.get("text", "") for part in parts if "text" in part)
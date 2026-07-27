import json
import logging
import re
from typing import Any, Dict, Optional

from llm.openrouter_client import OpenRouterClient

logger = logging.getLogger(__name__)


class LLMClient:
    """Adapter so query_generator.py and ranker.py can keep calling
    complete()/complete_json() unchanged, backed by the same OpenRouterClient
    used everywhere else in Nova now.

    This used to call the Gemini SDK directly and was never migrated when
    the rest of the app switched providers -- that's what caused
    youtube_recommend to keep hitting Gemini's 503s after the switch.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 120,
    ):
        self.timeout = timeout
        self._client = OpenRouterClient(model=model, api_key=api_key)

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Dict[str, Any]:
        raw = self.complete(system_prompt, user_prompt, json_mode=True)
        try:
            return self._parse_json(raw)
        except RuntimeError as exc:
            logger.warning("Initial JSON parse failed: %s", exc)
            retry_prompt = (
                f"{user_prompt}\n\nIMPORTANT: Return ONLY valid JSON. "
                "Do not add explanations, markdown, or code fences."
            )
            retry_raw = self.complete(system_prompt, retry_prompt, json_mode=True)
            try:
                return self._parse_json(retry_raw)
            except RuntimeError as retry_exc:
                logger.warning("Retry JSON parse failed: %s", retry_exc)
                return self._extract_json_fallback(retry_raw)

    def complete(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        logger.debug("Calling LLM model=%s json_mode=%s", self._client.model, json_mode)

        effective_system_prompt = system_prompt
        if json_mode:
            # Ask nicely for JSON too (harmless if the routed model ignores
            # it) -- but _parse_json below does the real, robust extraction
            # rather than trusting this blindly, since "openrouter/free" can
            # route to different underlying models with inconsistent
            # JSON-mode support.
            effective_system_prompt = (
                f"{system_prompt}\n\n"
                "Respond with ONLY valid JSON. No markdown, no code fences, no explanations."
            )

        try:
            content = self._client.chat_text(effective_system_prompt, user_prompt, timeout=self.timeout)
        except Exception as exc:
            logger.exception("LLM request failed")
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        if not content:
            logger.error("Empty LLM response")
            raise RuntimeError("LLM returned an invalid response format")

        logger.debug("LLM raw output length=%s", len(content))
        return content.strip()

    def _parse_json(self, raw: str) -> Dict[str, Any]:
        cleaned = raw.strip()

        if not cleaned:
            raise RuntimeError("LLM returned empty content")

        if cleaned.startswith("```"):
            cleaned = re.sub(
                r"^```(?:json)?\s*",
                "",
                cleaned,
                flags=re.IGNORECASE,
            )
            cleaned = re.sub(r"\s*```$", "", cleaned)

        for candidate in self._extract_json_candidates(cleaned):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
                if isinstance(parsed, list):
                    return {"ranked": parsed}
            except json.JSONDecodeError:
                continue

        logger.error("Failed to parse LLM JSON output: %s", raw)
        raise RuntimeError("LLM did not return valid JSON")

    def _extract_json_candidates(self, text: str) -> list[str]:
        candidates: list[str] = []
        if not text:
            return candidates

        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            candidates.append(stripped)

        fenced_matches = re.findall(
            r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```",
            stripped,
            flags=re.IGNORECASE | re.DOTALL,
        )
        candidates.extend(fenced_matches)

        brace_matches = re.findall(r"(\{.*?\})", stripped, flags=re.DOTALL)
        candidates.extend(brace_matches)
        bracket_matches = re.findall(r"(\[.*?\])", stripped, flags=re.DOTALL)
        candidates.extend(bracket_matches)

        return candidates

    def _extract_json_fallback(self, raw: str) -> Dict[str, Any]:
        logger.warning("Falling back to empty JSON payload after parse failure: %s", raw)
        return {"ranked": []}
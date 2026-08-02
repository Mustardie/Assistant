"""Vision understanding for the Skills subsystem.

Turns screenshots into structured UI frames:

    {
      "width": ..., "height": ...,          # analyzed image size
      "window": {"title": ..., "app": ...},
      "objects": [UIObject, ...],           # buttons, inputs, menus, icons...
      "text": "all visible OCR text"
    }

Every action during recording/playback references these UI objects instead
of raw pixels. Providers (Gemini when configured, else a local Ollama
multimodal model) are tried in order; a lightweight LRU cache keeps
repeated analysis of the same frame free. OCR may additionally use
pytesseract when it is installed.
"""

import base64
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

_OBJECT_TYPES = (
    "button", "input", "menu", "menuitem", "icon", "title", "text",
    "link", "image", "checkbox", "radio", "dropdown", "tab", "dialog",
    "window", "panel", "card", "badge",
)

_CACHE_MAX = 64


@dataclass
class UIObject:
    id: str
    type: str
    text: str
    bbox: tuple[int, int, int, int]  # x, y, width, height (image pixels)
    confidence: float = 1.0
    extra: dict = field(default_factory=dict)

    @property
    def cx(self) -> float:
        return self.bbox[0] + self.bbox[2] / 2

    @property
    def cy(self) -> float:
        return self.bbox[1] + self.bbox[3] / 2

    @property
    def center(self) -> tuple[float, float]:
        return (self.cx, self.cy)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "bbox": list(self.bbox),
            "confidence": round(self.confidence, 3),
            "extra": self.extra,
        }

    @staticmethod
    def from_dict(data: dict) -> "UIObject":
        bbox = tuple(int(v) for v in data.get("bbox", [0, 0, 0, 0]))
        return UIObject(
            id=str(data.get("id", "")),
            type=str(data.get("type", "text")),
            text=str(data.get("text", "")),
            bbox=bbox,
            confidence=float(data.get("confidence", 1.0)),
            extra=dict(data.get("extra") or {}),
        )


def bbox_contains(bbox, x: float, y: float) -> bool:
    bx, by, bw, bh = bbox
    return bx <= x <= bx + bw and by <= y <= by + bh


# --------------------------------------------------------------------- #
# JSON extraction helpers (models are chatty)
# --------------------------------------------------------------------- #

def _extract_json(text: str) -> Any | None:
    """Best-effort JSON extraction from model output: strip code fences,
    then find the first balanced {...} or [...] block."""
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start == -1:
        start = cleaned.find("[")
    if start != -1:
        depth = 0
        in_str = False
        escape = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_str:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_str = False
            elif char == '"':
                in_str = True
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start : index + 1])
                    except json.JSONDecodeError:
                        return None
    return None


# --------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------- #

class _ProviderError(Exception):
    pass


class _GeminiVision:
    """Gemini generateContent with inline image payloads."""

    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, model: str, api_key: str):
        self.model = model
        self.api_key = api_key

    @staticmethod
    def _auth_variants(api_key: str) -> list:
        if api_key.startswith("AQ."):
            return [
                ("", {"Authorization": f"Bearer {api_key}"}),
                ("", {"x-goog-api-key": api_key}),
                (f"?key={api_key}", {}),
            ]
        return [(f"?key={api_key}", {})]

    def _post(self, body: dict, timeout: int = 120) -> dict:
        import requests

        url = f"{self.BASE}/{self.model}:generateContent"
        last_error = "unknown error"
        for query, headers in self._auth_variants(self.api_key):
            try:
                response = requests.post(url, json=body, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
                continue
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        raise _ProviderError(f"Gemini vision failed: {last_error}")

    def chat_json(self, system_prompt: str, user_prompt: str, image) -> dict:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=settings.skills_screenshot_quality)
        image_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": user_prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                ],
            }],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        data = self._post(body)
        candidates = data.get("candidates") or []
        if not candidates:
            raise _ProviderError("Gemini vision returned no candidates")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if "text" in part)
        parsed = _extract_json(text)
        if parsed is None:
            raise _ProviderError("Gemini vision returned non-JSON output")
        return parsed


class _OllamaVision:
    """Local multimodal model via Ollama's /api/chat with format=json."""

    def __init__(self, model: str, url: str):
        self.model = model
        raw_url = (url or "http://localhost:11434/api/chat").rstrip("/")
        self._api_url = raw_url if raw_url.endswith("/api/chat") else f"{raw_url}/api/chat"

    def chat_json(self, system_prompt: str, user_prompt: str, image) -> dict:
        import requests

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=settings.skills_screenshot_quality)
        image_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt, "images": [image_b64]},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.0},
        }
        try:
            response = requests.post(self._api_url, json=body, timeout=300)
        except requests.RequestException as exc:
            raise _ProviderError(f"Ollama vision unreachable: {exc}") from exc
        if response.status_code != 200:
            raise _ProviderError(f"Ollama vision HTTP {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise _ProviderError("Ollama vision returned empty body") from exc
        text = (payload.get("message") or {}).get("content") or ""
        parsed = _extract_json(text)
        if parsed is None:
            raise _ProviderError("Ollama vision returned non-JSON output")
        return parsed


class _PytesseractOcr:
    """Optional local OCR -- only used when pytesseract is importable."""

    @staticmethod
    def available() -> bool:
        try:
            import pytesseract  # noqa: F401

            return True
        except Exception:
            return False

    @classmethod
    def ocr(cls, image) -> str:
        import pytesseract

        try:
            return pytesseract.image_to_string(image)
        except Exception:
            return ""


# --------------------------------------------------------------------- #
# Main facade
# --------------------------------------------------------------------- #

_SYSTEM_PROMPT = f"""You are the UI understanding engine for a desktop automation system.
Analyze the screenshot and return STRICT JSON with this exact shape:
{{
  "objects": [
    {{
      "type": one of {sorted(_OBJECT_TYPES)},
      "text": "visible text of the element, or empty for icons",
      "bbox": [x, y, width, height],
      "confidence": 0.0-1.0
    }}
  ],
  "text": "every visible piece of text, in reading order"
}}
Rules:
- Detect interactive UI elements: buttons, text inputs, menus, menu items, tabs,
  checkboxes, radio buttons, dropdowns, links, icons, dialogs, and the window title.
- bbox coordinates are in pixels of the provided image.
- For icons/images with no text, set text to a short descriptive label like
  "search icon" or "send icon".
- Include ALL important text such as headings and field labels.
- Do not include whole-paragraph body text as objects; put it in "text" instead.
- Never include coordinates in "text"."""


class ScreenVision:
    """Analyzes screenshots into UI objects + OCR text, with caching."""

    def __init__(self, cache_size: int = _CACHE_MAX):
        self._cache: dict[str, dict] = {}
        self._cache_size = cache_size
        self._gemini = None
        self._ollama = None

    # -- providers --------------------------------------------------- #

    def _get_gemini(self):
        if self._gemini is None and settings.gemini_api_key:
            self._gemini = _GeminiVision(settings.gemini_model, settings.gemini_api_key)
        return self._gemini

    def _get_ollama(self):
        if self._ollama is None:
            self._ollama = _OllamaVision(settings.ollama_model, settings.ollama_url)
        return self._ollama

    def _providers(self):
        providers = []
        gemini = self._get_gemini()
        if gemini is not None:
            providers.append(("gemini", gemini))
        providers.append(("ollama", self._get_ollama()))
        return providers

    # -- cache -------------------------------------------------------- #

    @staticmethod
    def _fingerprint(image) -> str:
        small = image.convert("RGB").resize((64, 64))
        digest = hashlib.sha256(small.tobytes()).hexdigest()
        return digest[:20]

    def _cache_get(self, key: str):
        return self._cache.get(key)

    def _cache_put(self, key: str, value: dict):
        if len(self._cache) >= self._cache_size:
            self._cache.clear()
        self._cache[key] = value

    # -- public API --------------------------------------------------- #

    def analyze(self, image) -> dict:
        """Analyze a screenshot into a UI frame dict:
        {"width", "height", "objects": [...], "text": str}.
        Never raises -- degrades to an empty frame on provider failure."""
        key = self._fingerprint(image)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        result = {"width": image.width, "height": image.height, "objects": [], "text": ""}
        for provider_name, provider in self._providers():
            try:
                data = provider.chat_json(_SYSTEM_PROMPT, "Return the UI elements JSON.", image)
            except Exception as exc:
                logger.warning("[SkillsVision] %s analysis failed: %s", provider_name, exc)
                continue
            result = self._normalize_analysis(data, image)
            if result["objects"] or result["text"]:
                break
        if not result["text"] and _PytesseractOcr.available():
            result["text"] = _PytesseractOcr.ocr(image)
        self._cache_put(key, result)
        return result

    def locate(self, image, description: str) -> dict | None:
        """Ask the vision model where an element described in natural
        language is. Returns a UIObject dict or None. Used as the recovery
        step when matching against stored objects fails."""
        prompt = (
            f'Locate the UI element described as: "{description}".\n'
            'Return JSON: {"found": true, "object": {"type": "...", "text": "...", '
            '"bbox": [x, y, width, height], "confidence": 0.0-1.0}} if you see it, '
            'or {"found": false}.'
        )
        for provider_name, provider in self._providers():
            try:
                data = provider.chat_json(_SYSTEM_PROMPT, prompt, image)
            except Exception as exc:
                logger.warning("[SkillsVision] %s locate failed: %s", provider_name, exc)
                continue
            if isinstance(data, dict) and data.get("found"):
                obj = data.get("object") or {}
                if isinstance(obj, dict) and obj.get("bbox"):
                    return {
                        "type": str(obj.get("type", "button")),
                        "text": str(obj.get("text", "")),
                        "bbox": [int(v) for v in obj["bbox"]],
                        "confidence": float(obj.get("confidence", 0.6)),
                        "source": provider_name,
                    }
        return None

    def ocr(self, image) -> str:
        frame = self.analyze(image)
        if frame["text"]:
            return frame["text"]
        return _PytesseractOcr.ocr(image) if _PytesseractOcr.available() else ""

    # -- normalization ------------------------------------------------ #

    @staticmethod
    def _normalize_analysis(data, image) -> dict:
        width, height = image.size
        objects = []
        seen = set()
        for index, raw in enumerate((data or {}).get("objects") or []):
            if not isinstance(raw, dict):
                continue
            bbox = raw.get("bbox") or []
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            try:
                x, y, w, h = (max(0, int(float(v))) for v in bbox)
            except (TypeError, ValueError):
                continue
            if w <= 0 or h <= 0:
                continue
            obj_type = str(raw.get("type", "text")).lower().strip()
            if obj_type not in _OBJECT_TYPES:
                obj_type = "text"
            text = str(raw.get("text", "")).strip()[:200]
            key = (obj_type, text, x, y)
            if key in seen:
                continue
            seen.add(key)
            objects.append(UIObject(
                id=f"o{index}",
                type=obj_type,
                text=text,
                bbox=(x, y, w, h),
                confidence=max(0.0, min(1.0, float(raw.get("confidence", 1.0)))),
            ))
        return {
            "width": width,
            "height": height,
            "objects": [obj.to_dict() for obj in objects],
            "text": str((data or {}).get("text", "")).strip(),
        }


screen_vision = ScreenVision()

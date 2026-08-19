"""The Qwen3-VL client.

This layer never loads model weights. It talks to whatever is already serving
``Qwen3-VL-8B-Instruct`` locally, through one of two wire formats:

``openai``  an OpenAI-compatible ``/chat/completions`` endpoint. This is what
            vLLM, LM Studio, llama.cpp's server and SGLang all expose, so it is
            the default and covers most local setups.
``ollama``  Ollama's ``/api/chat``, which takes images as a separate base64
            list rather than inline content parts.
``mock``    a deterministic fake, for tests and for exercising the pipeline
            without a GPU.

The two real backends differ only in how a request is built and where the text
is found in the response; retries, JSON extraction and error typing are shared.

Everything returns a **dict**, never prose. A model that answers with an
apology, a markdown fence or a half-object is handled here so that by the time
a response reaches ``analyzer`` it is either a parsed object or a typed
``ModelError``. Nothing downstream should ever see raw model text.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from editing.config import EditingConfig
from editing.errors import ModelError

logger = logging.getLogger("nova.editing.visual.qwen")

#: Backoff between retries. Local servers usually fail fast (model still
#: loading, queue full) and recover quickly, so the first retry is short.
_RETRY_DELAYS = (1.0, 3.0, 8.0)

#: HTTP statuses worth retrying: the server is up but temporarily unable.
_RETRYABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class VisionModel(Protocol):
    """What the analyzer requires of a vision backend."""

    name: str

    def analyze(
        self, frames: Sequence[Path], *, system: str, user: str
    ) -> dict: ...


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


def extract_json(text: str) -> dict:
    """Pull the JSON object out of a model response.

    Small VLMs wrap their answer in a markdown fence, prefix it with "Here is
    the analysis:", or append a sentence after the closing brace. Rather than
    forbid all of that in the prompt and hope, this finds the first complete
    top-level object by scanning braces -- string-aware, so a ``}`` inside
    ``"notes"`` does not end the scan early.
    """
    if not isinstance(text, str) or not text.strip():
        raise ModelError(
            "The vision model returned an empty response",
            hint="Check the model server is serving a vision model and did not "
                 "run out of context.",
        )

    stripped = _FENCE.sub("", text.strip())
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    candidate = _first_object(stripped)
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise ModelError(
                "The vision model's JSON object was malformed",
                hint="Lower the temperature, or check the served model is "
                     "instruction-tuned.",
                detail={"reason": str(exc), "response": stripped[:400]},
            ) from exc

    raise ModelError(
        "No JSON object found in the vision model's response",
        hint="The model answered with prose. Check it is an -Instruct build "
             "and that the system prompt reached it.",
        detail={"response": stripped[:400]},
    )


def _first_object(text: str) -> Optional[str]:
    """The first balanced ``{...}`` span, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def encode_image(path: str | Path) -> str:
    """Base64 a frame. Raises rather than sending an empty image."""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ModelError(
            f"Could not read frame {target.name}",
            detail={"reason": str(exc)},
        ) from exc
    if not data:
        raise ModelError(f"Frame {target.name} is empty")
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# HTTP backends
# ---------------------------------------------------------------------------

class _HttpVision:
    """Shared retry loop and error typing for the HTTP backends."""

    def __init__(self, config: EditingConfig):
        self.config = config
        self.name = config.vision_model
        self.timeout = float(config.vision_timeout)
        self.max_retries = max(0, int(config.vision_max_retries))
        self._session = None

    # -- transport ------------------------------------------------------

    @property
    def session(self):
        if self._session is None:
            try:
                import requests
            except ImportError as exc:
                raise ModelError(
                    "The 'requests' package is required to reach the vision model",
                    hint="pip install requests",
                    detail={"reason": str(exc)},
                ) from None
            self._session = requests.Session()
            # The model server is on loopback. This environment sets a global
            # HTTPS_PROXY, and routing a localhost request through it fails.
            self._session.trust_env = False
        return self._session

    def _post(self, url: str, body: dict) -> dict:
        import requests

        last: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(url, json=body, timeout=self.timeout)
            except requests.Timeout as exc:
                last = f"timed out after {self.timeout:.0f}s"
                logger.debug("Vision request timeout: %s", exc)
            except requests.RequestException as exc:
                last = str(exc)
                logger.debug("Vision request failed: %s", exc)
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ModelError(
                            "The vision server returned a non-JSON response",
                            detail={"reason": str(exc),
                                    "body": (response.text or "")[:300]},
                        ) from None
                if response.status_code not in _RETRYABLE:
                    raise ModelError(
                        f"The vision server rejected the request "
                        f"(HTTP {response.status_code})",
                        hint=self._status_hint(response.status_code),
                        detail={"body": (response.text or "")[:300], "url": url},
                    )
                last = f"HTTP {response.status_code}: {(response.text or '')[:200]}"

            if attempt < self.max_retries:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                logger.debug("Retrying vision request in %.1fs (%s)", delay, last)
                time.sleep(delay)

        raise ModelError(
            f"Could not reach the vision model after {self.max_retries + 1} attempt(s)",
            hint=self._unreachable_hint(),
            detail={"url": url, "last_error": last},
        )

    @staticmethod
    def _status_hint(status: int) -> str:
        if status in (401, 403):
            return "Set EDITING_VISION_API_KEY if your server requires one."
        if status == 404:
            return ("Check EDITING_VISION_BASE_URL points at the API root "
                    "(usually ending in /v1).")
        if status == 413:
            return ("The request was too large. Lower EDITING_FRAME_WIDTH or "
                    "EDITING_FRAMES_PER_WINDOW.")
        return ""

    def _unreachable_hint(self) -> str:
        return (
            f"Start a server for {self.config.vision_model} (weights are "
            f"expected at {self.config.model_dir}) and check "
            f"EDITING_VISION_BASE_URL={self.config.vision_base_url}. "
            "Use EDITING_VISION_BACKEND=mock to exercise the pipeline without one."
        )

    # -- health ---------------------------------------------------------

    def health(self) -> dict:
        """Best-effort reachability check, for the CLI to report before a run."""
        raise NotImplementedError


class OpenAICompatibleVision(_HttpVision):
    """vLLM / LM Studio / llama.cpp-server / SGLang, via /chat/completions."""

    def _url(self, suffix: str) -> str:
        return f"{self.config.vision_base_url.rstrip('/')}{suffix}"

    def analyze(self, frames: Sequence[Path], *, system: str, user: str) -> dict:
        content: list[dict] = [{"type": "text", "text": user}]
        for frame in frames:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{encode_image(frame)}"
                },
            })

        body = {
            "model": self.config.vision_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            # Near-zero temperature: this is an extraction task, and a
            # re-analysis of unchanged footage should agree with the cached
            # answer it replaces.
            "temperature": 0.1,
            "top_p": 0.8,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        }

        headers_key = self.config.vision_api_key
        if headers_key and headers_key != "not-needed":
            self.session.headers.update({"Authorization": f"Bearer {headers_key}"})

        payload = self._post(self._url("/chat/completions"), body)
        return extract_json(self._text_of(payload))

    @staticmethod
    def _text_of(payload: dict) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise ModelError(
                "The vision server returned no choices",
                detail={"payload": str(payload)[:300]},
            )
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # Some servers return content parts even for text-only answers.
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return str(content or choices[0].get("text") or "")

    def health(self) -> dict:
        try:
            import requests
        except ImportError:
            return {"reachable": False, "error": "requests is not installed"}
        try:
            response = self.session.get(self._url("/models"), timeout=10.0)
            served = []
            if response.status_code == 200:
                served = [
                    entry.get("id")
                    for entry in (response.json().get("data") or [])
                ]
            return {
                "reachable": response.status_code == 200,
                "status": response.status_code,
                "backend": "openai",
                "url": self.config.vision_base_url,
                "models": served,
                "model_served": (not served) or self.config.vision_model in served,
            }
        except requests.RequestException as exc:
            return {
                "reachable": False,
                "backend": "openai",
                "url": self.config.vision_base_url,
                "error": str(exc),
                "hint": self._unreachable_hint(),
            }


class OllamaVision(_HttpVision):
    """Ollama's /api/chat, which carries images as a base64 list."""

    def _root(self) -> str:
        base = self.config.vision_base_url.rstrip("/")
        for suffix in ("/api/chat", "/api", "/v1"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    def analyze(self, frames: Sequence[Path], *, system: str, user: str) -> dict:
        body = {
            "model": self.config.vision_model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": user,
                    "images": [encode_image(frame) for frame in frames],
                },
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1, "top_p": 0.8},
        }
        payload = self._post(f"{self._root()}/api/chat", body)
        message = payload.get("message") or {}
        return extract_json(str(message.get("content") or payload.get("response") or ""))

    def health(self) -> dict:
        try:
            import requests
        except ImportError:
            return {"reachable": False, "error": "requests is not installed"}
        try:
            response = self.session.get(f"{self._root()}/api/tags", timeout=10.0)
            served = []
            if response.status_code == 200:
                served = [
                    entry.get("name") for entry in (response.json().get("models") or [])
                ]
            return {
                "reachable": response.status_code == 200,
                "status": response.status_code,
                "backend": "ollama",
                "url": self._root(),
                "models": served,
                "model_served": any(
                    str(name).startswith(self.config.vision_model) for name in served
                ) if served else True,
            }
        except requests.RequestException as exc:
            return {
                "reachable": False,
                "backend": "ollama",
                "url": self._root(),
                "error": str(exc),
                "hint": self._unreachable_hint(),
            }


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

class MockVisionModel:
    """A deterministic stand-in for the real model.

    Two uses: unit tests, and letting a user run the whole pipeline end to end
    (``EDITING_VISION_BACKEND=mock``) to check discovery, sampling, caching and
    alignment before setting up a GPU server.

    Answers are derived from the window's own start time, so the same window
    always gets the same answer -- which is what makes cache behaviour testable.
    Every response is deliberately marked ``mock: true`` in its notes so a mock
    result can never be mistaken for a real analysis in an output file.
    """

    def __init__(self, name: str = "mock-Qwen3-VL", responses: Optional[list] = None):
        self.name = name
        #: Canned responses, consumed in order. Falls back to the derived
        #: answer once exhausted.
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def analyze(self, frames: Sequence[Path], *, system: str, user: str) -> dict:
        self.calls.append({
            "frames": [str(frame) for frame in frames],
            "system": system,
            "user": user,
        })
        if self.responses:
            answer = self.responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return dict(answer)
        return self._derive(user)

    @staticmethod
    def _derive(user: str) -> dict:
        match = re.search(r"Window:\s*([0-9.]+)s to ([0-9.]+)s", user)
        start = float(match.group(1)) if match else 0.0
        end = float(match.group(2)) if match else 1.0

        # A repeating pattern, so a mock run produces a timeline with variety
        # in it rather than 400 identical segments.
        bucket = int(start) % 4
        table = [
            ("cave", ["mining"], [], "setup", 0.4),
            ("cave", ["fighting"], ["zombie"], "danger", 0.8),
            ("forest", ["travelling"], ["sheep"], "boring", 0.2),
            ("base", ["building"], [], "payoff", 0.7),
        ][bucket]
        environment, actions, entities, importance, confidence = table
        return {
            "environment": environment,
            "actions": actions,
            "entities": entities,
            "threats": entities if importance == "danger" else [],
            "ui": {"low_health": importance == "danger"},
            "camera": {"motion": "walk", "intensity": 0.3},
            "importance": importance,
            "confidence": confidence,
            "suggested_range": {"start": start, "end": end},
            "notes": f"mock: true -- generated answer for {start:.2f}s-{end:.2f}s",
        }

    def health(self) -> dict:
        return {"reachable": True, "backend": "mock", "model_served": True,
                "note": "Mock backend: no real analysis is performed."}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

BACKENDS = {
    "openai": OpenAICompatibleVision,
    "openai_compatible": OpenAICompatibleVision,
    "vllm": OpenAICompatibleVision,
    "lmstudio": OpenAICompatibleVision,
    "llamacpp": OpenAICompatibleVision,
    "ollama": OllamaVision,
}


def build_model(config: EditingConfig) -> VisionModel:
    """The vision backend named by ``config.vision_backend``."""
    backend = (config.vision_backend or "openai").strip().lower()
    if backend == "mock":
        return MockVisionModel(name=f"mock:{config.vision_model}")
    factory = BACKENDS.get(backend)
    if factory is None:
        raise ModelError(
            f"Unknown vision backend '{backend}'",
            hint="Set EDITING_VISION_BACKEND to one of: "
                 + ", ".join(sorted(set(BACKENDS) | {"mock"})),
        )
    return factory(config)


def health(config: EditingConfig) -> dict:
    """Reachability of the configured backend, without raising."""
    try:
        model = build_model(config)
    except ModelError as exc:
        return {"reachable": False, "error": exc.message, "hint": exc.hint}
    checker = getattr(model, "health", None)
    if checker is None:
        return {"reachable": True, "note": "backend does not report health"}
    return checker()

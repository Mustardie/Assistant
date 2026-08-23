"""Where the director's judgement actually comes from.

Two backends ship, and the split between them is the same one Session 10A
drew for transcription: a real one that talks to a server, and a mock that is
never quiet about being a mock.

## Provider-agnostic on purpose

``OpenAICompatibleDirector`` speaks ``/chat/completions``, which is what vLLM,
LM Studio, llama.cpp's server, SGLang, OpenRouter, Together, DeepInfra, Groq
and OpenAI itself all serve. So "which provider" is a base URL and a model
name, both configurable, and nothing in this package names a vendor. Pointing
it at a different service is two environment variables, not a code change.

The settings are deliberately *separate* from the vision model's. The two jobs
want different models -- one reads pictures window by window, one reasons over
a whole document -- and a machine serving Qwen3-VL on :8000 may well want
something else for this. Sharing the config would make that impossible to say.

## The JSON contract

The model is asked for one JSON object. What comes back is frequently not
quite that: a markdown fence, a sentence of preamble, a trailing "Let me know
if you'd like me to adjust!". ``editing.visual.qwen.extract_json`` already
solves exactly this problem -- brace-scanning that is string-aware -- so this
module reuses it rather than growing a second parser that will disagree with
the first.

**What is never done:** inventing a decision because the answer was
unreadable. A response that cannot be parsed is a typed failure with the first
part of the response attached, and the pass falls back to the heuristic
selector -- which is a worse cut, and an honest one.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional, Protocol

from editing.errors import ModelError
from editing.director.schema import DirectorConfig

logger = logging.getLogger("nova.editing.director.backends")

#: HTTP statuses worth another attempt. Same set the vision client uses.
RETRYABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
RETRY_DELAYS = (1.0, 3.0, 8.0)


class DirectorModel(Protocol):
    """What the director pass needs from a model."""

    name: str

    def complete(self, *, system: str, user: str) -> str:
        """Return the model's raw text answer."""

    def health(self) -> dict:
        """Whether this could run right now, without running it."""


class OpenAICompatibleDirector:
    """Any server that speaks ``/chat/completions``.

    Which is nearly all of them, local and hosted. The request asks for JSON
    via ``response_format`` when the server supports it and simply ignores
    that field otherwise -- there is no way to know in advance, and the prompt
    asks for JSON anyway, so this is an optimisation rather than a dependency.
    """

    def __init__(self, config: DirectorConfig):
        self.config = config.validated()
        self.name = self.config.model
        self._session = None

    # -- transport --------------------------------------------------------

    @property
    def session(self):
        if self._session is None:
            try:
                import requests
            except ImportError as exc:
                raise ModelError(
                    "The 'requests' package is required to reach the director "
                    "model",
                    hint="pip install requests",
                    detail={"reason": str(exc)},
                ) from None
            self._session = requests.Session()
            # A local model server is on loopback; this environment sets a
            # global HTTPS_PROXY and routing localhost through it fails.
            self._session.trust_env = not _is_local(self.config.base_url)
        return self._session

    def _url(self, suffix: str) -> str:
        return f"{self.config.base_url.rstrip('/')}{suffix}"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        key = self.config.api_key
        if key and key != "not-needed":
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def complete(self, *, system: str, user: str) -> str:
        import requests

        body = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            # Honoured by vLLM, LM Studio and OpenAI; ignored elsewhere.
            "response_format": {"type": "json_object"},
        }

        last: Optional[str] = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.post(
                    self._url("/chat/completions"),
                    json=body,
                    headers=self._headers(),
                    timeout=self.config.timeout,
                )
            except requests.Timeout:
                last = f"timed out after {self.config.timeout:.0f}s"
            except requests.RequestException as exc:
                last = str(exc)[:200]
            else:
                if response.status_code == 200:
                    return _text_of(_json_of(response))
                if response.status_code == 400 and "response_format" in \
                        (response.text or ""):
                    # A server that rejects the JSON-mode hint rather than
                    # ignoring it. Drop it and try again; the prompt still
                    # asks for JSON.
                    body.pop("response_format", None)
                    last = "server rejected response_format; retrying without"
                elif response.status_code not in RETRYABLE:
                    raise ModelError(
                        f"The director model rejected the request "
                        f"(HTTP {response.status_code})",
                        hint=_status_hint(response.status_code),
                        detail={"body": (response.text or "")[:300],
                                "url": self._url("/chat/completions")},
                    )
                else:
                    last = (f"HTTP {response.status_code}: "
                            f"{(response.text or '')[:200]}")

            if attempt < self.config.max_retries:
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                logger.debug("Retrying director request in %.1fs (%s)",
                             delay, last)
                time.sleep(delay)

        raise ModelError(
            f"Could not reach the director model after "
            f"{self.config.max_retries + 1} attempt(s)",
            hint=self._unreachable_hint(),
            detail={"url": self._url("/chat/completions"), "last_error": last},
        )

    def _unreachable_hint(self) -> str:
        return (
            f"Start a server for '{self.config.model}' and check "
            f"EDITING_DIRECTOR_BASE_URL={self.config.base_url}. Any "
            "OpenAI-compatible endpoint works -- vLLM, LM Studio, "
            "llama.cpp-server, or a hosted API with "
            "EDITING_DIRECTOR_API_KEY set. Use --backend mock to exercise the "
            "pipeline without one."
        )

    def health(self) -> dict:
        """Best-effort reachability check. Never raises."""
        base = {
            "backend": "openai",
            "model": self.config.model,
            "base_url": self.config.base_url,
            "ready": False,
        }
        try:
            import requests  # noqa: F401
        except ImportError:
            base["error"] = "the 'requests' package is not installed"
            base["hint"] = "pip install requests"
            return base

        import requests
        try:
            response = self.session.get(
                self._url("/models"), headers=self._headers(), timeout=10.0)
        except requests.RequestException as exc:
            base["error"] = str(exc)[:200]
            base["hint"] = self._unreachable_hint()
            return base

        if response.status_code != 200:
            base["error"] = f"HTTP {response.status_code}"
            base["hint"] = _status_hint(response.status_code) \
                or self._unreachable_hint()
            return base

        base["ready"] = True
        try:
            payload = response.json()
            served = [
                str(item.get("id")) for item in (payload.get("data") or [])
                if isinstance(item, dict)
            ]
            base["served_models"] = served[:20]
            if served and self.config.model not in served:
                base["warning"] = (
                    f"The server is reachable but does not list "
                    f"'{self.config.model}'. It serves: "
                    + ", ".join(served[:5])
                )
        except ValueError:
            pass
        return base


class MockDirector:
    """Decides by rule, and says so everywhere.

    Exists so this package can be tested and exercised without a model, and it
    is deliberately *not* a good editor: it applies four obvious rules to the
    context and stamps ``mock`` on everything downstream. A mock plan that read
    as a real one would be the worst artifact in this system -- every decision
    in it would look considered.

    The rules are the ones a threshold could already have found, which is the
    point: if a mock plan and a model plan look the same, the model added
    nothing.
    """

    name = "mock-director"

    def __init__(self, config: Optional[DirectorConfig] = None,
                 responses: Optional[list] = None):
        self.config = (config or DirectorConfig()).validated()
        #: Canned answers, for testing the parser and the failure paths.
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def complete(self, *, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        if self.responses:
            answer = self.responses.pop(0)
            return answer if isinstance(answer, str) else json.dumps(answer)
        return json.dumps(self._derive(user))

    def _derive(self, user: str) -> dict:
        """Four rules over the candidate list in the prompt.

        Parsed back out of the rendered context rather than taken from the
        object, because that exercises the same path a real model takes: if
        the context is unreadable, the mock produces nothing too, and that is
        a signal worth having.
        """
        decisions: list[dict] = []
        candidates = _parse_candidates(user)

        for index, entry in enumerate(candidates):
            segment_id, verdict, has_speech, dead = entry
            if dead:
                decisions.append(_mock_decision(
                    segment_id, "cut", "dead_air",
                    "Silence with nothing on screen.", 0.9,
                    "removes_a_dull_stretch"))
            elif verdict == "keep" and index == 0:
                decisions.append(_mock_decision(
                    segment_id, "hook", "hook_strength",
                    "First strong moment; open on it.", 0.6,
                    "opens_a_question", order=0))
            elif verdict == "keep":
                decisions.append(_mock_decision(
                    segment_id, "keep", "pacing",
                    "The rule-based pass would keep this and nothing "
                    "contradicts it.", 0.6, "keeps_momentum"))
            elif verdict == "speed_up" and not has_speech:
                decisions.append(_mock_decision(
                    segment_id, "speed_up", "boring_repetition",
                    "Low-value silent footage.", 0.65,
                    "removes_a_dull_stretch", speed=2.0))
            else:
                decisions.append(_mock_decision(
                    segment_id, "keep", "continuity",
                    "Kept for continuity: somebody is talking over it.", 0.55,
                    "keeps_momentum"))

        return {
            "approach": (
                "MOCK DIRECTOR: four fixed rules over the candidate list. "
                "This is not an editorial judgement and must not be read as "
                "one."
            ),
            "decisions": decisions,
        }

    def health(self) -> dict:
        return {
            "backend": "mock",
            "model": self.name,
            "ready": True,
            "note": "the mock director decides by rule and stamps every "
                    "artifact it touches",
        }


def _mock_decision(
    segment_id: str, action: str, category: str, text: str,
    confidence: float, effect: str, *, speed: float = 1.0, order: int = 100,
) -> dict:
    entry = {
        "segment_ids": [segment_id],
        "action": action,
        "reason": {"category": category, "text": "MOCK: " + text,
                   "style_rule": ""},
        "confidence": confidence,
        "priority": 0.5,
        "viewer_effect": effect,
        "evidence": [segment_id],
        "order": order,
    }
    if speed != 1.0:
        entry["speed"] = speed
    return entry


def _parse_candidates(user: str) -> list:
    """``[(segment_id, heuristic verdict, has speech, dead air), ...]``."""
    out: list = []
    in_section = False
    pending: Optional[list] = None
    for line in user.splitlines():
        if line.startswith("# CANDIDATE RANGES"):
            in_section = True
            continue
        if in_section and line.startswith("# "):
            break
        if not in_section:
            continue
        stripped = line.strip()
        if stripped.startswith("said:"):
            if pending is not None:
                pending[2] = True
            continue
        if not stripped.startswith("["):
            continue
        if pending is not None:
            out.append(tuple(pending))
        segment_id = stripped[1:stripped.find("]")] if "]" in stripped else ""
        if not segment_id:
            pending = None
            continue
        verdict = "keep"
        if "heur:" in stripped:
            verdict = stripped.split("heur:")[-1].split()[0]
        pending = [segment_id, verdict, False, "DEAD AIR" in stripped]
    if pending is not None:
        out.append(tuple(pending))
    return out


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _json_of(response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelError(
            "The director server returned a non-JSON response",
            detail={"reason": str(exc), "body": (response.text or "")[:300]},
        ) from None
    return payload if isinstance(payload, dict) else {}


def _text_of(payload: dict) -> str:
    """The assistant's text out of a chat-completions envelope."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelError(
            "The director server returned no choices",
            hint="The server may have hit its context limit. Lower "
                 "--context-chars or --max-segments.",
            detail={"payload": str(payload)[:300]},
        )
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) \
        else {}
    content = message.get("content") or first.get("text") or ""
    if isinstance(content, list):
        # Some servers return content as a list of typed parts.
        content = "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict)
        )
    if not str(content).strip():
        finish = first.get("finish_reason") or ""
        raise ModelError(
            "The director model returned an empty answer",
            hint=("The answer hit the token limit -- raise --max-tokens."
                  if finish == "length" else
                  "Check the served model is instruction-tuned."),
            detail={"finish_reason": finish},
        )
    return str(content)


def _status_hint(status: int) -> str:
    if status in (401, 403):
        return ("Set EDITING_DIRECTOR_API_KEY, or --api-key, if the endpoint "
                "requires one.")
    if status == 404:
        return ("Check EDITING_DIRECTOR_BASE_URL points at the API root "
                "(usually ending in /v1) and that the model name exists.")
    if status == 413:
        return ("The prompt was too large. Lower --context-chars or "
                "--max-segments.")
    if status == 429:
        return "Rate limited. Wait, or lower the concurrency of your run."
    return ""


def _is_local(url: str) -> bool:
    text = (url or "").lower()
    return "localhost" in text or "127.0.0.1" in text or "0.0.0.0" in text


def build_model(config: DirectorConfig) -> DirectorModel:
    """The backend this configuration asks for.

    Anything unrecognised gets the real one, which then fails loudly if it
    cannot connect. Falling back to the mock would silently replace an
    editor's judgement with four hard-coded rules.
    """
    clean = config.validated()
    if clean.backend == "mock":
        return MockDirector(clean)
    return OpenAICompatibleDirector(clean)


def check(config: DirectorConfig) -> dict:
    """Could a director pass run right now? Calls no model."""
    clean = config.validated()
    health = build_model(clean).health()
    health["config_warnings"] = clean.warnings
    return health

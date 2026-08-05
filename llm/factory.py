"""
Single source of truth for turning a provider name into an LLM client.

Every place in the codebase that used to hand-roll an
``if provider == "ollama": ... elif provider == "gemini": ...`` chain
(``brain/brain.py``, ``memory/long_memory.py``) should call
``make_llm_client`` instead, so adding a new provider is a one-file change.
"""
from __future__ import annotations

from .ollama_client import OllamaClient
from .gemini_client import GeminiClient
from .openrouter_client import OpenRouterClient
from .openai_client import OpenAIClient
from .anthropic_client import AnthropicClient
from .groq_client import GroqClient
from .deepseek_client import DeepSeekClient

# Provider key (lowercase, matches config.settings.llm_provider /
# apply_runtime_overrides) -> client class.
PROVIDER_CLIENTS = {
    "ollama": OllamaClient,
    "gemini": GeminiClient,
    "openrouter": OpenRouterClient,
    "openai": OpenAIClient,
    "anthropic": AnthropicClient,
    "groq": GroqClient,
    "deepseek": DeepSeekClient,
}

# Display name (as shown in Settings) -> provider key.
DISPLAY_TO_PROVIDER_KEY = {
    "Ollama": "ollama",
    "OpenRouter": "openrouter",
    "Gemini": "gemini",
    "OpenAI": "openai",
    "Anthropic": "anthropic",
    "Groq": "groq",
    "DeepSeek": "deepseek",
}

PROVIDER_DISPLAY_NAMES = list(DISPLAY_TO_PROVIDER_KEY.keys())


def make_llm_client(provider: str, *, model: str | None = None):
    """Build an LLM client for the given provider key (e.g. "ollama",
    "openrouter", ...; case-insensitive, also accepts the display name
    e.g. "OpenRouter"). Falls back to Ollama for an unknown provider
    rather than raising, since Ollama needs no API key and always works
    if the local server is up.
    """
    key = (provider or "").strip().lower()
    if key not in PROVIDER_CLIENTS:
        key = DISPLAY_TO_PROVIDER_KEY.get((provider or "").strip(), "ollama").lower()
    client_cls = PROVIDER_CLIENTS.get(key, OllamaClient)
    return client_cls(model=model)

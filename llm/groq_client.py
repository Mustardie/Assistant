from config.settings import settings
from .openai_compatible_base import (
    OpenAICompatibleClient,
    OpenAICompatibleConfigurationError as GroqConfigurationError,
    OpenAICompatibleRequestError as GroqRequestError,
)

__all__ = ["GroqClient", "GroqConfigurationError", "GroqRequestError"]


class GroqClient(OpenAICompatibleClient):
    """Thin REST wrapper around Groq's OpenAI-compatible chat/completions
    endpoint (fast Llama/Mixtral/etc inference)."""

    API_URL = "https://api.groq.com/openai/v1/chat/completions"
    PROVIDER_LABEL = "Groq"
    ENV_KEY_NAME = "GROQ_API_KEY"
    DOCS_URL = "https://console.groq.com/keys"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        super().__init__(
            model=model or settings.groq_model,
            api_key=api_key or settings.groq_api_key,
        )

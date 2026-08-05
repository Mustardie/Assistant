from config.settings import settings
from .openai_compatible_base import (
    OpenAICompatibleClient,
    OpenAICompatibleConfigurationError as DeepSeekConfigurationError,
    OpenAICompatibleRequestError as DeepSeekRequestError,
)

__all__ = ["DeepSeekClient", "DeepSeekConfigurationError", "DeepSeekRequestError"]


class DeepSeekClient(OpenAICompatibleClient):
    """Thin REST wrapper around DeepSeek's OpenAI-compatible chat/completions
    endpoint."""

    API_URL = "https://api.deepseek.com/chat/completions"
    PROVIDER_LABEL = "DeepSeek"
    ENV_KEY_NAME = "DEEPSEEK_API_KEY"
    DOCS_URL = "https://platform.deepseek.com/api_keys"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        super().__init__(
            model=model or settings.deepseek_model,
            api_key=api_key or settings.deepseek_api_key,
        )

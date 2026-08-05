from config.settings import settings
from .openai_compatible_base import (
    OpenAICompatibleClient,
    OpenAICompatibleConfigurationError as OpenAIConfigurationError,
    OpenAICompatibleRequestError as OpenAIRequestError,
)

__all__ = ["OpenAIClient", "OpenAIConfigurationError", "OpenAIRequestError"]


class OpenAIClient(OpenAICompatibleClient):
    """Thin REST wrapper around OpenAI's chat/completions endpoint."""

    API_URL = "https://api.openai.com/v1/chat/completions"
    PROVIDER_LABEL = "OpenAI"
    ENV_KEY_NAME = "OPENAI_API_KEY"
    DOCS_URL = "https://platform.openai.com/api-keys"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        super().__init__(
            model=model or settings.openai_model,
            api_key=api_key or settings.openai_api_key,
        )

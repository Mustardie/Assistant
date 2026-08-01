"""Provider factory: pick the configured research provider."""

import logging

from config.settings import settings
from research.providers.base import ResearchProvider, ResearchProviderError
from research.providers.tavily import TavilyProvider

logger = logging.getLogger(__name__)


def get_provider() -> ResearchProvider:
    """Instantiate the provider named by settings.research_provider."""
    provider_name = (settings.research_provider or "tavily").lower()

    if provider_name == "tavily":
        if not settings.tavily_api_key:
            raise ResearchProviderError(
                "TAVILY_API_KEY is not set. Add your Tavily API key to the "
                ".env file (get one free at https://tavily.com)."
            )
        logger.info("Research provider: tavily")
        return TavilyProvider(api_key=settings.tavily_api_key)

    raise ResearchProviderError(
        f"Unknown research provider '{settings.research_provider}'. "
        "Supported: tavily"
    )

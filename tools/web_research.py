import logging

from config.settings import settings
from llm.openrouter_client import OpenRouterClient, OpenRouterConfigurationError
from llm.gemini_client import GeminiClient
from llm.ollama_client import OllamaClient

logger = logging.getLogger(__name__)

_RESEARCH_SYSTEM_PROMPT = """
Answer the user's question directly and concisely using current web results.
Keep it under 200 words. State only things you actually found -- never invent
facts, links, or numbers you didn't see in the search results.
"""


def _get_client():
    provider = settings.llm_provider
    if provider == "gemini":
        return GeminiClient()
    if provider == "ollama":
        return OllamaClient()
    return OpenRouterClient()


def web_research(query: str) -> dict:
    """Look something up live on the web (Gemini/OpenRouter have built-in
    search grounding; Ollama falls back to training-data answer)."""
    try:
        client = _get_client()
        answer, sources = client.chat_with_search(_RESEARCH_SYSTEM_PROMPT, query)
        return {
            "success": True,
            "query": query,
            "answer": answer,
            "sources": sources,
        }
    except OpenRouterConfigurationError as error:
        return {"success": False, "query": query, "error": str(error)}
    except Exception as error:
        logger.exception("web_research failed for query=%s", query)
        return {"success": False, "query": query, "error": str(error)}
import logging

from llm.openrouter_client import OpenRouterClient, OpenRouterConfigurationError

logger = logging.getLogger(__name__)

_RESEARCH_SYSTEM_PROMPT = """
Answer the user's question directly and concisely using current web results.
Keep it under 200 words. State only things you actually found -- never invent
facts, links, or numbers you didn't see in the search results.
"""


_or_client = None


def _get_client():
    global _or_client
    if _or_client is None:
        _or_client = OpenRouterClient()
    return _or_client


def web_research(query: str) -> dict:
    """Look something up live on the web via OpenRouter's web-search plugin
    and return a short, cited answer. Used by the agent when it needs
    current information or needs to learn how to do something it doesn't
    already know how to do (e.g. "how to script Blender via its Python API").
    This is NOT a way to perform an action -- only to learn about one."""
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
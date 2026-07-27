import logging
from typing import List, Optional

from config.settings import settings
from recommendation.llm_client import LLMClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You generate high-quality search queries for a recommendation engine.

Given a user request, produce diverse search queries that explore different
angles of intent: topic, style, quality signals, audience, and format.

Rules:
1. Return ONLY valid JSON.
2. Do not include markdown.
3. Queries must be concise and searchable.
4. Avoid duplicate or near-duplicate queries.
5. Match the content type implied by the request (tutorial, funny clip, music, documentary, etc.).

Output format:
{
  "queries": ["query 1", "query 2", "query 3"]
}
"""


class QueryGenerator:
    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm or LLMClient()

    def generate(
        self,
        user_request: str,
        min_queries: Optional[int] = None,
        max_queries: Optional[int] = None,
    ) -> List[str]:
        min_queries = min_queries or settings.recommendation_queries_min
        max_queries = max_queries or settings.recommendation_queries_max

        prompt = f"""
User request:
{user_request}

Generate between {min_queries} and {max_queries} YouTube search queries.
"""

        logger.info("Generating search queries for request: %s", user_request)

        try:
            payload = self.llm.complete_json(SYSTEM_PROMPT, prompt)
        except Exception as exc:
            # The LLM call itself failed (rate limit, provider outage,
            # timeout, etc.) rather than returning malformed data. Don't let
            # this kill the whole recommendation -- fall through to the same
            # "use the raw request as the only query" path below that
            # already exists for malformed/empty responses.
            logger.warning(
                "Query generation LLM call failed (%s); falling back to the raw request as the only query.",
                exc,
            )
            payload = {}

        queries = payload.get("queries", []) if isinstance(payload, dict) else []

        if not isinstance(queries, list):
            logger.warning("Query generator returned invalid query list; falling back to the raw request.")
            queries = []

        cleaned = []
        seen = set()

        for query in queries:
            if not isinstance(query, str):
                continue

            normalized = " ".join(query.split()).strip()
            key = normalized.lower()

            if not normalized or key in seen:
                continue

            seen.add(key)
            cleaned.append(normalized)

        if len(cleaned) < min_queries:
            logger.warning(
                "Only %s valid queries generated; minimum is %s",
                len(cleaned),
                min_queries,
            )

        if not cleaned:
            fallback = user_request.strip()
            if not fallback:
                raise RuntimeError("Could not generate any search queries")
            cleaned = [fallback]

        result = cleaned[:max_queries]
        logger.info(
            "Generated search queries (%s): %s",
            len(result),
            result,
        )
        if len(result) < min_queries:
            logger.warning(
                "Generated search query count %s below minimum %s",
                len(result),
                min_queries,
            )
        return result
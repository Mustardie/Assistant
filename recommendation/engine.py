import logging
from typing import Callable, Optional

from config.settings import settings
from recommendation.llm_client import LLMClient
from recommendation.models import RecommendationResult
from recommendation.query_generator import QueryGenerator
from recommendation.ranker import Ranker
from recommendation.sources.base import ContentSource

logger = logging.getLogger(__name__)


class RecommendationEngine:
    def __init__(
        self,
        source: ContentSource,
        query_generator: Optional[QueryGenerator] = None,
        ranker: Optional[Ranker] = None,
        llm_client: Optional[LLMClient] = None,
        pool_size: Optional[int] = None,
        top_n: Optional[int] = None,
    ):
        self.source = source
        self.llm_client = llm_client or LLMClient()
        self.query_generator = query_generator or QueryGenerator(self.llm_client)
        self.ranker = ranker or Ranker(self.llm_client)
        self.pool_size = pool_size or settings.recommendation_pool_size
        self.top_n = top_n or settings.recommendation_top_n

    def recommend(
        self,
        user_request: str,
        top_n: Optional[int] = None,
        target_pool: Optional[int] = None,
        auto_play: bool = False,
        play_callback: Optional[Callable[[str], str]] = None,
    ) -> RecommendationResult:
        request = user_request.strip()
        if not request:
            raise ValueError("Recommendation request cannot be empty")

        top_n = top_n or self.top_n
        target_pool = target_pool or self.pool_size

        logger.debug(
            "Starting recommendation pipeline source=%s request=%s",
            self.source.source_name,
            request,
        )

        queries = self.query_generator.generate(request)
        logger.debug("Generated search queries: %s", queries)

        matched_queries = self.source.search_candidates(
            queries=queries,
            target_pool=target_pool,
        )

        if not matched_queries:
            raise RuntimeError("No candidate videos were found for this request")

        logger.debug(
            "Recommendation pipeline candidate count=%s",
            len(matched_queries),
        )

        candidates = self.source.enrich_metadata(list(matched_queries.keys()))
        if not candidates:
            raise RuntimeError("Could not enrich metadata for candidate videos")

        for candidate in candidates:
            candidate.matched_query = matched_queries.get(candidate.id, "")

        ranked = self.ranker.rank(
            user_request=request,
            candidates=candidates,
            top_n=top_n,
        )

        top_pick = ranked[0].candidate if ranked else None
        played = False
        play_message = ""

        if auto_play and top_pick and play_callback:
            logger.info(
                "Chosen video: rank=1 id=%s title=%r url=%s fallback_used=false",
                top_pick.id,
                top_pick.title,
                top_pick.url,
            )
            play_message = play_callback(top_pick.url)
            played = True

        result = RecommendationResult(
            user_request=request,
            search_queries=queries,
            candidate_count=len(candidates),
            ranked=ranked,
            top_pick=top_pick,
            played=played,
            play_message=play_message,
        )

        logger.info(
            "Recommendation pipeline complete candidates=%s ranked=%s played=%s",
            result.candidate_count,
            len(result.ranked),
            result.played,
        )
        logger.info(
            "Selected video summary: %s",
            result.summary(),
        )
        return result

import logging

from config.settings import settings
from recommendation.engine import RecommendationEngine
from recommendation.errors import RecommendationConfigurationError
from recommendation.sources.youtube import YouTubeSource
from tools.browser_tool import browser

logger = logging.getLogger(__name__)


class YouTubeSkill:
    def __init__(self):
        self._engine = None

    def _require_api_key(self):
        if settings.youtube_api_key:
            return

        message = (
            "YouTube recommendations require YOUTUBE_API_KEY. "
            "Copy .env.example to .env and add your YouTube Data API v3 key. "
            "Browser search fallback is disabled unless "
            "YOUTUBE_ALLOW_BROWSER_FALLBACK=true."
        )
        logger.error("API key validation failed: %s", message)
        raise RecommendationConfigurationError(message)

    def _get_engine(self) -> RecommendationEngine:
        self._require_api_key()

        if self._engine is None:
            self._engine = RecommendationEngine(
                source=YouTubeSource(),
            )
        return self._engine

    def _browser_fallback(self, request: str) -> str:
        logger.warning(
            "Fallback decision: browser fallback enabled for request=%r",
            request,
        )

        query = request.strip() or "youtube"
        logger.info("Fallback search query: %s", query)
        browser.youtube_search(query)
        play_message = browser.youtube_play_first_result()

        logger.info("Fallback decision: browser fallback executed")
        return (
            f"FALLBACK USED: browser search played the first result for '{query}'.\n"
            f"{play_message}\n"
            "Set YOUTUBE_API_KEY to use the smart recommendation pipeline."
        )

    def recommend(self, request: str):
        logger.info("YouTube recommend request: %s", request)

        if not settings.youtube_api_key:
            logger.warning(
                "Fallback decision: missing API key, no fallback allowed unless explicitly enabled"
            )
            self._require_api_key()

        result = self._get_engine().recommend(
            user_request=request,
            auto_play=True,
            play_callback=browser.youtube_play_url,
        )

        logger.info(
            "Selected video: rank=1 title=%r url=%s fallback_used=false",
            result.top_pick.title if result.top_pick else None,
            result.top_pick.url if result.top_pick else None,
        )

        return result.summary()

    def play_url(self, url: str):
        logger.info("Play URL requested: %s", url)
        return browser.youtube_play_url(url)


youtube = YouTubeSkill()

import logging
import re
import time
from typing import Dict, List, Optional

import requests

from config.settings import settings
from recommendation.models import MediaCandidate
from recommendation.sources.base import ContentSource

_SEARCH_CACHE = {}
_SEARCH_CACHE_TTL = 600
_VIDEO_CACHE = {}
_VIDEO_CACHE_TTL = 600


def _cached_search(search_fn, query, max_results):
    cache_key = (query, max_results)
    now = time.time()
    if cache_key in _SEARCH_CACHE:
        cached_time, cached_items = _SEARCH_CACHE[cache_key]
        if now - cached_time < _SEARCH_CACHE_TTL:
            return cached_items
    items = search_fn(query, max_results)
    _SEARCH_CACHE[cache_key] = (now, items)
    return items


def _cached_video_details(fetch_fn, video_ids):
    cache_key = tuple(sorted(video_ids))
    now = time.time()
    if cache_key in _VIDEO_CACHE:
        cached_time, cached_items = _VIDEO_CACHE[cache_key]
        if now - cached_time < _VIDEO_CACHE_TTL:
            return cached_items
    items = fetch_fn(video_ids)
    _VIDEO_CACHE[cache_key] = (now, items)
    return items

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
ISO8601_DURATION = re.compile(
    r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def parse_iso8601_duration(value: Optional[str]) -> Optional[int]:
    if not value:
        return None

    match = ISO8601_DURATION.fullmatch(value)
    if not match:
        return None

    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class YouTubeSource(ContentSource):
    def __init__(self, api_key: Optional[str] = None, timeout: int = 20):
        self.api_key = api_key or settings.youtube_api_key
        self.timeout = timeout

        if not self.api_key:
            raise ValueError(
                "YOUTUBE_API_KEY is not set. Add it to your environment or .env file."
            )

    @property
    def source_name(self) -> str:
        return "youtube"

    def search_candidates(
        self,
        queries: List[str],
        max_per_query: int = 15,
        target_pool: int = 80,
    ) -> Dict[str, str]:
        discovered: Dict[str, str] = {}

        for query in queries:
            if len(discovered) >= target_pool:
                break

            logger.info("Searching YouTube for query: %s", query)

            try:
                items = _cached_search(self._search_query, query, max_per_query)
            except requests.RequestException as exc:
                logger.exception("YouTube search failed for query=%s", query)
                raise RuntimeError(f"YouTube search failed for '{query}': {exc}") from exc

            for item in items:
                video_id = item.get("id", {}).get("videoId")
                if not video_id or video_id in discovered:
                    continue

                discovered[video_id] = query

                if len(discovered) >= target_pool:
                    break

        logger.info("Collected %s unique candidate IDs", len(discovered))
        return discovered

    def enrich_metadata(self, candidate_ids: List[str]) -> List[MediaCandidate]:
        if not candidate_ids:
            return []

        candidates: List[MediaCandidate] = []

        for start in range(0, len(candidate_ids), 50):
            batch = candidate_ids[start:start + 50]

            try:
                items = _cached_video_details(self._fetch_video_details, batch)
            except requests.RequestException as exc:
                logger.exception("YouTube metadata fetch failed")
                raise RuntimeError(f"YouTube metadata fetch failed: {exc}") from exc

            for item in items:
                candidate = self._to_candidate(item)
                if candidate is not None:
                    candidates.append(candidate)

        logger.info("Enriched metadata for %s candidates", len(candidates))
        return candidates

    def _search_query(self, query: str, max_results: int) -> List[dict]:
        params = {
            "part": "snippet",
            "type": "video",
            "q": query,
            "maxResults": min(max_results, 50),
            "safeSearch": "none",
            "relevanceLanguage": "en",
            "key": self.api_key,
        }

        logger.debug(
            "YouTube API search request query=%r maxResults=%s",
            query,
            min(max_results, 50),
        )

        response = requests.get(
            SEARCH_URL,
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()

        if "error" in payload:
            message = payload["error"].get("message", "Unknown YouTube API error")
            raise RuntimeError(message)

        items = payload.get("items", [])
        logger.debug(
            "YouTube API search response query=%r result_count=%s",
            query,
            len(items),
        )
        return items

    def _fetch_video_details(self, video_ids: List[str]) -> List[dict]:
        logger.debug(
            "YouTube API videos.list request count=%s ids=%s",
            len(video_ids),
            video_ids[:5],
        )

        params = {
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(video_ids),
            "key": self.api_key,
        }

        response = requests.get(
            VIDEOS_URL,
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()

        if "error" in payload:
            message = payload["error"].get("message", "Unknown YouTube API error")
            raise RuntimeError(message)

        items = payload.get("items", [])
        logger.debug(
            "YouTube API videos.list response count=%s",
            len(items),
        )
        return items

    def _to_candidate(self, item: dict) -> Optional[MediaCandidate]:
        video_id = item.get("id")
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        content_details = item.get("contentDetails", {})

        title = snippet.get("title", "").strip()
        if not video_id or not title:
            return None

        thumbnails = snippet.get("thumbnails", {})
        aspect_ratio = None
        default_thumb = thumbnails.get("default") or thumbnails.get("medium") or thumbnails.get("high") or {}
        if default_thumb.get("width") and default_thumb.get("height"):
            try:
                aspect_ratio = float(default_thumb["width"]) / float(default_thumb["height"])
            except (TypeError, ValueError):
                aspect_ratio = None

        return MediaCandidate(
            id=video_id,
            source=self.source_name,
            title=title,
            creator=snippet.get("channelTitle", "").strip(),
            description=snippet.get("description", "").strip(),
            published_at=snippet.get("publishedAt"),
            duration_seconds=parse_iso8601_duration(
                content_details.get("duration")
            ),
            view_count=parse_int(statistics.get("viewCount")),
            like_count=parse_int(statistics.get("likeCount")),
            tags=list(snippet.get("tags", [])),
            url=f"https://www.youtube.com/watch?v={video_id}",
            channel_id=snippet.get("channelId", "").strip(),
            language=snippet.get("defaultAudioLanguage", "") or snippet.get("defaultLanguage", "") or snippet.get("language", ""),
            thumbnail_aspect_ratio=aspect_ratio,
            category_id=snippet.get("categoryId", ""),
            extra={
                "channel_description": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""),
            },
        )

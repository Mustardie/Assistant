import logging
from typing import Dict

from googleapiclient.discovery import build

from youtube_auth import ensure_youtube_auth

logger = logging.getLogger(__name__)


def get_watch_history(max_items: int = 200) -> Dict[str, dict]:
    try:
        creds = ensure_youtube_auth()
        youtube = build("youtube", "v3", credentials=creds)
    except Exception as exc:
        logger.warning("YouTube watch history unavailable: %s", exc)
        return {}

    watch_history: Dict[str, dict] = {}
    next_page_token = None

    while len(watch_history) < max_items:
        try:
            request = youtube.playlistItems().list(
                part="snippet,contentDetails",
                playlistId="HL",
                maxResults=min(50, max_items - len(watch_history)),
                pageToken=next_page_token,
            )
            response = request.execute()
        except Exception as exc:
            logger.warning("Failed to fetch YouTube watch history: %s", exc)
            break

        for item in response.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            if not video_id or video_id in watch_history:
                continue

            watch_history[video_id] = {
                "watched_at": item.get("snippet", {}).get("publishedAt"),
                "title": item.get("snippet", {}).get("title", ""),
                "description": item.get("snippet", {}).get("description", ""),
            }

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    logger.info("Loaded %s watch history items", len(watch_history))
    return watch_history

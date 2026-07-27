import json
import logging
from pathlib import Path
from typing import Set

from googleapiclient.discovery import build

from youtube_auth import ensure_youtube_auth

logger = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).resolve().parent / "subscriptions.json"


def get_subscriptions() -> Set[str]:
    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                return set(payload)
        except Exception as exc:
            logger.warning("Failed to load cached subscriptions: %s", exc)

    try:
        creds = ensure_youtube_auth()
        youtube = build("youtube", "v3", credentials=creds)
    except Exception as exc:
        logger.warning("YouTube OAuth is not available for subscriptions: %s", exc)
        return set()

    subscriptions = set()
    next_page_token = None

    while True:
        try:
            request = youtube.subscriptions().list(
                part="snippet",
                mine=True,
                maxResults=50,
                pageToken=next_page_token,
            )
            response = request.execute()
        except Exception as exc:
            logger.warning("Failed to fetch YouTube subscriptions: %s", exc)
            return set()

        for item in response.get("items", []):
            channel_id = item.get("snippet", {}).get("resourceId", {}).get("channelId")
            if channel_id:
                subscriptions.add(channel_id)

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    CACHE_PATH.write_text(json.dumps(sorted(subscriptions), indent=2), encoding="utf-8")
    logger.info("Saved %s subscriptions to %s", len(subscriptions), CACHE_PATH)
    return subscriptions

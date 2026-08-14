"""Apple Music adapter -- search + playback via Apple's public iTunes
Search API and deep links.

Apple has no open playback-control API for third parties, so this adapter
provides what is genuinely possible without hacks:

    * search_media -- full catalog search (songs, albums, playlists) via
      the public, key-free iTunes Search API.
    * play_media   -- opens the best match in the Apple Music web player /
      the default browser (which the Apple Music app on Windows registers
      itself with).

No account or token is required; the adapter is always available.
"""

from __future__ import annotations

import logging

from adapters.api import ApiClient, ApiError
from adapters.base import BaseAdapter

logger = logging.getLogger(__name__)


class AppleMusicAdapter(BaseAdapter):
    name = "apple_music"
    display_name = "Apple Music"
    authentication = "none"
    description = ("Apple Music catalog search and playback via Apple's "
                   "public iTunes Search API. No account or token needed: "
                   "Nova can find songs and albums, and playing opens them "
                   "in Apple Music.")
    capabilities = ["search_media", "play_media"]

    def __init__(self):
        super().__init__()
        self._client = ApiClient("https://itunes.apple.com")

    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        return {"status": "connected",
                "message": "Apple Music catalog search is available."}

    def connect(self) -> dict:
        return {"success": True, "message": "Apple Music needs no setup."}

    def disconnect(self) -> dict:
        return {"success": True, "message": "Apple Music disconnected."}

    # ------------------------------------------------------------------ #
    def search_media(self, query, **kwargs):
        limit = min(int(kwargs.get("limit") or 10), 25)
        tracks, albums = [], []
        # The iTunes Search API rejects comma-separated entity values, and
        # playlist search is disabled upstream -- song + album are reliable.
        for entity in ("song", "album"):
            try:
                data = self._client.get(
                    "/search",
                    params={"term": query, "entity": entity,
                            "limit": str(limit)})
            except ApiError as exc:
                logger.warning("Apple Music %s search failed: %s",
                               entity, exc)
                continue
            for item in (data.get("results") or []):
                name = (item.get("trackName")
                        or item.get("collectionName") or "")
                url = (item.get("trackViewUrl")
                       or item.get("collectionViewUrl") or "")
                artist = item.get("artistName", "")
                if entity == "song":
                    tracks.append({"name": name, "artist": artist,
                                   "url": url, "type": "track"})
                else:
                    albums.append({"name": name, "artist": artist,
                                   "url": url, "type": "album"})
        if not (tracks or albums):
            return self._fail(f"Apple Music search failed for '{query}'.")
        return self._ok(tracks=tracks, albums=albums, playlists=[],
                        count=len(tracks))

    def play_media(self, query=None, url=None, **kwargs):
        """Play a track/album/playlist. With a 'query', searches and plays
        the best match; with 'url', opens that Apple Music link directly."""
        import webbrowser

        target = url
        if not target and query:
            found = self.search_media(query, limit=5)
            if not found.get("success"):
                return found
            target = _first_url(found)
            if not target:
                return self._fail(
                    f"No Apple Music result found for '{query}'.")
        if not target:
            return self._fail("play_media needs a 'query' or 'url'.")
        try:
            webbrowser.open(target)
        except Exception as exc:
            return self._fail(f"Could not open Apple Music: {exc}")
        return self._ok(url=target, opened=True,
                        message="Opened in Apple Music.")


def _first_url(result: dict) -> str | None:
    for key in ("tracks", "albums"):
        items = result.get(key) or []
        if items and items[0].get("url"):
            return items[0]["url"]
    return None
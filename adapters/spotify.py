"""Spotify adapter -- playback + search via the official Spotify Web API
(OAuth)."""

from __future__ import annotations

import logging

from adapters.api import OAuthRESTAdapter

logger = logging.getLogger(__name__)


class SpotifyAdapter(OAuthRESTAdapter):
    name = "spotify"
    display_name = "Spotify"
    description = ("Playback control for Spotify (Play, Pause, Next/Previous, "
                   "Volume, Shuffle, Repeat, Queue) plus search and "
                   "liking/saving tracks. Needs SPOTIFY_CLIENT_ID / "
                   "SPOTIFY_CLIENT_SECRET from a Spotify Developer app; "
                   "Connect opens the consent screen.")
    capabilities = [
        "play_media", "pause_media", "skip_media", "search_media",
        "now_playing", "set_volume", "toggle_shuffle", "toggle_repeat",
        "save_media", "add_to_queue",
    ]

    # ------------------------------------------------------------------ #
    def _build_oauth(self):
        from connections.oauth import OAuthConfig, OAuthHelper

        config = OAuthConfig(
            service_name="spotify",
            client_id=_env("SPOTIFY_CLIENT_ID"),
            client_secret=_env("SPOTIFY_CLIENT_SECRET"),
            auth_url="https://accounts.spotify.com/authorize",
            token_url="https://accounts.spotify.com/api/token",
            scopes=[
                "user-read-playback-state",
                "user-modify-playback-state",
                "user-read-currently-playing",
                "user-library-modify",
                "playlist-modify-public",
                "playlist-modify-private",
            ],
            redirect_port=8767,
        )
        return OAuthHelper(config)

    def _http(self):
        client = super()._http()
        client.base_url = "https://api.spotify.com/v1"
        return client

    # ------------------------------------------------------------------ #
    def play_media(self, query=None, **kwargs):
        client = self._http()
        if query:
            found = self.search_media(query)
            if not found.get("success"):
                return found
            uri = _first_uri(found)
            if not uri:
                return self._fail("No playable track found for that query.")
            body = {"context_uri": uri} if ":" in uri and not uri.startswith("track") \
                else {"uris": [uri]}
            client.put("/me/player/play", json_body=body)
            return self._ok(query=query, uri=uri)
        client.put("/me/player/play", json_body={})
        return self._ok()

    def pause_media(self, **kwargs):
        self._http().put("/me/player/pause", json_body={})
        return self._ok()

    def skip_media(self, direction="next", **kwargs):
        client = self._http()
        path = "/me/player/next" if direction == "next" else "/me/player/previous"
        client.post(path, json_body={})
        return self._ok(direction=direction)

    def search_media(self, query, **kwargs):
        client = self._http()
        data = client.get("/search",
                          params={"q": query, "type": "track,album,playlist", "limit": "10"})
        tracks = []
        for item in (data.get("tracks") or {}).get("items") or []:
            tracks.append({"name": item.get("name"),
                           "artist": ", ".join(a.get("name") for a in item.get("artists") or []),
                           "uri": item.get("uri"), "type": "track"})
        albums = [{"name": a.get("name"), "uri": a.get("uri"), "type": "album"}
                  for a in (data.get("albums") or {}).get("items") or []]
        playlists = [{"name": p.get("name"), "uri": p.get("uri"), "type": "playlist"}
                     for p in (data.get("playlists") or {}).get("items") or []]
        return self._ok(tracks=tracks, albums=albums, playlists=playlists,
                        count=len(tracks))

    # ------------------------------------------------------------------ #
    # Full playback control
    # ------------------------------------------------------------------ #
    def now_playing(self, **kwargs):
        """What is currently playing (track, artist, progress)."""
        client = self._http()
        data = client.get("/me/player/currently-playing")
        item = data.get("item") or {}
        if not item:
            return self._ok(playing=False, track=None,
                            message="Nothing is playing right now.")
        return self._ok(
            playing=data.get("is_playing", True),
            track=item.get("name"),
            artists=", ".join(a.get("name") for a in item.get("artists") or []),
            album=(item.get("album") or {}).get("name"),
            uri=item.get("uri"),
            progress_ms=data.get("progress_ms"),
            duration_ms=item.get("duration_ms"),
        )

    def set_volume(self, percent, **kwargs):
        """Set playback volume 0-100."""
        try:
            percent = int(percent)
        except (TypeError, ValueError):
            return self._fail("set_volume needs a percent value 0-100.")
        percent = max(0, min(100, percent))
        self._http().put("/me/player/volume",
                         params={"volume_percent": str(percent)})
        return self._ok(percent=percent)

    def toggle_shuffle(self, state=None, **kwargs):
        """state: True/False; omitting it toggles based on the current state."""
        client = self._http()
        if state is None:
            current = client.get("/me/player")
            state = not bool((current or {}).get("shuffle_state"))
        client.put("/me/player/shuffle", params={"state": str(bool(state)).lower()})
        return self._ok(shuffle=bool(state))

    def toggle_repeat(self, state="context", **kwargs):
        """state: off | track | context."""
        if state not in ("off", "track", "context"):
            return self._fail("repeat state must be off, track or context.")
        self._http().put("/me/player/repeat", params={"state": state})
        return self._ok(repeat=state)

    def save_media(self, uri=None, query=None, **kwargs):
        """Save (like) a track to the user's library."""
        client = self._http()
        if uri:
            ids = _uri_to_ids(uri)
        elif query:
            found = self.search_media(query)
            if not found.get("success"):
                return found
            ids = _uri_to_ids(_first_uri(found))
        else:
            return self._fail("save_media needs a track 'uri' or 'query'.")
        if not ids:
            return self._fail("No playable track id could be resolved.")
        client.put("/me/tracks", params={"ids": ",".join(ids)})
        return self._ok(saved=True, ids=ids)

    def add_to_queue(self, uri, **kwargs):
        """Queue a track (or album/playlist) to play next."""
        if not uri:
            return self._fail("add_to_queue needs a track 'uri'.")
        self._http().post("/me/player/queue", params={"uri": uri})
        return self._ok(queued=uri)


def _first_uri(result: dict) -> str | None:
    tracks = result.get("tracks") or []
    if tracks:
        return tracks[0].get("uri")
    albums = result.get("albums") or []
    if albums:
        return albums[0].get("uri")
    playlists = result.get("playlists") or []
    if playlists:
        return playlists[0].get("uri")
    return None


def _uri_to_ids(uri: str) -> list[str]:
    """'spotify:track:abc' -> ['abc']; also accepts a bare id."""
    uri = str(uri or "").strip()
    if not uri:
        return []
    if ":" in uri:
        return [uri.split(":")[-1]]
    return [uri]


def _env(key: str) -> str:
    import os
    return os.getenv(key, "")
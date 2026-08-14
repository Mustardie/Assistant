"""Universal MEDIA tools.

Route to a connected media-capable adapter (Spotify, YouTube, Windows
media keys) via the capability dispatcher; volume/media keys use the
system_tool backend. Keeps the agent from needing per-app media tools.
"""

import logging

logger = logging.getLogger(__name__)


def _dispatch(capability: str, method: str, **kwargs):
    from connections.manager import connection_manager

    adapters = connection_manager.find_adapters_with_capability(capability)
    if not adapters:
        return {
            "success": False,
            "error": f"No connected service supports '{capability}'. "
                     "Check Connections to connect one.",
        }
    errors = []
    for name in adapters:
        adapter = connection_manager.get(name)
        if adapter is None:
            continue
        if not connection_manager.get_status(name).get("connected"):
            continue
        fn = getattr(adapter, method, None)
        if not callable(fn):
            continue
        try:
            result = fn(**kwargs)
        except Exception as exc:
            logger.warning("[Media] %s.%s failed: %s", name, method, exc)
            errors.append(f"{name}: {exc}")
            continue
        if isinstance(result, dict) and result.get("success") is False:
            errors.append(f"{name}: {result.get('error', 'failed')}")
            continue
        return result
    detail = "; ".join(errors) if errors else "no connected adapter responded"
    return {"success": False, "error": f"No connected service could handle '{capability}': {detail}"}


def play_media(query=None, source=None, **kwargs):
    """Play media (track/album/playlist) on a connected media service."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "play_media", None)
            if callable(fn):
                return fn(query=query, **kwargs)
    return _dispatch("play_media", "play_media", query=query, **kwargs)


def pause_media(source=None, **kwargs):
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "pause_media", None)
            if callable(fn):
                return fn(**kwargs)
    return _dispatch("pause_media", "pause_media", **kwargs)


def skip_media(direction="next", source=None, **kwargs):
    """direction: next | prev."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "skip_media", None)
            if callable(fn):
                return fn(direction=direction, **kwargs)
    return _dispatch("skip_media", "skip_media", direction=direction, **kwargs)


def search_media(query, source=None, **kwargs):
    """Search for media on a connected service."""
    if source:
        from connections.manager import connection_manager

        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, "search_media", None)
            if callable(fn):
                return fn(query=query, **kwargs)
    return _dispatch("search_media", "search_media", query=query, **kwargs)


def control_volume(direction, **kwargs):
    """Volume/media-key control: up | down | mute | play | pause | next |
    prev. Uses the local system backend, not a connected app."""
    from tools.system_tool import set_volume

    return set_volume(direction)


def now_playing(source=None, **kwargs):
    """What a connected media service is currently playing."""
    return _routed("now_playing", source, **kwargs)


def set_volume(percent, source=None, **kwargs):
    """Set a connected media service's playback volume (0-100)."""
    return _routed("set_volume", source, percent=percent, **kwargs)


def toggle_shuffle(source=None, **kwargs):
    """Toggle (or set) shuffle on a connected media service."""
    return _routed("toggle_shuffle", source, **kwargs)


def toggle_repeat(state="context", source=None, **kwargs):
    """Set repeat: off | track | context."""
    return _routed("toggle_repeat", source, state=state, **kwargs)


def save_media(uri=None, query=None, source=None, **kwargs):
    """Save (like) a track on a connected media service."""
    return _routed("save_media", source, uri=uri, query=query, **kwargs)


def add_to_queue(uri, source=None, **kwargs):
    """Queue a track to play next on a connected media service."""
    return _routed("add_to_queue", source, uri=uri, **kwargs)


def _routed(method: str, source=None, **kwargs):
    """Run a media method on one named adapter, else try all connected
    adapters that support the capability."""
    from connections.manager import connection_manager

    if source:
        adapter = connection_manager.get(source)
        if adapter is not None and connection_manager.get_status(source).get("connected"):
            fn = getattr(adapter, method, None)
            if callable(fn):
                return fn(**kwargs)
    return _dispatch(method, method, **kwargs)
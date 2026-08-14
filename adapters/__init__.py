"""Adapters package -- thin integration adapters.

Register all built-in adapters into the global ConnectionManager with
`register_all()`. Adapters are lightweight; they declare capabilities and
hand the universal tool layer a uniform method set.

No network call or app access happens at import time.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_all() -> int:
    """Register every built-in adapter into connection_manager. Returns the
    number of adapters registered."""
    from connections.manager import connection_manager

    from .google import GoogleAdapter
    from .microsoft import MicrosoftAdapter
    from .discord import DiscordAdapter
    from .telegram import TelegramAdapter
    from .slack import SlackAdapter
    from .spotify import SpotifyAdapter
    from .notion import NotionAdapter
    from .todoist import TodoistAdapter
    from .whatsapp import WhatsAppAdapter
    from .windows import WindowsAdapter
    from .filesystem import FileSystemAdapter
    from .vscode import VSCodeAdapter
    from .apple_music import AppleMusicAdapter

    adapters = [
        GoogleAdapter(),
        MicrosoftAdapter(),
        DiscordAdapter(),
        TelegramAdapter(),
        SlackAdapter(),
        SpotifyAdapter(),
        NotionAdapter(),
        TodoistAdapter(),
        WhatsAppAdapter(),
        WindowsAdapter(),
        FileSystemAdapter(),
        VSCodeAdapter(),
        AppleMusicAdapter(),
    ]
    count = 0
    for adapter in adapters:
        if connection_manager.register(adapter):
            count += 1
    logger.info("[Adapters] Registered %d adapters.", count)
    return count


__all__ = ["register_all"]
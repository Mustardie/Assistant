"""Events package."""

from .models import (
    AppEvent,
    make_event,
    EVENT_TYPES,
    NEW_MESSAGE,
    NEW_EMAIL,
    NEW_FILE,
    NEW_NOTIFICATION,
    CALENDAR_EVENT,
    APP_OPENED,
    APP_CLOSED,
    DOWNLOAD_COMPLETED,
    SYSTEM,
)
from .dispatcher import EventDispatcher, event_dispatcher

__all__ = [
    "AppEvent", "make_event", "EVENT_TYPES",
    "NEW_MESSAGE", "NEW_EMAIL", "NEW_FILE", "NEW_NOTIFICATION",
    "CALENDAR_EVENT", "APP_OPENED", "APP_CLOSED", "DOWNLOAD_COMPLETED",
    "SYSTEM", "EventDispatcher", "event_dispatcher",
]
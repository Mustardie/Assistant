"""Futuristic, voice-first JARVIS UI package."""

from ui.jarvis.events import JarvisEvent, JarvisEventBus, JarvisEventType
from ui.jarvis.models import JarvisState, WidgetAction, WidgetSpec, WidgetState
from ui.jarvis.registry import WidgetRegistry, build_default_registry
from ui.jarvis.manager import WidgetManager

__all__ = [
    "JarvisEvent",
    "JarvisEventBus",
    "JarvisEventType",
    "JarvisState",
    "WidgetAction",
    "WidgetSpec",
    "WidgetState",
    "WidgetRegistry",
    "WidgetManager",
    "build_default_registry",
]


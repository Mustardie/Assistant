"""Discoverable, declarative capabilities for JARVIS.

This package deliberately sits above the existing trusted tool and connector
layers.  Learned records describe how to compose those primitives; they never
contain executable Python source.
"""

from .models import Capability, CapabilityState, LearnedSkill, Permission
from .service import CapabilityService, default_capability_service

__all__ = [
    "Capability",
    "CapabilityState",
    "LearnedSkill",
    "Permission",
    "CapabilityService",
    "default_capability_service",
]

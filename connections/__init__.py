"""Connections package for Jarvis desktop assistant."""

from .manager import connection_manager, ConnectionManager
from .storage import credential_storage, CredentialStorage

__all__ = [
    "connection_manager",
    "ConnectionManager",
    "credential_storage",
    "CredentialStorage",
]

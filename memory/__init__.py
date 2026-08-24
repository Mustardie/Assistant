"""Memory package with lazy loading for optional model dependencies."""

__all__ = ["MemoryManager"]


def __getattr__(name):
    if name == "MemoryManager":
        from .memory_manager import MemoryManager
        return MemoryManager
    raise AttributeError(name)

__all__ = ["MemoryManager"]

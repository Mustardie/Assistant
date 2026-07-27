import os
from pathlib import Path

APP_NAME = os.getenv("NOVA_APP_NAME", "Nova")
APP_DATA_DIR = os.getenv("NOVA_DATA_DIR")


def get_nova_data_dir() -> Path:
    if APP_DATA_DIR:
        path = Path(APP_DATA_DIR)
    elif os.getenv("LOCALAPPDATA"):
        path = Path(os.getenv("LOCALAPPDATA")) / APP_NAME
    else:
        path = Path.home() / f".{APP_NAME.lower()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_nova_memory_dir() -> Path:
    path = get_nova_data_dir() / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_nova_cache_dir() -> Path:
    path = get_nova_data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_nova_index_dir() -> Path:
    path = get_nova_data_dir() / "index"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_nova_app_file(filename: str) -> Path:
    path = get_nova_data_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

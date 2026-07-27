import json
import os

from config.paths import get_nova_app_file

CACHE_FILE = str(get_nova_app_file("app_cache.json"))


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}

    with open(CACHE_FILE, "r") as f:
        try:
            return json.load(f)
        except:
            return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
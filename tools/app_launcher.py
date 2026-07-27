import json
import subprocess
from rapidfuzz import process, fuzz

from config.paths import get_nova_app_file

APP_DB = str(get_nova_app_file("apps.json"))

_apps_cache = None


def load_apps():
    global _apps_cache
    if _apps_cache is None:
        with open(APP_DB, "r", encoding="utf-8") as f:
            _apps_cache = json.load(f)
    return _apps_cache


def launch_app(query):
    apps = load_apps()

    query = query.lower().strip()

    # Exact match
    if query in apps:
        subprocess.Popen(apps[query]["target"])
        return True, apps[query]["name"]

    # Fuzzy match
    match = process.extractOne(
        query,
        apps.keys(),
        scorer=fuzz.WRatio
    )

    if match and match[1] >= 70:
        app = apps[match[0]]

        subprocess.Popen(app["target"])

        return True, app["name"]

    return False, None